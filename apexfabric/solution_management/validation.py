#!/usr/bin/env python3
"""Validate an ApexFabric Deployment Bundle before reconciliation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import jsonschema
import yaml


def path_text(path: Any) -> str:
    result = "$"
    for item in path:
        result += f"[{item}]" if isinstance(item, int) else f".{item}"
    return result


def semantic_errors(bundle: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    reserved_environment = {
        "POD_NAME", "POD_NAMESPACE", "NODE_NAME", "APPLICATION_VERSION",
        "APEXFABRIC_DEPLOYMENT_REVISION", "APEXFABRIC_DEPLOYMENT_ID",
        "APEXFABRIC_SOLUTION_ID", "APEXFABRIC_APPLICATION",
    }
    applications = bundle.get("applications", [])
    if not isinstance(applications, list):
        return errors
    names = [app.get("name") for app in applications if isinstance(app, dict)]
    duplicates = sorted({name for name in names if name and names.count(name) > 1})
    for name in duplicates:
        errors.append(f"$.applications: duplicate application name {name!r}")
    for index, app in enumerate(applications):
        if not isinstance(app, dict):
            continue
        deployment_id = bundle.get("deployment_id", "")
        app_name = app.get("name", "")
        if isinstance(deployment_id, str) and isinstance(app_name, str) and len(f"{deployment_id}-{app_name}") > 63:
            errors.append(f"$.applications[{index}].name: combined deployment/application resource name exceeds 63 characters")
        ports = app.get("ports", [])
        if isinstance(ports, list):
            port_names = [port.get("name") for port in ports if isinstance(port, dict)]
            port_numbers = [port.get("container_port") for port in ports if isinstance(port, dict)]
            for value in sorted({x for x in port_names if x and port_names.count(x) > 1}):
                errors.append(f"$.applications[{index}].ports: duplicate port name {value!r}")
            for value in sorted({x for x in port_numbers if x and port_numbers.count(x) > 1}):
                errors.append(f"$.applications[{index}].ports: duplicate container_port {value}")
            available = set(port_names)
            health = app.get("health", {})
            if isinstance(health, dict):
                for probe_name in ("readiness", "liveness", "startup"):
                    probe = health.get(probe_name, {})
                    if isinstance(probe, dict) and probe.get("port") not in available:
                        errors.append(f"$.applications[{index}].health.{probe_name}.port: references undefined port {probe.get('port')!r}")
            telemetry = app.get("telemetry", {})
            if isinstance(telemetry, dict):
                for endpoint_name in ("metrics", "events"):
                    endpoint = telemetry.get(endpoint_name)
                    if isinstance(endpoint, dict) and endpoint.get("port") not in available:
                        errors.append(f"$.applications[{index}].telemetry.{endpoint_name}.port: references undefined port {endpoint.get('port')!r}")
        volumes = app.get("persistent_volumes", [])
        mount_paths: list[str] = []
        volume_names: list[str] = []
        if isinstance(volumes, list):
            volume_names = [volume.get("name") for volume in volumes if isinstance(volume, dict)]
            mount_paths = [volume.get("mount_path") for volume in volumes if isinstance(volume, dict)]
            for value in sorted({x for x in volume_names if x and volume_names.count(x) > 1}):
                errors.append(f"$.applications[{index}].persistent_volumes: duplicate volume name {value!r}")
            for value in sorted({x for x in mount_paths if x and mount_paths.count(x) > 1}):
                errors.append(f"$.applications[{index}].persistent_volumes: duplicate mount path {value!r}")
            for volume_index, volume in enumerate(volumes):
                if not isinstance(volume, dict):
                    continue
                if len(f"{deployment_id}-{app_name}-{volume.get('name', '')}") > 63:
                    errors.append(f"$.applications[{index}].persistent_volumes[{volume_index}].name: generated claim name exceeds 63 characters")
                if volume.get("mount_path") in {"/dev", "/etc/apexfabric/config", "/etc/apexfabric/secrets"}:
                    errors.append(f"$.applications[{index}].persistent_volumes[{volume_index}].mount_path: conflicts with a platform-managed mount")
        external_mounts = app.get("external_mounts", [])
        if isinstance(external_mounts, list):
            external_names = [mount.get("name") for mount in external_mounts if isinstance(mount, dict)]
            external_paths = [mount.get("mount_path") for mount in external_mounts if isinstance(mount, dict)]
            for value in sorted({x for x in external_names if x and external_names.count(x) > 1}):
                errors.append(f"$.applications[{index}].external_mounts: duplicate mount name {value!r}")
            for value in sorted({x for x in external_paths if x and external_paths.count(x) > 1}):
                errors.append(f"$.applications[{index}].external_mounts: duplicate mount path {value!r}")
            for mount_index, mount in enumerate(external_mounts):
                if not isinstance(mount, dict):
                    continue
                path = mount.get("mount_path")
                if path in mount_paths or mount.get("name") in volume_names:
                    errors.append(f"$.applications[{index}].external_mounts[{mount_index}]: conflicts with a managed persistent volume")
                if path in {"/dev", "/etc/apexfabric/config", "/etc/apexfabric/secrets"}:
                    errors.append(f"$.applications[{index}].external_mounts[{mount_index}].mount_path: conflicts with a platform-managed mount")
                source = mount.get("source", {})
                if isinstance(source, dict) and source.get("type") == "secret" and not mount.get("read_only"):
                    errors.append(f"$.applications[{index}].external_mounts[{mount_index}].read_only: Secret mounts must be read-only")
            mount_paths.extend(path for path in external_paths if isinstance(path, str))
        placement = app.get("placement", {})
        camera_contract = app.get("camera_contract", {})
        if isinstance(placement, dict) and isinstance(camera_contract, dict) and camera_contract:
            scheduling_mode = camera_contract.get("scheduling_mode", "runtime-connectivity")
            requires_labels = placement.get("requires_camera_labels", True)
            if scheduling_mode == "node-locality" and not requires_labels:
                errors.append(f"$.applications[{index}].placement.requires_camera_labels: node-locality camera scheduling requires camera labels")
            if scheduling_mode == "runtime-connectivity" and requires_labels and app.get("cameras"):
                errors.append(f"$.applications[{index}].placement.requires_camera_labels: runtime-connectivity cameras must not be used as node labels")
        if isinstance(placement, dict) and placement.get("runtime_profile") == "intel-285h-metis":
            if placement.get("architecture") != "amd64":
                errors.append(f"$.applications[{index}].placement.architecture: intel-285h-metis requires 'amd64'")
            metis = app.get("resources", {}).get("accelerators", {}).get("metis", 0)
            if not isinstance(metis, int) or metis < 1:
                errors.append(f"$.applications[{index}].resources.accelerators.metis: intel-285h-metis requires at least one Metis device")
            if "/data" not in mount_paths:
                errors.append(f"$.applications[{index}].persistent_volumes: intel-285h-metis requires a volume mounted at '/data'")
        if isinstance(placement, dict) and placement.get("runtime_profile") == "intel-285h-gpu-npu":
            if placement.get("architecture") != "amd64":
                errors.append(f"$.applications[{index}].placement.architecture: intel-285h-gpu-npu requires 'amd64'")
            camera_streams = app.get("resources", {}).get("camera_streams", 0)
            if not isinstance(camera_streams, int) or camera_streams < 1:
                errors.append(f"$.applications[{index}].resources.camera_streams: intel-285h-gpu-npu requires at least one camera stream")
        if app.get("plan_compiler") and any(path == "/plans" or path.startswith("/plans/") for path in mount_paths):
            errors.append(f"$.applications[{index}].external_mounts: plan compiler owns the /plans mount")
        secrets = app.get("secrets", {})
        secret_environment = app.get("secret_environment", {})
        if isinstance(secrets, dict) and isinstance(secret_environment, dict):
            for env_name, secret_key in secret_environment.items():
                if secret_key not in secrets:
                    errors.append(f"$.applications[{index}].secret_environment.{env_name}: references undefined secret key {secret_key!r}")
        configured_environment = set(app.get("environment", {})) | set(app.get("secret_environment", {}))
        for name in sorted(configured_environment & reserved_environment):
            errors.append(f"$.applications[{index}]: environment name {name!r} is reserved for workload identity")
    return errors


def validate_bundle(bundle: Any, schema: dict[str, Any]) -> list[str]:
    validator = jsonschema.Draft202012Validator(schema)
    errors = [
        f"{path_text(error.absolute_path)}: {error.message}"
        for error in sorted(validator.iter_errors(bundle), key=lambda error: (list(error.absolute_path), error.message))
    ]
    if isinstance(bundle, dict):
        errors.extend(semantic_errors(bundle))
    return errors


def load_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise ValueError(f"invalid YAML: {error}") from error


def main() -> int:
    from tvt_edge.paths import RESOURCE_ROOT
    root = RESOURCE_ROOT
    parser = argparse.ArgumentParser(description="Validate an ApexFabric Deployment Bundle")
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--schema", type=Path, default=root / "solution-packs/schema/deployment-bundle.schema.json")
    args = parser.parse_args()
    try:
        schema = json.loads(args.schema.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
        bundle = load_yaml(args.bundle)
    except (OSError, ValueError, json.JSONDecodeError, jsonschema.SchemaError) as error:
        print(f"INVALID: {args.bundle}", file=sys.stderr)
        print(f"  $: {error}", file=sys.stderr)
        return 2
    errors = validate_bundle(bundle, schema)
    if errors:
        print(f"INVALID: {args.bundle} ({len(errors)} error(s))", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print(f"VALID: {args.bundle}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
