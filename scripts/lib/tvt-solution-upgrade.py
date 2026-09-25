#!/usr/bin/env python3
"""Resumable host-side orchestration for a pinned TVT solution image upgrade."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any


API_ROOT = "http://127.0.0.1:8089/api/v1"
DEFAULT_STATE_ROOT = Path(
    os.environ.get("TVT_SOLUTION_UPGRADE_STATE_DIR", "/var/lib/tvt/pipeline/upgrades")
)
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
HEX = re.compile(r"^[0-9a-f]{64}$")
CATALOG_FILES = (
    "image-contract.yaml",
    "desired-state.schema.json",
    "desired-state.example.json",
    "metrics.schema.json",
    "analytics-event.schema.json",
    "analytics-event.example.json",
    "provenance.json",
)
ACTIVE_FILES = (
    Path("/opt/tvt/scripts/tvt-edge-operations.sh"),
    Path("/opt/tvt/config/platform.env"),
    Path("/opt/tvt/config/pipeline.env"),
    Path("/etc/tvt/pipeline-bundle.env"),
    Path("/var/lib/tvt/pipeline/traffic-image.lock.json"),
    Path("/etc/systemd/system/tvt-pipeline-image-sync.service"),
    Path("/etc/systemd/system/tvt-pipeline-image-sync.timer"),
)


class UpgradeError(RuntimeError):
    pass


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def require_root() -> None:
    if os.geteuid() != 0:
        raise UpgradeError("run this command as root (for example, with sudo)")


def checked_id(value: str, label: str) -> str:
    if not SAFE_ID.fullmatch(value):
        raise UpgradeError(f"{label} contains unsupported characters")
    return value


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise UpgradeError(f"invalid assignment at {path}:{number}")
        key, value = line.split("=", 1)
        fields = shlex.split(value, comments=False, posix=True)
        if len(fields) > 1:
            raise UpgradeError(f"value must resolve to one field at {path}:{number}")
        values[key.strip()] = fields[0] if fields else ""
    return values


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_state(operation_id: str) -> tuple[Path, dict[str, Any]]:
    checked_id(operation_id, "operation ID")
    state_path = DEFAULT_STATE_ROOT / operation_id / "state.json"
    try:
        value = json.loads(state_path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise UpgradeError(f"unknown solution-upgrade operation {operation_id!r}") from error
    except json.JSONDecodeError as error:
        raise UpgradeError("solution-upgrade state is invalid") from error
    if value.get("operation_id") != operation_id:
        raise UpgradeError("solution-upgrade state identity does not match its path")
    return state_path, value


def update_state(state_path: Path, state: dict[str, Any], **changes: Any) -> None:
    state.update(changes)
    state["updated_at"] = now()
    atomic_json(state_path, state)


def run(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(arguments, check=True, text=True, **kwargs)


def api(method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    body = None
    headers = {"Accept": "application/json", "X-TVT-Actor": "solution-upgrade"}
    if payload is not None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        API_ROOT + path, data=body, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            content = response.read()
    except urllib.error.HTTPError as error:
        detail = error.read(2048).decode("utf-8", "replace")
        raise UpgradeError(
            f"management API rejected {method} {path}: HTTP {error.code}: {detail}"
        ) from error
    except urllib.error.URLError as error:
        raise UpgradeError(f"management API is unavailable: {error.reason}") from error
    return json.loads(content) if content else None


def deployment(deployment_id: str) -> dict[str, Any]:
    values = api("GET", "/deployments")
    match = next(
        (item for item in values if item.get("deployment_id") == deployment_id), None
    )
    if match is None:
        raise UpgradeError(f"deployment {deployment_id!r} is not present")
    return match


def copy_file(source: Path, target: Path, mode: int) -> None:
    if not source.is_file() or source.is_symlink():
        raise UpgradeError(f"required regular file is missing: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    os.close(descriptor)
    temporary = Path(name)
    try:
        shutil.copyfile(source, temporary)
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def backup_active_files(operation_directory: Path) -> dict[str, str | None]:
    backup = operation_directory / "active-config-backup"
    backup.mkdir(mode=0o700, exist_ok=True)
    result: dict[str, str | None] = {}
    for index, source in enumerate(ACTIVE_FILES):
        key = str(source)
        if not source.exists():
            result[key] = None
            continue
        if not source.is_file() or source.is_symlink():
            raise UpgradeError(f"refusing unsafe active configuration path: {source}")
        target = backup / f"{index:02d}-{source.name}"
        shutil.copy2(source, target)
        os.chmod(target, 0o600)
        result[key] = str(target)
    return result


def restore_active_files(state: dict[str, Any]) -> None:
    backup_files = state.get("active_config_backup")
    if not isinstance(backup_files, dict):
        raise UpgradeError("operation has no active configuration backup")
    for target_name, backup_name in backup_files.items():
        target = Path(target_name)
        if target not in ACTIVE_FILES:
            raise UpgradeError("operation contains an unexpected backup target")
        if backup_name is None:
            target.unlink(missing_ok=True)
            continue
        backup = Path(backup_name)
        if not backup.is_file() or backup.is_symlink():
            raise UpgradeError(f"active configuration backup is missing: {backup}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup, target)
    run(["systemctl", "daemon-reload"])
    run(["systemctl", "restart", "tvt-pipeline-image-sync.timer"])


def catalog_command(*arguments: str) -> None:
    executable = Path("/opt/tvt/venv/bin/tvt-edge")
    if not executable.is_file():
        raise UpgradeError("installed TVT management CLI is missing")
    resource_root = Path("/opt/tvt/current/resources")
    if not resource_root.is_dir():
        resource_root = Path("/opt/tvt/current")
    run(
        [
            "runuser",
            "-u",
            "tvt-edge",
            "--",
            "env",
            "TVT_DATABASE_URL=postgresql+psycopg:///tvt",
            f"TVT_RESOURCE_ROOT={resource_root}",
            str(executable),
            *arguments,
        ]
    )


def prepare(args: argparse.Namespace) -> dict[str, Any]:
    require_root()
    deployment_id = checked_id(args.deployment_id, "deployment ID")
    operation_id = checked_id(args.operation_id or str(uuid.uuid4()), "operation ID")
    operation_directory = DEFAULT_STATE_ROOT / operation_id
    state_path = operation_directory / "state.json"

    bundle_input = Path(args.bundle)
    bundle = bundle_input.resolve(strict=True)
    if not bundle.is_dir() or bundle_input.is_symlink():
        raise UpgradeError("release bundle must be a non-symlink directory")
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    pipeline_env = bundle / "config/pipeline.env"
    platform_env = bundle / "config/platform.env"
    values = load_env(pipeline_env)
    catalog_id = values.get("PIPELINE_TRAFFIC_CATALOG_ID", "")
    version = values.get("PIPELINE_TRAFFIC_VERSION", "")
    if not catalog_id or not version:
        raise UpgradeError("release pipeline pin is incomplete")
    archive_relative = manifest.get("artifacts", {}).get("traffic_image")
    if not isinstance(archive_relative, str):
        raise UpgradeError("release manifest has no Traffic image artifact")
    archive = bundle / archive_relative
    catalog = bundle / "solution-packs/catalog" / f"tvt-mills-pilot-{version}"
    if not archive.is_file() or archive.is_symlink():
        raise UpgradeError("release Traffic image artifact is missing or unsafe")
    if not catalog.is_dir() or catalog.is_symlink():
        raise UpgradeError("release solution catalog is missing or unsafe")
    for filename in CATALOG_FILES:
        path = catalog / filename
        if not path.is_file() or path.is_symlink():
            raise UpgradeError(f"release solution catalog is missing {filename}")

    operation_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(operation_directory, 0o700)
    staged = operation_directory / "delivery"
    staged_catalog = staged / "catalog"
    staged_archive = staged / archive.name
    if state_path.exists():
        _, state = read_state(operation_id)
        expected_identity = {
            "deployment_id": deployment_id,
            "bundle": str(bundle),
            "catalog_id": catalog_id,
            "version": version,
            "staged_archive": str(staged_archive),
            "staged_catalog": str(staged_catalog),
        }
        if any(state.get(key) != value for key, value in expected_identity.items()):
            raise UpgradeError("prepare arguments do not match the existing operation")
        if state.get("status") == "prepared":
            return state
        if state.get("status") != "preparing":
            raise UpgradeError(
                f"prepare cannot resume from status {state.get('status')!r}"
            )
        for path in (
            staged_archive,
            staged / "pipeline.env",
            staged / "platform.env",
            *(staged_catalog / name for name in CATALOG_FILES),
        ):
            if not path.is_file() or path.is_symlink():
                raise UpgradeError(f"staged upgrade input is missing or unsafe: {path}")
    else:
        staged_catalog.mkdir(parents=True, exist_ok=True, mode=0o700)
        copy_file(archive, staged_archive, 0o600)
        for filename in CATALOG_FILES:
            copy_file(catalog / filename, staged_catalog / filename, 0o600)
        copy_file(pipeline_env, staged / "pipeline.env", 0o600)
        copy_file(platform_env, staged / "platform.env", 0o600)
        state = {
            "schema_version": 1,
            "operation_id": operation_id,
            "status": "preparing",
            "deployment_id": deployment_id,
            "bundle": str(bundle),
            "catalog_id": catalog_id,
            "version": version,
            "staged_archive": str(staged_archive),
            "staged_catalog": str(staged_catalog),
            "image_lock": str(operation_directory / "traffic-image.lock.json"),
            "active_config_backup": backup_active_files(operation_directory),
            "created_at": now(),
        }
        atomic_json(state_path, state)

    installed_python = Path("/opt/tvt/venv/bin/python")
    if not installed_python.is_file():
        raise UpgradeError("installed TVT Python environment is missing")
    run(
        [
            str(installed_python),
            str(bundle / "scripts/validate-solution-delivery.py"),
            "--catalog",
            str(staged_catalog),
            "--config",
            str(staged / "pipeline.env"),
            "--archive",
            str(staged_archive),
        ]
    )
    run(
        [
            str(bundle / "scripts/tvt-edge-operations.sh"),
            "import-pipeline-traffic-image",
            "--mode",
            "archive",
            "--archive-file",
            str(staged_archive),
            "--metadata-directory",
            str(staged_catalog),
            "--work-dir",
            "/var/lib/tvt/pipeline/work",
            "--lock-output",
            state["image_lock"],
            "--concurrency-lock",
            "/var/lib/tvt/pipeline/import.lock",
        ]
    )
    lock = json.loads(Path(state["image_lock"]).read_text(encoding="utf-8"))
    reference = lock.get("image", {}).get("reference")
    digest = lock.get("image", {}).get("digest")
    if (
        not isinstance(reference, str)
        or not isinstance(digest, str)
        or not DIGEST.fullmatch(digest)
        or not reference.endswith("@" + digest)
        or lock.get("catalog_id") != catalog_id
    ):
        raise UpgradeError("imported image lock is invalid")
    run(["k3s", "crictl", "pull", reference])
    run(["k3s", "crictl", "inspecti", reference], stdout=subprocess.DEVNULL)

    catalog_command(
        "seed-solutions",
        "--delivery-directory",
        str(staged_catalog),
        "--registry",
        "127.0.0.1:5000",
    )
    catalog_command("refresh-solutions")
    quoted = urllib.parse.quote(deployment_id, safe="")
    preview = api(
        "POST",
        f"/deployments/{quoted}/upgrade/preview",
        {"catalog_id": catalog_id},
    )
    source_sha = preview.get("source_bundle_sha256")
    target_sha = preview.get("bundle_sha256")
    if (
        preview.get("catalog_id") != catalog_id
        or preview.get("image_reference") != reference
        or not isinstance(source_sha, str)
        or not HEX.fullmatch(source_sha)
        or not isinstance(target_sha, str)
        or not HEX.fullmatch(target_sha)
    ):
        raise UpgradeError("management API upgrade preview does not match the staged image")

    update_state(
        state_path,
        state,
        status="prepared",
        source_catalog_id=preview.get("source_catalog_id"),
        source_applied_revision=preview.get("source_applied_revision"),
        source_bundle_sha256=source_sha,
        source_image_digest=preview.get("source_image_digest"),
        target_bundle_sha256=target_sha,
        target_image_digest=digest,
        target_image_reference=reference,
        prepared_at=now(),
    )
    return state


def wait_for_bundle(
    state: dict[str, Any], target_sha: str, target_digest: str | None, timeout: int
) -> tuple[str, dict[str, Any]]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = deployment(state["deployment_id"])
        if (
            current.get("sync_state") == "applied"
            and current.get("applied_bundle_sha256") == target_sha
            and (
                target_digest is None
                or current.get("applied_image_digest") == target_digest
            )
        ):
            return "applied", current
        if current.get("sync_state") in {"operator_required", "failed"}:
            return str(current["sync_state"]), current
        time.sleep(2)
    return "timeout", deployment(state["deployment_id"])


def install_active_pipeline(state: dict[str, Any]) -> None:
    bundle = Path(state["bundle"])
    if not bundle.is_dir() or bundle.is_symlink():
        raise UpgradeError("the verified release bundle is unavailable for activation")
    run(
        [
            str(bundle / "scripts/tvt-edge-operations.sh"),
            "install-pipeline-image-sync",
            "--archive-file",
            state["staged_archive"],
            "--metadata-directory",
            state["staged_catalog"],
        ]
    )
    copy_file(
        Path(state["image_lock"]),
        Path("/var/lib/tvt/pipeline/traffic-image.lock.json"),
        0o600,
    )


def rollback_state(
    state_path: Path,
    state: dict[str, Any],
    timeout: int,
    reason: str,
) -> dict[str, Any]:
    quoted = urllib.parse.quote(state["deployment_id"], safe="")
    if state.get("status") != "rolling_back":
        response = api(
            "POST",
            f"/deployments/{quoted}/rollback",
            {"bundle_sha256": state["source_bundle_sha256"]},
        )
        update_state(
            state_path,
            state,
            status="rolling_back",
            rollback_reason=reason,
            rollback_desired_revision=response.get("desired_revision"),
        )
    outcome, current = wait_for_bundle(
        state, state["source_bundle_sha256"], state.get("source_image_digest"), timeout
    )
    if outcome != "applied":
        update_state(
            state_path,
            state,
            status="rollback_failed" if outcome != "timeout" else "rollback_timeout",
            observed_sync_state=current.get("sync_state"),
        )
        raise UpgradeError(f"rollback did not complete: {outcome}")
    if state.get("active_pipeline_install_started"):
        restore_active_files(state)
    update_state(
        state_path,
        state,
        status="rolled_back",
        rolled_back_at=now(),
        observed_sync_state=current.get("sync_state"),
    )
    return state


def activate(args: argparse.Namespace) -> dict[str, Any]:
    require_root()
    state_path, state = read_state(args.operation_id)
    if state.get("status") == "applied":
        return state
    if state.get("status") not in {"prepared", "activating", "finalizing"}:
        raise UpgradeError(
            f"operation cannot activate from status {state.get('status')!r}"
        )
    quoted = urllib.parse.quote(state["deployment_id"], safe="")
    response = api(
        "POST",
        f"/deployments/{quoted}/upgrade",
        {
            "catalog_id": state["catalog_id"],
            "source_bundle_sha256": state["source_bundle_sha256"],
            "preview_bundle_sha256": state["target_bundle_sha256"],
            "idempotency_key": f"solution-upgrade:{state['operation_id']}",
        },
    )
    update_state(
        state_path,
        state,
        status="activating",
        target_desired_revision=response.get("desired_revision"),
        activated_at=state.get("activated_at") or now(),
    )
    outcome, current = wait_for_bundle(
        state,
        state["target_bundle_sha256"],
        state["target_image_digest"],
        args.timeout,
    )
    if outcome != "applied":
        update_state(
            state_path,
            state,
            status="activation_timeout" if outcome == "timeout" else "activation_failed",
            observed_sync_state=current.get("sync_state"),
        )
        if outcome != "timeout":
            return rollback_state(
                state_path, state, args.timeout, f"automatic after {outcome}"
            )
        raise UpgradeError("activation timed out; inspect status before choosing rollback")

    update_state(
        state_path,
        state,
        status="finalizing",
        active_pipeline_install_started=True,
    )
    install_active_pipeline(state)
    update_state(
        state_path,
        state,
        status="applied",
        active_pipeline_installed=True,
        applied_at=now(),
        observed_sync_state=current.get("sync_state"),
    )
    return state


def status(args: argparse.Namespace) -> dict[str, Any]:
    state_path, state = read_state(args.operation_id)
    result = dict(state)
    try:
        result["deployment"] = deployment(state["deployment_id"])
    except UpgradeError as error:
        result["deployment_error"] = str(error)
    result["state_file"] = str(state_path)
    return result


def rollback(args: argparse.Namespace) -> dict[str, Any]:
    require_root()
    state_path, state = read_state(args.operation_id)
    if state.get("status") == "rolled_back":
        return state
    if not state.get("source_bundle_sha256"):
        raise UpgradeError("operation has no source bundle to restore")
    return rollback_state(state_path, state, args.timeout, "operator requested")


def summary(state: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "operation_id",
        "status",
        "deployment_id",
        "version",
        "catalog_id",
        "source_catalog_id",
        "source_applied_revision",
        "source_bundle_sha256",
        "source_image_digest",
        "target_bundle_sha256",
        "target_image_digest",
        "target_image_reference",
        "target_desired_revision",
        "observed_sync_state",
        "created_at",
        "prepared_at",
        "activated_at",
        "applied_at",
        "rolled_back_at",
        "updated_at",
        "state_file",
        "deployment",
        "deployment_error",
    )
    return {key: state[key] for key in keys if key in state}


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="action", required=True)
    prepare_command = commands.add_parser("prepare")
    prepare_command.add_argument("--bundle", required=True)
    prepare_command.add_argument("--deployment-id", required=True)
    prepare_command.add_argument("--operation-id")
    activate_command = commands.add_parser("activate")
    activate_command.add_argument("--operation-id", required=True)
    activate_command.add_argument("--timeout", type=int, default=900)
    status_command = commands.add_parser("status")
    status_command.add_argument("--operation-id", required=True)
    rollback_command = commands.add_parser("rollback")
    rollback_command.add_argument("--operation-id", required=True)
    rollback_command.add_argument("--timeout", type=int, default=900)
    return root


def main() -> int:
    args = parser().parse_args()
    if getattr(args, "timeout", 1) < 1:
        raise UpgradeError("--timeout must be positive")
    actions = {
        "prepare": prepare,
        "activate": activate,
        "status": status,
        "rollback": rollback,
    }
    try:
        result = actions[args.action](args)
    except (
        UpgradeError,
        OSError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
        subprocess.CalledProcessError,
    ) as error:
        print(f"solution-upgrade: ERROR: {error}", file=sys.stderr)
        return 1
    print(json.dumps(summary(result), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
