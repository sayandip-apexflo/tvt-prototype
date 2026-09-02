"""Strict, bounded Alertmanager webhook contracts."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tvt_edge.security import redact


MAX_ALERTS = 100
MAX_LABELS = 24
MAX_ANNOTATIONS = 8
MAX_LABEL_VALUE = 256
MAX_ANNOTATION_VALUE = 2000
ALLOWED_LABELS = {
    "alertname",
    "camera_id",
    "cluster",
    "component",
    "deployment_id",
    "instance",
    "job",
    "namespace",
    "node",
    "service",
    "severity",
    "site_id",
    "solution_id",
    "use_case",
}
ALLOWED_ANNOTATIONS = {"summary", "description", "runbook_url", "dashboard_url"}
REQUIRED_LABELS = {"site_id", "alertname", "severity", "service"}
SAFE_KEY = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
STABLE_VALUE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:/-]*$")
SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|token|secret|username|authorization)\s*[:=]\s*\S+"
)
HTTP_USERINFO = re.compile(r"(?i)(https?://)[^/\s:@]+:[^/\s@]+@")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class AlertmanagerAlert(StrictModel):
    status: Literal["firing", "resolved"]
    labels: dict[str, str]
    annotations: dict[str, str] = Field(default_factory=dict)
    starts_at: datetime = Field(alias="startsAt")
    ends_at: datetime | None = Field(default=None, alias="endsAt")
    generator_url: str | None = Field(
        default=None, alias="generatorURL", max_length=2000
    )
    fingerprint: str | None = Field(default=None, max_length=128)


class AlertmanagerWebhook(StrictModel):
    version: str | None = Field(default=None, max_length=16)
    group_key: str = Field(default="", alias="groupKey", max_length=512)
    truncated_alerts: int = Field(default=0, alias="truncatedAlerts", ge=0)
    status: Literal["firing", "resolved"] | None = None
    receiver: str | None = Field(default=None, max_length=128)
    group_labels: dict[str, str] = Field(default_factory=dict, alias="groupLabels")
    common_labels: dict[str, str] = Field(default_factory=dict, alias="commonLabels")
    common_annotations: dict[str, str] = Field(
        default_factory=dict, alias="commonAnnotations"
    )
    external_url: str | None = Field(
        default=None, alias="externalURL", max_length=2000
    )
    alerts: list[AlertmanagerAlert] = Field(min_length=1, max_length=MAX_ALERTS)


class NormalizedAlert(StrictModel):
    schema_version: Literal["1.0"]
    source: Literal["alertmanager"]
    status: Literal["firing", "resolved"]
    starts_at: datetime
    ends_at: datetime | None = None
    labels: dict[str, str]
    annotations: dict[str, str] = Field(default_factory=dict)
    group_key: str = Field(default="", max_length=512)


def _safe_text(value: str) -> str:
    safe_value = str(redact(value))
    safe_value = SECRET_ASSIGNMENT.sub(r"\1=[REDACTED]", safe_value)
    return HTTP_USERINFO.sub(r"\1[REDACTED]@", safe_value)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("alert timestamps must include a timezone")
    return value.astimezone(timezone.utc)


def _safe_fields(
    values: dict[str, str], *, allowed: set[str], count: int, value_limit: int
) -> dict[str, str]:
    if len(values) > count:
        raise ValueError("alert contains too many fields")
    result: dict[str, str] = {}
    for key, value in values.items():
        if not SAFE_KEY.fullmatch(key) or key not in allowed:
            raise ValueError(f"alert field {key!r} is not allowed")
        if not isinstance(value, str) or not value or len(value) > value_limit:
            raise ValueError(f"alert field {key!r} has an invalid value")
        result[key] = _safe_text(value)
    return result


def _validate_event(event: dict[str, Any]) -> dict[str, Any]:
    labels = _safe_fields(
        event["labels"],
        allowed=ALLOWED_LABELS,
        count=MAX_LABELS,
        value_limit=MAX_LABEL_VALUE,
    )
    missing = REQUIRED_LABELS - labels.keys()
    if missing:
        raise ValueError(f"missing required alert labels: {', '.join(sorted(missing))}")
    if labels["severity"] not in {"critical", "warning", "info"}:
        raise ValueError("alert severity is not supported")
    for key in ("site_id", "alertname", "service"):
        if not STABLE_VALUE.fullmatch(labels[key]):
            raise ValueError(f"alert label {key!r} is not a stable identifier")
    for key in ("camera_id", "use_case"):
        if key in labels and not STABLE_VALUE.fullmatch(labels[key]):
            raise ValueError(f"alert label {key!r} is not a stable identifier")
    annotations = _safe_fields(
        event["annotations"],
        allowed=ALLOWED_ANNOTATIONS,
        count=MAX_ANNOTATIONS,
        value_limit=MAX_ANNOTATION_VALUE,
    )
    starts_at = _utc(event["starts_at"])
    ends_at = _utc(event["ends_at"]) if event.get("ends_at") else None
    now = datetime.now(timezone.utc)
    if starts_at > now + timedelta(minutes=5):
        raise ValueError("alert start timestamp is too far in the future")
    if event["status"] == "resolved":
        if ends_at is None:
            raise ValueError("resolved alert requires an end timestamp")
        if ends_at < starts_at:
            raise ValueError("alert end timestamp precedes its start")
    else:
        ends_at = None
    return {
        "source": "alertmanager",
        "status": event["status"],
        "starts_at": starts_at,
        "ends_at": ends_at,
        "labels": labels,
        "annotations": annotations,
        "group_key": _safe_text(event.get("group_key", "")),
    }


def parse_alertmanager_payload(payload: Any) -> list[dict[str, Any]]:
    """Accept the native grouped webhook or the documented normalized event."""

    if not isinstance(payload, dict):
        raise ValueError("webhook body must be a JSON object")
    try:
        if "alerts" in payload:
            envelope = AlertmanagerWebhook.model_validate(payload)
            return [
                _validate_event(
                    {
                        "status": item.status,
                        "starts_at": item.starts_at,
                        "ends_at": item.ends_at,
                        "labels": item.labels,
                        "annotations": item.annotations,
                        "group_key": envelope.group_key,
                    }
                )
                for item in envelope.alerts
            ]
        item = NormalizedAlert.model_validate(payload)
        return [_validate_event(item.model_dump())]
    except ValidationError as error:
        raise ValueError("webhook body does not match the alert contract") from error
