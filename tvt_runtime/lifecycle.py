"""Pure helpers for declarative Solution Pack lifecycle operations."""

from __future__ import annotations

import copy
from typing import Any


def with_desired_state(
    bundle: dict[str, Any], desired_state: str
) -> dict[str, Any]:
    if desired_state not in {"Running", "Stopped"}:
        raise ValueError("desired_state must be Running or Stopped")
    updated = copy.deepcopy(bundle)
    for application in updated["applications"]:
        lifecycle = application.setdefault("lifecycle", {})
        lifecycle["desired_state"] = desired_state
    return updated


def camera_contract_signature(bundle: dict[str, Any]) -> tuple[Any, ...]:
    """Return non-secret camera/mount intent used to gate safe rollback."""

    signatures = []
    for application in bundle.get("applications", []):
        camera_mounts = [
            mount
            for mount in application.get("external_mounts", [])
            if mount.get("source", {}).get("type") == "secret"
            and mount.get("mount_path", "").startswith(
                "/run/secrets/apexfabric/"
            )
        ]
        signatures.append(
            (
                application.get("name"),
                tuple(application.get("cameras", [])),
                tuple(
                    sorted(
                        (
                            mount.get("mount_path"),
                            mount.get("source", {}).get("name"),
                            mount.get("source", {}).get("key"),
                        )
                        for mount in camera_mounts
                    )
                ),
            )
        )
    return tuple(signatures)
