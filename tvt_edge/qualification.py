"""Bounded, redacted qualification of the catalog-backed Traffic workload."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import stat
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import jsonschema

from apexfabric.solution_management.catalog import load_delivery_metadata
from tvt_edge.security import redact, redact_text
from tvt_edge.paths import RESOURCE_ROOT


ROOT = RESOURCE_ROOT
DEFAULT_CATALOG_DIRECTORY = (
    ROOT / "solution-packs/catalog/traffic-edge-runtime-2026.08.21-v4"
)
DEFAULT_REPORT_DIRECTORY = Path("/var/lib/tvt/qualification")
DEFAULT_IMAGE_LOCK = Path("/var/lib/tvt/pipeline/traffic-image.lock.json")
CATALOG_ID = "traffic-edge-runtime:2026.08.21-v4"
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
MAX_HTTP_BYTES = 2 * 1024 * 1024
MAX_COMMAND_BYTES = 4 * 1024 * 1024
MAX_INPUT_BYTES = 2 * 1024 * 1024
REQUIRED_SERVICES = (
    "postgresql.service",
    "docker.service",
    "tvt-local-registry.service",
    "k3s.service",
    "tvt-edge.service",
    "tvt-camera-sync.service",
    "tvt-pipeline-image-sync.timer",
)
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


class Api(Protocol):
    def get(self, path: str) -> Any: ...

    def post(self, path: str, body: dict[str, Any]) -> Any: ...


class Commands(Protocol):
    def run(
        self, arguments: list[str], *, timeout: int = 10, check: bool = True
    ) -> subprocess.CompletedProcess[str]: ...


class LocalApiClient:
    """Small loopback-only client for exercising the real management API."""

    def __init__(self, base_url: str = "http://127.0.0.1:8088", timeout: int = 10):
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "::1"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("qualification API URL must be an uncredentialed loopback HTTP URL")
        self.base_url = base_url.rstrip("/")
        self.timeout = max(1, min(timeout, 60))

    def _request(self, path: str, body: dict[str, Any] | None = None) -> Any:
        if not path.startswith("/api/v1/"):
            raise ValueError("qualification may call only the public v1 management API")
        payload = None
        headers = {"Accept": "application/json"}
        method = "GET"
        if body is not None:
            payload = json.dumps(body, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
            method = "POST"
        request = Request(
            self.base_url + path,
            data=payload,
            headers=headers,
            method=method,
        )
        with urlopen(request, timeout=self.timeout) as response:
            content = response.read(MAX_HTTP_BYTES + 1)
        if len(content) > MAX_HTTP_BYTES:
            raise ValueError("management API response exceeds the qualification limit")
        value = json.loads(content)
        return value

    def get(self, path: str) -> Any:
        return self._request(path)

    def post(self, path: str, body: dict[str, Any]) -> Any:
        return self._request(path, body)


class CommandRunner:
    def run(
        self, arguments: list[str], *, timeout: int = 10, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            arguments,
            text=True,
            capture_output=True,
            timeout=max(1, min(timeout, 3600)),
            check=check,
        )
        if len(result.stdout.encode()) > MAX_COMMAND_BYTES:
            raise ValueError("command response exceeds the qualification limit")
        return result


@dataclass(frozen=True)
class QualificationOptions:
    deployment_id: str
    namespace: str = "apexfabric"
    checkpoint: str = "steady"
    baseline: dict[str, Any] | None = None
    strict_events: bool = False
    event_timeout: int = 7
    wait_seconds: int = 300
    deployment_request: dict[str, Any] | None = None
    commit_preview: bool = False
    idempotency_key: str | None = None
    rollback_bundle_sha256: str | None = None


def _json_command(
    runner: Commands, arguments: list[str], *, timeout: int = 10
) -> dict[str, Any]:
    result = runner.run(arguments, timeout=timeout)
    if len(result.stdout.encode()) > MAX_COMMAND_BYTES:
        raise ValueError("Kubernetes response exceeds the qualification limit")
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise ValueError("Kubernetes response must be a JSON object")
    return value


def parse_sse_events(body: str) -> list[dict[str, Any]]:
    """Return JSON data records without retaining heartbeat or event metadata."""

    records: list[dict[str, Any]] = []
    data_lines: list[str] = []
    for line in body.splitlines() + [""]:
        if not line:
            if data_lines:
                value = json.loads("\n".join(data_lines))
                if not isinstance(value, dict):
                    raise ValueError("SSE analytics data must be a JSON object")
                records.append(value)
                data_lines = []
            continue
        if line.startswith("data:"):
            data_lines.append(line.removeprefix("data:").lstrip())
    return records


def validate_json_contract(
    validator: jsonschema.Draft202012Validator,
    instance: Any,
    label: str,
) -> None:
    """Validate without allowing instance values into an evidence error."""

    try:
        validator.validate(instance)
    except jsonschema.ValidationError as error:
        path = ".".join(str(part) for part in error.absolute_path) or "<root>"
        raise ValueError(f"{label} violates the pinned schema at {path}") from None


def _contains_secret(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in {
                "authorization",
                "camera_sources",
                "credential",
                "credentials",
                "password",
                "secret",
                "stringdata",
                "token",
                "username",
            }:
                return True
            if _contains_secret(item):
                return True
    elif isinstance(value, list):
        return any(_contains_secret(item) for item in value)
    elif isinstance(value, str):
        return bool(re.search(r"rtsps?://", value, re.IGNORECASE))
    return False


def _is_passing_baseline(report: dict[str, Any]) -> bool:
    checks = report.get("checks")
    if not isinstance(checks, list) or not checks:
        return False
    identifiers = [item.get("id") for item in checks if isinstance(item, dict)]
    statuses = [item.get("status") for item in checks if isinstance(item, dict)]
    expected_summary = {
        "passed": sum(status == "passed" for status in statuses),
        "failed": 0,
        "skipped": sum(status == "skipped" for status in statuses),
    }
    return bool(
        report.get("format_version") == 1
        and report.get("qualification") == "traffic-edge-runtime-v4"
        and report.get("outcome") == "passed"
        and isinstance(report.get("invariants"), dict)
        and len(identifiers) == len(checks)
        and len(set(identifiers)) == len(identifiers)
        and REQUIRED_CHECK_IDS <= set(identifiers)
        and all(status in {"passed", "skipped"} for status in statuses)
        and report.get("summary") == expected_summary
        and not _contains_secret(report)
    )


def atomic_write_report(path: Path, report: dict[str, Any]) -> None:
    """Write a mode-0600 report without following a target symlink."""

    parent = path.parent
    if parent.is_symlink() or path.is_symlink():
        raise ValueError("qualification report paths must not be symlinks")
    parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    temporary = parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(redact(report), stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


class TrafficQualifier:
    def __init__(
        self,
        api: Api,
        commands: Commands,
        catalog_directory: Path = DEFAULT_CATALOG_DIRECTORY,
        *,
        path_exists: Callable[[Path], bool] = Path.exists,
        cpuinfo_reader: Callable[[], str] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        image_lock_path: Path = DEFAULT_IMAGE_LOCK,
        image_lock_reader: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.api = api
        self.commands = commands
        self.catalog_directory = catalog_directory
        self.path_exists = path_exists
        self.cpuinfo_reader = cpuinfo_reader or (
            lambda: Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace")
        )
        self.clock = clock
        self.sleeper = sleeper
        self.image_lock_path = image_lock_path
        self.image_lock_reader = image_lock_reader or (
            lambda: _load_object(self.image_lock_path)
        )
        self.checks: list[dict[str, Any]] = []

    def _check(
        self,
        identifier: str,
        passed: bool,
        summary: str,
        evidence: dict[str, Any] | None = None,
        *,
        skipped: bool = False,
    ) -> None:
        status = "skipped" if skipped else "passed" if passed else "failed"
        item: dict[str, Any] = {
            "id": identifier,
            "status": status,
            "summary": redact_text(summary, 300),
        }
        if evidence:
            item["evidence"] = redact(evidence)
        self.checks.append(item)

    def _failure(self, identifier: str, error: Exception) -> None:
        self._check(identifier, False, f"{type(error).__name__}: {redact_text(str(error), 240)}")

    def _prepare_requested_change(self, options: QualificationOptions) -> str | None:
        request = options.deployment_request
        if request is None:
            if options.commit_preview:
                raise ValueError("--commit-preview requires --deployment-request")
            return None
        if _contains_secret(request):
            raise ValueError("deployment qualification request contains secret material")
        if request.get("deployment_id") != options.deployment_id:
            raise ValueError("deployment request ID does not match the qualified deployment")
        preview = self.api.post("/api/v1/deployments/preview", request)
        if not isinstance(preview, dict) or not DIGEST.search(
            str(preview.get("image_reference", ""))
        ):
            raise ValueError("deployment preview has no immutable image reference")
        if _contains_secret(preview):
            raise ValueError("deployment preview contains secret material")
        preview_sha = preview.get("bundle_sha256")
        if not isinstance(preview_sha, str) or not re.fullmatch(
            r"[0-9a-f]{64}", preview_sha
        ):
            raise ValueError("deployment preview has an invalid bundle digest")
        self._check(
            "deployment.preview",
            True,
            "Public API returned a safe immutable preview",
            {"bundle_sha256": preview_sha},
        )
        if options.commit_preview:
            body = {
                **request,
                "preview_bundle_sha256": preview_sha,
                "idempotency_key": options.idempotency_key
                or f"qualification:{options.deployment_id}:{preview_sha}",
            }
            committed = self.api.post("/api/v1/deployments", body)
            if not isinstance(committed, dict) or committed.get("state") != "pending":
                raise ValueError("deployment commit was not queued")
            self._check(
                "deployment.commit",
                True,
                "The exact preview was committed through the public API",
                {"desired_revision": committed.get("desired_revision")},
            )
        return preview_sha

    def _wait_for_deployment(self, options: QualificationOptions) -> dict[str, Any]:
        deadline = self.clock() + max(0, min(options.wait_seconds, 3600))
        last: dict[str, Any] | None = None
        while True:
            deployments = self.api.get("/api/v1/deployments")
            if not isinstance(deployments, list):
                raise ValueError("deployment API response is invalid")
            last = next(
                (
                    item
                    for item in deployments
                    if isinstance(item, dict)
                    and item.get("deployment_id") == options.deployment_id
                ),
                None,
            )
            if last is not None and last.get("sync_state") == "applied":
                return last
            if self.clock() >= deadline:
                if last is None:
                    raise ValueError("qualified deployment does not exist")
                raise ValueError(
                    f"deployment did not become applied (state={last.get('sync_state')})"
                )
            self.sleeper(2)

    def qualify(self, options: QualificationOptions) -> dict[str, Any]:
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,61}[a-z0-9])?", options.deployment_id):
            raise ValueError("deployment ID must be DNS compatible")
        if options.namespace != "apexfabric":
            raise ValueError("Traffic qualification is restricted to the apexfabric namespace")
        if options.checkpoint not in {
            "steady",
            "pre-reboot",
            "post-reboot",
            "post-rollback",
        }:
            raise ValueError("unsupported qualification checkpoint")
        if options.checkpoint.startswith("post-") and options.baseline is None:
            raise ValueError("post-reboot and post-rollback checks require a baseline report")
        if options.baseline is not None:
            if not _is_passing_baseline(options.baseline):
                raise ValueError("baseline must be a passing safe Traffic qualification report")
        if options.rollback_bundle_sha256 is not None and options.checkpoint != "post-rollback":
            raise ValueError("rollback requests require --checkpoint post-rollback")
        if options.rollback_bundle_sha256 is not None and not re.fullmatch(
            r"[0-9a-f]{64}", options.rollback_bundle_sha256
        ):
            raise ValueError("rollback bundle digest is invalid")
        if options.checkpoint == "post-rollback":
            if options.rollback_bundle_sha256 is None:
                raise ValueError("post-rollback qualification requires a rollback bundle digest")
            baseline_bundle = (options.baseline or {}).get("invariants", {}).get(
                "bundle_sha256"
            )
            if options.rollback_bundle_sha256 != baseline_bundle:
                raise ValueError("rollback digest must exactly match the baseline bundle")
        if options.commit_preview and options.rollback_bundle_sha256 is not None:
            raise ValueError("deployment commit and rollback cannot run together")
        self.checks = []
        started = datetime.now(timezone.utc)

        try:
            metadata = load_delivery_metadata(self.catalog_directory)
            valid_metadata = metadata["catalog_id"] == CATALOG_ID
            self._check(
                "contracts.provenance",
                valid_metadata,
                "Pinned Traffic schemas and examples match provenance",
                {
                    "catalog_id": metadata["catalog_id"],
                    "checksums": metadata["checksums"],
                },
            )
        except Exception as error:
            self._failure("contracts.provenance", error)
            metadata = None

        try:
            architecture = self.commands.run(
                ["dpkg", "--print-architecture"], timeout=5
            ).stdout.strip()
            cpuinfo = self.cpuinfo_reader()
            correct_host = architecture == "amd64" and bool(
                re.search(r"Intel.*Core.*Ultra.*285H", cpuinfo, re.IGNORECASE)
            )
            self._check(
                "host.platform",
                correct_host,
                "Host matches the qualified Linux amd64 Intel 285H profile",
                {"architecture": architecture, "machine": platform.machine()},
            )
        except Exception as error:
            self._failure("host.platform", error)

        devices = {
            path: self.path_exists(Path(path)) for path in ("/dev/dri", "/dev/accel")
        }
        self._check(
            "host.devices",
            all(devices.values()),
            "Intel GPU and NPU device paths are available",
            devices,
        )

        try:
            if self.path_exists(Path("/var/lib/tvt/hardware-driver-reboot-required")):
                raise ValueError("hardware-driver reboot-required marker is still present")
            self.commands.run(
                ["vainfo", "--display", "drm", "--device", "/dev/dri/renderD128"],
                timeout=20,
            )
            self.commands.run(["clinfo", "-l"], timeout=20)
            self.commands.run(
                [
                    "/opt/apexfabric/openvino-env/bin/python",
                    "-c",
                    (
                        "from openvino import Core; d=set(Core().available_devices); "
                        "assert {'CPU','GPU','NPU'} <= d"
                    ),
                ],
                timeout=30,
            )
            self._check(
                "host.accelerators",
                True,
                "VA-API, OpenCL, and OpenVINO expose the qualified accelerators",
            )
        except Exception as error:
            self._failure("host.accelerators", error)

        for service in REQUIRED_SERVICES:
            try:
                self.commands.run(
                    ["systemctl", "is-active", "--quiet", service], timeout=5
                )
                self._check(
                    f"service.{service.removesuffix('.service').removesuffix('.timer')}",
                    True,
                    f"{service} is active",
                )
            except Exception as error:
                self._failure(
                    f"service.{service.removesuffix('.service').removesuffix('.timer')}",
                    error,
                )

        try:
            self.commands.run(
                [
                    "curl",
                    "--fail",
                    "--silent",
                    "--show-error",
                    "--max-time",
                    "5",
                    "http://127.0.0.1:5000/v2/",
                ],
                timeout=10,
            )
            self._check(
                "registry.api",
                True,
                "The loopback-only OCI registry API is reachable",
            )
        except Exception as error:
            self._failure("registry.api", error)

        try:
            self._prepare_requested_change(options)
        except Exception as error:
            self._failure("deployment.preview_commit", error)

        if options.rollback_bundle_sha256 is not None:
            try:
                if not re.fullmatch(r"[0-9a-f]{64}", options.rollback_bundle_sha256):
                    raise ValueError("rollback bundle digest is invalid")
                result = self.api.post(
                    f"/api/v1/deployments/{options.deployment_id}/rollback",
                    {"bundle_sha256": options.rollback_bundle_sha256},
                )
                if not isinstance(result, dict) or result.get("state") != "pending":
                    raise ValueError("rollback was not queued")
                self._check(
                    "rollback.request",
                    True,
                    "Explicit rollback was queued through the management API",
                    {"desired_revision": result.get("desired_revision")},
                )
            except Exception as error:
                self._failure("rollback.request", error)

        try:
            deployment = self._wait_for_deployment(options)
            self._check(
                "deployment.applied",
                deployment.get("desired_revision") == deployment.get("applied_revision"),
                "Desired and applied database revisions match",
                {
                    "desired_revision": deployment.get("desired_revision"),
                    "applied_revision": deployment.get("applied_revision"),
                    "bundle_sha256": deployment.get("applied_bundle_sha256"),
                },
            )
        except Exception as error:
            self._failure("deployment.applied", error)
            deployment = None

        try:
            solutions = self.api.get("/api/v1/solutions")
            catalog = next(
                item
                for item in solutions
                if isinstance(item, dict)
                and item.get("catalog_id")
                == (deployment or {}).get("catalog_id", CATALOG_ID)
            )
            image_reference = catalog["image"]["reference"]
            if catalog.get("status") != "available" or not DIGEST.search(
                str(image_reference)
            ):
                raise ValueError("catalog image is not available by digest")
            if (
                deployment
                and deployment.get("applied_image_digest") not in image_reference
            ):
                raise ValueError("catalog and applied image digests disagree")
            self._check(
                "catalog.available",
                True,
                "Applied catalog entry is available by immutable digest",
                {"catalog_id": catalog["catalog_id"], "image_reference": image_reference},
            )
        except Exception as error:
            self._failure("catalog.available", error)
            catalog = None
            image_reference = None

        if image_reference and metadata is not None:
            try:
                image_lock = self.image_lock_reader()
                provenance = metadata["provenance"]
                lock_pipeline = image_lock.get("pipeline", {})
                lock_archive = image_lock.get("archive", {})
                lock_image = image_lock.get("image", {})
                lock_metadata = image_lock.get("metadata", {})
                expected_metadata = {
                    "image_contract_sha256": metadata["checksums"][
                        "image-contract.yaml"
                    ],
                    "desired_state_schema_sha256": metadata["checksums"][
                        "desired-state.schema.json"
                    ],
                    "metrics_schema_sha256": metadata["checksums"][
                        "metrics.schema.json"
                    ],
                    "analytics_event_schema_sha256": metadata["checksums"][
                        "analytics-event.schema.json"
                    ],
                    "analytics_event_example_sha256": metadata["checksums"][
                        "analytics-event.example.json"
                    ],
                }
                lock_ok = bool(
                    image_lock.get("format_version") == 2
                    and image_lock.get("catalog_id") == CATALOG_ID
                    and lock_pipeline.get("repository")
                    == provenance["pipeline"]["repository"]
                    and lock_pipeline.get("commit")
                    == provenance["pipeline"]["commit"]
                    and lock_pipeline.get("delivery_directory")
                    == provenance["delivery"]["directory"]
                    and lock_archive.get("filename")
                    == provenance["archive"]["filename"]
                    and lock_archive.get("sha256") == provenance["archive"]["sha256"]
                    and lock_archive.get("size") == provenance["archive"]["size"]
                    and image_lock.get("source", {}).get("mode") == "archive"
                    and lock_image.get("reference") == image_reference
                    and lock_image.get("digest")
                    == (deployment or {}).get("applied_image_digest")
                    and lock_image.get("architecture") == "amd64"
                    and lock_metadata == expected_metadata
                )
                self._check(
                    "image_lock.immutable",
                    lock_ok,
                    "The imported image lock binds the archive, image, and runtime contracts",
                    {
                        "path": str(self.image_lock_path),
                        "contract_count": len(expected_metadata),
                    },
                )
            except Exception as error:
                self._failure("image_lock.immutable", error)

        if image_reference:
            try:
                self.commands.run(
                    ["k3s", "crictl", "inspecti", image_reference], timeout=30
                )
                self._check(
                    "containerd.image",
                    True,
                    "K3s containerd has the immutable Traffic image",
                    {"image_reference": image_reference},
                )
            except Exception as error:
                self._failure("containerd.image", error)

        try:
            cluster = self.api.get("/api/v1/cluster")
            nodes = cluster.get("nodes", {}).get("items", [])
            node = nodes[0] if len(nodes) == 1 else None
            node_ok = bool(
                node
                and node.get("ready")
                and node.get("qualified")
                and node.get("architecture") == "amd64"
                and node.get("hardware_profile") == "intel-285h"
            )
            self._check(
                "kubernetes.node",
                node_ok,
                "Exactly one qualified Intel 285H K3s node is Ready",
                {"node": node.get("name") if node else None},
            )
        except Exception as error:
            self._failure("kubernetes.node", error)

        workload_name = f"{options.deployment_id}-runtime"
        deployment_resource: dict[str, Any] | None = None
        pod: dict[str, Any] | None = None
        pvc_uid: str | None = None
        resource_stage = "kubernetes.workload_contract"
        try:
            deployment_resource = _json_command(
                self.commands,
                [
                    "k3s",
                    "kubectl",
                    "get",
                    "deployment",
                    workload_name,
                    "-n",
                    options.namespace,
                    "-o",
                    "json",
                    "--request-timeout=5s",
                ],
            )
            pod_spec = deployment_resource["spec"]["template"]["spec"]
            main = pod_spec["containers"][0]
            compiler = pod_spec["initContainers"][0]
            mounts = {item["mountPath"] for item in main.get("volumeMounts", [])}
            compiler_mounts = {
                item["mountPath"] for item in compiler.get("volumeMounts", [])
            }
            volumes = pod_spec.get("volumes", [])
            required_main = {
                "/configs/desired_state.json",
                "/plans",
                "/tmp/apexfabric",
                "/state",
                "/dev/dri",
                "/dev/accel",
            }
            required_compiler = {
                "/configs/desired_state.json",
                "/plans",
                "/tmp/apexfabric",
                "/dev/dri",
                "/dev/accel",
            }
            camera_main = any(
                value.startswith("/run/secrets/apexfabric/") for value in mounts
            )
            camera_compiler = any(
                value.startswith("/run/secrets/apexfabric/")
                for value in compiler_mounts
            )
            no_models = not any(
                value.startswith("/models") for value in mounts | compiler_mounts
            ) and not any("model" in str(item.get("name", "")).lower() for item in volumes)
            image_ok = bool(
                image_reference
                and main.get("image") == image_reference
                and compiler.get("image") == image_reference
            )
            contract_ok = (
                image_ok
                and required_main <= mounts
                and required_compiler <= compiler_mounts
                and camera_main
                and camera_compiler
                and no_models
            )
            self._check(
                "kubernetes.workload_contract",
                contract_ok,
                "Deployment has the immutable two-container Traffic contract",
                {
                    "deployment": workload_name,
                    "same_image": image_ok,
                    "model_mount_absent": no_models,
                },
            )

            resource_stage = "kubernetes.secrets"
            secret_names = sorted(
                item["secret"]["secretName"]
                for item in volumes
                if isinstance(item.get("secret"), dict)
                and item["secret"].get("secretName")
            )
            if len(secret_names) < 2:
                raise ValueError("desired-state and camera-source Secrets are not mounted")
            for secret_name in secret_names:
                self.commands.run(
                    [
                        "k3s",
                        "kubectl",
                        "get",
                        "secret",
                        secret_name,
                        "-n",
                        options.namespace,
                        "-o",
                        "name",
                        "--request-timeout=5s",
                    ],
                    timeout=10,
                )
            self._check(
                "kubernetes.secrets",
                True,
                "Referenced desired-state and camera-source Secrets exist",
                {"count": len(secret_names)},
            )
        except Exception as error:
            self._failure(resource_stage, error)

        try:
            selector = (
                f"apexfabric.com/deployment-id={options.deployment_id},"
                "apexfabric.com/application=runtime"
            )
            pods = _json_command(
                self.commands,
                [
                    "k3s",
                    "kubectl",
                    "get",
                    "pods",
                    "-n",
                    options.namespace,
                    "-l",
                    selector,
                    "-o",
                    "json",
                    "--request-timeout=5s",
                ],
            ).get("items", [])
            pod = next(
                item
                for item in pods
                if item.get("status", {}).get("phase") == "Running"
            )
            status = pod.get("status", {})
            init_status = next(
                item
                for item in status.get("initContainerStatuses", [])
                if item.get("name") == "plan-compiler"
            )
            main_status = next(
                item
                for item in status.get("containerStatuses", [])
                if item.get("name") == "runtime"
            )
            compiler_ok = (
                init_status.get("state", {})
                .get("terminated", {})
                .get("exitCode")
                == 0
            )
            runtime_ok = bool(main_status.get("ready"))
            image_id = str(main_status.get("imageID", ""))
            digest_ok = bool(
                deployment
                and deployment.get("applied_image_digest")
                and deployment["applied_image_digest"] in image_id
            )
            self._check(
                "kubernetes.pod",
                compiler_ok and runtime_ok and digest_ok,
                "Plan compiler completed and digest-pinned runtime is Ready",
                {
                    "pod": pod.get("metadata", {}).get("name"),
                    "compiler_exit_code": init_status.get("state", {})
                    .get("terminated", {})
                    .get("exitCode"),
                    "runtime_ready": runtime_ok,
                    "image_digest_matches": digest_ok,
                },
            )
        except Exception as error:
            self._failure("kubernetes.pod", error)

        try:
            pvcs = _json_command(
                self.commands,
                [
                    "k3s",
                    "kubectl",
                    "get",
                    "persistentvolumeclaims",
                    "-n",
                    options.namespace,
                    "-l",
                    f"apexfabric.com/deployment-id={options.deployment_id}",
                    "-o",
                    "json",
                    "--request-timeout=5s",
                ],
            ).get("items", [])
            state_claim = next(
                item
                for item in pvcs
                if item.get("metadata", {}).get("name")
                == f"{workload_name}-state"
            )
            pvc_uid = state_claim.get("metadata", {}).get("uid")
            pvc_ok = state_claim.get("status", {}).get("phase") == "Bound" and bool(
                pvc_uid
            )
            self._check(
                "kubernetes.persistent_state",
                pvc_ok,
                "The retained Traffic state PVC is Bound",
                {
                    "claim": state_claim.get("metadata", {}).get("name"),
                    "uid": pvc_uid,
                },
            )
        except Exception as error:
            self._failure("kubernetes.persistent_state", error)

        try:
            telemetry = self.api.get(
                f"/api/v1/cluster/workloads/{workload_name}/telemetry"
            )
            health = telemetry.get("health") or {}
            readiness = telemetry.get("readiness") or {}
            endpoint_ok = bool(
                telemetry.get("available")
                and health.get("status") == "ok"
                and readiness.get("ready") is True
            )
            self._check(
                "runtime.health",
                endpoint_ok,
                "Traffic health, readiness, and telemetry endpoints are available",
            )
            metrics = json.loads(telemetry.get("metrics") or "")
            if metadata is None:
                raise ValueError("metrics schema is unavailable")
            validate_json_contract(
                jsonschema.Draft202012Validator(metadata["metrics_schema"]),
                metrics,
                "runtime metrics",
            )
            runtime = metrics.get("runtime", {})
            metrics_ok = bool(
                runtime.get("plan_loaded")
                and runtime.get("models_ready")
                and runtime.get("child_running")
                and runtime.get("configured_cameras", 0) > 0
            )
            self._check(
                "runtime.metrics_schema",
                metrics_ok,
                "Runtime metrics match the pinned schema and report a ready plan/models/child",
                {
                    "configured_cameras": runtime.get("configured_cameras"),
                    "revision": runtime.get("revision"),
                },
            )
        except Exception as error:
            self._failure("runtime.metrics_schema", error)

        try:
            if pod is None:
                raise ValueError("no running Traffic Pod is available for SSE")
            pod_name = pod["metadata"]["name"]
            result = self.commands.run(
                [
                    "k3s",
                    "kubectl",
                    "get",
                    "--raw",
                    f"/api/v1/namespaces/{options.namespace}/pods/http:{pod_name}:8080/proxy/events",
                    f"--request-timeout={options.event_timeout}s",
                ],
                timeout=options.event_timeout + 5,
                check=False,
            )
            output = result.stdout[:MAX_COMMAND_BYTES]
            reachable = result.returncode == 0 or bool(output.strip())
            self._check(
                "runtime.events_sse",
                reachable,
                "The bounded SSE request reached the Traffic event endpoint",
            )
            events = parse_sse_events(output)
            if metadata is None:
                raise ValueError("analytics event schema is unavailable")
            validator = jsonschema.Draft202012Validator(
                metadata["analytics_event_schema"],
                format_checker=jsonschema.FormatChecker(),
            )
            for event in events:
                validate_json_contract(validator, event, "analytics event")
            if events:
                self._check(
                    "runtime.analytics_events",
                    True,
                    "Observed analytics events match the pinned schema",
                    {"validated_event_count": len(events)},
                )
            else:
                self._check(
                    "runtime.analytics_events",
                    not options.strict_events,
                    "No analytics event arrived during the bounded observation window",
                    {"validated_event_count": 0},
                    skipped=not options.strict_events,
                )
        except Exception as error:
            self._failure("runtime.analytics_events", error)

        invariants = {
            "deployment_id": options.deployment_id,
            "namespace": options.namespace,
            "catalog_id": (deployment or {}).get("catalog_id"),
            "applied_revision": (deployment or {}).get("applied_revision"),
            "bundle_sha256": (deployment or {}).get("applied_bundle_sha256"),
            "image_digest": (deployment or {}).get("applied_image_digest"),
            "pvc_uid": pvc_uid,
        }
        if options.checkpoint == "pre-reboot":
            self._check(
                "checkpoint.pre-reboot",
                True,
                "Captured the pre-reboot deployment, bundle, image, and PVC baseline",
            )
        elif options.baseline is not None:
            expected = options.baseline.get("invariants", {})
            keys = ("deployment_id", "namespace", "bundle_sha256", "image_digest", "pvc_uid")
            differences = {
                key: {"expected": expected.get(key), "actual": invariants.get(key)}
                for key in keys
                if expected.get(key) != invariants.get(key)
            }
            self._check(
                f"checkpoint.{options.checkpoint}",
                not differences,
                "Deployment, bundle, image, and PVC invariants match the baseline",
                {"difference_keys": sorted(differences)},
            )

        outcome = (
            "passed"
            if all(item["status"] in {"passed", "skipped"} for item in self.checks)
            else "failed"
        )
        return {
            "format_version": 1,
            "qualification": "traffic-edge-runtime-v4",
            "checkpoint": options.checkpoint,
            "outcome": outcome,
            "started_at": started.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "actions": {
                "preview_requested": options.deployment_request is not None,
                "commit_requested": options.commit_preview,
                "rollback_requested": options.rollback_bundle_sha256 is not None,
            },
            "invariants": invariants,
            "summary": {
                "passed": sum(item["status"] == "passed" for item in self.checks),
                "failed": sum(item["status"] == "failed" for item in self.checks),
                "skipped": sum(item["status"] == "skipped" for item in self.checks),
            },
            "checks": self.checks,
        }


def _load_object(path: Path) -> dict[str, Any]:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise ValueError(f"{path.name} must be a regular non-symlink file") from error
    with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
        status = os.fstat(stream.fileno())
        if not stat.S_ISREG(status.st_mode):
            raise ValueError(f"{path.name} must be a regular non-symlink file")
        if stat.S_IMODE(status.st_mode) != 0o600:
            raise ValueError(f"{path.name} must have mode 0600")
        if status.st_size > MAX_INPUT_BYTES:
            raise ValueError(f"{path.name} exceeds the qualification input limit")
        body = stream.read(MAX_INPUT_BYTES + 1)
    if len(body.encode()) > MAX_INPUT_BYTES:
        raise ValueError(f"{path.name} exceeds the qualification input limit")
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Qualify a deployed PIPELINE Traffic v4 workload"
    )
    result.add_argument("deployment_id")
    result.add_argument("--namespace", default="apexfabric")
    result.add_argument("--api-url", default="http://127.0.0.1:8088")
    result.add_argument("--catalog-directory", type=Path, default=DEFAULT_CATALOG_DIRECTORY)
    result.add_argument("--image-lock", type=Path, default=DEFAULT_IMAGE_LOCK)
    result.add_argument("--output", type=Path)
    result.add_argument(
        "--checkpoint",
        choices=("steady", "pre-reboot", "post-reboot", "post-rollback"),
        default="steady",
    )
    result.add_argument("--baseline", type=Path)
    result.add_argument("--deployment-request", type=Path)
    result.add_argument("--commit-preview", action="store_true")
    result.add_argument("--idempotency-key")
    result.add_argument("--rollback-bundle-sha256")
    result.add_argument("--strict-events", action="store_true")
    result.add_argument("--event-timeout", type=int, default=7)
    result.add_argument("--wait-seconds", type=int, default=300)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if not 1 <= args.event_timeout <= 60:
        raise ValueError("--event-timeout must be between 1 and 60 seconds")
    if not 0 <= args.wait_seconds <= 3600:
        raise ValueError("--wait-seconds must be between 0 and 3600 seconds")
    baseline = _load_object(args.baseline) if args.baseline else None
    request = _load_object(args.deployment_request) if args.deployment_request else None
    options = QualificationOptions(
        deployment_id=args.deployment_id,
        namespace=args.namespace,
        checkpoint=args.checkpoint,
        baseline=baseline,
        strict_events=args.strict_events,
        event_timeout=args.event_timeout,
        wait_seconds=args.wait_seconds,
        deployment_request=request,
        commit_preview=args.commit_preview,
        idempotency_key=args.idempotency_key,
        rollback_bundle_sha256=args.rollback_bundle_sha256,
    )
    qualifier = TrafficQualifier(
        LocalApiClient(args.api_url),
        CommandRunner(),
        args.catalog_directory,
        image_lock_path=args.image_lock,
    )
    report = qualifier.qualify(options)
    output = args.output or (
        DEFAULT_REPORT_DIRECTORY
        / f"traffic-{args.deployment_id}-{args.checkpoint}-{int(time.time())}.json"
    )
    atomic_write_report(output, report)
    print(
        json.dumps(
            {
                "outcome": report["outcome"],
                "report": str(output),
                "summary": report["summary"],
            },
            sort_keys=True,
        )
    )
    return 0 if report["outcome"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
