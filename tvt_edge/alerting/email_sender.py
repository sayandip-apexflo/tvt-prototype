"""Versioned local email rendering and certificate-verified SMTP delivery."""

from __future__ import annotations

import smtplib
import socket
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage

from tvt_edge.db.models import AlertInstance, AlertTransition, NotificationOutbox


@dataclass(frozen=True)
class DeliveryFailure(Exception):
    category: str
    transient: bool
    smtp_code: int | None = None

    def __str__(self) -> str:
        return self.category


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def render_message(
    item: NotificationOutbox,
    alert: AlertInstance,
    transition: AlertTransition,
    sender: str,
) -> EmailMessage:
    """Render only allowlisted persisted fields; webhook content sets no headers."""

    state = "resolved" if item.notification_type == "resolved" else alert.state
    summary = str(alert.safe_annotations.get("summary", "Operational alert"))[:500]
    subject = (
        f"[TVT {alert.severity.upper()}] {alert.alert_name} "
        f"at {alert.site_key} ({state})"
    )
    lines = [
        "TVT operational alert",
        "",
        f"Site: {alert.site_key}",
        f"Severity: {alert.severity}",
        f"Alert: {alert.alert_name}",
        f"Service: {alert.service}",
        f"Camera: {alert.camera_key or '-'}",
        f"Use case: {alert.use_case or '-'}",
        f"State: {state}",
        f"Started: {_timestamp(transition.occurrence_starts_at)}",
        f"Summary: {summary}",
    ]
    for label, key in (("Runbook", "runbook_url"), ("Dashboard", "dashboard_url")):
        value = alert.safe_annotations.get(key)
        if isinstance(value, str) and value.startswith(("https://", "http://")):
            lines.append(f"{label}: {value[:2000]}")
    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(item.recipients)
    message["Subject"] = subject[:240]
    message["Message-ID"] = f"<{item.message_id}>"
    message["X-TVT-Alert-Fingerprint"] = alert.fingerprint
    message["X-TVT-Template-Version"] = "operational-alert-v1"
    message.set_content("\n".join(lines) + "\n")
    return message


class SMTPEmailSender:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        sender: str,
        timeout: float = 15.0,
    ):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.sender = sender
        self.timeout = timeout

    def send(
        self,
        item: NotificationOutbox,
        alert: AlertInstance,
        transition: AlertTransition,
    ) -> int:
        message = render_message(item, alert, transition, self.sender)
        try:
            with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as smtp:
                smtp.ehlo()
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
                smtp.login(self.username, self.password)
                smtp.send_message(message)
            return 250
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
            transient = bool(codes) and all(400 <= item < 500 for item in codes)
            raise DeliveryFailure("SMTP_RECIPIENTS_REFUSED", transient, code) from error
        except (OSError, socket.timeout, smtplib.SMTPServerDisconnected) as error:
            raise DeliveryFailure("SMTP_UNAVAILABLE", True) from error
        except smtplib.SMTPException as error:
            raise DeliveryFailure("SMTP_PROTOCOL_ERROR", True) from error
