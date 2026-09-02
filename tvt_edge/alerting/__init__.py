"""Durable operational-alert ingestion and email delivery."""

from tvt_edge.alerting.receiver import create_alert_app
from tvt_edge.alerting.service import AlertingService

__all__ = ["AlertingService", "create_alert_app"]
