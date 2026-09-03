#!/usr/bin/env python3
"""Verify a completed qualification report without exposing its contents."""

from __future__ import annotations

import argparse
import json
import re
import stat
from pathlib import Path
from typing import Any


DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
HEX_DIGEST = re.compile(r"[0-9a-f]{64}")
RTSP = re.compile(r"rtsps?://", re.IGNORECASE)
SENSITIVE_KEYS = {
    "authorization",
    "camera_sources",
    "ciphertext",
    "credential",
    "credentials",
    "password",
    "secret",
    "stringdata",
    "token",
    "username",
}
REQUIRED_CHECK_IDS = {
    "contracts.provenance",
    "host.platform",
    "host.devices",
    "host.accelerators",
    "service.postgresql",
    "service.docker",
    "service.tvt-local-registry",
    "service.k3s",
    "service.tvt-edge",
    "service.tvt-camera-sync",
    "service.tvt-pipeline-image-sync",
    "registry.api",
    "deployment.applied",
    "catalog.available",
    "image_lock.immutable",
    "containerd.image",
    "kubernetes.node",
    "kubernetes.workload_contract",
    "kubernetes.secrets",
    "kubernetes.pod",
    "kubernetes.persistent_state",
    "runtime.health",
    "runtime.metrics_schema",
    "runtime.events_sse",
    "runtime.analytics_events",
}


def contains_sensitive_key(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            key.lower() in SENSITIVE_KEYS or contains_sensitive_key(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(contains_sensitive_key(item) for item in value)
    return False


def main(argv: list[str] | None = None) -> int:
    arguments = argparse.ArgumentParser(
        description="Verify a redacted Traffic qualification evidence report"
    )
    arguments.add_argument("report", type=Path)
    args = arguments.parse_args(argv)
    path = args.report
    if path.is_symlink() or not path.is_file():
        raise SystemExit("qualification report must be a regular non-symlink file")
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise SystemExit("qualification report must have mode 0600")
    body = path.read_text(encoding="utf-8")
    if RTSP.search(body):
        raise SystemExit("qualification report contains an RTSP URL")
    try:
        report = json.loads(body)
    except json.JSONDecodeError as error:
        raise SystemExit("qualification report is not valid JSON") from error
    if not isinstance(report, dict) or report.get("format_version") != 1:
        raise SystemExit("qualification report format is unsupported")
    if report.get("qualification") != "traffic-edge-runtime-v4":
        raise SystemExit("qualification report has the wrong qualification ID")
    if contains_sensitive_key(report):
        raise SystemExit("qualification report contains a sensitive field")
    checks = report.get("checks")
    if not isinstance(checks, list) or not checks:
        raise SystemExit("qualification report has no checks")
    identifiers = [item.get("id") for item in checks if isinstance(item, dict)]
    if len(identifiers) != len(checks) or len(set(identifiers)) != len(identifiers):
        raise SystemExit("qualification check identifiers must be present and unique")
    identifier_set = set(identifiers)
    missing = REQUIRED_CHECK_IDS - identifier_set
    if missing:
        raise SystemExit("qualification report is missing required checks")
    if any(item.get("status") not in {"passed", "skipped"} for item in checks):
        raise SystemExit("qualification contains an invalid or unsuccessful check status")
    failed = [item for item in checks if item.get("status") == "failed"]
    if report.get("outcome") != "passed" or failed:
        raise SystemExit("qualification did not pass")
    expected_summary = {
        "passed": sum(item.get("status") == "passed" for item in checks),
        "failed": 0,
        "skipped": sum(item.get("status") == "skipped" for item in checks),
    }
    if report.get("summary") != expected_summary:
        raise SystemExit("qualification summary does not match its checks")
    checkpoint = report.get("checkpoint")
    if checkpoint not in {"steady", "pre-reboot", "post-reboot", "post-rollback"}:
        raise SystemExit("qualification checkpoint is invalid")
    if checkpoint != "steady" and f"checkpoint.{checkpoint}" not in identifier_set:
        raise SystemExit("qualification report is missing its checkpoint comparison")
    actions = report.get("actions")
    if (
        not isinstance(actions, dict)
        or set(actions)
        != {"preview_requested", "commit_requested", "rollback_requested"}
        or any(not isinstance(value, bool) for value in actions.values())
        or (actions["commit_requested"] and not actions["preview_requested"])
    ):
        raise SystemExit("qualification action record is invalid")
    if actions.get("preview_requested") and "deployment.preview" not in identifier_set:
        raise SystemExit("qualification report is missing its deployment preview")
    if actions.get("commit_requested") and "deployment.commit" not in identifier_set:
        raise SystemExit("qualification report is missing its deployment commit")
    if actions.get("rollback_requested") and "rollback.request" not in identifier_set:
        raise SystemExit("qualification report is missing its rollback request")
    invariants = report.get("invariants", {})
    if (
        invariants.get("catalog_id") != "traffic-edge-runtime:2026.08.21-v4"
        or not invariants.get("deployment_id")
        or invariants.get("namespace") != "apexfabric"
        or not isinstance(invariants.get("applied_revision"), int)
    ):
        raise SystemExit("qualification deployment invariants are invalid")
    if not HEX_DIGEST.fullmatch(str(invariants.get("bundle_sha256", ""))):
        raise SystemExit("qualification bundle digest is invalid")
    if not DIGEST.fullmatch(str(invariants.get("image_digest", ""))):
        raise SystemExit("qualification image digest is invalid")
    if not invariants.get("pvc_uid"):
        raise SystemExit("qualification report has no persistent-state PVC identity")
    print(
        json.dumps(
            {
                "outcome": "verified",
                "report": str(path),
                "checks": len(checks),
                "checkpoint": report.get("checkpoint"),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
