"""Prometheus instruments with an enforced, bounded label contract.

Metric objects are deliberately kept private.  Callers update them through
methods which validate every label before it can create a time series.
"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field
from typing import Iterable

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    multiprocess,
)


MAX_CAMERAS = 8
IDENTIFIER = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,61}[a-z0-9])?$")
VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")

DEFAULT_USE_CASES = frozenset(
    {
        "anpr",
        "attendance",
        "face-enrollment",
        "face-recognition",
        "presence",
        "reporting",
        "traffic",
        "vehicle-reporting",
    }
)
DEFAULT_SERVICES = frozenset(
    {
        "alert-dispatcher",
        "edge-management",
        "face-recognition",
        "anpr",
        "presence",
        "reporting",
        "traffic-runtime",
    }
)
DEFAULT_REASONS = frozenset(
    {
        "AUTH_FAILED",
        "CONFIG_INVALID",
        "CONNECTION_REFUSED",
        "DATABASE_UNAVAILABLE",
        "DECODE_FAILED",
        "INTERNAL_ERROR",
        "MEDIA_TIMEOUT",
        "NETWORK_TIMEOUT",
        "NOT_FOUND",
        "PROBE_INTERNAL_ERROR",
        "QUEUE_FULL",
        "RATE_LIMITED",
        "RTSP_AUTH_FAILED",
        "RTSP_NEGOTIATION_FAILED",
        "RTSP_PATH_NOT_FOUND",
        "SYNC_APPLY_FAILED",
        "UNSUPPORTED_CODEC",
        "UNKNOWN",
    }
)
DEFAULT_ROUTES = frozenset(
    {
        "/metrics",
        "/docs",
        "/docs/oauth2-redirect",
        "/healthz",
        "/openapi.json",
        "/redoc",
        "/readyz",
        "/api/v1/health",
        "/api/v1/cluster",
        "/api/v1/site",
        "/api/v1/alerts",
        "/api/v1/alerts/{alert_id}/acknowledge",
        "/api/v1/alerts/{alert_id}/notifications",
        "/api/v1/cameras",
        "/api/v1/cameras/{camera_id}",
        "/api/v1/cameras/{camera_id}/credentials",
        "/api/v1/cameras/{camera_id}/enabled",
        "/api/v1/cameras/{camera_id}/roles",
        "/api/v1/cameras/{camera_id}/stream",
        "/api/v1/cameras/{camera_id}/validate",
        "/api/v1/cameras/{camera_id}/validation-attempts",
        "/api/v1/deployments",
        "/api/v1/deployments/{deployment_id}/assignments",
        "/api/v1/deployments/{deployment_id}/rollback",
        "/api/v1/deployments/{deployment_id}/start",
        "/api/v1/deployments/{deployment_id}/stop",
        "/api/v1/discovery-runs",
        "/api/v1/discovery-runs/{operation_id}",
        "/internal/v1/alerts/alertmanager",
        "/internal/v1/sites",
        "/internal/v1/validation-attempts/{attempt_id}/result",
        "__unmatched__",
    }
)


class MetricsContractError(ValueError):
    """Raised before an unsafe or unbounded Prometheus label is emitted."""


def _set(values: Iterable[str]) -> frozenset[str]:
    return frozenset(str(value) for value in values)


@dataclass
class LabelPolicy:
    """Allowlist and cardinality guard for every product metric label."""

    use_cases: frozenset[str] = DEFAULT_USE_CASES
    services: frozenset[str] = DEFAULT_SERVICES
    reasons: frozenset[str] = DEFAULT_REASONS
    routes: frozenset[str] = DEFAULT_ROUTES
    max_cameras: int = MAX_CAMERAS
    _camera_ids: set[str] = field(default_factory=set, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    @classmethod
    def configured(
        cls,
        *,
        camera_ids: Iterable[str] = (),
        use_cases: Iterable[str] = DEFAULT_USE_CASES,
        services: Iterable[str] = DEFAULT_SERVICES,
        reasons: Iterable[str] = DEFAULT_REASONS,
        routes: Iterable[str] = DEFAULT_ROUTES,
    ) -> "LabelPolicy":
        policy = cls(_set(use_cases), _set(services), _set(reasons), _set(routes))
        for camera_id in camera_ids:
            policy.camera_id(camera_id)
        return policy

    @staticmethod
    def _allowed(name: str, value: str, allowed: frozenset[str]) -> str:
        if value not in allowed:
            raise MetricsContractError(f"{name} is outside its bounded allowlist")
        return value

    def camera_id(self, value: str) -> str:
        if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
            raise MetricsContractError("camera_id must be a stable DNS-safe identifier")
        with self._lock:
            if value not in self._camera_ids and len(self._camera_ids) >= self.max_cameras:
                raise MetricsContractError(
                    f"camera_id cardinality exceeds the TVT ceiling of {self.max_cameras}"
                )
            self._camera_ids.add(value)
        return value

    def use_case(self, value: str) -> str:
        return self._allowed("use_case", value, self.use_cases)

    def service(self, value: str) -> str:
        return self._allowed("service", value, self.services)

    def reason(self, value: str) -> str:
        return self._allowed("reason/error_code", value, self.reasons)

    def route(self, value: str | None) -> str:
        return self._allowed("route", value or "__unmatched__", self.routes)

    @staticmethod
    def method(value: str) -> str:
        normalized = value.upper()
        if normalized not in {"DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"}:
            raise MetricsContractError("HTTP method is outside its bounded allowlist")
        return normalized

    @staticmethod
    def status_class(status_code: int) -> str:
        if status_code < 100 or status_code > 599:
            return "unknown"
        return f"{status_code // 100}xx"

    @staticmethod
    def version(value: str) -> str:
        if not VERSION.fullmatch(value):
            raise MetricsContractError("version is not a bounded version identifier")
        return value


class HttpMetrics:
    """Bounded RED metrics for one HTTP service."""

    def __init__(
        self,
        service: str = "edge-management",
        *,
        registry: CollectorRegistry | None = None,
        policy: LabelPolicy | None = None,
    ) -> None:
        self.registry = registry or CollectorRegistry()
        self.policy = policy or LabelPolicy()
        self.service = self.policy.service(service)

        self._http_requests = Counter(
            "http_requests_total", "Completed HTTP requests", ("service", "method", "route", "status_class"), registry=self.registry
        )
        self._http_duration = Histogram(
            "http_request_duration_seconds", "HTTP request duration", ("service", "method", "route"),
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10), registry=self.registry,
        )
        self._http_in_progress = Gauge(
            "http_requests_in_progress", "HTTP requests currently running", ("service",), multiprocess_mode="livesum", registry=self.registry
        )
        self._application_errors = Counter(
            "application_errors_total", "Bounded application errors", ("service", "error_code"), registry=self.registry
        )
        self._build_info = Gauge(
            "application_build_info", "Application build metadata", ("service", "version"), registry=self.registry
        )

    def set_build(self, version: str) -> None:
        self._build_info.labels(self.service, self.policy.version(version)).set(1)

    def http_started(self) -> None:
        self._http_in_progress.labels(self.service).inc()

    def http_finished(self, method: str, route: str | None, status_code: int, duration: float) -> None:
        labels = (self.service, self.policy.method(method), self.policy.route(route))
        self._http_in_progress.labels(self.service).dec()
        self._http_requests.labels(*labels, self.policy.status_class(status_code)).inc()
        self._http_duration.labels(*labels).observe(max(0.0, duration))

    def application_error(self, error_code: str) -> None:
        self._application_errors.labels(self.service, self.policy.reason(error_code)).inc()


class EdgeMetrics(HttpMetrics):
    """Product metrics shared by the host edge runtime and CV apps."""

    def __init__(
        self,
        service: str = "edge-management",
        *,
        registry: CollectorRegistry | None = None,
        policy: LabelPolicy | None = None,
    ) -> None:
        super().__init__(service, registry=registry, policy=policy)

        self._camera_discovered = Gauge("edge_camera_discovered", "Camera is present in inventory", ("camera_id",), registry=self.registry)
        self._camera_enabled = Gauge("edge_camera_enabled", "Camera is enabled", ("camera_id",), registry=self.registry)
        self._camera_valid = Gauge("edge_camera_rtsp_valid", "Last RTSP validation succeeded", ("camera_id",), registry=self.registry)
        self._camera_last_seen = Gauge("edge_camera_last_seen_timestamp_seconds", "Last camera observation", ("camera_id",), registry=self.registry)
        self._validation_last_success = Gauge("edge_camera_validation_last_success_timestamp_seconds", "Last successful RTSP validation", ("camera_id",), registry=self.registry)
        self._validation_failures = Counter("edge_camera_validation_failures_total", "RTSP validation failures", ("camera_id", "reason"), registry=self.registry)
        self._validation_duration = Histogram("edge_camera_validation_duration_seconds", "RTSP validation duration", ("camera_id",), buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30), registry=self.registry)
        self._assigned_consumers = Gauge("edge_camera_assigned_consumers", "Assigned camera consumers", ("camera_id",), registry=self.registry)
        self._sync_pending = Gauge("edge_camera_sync_pending", "Camera configuration is pending", ("camera_id",), registry=self.registry)
        self._sync_attempts = Counter("edge_camera_config_sync_attempts_total", "Camera configuration sync attempts", ("camera_id", "result"), registry=self.registry)
        self._sync_duration = Histogram("edge_camera_config_sync_duration_seconds", "Camera configuration sync duration", buckets=(0.1, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300), registry=self.registry)
        self._sync_oldest = Gauge("edge_camera_config_sync_oldest_pending_age_seconds", "Age of oldest pending camera sync", registry=self.registry)

        self._source_up = Gauge("cv_source_up", "Camera source is readable", ("camera_id", "use_case"), registry=self.registry)
        self._source_last_media = Gauge("cv_source_last_media_timestamp_seconds", "Last source media timestamp", ("camera_id", "use_case"), registry=self.registry)
        self._source_bitrate = Gauge("cv_source_bitrate_bytes_per_second", "Source bitrate", ("camera_id", "use_case"), registry=self.registry)
        self._source_reconnects = Counter("cv_source_reconnects_total", "Source reconnects", ("camera_id", "use_case", "reason"), registry=self.registry)
        self._source_packets = Counter("cv_source_packets_received_total", "Source packets received", ("camera_id", "use_case"), registry=self.registry)
        self._source_drops = Counter("cv_source_packets_dropped_total", "Source packets dropped", ("camera_id", "use_case", "reason"), registry=self.registry)
        self._source_sessions = Gauge("cv_source_active_sessions", "Active physical camera sessions", ("camera_id",), registry=self.registry)
        self._source_fps = Gauge("cv_source_observed_fps", "Observed source frame rate", ("camera_id", "use_case"), registry=self.registry)
        self._decode_errors = Counter("cv_source_decode_errors_total", "Source decode errors", ("camera_id", "use_case", "reason"), registry=self.registry)
        self._reconnect_duration = Histogram("cv_source_reconnect_duration_seconds", "Source reconnect duration", ("use_case",), buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60), registry=self.registry)

        self._frames_received = Counter("cv_frames_received_total", "Frames received for inference", ("camera_id", "use_case"), registry=self.registry)
        self._frames_processed = Counter("cv_frames_processed_total", "Frames processed", ("camera_id", "use_case"), registry=self.registry)
        self._frames_dropped = Counter("cv_frames_dropped_total", "Frames dropped", ("camera_id", "use_case", "reason"), registry=self.registry)
        self._inference_attempts = Counter("cv_inference_attempts_total", "Inference attempts", ("camera_id", "use_case"), registry=self.registry)
        self._inference_errors = Counter("cv_inference_errors_total", "Inference errors", ("camera_id", "use_case", "reason"), registry=self.registry)
        self._inference_duration = Histogram("cv_inference_duration_seconds", "Inference duration", ("use_case",), buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5), registry=self.registry)
        self._queue_depth = Gauge("cv_queue_depth", "Inference queue depth", ("use_case",), multiprocess_mode="livesum", registry=self.registry)
        self._last_success = Gauge("cv_last_success_timestamp_seconds", "Last successful inference", ("camera_id", "use_case"), registry=self.registry)
        self._model_ready = Gauge("cv_model_ready", "Model readiness", ("use_case", "model_version"), multiprocess_mode="livemax", registry=self.registry)

    def camera_state(self, camera_id: str, *, discovered: bool, enabled: bool, rtsp_valid: bool, last_seen: float | None = None, assigned_consumers: int | None = None, sync_pending: bool | None = None) -> None:
        camera = self.policy.camera_id(camera_id)
        self._camera_discovered.labels(camera).set(bool(discovered))
        self._camera_enabled.labels(camera).set(bool(enabled))
        self._camera_valid.labels(camera).set(bool(rtsp_valid))
        if last_seen is not None:
            self._camera_last_seen.labels(camera).set(max(0.0, last_seen))
        if assigned_consumers is not None:
            self._assigned_consumers.labels(camera).set(max(0, assigned_consumers))
        if sync_pending is not None:
            self._sync_pending.labels(camera).set(bool(sync_pending))

    def validation(self, camera_id: str, duration: float, *, result: str, completed_at: float | None = None) -> None:
        camera = self.policy.camera_id(camera_id)
        self._validation_duration.labels(camera).observe(max(0.0, duration))
        if result == "OK":
            self._camera_valid.labels(camera).set(1)
            if completed_at is not None:
                self._validation_last_success.labels(camera).set(max(0.0, completed_at))
        else:
            self._camera_valid.labels(camera).set(0)
            self._validation_failures.labels(camera, self.policy.reason(result)).inc()

    def sync_attempt(self, camera_id: str, result: str) -> None:
        bounded = self.policy._allowed("result", result, frozenset({"succeeded", "failed"}))
        self._sync_attempts.labels(self.policy.camera_id(camera_id), bounded).inc()

    def observe_sync_duration(self, duration: float) -> None:
        self._sync_duration.observe(max(0.0, duration))

    def set_oldest_sync_age(self, seconds: float) -> None:
        self._sync_oldest.set(max(0.0, seconds))

    def source_state(self, camera_id: str, use_case: str, *, up: bool, last_media: float | None = None, bitrate: float | None = None, fps: float | None = None) -> None:
        labels = (self.policy.camera_id(camera_id), self.policy.use_case(use_case))
        self._source_up.labels(*labels).set(bool(up))
        if last_media is not None:
            self._source_last_media.labels(*labels).set(max(0.0, last_media))
        if bitrate is not None:
            self._source_bitrate.labels(*labels).set(max(0.0, bitrate))
        if fps is not None:
            self._source_fps.labels(*labels).set(max(0.0, fps))

    def source_event(self, metric: str, camera_id: str, use_case: str, amount: float = 1, *, reason: str | None = None) -> None:
        labels = (self.policy.camera_id(camera_id), self.policy.use_case(use_case))
        if metric == "packets_received":
            self._source_packets.labels(*labels).inc(max(0.0, amount))
            return
        target = {"reconnect": self._source_reconnects, "packet_drop": self._source_drops, "decode_error": self._decode_errors}.get(metric)
        if target is None or reason is None:
            raise MetricsContractError("source event and reason combination is invalid")
        target.labels(*labels, self.policy.reason(reason)).inc(max(0.0, amount))

    def set_source_sessions(self, camera_id: str, sessions: int) -> None:
        self._source_sessions.labels(self.policy.camera_id(camera_id)).set(max(0, sessions))

    def observe_reconnect(self, use_case: str, duration: float) -> None:
        self._reconnect_duration.labels(self.policy.use_case(use_case)).observe(max(0.0, duration))

    def inference_event(self, metric: str, camera_id: str, use_case: str, amount: float = 1, *, reason: str | None = None) -> None:
        labels = (self.policy.camera_id(camera_id), self.policy.use_case(use_case))
        simple = {"received": self._frames_received, "processed": self._frames_processed, "attempt": self._inference_attempts}
        if metric in simple:
            simple[metric].labels(*labels).inc(max(0.0, amount))
            return
        target = {"dropped": self._frames_dropped, "error": self._inference_errors}.get(metric)
        if target is None or reason is None:
            raise MetricsContractError("inference event and reason combination is invalid")
        target.labels(*labels, self.policy.reason(reason)).inc(max(0.0, amount))

    def observe_inference(self, use_case: str, duration: float) -> None:
        self._inference_duration.labels(self.policy.use_case(use_case)).observe(max(0.0, duration))

    def set_inference_state(self, camera_id: str, use_case: str, *, queue_depth: int | None = None, last_success: float | None = None) -> None:
        camera = self.policy.camera_id(camera_id)
        use = self.policy.use_case(use_case)
        if queue_depth is not None:
            self._queue_depth.labels(use).set(max(0, queue_depth))
        if last_success is not None:
            self._last_success.labels(camera, use).set(max(0.0, last_success))

    def set_model_ready(self, use_case: str, model_version: str, ready: bool) -> None:
        self._model_ready.labels(self.policy.use_case(use_case), self.policy.version(model_version)).set(bool(ready))


class AlertDispatcherMetrics:
    """Metrics owned by the host alert dispatcher."""

    SOURCES = frozenset({"alertmanager", "host"})
    STATUSES = frozenset({"firing", "resolved", "unknown"})
    RESULTS = frozenset({"accepted", "rejected", "persistence_error"})
    SEVERITIES = frozenset({"critical", "warning", "info"})
    OUTBOX_STATES = frozenset({"pending", "delivering", "sent", "failed", "expired"})
    DELIVERY_RESULTS = frozenset({"sent", "transient_failure", "permanent_failure", "expired"})

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry()
        self._events = Counter("tvt_alert_events_total", "Alert events received", ("source", "status", "result"), registry=self.registry)
        self._active = Gauge("tvt_alerts_active", "Active alerts", ("severity",), registry=self.registry)
        self._outbox = Gauge("tvt_notification_outbox_depth", "Notification outbox items", ("state",), registry=self.registry)
        self._oldest = Gauge("tvt_notification_oldest_pending_age_seconds", "Age of oldest pending notification", registry=self.registry)
        self._delivery_attempts = Counter("tvt_notification_delivery_attempts_total", "Notification delivery attempts", ("result",), registry=self.registry)
        self._delivery_duration = Histogram("tvt_notification_delivery_duration_seconds", "Notification delivery duration", ("result",), buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30), registry=self.registry)
        self._last_success = Gauge("tvt_notification_last_success_timestamp_seconds", "Last successful notification delivery", registry=self.registry)
        self._spool = Gauge("tvt_alert_emergency_spool_items", "Emergency alert spool items", registry=self.registry)

    @staticmethod
    def _bounded(name: str, value: str, allowed: frozenset[str]) -> str:
        if value not in allowed:
            raise MetricsContractError(f"{name} is outside its bounded allowlist")
        return value

    def event(self, source: str, status: str, result: str, amount: int = 1) -> None:
        self._events.labels(self._bounded("source", source, self.SOURCES), self._bounded("status", status, self.STATUSES), self._bounded("result", result, self.RESULTS)).inc(max(0, amount))

    def snapshot(self, *, active: dict[str, int], outbox: dict[str, int], oldest_pending_age: float) -> None:
        for severity in self.SEVERITIES:
            self._active.labels(severity).set(max(0, active.get(severity, 0)))
        for state in self.OUTBOX_STATES:
            self._outbox.labels(state).set(max(0, outbox.get(state, 0)))
        self._oldest.set(max(0.0, oldest_pending_age))

    def delivery(self, result: str, duration: float, *, completed_at: float | None = None) -> None:
        bounded = self._bounded("result", result, self.DELIVERY_RESULTS)
        self._delivery_attempts.labels(bounded).inc()
        self._delivery_duration.labels(bounded).observe(max(0.0, duration))
        if bounded == "sent" and completed_at is not None:
            self._last_success.set(max(0.0, completed_at))

    def set_emergency_spool_items(self, count: int) -> None:
        self._spool.set(max(0, count))


def render_metrics(registry: CollectorRegistry) -> tuple[bytes, str]:
    """Render a registry, using the mandated clean multiprocess collector."""

    if os.getenv("PROMETHEUS_MULTIPROC_DIR"):
        scrape_registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(scrape_registry)
    else:
        scrape_registry = registry
    return generate_latest(scrape_registry), CONTENT_TYPE_LATEST
