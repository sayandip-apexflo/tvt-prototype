"""Command-line entry point for ANPR collection and once-daily delivery."""

from __future__ import annotations

import argparse

from tvt_edge.alerting.settings import read_protected_secret
from tvt_edge.observability import configure_json_logging, get_logger
from tvt_edge.reporting.collector import collect_forever
from tvt_edge.reporting.database import ReportingStore
from tvt_edge.reporting.email_report import SMTPReportSender, send_daily_report
from tvt_edge.reporting.settings import ReportingSettings


LOGGER = get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tvt-anpr-report")
    parser.add_argument("command", choices=("collect", "send-daily"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_json_logging("anpr-reporting")
    settings = ReportingSettings.from_environment()
    store = ReportingStore(settings.database_path)
    if args.command == "collect":
        collect_forever(store, settings)
        return 0

    settings.validate_delivery()
    api_key = read_protected_secret(settings.smtp_key_file)
    try:
        result = send_daily_report(store, settings, SMTPReportSender(settings, api_key))
    except Exception:
        LOGGER.error(
            "Daily ANPR report delivery failed",
            extra={"event": "anpr_report_delivery_failed", "error_code": "EMAIL_DELIVERY_FAILED"},
            exc_info=True,
        )
        return 1
    LOGGER.info(
        "Daily ANPR report delivery finished",
        extra={"event": "anpr_report_delivery_finished", "result": result},
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
