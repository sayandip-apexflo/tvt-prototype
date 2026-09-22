#!/usr/bin/env python3
"""Deploy target-specific TVT edge bundles to a fleet over SSH.

The fleet controller deliberately delegates probing, bundle creation, and host
installation to the existing single-edge scripts.  Its responsibilities are
inventory collection, compatibility grouping, bounded concurrency, resumable
state, and reporting.
"""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - exercised only on minimal workstations
    yaml = None


REPO_ROOT = Path(__file__).resolve().parents[1]
OPERATIONS = REPO_ROOT / "scripts" / "tvt-edge-operations.sh"
RELEASE_BUILDER = REPO_ROOT / "scripts" / "make-tvt-edge-release.sh"
STATE_SCHEMA = 1
EDGE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
DEFAULT_CONCURRENCY = 5
DEFAULT_SSH_CONNECT_TIMEOUT = 15
DEFAULT_REBOOT_TIMEOUT = 900


class FleetError(RuntimeError):
    pass


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def compatibility_key(inventory: dict[str, Any]) -> str:
    """Return a stable package-compatibility key.

    PCI bus addresses, collection timestamps, RAM, and free disk are omitted:
    they are useful diagnostics but do not select a different release bundle.
    The PCI signature retains vendor/device/class, so an accelerator variant is
    never accidentally shared with an incompatible box.
    """

    pci = sorted(
        (
            item.get("vendor_id"),
            item.get("device_id"),
            item.get("class"),
        )
        for item in inventory.get("pci_devices", [])
    )
    profile = {
        "os_id": inventory.get("os_id"),
        "os_version_id": inventory.get("os_version_id"),
        "architecture": inventory.get("architecture"),
        "cpu_model": inventory.get("cpu_model"),
        "kernel_version": inventory.get("kernel_version"),
        "axelera_present": inventory.get("axelera_present"),
        "secure_boot": inventory.get("secure_boot"),
        "pci_signature": pci,
    }
    digest = hashlib.sha256(canonical_json(profile).encode("utf-8")).hexdigest()
    return f"group-{digest[:16]}"


def parse_document(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    try:
        document = yaml.safe_load(text) if yaml is not None else json.loads(text)
    except Exception as exc:  # pragma: no cover - parser-specific details
        raise FleetError(f"could not parse fleet manifest {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise FleetError("fleet manifest must contain a top-level mapping")
    return document


def load_fleet(path: Path) -> list[dict[str, Any]]:
    document = parse_document(path)
    raw_edges = document.get("edges")
    if not isinstance(raw_edges, list) or not raw_edges:
        raise FleetError("fleet manifest must contain a non-empty 'edges' list")

    edges: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_edges, start=1):
        if not isinstance(raw, dict):
            raise FleetError(f"edge entry {index} must be a mapping")
        edge_id = str(raw.get("id", "")).strip()
        if not EDGE_ID_RE.fullmatch(edge_id):
            raise FleetError(
                f"edge entry {index} has invalid id {edge_id!r}; use letters, digits, '.', '_' or '-'")
        if edge_id in seen:
            raise FleetError(f"duplicate edge id: {edge_id}")
        seen.add(edge_id)

        target = str(raw.get("ssh_target", "")).strip()
        if not target:
            host = str(raw.get("host", "")).strip()
            user = str(raw.get("user", "")).strip()
            if not host:
                raise FleetError(f"edge {edge_id}: host or ssh_target is required")
            target = f"{user}@{host}" if user else host
        if target.startswith("-") or any(char.isspace() for char in target):
            raise FleetError(f"edge {edge_id}: ssh target must be one SSH target token, not an option or command")

        site_config = raw.get("site_config")
        if not isinstance(site_config, str) or not site_config.strip():
            raise FleetError(f"edge {edge_id}: site_config is required")
        site_input = Path(site_config).expanduser()
        if site_input.is_symlink() or not site_input.is_file():
            raise FleetError(f"edge {edge_id}: site_config is not a regular file: {site_input}")
        site_path = site_input.resolve()

        edges.append({
            "id": edge_id,
            "ssh_target": target,
            "site_config": str(site_path),
            "metadata": raw.get("metadata", {}),
        })
    return edges


def atomic_write_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def run_logged(
    command: list[str],
    log_path: Path,
    timeout: int | None = None,
    *,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"\n$ {shlex.join(command)}\n")
        log.flush()
        try:
            result = subprocess.run(
                command,
                cwd=REPO_ROOT,
                text=True,
                stdout=log,
                stderr=subprocess.STDOUT,
                input=input_text,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            log.write(f"command timed out after {timeout}s\n")
            return subprocess.CompletedProcess(command, 124)
        log.write(f"exit={result.returncode}\n")
        return result


def capture_logged(
    command: list[str],
    log_path: Path,
    timeout: int | None = None,
    *,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command while retaining stdout for machine-readable responses."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"\n$ {shlex.join(command)}\n")
        try:
            result = subprocess.run(
                command,
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                input=input_text,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            log.write(f"command timed out after {timeout}s\n")
            return subprocess.CompletedProcess(command, 124, "", "")
        log.write(result.stdout)
        log.write(result.stderr)
        log.write(f"exit={result.returncode}\n")
        return result


def ssh_base(target: str) -> list[str]:
    return [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={DEFAULT_SSH_CONNECT_TIMEOUT}",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=3",
        target,
    ]


def ssh_command(target: str, remote: str) -> list[str]:
    return ssh_base(target) + [remote]


def rsync_command(source: Path, target: str, destination: str) -> list[str]:
    transport = shlex.join(ssh_base(target)[:-1])
    return ["rsync", "-a", "--checksum", "--partial", "-e", transport,
            f"{source}/", f"{target}:{destination}/"]


def rsync_file_command(source: Path, target: str, destination: str) -> list[str]:
    transport = shlex.join(ssh_base(target)[:-1])
    return ["rsync", "-a", "--checksum", "--partial", "-e", transport,
            str(source), f"{target}:{destination}"]


class FleetController:
    def __init__(
        self,
        *,
        state_dir: Path,
        edges: list[dict[str, Any]],
        version: str,
        output_dir: Path,
        concurrency: int,
        skip_tests: bool,
        allow_dirty_source: bool,
        source_commit: str,
        resume: bool,
        sudo_password: str | None,
    ) -> None:
        self.state_dir = state_dir.resolve()
        self.state_path = self.state_dir / "fleet-state.json"
        self.report_path = self.state_dir / "fleet-report.json"
        self.edges = {edge["id"]: edge for edge in edges}
        self.version = version
        self.output_dir = output_dir.resolve()
        self.concurrency = concurrency
        self.skip_tests = skip_tests
        self.allow_dirty_source = allow_dirty_source
        self.source_commit = source_commit
        self.resume = resume
        # Interactive sudo credentials are process-memory only. They are never
        # written to argv, environment variables, state, reports, or logs.
        self.sudo_password = sudo_password
        self.lock = threading.Lock()
        self.state = self._load_or_initialize()

    def _load_or_initialize(self) -> dict[str, Any]:
        if self.resume:
            if not self.state_path.is_file():
                raise FleetError(f"fleet state does not exist: {self.state_path}")
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if state.get("schema_version") != STATE_SCHEMA:
                raise FleetError("unsupported fleet state schema")
            if state.get("version") != self.version:
                raise FleetError("resume version does not match saved fleet state")
            saved_edges = state.get("edges", {})
            if set(saved_edges) != set(self.edges):
                raise FleetError("resume fleet manifest does not match saved fleet state")
            for edge_id, edge in self.edges.items():
                saved = saved_edges[edge_id]
                if (saved.get("ssh_target"), saved.get("site_config")) != (edge["ssh_target"], edge["site_config"]):
                    raise FleetError(f"resume target or site config changed for edge {edge_id}")
            return state

        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        if self.state_path.exists():
            raise FleetError(f"fleet state already exists; use resume or a new output directory: {self.state_path}")
        return {
            "schema_version": STATE_SCHEMA,
            "version": self.version,
            "created_at": now(),
            "updated_at": now(),
            "output_dir": str(self.output_dir),
            "source_commit": self.source_commit,
            "edges": {
                edge_id: {
                    **edge,
                    "status": "pending",
                    "group_id": None,
                    "inventory_path": None,
                    "last_error": None,
                    "updated_at": now(),
                }
                for edge_id, edge in self.edges.items()
            },
            "groups": {},
        }

    def save(self) -> None:
        with self.lock:
            self.state["updated_at"] = now()
            atomic_write_json(self.state_path, self.state)

    def edge_dir(self, edge_id: str) -> Path:
        return self.state_dir / "edges" / edge_id

    def edge_log(self, edge_id: str, stage: str) -> Path:
        return self.edge_dir(edge_id) / "logs" / f"{stage}.log"

    def update_edge(self, edge_id: str, **updates: Any) -> None:
        with self.lock:
            self.state["edges"][edge_id].update(updates, updated_at=now())
            atomic_write_json(self.state_path, self.state)

    def fail_edge(self, edge_id: str, stage: str, message: str) -> None:
        self.update_edge(edge_id, status=f"{stage}_failed", last_error=message)

    def sudo_invocation(self, *arguments: str) -> tuple[str, str | None]:
        if self.sudo_password is None:
            command = ["sudo", "-n", *arguments]
            return shlex.join(command), None
        command = ["sudo", "-S", "-p", "", *arguments]
        return shlex.join(command), f"{self.sudo_password}\n"

    def probe_one(self, edge_id: str) -> bool:
        edge = self.edges[edge_id]
        entry = self.state["edges"][edge_id]
        inventory_path = Path(entry["inventory_path"]) if entry.get("inventory_path") else self.edge_dir(edge_id) / "inventory.json"
        if self.resume and entry.get("inventory_path") and inventory_path.is_file() and entry.get("status") not in {"probe_failed", "pending"}:
            return True
        inventory_path.parent.mkdir(parents=True, exist_ok=True)
        command = [str(OPERATIONS), "probe-edge-hardware", "--ssh", edge["ssh_target"], "--output", str(inventory_path)]
        input_text = None
        if self.sudo_password is not None:
            command.append("--sudo-stdin")
            input_text = f"{self.sudo_password}\n"
        result = run_logged(
            command,
            self.edge_log(edge_id, "probe"),
            timeout=300,
            input_text=input_text,
        )
        if result.returncode != 0:
            self.fail_edge(edge_id, "probe", f"probe command exited {result.returncode}")
            return False
        sidecar = Path(f"{inventory_path}.sha256")
        if not inventory_path.is_file() or not sidecar.is_file():
            self.fail_edge(edge_id, "probe", "probe did not produce inventory and checksum")
            return False
        try:
            inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
            group_id = compatibility_key(inventory)
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            self.fail_edge(edge_id, "probe", f"invalid inventory: {exc}")
            return False
        self.update_edge(edge_id, status="grouped", inventory_path=str(inventory_path), group_id=group_id, last_error=None)
        return True

    def probe_all(self) -> None:
        todo = [edge_id for edge_id in self.edges if self.state["edges"][edge_id].get("status") != "verified"]
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            futures = {pool.submit(self.probe_one, edge_id): edge_id for edge_id in todo}
            for future in as_completed(futures):
                edge_id = futures[future]
                try:
                    future.result()
                except Exception as exc:  # keep one broken worker from stopping the fleet
                    self.fail_edge(edge_id, "probe", str(exc))

    def build_groups(self) -> None:
        grouped: dict[str, list[str]] = {}
        for edge_id, entry in self.state["edges"].items():
            if entry.get("group_id") and entry.get("status") not in {"probe_failed"}:
                grouped.setdefault(entry["group_id"], []).append(edge_id)

        for group_id, edge_ids in sorted(grouped.items()):
            group = self.state["groups"].setdefault(group_id, {
                "group_id": group_id,
                "edge_ids": sorted(edge_ids),
                "status": "pending",
                "bundle_directory": None,
                "inventory_path": None,
                "last_error": None,
            })
            group["edge_ids"] = sorted(edge_ids)
            inventory_path = Path(self.state["edges"][edge_ids[0]]["inventory_path"])
            group["inventory_path"] = str(inventory_path)
            variant = "metis" if json.loads(inventory_path.read_text(encoding="utf-8")).get("axelera_present") else "intel-only"
            bundle_name = f"tvt-edge-release-{self.version}-intel-285h-{variant}"
            group_root = self.output_dir / "groups" / group_id
            bundle_directory = group_root / bundle_name
            group["bundle_directory"] = str(bundle_directory)
            self.save()

            if bundle_directory.is_dir() and (bundle_directory / "checksums.sha256").is_file():
                group["status"] = "bundle_ready"
                self.save()
                continue

            input_directory = group_root / "inputs"
            command = [
                str(RELEASE_BUILDER),
                "--input-directory", str(input_directory),
                "--output-directory", str(bundle_directory),
                "--edge-inventory", str(inventory_path),
                "--version", self.version,
                "--source-commit", self.source_commit,
            ]
            if self.skip_tests:
                command.append("--skip-tests")
            if self.allow_dirty_source:
                command.append("--allow-dirty-source")
            result = run_logged(command, self.state_dir / "groups" / group_id / "build.log", timeout=7200)
            if result.returncode == 0 and bundle_directory.is_dir():
                group["status"] = "bundle_ready"
                group["last_error"] = None
            else:
                group["status"] = "bundle_failed"
                group["last_error"] = f"bundle build exited {result.returncode}"
            self.save()

    def remote_paths(self, edge_id: str, bundle: Path) -> tuple[str, str]:
        run_name = f"{self.version}-{self.state['created_at'].replace(':', '').replace('+00:00', 'Z')}"
        remote_root = f"/var/tmp/tvt-edge-fleet/{run_name}/{edge_id}"
        return f"{remote_root}/{bundle.name}", f"{remote_root}/site-config.yaml"

    def wait_for_ssh(self, target: str, edge_id: str) -> bool:
        deadline = time.monotonic() + DEFAULT_REBOOT_TIMEOUT
        log = self.edge_log(edge_id, "reboot-wait")
        # Do not mistake an SSH connection that survived the reboot request
        # for a completed reboot.  Require one observed disconnect first.
        time.sleep(3)
        saw_disconnect = False
        while time.monotonic() < deadline:
            result = run_logged(ssh_command(target, "true"), log, timeout=30)
            if result.returncode != 0:
                saw_disconnect = True
            elif saw_disconnect:
                return True
            time.sleep(10)
        return False

    def remote_prepare_state(self, edge: dict[str, Any], edge_id: str) -> dict[str, Any] | None:
        remote, input_text = self.sudo_invocation(
            "cat", "/var/lib/tvt/install/prepare-state.json"
        )
        result = capture_logged(
            ssh_command(edge["ssh_target"], remote),
            self.edge_log(edge_id, "prepare"), timeout=60,
            input_text=input_text)
        if result.returncode != 0:
            return None
        try:
            document = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None
        return document if isinstance(document, dict) else None

    def reboot_edge(self, edge: dict[str, Any], edge_id: str) -> bool:
        self.update_edge(edge_id, status="reboot_required")
        remote, input_text = self.sudo_invocation("systemctl", "reboot")
        reboot = run_logged(
            ssh_command(edge["ssh_target"], remote),
            self.edge_log(edge_id, "reboot"), timeout=60,
            input_text=input_text)
        if reboot.returncode != 0:
            # SSH commonly closes with a non-zero result as reboot starts.
            time.sleep(5)
        if not self.wait_for_ssh(edge["ssh_target"], edge_id):
            self.fail_edge(edge_id, "reboot", "edge did not return within reboot timeout")
            return False
        return True

    def deploy_one(self, edge_id: str) -> bool:
        edge = self.edges[edge_id]
        entry = self.state["edges"][edge_id]
        group_id = entry.get("group_id")
        group = self.state["groups"].get(group_id or "")
        if not group or group.get("status") != "bundle_ready":
            self.fail_edge(edge_id, "bundle", f"compatibility group {group_id or 'unknown'} has no ready bundle")
            return False
        bundle = Path(group["bundle_directory"])
        remote_bundle, remote_site = self.remote_paths(edge_id, bundle)
        remote_root = str(Path(remote_bundle).parent)

        if entry.get("status") in {"verified"}:
            return True
        self.update_edge(edge_id, status="transferring", last_error=None)
        mkdir = run_logged(ssh_command(edge["ssh_target"], f"mkdir -p {shlex.quote(remote_root)}"), self.edge_log(edge_id, "transfer"), timeout=60)
        if mkdir.returncode != 0:
            self.fail_edge(edge_id, "transfer", f"remote staging directory failed ({mkdir.returncode})")
            return False
        transfer = run_logged(rsync_command(bundle, edge["ssh_target"], remote_bundle), self.edge_log(edge_id, "transfer"), timeout=1800)
        if transfer.returncode != 0:
            self.fail_edge(edge_id, "transfer", f"bundle transfer exited {transfer.returncode}")
            return False
        site = run_logged(rsync_file_command(Path(edge["site_config"]), edge["ssh_target"], remote_site), self.edge_log(edge_id, "transfer"), timeout=300)
        if site.returncode != 0:
            self.fail_edge(edge_id, "transfer", f"site config transfer exited {site.returncode}")
            return False
        verify = run_logged(
            ssh_command(edge["ssh_target"], f"cd {shlex.quote(remote_bundle)} && sha256sum --check checksums.sha256"),
            self.edge_log(edge_id, "transfer"), timeout=300)
        if verify.returncode != 0:
            self.fail_edge(edge_id, "transfer", f"remote bundle checksum verification exited {verify.returncode}")
            return False
        self.update_edge(edge_id, status="transferred", remote_bundle=remote_bundle, remote_site_config=remote_site)

        prepare_state = self.remote_prepare_state(edge, edge_id)
        if prepare_state and prepare_state.get("status") == "reboot_required":
            if not self.reboot_edge(edge, edge_id):
                return False
            prepare_state = None
        if not prepare_state:
            remote, input_text = self.sudo_invocation(
                remote_bundle + "/prepare-tvt-edge-host.sh",
                "--bundle", remote_bundle,
                "--mode", "offline",
            )
            prepare = run_logged(
                ssh_command(edge["ssh_target"], remote),
                self.edge_log(edge_id, "prepare"), timeout=3600,
                input_text=input_text)
            if prepare.returncode != 0:
                self.fail_edge(edge_id, "prepare", f"remote preparation exited {prepare.returncode}")
                return False
            prepare_state = self.remote_prepare_state(edge, edge_id)
        if not prepare_state:
            self.fail_edge(edge_id, "prepare", "could not read or parse remote preparation state")
            return False
        if prepare_state.get("status") == "reboot_required":
            if not self.reboot_edge(edge, edge_id):
                return False
        elif prepare_state.get("status") != "prepared":
            self.fail_edge(edge_id, "prepare", f"unexpected preparation state: {prepare_state.get('status')!r}")
            return False

        self.update_edge(edge_id, status="installing")
        install_arguments = [
            remote_bundle + "/install-tvt-edge-host.sh",
            "--bundle", remote_bundle,
            "--site-config", remote_site,
            "--prepare-mode", "offline",
        ]
        if self.resume:
            install_arguments.append("--resume")
        remote, input_text = self.sudo_invocation(*install_arguments)
        install = run_logged(
            ssh_command(edge["ssh_target"], remote),
            self.edge_log(edge_id, "install"), timeout=7200,
            input_text=input_text)
        if install.returncode != 0:
            self.fail_edge(edge_id, "install", f"remote installation exited {install.returncode}")
            return False
        remote, input_text = self.sudo_invocation(
            remote_bundle + "/install-tvt-edge-host.sh",
            "--bundle", remote_bundle,
            "--verify-only",
        )
        verify = run_logged(
            ssh_command(edge["ssh_target"], remote),
            self.edge_log(edge_id, "verify"), timeout=1800,
            input_text=input_text)
        if verify.returncode != 0:
            self.fail_edge(edge_id, "verify", f"remote verification exited {verify.returncode}")
            return False
        remote, input_text = self.sudo_invocation(
            "cat", "/var/lib/tvt/install/installation-report.json"
        )
        evidence = capture_logged(
            ssh_command(edge["ssh_target"], remote),
            self.edge_log(edge_id, "evidence"), timeout=60,
            input_text=input_text)
        if evidence.returncode != 0:
            self.fail_edge(edge_id, "evidence", f"installation evidence retrieval exited {evidence.returncode}")
            return False
        try:
            installation_report = json.loads(evidence.stdout)
        except json.JSONDecodeError as exc:
            self.fail_edge(edge_id, "evidence", f"installation evidence is invalid JSON: {exc}")
            return False
        if not isinstance(installation_report, dict):
            self.fail_edge(edge_id, "evidence", "installation evidence is not a JSON object")
            return False
        evidence_path = self.edge_dir(edge_id) / "installation-report.json"
        atomic_write_json(evidence_path, installation_report)
        self.update_edge(
            edge_id,
            status="verified",
            installation_report=str(evidence_path),
            last_error=None,
        )
        return True

    def deploy_all(self) -> None:
        todo = [
            edge_id for edge_id, entry in self.state["edges"].items()
            if entry.get("status") != "verified" and entry.get("group_id")
        ]
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            futures = {pool.submit(self.deploy_one, edge_id): edge_id for edge_id in todo}
            for future in as_completed(futures):
                edge_id = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    self.fail_edge(edge_id, "deploy", str(exc))

    def report(self) -> int:
        counts: dict[str, int] = {}
        failures = []
        for edge_id, entry in sorted(self.state["edges"].items()):
            status = entry.get("status", "unknown")
            counts[status] = counts.get(status, 0) + 1
            if status != "verified":
                failures.append({"id": edge_id, "status": status, "error": entry.get("last_error")})
        report = {
            "schema_version": STATE_SCHEMA,
            "version": self.version,
            "generated_at": now(),
            "counts": counts,
            "failures": failures,
            "edges": self.state["edges"],
            "groups": self.state["groups"],
        }
        atomic_write_json(self.report_path, report)
        print(json.dumps({"counts": counts, "failures": failures}, indent=2, sort_keys=True))
        return 0 if not failures else 1

    def run(self) -> int:
        self.save()
        self.probe_all()
        self.build_groups()
        self.deploy_all()
        self.save()
        return self.report()


def git_source_commit() -> str:
    result = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"], text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise FleetError("could not determine source commit; pass --source-commit")
    return result.stdout.strip()


def parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    common.add_argument("--skip-tests", action="store_true", help="skip release-builder tests (not recommended for production)")
    common.add_argument("--allow-dirty-source", action="store_true", help="pass the development-only dirty-source override to the release builder")
    common.add_argument("--source-commit", default=None)
    common.add_argument(
        "--interactive-sudo",
        action="store_true",
        help="prompt once for one edge's sudo password; never stores or logs it",
    )

    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)

    install = sub.add_parser("install", parents=[common], help="probe, build, transfer, and install a fleet")
    install.add_argument("--fleet", required=True, type=Path)
    install.add_argument("--version", required=True)
    install.add_argument("--output-directory", required=True, type=Path)

    resume = sub.add_parser("resume", parents=[common], help="resume a saved fleet run")
    resume.add_argument("--state-directory", required=True, type=Path)
    resume.add_argument("--fleet", required=True, type=Path)

    status = sub.add_parser("status", help="print the saved fleet report")
    status.add_argument("--state-directory", required=True, type=Path)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "status":
        report = args.state_directory / "fleet-report.json"
        if not report.is_file():
            raise FleetError(f"fleet report does not exist: {report}")
        print(report.read_text(encoding="utf-8"), end="")
        return 0

    if args.concurrency < 1 or args.concurrency > 32:
        raise FleetError("--concurrency must be between 1 and 32")
    fleet_path = args.fleet.resolve()
    edges = load_fleet(fleet_path)
    if args.interactive_sudo and (len(edges) != 1 or args.concurrency != 1):
        raise FleetError("--interactive-sudo requires exactly one edge and --concurrency 1")
    sudo_password = None
    if args.interactive_sudo:
        if not sys.stdin.isatty():
            raise FleetError("--interactive-sudo requires an interactive terminal")
        sudo_password = getpass.getpass(
            f"Sudo password for {edges[0]['ssh_target']}: "
        )
        if not sudo_password:
            raise FleetError("sudo password cannot be empty")
    if args.command == "resume":
        state_path = args.state_directory.resolve() / "fleet-state.json"
        if not state_path.is_file():
            raise FleetError(f"fleet state does not exist: {state_path}")
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        version = str(saved["version"])
        output_dir = Path(saved["output_dir"])
        source_commit = str(saved["source_commit"])
        resume = True
    else:
        version = args.version
        output_dir = args.output_directory
        source_commit = args.source_commit or git_source_commit()
        resume = False
    controller = FleetController(
        state_dir=args.state_directory if args.command == "resume" else output_dir / "fleet-state",
        edges=edges,
        version=version,
        output_dir=output_dir,
        concurrency=args.concurrency,
        skip_tests=args.skip_tests,
        allow_dirty_source=args.allow_dirty_source,
        source_commit=source_commit,
        resume=resume,
        sudo_password=sudo_password,
    )
    return controller.run()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except FleetError as exc:
        print(f"tvt-edge-fleet: ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
