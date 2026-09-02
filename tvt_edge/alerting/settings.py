"""Strict environment settings for the host alert dispatcher."""

from __future__ import annotations

import ipaddress
import os
import stat
from dataclasses import dataclass
from pathlib import Path


def read_protected_secret(path: Path) -> str:
    mode = path.stat().st_mode
    if not stat.S_ISREG(mode) or mode & (stat.S_IWGRP | stat.S_IRWXO):
        raise PermissionError(f"unsafe secret file permissions: {path}")
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError(f"secret file is empty: {path}")
    return value


@dataclass(frozen=True)
class AlertDispatcherSettings:
    database_url: str
    listen_host: str
    listen_port: int
    webhook_token_file: Path
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_key_file: Path
    sender: str
    poll_interval: float
    smtp_timeout: float
    max_delivery_age_seconds: int

    @classmethod
    def from_environment(cls) -> "AlertDispatcherSettings":
        listen_host = os.getenv("TVT_ALERT_LISTEN_HOST", "127.0.0.1")
        try:
            address = ipaddress.ip_address(listen_host)
        except ValueError as error:
            raise ValueError(
                "TVT_ALERT_LISTEN_HOST must be an explicit IP address"
            ) from error
        if address.is_unspecified:
            raise ValueError("TVT_ALERT_LISTEN_HOST must not expose every host interface")
        listen_port = int(os.getenv("TVT_ALERT_LISTEN_PORT", "8090"))
        smtp_port = int(os.getenv("TVT_ALERT_SMTP_PORT", "587"))
        poll_interval = float(os.getenv("TVT_ALERT_POLL_INTERVAL", "5"))
        smtp_timeout = float(os.getenv("TVT_ALERT_SMTP_TIMEOUT", "15"))
        max_age = int(os.getenv("TVT_ALERT_MAX_DELIVERY_AGE", "86400"))
        if not 1 <= listen_port <= 65535 or not 1 <= smtp_port <= 65535:
            raise ValueError("dispatcher ports must be between 1 and 65535")
        if not 0.25 <= poll_interval <= 300:
            raise ValueError("TVT_ALERT_POLL_INTERVAL must be between 0.25 and 300")
        if not 1 <= smtp_timeout <= 120:
            raise ValueError("TVT_ALERT_SMTP_TIMEOUT must be between 1 and 120")
        if not 60 <= max_age <= 604800:
            raise ValueError("TVT_ALERT_MAX_DELIVERY_AGE must be between 60 and 604800")
        return cls(
            database_url=os.getenv(
                "TVT_ALERT_DATABASE_URL",
                os.getenv("TVT_DATABASE_URL", "postgresql+psycopg:///tvt"),
            ),
            listen_host=listen_host,
            listen_port=listen_port,
            webhook_token_file=Path(
                os.getenv(
                    "TVT_ALERT_WEBHOOK_TOKEN_FILE",
                    "/etc/tvt/alertmanager-webhook.token",
                )
            ),
            smtp_host=os.getenv("TVT_ALERT_SMTP_HOST", "smtp.sendgrid.net"),
            smtp_port=smtp_port,
            smtp_username=os.getenv("TVT_ALERT_SMTP_USERNAME", "apikey"),
            smtp_key_file=Path(
                os.getenv("TVT_ALERT_SMTP_KEY_FILE", "/etc/tvt/sendgrid-api-key")
            ),
            sender=os.getenv("TVT_ALERT_EMAIL_FROM", "tvt-alerts@tvt.example"),
            poll_interval=poll_interval,
            smtp_timeout=smtp_timeout,
            max_delivery_age_seconds=max_age,
        )
