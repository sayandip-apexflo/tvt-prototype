"""Single-line JSON logging with correlation context and primary redaction."""

from __future__ import annotations

import contextvars
import ipaddress
import json
import logging
import re
import sys
import traceback
import uuid
from datetime import datetime, timezone
from typing import Any


REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
URL = re.compile(r"\b(?:rtsp|rtsps)://[^\s\"'<>]+", re.IGNORECASE)
BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+")
SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|token|secret|api[_-]?key|authorization)=([^\s&;,]+)"
)
IP_TOKEN = re.compile(
    r"(?<![A-Za-z0-9])(?:\[[0-9A-Fa-f:]+\]|(?:[0-9]{1,3}\.){3}[0-9]{1,3}|[0-9A-Fa-f]*:[0-9A-Fa-f:]{2,})(?![A-Za-z0-9])"
)
SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "body",
        "camera_ip",
        "camera_sources",
        "ciphertext",
        "credential",
        "credentials",
        "embedding",
        "embeddings",
        "face",
        "face_image",
        "image",
        "number_plate",
        "password",
        "person_name",
        "plate",
        "raw_body",
        "request_body",
        "response_body",
        "rtsp_url",
        "secret",
        "secret_value",
        "stringdata",
        "token",
        "username",
    }
)
CONTEXT_FIELDS = ("request_id", "operation_id", "event_id", "stream_session_id")
SAFE_FIELDS = frozenset(
    {
        "camera_id",
        "container",
        "deployment",
        "duration_seconds",
        "event_id",
        "exception_type",
        "model_version",
        "namespace",
        "operation_id",
        "pod",
        "reason",
        "request_id",
        "result",
        "retry_in_seconds",
        "software_version",
        "stack_trace",
        "stream_session_id",
        "use_case",
    }
)
_context: contextvars.ContextVar[dict[str, str | None]] = contextvars.ContextVar(
    "tvt_log_context", default={}
)


def request_id_or_new(candidate: str | None) -> str:
    """Accept a bounded safe request ID, otherwise generate one."""

    if candidate and REQUEST_ID.fullmatch(candidate):
        return candidate
    return str(uuid.uuid4())


def bind_log_context(**values: str | uuid.UUID | None) -> contextvars.Token:
    current = dict(_context.get())
    for key, value in values.items():
        if key not in CONTEXT_FIELDS:
            raise ValueError(f"unsupported correlation field: {key}")
        text = None if value is None else str(value)
        if text is not None and not REQUEST_ID.fullmatch(text):
            raise ValueError(f"invalid correlation identifier: {key}")
        current[key] = text
    return _context.set(current)


def reset_log_context(token: contextvars.Token) -> None:
    _context.reset(token)


def redact_text(value: str, limit: int = 8000) -> str:
    """Remove transport credentials, tokens and literal IP addresses."""

    result = URL.sub("[REDACTED_RTSP_URL]", value)
    result = BEARER.sub(lambda match: f"{match.group(1)} [REDACTED]", result)
    result = SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=[REDACTED]", result)

    def redact_ip(match: re.Match[str]) -> str:
        token = match.group(0).strip("[]")
        try:
            ipaddress.ip_address(token)
        except ValueError:
            return match.group(0)
        return "[REDACTED_IP]"

    result = IP_TOKEN.sub(redact_ip, result)
    return result.replace("\r", "\\r").replace("\n", "\\n")[:limit]


def redact_value(key: str, value: Any) -> Any:
    normalized = key.lower()
    if normalized in SENSITIVE_KEYS:
        return "[REDACTED]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(item_key): redact_value(str(item_key), item) for item_key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_value(key, item) for item in value]
    return redact_text(str(value))


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        event = getattr(record, "event", record.name.replace(".", "_").replace("-", "_"))
        error_code = getattr(record, "error_code", "NONE")
        document: dict[str, Any] = {
            "timestamp": timestamp,
            "level": record.levelname.lower(),
            "service": self.service,
            "event": redact_text(str(event), 128),
            "message": redact_text(record.getMessage()),
            "error_code": redact_text(str(error_code), 128),
        }
        context = _context.get()
        for field in CONTEXT_FIELDS:
            value = getattr(record, field, context.get(field))
            if value is not None:
                document[field] = redact_value(field, value)
        for field in SAFE_FIELDS - set(CONTEXT_FIELDS):
            if hasattr(record, field):
                document[field] = redact_value(field, getattr(record, field))
        if record.exc_info:
            document["exception_type"] = record.exc_info[0].__name__
            rendered = "".join(traceback.format_exception(*record.exc_info))
            document["stack_trace"] = redact_text(rendered)
        return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def configure_json_logging(service: str, level: str | int = "INFO") -> None:
    """Replace root handlers so stdout contains JSON records only."""

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
