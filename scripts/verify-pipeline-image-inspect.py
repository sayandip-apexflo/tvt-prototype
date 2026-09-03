#!/usr/bin/env python3
"""Validate Docker image-inspect JSON against the pinned Traffic v4 contract."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def verify(document: object, expected: argparse.Namespace) -> list[str]:
    if not isinstance(document, list) or len(document) != 1:
        return ["Docker returned invalid image inspection data"]
    image = document[0]
    if not isinstance(image, dict):
        return ["Docker returned invalid image inspection data"]
    config = image.get("Config") or {}
    labels = config.get("Labels") or {}
    expected_labels = {
        "org.opencontainers.image.source": expected.source,
        "org.opencontainers.image.title": expected.title,
        "org.opencontainers.image.version": expected.version,
        "io.apexfabric.contract.version": expected.contract_version,
        "io.apexfabric.hardware.profile": expected.hardware_profile,
        "io.apexfabric.models.delivery": expected.models_delivery,
    }
    errors = []
    if image.get("Architecture") != "amd64":
        errors.append(f"architecture {image.get('Architecture')!r}, expected 'amd64'")
    for name, wanted in expected_labels.items():
        if labels.get(name) != wanted:
            errors.append(f"label {name} is {labels.get(name)!r}, expected {wanted!r}")
    if config.get("User") != expected.user:
        errors.append(f"user {config.get('User')!r}, expected {expected.user!r}")
    if expected.port not in (config.get("ExposedPorts") or {}):
        errors.append(f"port {expected.port!r} is not exposed")
    command = " ".join((config.get("Entrypoint") or []) + (config.get("Cmd") or []))
    if command != expected.command:
        errors.append(f"container command is {command!r}, expected {expected.command!r}")
    return errors


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser()
    value.add_argument("inspection", type=Path)
    value.add_argument("--source", required=True)
    value.add_argument("--title", required=True)
    value.add_argument("--version", required=True)
    value.add_argument("--contract-version", required=True)
    value.add_argument("--hardware-profile", required=True)
    value.add_argument("--models-delivery", required=True)
    value.add_argument("--user", required=True)
    value.add_argument("--port", required=True)
    value.add_argument("--command", required=True)
    return value


def main() -> int:
    args = parser().parse_args()
    try:
        document = json.loads(args.inspection.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"cannot read Docker image inspection: {error}") from error
    errors = verify(document, args)
    if errors:
        raise SystemExit("Traffic image contract verification failed: " + "; ".join(errors))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
