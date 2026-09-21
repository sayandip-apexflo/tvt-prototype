from __future__ import annotations

from datetime import datetime, time, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from tvt_edge.apex_client import ApexUnavailableError
from tvt_edge.reporting.database import ReportingStore
from tvt_edge.reporting.email_report import (
    ensure_attendance_report,
    ensure_vehicle_report,
    render_attendance_message,
    render_vehicle_message,
    send_daily_report,
)
from tvt_edge.reporting.settings import ReportingSettings


def settings(tmp_path: Path) -> ReportingSettings:
    return ReportingSettings(
        state_directory=tmp_path / "reporting",
        apex_url="http://127.0.0.1:8088",
        timezone_name="Asia/Kolkata",
        window_start=time(9, 0),
        window_end=time(18, 0),
        retention_days=90,
        smtp_host="smtp.sendgrid.net",
        smtp_port=587,
        smtp_username="apikey",
        smtp_key_file=tmp_path / "sendgrid-key",
        smtp_timeout=15,
        sender="reports@example.invalid",
        recipients=("recipient@example.invalid",),
    )


class FakeApex:
    """Duck-typed stand-in for tvt_edge.apex_client.ApexClient -- email_report
    only calls .vehicle_traffic_report(date=...)/.attendance_report(date=...)."""

    def __init__(self, vehicle: dict[str, Any] | None = None, attendance: dict[str, Any] | None = None):
        self._vehicle = vehicle or {"sessions": [], "entered_count": 0, "exited_count": 0}
        self._attendance = attendance or {"sessions": [], "total_duration_seconds": 0}
        self.calls: list[str] = []

    def vehicle_traffic_report(self, *, date: str | None = None, gate: str | None = None) -> dict[str, Any]:
        self.calls.append(f"vehicle:{date}")
        return self._vehicle

    def attendance_report(self, *, person_id: str | None = None, date: str | None = None) -> dict[str, Any]:
        self.calls.append(f"attendance:{date}")
        return self._attendance


class UnavailableApex:
    def vehicle_traffic_report(self, **_kwargs: Any) -> dict[str, Any]:
        raise ApexUnavailableError("connection refused")

    def attendance_report(self, **_kwargs: Any) -> dict[str, Any]:
        raise ApexUnavailableError("connection refused")


def vehicle_session(plate: str, gate: str, entry: str, exit_: str | None, status: str = "closed") -> dict[str, Any]:
    return {
        "plate_text": plate, "gate": gate,
        "entry_time": datetime.fromisoformat(entry).timestamp(),
        "exit_time": datetime.fromisoformat(exit_).timestamp() if exit_ else None,
        "status": status,
    }


def attendance_session(person_id: str, display_name: str, gate: str, entry: str, exit_: str, duration: float) -> dict[str, Any]:
    return {
        "person_id": person_id, "display_name": display_name, "gate": gate,
        "entry_time": datetime.fromisoformat(entry).timestamp(),
        "exit_time": datetime.fromisoformat(exit_).timestamp(),
        "duration_seconds": duration, "status": "closed",
    }


def test_vehicle_report_never_puts_plate_text_in_the_csv_or_email(tmp_path: Path) -> None:
    report_settings = settings(tmp_path)
    store = ReportingStore(report_settings.database_path)
    apex = FakeApex(vehicle={
        "sessions": [
            vehicle_session("AB12CD3456", "main-entrance", "2026-09-18T09:15:00+05:30", "2026-09-18T17:00:00+05:30"),
            vehicle_session("XY98ZT7654", "plant-entrance", "2026-09-18T10:00:00+05:30", None, status="open"),
        ],
        "entered_count": 2, "exited_count": 1,
    })

    report = ensure_vehicle_report(store, report_settings, apex, datetime(2026, 9, 18).date())
    message = render_vehicle_message(report, report_settings)
    rendered = message.as_string()

    assert report.row_count == 2
    assert report.summary == {"entered_count": 2, "exited_count": 1}
    assert "Vehicles entered: 2" in rendered
    assert "Vehicles exited: 1" in rendered
    assert "AB12CD3456" not in rendered
    assert "XY98ZT7654" not in rendered
    assert "AB12CD3456" not in report.csv_text
    assert "XY98ZT7654" not in report.csv_text
    assert "vehicle_ref" in report.csv_text
    assert "main-entrance" in report.csv_text


def test_attendance_report_never_puts_display_name_in_the_csv_or_email(tmp_path: Path) -> None:
    report_settings = settings(tmp_path)
    store = ReportingStore(report_settings.database_path)
    apex = FakeApex(attendance={
        "sessions": [
            attendance_session("p-1", "Jane Doe", "main-entrance", "2026-09-18T09:00:00+05:30", "2026-09-18T17:30:00+05:30", 8.5 * 3600),
        ],
        "total_duration_seconds": 8.5 * 3600,
    })

    report = ensure_attendance_report(store, report_settings, apex, datetime(2026, 9, 18).date())
    message = render_attendance_message(report, report_settings)
    rendered = message.as_string()

    assert report.row_count == 1
    assert report.summary == {"total_duration_seconds": 8.5 * 3600}
    assert "Total time inside the plant: 08:30:00" in rendered
    assert "Jane Doe" not in rendered
    assert "Jane Doe" not in report.csv_text
    assert "p-1" in report.csv_text


def test_window_filters_out_sessions_starting_outside_the_configured_hours(tmp_path: Path) -> None:
    report_settings = settings(tmp_path)
    store = ReportingStore(report_settings.database_path)
    apex = FakeApex(vehicle={
        "sessions": [
            vehicle_session("AB12CD3456", "main-entrance", "2026-09-18T09:15:00+05:30", "2026-09-18T09:20:00+05:30"),
            vehicle_session("XY98ZT7654", "main-entrance", "2026-09-18T23:00:00+05:30", "2026-09-18T23:05:00+05:30"),
        ],
    })
    report = ensure_vehicle_report(store, report_settings, apex, datetime(2026, 9, 18).date())
    assert report.row_count == 1


class RecordingSender:
    def __init__(self) -> None:
        self.messages: list[EmailMessage] = []

    def send(self, message: EmailMessage) -> None:
        self.messages.append(message)


def test_daily_report_is_delivered_at_most_once_per_report_kind(tmp_path: Path) -> None:
    report_settings = settings(tmp_path)
    store = ReportingStore(report_settings.database_path)
    apex = FakeApex()
    sender = RecordingSender()
    now = datetime(2026, 9, 18, 13, 0, tzinfo=timezone.utc)  # 18:30 Asia/Kolkata

    assert send_daily_report(store, report_settings, apex, sender, "vehicle_traffic", now=now) == "sent"
    assert send_daily_report(store, report_settings, apex, sender, "vehicle_traffic", now=now) == "sent"
    assert len(sender.messages) == 1
    assert store.report(datetime(2026, 9, 18).date(), "vehicle_traffic").state == "sent"

    assert send_daily_report(store, report_settings, apex, sender, "attendance", now=now) == "sent"
    assert len(sender.messages) == 2
    assert store.report(datetime(2026, 9, 18).date(), "attendance").state == "sent"


def test_send_daily_report_rejects_an_unknown_report_kind(tmp_path: Path) -> None:
    report_settings = settings(tmp_path)
    store = ReportingStore(report_settings.database_path)
    try:
        send_daily_report(store, report_settings, FakeApex(), RecordingSender(), "faces")
    except ValueError as error:
        assert "unknown report_kind" in str(error)
    else:
        raise AssertionError("expected ValueError")


def test_apex_unavailable_propagates_without_sending_or_recording_a_report(tmp_path: Path) -> None:
    # No row is created at all when apex can't be reached (generation fails
    # before store.create_report), so the next timer run simply retries.
    report_settings = settings(tmp_path)
    store = ReportingStore(report_settings.database_path)
    sender = RecordingSender()
    now = datetime(2026, 9, 18, 13, 0, tzinfo=timezone.utc)
    try:
        send_daily_report(store, report_settings, UnavailableApex(), sender, "vehicle_traffic", now=now)
    except Exception:
        pass
    else:
        raise AssertionError("expected the reporting-unavailable failure to propagate")
    assert not sender.messages
    assert store.report(datetime(2026, 9, 18).date(), "vehicle_traffic") is None


def test_timer_is_exactly_1830_and_has_no_late_catchup() -> None:
    timer = Path("deploy/systemd/tvt-anpr-report.timer").read_text(encoding="utf-8")
    assert "OnCalendar=*-*-* 18:30:00 Asia/Kolkata" in timer
    assert "AccuracySec=1s" in timer
    assert "Persistent=false" in timer


def test_attendance_timer_is_distinct_from_the_vehicle_traffic_timer() -> None:
    timer = Path("deploy/systemd/tvt-anpr-report-attendance.timer").read_text(encoding="utf-8")
    assert "OnCalendar=*-*-* 18:35:00 Asia/Kolkata" in timer
    assert "Unit=tvt-anpr-report-attendance.service" in timer
