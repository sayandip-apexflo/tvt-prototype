"""Immutable daily report rendering and SendGrid SMTP delivery."""

from __future__ import annotations

import csv
import io
import smtplib
import socket
import ssl
import time
from datetime import date, datetime, timezone
from email.message import EmailMessage
from typing import Protocol

from tvt_edge.alerting.email_sender import DeliveryFailure
from tvt_edge.reporting.database import Observation, ReportingStore, StoredReport
from tvt_edge.reporting.settings import ReportingSettings


def _local_time(timestamp: float, settings: ReportingSettings) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).astimezone(settings.timezone).isoformat()


def _duration(seconds: float) -> str:
    whole = max(0, round(seconds))
    hours, remainder = divmod(whole, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def render_csv(observations: list[Observation], settings: ReportingSettings) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(
        [
            "vehicle_ref", "first_seen", "last_seen", "duration_hh_mm_ss", "read_count",
            "first_camera_id", "last_camera_id",
        ]
    )
    for item in observations:
        writer.writerow(
            [
                item.vehicle_ref, _local_time(item.first_seen_at, settings),
                _local_time(item.last_seen_at, settings), _duration(item.duration_seconds),
                item.read_count, item.first_camera_id, item.last_camera_id,
            ]
        )
    return output.getvalue()


def ensure_report(
    store: ReportingStore,
    settings: ReportingSettings,
    report_date: date,
    now: datetime | None = None,
) -> StoredReport:
    existing = store.report(report_date)
    if existing is not None:
        return existing
    start = datetime.combine(report_date, settings.window_start, settings.timezone)
    end = datetime.combine(report_date, settings.window_end, settings.timezone)
    observations = store.observations(report_date)
    total = sum(item.duration_seconds for item in observations)
    return store.create_report(
        report_date=report_date,
        window_start=start.timestamp(),
        window_end=end.timestamp(),
        row_count=len(observations),
        total_duration_seconds=total,
        csv_text=render_csv(observations, settings),
        message_id=f"anpr-{report_date.isoformat()}@tvt-edge.local",
        generated_at=(now or datetime.now(timezone.utc)).timestamp(),
    )


def render_message(
    report: StoredReport,
    settings: ReportingSettings,
) -> EmailMessage:
    message = EmailMessage()
    message["From"] = settings.sender
    message["To"] = ", ".join(settings.recipients)
    message["Subject"] = f"TVT daily ANPR duration report - {report.report_date}"
    message["Message-ID"] = f"<{report.message_id}>"
    message["X-TVT-Template-Version"] = "daily-anpr-duration-v1"
    message.set_content(
        "\n".join(
            [
                "TVT daily ANPR duration report",
                "",
                f"Date: {report.report_date}",
                f"Timezone: {settings.timezone_name}",
                f"Window: {settings.window_start.strftime('%H:%M')} - {settings.window_end.strftime('%H:%M')}",
                f"Distinct number plates: {report.row_count}",
                f"Total observed vehicle duration: {_duration(report.total_duration_seconds)}",
                "",
                "Each vehicle duration is last observed time minus first observed time across the configured cameras.",
                "A single observation has duration 00:00:00. See the attached CSV for per-plate details.",
                "",
            ]
        )
    )
    message.add_attachment(
        report.csv_text.encode("utf-8"),
        maintype="text",
        subtype="csv",
        filename=f"anpr-duration-{report.report_date}.csv",
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


def send_daily_report(
    store: ReportingStore,
    settings: ReportingSettings,
    sender: ReportSender,
    *,
    now: datetime | None = None,
) -> str:
    current = now or datetime.now(timezone.utc)
    report_date = current.astimezone(settings.timezone).date()
    report = ensure_report(store, settings, report_date, current)
    if report.state != "pending":
        return report.state
    if not store.claim_delivery(report_date, time.time()):
        claimed = store.report(report_date)
        return claimed.state if claimed is not None else "failed"
    try:
        sender.send(render_message(report, settings))
    except DeliveryFailure as error:
        store.mark_failed(report_date, error.category, time.time())
        raise
    except Exception:
        store.mark_failed(report_date, "EMAIL_DELIVERY_FAILED", time.time())
        raise
    store.mark_sent(report_date, time.time())
    return "sent"
