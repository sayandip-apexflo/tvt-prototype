"""Immutable daily report rendering and SendGrid SMTP delivery.

Report content (vehicle entry/exit sessions, attendance sessions) is fetched
live from apexfabric-control's already-aggregated reporting endpoints at
generation time (tvt_edge/apex_client.py ->
apexfabric/control_plane/reporting.py's attendance_report/
vehicle_traffic_report) -- this module only renders that data to CSV/email
and tracks once-daily delivery state so a retry never double-sends.
"""

from __future__ import annotations

import csv
import io
import secrets
import smtplib
import socket
import ssl
import time
from datetime import date, datetime, timezone
from email.message import EmailMessage
from typing import Any, Protocol

from tvt_edge.alerting.email_sender import DeliveryFailure
from tvt_edge.apex_client import ApexClient, ApexUnavailableError
from tvt_edge.reporting.database import ReportingStore, StoredReport
from tvt_edge.reporting.settings import ReportingSettings


def _local_time(value: object, settings: ReportingSettings) -> str:
    if not isinstance(value, (int, float)):
        return ""
    return datetime.fromtimestamp(value, timezone.utc).astimezone(settings.timezone).isoformat()


def _in_window(value: object, settings: ReportingSettings) -> bool:
    """A session with no start time yet (still open) is never window-filtered out."""
    if not isinstance(value, (int, float)):
        return True
    local = datetime.fromtimestamp(value, timezone.utc).astimezone(settings.timezone).time()
    return settings.window_start <= local.replace(tzinfo=None) < settings.window_end


def _duration(seconds: float) -> str:
    whole = max(0, round(seconds))
    hours, remainder = divmod(whole, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def render_vehicle_csv(vehicles: list[dict[str, Any]], settings: ReportingSettings) -> str:
    # Plate text never leaves the loopback API for email (AGENTS.md security
    # invariants: number plates do not belong in email). A vehicle_ref token
    # is assigned per distinct plate within this render so the same vehicle's
    # sessions in the CSV are still recognizable as the same vehicle.
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow([
        "vehicle_ref", "first_detection_time", "last_detection_time",
        "duration_hh_mm_ss", "detection_count", "status",
    ])
    tokens: dict[str, str] = {}
    for item in vehicles:
        plate = item.get("plate_text") or ""
        token = tokens.setdefault(plate, secrets.token_hex(8))
        writer.writerow(
            [
                token,
                _local_time(item.get("first_detection_time"), settings),
                _local_time(item.get("last_detection_time"), settings),
                _duration(item["duration_seconds"]) if item.get("duration_seconds") is not None else "",
                item.get("detection_count", 0),
                item.get("status", ""),
            ]
        )
    return output.getvalue()


def render_attendance_csv(people: list[dict[str, Any]], settings: ReportingSettings) -> str:
    # Person display names never leave the loopback API for email (AGENTS.md
    # security invariants: person names do not belong in email) -- person_id
    # is an internal enrollment identifier, not a name.
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow([
        "person_id", "first_entry_time", "last_exit_time", "visit_count",
        "total_duration_hh_mm_ss", "incomplete_session_count",
    ])
    for item in people:
        writer.writerow(
            [
                item.get("person_id", ""),
                _local_time(item.get("first_entry_time"), settings),
                _local_time(item.get("last_exit_time"), settings),
                item.get("visit_count", 0),
                _duration(item.get("total_duration_seconds") or 0),
                item.get("incomplete_session_count", 0),
            ]
        )
    return output.getvalue()


def _attendance_people_for_window(
    result: dict[str, Any], start: datetime, end: datetime
) -> list[dict[str, Any]]:
    start_timestamp = start.timestamp()
    end_timestamp = end.timestamp()
    people = {
        item["person_id"]: {
            "person_id": item["person_id"],
            "display_name": item.get("display_name"),
            "first_entry_time": None,
            "last_exit_time": None,
            "visit_count": 0,
            "total_duration_seconds": 0.0,
            "incomplete_session_count": 0,
        }
        for item in result.get("people", [])
        if item.get("person_id") and item.get("display_name")
    }
    for session in result.get("sessions", []):
        person = people.get(session.get("person_id"))
        if person is None:
            continue
        entry_time = session.get("entry_time")
        exit_time = session.get("exit_time")
        complete = (
            session.get("status") == "closed"
            and isinstance(entry_time, (int, float))
            and isinstance(exit_time, (int, float))
            and exit_time >= entry_time
        )
        if not complete:
            entry_is_time = isinstance(entry_time, (int, float))
            exit_is_time = isinstance(exit_time, (int, float))
            if entry_is_time and not exit_is_time:
                overlaps_window = entry_time < end_timestamp
            elif exit_is_time and not entry_is_time:
                overlaps_window = start_timestamp <= exit_time < end_timestamp
            else:
                reference_time = entry_time if entry_is_time else exit_time
                overlaps_window = (
                    isinstance(reference_time, (int, float))
                    and start_timestamp <= reference_time < end_timestamp
                )
            if overlaps_window:
                person["incomplete_session_count"] += 1
            continue
        clipped_entry = max(entry_time, start_timestamp)
        clipped_exit = min(exit_time, end_timestamp)
        if clipped_exit <= clipped_entry:
            continue
        person["visit_count"] += 1
        person["total_duration_seconds"] += clipped_exit - clipped_entry
        current_first = person["first_entry_time"]
        current_last = person["last_exit_time"]
        person["first_entry_time"] = clipped_entry if current_first is None else min(current_first, clipped_entry)
        person["last_exit_time"] = clipped_exit if current_last is None else max(current_last, clipped_exit)
    return sorted(people.values(), key=lambda item: (item["display_name"].casefold(), item["person_id"]))


def _fetch(apex: ApexClient, get_report: Any, date_text: str) -> dict[str, Any]:
    try:
        return get_report(date=date_text)
    except ApexUnavailableError as error:
        raise DeliveryFailure("REPORTING_UNAVAILABLE", transient=True) from error


def ensure_vehicle_report(
    store: ReportingStore,
    settings: ReportingSettings,
    apex: ApexClient,
    report_date: date,
    now: datetime | None = None,
) -> StoredReport:
    existing = store.report(report_date, "vehicle_traffic")
    if existing is not None:
        return existing
    start = datetime.combine(report_date, settings.window_start, settings.timezone)
    end = datetime.combine(report_date, settings.window_end, settings.timezone)
    result = _fetch(apex, apex.vehicle_traffic_report, report_date.isoformat())
    vehicles = [
        item for item in result.get("vehicles", [])
        if _in_window(item.get("first_detection_time"), settings)
    ]
    summary = {
        "vehicle_count": len(vehicles),
        "duration_count": sum(item.get("duration_seconds") is not None for item in vehicles),
        "single_detection_count": sum(item.get("duration_seconds") is None for item in vehicles),
    }
    return store.create_report(
        report_date=report_date, report_kind="vehicle_traffic",
        window_start=start.timestamp(), window_end=end.timestamp(),
        row_count=len(vehicles), summary=summary,
        csv_text=render_vehicle_csv(vehicles, settings),
        message_id=f"vehicle-traffic-{report_date.isoformat()}@tvt-edge.local",
        generated_at=(now or datetime.now(timezone.utc)).timestamp(),
    )


def ensure_attendance_report(
    store: ReportingStore,
    settings: ReportingSettings,
    apex: ApexClient,
    report_date: date,
    now: datetime | None = None,
) -> StoredReport:
    existing = store.report(report_date, "attendance")
    if existing is not None:
        return existing
    start = datetime.combine(report_date, settings.window_start, settings.timezone)
    end = datetime.combine(report_date, settings.window_end, settings.timezone)
    result = _fetch(apex, apex.attendance_report, report_date.isoformat())
    people = _attendance_people_for_window(result, start, end)
    summary = {
        "registered_person_count": len(people),
        "people_present_count": sum(item.get("visit_count", 0) > 0 for item in people),
        "incomplete_session_count": sum(item.get("incomplete_session_count", 0) for item in people),
        "total_duration_seconds": sum(item.get("total_duration_seconds") or 0 for item in people),
    }
    return store.create_report(
        report_date=report_date, report_kind="attendance",
        window_start=start.timestamp(), window_end=end.timestamp(),
        row_count=len(people), summary=summary,
        csv_text=render_attendance_csv(people, settings),
        message_id=f"attendance-{report_date.isoformat()}@tvt-edge.local",
        generated_at=(now or datetime.now(timezone.utc)).timestamp(),
    )


def render_vehicle_message(report: StoredReport, settings: ReportingSettings) -> EmailMessage:
    message = EmailMessage()
    message["From"] = settings.sender
    message["To"] = ", ".join(settings.recipients)
    message["Subject"] = f"TVT daily vehicle entry/exit report - {report.report_date}"
    message["Message-ID"] = f"<{report.message_id}>"
    message["X-TVT-Template-Version"] = "daily-vehicle-duration-v2"
    message.set_content(
        "\n".join(
            [
                "TVT daily vehicle duration report", "",
                f"Date: {report.report_date}", f"Timezone: {settings.timezone_name}",
                f"Window: {settings.window_start.strftime('%H:%M')} - {settings.window_end.strftime('%H:%M')}",
                f"Vehicles observed: {report.summary.get('vehicle_count', 0)}",
                f"Vehicles with a duration: {report.summary.get('duration_count', 0)}",
                f"Vehicles seen once: {report.summary.get('single_detection_count', 0)}",
                "",
                "Duration is the last accepted plate detection minus the first accepted "
                "plate detection in the reporting window. See the attached CSV for details.",
                "",
            ]
        )
    )
    message.add_attachment(
        report.csv_text.encode("utf-8"), maintype="text", subtype="csv",
        filename=f"vehicle-traffic-{report.report_date}.csv",
    )
    return message


def render_attendance_message(report: StoredReport, settings: ReportingSettings) -> EmailMessage:
    message = EmailMessage()
    message["From"] = settings.sender
    message["To"] = ", ".join(settings.recipients)
    message["Subject"] = f"TVT daily attendance report - {report.report_date}"
    message["Message-ID"] = f"<{report.message_id}>"
    message["X-TVT-Template-Version"] = "daily-attendance-duration-v2"
    message.set_content(
        "\n".join(
            [
                "TVT daily attendance report", "",
                f"Date: {report.report_date}", f"Timezone: {settings.timezone_name}",
                f"Window: {settings.window_start.strftime('%H:%M')} - {settings.window_end.strftime('%H:%M')}",
                f"Registered people: {report.summary.get('registered_person_count', 0)}",
                f"People present: {report.summary.get('people_present_count', 0)}",
                f"Incomplete entry/exit sessions: {report.summary.get('incomplete_session_count', 0)}",
                f"Total time inside the plant: {_duration(report.summary.get('total_duration_seconds', 0))}",
                "",
                "Each registered person's total is the sum of completed face-recognition "
                "entry-to-exit visits. See the attached CSV for per-person totals.",
                "",
            ]
        )
    )
    message.add_attachment(
        report.csv_text.encode("utf-8"), maintype="text", subtype="csv",
        filename=f"attendance-{report.report_date}.csv",
    )
    return message


class ReportSender(Protocol):
    def send(self, message: EmailMessage) -> None: ...


class SMTPReportSender:
    def __init__(self, settings: ReportingSettings, api_key: str):
        self.settings = settings
        self.api_key = api_key

    def send(self, message: EmailMessage) -> None:
        try:
            with smtplib.SMTP(
                self.settings.smtp_host,
                self.settings.smtp_port,
                timeout=self.settings.smtp_timeout,
            ) as smtp:
                smtp.ehlo()
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
                smtp.login(self.settings.smtp_username, self.api_key)
                smtp.send_message(message)
        except smtplib.SMTPResponseException as error:
            code = int(error.smtp_code)
            raise DeliveryFailure(
                "SMTP_TRANSIENT" if 400 <= code < 500 else "SMTP_PERMANENT",
                transient=400 <= code < 500,
                smtp_code=code,
            ) from error
        except smtplib.SMTPRecipientsRefused as error:
            codes = [int(value[0]) for value in error.recipients.values()]
            code = min(codes) if codes else None
            transient = bool(codes) and all(400 <= value < 500 for value in codes)
            raise DeliveryFailure("SMTP_RECIPIENTS_REFUSED", transient, code) from error
        except (OSError, socket.timeout, smtplib.SMTPServerDisconnected) as error:
            raise DeliveryFailure("SMTP_UNAVAILABLE", True) from error
        except smtplib.SMTPException as error:
            raise DeliveryFailure("SMTP_PROTOCOL_ERROR", True) from error


_ENSURE = {"vehicle_traffic": ensure_vehicle_report, "attendance": ensure_attendance_report}
_RENDER = {"vehicle_traffic": render_vehicle_message, "attendance": render_attendance_message}


def send_daily_report(
    store: ReportingStore,
    settings: ReportingSettings,
    apex: ApexClient,
    sender: ReportSender,
    report_kind: str,
    *,
    now: datetime | None = None,
) -> str:
    if report_kind not in _ENSURE:
        raise ValueError(f"unknown report_kind {report_kind!r}")
    current = now or datetime.now(timezone.utc)
    report_date = current.astimezone(settings.timezone).date()
    report = _ENSURE[report_kind](store, settings, apex, report_date, current)
    if report.state != "pending":
        return report.state
    if not store.claim_delivery(report_date, report_kind, time.time()):
        claimed = store.report(report_date, report_kind)
        return claimed.state if claimed is not None else "failed"
    try:
        sender.send(_RENDER[report_kind](report, settings))
    except DeliveryFailure as error:
        store.mark_failed(report_date, report_kind, error.category, time.time())
        raise
    except Exception:
        store.mark_failed(report_date, report_kind, "EMAIL_DELIVERY_FAILED", time.time())
        raise
    store.mark_sent(report_date, report_kind, time.time())
    return "sent"
