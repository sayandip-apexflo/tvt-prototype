"""Camera onboarding and validation state transitions."""

from __future__ import annotations

from dataclasses import dataclass


class CameraStateError(ValueError):
    """Raised when an invalid camera state transition is requested."""


STATES = {
    "discovered",
    "needs_credentials",
    "validating",
    "online",
    "offline",
    "invalid",
    "disabled",
    "deleted",
}

VALIDATION_SUCCESS = "OK"

RETRYABLE_FAILURES = {
    "NETWORK_TIMEOUT",
    "CONNECTION_REFUSED",
    "DNS_FAILED",
    "RTSP_AUTH_FAILED",
    "RTSP_PATH_NOT_FOUND",
    "RTSP_NEGOTIATION_FAILED",
    "UNSUPPORTED_CODEC",
    "MEDIA_TIMEOUT",
    "DECODE_FAILED",
    "PROBE_INTERNAL_ERROR",
}


@dataclass(frozen=True)
class CameraTransition:
    from_state: str
    to_state: str
    result_code: str | None = None


def normalize_state(value: str) -> str:
    if value not in STATES:
        raise CameraStateError(f"unknown camera state: {value!r}")
    return value


def should_retry_validation(result_code: str) -> bool:
    """Return true if the RTSP result is eligible for retry."""

    return result_code in RETRYABLE_FAILURES


def validation_delay_seconds(failures: int, *, cap: int = 300) -> int:
    if failures <= 0:
        return 0
    return min(cap, 2 ** min(failures, 8))


def after_validation(current_state: str, result_code: str) -> str:
    """Compute onboarding state after RTSP validation completion."""

    normalize_state(current_state)
    if result_code == VALIDATION_SUCCESS:
        # A validated stream is always an online observation.
        # Enablement is handled by desired operator state.
        return "online"
    if result_code == "RTSP_AUTH_FAILED":
        return "needs_credentials"
    return "invalid"


def after_enable(current_state: str, has_stream: bool = False) -> str:
    """Compute state after operator enables a camera."""

    normalize_state(current_state)
    if current_state in {"discovered", "needs_credentials", "invalid", "validating"}:
        return "validating" if has_stream else "discovered"
    if current_state == "offline":
        return "validating"
    if current_state == "disabled":
        return "validating"
    return current_state


def after_disable(current_state: str) -> str:
    """Compute state after operator disables a camera."""

    normalize_state(current_state)
    if current_state == "deleted":
        return "deleted"
    return "disabled"


def after_stream_configured(current_state: str, *, has_credentials: bool) -> str:
    """Transition used when a stream path/profile becomes available."""

    normalize_state(current_state)
    if current_state == "discovered" and has_credentials:
        return "validating"
    if current_state == "needs_credentials" and has_credentials:
        return "validating"
    return current_state


def transition_for_discovery(current_state: str, *, method: str) -> str:
    """Transition during discovery for an observed camera."""

    normalize_state(current_state)
    if method in {"tcp", "neighbor", "onvif"} and current_state == "discovered":
        return "discovered"
    return current_state


__all__ = [
    "CameraTransition",
    "CameraStateMachine",
    "CameraStateError",
    "STATES",
    "VALIDATION_SUCCESS",
    "RETRYABLE_FAILURES",
    "after_disable",
    "after_enable",
    "after_stream_configured",
    "after_validation",
    "normalize_state",
    "should_retry_validation",
    "transition_for_discovery",
    "validation_delay_seconds",
]


class CameraStateMachine:
    """Small namespace for backwards-compatible state transitions."""

    STATES = STATES
    VALIDATION_SUCCESS = VALIDATION_SUCCESS
    RETRYABLE_FAILURES = RETRYABLE_FAILURES

    after_disable = staticmethod(after_disable)
    after_enable = staticmethod(after_enable)
    after_stream_configured = staticmethod(after_stream_configured)
    after_validation = staticmethod(after_validation)
    should_retry_validation = staticmethod(should_retry_validation)
    transition_for_discovery = staticmethod(transition_for_discovery)
    normalize_state = staticmethod(normalize_state)
    validation_delay_seconds = staticmethod(validation_delay_seconds)

