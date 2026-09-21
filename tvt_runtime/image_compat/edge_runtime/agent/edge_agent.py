"""Validate TVT Mills desired state for the ApexFabric V1 init contract.

The vendor TVT Mills runtime consumes ``/configs/desired_state.json`` directly
and watches that file for later revisions.  The frozen ApexFabric renderer,
however, gates every V1 workload with ``python -m
edge_runtime.agent.edge_agent``.  This compatibility entry point preserves
that gate without copying camera URLs or other secret material into ``/plans``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any


COMPATIBILITY_ID = "tvt-direct-desired-state-v1"
CAMERA_ID = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
ALLOWED_APPS = {"face_recognition", "face_enrollment", "anpr"}
MAX_DESIRED_STATE_BYTES = 1024 * 1024


class PlanCompilerError(ValueError):
    """Raised when desired state cannot safely start the direct runtime."""


def _load_desired_state(path: Path) -> tuple[dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise PlanCompilerError("desired state is unavailable") from error
    if not raw or len(raw) > MAX_DESIRED_STATE_BYTES:
        raise PlanCompilerError("desired state has an invalid size")
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PlanCompilerError("desired state is not valid JSON") from error
    if not isinstance(document, dict):
        raise PlanCompilerError("desired state must be an object")
    return document, raw


def _validate(document: dict[str, Any], models_root: Path) -> tuple[int, list[str]]:
    revision = document.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise PlanCompilerError("desired-state revision must be a positive integer")
    if not models_root.is_dir():
        raise PlanCompilerError("baked model root is unavailable")

    cameras = document.get("cameras")
    if not isinstance(cameras, list) or not cameras:
        raise PlanCompilerError("desired state must contain at least one camera")
    camera_ids: list[str] = []
    for camera in cameras:
        if not isinstance(camera, dict):
            raise PlanCompilerError("camera entry must be an object")
        camera_id = camera.get("camera_id")
        if not isinstance(camera_id, str) or CAMERA_ID.fullmatch(camera_id) is None:
            raise PlanCompilerError("camera entry has an invalid camera_id")
        if camera_id in camera_ids:
            raise PlanCompilerError("desired state contains a duplicate camera_id")
        expected_source = f"file:/run/secrets/apexfabric/{camera_id}.rtsp"
        if camera.get("source") != expected_source:
            raise PlanCompilerError("camera source does not use its protected file mount")
        if camera.get("solution_pack") != "tvt-mills-pilot":
            raise PlanCompilerError("camera has the wrong solution_pack")
        apps = camera.get("apps")
        if (
            not isinstance(apps, list)
            or not apps
            or any(not isinstance(app, str) or app not in ALLOWED_APPS for app in apps)
        ):
            raise PlanCompilerError("camera has an invalid application selection")
        if not isinstance(camera.get("config", {}), dict):
            raise PlanCompilerError("camera config must be an object")
        camera_ids.append(camera_id)
    return revision, sorted(camera_ids)


def compile_plan(desired_state: Path, output_dir: Path, models_root: Path) -> Path:
    """Validate direct-runtime inputs and atomically emit a non-secret receipt."""

    document, raw = _load_desired_state(desired_state)
    revision, camera_ids = _validate(document, models_root)
    receipt = {
        "camera_ids": camera_ids,
        "compiler": COMPATIBILITY_ID,
        "desired_state_sha256": hashlib.sha256(raw).hexdigest(),
        "format_version": 1,
        "revision": revision,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=output_dir, prefix=".runtime-plan.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    target = output_dir / "runtime-plan.json"
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(receipt, stream, separators=(",", ":"), sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--desired-state", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--models-root", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        receipt = compile_plan(args.desired_state, args.output_dir, args.models_root)
    except PlanCompilerError as error:
        parser.exit(1, f"plan-compiler: ERROR: {error}\n")
    document = json.loads(receipt.read_text(encoding="utf-8"))
    print(
        "Validated desired-state revision "
        f"{document['revision']} for {len(document['camera_ids'])} camera(s)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
