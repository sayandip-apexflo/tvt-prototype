"""Face-enrollment session state machine support.

Owns two things kept deliberately separate from tvt_edge/service.py's DB
transactions:

1. Pure, database-free capture-eligibility rules (this module can be unit
   tested without a database or apexfabric-control).
2. EnrollmentReconciler, a bounded background loop that calls
   ManagementService.reconcile_enrollment_sessions on an interval from
   inside the management API process -- see tvt_edge/api/app.py's
   create_app(start_enrollment_reconciler=...). This replaces relying on
   the once-daily retention timer for interactive enrollment timeouts.

See docs/contracts/tvt-mills-v1/README.md for why enrollment borrows an
existing face_recognition camera instead of a dedicated one, and AGENTS.md
section 6 for why apexfabric/control_plane (the Apex identity/business-data
plane that actually resolves embeddings into persons) is never edited here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

ACTIVE_STATUSES = frozenset({"activating", "capturing", "restoring"})
TERMINAL_STATUSES = frozenset({"completed", "timed_out", "cancelled", "failed"})
ALL_STATUSES = ACTIVE_STATUSES | TERMINAL_STATUSES

NAMING_STATUSES = frozenset({"not_applicable", "pending_name", "named", "discarded"})
CAPTURE_RESULTS = frozenset({"created", "duplicate"})

# A session collects up to MAX_ENROLLMENT_CAPTURES faces: it stops capturing
# once it has that many, or CAPTURE_SETTLE_SECONDS after the first one, or at
# the capture deadline. Several frames per person give recognition cameras
# more than one reference view -- recognition itself never adds faces.
MAX_ENROLLMENT_CAPTURES = 5
CAPTURE_SETTLE_SECONDS = 10
# Captured faces nobody names are discarded after this long; must stay below
# apexfabric/control_plane/identity.py ENROLLMENT_CAPTURE_MAX_AGE_SECONDS.
NAMING_TIMEOUT_SECONDS = 900
UNNAMED_DISCARDED_MESSAGE = "No record created for unnamed person"

DEFAULT_CAPTURE_WINDOW_SECONDS = 300
MIN_CAPTURE_WINDOW_SECONDS = 30
MAX_CAPTURE_WINDOW_SECONDS = 1800
DEFAULT_ACTIVATION_TIMEOUT_SECONDS = 180
DEFAULT_CLOCK_TOLERANCE_SECONDS = 15
DEFAULT_RECONCILE_INTERVAL_SECONDS = 2.0

# Bounded, documented in METRICS.md/MONITORING.md alongside metrics.py's
# DEFAULT_REASONS -- these are the only enrollment-specific error_codes an
# actionable failure may use.
RESULT_CODES = frozenset({"ok", "timed_out", "cancelled", "activation_failed"})
# The terminal status a session reaches once its (possibly fallback)
# restoration is confirmed applied, keyed by the result_code recorded when
# it left 'capturing'/'activating' for 'restoring' -- see the state machine
# in docs/contracts/tvt-mills-v1/README.md and AGENTS.md's enrollment task.
RESULT_CODE_TERMINAL_STATUS = {
    "ok": "completed",
    "timed_out": "timed_out",
    "cancelled": "cancelled",
    "activation_failed": "failed",
}
ENROLLMENT_ERROR_CODES = frozenset(
    {
        "ENROLLMENT_ACTIVATION_FAILED",
        "ENROLLMENT_CAPTURE_REJECTED",
        "ENROLLMENT_TIMEOUT",
        "ENROLLMENT_RESTORE_DEGRADED",
        "ENROLLMENT_UNNAMED_DISCARDED",
    }
)

REQUIRED_EVENT_TYPE = "enrollment_capture_event"
REQUIRED_APPLICATION = "face_enrollment"


@dataclass(frozen=True)
class CaptureCandidate:
    event_id: str
    occurred_at: datetime


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def quality_passes(quality: Any, minimum_sharpness: float | None) -> bool:
    """Reads only the vendor's optional, non-sensitive quality block --
    never payload["payload"]["embeddings"]. A missing block, or a missing
    sharpness field, passes (the vendor schema documents both as optional);
    a present value below the configured floor is rejected."""

    if minimum_sharpness is None:
        return True
    if not isinstance(quality, dict):
        return True
    sharpness = quality.get("sharpness")
    if sharpness is None:
        return True
    try:
        return float(sharpness) >= minimum_sharpness
    except (TypeError, ValueError):
        return False


def eligible_capture_candidate(
    event: dict[str, Any],
    *,
    deployment_key: str,
    camera_key: str,
    window_start: datetime,
    window_end: datetime,
    minimum_sharpness: float | None = None,
    clock_tolerance_seconds: int = DEFAULT_CLOCK_TOLERANCE_SECONDS,
) -> CaptureCandidate | None:
    """Evaluate one /api/telemetry/events envelope (tvt_edge/apex_client.py
    ApexClient.recent_events) against every capture eligibility rule except
    session-state and idempotency -- the caller only evaluates events while
    its session is 'capturing' and stops evaluating once one is accepted
    (see ManagementService.reconcile_enrollment_sessions), so those two are
    trivially satisfied by construction.

    Reads only the envelope's own routing fields and the vendor's optional
    non-sensitive quality block; never payload["payload"]["embeddings"].
    Vector shape/normalization validity is enforced authoritatively by
    apexfabric/control_plane/identity.py (frozen reference plane, the
    "Apex identity/business-data plane") when it resolves the same event --
    duplicating that check here would mean carrying a second copy of face
    embedding dimension/threshold configuration into the TVT management
    plane for no additional safety.
    """

    apex_event_id = event.get("event_id")
    if not isinstance(apex_event_id, str) or not apex_event_id:
        return None
    if event.get("deployment_id") != deployment_key:
        return None
    envelope = event.get("payload")
    if not isinstance(envelope, dict):
        return None
    if envelope.get("event_type") != REQUIRED_EVENT_TYPE:
        return None
    if envelope.get("application") != REQUIRED_APPLICATION:
        return None
    if envelope.get("camera_id") != camera_key:
        return None
    occurred_at = _parse_timestamp(event.get("occurred_at")) or _parse_timestamp(envelope.get("timestamp"))
    if occurred_at is None:
        return None
    tolerance = timedelta(seconds=max(0, clock_tolerance_seconds))
    if occurred_at < window_start - tolerance or occurred_at > window_end + tolerance:
        return None
    inner = envelope.get("payload")
    quality = inner.get("quality") if isinstance(inner, dict) else None
    if not quality_passes(quality, minimum_sharpness):
        return None
    return CaptureCandidate(event_id=apex_event_id, occurred_at=occurred_at)


def has_rejected_capture_attempt(
    events: list[dict[str, Any]], *, deployment_key: str, camera_key: str
) -> bool:
    """True if at least one enrollment_capture_event for this camera arrived
    but failed some eligibility rule (window, quality, ...) -- used only to
    decide whether a poll tick is worth a bounded ENROLLMENT_CAPTURE_REJECTED
    metric/log; never inspects payload["payload"]."""

    for event in events:
        envelope = event.get("payload")
        if not isinstance(envelope, dict):
            continue
        if (
            event.get("deployment_id") == deployment_key
            and envelope.get("event_type") == REQUIRED_EVENT_TYPE
            and envelope.get("application") == REQUIRED_APPLICATION
            and envelope.get("camera_id") == camera_key
        ):
            return True
    return False


def eligible_capture_candidates(events: list[dict[str, Any]], **kwargs: Any) -> list[CaptureCandidate]:
    """Every eligible candidate in this poll, earliest occurred_at first."""

    candidates = [
        candidate
        for candidate in (eligible_capture_candidate(event, **kwargs) for event in events)
        if candidate is not None
    ]
    return sorted(candidates, key=lambda item: (item.occurred_at, item.event_id))


def select_first_capture(events: list[dict[str, Any]], **kwargs: Any) -> CaptureCandidate | None:
    """The first accepted capture wins (bounded rule from the enrollment
    contract): among every eligible candidate in this poll, return the one
    with the earliest occurred_at."""

    candidates = [
        candidate
        for candidate in (eligible_capture_candidate(event, **kwargs) for event in events)
        if candidate is not None
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda item: item.occurred_at)


class EnrollmentReconciler:
    """Bounded worker for interactive enrollment timing.

    `run_once()` advances every non-terminal enrollment session by one step
    and handles this tick's observability (bounded error metric +
    correlated JSON log); the transactional work itself lives in
    ManagementService.reconcile_enrollment_sessions. tvt_edge/api/app.py's
    create_app schedules run_once() on a short asyncio interval inside the
    management API process's own event loop (the same lifespan-task pattern
    tvt_edge/alerting/receiver.py's create_alert_app already uses for
    OutboxWorker) so an active session is checked every `interval_seconds`
    (default 2s) instead of depending on the once-daily retention timer.
    """

    def __init__(
        self,
        service: Any,
        apex: Any,
        *,
        metrics: Any | None = None,
        logger: Any | None = None,
        interval_seconds: float = DEFAULT_RECONCILE_INTERVAL_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.service = service
        self.apex = apex
        self.metrics = metrics
        self.logger = logger
        self.interval_seconds = max(0.5, interval_seconds)
        self._clock = clock

    def run_once(self) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {}
        if self._clock is not None:
            kwargs["now"] = self._clock()
        results = self.service.reconcile_enrollment_sessions(self.apex, **kwargs)
        for item in results:
            session_id = item.get("session_id")
            error_code = item.get("error_code")
            if error_code:
                if self.metrics is not None:
                    self.metrics.application_error(error_code)
                if self.logger is not None:
                    self.logger.error(
                        "Enrollment session transition failed",
                        extra={
                            "event": "enrollment_session_error",
                            "error_code": error_code,
                            "operation_id": str(session_id) if session_id else None,
                            "result": item.get("transition"),
                        },
                    )
            elif self.logger is not None and item.get("transition"):
                self.logger.info(
                    "Enrollment session transitioned",
                    extra={
                        "event": "enrollment_session_transition",
                        "error_code": "NONE",
                        "operation_id": str(session_id) if session_id else None,
                        "result": item.get("transition"),
                    },
                )
        return results
