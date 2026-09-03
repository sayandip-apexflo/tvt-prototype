"""TVT-specific instantiation around the unchanged Solution Pack contract."""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apexfabric.solution_management.renderer import revision
from apexfabric.solution_management.validation import validate_bundle
from tvt_runtime.camera_secrets import TRAFFIC_APPS
from tvt_runtime.state import ensure_bundle_has_no_inline_secrets


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads(
    (ROOT / "solution-packs/schema/deployment-bundle.schema.json").read_text(
        encoding="utf-8"
    )
)
DNS_ID = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,61}[a-z0-9])?$")


@dataclass(frozen=True)
class BundleCamera:
    camera_key: str
    requested_fps: int
    apps: tuple[str, ...]


def catalog_traffic_bundle(
    catalog: dict[str, Any],
    deployment_id: str,
    edge_id: str,
    cameras: list[BundleCamera],
    *,
    inference_mode: str,
    cpu_request: str,
    cpu_limit: str,
    memory_request: str,
    memory_limit: str,
    state_size: str,
) -> dict[str, Any]:
    """Build the Traffic bundle only from a resolved catalog record."""

    if catalog.get("status") != "available":
        raise ValueError("only an available catalog entry can be deployed")
    image = catalog.get("image", {})
    digest = image.get("digest")
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("catalog image has no resolved immutable digest")
    if image.get("tag") == "latest":
        raise ValueError("mutable-only catalog images cannot be deployed")
    if not DNS_ID.fullmatch(deployment_id):
        raise ValueError("deployment_id must be a DNS-compatible identifier")
    if inference_mode not in {"cpu-compatible", "gpu-npu"}:
        raise ValueError("inference_mode must be cpu-compatible or gpu-npu")
    contract = catalog.get("contract", {})
    if contract.get("models", {}).get("delivery") != "baked-in":
        raise ValueError("Traffic catalog entry must deliver baked-in models")
    if contract.get("hardwareProfile") != "intel-285h":
        raise ValueError("Traffic catalog entry is not for intel-285h")
    repository = f"{image['registry'].rstrip('/')}/{image['repository']}"
    devices = (
        {"VEHICLE_DEVICE": "GPU", "PLATE_DEVICE": "NPU", "OCR_DEVICE": "MULTI:GPU,NPU"}
        if inference_mode == "gpu-npu"
        else {"VEHICLE_DEVICE": "CPU", "PLATE_DEVICE": "CPU", "OCR_DEVICE": "CPU"}
    )
    template = {
        "api_version": "apexfabric.com/v1alpha1",
        "kind": "DeploymentBundle",
        "deployment_id": deployment_id,
        "solution": {"solution_id": "traffic-edge", "version": catalog["version"]},
        "applications": [{
            "name": "runtime",
            "image": {
                "repository": repository,
                "tag": image["tag"],
                "digest": digest,
                "pull_policy": "IfNotPresent",
            },
            "replicas": 1,
            "lifecycle": {"desired_state": "Running", "termination_grace_period_seconds": 60},
            "resources": {
                "cpu": {"request": cpu_request, "limit": cpu_limit},
                "memory": {"request": memory_request, "limit": memory_limit},
                "accelerators": {"decoder": 0, "metis": 0},
                "camera_streams": len(cameras),
            },
            "cameras": [],
            "camera_contract": {
                "configuration_owner": "platform",
                "transport": "rtsp",
                "required_for_readiness": False,
                "scheduling_mode": "runtime-connectivity",
            },
            "configuration": {
                "desired_state_secret": f"{deployment_id}-desired-state",
                "desired_state_key": "desired_state.json",
                "models_delivery": "baked-in",
                "models_root": contract.get("models", {}).get("root", "/models/traffic/openvino"),
                "inference_mode": inference_mode,
            },
            "placement": {
                "runtime_profile": "intel-285h-gpu-npu",
                "node_class": "cv",
                "architecture": "amd64",
                "requires_qualified_node": True,
                "requires_camera_labels": False,
                "characteristics": {"hardware-profile": "intel-285h"},
            },
            "persistent_volumes": [{"name": "state", "mount_path": "/state", "size": state_size}],
            "external_mounts": [],
            "plan_compiler": {
                "type": "edge-agent-v1",
                "desired_state_secret": f"{deployment_id}-desired-state",
                "desired_state_key": "desired_state.json",
            },
            "health": {
                "startup": {"path": "/readyz", "port": "management", "period_seconds": 5, "timeout_seconds": 4, "failure_threshold": 60},
                "readiness": {"path": "/readyz", "port": "management", "period_seconds": 10, "timeout_seconds": 4, "failure_threshold": 3},
                "liveness": {"path": "/healthz", "port": "management", "initial_delay_seconds": 30, "period_seconds": 15, "timeout_seconds": 4, "failure_threshold": 4},
            },
            "ports": [{"name": "management", "container_port": 8080, "protocol": "TCP"}],
            "telemetry": {
                "metrics": {"path": "/metrics", "port": "management", "format": "json"},
                "events": {"path": "/events", "port": "management", "protocol": "sse"},
            },
            "environment": {"SOLUTION_PACK": "traffic", **devices},
        }],
        "configuration": {
            "edge_id": edge_id,
            "catalog_id": catalog["catalog_id"],
            "runtime_plan_is_compiled_by_init_container": True,
            "image_contract": "apexfabric-v1",
            "secret_input_contract": "traffic-runtime-v1",
            "models_delivery": "baked-in",
            "inference_mode": inference_mode,
        },
    }
    return instantiate_traffic_bundle(template, edge_id, cameras)


def validate_tvt_bundle(bundle: dict[str, Any]) -> None:
    ensure_bundle_has_no_inline_secrets(bundle)
    errors = validate_bundle(bundle, SCHEMA)
    if errors:
        raise ValueError("bundle validation failed: " + "; ".join(errors))


def apply_registry(bundle: dict[str, Any], registry: str) -> None:
    registry = registry.strip().rstrip("/")
    if not registry or "://" in registry or any(char.isspace() for char in registry):
        raise ValueError("registry must be host[:port] without a URL scheme")
    for application in bundle["applications"]:
        repository = application["image"]["repository"]
        if "__APEXFABRIC_REGISTRY__" in repository:
            application["image"]["repository"] = repository.replace(
                "__APEXFABRIC_REGISTRY__", registry
            )


def instantiate_traffic_bundle(
    template: dict[str, Any], edge_id: str, cameras: list[BundleCamera]
) -> dict[str, Any]:
    """Create a concrete non-secret bundle while preserving the V1 format."""

    result = copy.deepcopy(template)
    validate_tvt_bundle(result)
    if result.get("configuration", {}).get("secret_input_contract") != "traffic-runtime-v1":
        if cameras:
            raise ValueError("camera assignments require traffic-runtime-v1")
        return result
    if len(result["applications"]) != 1 or result["applications"][0]["name"] != "runtime":
        raise ValueError("traffic-runtime-v1 requires one runtime application")
    if not cameras:
        raise ValueError("at least one camera assignment is required")
    if len({item.camera_key for item in cameras}) != len(cameras):
        raise ValueError("camera assignments must be unique")
    deployment_id = result["deployment_id"]
    desired_secret = f"{deployment_id}-desired-state"
    camera_secret = f"{deployment_id}-camera-sources"
    app = result["applications"][0]
    camera_keys: list[str] = []
    mounts: list[dict[str, Any]] = []
    for camera in cameras:
        if not DNS_ID.fullmatch(camera.camera_key):
            raise ValueError(f"invalid camera key {camera.camera_key!r}")
        if not 1 <= camera.requested_fps <= 120:
            raise ValueError("requested FPS must be between 1 and 120")
        invalid_apps = sorted(set(camera.apps) - TRAFFIC_APPS)
        if not camera.apps or invalid_apps:
            raise ValueError(f"unsupported Traffic apps: {invalid_apps}")
        camera_keys.append(camera.camera_key)
        mounts.append(
            {
                "name": f"{camera.camera_key}-source",
                "mount_path": f"/run/secrets/apexfabric/{camera.camera_key}.rtsp",
                "read_only": True,
                "source": {
                    "type": "secret",
                    "name": camera_secret,
                    "key": f"{camera.camera_key}.rtsp",
                },
            }
        )
    result.setdefault("configuration", {})["edge_id"] = edge_id
    app["cameras"] = camera_keys
    app["external_mounts"] = mounts
    app["resources"]["camera_streams"] = len(camera_keys)
    app.setdefault("configuration", {})["desired_state_secret"] = desired_secret
    app["configuration"]["desired_state_key"] = "desired_state.json"
    app["plan_compiler"] = {
        "type": "edge-agent-v1",
        "desired_state_secret": desired_secret,
        "desired_state_key": "desired_state.json",
    }
    validate_tvt_bundle(result)
    return result


def canonical_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    """Round-trip through canonical JSON to reject non-JSON Python values."""

    return json.loads(json.dumps(bundle, sort_keys=True, separators=(",", ":")))


def bundle_sha256(bundle: dict[str, Any]) -> str:
    return revision(bundle)
