"""Loss-tolerant polling adapter from Apex telemetry to the reporting store."""

from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
import urllib.request
from datetime import datetime, timezone
from typing import Any, Iterable

from tvt_edge.observability import get_logger
from tvt_edge.reporting.database import ReportingStore
from tvt_edge.reporting.settings import ReportingSettings


LOGGER = get_logger(__name__)
PLATE = re.compile(r"^[A-Z0-9]{4,20}$")


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _plate(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = unicodedata.normalize("NFKC", value).upper()
    normalized = re.sub(r"[\s-]+", "", normalized)
    return normalized if PLATE.fullmatch(normalized) else None


def fetch_events(settings: ReportingSettings) -> list[dict[str, Any]]:
    request = urllib.request.Request(
        f"{settings.apex_url}/api/telemetry/events?limit=1000",
        headers={"Accept": "application/json"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        document = json.load(response)
    events = document.get("events") if isinstance(document, dict) else None
    if not isinstance(events, list):
        raise ValueError("Apex telemetry response does not contain an events list")
    return [item for item in events if isinstance(item, dict)]


def ingest_events(
    store: ReportingStore,
    settings: ReportingSettings,
    events: Iterable[dict[str, Any]],
) -> int:
    accepted = 0
    for item in events:
        payload = item.get("payload")
        if not isinstance(payload, dict):
            continue
        if payload.get("event_type") != "plate_read_event" or payload.get("application") != "anpr":
            continue
        camera_id = payload.get("camera_id")
        if not isinstance(camera_id, str) or camera_id not in settings.camera_ids:
            continue
        event_id = item.get("event_id")
        if not isinstance(event_id, str) or not event_id or len(event_id) > 512:
            continue
        event_payload = payload.get("payload")
        if not isinstance(event_payload, dict):
            continue
        plate_object = event_payload.get("plate")
        plate_text = _plate(plate_object.get("text") if isinstance(plate_object, dict) else None)
        occurred = _timestamp(item.get("occurred_at") or payload.get("timestamp"))
        if plate_text is None or occurred is None:
            continue
        local = occurred.astimezone(settings.timezone)
        if not settings.window_start <= local.time().replace(tzinfo=None) < settings.window_end:
            continue
        received_at = item.get("received_at")
        if not isinstance(received_at, (float, int)):
            received_at = time.time()
        if store.record_observation(
            event_id=event_id,
            received_at=float(received_at),
            report_date=local.date(),
            plate_key=hashlib.sha256(plate_text.encode("utf-8")).hexdigest(),
            plate_text=plate_text,
            occurred_at=occurred.timestamp(),
            camera_id=camera_id,
        ):
            accepted += 1
    return accepted


def collect_forever(store: ReportingStore, settings: ReportingSettings) -> None:
    LOGGER.info("ANPR report collector started", extra={"event": "anpr_collector_started"})
    last_prune = 0.0
    while True:
        try:
            accepted = ingest_events(store, settings, fetch_events(settings))
            if accepted:
                LOGGER.info(
                    "ANPR observations recorded",
                    extra={"event": "anpr_observations_recorded"},
                )
            now = time.time()
            if now - last_prune >= 3600:
                store.prune(retention_days=settings.retention_days)
                last_prune = now
        except Exception:
            LOGGER.error(
                "ANPR telemetry collection failed",
                extra={"event": "anpr_collection_failed", "error_code": "TELEMETRY_UNAVAILABLE"},
                exc_info=True,
            )
        time.sleep(settings.poll_interval)
