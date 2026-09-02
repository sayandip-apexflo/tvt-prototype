"""Health aggregation shared by the TVT API and command line."""

from __future__ import annotations

from typing import Any


GOOD_COMPONENT_STATES = {"healthy", "unconfigured"}


def aggregate_health(
    management: dict[str, Any] | None,
    cluster: dict[str, Any],
    *,
    database_status: str = "healthy",
) -> dict[str, Any]:
    """Build independent component health without hiding degraded dependencies."""

    if management is None:
        cameras = {"status": "unavailable"}
        synchronization = {"status": "unavailable"}
    else:
        cameras = management["cameras"]
        synchronization = management["synchronization"]

    components = {
        "host": {"status": "healthy"},
        "database": {"status": database_status},
        "k3s_api": dict(cluster["api"]),
        "node": {
            key: value
            for key, value in cluster["nodes"].items()
            if key != "items"
        },
        "camera_validation": cameras,
        "secret_synchronization": synchronization,
        "workloads": {
            "status": cluster["workloads"]["status"],
            "deployments": cluster["workloads"]["deployments"]["total"],
            "pods": cluster["workloads"]["pods"]["total"],
        },
    }
    states = [component["status"] for component in components.values()]
    if database_status == "unavailable":
        overall = "unavailable"
    elif all(state in GOOD_COMPONENT_STATES for state in states):
        overall = "healthy"
    else:
        overall = "degraded"
    return {
        "status": overall,
        # Preserve the original lightweight health fields for existing callers.
        "service": "healthy",
        "database": database_status,
        "components": components,
    }


__all__ = ["aggregate_health"]
