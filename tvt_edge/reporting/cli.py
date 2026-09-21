"""Command-line entry point for the daily vehicle-traffic/attendance report emails."""

from __future__ import annotations

import argparse

from tvt_edge.alerting.settings import read_protected_secret
from tvt_edge.apex_client import ApexClient
from tvt_edge.observability import configure_json_logging, get_logger
from tvt_edge.reporting.database import ReportingStore
from tvt_edge.reporting.email_report import SMTPReportSender, send_daily_report
from tvt_edge.reporting.settings import ReportingSettings


LOGGER = get_logger(__name__)

_COMMAND_TO_REPORT_KIND = {
    "send-daily-vehicle-traffic": "vehicle_traffic",
    "send-daily-attendance": "attendance",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tvt-anpr-report")
    parser.add_argument("command", choices=tuple(_COMMAND_TO_REPORT_KIND))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_json_logging("tvt-reporting")
    settings = ReportingSettings.from_environment()
    settings.validate_delivery()
    store = ReportingStore(settings.database_path)
    apex = ApexClient(settings.apex_url)
    api_key = read_protected_secret(settings.smtp_key_file)
    report_kind = _COMMAND_TO_REPORT_KIND[args.command]
    try:
        result = send_daily_report(
            store, settings, apex, SMTPReportSender(settings, api_key), report_kind
        )
    except Exception:
        LOGGER.error(
            "Daily report delivery failed",
            extra={
                "event": "report_delivery_failed",
                "error_code": "EMAIL_DELIVERY_FAILED",
                "reason": report_kind,
            },
            exc_info=True,
        )
        return 1
    LOGGER.info(
        "Daily report delivery finished",
        extra={"event": "report_delivery_finished", "result": result, "reason": report_kind},
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
