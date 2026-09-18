from __future__ import annotations

from datetime import datetime, time, timezone
from email.message import EmailMessage
from pathlib import Path

from tvt_edge.reporting.collector import ingest_events
from tvt_edge.reporting.database import ReportingStore
from tvt_edge.reporting.email_report import ensure_report, render_message, send_daily_report
from tvt_edge.reporting.settings import ReportingSettings


def settings(tmp_path: Path) -> ReportingSettings:
    return ReportingSettings(
        state_directory=tmp_path / "reporting",
        apex_url="http://127.0.0.1:8088",
        camera_ids=frozenset({"camera-a", "camera-b"}),
        timezone_name="Asia/Kolkata",
        window_start=time(9, 0),
        window_end=time(18, 0),
        poll_interval=3,
        retention_days=90,
        smtp_host="smtp.sendgrid.net",
        smtp_port=587,
        smtp_username="apikey",
        smtp_key_file=tmp_path / "sendgrid-key",
        smtp_timeout=15,
        sender="reports@example.invalid",
        recipients=("recipient@example.invalid",),
    )


def event(event_id: str, camera_id: str, plate: str, timestamp: str) -> dict[str, object]:
    return {
        "event_id": event_id,
        "occurred_at": timestamp,
        "received_at": 1_789_000_000.0,
        "payload": {
            "event_id": event_id,
            "timestamp": timestamp,
            "camera_id": camera_id,
            "application": "anpr",
            "event_type": "plate_read_event",
            "payload": {"plate": {"text": plate, "confidence": 0.92}},
        },
    }


def test_first_and_last_across_cameras_are_aggregated_and_deduplicated(tmp_path: Path) -> None:
    report_settings = settings(tmp_path)
    store = ReportingStore(report_settings.database_path)
    events = [
        event("event-2", "camera-b", "AB-12 CD 3456", "2026-09-18T11:30:00+05:30"),
        event("event-1", "camera-a", "AB12CD3456", "2026-09-18T09:15:00+05:30"),
        event("event-2", "camera-b", "AB-12 CD 3456", "2026-09-18T11:30:00+05:30"),
        event("event-3", "camera-a", "AB12CD3456", "2026-09-18T17:59:59+05:30"),
        event("event-4", "camera-a", "AB12CD3456", "2026-09-18T18:00:00+05:30"),
        event("event-5", "camera-not-configured", "AB12CD3456", "2026-09-18T12:00:00+05:30"),
    ]

    assert ingest_events(store, report_settings, events) == 3
    observations = store.observations(datetime(2026, 9, 18).date())
    assert len(observations) == 1
    assert observations[0].read_count == 3
    assert observations[0].first_camera_id == "camera-a"
    assert observations[0].last_camera_id == "camera-a"
    assert observations[0].duration_seconds == 8 * 3600 + 44 * 60 + 59


def test_report_sums_vehicle_spans_and_email_contains_no_plate_text(tmp_path: Path) -> None:
    report_settings = settings(tmp_path)
    store = ReportingStore(report_settings.database_path)
    ingest_events(
        store,
        report_settings,
        [
            event("a-1", "camera-a", "AB12CD3456", "2026-09-18T09:00:00+05:30"),
            event("a-2", "camera-b", "AB12CD3456", "2026-09-18T10:00:00+05:30"),
            event("b-1", "camera-a", "XY98ZT7654", "2026-09-18T12:00:00+05:30"),
            event("b-2", "camera-b", "XY98ZT7654", "2026-09-18T14:30:00+05:30"),
        ],
    )

    report = ensure_report(store, report_settings, datetime(2026, 9, 18).date())
    message = render_message(report, report_settings)
    rendered = message.as_string()

    assert report.row_count == 2
    assert report.total_duration_seconds == 3.5 * 3600
    assert "Total observed vehicle duration: 03:30:00" in rendered
    assert "AB12CD3456" not in rendered
    assert "XY98ZT7654" not in rendered
    assert "vehicle_ref" in report.csv_text
    assert "AB12CD3456" not in report.csv_text
    assert "XY98ZT7654" not in report.csv_text


class RecordingSender:
    def __init__(self) -> None:
        self.messages: list[EmailMessage] = []

    def send(self, message: EmailMessage) -> None:
        self.messages.append(message)


def test_daily_report_is_delivered_at_most_once(tmp_path: Path) -> None:
    report_settings = settings(tmp_path)
    store = ReportingStore(report_settings.database_path)
    sender = RecordingSender()
    now = datetime(2026, 9, 18, 13, 0, tzinfo=timezone.utc)  # 18:30 Asia/Kolkata

    assert send_daily_report(store, report_settings, sender, now=now) == "sent"
    assert send_daily_report(store, report_settings, sender, now=now) == "sent"
    assert len(sender.messages) == 1
    assert store.report(datetime(2026, 9, 18).date()).state == "sent"


def test_timer_is_exactly_1830_and_has_no_late_catchup() -> None:
    timer = Path("deploy/systemd/tvt-anpr-report.timer").read_text(encoding="utf-8")
    assert "OnCalendar=*-*-* 18:30:00 Asia/Kolkata" in timer
    assert "AccuracySec=1s" in timer
    assert "Persistent=false" in timer
