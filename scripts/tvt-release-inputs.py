#!/usr/bin/env python3
"""Create and verify the immutable external-input lock for a TVT release."""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import pathlib
import re
import shlex
import stat
import tempfile
from typing import Any


DIGEST = re.compile(r"[0-9a-f]{64}")
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?")
COMMIT = re.compile(r"[0-9a-f]{40}")
REQUIRED_FILES = {
    "images/registry.tar",
    "images/node-reporter.tar",
    "images/node-status-controller.tar",
    "images/traffic-edge-runtime-v4.tar",
    "k3s/install.sh",
    "k3s/k3s",
    "hardware/driver-recipe.json",
    "hardware/linux-npu-driver.tar.gz",
}
PIN_KEYS = (
    "K3S_VERSION",
    "NODE_MANAGEMENT_IMAGE_VERSION",
    "LOCAL_REGISTRY_IMAGE",
    "PIPELINE_REVISION",
    "PIPELINE_TRAFFIC_VERSION",
    "PIPELINE_TRAFFIC_ARCHIVE_SHA256",
    "PIPELINE_TRAFFIC_ARCHIVE_SIZE",
    "PIPELINE_TRAFFIC_ARCHIVE_IMAGE",
)


class InputError(ValueError):
    pass


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_env(path: pathlib.Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise InputError(f"invalid environment assignment at {path}:{number}")
        key, value = line.split("=", 1)
        try:
            fields = shlex.split(value, comments=False, posix=True)
        except ValueError as error:
            raise InputError(f"invalid environment value at {path}:{number}") from error
        if len(fields) != 1:
            raise InputError(f"environment value must resolve to one field at {path}:{number}")
        values[key.strip()] = fields[0]
    return values


def validate_identity(version: str, source_commit: str) -> None:
    if not VERSION.fullmatch(version):
        raise InputError("release version is not valid semantic version text")
    if not COMMIT.fullmatch(source_commit):
        raise InputError("source commit must be a full lowercase 40-character Git SHA")


def input_files(root: pathlib.Path, lock_path: pathlib.Path | None = None) -> dict[str, pathlib.Path]:
    if not root.is_dir() or root.is_symlink():
        raise InputError("input directory is missing or symlinked")
    result: dict[str, pathlib.Path] = {}
    resolved_lock = lock_path.resolve() if lock_path and lock_path.exists() else None
    for path in root.rglob("*"):
        if path.is_symlink():
            raise InputError(f"input directory contains a symlink: {path.relative_to(root)}")
        if not path.is_file():
            continue
        if resolved_lock is not None and path.resolve() == resolved_lock:
            continue
        relative = path.relative_to(root).as_posix()
        result[relative] = path
    missing = sorted(REQUIRED_FILES - result.keys())
    if missing:
        raise InputError("required release inputs are missing: " + ", ".join(missing))
    if not any(name.startswith("hardware/wheels/") and name.endswith(".whl") for name in result):
        raise InputError("hardware/wheels contains no wheel files")
    if not any(name.startswith("apt/") and name.endswith(".deb") for name in result):
        raise InputError("apt contains no Debian packages")
    unsupported = sorted(
        name
        for name in result
        if name not in REQUIRED_FILES
        and not (name.startswith("hardware/wheels/") and name.endswith(".whl"))
        and not (name.startswith("apt/") and name.endswith(".deb"))
    )
    if unsupported:
        raise InputError("unsupported files are present in the input directory: " + ", ".join(unsupported))
    for executable in (root / "k3s/install.sh", root / "k3s/k3s"):
        if not executable.stat().st_mode & stat.S_IXUSR:
            raise InputError(f"release executable is not owner-executable: {executable.relative_to(root)}")
    return result


def validate_hardware(root: pathlib.Path, files: dict[str, pathlib.Path]) -> dict[str, Any]:
    try:
        recipe = json.loads((root / "hardware/driver-recipe.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise InputError(f"invalid hardware driver recipe: {error}") from error
    expected = {
        "schema_version": 1,
        "hardware_profile": "intel-285h",
        "os_id": "ubuntu",
        "os_version_id": "24.04",
        "architecture": "amd64",
    }
    for key, value in expected.items():
        if recipe.get(key) != value:
            raise InputError(f"hardware recipe {key} does not equal {value!r}")
    kernel = recipe.get("kernel_version")
    if not isinstance(kernel, str) or not kernel:
        raise InputError("hardware recipe has no kernel version")
    npu_digest = recipe.get("npu", {}).get("sha256")
    if not isinstance(npu_digest, str) or not DIGEST.fullmatch(npu_digest):
        raise InputError("hardware recipe has no valid NPU digest")
    if sha256(root / "hardware/linux-npu-driver.tar.gz") != npu_digest:
        raise InputError("hardware NPU archive does not match the driver recipe")
    wheel_pins = recipe.get("wheels")
    if not isinstance(wheel_pins, dict) or not wheel_pins:
        raise InputError("hardware recipe has no wheel closure")
    actual_wheels = {
        name.removeprefix("hardware/wheels/")
        for name in files
        if name.startswith("hardware/wheels/") and name.endswith(".whl")
    }
    if set(wheel_pins) != actual_wheels:
        raise InputError("hardware wheel files do not exactly match the driver recipe")
    for filename, expected_digest in wheel_pins.items():
        if not isinstance(expected_digest, str) or not DIGEST.fullmatch(expected_digest):
            raise InputError(f"hardware recipe has an invalid wheel digest: {filename}")
        if sha256(root / "hardware/wheels" / filename) != expected_digest:
            raise InputError(f"hardware wheel does not match the recipe: {filename}")
    return {"kernel_version": kernel, "recipe_sha256": sha256(root / "hardware/driver-recipe.json")}


def create_lock(args: argparse.Namespace) -> dict[str, Any]:
    root = args.input_directory.resolve()
    output = args.output.resolve()
    validate_identity(args.release_version, args.source_commit)
    files = input_files(root, output)
    platform = load_env(args.platform_config)
    pipeline = load_env(args.pipeline_config)
    pins = {key: (platform | pipeline).get(key) for key in PIN_KEYS}
    missing_pins = [key for key, value in pins.items() if not value]
    if missing_pins:
        raise InputError("release configuration pins are missing: " + ", ".join(missing_pins))
    traffic = root / "images/traffic-edge-runtime-v4.tar"
    if sha256(traffic) != pins["PIPELINE_TRAFFIC_ARCHIVE_SHA256"]:
        raise InputError("Traffic archive checksum does not match config/pipeline.env")
    if traffic.stat().st_size != int(pins["PIPELINE_TRAFFIC_ARCHIVE_SIZE"]):
        raise InputError("Traffic archive size does not match config/pipeline.env")
    hardware = validate_hardware(root, files)
    return {
        "schema_version": 1,
        "release_version": args.release_version,
        "source_commit": args.source_commit,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "configuration": {
            "platform_sha256": sha256(args.platform_config),
            "pipeline_sha256": sha256(args.pipeline_config),
            "pins": pins,
        },
        "hardware": hardware,
        "files": {
            relative: {"sha256": sha256(path), "size": path.stat().st_size}
            for relative, path in sorted(files.items())
        },
    }


def write_private(path: pathlib.Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(document, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
    except Exception:
        pathlib.Path(temporary_name).unlink(missing_ok=True)
        raise


def verify_lock(args: argparse.Namespace) -> None:
    root = args.input_directory.resolve()
    try:
        lock = json.loads(args.lock.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise InputError(f"invalid release input lock: {error}") from error
    if lock.get("schema_version") != 1:
        raise InputError("unsupported release input lock schema")
    validate_identity(str(lock.get("release_version", "")), str(lock.get("source_commit", "")))
    if args.release_version and lock["release_version"] != args.release_version:
        raise InputError("input lock release version does not match the requested release")
    if args.source_commit and lock["source_commit"] != args.source_commit:
        raise InputError("input lock source commit does not match the requested release")
    files = input_files(root, args.lock.resolve())
    locked_files = lock.get("files")
    if not isinstance(locked_files, dict) or set(locked_files) != set(files):
        raise InputError("input lock file coverage does not match the input directory")
    for relative, path in files.items():
        record = locked_files.get(relative, {})
        if not isinstance(record, dict):
            raise InputError(f"input lock has an invalid file record: {relative}")
        if record.get("size") != path.stat().st_size or record.get("sha256") != sha256(path):
            raise InputError(f"release input does not match its lock: {relative}")
    configuration = lock.get("configuration", {})
    if not isinstance(configuration, dict):
        raise InputError("input lock has an invalid configuration record")
    if args.platform_config and configuration.get("platform_sha256") != sha256(args.platform_config):
        raise InputError("platform configuration changed after the input lock was created")
    if args.pipeline_config and configuration.get("pipeline_sha256") != sha256(args.pipeline_config):
        raise InputError("pipeline configuration changed after the input lock was created")
    validate_hardware(root, files)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="hash and lock reviewed input artifacts")
    create.add_argument("--input-directory", required=True, type=pathlib.Path)
    create.add_argument("--output", required=True, type=pathlib.Path)
    create.add_argument("--release-version", required=True)
    create.add_argument("--source-commit", required=True)
    create.add_argument("--platform-config", required=True, type=pathlib.Path)
    create.add_argument("--pipeline-config", required=True, type=pathlib.Path)
    verify = commands.add_parser("verify", help="verify artifacts against an existing lock")
    verify.add_argument("--input-directory", required=True, type=pathlib.Path)
    verify.add_argument("--lock", required=True, type=pathlib.Path)
    verify.add_argument("--release-version")
    verify.add_argument("--source-commit")
    verify.add_argument("--platform-config", type=pathlib.Path)
    verify.add_argument("--pipeline-config", type=pathlib.Path)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "create":
            write_private(args.output, create_lock(args))
            print(f"Wrote release input lock: {args.output}")
        else:
            verify_lock(args)
            print(f"Verified release inputs: {args.input_directory}")
        return 0
    except (InputError, OSError, ValueError) as error:
        print(f"release-inputs: ERROR: {error}", file=os.sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
