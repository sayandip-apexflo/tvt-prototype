#!/usr/bin/env python3
"""Plan and orchestrate a complete TVT edge release upgrade.

The target release bundle is the source of truth.  This command deliberately
does not infer actions from a Git checkout on the edge: it compares the
installed and target release manifests/checksums, stages immutable artifacts,
and delegates component mutations to the consolidated host operations.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
from typing import Any


STATE_ROOT = Path(
    os.environ.get("TVT_RELEASE_UPGRADE_STATE_DIR", "/var/lib/tvt/install/release-upgrades")
)
OPT_TVT = Path(os.environ.get("TVT_OPT_DIRECTORY", "/opt/tvt"))
INSTALL_STATE_ROOT = Path(
    os.environ.get("TVT_INSTALL_STATE_ROOT", "/var/lib/tvt/install")
)
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")

RESOURCE_ITEMS = (
    "manifest.json",
    "checksums.sha256",
    "alembic.ini",
    "apexfabric",
    "config",
    "deploy",
    "docs",
    "examples",
    "scripts",
    "solution-packs",
    "images",
    "k3s",
    "tvt_edge",
)

UNIT_NAMES = (
    "apexfabric-control.service",
    "tvt-edge.service",
    "tvt-camera-sync.service",
    "tvt-alert-dispatcher.service",
    "tvt-retention.service",
    "tvt-retention.timer",
    "tvt-k3s-watchdog.service",
    "tvt-k3s-watchdog.timer",
    "tvt-anpr-report.service",
    "tvt-anpr-report.timer",
    "tvt-anpr-report-attendance.service",
    "tvt-anpr-report-attendance.timer",
    "tvt-pipeline-image-sync.service",
    "tvt-pipeline-image-sync.timer",
)


class ReleaseUpgradeError(RuntimeError):
    pass


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def require_root() -> None:
    if os.geteuid() != 0:
        raise ReleaseUpgradeError("run this command as root (for example, with sudo)")


def checked_id(value: str, label: str) -> str:
    if not SAFE_ID.fullmatch(value):
        raise ReleaseUpgradeError(f"{label} contains unsupported characters")
    return value


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ReleaseUpgradeError(f"required file is missing: {path}") from error
    except json.JSONDecodeError as error:
        raise ReleaseUpgradeError(f"invalid JSON in {path}: {error}") from error
    if not isinstance(value, dict):
        raise ReleaseUpgradeError(f"expected a JSON object in {path}")
    return value


def load_checksums(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as error:
        raise ReleaseUpgradeError(f"release checksums are missing: {path}") from error
    for number, line in enumerate(lines, 1):
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if not match:
            raise ReleaseUpgradeError(f"invalid checksum line {number} in {path}")
        digest, raw = match.groups()
        relative = PurePosixPath(raw)
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise ReleaseUpgradeError(f"unsafe checksum path in {path}: {raw!r}")
        result[relative.as_posix()] = digest
    return result


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


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def command(
    arguments: list[str], *, env: dict[str, str] | None = None, capture: bool = False
) -> subprocess.CompletedProcess[str]:
    effective_env = env or os.environ
    pass_fds: tuple[int, ...] = ()
    if effective_env.get("TVT_INSTALL_LOCK_HELD") == "1" and Path("/proc/self/fd/8").exists():
        pass_fds = (8,)
    return subprocess.run(
        arguments,
        check=True,
        text=True,
        env=env,
        capture_output=capture,
        pass_fds=pass_fds,
    )


def artifact_digest(
    manifest: dict[str, Any], checksums: dict[str, str], role: str
) -> str | None:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        return None
    relative = artifacts.get(role)
    if not isinstance(relative, str):
        return None
    return checksums.get(PurePosixPath(relative).as_posix())


def group_signature(checksums: dict[str, str], prefixes: tuple[str, ...]) -> list[tuple[str, str]]:
    return sorted(
        (path, digest)
        for path, digest in checksums.items()
        if any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in prefixes)
    )


def component_changed(
    current_manifest: dict[str, Any],
    current_checksums: dict[str, str],
    target_manifest: dict[str, Any],
    target_checksums: dict[str, str],
    *,
    artifacts: tuple[str, ...] = (),
    prefixes: tuple[str, ...] = (),
) -> bool:
    for role in artifacts:
        if artifact_digest(current_manifest, current_checksums, role) != artifact_digest(
            target_manifest, target_checksums, role
        ):
            return True
    return group_signature(current_checksums, prefixes) != group_signature(
        target_checksums, prefixes
    )


def installed_resources() -> Path:
    current = OPT_TVT / "current"
    try:
        release = current.resolve(strict=True)
    except FileNotFoundError as error:
        raise ReleaseUpgradeError("the installed /opt/tvt/current release link is missing") from error
    resources = release / "resources"
    return resources if resources.is_dir() else release


def validate_upgrade_policy(manifest: dict[str, Any]) -> dict[str, Any]:
    policy = manifest.get("upgrade", {})
    if not isinstance(policy, dict):
        raise ReleaseUpgradeError("manifest upgrade policy must be an object")
    database = policy.get("database", {})
    if not isinstance(database, dict):
        raise ReleaseUpgradeError("manifest database upgrade policy must be an object")
    strategy = database.get("strategy", "forward-only")
    rollback_compatible = database.get("rollback_compatible", False)
    if strategy not in {"expand-contract", "forward-only", "none"}:
        raise ReleaseUpgradeError("manifest database upgrade strategy is invalid")
    if not isinstance(rollback_compatible, bool):
        raise ReleaseUpgradeError("database rollback_compatible must be boolean")
    actions = policy.get("operator_actions", [])
    if not isinstance(actions, list):
        raise ReleaseUpgradeError("manifest operator_actions must be a list")
    validated_actions: list[dict[str, Any]] = []
    for action in actions:
        if not isinstance(action, dict):
            raise ReleaseUpgradeError("every operator action must be an object")
        operation = action.get("operation")
        timing = action.get("timing")
        required = action.get("required")
        if not isinstance(operation, str) or not SAFE_ID.fullmatch(operation):
            raise ReleaseUpgradeError("operator action operation is invalid")
        if timing not in {"before_activation", "after_activation"}:
            raise ReleaseUpgradeError("operator action timing is invalid")
        if not isinstance(required, bool):
            raise ReleaseUpgradeError("operator action required must be boolean")
        validated_actions.append(dict(action))
    return {
        "database": {
            "strategy": strategy,
            "rollback_compatible": rollback_compatible,
        },
        "operator_actions": validated_actions,
    }


def build_plan(bundle: Path, current_resources: Path | None = None) -> dict[str, Any]:
    target = bundle.resolve(strict=True)
    if not target.is_dir() or bundle.is_symlink():
        raise ReleaseUpgradeError("release bundle must be a non-symlink directory")
    current = current_resources or installed_resources()
    target_manifest = load_json(target / "manifest.json")
    current_manifest = load_json(current / "manifest.json")
    target_checksums = load_checksums(target / "checksums.sha256")
    current_checksums = load_checksums(current / "checksums.sha256")
    target_version = target_manifest.get("release_version")
    current_version = current_manifest.get("release_version")
    if not isinstance(target_version, str) or not VERSION.fullmatch(target_version):
        raise ReleaseUpgradeError("target release version is invalid")
    if not isinstance(current_version, str) or not VERSION.fullmatch(current_version):
        raise ReleaseUpgradeError("installed release version is invalid")
    if target_version == current_version:
        raise ReleaseUpgradeError(f"release {target_version} is already active")

    changes = {
        "host_application": component_changed(
            current_manifest,
            current_checksums,
            target_manifest,
            target_checksums,
            artifacts=("application_wheel",),
            prefixes=("alembic.ini", "tvt_edge/", "apexfabric/control_plane/"),
        ),
        "host_resources_and_units": component_changed(
            current_manifest,
            current_checksums,
            target_manifest,
            target_checksums,
            prefixes=("scripts/", "config/", "deploy/host/", "deploy/systemd/"),
        ),
        "dashboard": component_changed(
            current_manifest,
            current_checksums,
            target_manifest,
            target_checksums,
            artifacts=("ui_image",),
            prefixes=("deploy/ui/", "deploy/single-box/ui.yaml"),
        ),
        "node_management": component_changed(
            current_manifest,
            current_checksums,
            target_manifest,
            target_checksums,
            artifacts=("node_reporter_image", "node_status_controller_image"),
            prefixes=(
                "deploy/k8s/apexfabric-foundation.yaml",
                "deploy/k8s/apexfabric-node-management.yaml",
            ),
        ),
        "cv_solution": component_changed(
            current_manifest,
            current_checksums,
            target_manifest,
            target_checksums,
            artifacts=("traffic_image",),
            prefixes=("config/pipeline.env", "solution-packs/"),
        ),
        "local_registry": component_changed(
            current_manifest,
            current_checksums,
            target_manifest,
            target_checksums,
            artifacts=("registry_image",),
        ),
        "k3s": component_changed(
            current_manifest,
            current_checksums,
            target_manifest,
            target_checksums,
            artifacts=("k3s_installer", "k3s_binary"),
        ),
        "host_packages_or_drivers": component_changed(
            current_manifest,
            current_checksums,
            target_manifest,
            target_checksums,
            prefixes=("packages/apt/", "hardware/", "prepare-tvt-edge-host.sh"),
        ),
    }
    upgrade_policy = validate_upgrade_policy(target_manifest)
    platform_changes = [
        name
        for name in ("local_registry", "k3s", "host_packages_or_drivers")
        if changes[name]
    ]
    if changes["host_application"] and not upgrade_policy["database"]["rollback_compatible"]:
        platform_changes.append("database_offline_migration")
    interruption: list[str] = ["brief management-plane service restart"]
    if changes["dashboard"]:
        interruption.append("brief dashboard Recreate rollout")
    if changes["node_management"]:
        interruption.append("rolling node-management restart")
    if changes["cv_solution"]:
        interruption.append("CV inference interruption during the single-replica Recreate rollout")
    if platform_changes:
        interruption.append("full platform maintenance/reboot may be required")
    return {
        "schema_version": 1,
        "current_release_version": current_version,
        "target_release_version": target_version,
        "target_source_commit": target_manifest.get("source_commit"),
        "bundle": str(target),
        "current_resources": str(current),
        "changes": changes,
        "platform_changes": platform_changes,
        "activation_mode": "platform_maintenance" if platform_changes else "in_place",
        "availability_impact": interruption,
        "upgrade_policy": upgrade_policy,
    }


def staged_release_is_valid(release: Path, bundle_digest: str) -> bool:
    marker = release / ".prepared-bundle-sha256"
    executable = release / "venv/bin/tvt-edge"
    try:
        shebang = executable.open("rb").readline().rstrip()
        expected_shebangs = {
            f"#!{release}/venv/bin/python".encode(),
            f"#!{release}/venv/bin/python3".encode(),
        }
        return (
            marker.read_text(encoding="utf-8").strip() == bundle_digest
            and executable.is_file()
            and os.access(executable, os.X_OK)
            and shebang in expected_shebangs
            and (release / "resources/manifest.json").is_file()
        )
    except OSError:
        return False


def stage_release(bundle: Path, version: str) -> Path:
    releases = OPT_TVT / "releases"
    releases.mkdir(parents=True, exist_ok=True, mode=0o755)
    final = releases / version
    bundle_digest = sha256(bundle / "checksums.sha256")
    preparing_marker = final / ".preparing-bundle-sha256"
    if final.exists():
        if final.is_symlink():
            raise ReleaseUpgradeError(
                f"release directory {final} already exists but is not the prepared target bundle"
            )
        if staged_release_is_valid(final, bundle_digest):
            return final
        try:
            resumable = preparing_marker.read_text(encoding="utf-8").strip() == bundle_digest
        except OSError:
            resumable = False
        if not resumable:
            raise ReleaseUpgradeError(
                f"release directory {final} already exists but is not the prepared target bundle"
            )
        shutil.rmtree(final)

    temporary = Path(tempfile.mkdtemp(prefix=f".{version}.prepare.", dir=releases))
    try:
        (temporary / ".preparing-bundle-sha256").write_text(
            bundle_digest + "\n", encoding="utf-8"
        )
        os.chmod(temporary, 0o755)
        os.replace(temporary, final)

        # Python virtual environments are not relocatable: pip-generated entry
        # points contain an absolute shebang. Build the venv only after the
        # directory has its permanent path so activation cannot reference the
        # now-absent temporary directory.
        resources = final / "resources"
        resources.mkdir(mode=0o755)
        for item in RESOURCE_ITEMS:
            source = bundle / item
            if not source.exists() or source.is_symlink():
                raise ReleaseUpgradeError(f"release resource is missing or unsafe: {source}")
            target = resources / item
            if source.is_dir():
                shutil.copytree(source, target)
            else:
                shutil.copy2(source, target)
        venv = final / "venv"
        command(["python3", "-m", "venv", "--clear", str(venv)])
        manifest = load_json(bundle / "manifest.json")
        wheel_relative = manifest.get("artifacts", {}).get("application_wheel")
        if not isinstance(wheel_relative, str):
            raise ReleaseUpgradeError("release manifest has no application wheel")
        command(
            [
                str(venv / "bin/python"),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-index",
                "--find-links",
                str(bundle / "wheels"),
                str(bundle / wheel_relative),
            ]
        )
        command(["chown", "-R", "root:root", str(final)])
        command(["chmod", "-R", "go+rX", str(venv), str(resources)])
        os.replace(preparing_marker, final / ".prepared-bundle-sha256")
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return final


def read_state(operation_id: str) -> tuple[Path, dict[str, Any]]:
    checked_id(operation_id, "operation ID")
    path = STATE_ROOT / operation_id / "state.json"
    state = load_json(path)
    if state.get("operation_id") != operation_id:
        raise ReleaseUpgradeError("release-upgrade state identity does not match its path")
    return path, state


def update_state(path: Path, state: dict[str, Any], **changes: Any) -> None:
    state.update(changes)
    state["updated_at"] = now()
    atomic_json(path, state)


def helper_environment(state: dict[str, Any]) -> dict[str, str]:
    environment = dict(os.environ)
    # Release bundles are checksum-complete immutable inputs. Several component
    # operations run Python with the bundle as their working directory, where
    # normal imports would otherwise create unlisted __pycache__ files and make
    # the next integrity check reject the bundle.
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PATH"] = f"{state['staged_release']}/venv/bin:{environment.get('PATH', '')}"
    environment["TVT_SOLUTION_UPGRADE_STATE_DIR"] = str(
        Path(state["operation_directory"]) / "solution-upgrades"
    )
    return environment


def run_solution(state: dict[str, Any], action: str, timeout: int = 900) -> dict[str, Any]:
    helper = Path(state["bundle"]) / "scripts/lib/tvt-solution-upgrade.py"
    arguments = [sys.executable, str(helper), action]
    if action == "prepare":
        arguments += [
            "--bundle",
            state["bundle"],
            "--deployment-id",
            state["deployment_id"],
            "--operation-id",
            state["solution_operation_id"],
        ]
    else:
        arguments += ["--operation-id", state["solution_operation_id"]]
        if action in {"activate", "rollback"}:
            arguments += ["--timeout", str(timeout)]
    result = command(arguments, env=helper_environment(state), capture=True)
    return json.loads(result.stdout)


def run_platform_preparation(state: dict[str, Any]) -> dict[str, Any]:
    bundle = Path(state["bundle"])
    command(
        [
            "/bin/bash",
            str(bundle / "prepare-tvt-edge-host.sh"),
            "--bundle",
            state["bundle"],
            "--mode",
            "offline",
            "--platform-upgrade",
        ],
        env=helper_environment(state),
    )
    preparation = load_json(INSTALL_STATE_ROOT / "prepare-state.json")
    target = state["plan"]["target_release_version"]
    if preparation.get("release_version") != target:
        raise ReleaseUpgradeError("platform preparation state does not match target release")
    return preparation


def apply_platform_components(state: dict[str, Any]) -> None:
    changes = state["plan"]["changes"]
    operations = Path(state["bundle"]) / "scripts/tvt-edge-operations.sh"
    environment = helper_environment(state)
    if changes["local_registry"]:
        command(
            [
                str(operations),
                "install-local-registry",
                "--image-archive",
                str(Path(state["bundle"]) / "images/registry.tar"),
            ],
            env=environment,
        )
    if changes["k3s"]:
        command(
            [
                str(operations),
                "install-k3s-single-node",
                "--installer",
                str(Path(state["bundle"]) / "k3s/install.sh"),
                "--k3s-binary",
                str(Path(state["bundle"]) / "k3s/k3s"),
            ],
            env=environment,
        )


def prepare(args: argparse.Namespace) -> dict[str, Any]:
    require_root()
    bundle_input = Path(args.bundle)
    bundle = bundle_input.resolve(strict=True)
    plan = build_plan(bundle)
    platform_maintenance = bool(plan["platform_changes"])
    if platform_maintenance and not args.platform_maintenance:
        changed = ", ".join(plan["platform_changes"])
        raise ReleaseUpgradeError(
            "target changes platform artifacts requiring the rebooting host-maintenance "
            f"track ({changed}); rerun prepare with --platform-maintenance"
        )
    if args.platform_maintenance and not platform_maintenance:
        raise ReleaseUpgradeError("--platform-maintenance was supplied for an in-place release")
    if "database_offline_migration" in plan["platform_changes"]:
        raise ReleaseUpgradeError(
            "forward-only database migration requires a release-specific reviewed recovery plan"
        )
    if plan["changes"]["cv_solution"] and not args.deployment_id:
        raise ReleaseUpgradeError("--deployment-id is required because the CV solution changes")
    deployment_id = checked_id(args.deployment_id, "deployment ID") if args.deployment_id else None
    operation_id = checked_id(args.operation_id or str(uuid.uuid4()), "operation ID")
    operation_directory = STATE_ROOT / operation_id
    state_path = operation_directory / "state.json"
    if state_path.exists():
        _, state = read_state(operation_id)
        if (
            state.get("bundle") != str(bundle)
            or state.get("deployment_id") != deployment_id
            or state.get("platform_maintenance") != platform_maintenance
        ):
            raise ReleaseUpgradeError("prepare arguments do not match the existing operation")
        if state.get("status") in {"prepared", "platform_reboot_required"}:
            return state
        if state.get("status") != "preparing":
            raise ReleaseUpgradeError(f"prepare cannot resume from {state.get('status')!r}")
    else:
        operation_directory.mkdir(parents=True, mode=0o700)
        os.chmod(operation_directory, 0o700)
        state = {
            "schema_version": 1,
            "operation_id": operation_id,
            "status": "preparing",
            "bundle": str(bundle),
            "operation_directory": str(operation_directory),
            "deployment_id": deployment_id,
            "platform_maintenance": platform_maintenance,
            "solution_operation_id": f"{operation_id}-solution",
            "plan": plan,
            "created_at": now(),
        }
        atomic_json(state_path, state)

    staged = stage_release(bundle, plan["target_release_version"])
    update_state(state_path, state, staged_release=str(staged), stage="application_staged")
    operations = bundle / "scripts/tvt-edge-operations.sh"
    environment = helper_environment(state)
    ui_lock = operation_directory / "ui-image.lock.json"
    node_lock = operation_directory / "node-management-images.lock.json"
    command(
        [
            str(operations), "publish-ui-image", "--registry", "127.0.0.1:5000",
            "--scheme", "http", "--archive-dir", str(bundle / "images"),
            "--lock-output", str(ui_lock),
        ],
        env=environment,
    )
    command(
        [
            str(operations), "publish-control-images", "--registry", "127.0.0.1:5000",
            "--scheme", "http", "--archive-dir", str(bundle / "images"),
            "--lock-output", str(node_lock),
        ],
        env=environment,
    )
    update_state(
        state_path,
        state,
        ui_image_lock=str(ui_lock),
        node_management_image_lock=str(node_lock),
        stage="images_staged",
    )
    if plan["changes"]["cv_solution"]:
        solution = run_solution(state, "prepare")
        update_state(state_path, state, solution=solution, stage="solution_staged")
    if platform_maintenance and plan["changes"]["host_packages_or_drivers"]:
        update_state(state_path, state, stage="platform_preparing")
        preparation = run_platform_preparation(state)
        if preparation.get("status") != "reboot_required":
            raise ReleaseUpgradeError("platform preparation did not request the required reboot")
        update_state(
            state_path, state, status="platform_reboot_required",
            stage="platform_reboot_required", platform_preparation=preparation,
        )
        return state
    update_state(state_path, state, status="prepared", stage="prepared", prepared_at=now())
    return state


def activate(args: argparse.Namespace) -> dict[str, Any]:
    require_root()
    state_path, state = read_state(args.operation_id)
    if state.get("status") == "applied":
        return state
    if state.get("status") == "platform_reboot_required":
        preparation = run_platform_preparation(state)
        if preparation.get("status") != "prepared":
            raise ReleaseUpgradeError("post-reboot platform verification is incomplete")
        update_state(
            state_path, state, status="prepared", stage="platform_prepared",
            platform_preparation=preparation, platform_prepared_at=now(),
        )
    if state.get("status") not in {"prepared", "activating", "operator_required"}:
        raise ReleaseUpgradeError(f"operation cannot activate from {state.get('status')!r}")
    update_state(state_path, state, status="activating", activated_at=state.get("activated_at") or now())
    operations = Path(state["bundle"]) / "scripts/tvt-edge-operations.sh"
    if state["plan"]["platform_changes"] and not state.get("platform_components_applied"):
        try:
            apply_platform_components(state)
        except subprocess.CalledProcessError as error:
            update_state(
                state_path,
                state,
                status="operator_required",
                failed_stage="platform_components",
                last_error="registry or K3s platform activation failed",
            )
            raise ReleaseUpgradeError("platform component activation failed") from error
        update_state(
            state_path, state, platform_components_applied=True,
            stage="platform_components_applied",
        )
    if not state.get("application_applied"):
        try:
            active_release = (OPT_TVT / "current").resolve(strict=True)
        except FileNotFoundError:
            active_release = Path()
        if active_release != Path(state["staged_release"]):
            try:
                command(
                    [
                        str(operations),
                        "upgrade-application",
                        "--bundle",
                        state["bundle"],
                        "--ui-image-lock",
                        state["ui_image_lock"],
                        "--node-image-lock",
                        state["node_management_image_lock"],
                    ],
                    env=helper_environment(state),
                )
            except subprocess.CalledProcessError as error:
                update_state(
                    state_path,
                    state,
                    status="operator_required",
                    failed_stage="application",
                    last_error="application activation failed; inspect the application upgrade report",
                )
                raise ReleaseUpgradeError("application activation failed") from error
        report = load_json(INSTALL_STATE_ROOT / "application-upgrade-report.json")
        if report.get("release_version") != state["plan"]["target_release_version"]:
            raise ReleaseUpgradeError(
                "active target release has no matching application upgrade report"
            )
        update_state(
            state_path,
            state,
            application_applied=True,
            application_backup_directory=report.get("backup_directory"),
            stage="application_applied",
        )

    if state["plan"]["changes"]["cv_solution"] and not state.get("solution_applied"):
        try:
            solution = run_solution(state, "activate", args.timeout)
        except subprocess.CalledProcessError as error:
            update_state(
                state_path,
                state,
                status="operator_required",
                failed_stage="cv_solution",
                last_error="CV activation did not complete; inspect nested solution status",
            )
            raise ReleaseUpgradeError("CV solution activation failed") from error
        if solution.get("status") not in {"applied", "rolled_back"}:
            update_state(
                state_path,
                state,
                status="operator_required",
                failed_stage="cv_solution",
                solution=solution,
            )
            raise ReleaseUpgradeError("CV solution activation requires operator review")
        if solution.get("status") == "rolled_back":
            update_state(
                state_path,
                state,
                status="operator_required",
                failed_stage="cv_solution",
                solution=solution,
            )
            raise ReleaseUpgradeError("CV solution was restored to its previous release")
        update_state(state_path, state, solution=solution, solution_applied=True, stage="solution_applied")

    update_state(state_path, state, status="applied", stage="complete", applied_at=now())
    return state


def status(args: argparse.Namespace) -> dict[str, Any]:
    _, state = read_state(args.operation_id)
    result = dict(state)
    if state.get("plan", {}).get("changes", {}).get("cv_solution") \
            and state.get("staged_release") \
            and state.get("solution_operation_id"):
        try:
            result["solution"] = run_solution(state, "status")
        except (ReleaseUpgradeError, subprocess.CalledProcessError, json.JSONDecodeError) as error:
            result["solution_status_error"] = str(error)
    return result


def restore_application(state: dict[str, Any]) -> None:
    backup_value = state.get("application_backup_directory")
    if not isinstance(backup_value, str):
        raise ReleaseUpgradeError("operation has no application backup directory")
    backup = Path(backup_value).resolve(strict=True)
    allowed_root = (INSTALL_STATE_ROOT / "upgrade-backups").resolve(strict=True)
    if allowed_root not in backup.parents or backup.is_symlink():
        raise ReleaseUpgradeError("application backup directory is outside the approved root")
    previous_release = Path((backup / "previous-release").read_text(encoding="utf-8").strip())
    previous_venv = Path((backup / "previous-venv").read_text(encoding="utf-8").strip())
    if not previous_release.is_dir() or not (previous_venv / "bin/tvt-edge").is_file():
        raise ReleaseUpgradeError("previous application release is incomplete")
    active_units_path = backup / "active-units"
    active_units = (
        set(active_units_path.read_text(encoding="utf-8").splitlines())
        if active_units_path.is_file()
        else {"tvt-edge.service", "tvt-camera-sync.service"}
    )

    for unit in UNIT_NAMES:
        subprocess.run(["systemctl", "stop", unit], check=False)
    unit_backup = backup / "systemd"
    for unit in UNIT_NAMES:
        source = unit_backup / unit
        absent = unit_backup / f"{unit}.absent"
        target = Path("/etc/systemd/system") / unit
        if source.is_file() and not source.is_symlink():
            shutil.copy2(source, target)
        elif absent.is_file():
            target.unlink(missing_ok=True)

    for name in ("ui-image.lock.json", "node-management-images.lock.json"):
        source = backup / name
        target = INSTALL_STATE_ROOT / name
        if source.is_file() and not source.is_symlink():
            shutil.copy2(source, target)

    for name in ("install-state.json", "installation-report.json"):
        source = backup / name
        if source.is_file() and not source.is_symlink():
            shutil.copy2(source, INSTALL_STATE_ROOT / name)

    for link, target in ((OPT_TVT / "venv", previous_venv), (OPT_TVT / "current", previous_release)):
        temporary = link.parent / f".{link.name}.release-rollback.{os.getpid()}"
        temporary.symlink_to(target)
        os.replace(temporary, link)
    command(["systemctl", "daemon-reload"])
    old_operations = previous_release / "resources/scripts/tvt-edge-operations.sh"
    old_node_lock = INSTALL_STATE_ROOT / "node-management-images.lock.json"
    old_ui_lock = INSTALL_STATE_ROOT / "ui-image.lock.json"
    if old_node_lock.is_file():
        command([str(old_operations), "install-k3s-plane", "--image-lock", str(old_node_lock)])
    if old_ui_lock.is_file():
        command([str(old_operations), "install-apexfabric-ui", "--image-lock", str(old_ui_lock)])
    for unit in sorted(active_units):
        if unit in UNIT_NAMES:
            command(["systemctl", "start", unit])


def rollback(args: argparse.Namespace) -> dict[str, Any]:
    require_root()
    state_path, state = read_state(args.operation_id)
    if state.get("status") == "rolled_back":
        return state
    if state.get("solution_applied"):
        solution = run_solution(state, "rollback", args.timeout)
        update_state(state_path, state, solution=solution, solution_applied=False, stage="solution_rolled_back")
    if state.get("application_applied"):
        database_policy = state["plan"]["upgrade_policy"]["database"]
        if not database_policy.get("rollback_compatible"):
            update_state(
                state_path,
                state,
                status="operator_required",
                failed_stage="application_rollback",
                last_error="target manifest prohibits automatic application rollback after migration",
            )
            raise ReleaseUpgradeError(
                "automatic application rollback is prohibited by the database upgrade policy"
            )
        restore_application(state)
        update_state(state_path, state, application_applied=False, stage="application_rolled_back")
    update_state(state_path, state, status="rolled_back", rolled_back_at=now())
    return state


def summary(state: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "operation_id",
        "status",
        "stage",
        "deployment_id",
        "plan",
        "platform_maintenance",
        "platform_preparation",
        "platform_components_applied",
        "application_applied",
        "solution_applied",
        "solution",
        "failed_stage",
        "last_error",
        "created_at",
        "prepared_at",
        "platform_prepared_at",
        "activated_at",
        "applied_at",
        "rolled_back_at",
        "updated_at",
    )
    return {key: state[key] for key in keys if key in state}


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="action", required=True)
    plan_command = commands.add_parser("plan")
    plan_command.add_argument("--bundle", required=True)
    prepare_command = commands.add_parser("prepare")
    prepare_command.add_argument("--bundle", required=True)
    prepare_command.add_argument("--deployment-id")
    prepare_command.add_argument("--platform-maintenance", action="store_true")
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
    try:
        if getattr(args, "timeout", 1) < 1:
            raise ReleaseUpgradeError("--timeout must be positive")
        if args.action == "plan":
            result = build_plan(Path(args.bundle))
        else:
            result = {
                "prepare": prepare,
                "activate": activate,
                "status": status,
                "rollback": rollback,
            }[args.action](args)
    except (
        ReleaseUpgradeError,
        OSError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
        subprocess.CalledProcessError,
        urllib.error.URLError,
    ) as error:
        print(f"release-upgrade: ERROR: {error}", file=sys.stderr)
        return 1
    print(json.dumps(summary(result) if "operation_id" in result else result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
