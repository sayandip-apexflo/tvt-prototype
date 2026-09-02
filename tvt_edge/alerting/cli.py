"""Entry point for the standalone alert receiver and retry worker."""

from __future__ import annotations

from datetime import timedelta

import uvicorn

from tvt_edge.alerting.email_sender import SMTPEmailSender
from tvt_edge.alerting.outbox import OutboxWorker
from tvt_edge.alerting.receiver import create_alert_app
from tvt_edge.alerting.settings import AlertDispatcherSettings, read_protected_secret
from tvt_edge.db.session import build_engine, build_session_factory
from tvt_edge.observability import AlertDispatcherMetrics, configure_json_logging


def main() -> int:
    configure_json_logging("alert-dispatcher")
    settings = AlertDispatcherSettings.from_environment()
    bearer_token = read_protected_secret(settings.webhook_token_file)
    smtp_key = read_protected_secret(settings.smtp_key_file)
    sessions = build_session_factory(build_engine(settings.database_url))
    metrics = AlertDispatcherMetrics()
    sender = SMTPEmailSender(
        host=settings.smtp_host,
        port=settings.smtp_port,
        username=settings.smtp_username,
        password=smtp_key,
        sender=settings.sender,
        timeout=settings.smtp_timeout,
    )
    worker = OutboxWorker(
        sessions,
        sender,
        metrics=metrics,
        max_delivery_age=timedelta(seconds=settings.max_delivery_age_seconds),
    )
    uvicorn.run(
        create_alert_app(
            sessions,
            bearer_token,
            worker=worker,
            metrics=metrics,
            poll_interval=settings.poll_interval,
        ),
        host=settings.listen_host,
        port=settings.listen_port,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
