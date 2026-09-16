"""Validate and materialize installation-owned direct-camera inputs.

Mirrors ``apexfabric/control_plane/server.py::_apply_bundle_inputs`` at
``k3s-prototype@5ada504`` (ConfigMap ``<deployment>-desired-state`` +
Secret ``<deployment>-camera-sources``) but returns the Kubernetes List
instead of applying it. Callers must send it directly to the API and must
never log or persist Secret values.

``apexfabric/`` is an exact copy and must not be edited; this adapter stays
in ``tvt_runtime/``.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse


TRAFFIC_APPS = {
    "anpr",
    "illegal_parking",
    "pedestrian_counting",
    "vehicle_counting",
    "wrong_way",
}

SURVEILLANCE_APPS = {
    "reid",
    "face_recognition",
    "intrusion",
    "people_counting",
}

_CONTRACTS = {
    "traffic-runtime-v1": ("traffic", TRAFFIC_APPS),
    "surveillance-runtime-v1": ("surveillance", SURVEILLANCE_APPS),
}


def build_camera_secret_list(
    bundle: dict[str, Any],
    secret_inputs: Any,
    namespace: str = "apexfabric",
) -> dict[str, Any] | None:
    """Return the reference runtime input List (ConfigMap + Secret).

    ``None`` means the bundle has no ephemeral camera-secret contract. Passing
    inputs to such a bundle is rejected rather than silently ignored.
    """

    configuration = bundle.get("configuration", {})
    input_contract = configuration.get("secret_input_contract")
    if input_contract not in _CONTRACTS:
        if secret_inputs is not None:
            raise ValueError("secret_inputs are unsupported by this bundle")
        return None
    solution_pack, allowed_apps = _CONTRACTS[input_contract]
    if not isinstance(secret_inputs, dict):
        raise ValueError(
            f"camera Secret values are required for {solution_pack.title()} Edge Runtime"
        )

    desired_state = secret_inputs.get("desired_state")
    camera_sources = secret_inputs.get("camera_sources")
    if not isinstance(desired_state, dict):
        raise ValueError("desired_state is required")
    if not isinstance(camera_sources, dict):
        raise ValueError("camera_sources or camera_ids are required")

    applications = bundle.get("applications", [])
    if len(applications) != 1 or applications[0].get("name") != "runtime":
        raise ValueError(f"{input_contract} requires exactly one runtime application")
    app = applications[0]
    deployment_id = bundle["deployment_id"]
    expected_desired_config_map = f"{deployment_id}-desired-state"
    expected_camera_secret = f"{deployment_id}-camera-sources"

    compiler = app.get("plan_compiler", {})
    if (
        compiler.get("desired_state_config_map") != expected_desired_config_map
        or compiler.get("desired_state_key") != "desired_state.json"
    ):
        raise ValueError("bundle desired-state ConfigMap contract is invalid")

    expected_cameras = app.get("cameras", [])
    desired_cameras = desired_state.get("cameras")
    if desired_state.get("edge_id") != configuration.get("edge_id"):
        raise ValueError("desired_state edge_id does not match the bundle")
    desired_revision = desired_state.get("revision")
    if (
        isinstance(desired_revision, bool)
        or not isinstance(desired_revision, int)
        or desired_revision < 1
    ):
        raise ValueError("desired_state revision must be a positive integer")
    if not isinstance(desired_cameras, list) or [
        item.get("camera_id") for item in desired_cameras if isinstance(item, dict)
    ] != expected_cameras:
        raise ValueError("desired_state cameras do not match the bundle")
    if set(camera_sources) != set(expected_cameras):
        raise ValueError("camera Secret values must exactly match the configured camera IDs")

    mounts = {
        mount.get("mount_path"): mount for mount in app.get("external_mounts", [])
    }
    for camera in desired_cameras:
        camera_id = camera["camera_id"]
        expected_path = f"/run/secrets/apexfabric/{camera_id}.rtsp"
        if (
            camera.get("source") != f"file:{expected_path}"
            or camera.get("solution_pack") != solution_pack
            or not isinstance(camera.get("apps"), list)
            or not camera["apps"]
            or any(value not in allowed_apps for value in camera["apps"])
        ):
            raise ValueError(
                f"desired_state camera {camera_id} violates the {solution_pack.title()} contract"
            )
        mount_source = mounts.get(expected_path, {}).get("source", {})
        if mount_source != {
            "type": "secret",
            "name": expected_camera_secret,
            "key": f"{camera_id}.rtsp",
        }:
            raise ValueError(f"bundle camera Secret mount for {camera_id} is invalid")

        source = camera_sources[camera_id]
        parsed_source = urlparse(source) if isinstance(source, str) else None
        if (
            not isinstance(source, str)
            or len(source) > 4096
            or any(ord(character) < 32 for character in source)
            or parsed_source is None
            or parsed_source.scheme not in {"rtsp", "rtsps"}
            or not parsed_source.hostname
        ):
            raise ValueError(
                f"camera Secret value for {camera_id} must be a valid RTSP URL"
            )

    labels = {
        "app.kubernetes.io/managed-by": "apexfabric-control-plane",
        "apexfabric.com/deployment-id": deployment_id,
    }
    revision = desired_state.get("revision")
    return {
        "apiVersion": "v1",
        "kind": "List",
        "items": [
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {
                    "name": expected_desired_config_map,
                    "namespace": namespace,
                    "labels": labels,
                    "annotations": {
                        "apexfabric.com/desired-revision": str(revision),
                    },
                },
                "data": {
                    "desired_state.json": json.dumps(
                        desired_state, separators=(",", ":")
                    )
                },
            },
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {
                    "name": expected_camera_secret,
                    "namespace": namespace,
                    "labels": labels,
                },
                "type": "Opaque",
                "stringData": {
                    f"{camera_id}.rtsp": camera_sources[camera_id]
                    for camera_id in expected_cameras
                },
            },
        ],
    }


def secret_names(secret_list: dict[str, Any] | None) -> list[str]:
    """Return safe metadata for status and audit output."""

    if secret_list is None:
        return []
    return [item["metadata"]["name"] for item in secret_list["items"]]
