"""Strict settings for ANPR collection and daily email delivery."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _clock(name: str, default: str) -> time:
    value = os.getenv(name, default)
    try:
        hour_text, minute_text = value.split(":", 1)
        value_time = time(hour=int(hour_text), minute=int(minute_text))
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must use HH:MM") from error
    return value_time


def _addresses(name: str) -> tuple[str, ...]:
    values = tuple(item.strip() for item in os.getenv(name, "").split(",") if item.strip())
    for value in values:
        if "@" not in value or any(character in value for character in "\r\n"):
            raise ValueError(f"{name} contains an invalid email address")
    return values


@dataclass(frozen=True)
class ReportingSettings:
    state_directory: Path
    apex_url: str
    camera_ids: frozenset[str]
    timezone_name: str
    window_start: time
    window_end: time
    poll_interval: float
    retention_days: int
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_key_file: Path
    smtp_timeout: float
    sender: str
    recipients: tuple[str, ...]

    @property
    def database_path(self) -> Path:
        return self.state_directory / "reporting.sqlite3"

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)

    def validate_delivery(self) -> None:
        if not self.recipients:
            raise ValueError("TVT_REPORT_EMAIL_TO must contain at least one recipient")
        if "@" not in self.sender or any(character in self.sender for character in "\r\n"):
            raise ValueError("TVT_REPORT_EMAIL_FROM must be a valid email address")

    @classmethod
    def from_environment(cls) -> "ReportingSettings":
        apex_url = os.getenv("TVT_REPORT_APEX_URL", "http://127.0.0.1:8088").rstrip("/")
        parsed = urlsplit(apex_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}:
            raise ValueError("TVT_REPORT_APEX_URL must be an explicit loopback HTTP URL")
        if parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError("TVT_REPORT_APEX_URL must contain only scheme, host, and port")

        camera_ids = frozenset(
            item.strip()
            for item in os.getenv("TVT_REPORT_CAMERA_IDS", "").split(",")
            if item.strip()
        )
        if not camera_ids:
            raise ValueError("TVT_REPORT_CAMERA_IDS must contain at least one camera ID")
        if any(len(item) > 128 or any(character in item for character in "\r\n") for item in camera_ids):
            raise ValueError("TVT_REPORT_CAMERA_IDS contains an invalid camera ID")

        timezone_name = os.getenv("TVT_REPORT_TIMEZONE", "Asia/Kolkata")
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as error:
            raise ValueError("TVT_REPORT_TIMEZONE must be an installed IANA timezone") from error
        window_start = _clock("TVT_REPORT_WINDOW_START", "09:00")
        window_end = _clock("TVT_REPORT_WINDOW_END", "18:00")
        if window_start >= window_end:
            raise ValueError("the reporting window must begin before it ends")

        poll_interval = float(os.getenv("TVT_REPORT_POLL_INTERVAL", "3"))
        retention_days = int(os.getenv("TVT_REPORT_RETENTION_DAYS", "90"))
        smtp_port = int(os.getenv("TVT_REPORT_SMTP_PORT", "587"))
        smtp_timeout = float(os.getenv("TVT_REPORT_SMTP_TIMEOUT", "15"))
        if not 1 <= poll_interval <= 300:
            raise ValueError("TVT_REPORT_POLL_INTERVAL must be between 1 and 300")
        if not 7 <= retention_days <= 3650:
            raise ValueError("TVT_REPORT_RETENTION_DAYS must be between 7 and 3650")
        if not 1 <= smtp_port <= 65535:
            raise ValueError("TVT_REPORT_SMTP_PORT must be between 1 and 65535")
        if not 1 <= smtp_timeout <= 120:
            raise ValueError("TVT_REPORT_SMTP_TIMEOUT must be between 1 and 120")

        return cls(
            state_directory=Path(os.getenv("TVT_REPORT_STATE_DIR", "/var/lib/tvt-reporting")),
            apex_url=apex_url,
            camera_ids=camera_ids,
            timezone_name=timezone_name,
            window_start=window_start,
            window_end=window_end,
            poll_interval=poll_interval,
            retention_days=retention_days,
            smtp_host=os.getenv("TVT_REPORT_SMTP_HOST", "smtp.sendgrid.net"),
            smtp_port=smtp_port,
            smtp_username=os.getenv("TVT_REPORT_SMTP_USERNAME", "apikey"),
            smtp_key_file=Path(
                os.getenv("TVT_REPORT_SMTP_KEY_FILE", "/etc/tvt/anpr-report-sendgrid-key")
            ),
            smtp_timeout=smtp_timeout,
            sender=os.getenv("TVT_REPORT_EMAIL_FROM", ""),
            recipients=_addresses("TVT_REPORT_EMAIL_TO"),
        )
