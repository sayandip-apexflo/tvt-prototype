#!/usr/bin/env python3
"""Local-only ApexFabric site controller and guided prototype UI."""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qs, urlparse

import jsonschema
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from apexfabric.solution_management.catalog import SolutionCatalog
from apexfabric.solution_management.renderer import render
from apexfabric.solution_management.validation import validate_bundle
from apexfabric.control_plane.telemetry import TelemetryStore
from apexfabric.control_plane.device_registry import DeviceRegistry
from apexfabric.control_plane.storage_failover import reconcile_local_storage_failover

DEFAULT_STATE_DIR = Path(os.getenv("APEXFABRIC_STATE_DIR", ROOT / ".apexfabric"))
DEPLOYMENT_ID_RE = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,61}[a-z0-9])?")
IMAGE_RE = re.compile(r"[^@\s]+")
TVT_MILLS_APPS = {"face_recognition", "face_enrollment", "anpr"}
CAMERA_INVENTORY_CONFIG_MAP = "apexfabric-camera-inventory"
CAMERA_INVENTORY_SECRET = "apexfabric-camera-sources"
# The "people/vehicles currently in frame" feature was removed from the
# dashboard; drop these event types at ingestion so no telemetry endpoint
# serves them, rather than just hiding them client-side.
SUPPRESSED_TELEMETRY_EVENT_TYPES = {"pedestrian_count_per_frame", "vehicle_count_per_frame"}
TRAFFIC_INFERENCE_MODES = {
    "cpu-compatible": {"VEHICLE_DEVICE": "CPU", "PLATE_DEVICE": "CPU", "OCR_DEVICE": "CPU"},
    "intel-gpu-npu": {"VEHICLE_DEVICE": "GPU", "PLATE_DEVICE": "NPU", "OCR_DEVICE": "MULTI:GPU,NPU"},
}
# One entry per catalog solution_pack generate_traffic_runtime_bundle can build a
# DeploymentBundle for. tvt-mills-pilot is the single combined face_recognition +
# face_enrollment + anpr image that replaced the old two-pack (surveillance/traffic)
# architecture -- see docs/contracts/tvt-mills-v1/README.md. TRAFFIC_INFERENCE_MODES
# and restrict_inference_modes/needs_persistent_volume stay generic (not deleted
# outright) since a future pack may need the traffic-shaped branch again; nothing
# currently sets restrict_inference_modes=True.
PACK_PROFILES = {
    "tvt-mills-pilot": {
        "catalog_name": "tvt-mills-pilot",
        "allowed_apps": TVT_MILLS_APPS,
        "max_streams": 5,
        "default_deployment": "tvt-mills-edge-intel-285h",
        "default_tag": "intel-285h-2026.09.18-v1",
        "default_version": "2026.09.18-v1",
        "schema_directory": "tvt-mills-pilot-2026.09.18-v1",
        "models_root": "/models/tvt-mills",
        "needs_persistent_volume": True,
        "restrict_inference_modes": False,
        "required_camera_fields": ("config",),
    },
}
ALLOWED_PACKAGES = {
    "fake-cv-test": ROOT / "examples" / "bundles" / "fake-cv-test.yaml",
    "fake-cv-jetson": ROOT / "examples" / "bundles" / "fake-cv-jetson.yaml",
    "event-sink-test": ROOT / "examples" / "bundles" / "event-sink-test.yaml",
}
DEMO_DEPLOYMENTS = ("fake-cv-test-pipeline", "fake-cv-jetson-pipeline", "event-sink-test-receiver", "traffic-upgrade-test-pipeline", "prometheus-demo")
DEMO_RESOURCES = {
    "deployment": DEMO_DEPLOYMENTS,
    "service": DEMO_DEPLOYMENTS,
    "configmap": ("fake-cv-test-pipeline-config", "fake-cv-jetson-pipeline-config", "event-sink-test-receiver-config", "traffic-upgrade-test-pipeline-config", "prometheus-demo-config"),
    "secret": ("fake-cv-test-pipeline-secret", "fake-cv-jetson-pipeline-secret", "traffic-upgrade-test-pipeline-secret"),
    "networkpolicy": DEMO_DEPLOYMENTS,
    "pod": ("failure-resource-pending",),
}


class CommandError(RuntimeError):
    pass


class Runner:
    def run(self, command: list[str], timeout: int = 900, check: bool = True, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(command, cwd=ROOT, text=True, input=input_text, capture_output=True, timeout=timeout, check=False)
        if check and result.returncode:
            detail = (result.stderr or result.stdout).strip()
            raise CommandError(f"{' '.join(command)}: {detail or f'exit {result.returncode}'}")
        return result


class Controller:
    def __init__(self, state_dir: Path = DEFAULT_STATE_DIR, runner: Runner | None = None, start_background: bool = False):
        self.state_dir = state_dir
        self.runner = runner or Runner()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.jobs: dict[str, dict[str, Any]] = {}
        self.lock = threading.Lock()
        self.telemetry = TelemetryStore(self.state_dir / "telemetry")
        from .alerts import AlertStore
        self.alerts = AlertStore(self.telemetry)
        from .identity import PersonStore
        self.persons = PersonStore(self.telemetry)
        self.telemetry_stop = threading.Event()
        self.telemetry_targets: set[str] = set()
        self.telemetry_collectors: set[str] = set()
        # Snapshot fetches shell out to `kubectl` per image; running them inline in the
        # SSE readline loop throttles ingestion to that subprocess's latency. Fetching
        # them here instead lets the collector keep draining the stream at line speed.
        self.telemetry_snapshot_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="telemetry-snapshot",
        )
        configured_registry = os.getenv("APEXFABRIC_REGISTRY_ADDRESS")
        registry = (configured_registry or "127.0.0.1:5000").rstrip("/")
        self.catalog = SolutionCatalog(self.state_dir / "catalog.sqlite3")
        self.device_registry = DeviceRegistry(self.state_dir / "device-registry.sqlite3", self.site_id())
        catalog_manifest = os.getenv("APEXFABRIC_CATALOG_MANIFEST")
        if catalog_manifest:
            from apexfabric.solution_management.install_catalog import seed_manifest
            seed_manifest(self.catalog, ROOT, json.loads(Path(catalog_manifest).read_text()), registry)
        else:
            deliveries = (
                ("tvt-mills-pilot-2026.09.18-v1", "tvt-mills-pilot"),
            )
            for directory_name, solution_name in deliveries:
                delivery = ROOT / "solution-packs" / "catalog" / directory_name
                repository = f"apexfabric/{solution_name}"
                if (delivery / "image-contract.yaml").is_file():
                    self.catalog.seed_delivery(delivery, registry, repository)
                self.catalog.inherit_ui_annotations(solution_name)
                self.catalog.retarget(solution_name, registry, repository)
        if configured_registry:
            threading.Thread(target=self.catalog.refresh, name="catalog-refresh", daemon=True).start()
        if start_background:
            threading.Thread(target=self._telemetry_supervisor, name="telemetry-supervisor", daemon=True).start()
            threading.Thread(target=self._storage_failover_supervisor, name="storage-failover-supervisor", daemon=True).start()
            threading.Thread(target=self._session_sweep_supervisor, name="session-sweep-supervisor", daemon=True).start()

    def _storage_failover_supervisor(self) -> None:
        interval = max(10, int(os.getenv("APEXFABRIC_STORAGE_FAILOVER_INTERVAL_SECONDS", "30")))
        while not self.telemetry_stop.wait(interval):
            try:
                actions = reconcile_local_storage_failover(self.kubectl)
                for action in actions:
                    self.audit("storage:failover", "succeeded", action)
            except Exception as error:
                print(f"storage failover reconciliation failed: {error}", file=sys.stderr, flush=True)

    def _session_sweep_supervisor(self) -> None:
        from .reporting import sweep_stale_sessions
        interval = max(60, int(os.getenv("APEXFABRIC_SESSION_SWEEP_INTERVAL_SECONDS", "3600")))
        stale_after = max(60, int(os.getenv("APEXFABRIC_SESSION_STALE_AFTER_SECONDS", str(18 * 60 * 60))))
        while not self.telemetry_stop.wait(interval):
            try:
                with self.telemetry.lock, self.telemetry._connect() as connection:
                    result = sweep_stale_sessions(connection, time.time() - stale_after)
                if result["attendance_forced_closed"] or result["vehicle_forced_closed"]:
                    self.audit("reporting:sweep", "succeeded", result)
            except Exception as error:
                print(f"session sweep failed: {error}", file=sys.stderr, flush=True)
            try:
                self._revert_expired_enrollment_windows()
            except Exception as error:
                print(f"enrollment window sweep failed: {error}", file=sys.stderr, flush=True)

    def _revert_expired_enrollment_windows(self) -> None:
        from .enrollment_windows import expired_windows

        with self.telemetry._connect() as connection:
            expired = expired_windows(connection, time.time())
        for window in expired:
            try:
                self.stop_enrollment({"log": []}, {"name": window["deployment_id"], "window_id": window["id"]})
                self.audit("enrollment:auto-revert", "succeeded", {"window_id": window["id"], "camera_id": window["camera_id"]})
            except Exception as error:
                print(f"failed to auto-revert enrollment window {window['id']}: {error}", file=sys.stderr, flush=True)

    def kubectl(self, *arguments: str, check: bool = True, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
        command = ["k3s", "kubectl", *arguments]
        if input_text is None:
            return self.runner.run(command, check=check)
        return self.runner.run(command, check=check, input_text=input_text)

    def audit(self, action: str, outcome: str, detail: dict[str, Any]) -> None:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(), "action": action,
            "outcome": outcome, "detail": detail,
        }
        with (self.state_dir / "audit.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True) + "\n")

    def submit(self, action: str, function: Any, *args: Any) -> str:
        job_id = uuid.uuid4().hex[:12]
        job = {"id": job_id, "action": action, "state": "queued", "created_at": time.time(), "log": []}
        with self.lock:
            self.jobs[job_id] = job

        def execute() -> None:
            job["state"] = "running"
            job["started_at"] = time.time()
            try:
                job["result"] = function(job, *args)
                self.audit(action, "succeeded", job.get("result", {}))
                job["state"] = "succeeded"
            except Exception as error:  # The API exposes a bounded diagnostic, not a traceback.
                job["state"] = "failed"
                job["error"] = str(error)[-4000:]
                self.audit(action, "failed", {"error": job["error"]})
            finally:
                job["finished_at"] = time.time()

        threading.Thread(target=execute, name=f"job-{job_id}", daemon=True).start()
        return job_id

    def log(self, job: dict[str, Any], message: str) -> None:
        job["log"].append(message[-4000:])

    @staticmethod
    def _dns_id(value: Any, field: str) -> str:
        if not isinstance(value, str) or not DEPLOYMENT_ID_RE.fullmatch(value):
            raise ValueError(f"{field} must be a DNS-style identifier")
        return value

    @staticmethod
    def _positive_int(value: Any, field: str, maximum: int) -> int:
        if isinstance(value, bool):
            raise ValueError(f"{field} must be an integer")
        try:
            parsed = int(value)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{field} must be an integer") from error
        if parsed < 1 or parsed > maximum:
            raise ValueError(f"{field} must be between 1 and {maximum}")
        return parsed

    @staticmethod
    def _bounded_float(value: Any, field: str, minimum: float, maximum: float) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{field} must be a number") from error
        if parsed < minimum or parsed > maximum:
            raise ValueError(f"{field} must be between {minimum} and {maximum}")
        return parsed

    @staticmethod
    def _camera_source(value: Any) -> str:
        parsed = urlparse(value) if isinstance(value, str) else None
        if (not isinstance(value, str) or len(value) > 4096 or any(ord(char) < 32 for char in value)
                or parsed.scheme not in {"rtsp", "rtsps"} or not parsed.hostname):
            raise ValueError("camera source must be a valid RTSP URL")
        return value

    def _camera_inventory_resources(self) -> tuple[list[dict[str, Any]], dict[str, str]]:
        metadata_result = self.kubectl("get", "configmap", CAMERA_INVENTORY_CONFIG_MAP, "-n", "apexfabric", "-o", "json", check=False)
        cameras: list[dict[str, Any]] = []
        if metadata_result.returncode == 0:
            cameras = json.loads(json.loads(metadata_result.stdout).get("data", {}).get("cameras.json", "[]"))
            if not isinstance(cameras, list):
                raise ValueError("camera inventory metadata is invalid")
        source_result = self.kubectl("get", "secret", CAMERA_INVENTORY_SECRET, "-n", "apexfabric", "-o", "json", check=False)
        sources: dict[str, str] = {}
        if source_result.returncode == 0:
            encoded = json.loads(source_result.stdout).get("data", {})
            sources = {key.removesuffix(".rtsp"): base64.b64decode(value).decode("utf-8")
                       for key, value in encoded.items() if key.endswith(".rtsp")}
        return cameras, sources

    def _camera_assignments(self) -> dict[str, list[str]]:
        result = self.kubectl("get", "configmaps", "-n", "apexfabric", "-o", "json", check=False)
        assignments: dict[str, list[str]] = {}
        if result.returncode:
            return assignments
        for item in json.loads(result.stdout).get("items", []):
            raw = item.get("data", {}).get("desired_state.json")
            if not raw:
                continue
            try:
                desired = json.loads(raw)
            except json.JSONDecodeError:
                continue
            deployment_id = item.get("metadata", {}).get("labels", {}).get("apexfabric.com/deployment-id")
            if not deployment_id:
                continue
            for camera in desired.get("cameras", []):
                if isinstance(camera, dict) and isinstance(camera.get("camera_id"), str):
                    assignments.setdefault(camera["camera_id"], []).append(deployment_id)
        return assignments

    def camera_inventory(self) -> dict[str, Any]:
        legacy_cameras, sources = self._camera_inventory_resources()
        stored = self.device_registry.list("camera")
        if not stored and legacy_cameras:
            for camera in legacy_cameras:
                camera_id = camera.get("camera_id")
                name = camera.get("name")
                if isinstance(camera_id, str) and isinstance(name, str):
                    self.device_registry.upsert(camera_id, "camera", name, "configured", {
                        "has_source": camera_id in sources,
                        "migrated_from": CAMERA_INVENTORY_CONFIG_MAP,
                    })
            stored = self.device_registry.list("camera")
        assignments = self._camera_assignments()
        public = []
        for device in stored:
            camera_id = device["device_id"]
            assigned = sorted(set(assignments.get(camera_id, [])))
            public.append({
                "camera_id": camera_id, "name": device["display_name"],
                "created_at": device["created_at"], "updated_at": device["updated_at"],
                "has_source": camera_id in sources, "in_use": bool(assigned), "assigned_to": assigned,
            })
        return {"cameras": public}

    def customer_summary(self) -> dict[str, Any]:
        """Read-only presentation of deployed solutions; never return Secrets or pod specs."""
        response = self.kubectl("get", "deployments", "-n", "apexfabric", "-o", "json")
        deployments = []
        for item in json.loads(response.stdout).get("items", []):
            metadata = item.get("metadata", {})
            labels = metadata.get("labels", {})
            if labels.get("app.kubernetes.io/managed-by") != "apexfabric-node-agent":
                continue
            wanted = item.get("spec", {}).get("replicas", 1)
            ready = item.get("status", {}).get("readyReplicas", 0)
            deployments.append({
                "name": metadata.get("name"),
                "solution": labels.get("apexfabric.com/deployment-id", metadata.get("name")),
                "status": "Stopped" if wanted == 0 else "Running" if ready >= wanted else "Starting or unavailable",
                "ready_replicas": ready, "desired_replicas": wanted,
            })
        return {"site_id": self.site_id(), "cameras": self.camera_inventory()["cameras"], "deployments": deployments}

    def camera_snapshot(self, camera_id_value: Any) -> bytes:
        """Capture one bounded JPEG without exposing the inventory RTSP value."""
        camera_id = self._dns_id(camera_id_value, "camera_id")
        _, sources = self._camera_inventory_resources()
        source = sources.get(camera_id)
        if source is None:
            raise ValueError("camera does not exist or has no configured stream")
        failures: list[str] = []
        for transport in ("tcp", "udp"):
            try:
                result = subprocess.run(
                    ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                     "-rtsp_transport", transport, "-i", source, "-frames:v", "1",
                     "-vf", "scale='min(1280,iw)':-2", "-q:v", "3", "-f", "image2pipe",
                     "-vcodec", "mjpeg", "pipe:1"],
                    cwd=ROOT, capture_output=True, timeout=12, check=False,
                )
            except FileNotFoundError as error:
                raise ValueError("camera snapshots require ffmpeg on the control-plane host") from error
            except subprocess.TimeoutExpired:
                failures.append("timeout")
                continue
            if result.returncode == 0 and result.stdout:
                if len(result.stdout) > 8 * 1024 * 1024:
                    raise ValueError("camera snapshot exceeds the 8 MiB limit")
                return result.stdout
            # Keep diagnostics only in memory for classification. Never return
            # raw ffmpeg output because it can echo credential-bearing URLs.
            failures.append(result.stderr.decode("utf-8", errors="replace").lower())
        combined = "\n".join(failures)
        if any(value in combined for value in ("401 unauthorized", "403 forbidden", "authentication failed")):
            reason = "the camera rejected the configured credentials"
        elif any(value in combined for value in ("no route to host", "network is unreachable")):
            reason = "the camera network is not reachable from the control-plane host"
        elif "connection refused" in combined:
            reason = "the camera refused the RTSP connection"
        elif "timeout" in combined or "timed out" in combined:
            reason = "the camera did not return a frame before the timeout"
        elif any(value in combined for value in ("404 not found", "method describe failed")):
            reason = "the configured RTSP stream path was not found"
        else:
            reason = "the stream could not be decoded"
        raise ValueError(f"unable to capture camera snapshot: {reason}")

    def _apply_camera_inventory(self, cameras: list[dict[str, Any]], sources: dict[str, str]) -> None:
        labels = {"app.kubernetes.io/managed-by": "apexfabric-control-plane"}
        resources = {"apiVersion": "v1", "kind": "List", "items": [
            {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": CAMERA_INVENTORY_CONFIG_MAP, "namespace": "apexfabric", "labels": labels}, "data": {"cameras.json": json.dumps(cameras, separators=(",", ":"))}},
            {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": CAMERA_INVENTORY_SECRET, "namespace": "apexfabric", "labels": labels}, "type": "Opaque", "stringData": {f"{key}.rtsp": value for key, value in sources.items()}},
        ]}
        self.kubectl("apply", "-f", "-", input_text=json.dumps(resources))

    def save_camera(self, request: dict[str, Any]) -> dict[str, Any]:
        name = request.get("name")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            raise ValueError("camera name must contain 1 to 80 characters")
        camera_id_value = request.get("camera_id") or re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")[:63]
        camera_id = self._dns_id(camera_id_value, "camera_id")
        cameras, sources = self._camera_inventory_resources()
        existing = next((camera for camera in cameras if camera.get("camera_id") == camera_id), None)
        source = request.get("rtsp_url")
        if source is not None:
            sources[camera_id] = self._camera_source(source)
        elif camera_id not in sources:
            raise ValueError("rtsp_url is required for a new camera")
        now = datetime.now(timezone.utc).isoformat()
        record = {"camera_id": camera_id, "name": name.strip(), "created_at": existing.get("created_at", now) if existing else now, "updated_at": now}
        cameras = [camera for camera in cameras if camera.get("camera_id") != camera_id] + [record]
        cameras.sort(key=lambda camera: camera["name"].lower())
        self._apply_camera_inventory(cameras, sources)
        self.device_registry.upsert(camera_id, "camera", name.strip(), "configured", {"has_source": True})
        self.audit("camera:save", "succeeded", {"camera_id": camera_id, "name": name.strip()})
        return {**record, "has_source": True, "in_use": False, "assigned_to": []}

    def delete_camera(self, request: dict[str, Any]) -> dict[str, Any]:
        camera_id = self._dns_id(request.get("camera_id"), "camera_id")
        assignments = self._camera_assignments().get(camera_id, [])
        if assignments:
            raise ValueError(f"camera is used by {', '.join(assignments)} and cannot be removed")
        cameras, sources = self._camera_inventory_resources()
        if not any(camera.get("camera_id") == camera_id for camera in cameras):
            raise ValueError("camera does not exist")
        cameras = [camera for camera in cameras if camera.get("camera_id") != camera_id]
        sources.pop(camera_id, None)
        self._apply_camera_inventory(cameras, sources)
        self.device_registry.delete(camera_id, "camera")
        self.audit("camera:delete", "succeeded", {"camera_id": camera_id})
        return {"camera_id": camera_id, "deleted": True}

    def generate_bundle(self, request: dict[str, Any]) -> dict[str, Any]:
        """Convert bounded product intent into a supported Solution Pack."""
        solution_type = request.get("solution_type", "intel-traffic-pilot")
        if solution_type == "tvt-mills-pilot":
            return self.generate_traffic_runtime_bundle(request, solution_pack="tvt-mills-pilot")
        if solution_type == "jetson-event-simulator":
            return self.generate_jetson_event_bundle(request)
        if solution_type != "intel-traffic-pilot":
            raise ValueError("solution_type is unsupported")
        deployment_id = self._dns_id(request.get("deployment_id", "traffic-pilot-people-285h"), "deployment_id")
        solution_id = self._dns_id(request.get("solution_id", "traffic-pilot-people"), "solution_id")
        image_repository = request.get("image_repository", "people-pilot-285h")
        image_tag = request.get("image_tag", "1.0")
        if not isinstance(image_repository, str) or not IMAGE_RE.fullmatch(image_repository):
            raise ValueError("image_repository is invalid")
        if not isinstance(image_tag, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", image_tag):
            raise ValueError("image_tag is invalid")
        version = request.get("version", "1.0.0")
        if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?", version):
            raise ValueError("version must use semantic version syntax")
        camera_ids = request.get("cameras", [])
        if not isinstance(camera_ids, list):
            raise ValueError("cameras must be an array")
        cameras = [self._dns_id(item, "camera") for item in camera_ids]
        if len(set(cameras)) != len(cameras):
            raise ValueError("cameras must be unique")
        storage_gib = self._positive_int(request.get("storage_gib", 2), "storage_gib", 1024)
        cpu_request = self._positive_int(request.get("cpu_request", 2), "cpu_request", 64)
        cpu_limit = self._positive_int(request.get("cpu_limit", 8), "cpu_limit", 64)
        memory_request = self._positive_int(request.get("memory_request_gib", 2), "memory_request_gib", 256)
        memory_limit = self._positive_int(request.get("memory_limit_gib", 8), "memory_limit_gib", 256)
        if cpu_request > cpu_limit or memory_request > memory_limit:
            raise ValueError("resource requests cannot exceed limits")
        confidence = self._bounded_float(request.get("confidence", 0.45), "confidence", 0, 1)
        min_track_hits = self._positive_int(request.get("min_track_hits", 3), "min_track_hits", 100)
        live_fps = self._positive_int(request.get("live_fps", 10), "live_fps", 120)
        pull_policy = request.get("pull_policy", "Never")
        if pull_policy not in {"Never", "IfNotPresent", "Always"}:
            raise ValueError("pull_policy is invalid")

        bundle = {
            "api_version": "apexfabric.com/v1alpha1", "kind": "DeploymentBundle",
            "deployment_id": deployment_id,
            "solution": {"solution_id": solution_id, "version": version},
            "applications": [{
                "name": "pipeline",
                "image": {"repository": image_repository, "tag": image_tag, "pull_policy": pull_policy},
                "replicas": 1,
                "lifecycle": {"desired_state": "Running", "termination_grace_period_seconds": 60},
                "resources": {
                    "cpu": {"request": str(cpu_request), "limit": str(cpu_limit)},
                    "memory": {"request": f"{memory_request}Gi", "limit": f"{memory_limit}Gi"},
                    "accelerators": {"decoder": 0, "metis": 1},
                },
                "cameras": cameras,
                "camera_contract": {
                    "configuration_owner": "application-api", "transport": "rtsp",
                    "required_for_readiness": False, "scheduling_mode": "runtime-connectivity",
                },
                "configuration": {
                    "persistent_data_path": "/data", "persistent_data_size": f"{storage_gib}Gi",
                    "expected_voyager_runtime": "1.6.1", "model_compiler_runtime": "1.6.0",
                },
                "placement": {
                    "runtime_profile": "intel-285h-metis", "node_class": "cv", "architecture": "amd64",
                    "requires_qualified_node": True, "requires_camera_labels": False,
                    "characteristics": {"decoder": "vaapi", "metis": "true"},
                },
                "persistent_volumes": [{
                    "name": "data", "mount_path": "/data", "size": f"{storage_gib}Gi", "storage_class": "local-path",
                }],
                "health": {
                    "startup": {"path": "/readyz", "port": "api", "period_seconds": 5, "timeout_seconds": 4, "failure_threshold": 24},
                    "readiness": {"path": "/readyz", "port": "api", "period_seconds": 10, "timeout_seconds": 4, "failure_threshold": 3},
                    "liveness": {"path": "/healthz", "port": "api", "initial_delay_seconds": 30, "period_seconds": 15, "timeout_seconds": 4, "failure_threshold": 4},
                },
                "ports": [
                    {"name": "ui", "container_port": 5173, "protocol": "TCP"},
                    {"name": "api", "container_port": 8077, "protocol": "TCP"},
                ],
                "telemetry": {
                    "metrics": {"path": "/api/metrics", "port": "api", "format": "json"},
                    "events": {"path": "/api/events/ws", "port": "api", "protocol": "websocket"},
                },
                "environment": {
                    "PEOPLE_CONFIDENCE": format(confidence, "g"),
                    "PEOPLE_MIN_TRACK_HITS": str(min_track_hits),
                    "LIVE_FPS": str(live_fps), "CAMERA_STALE_S": "30",
                },
            }],
            "configuration": {
                "workload_profile": "intel-285h-metis", "source_bundle": "traffic-pilot-people-285h",
                "camera_configuration_owner": "application-api",
            },
        }
        schema = json.loads((ROOT / "solution-packs/schema/deployment-bundle.schema.json").read_text(encoding="utf-8"))
        errors = validate_bundle(bundle, schema)
        if errors:
            raise ValueError("generated bundle is invalid: " + "; ".join(errors))
        objects = render(bundle, "apexfabric")
        return {
            "bundle": bundle,
            "bundle_yaml": yaml.safe_dump(bundle, sort_keys=False),
            "objects": objects,
            "summary": [{"kind": item["kind"], "name": item["metadata"]["name"]} for item in objects],
        }

    def generate_jetson_event_bundle(self, request: dict[str, Any]) -> dict[str, Any]:
        deployment_id = self._dns_id(request.get("deployment_id", "jetson-event-demo"), "deployment_id")
        version = request.get("version", "0.1.0")
        if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?", version):
            raise ValueError("version must use semantic version syntax")
        image_repository = request.get("image_repository", "192.168.10.120:5000/apexfabric/event-simulator")
        image_tag = request.get("image_tag", "0.1.0")
        if not isinstance(image_repository, str) or not IMAGE_RE.fullmatch(image_repository):
            raise ValueError("image_repository is invalid")
        if not isinstance(image_tag, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", image_tag):
            raise ValueError("image_tag is invalid")
        pull_policy = request.get("pull_policy", "Always")
        if pull_policy not in {"Never", "IfNotPresent", "Always"}:
            raise ValueError("pull_policy is invalid")
        cameras = request.get("cameras", ["demo-camera"])
        if not isinstance(cameras, list):
            raise ValueError("cameras must be an array")
        cameras = [self._dns_id(item, "camera") for item in cameras] or ["demo-camera"]
        if len(set(cameras)) != len(cameras):
            raise ValueError("cameras must be unique")
        bundle = {
            "api_version": "apexfabric.com/v1alpha1", "kind": "DeploymentBundle",
            "deployment_id": deployment_id,
            "solution": {"solution_id": "event-simulator", "version": version},
            "applications": [{
                "name": "pipeline",
                "image": {"repository": image_repository, "tag": image_tag, "pull_policy": pull_policy},
                "replicas": 1,
                "lifecycle": {"desired_state": "Running", "termination_grace_period_seconds": 20},
                "resources": {
                    "cpu": {"request": "100m", "limit": "500m"},
                    "memory": {"request": "64Mi", "limit": "256Mi"},
                    "accelerators": {"decoder": 0, "metis": 0},
                },
                "cameras": cameras,
                "camera_contract": {
                    "configuration_owner": "platform", "transport": "mixed",
                    "required_for_readiness": True, "scheduling_mode": "runtime-connectivity",
                },
                "configuration": {
                    "enabled_apps": ["detection", "tracking", "zone_analytics"],
                    "event_interval_seconds": 1,
                },
                "placement": {
                    "runtime_profile": "standard", "node_class": "cv", "architecture": "arm64",
                    "requires_qualified_node": True, "requires_camera_labels": False,
                    "characteristics": {"hardware-profile": "jetson-orin"},
                },
                "health": {
                    "startup": {"path": "/ready", "port": "http", "period_seconds": 2, "timeout_seconds": 1, "failure_threshold": 30},
                    "readiness": {"path": "/ready", "port": "http", "period_seconds": 5, "timeout_seconds": 2, "failure_threshold": 3},
                    "liveness": {"path": "/health", "port": "http", "period_seconds": 10, "timeout_seconds": 2, "failure_threshold": 3},
                },
                "ports": [{"name": "http", "container_port": 8080, "protocol": "TCP"}],
                "telemetry": {"metrics": {"path": "/metrics", "port": "http", "format": "prometheus"}},
                "environment": {"LOG_LEVEL": "INFO"},
                "secrets": {"api-token": "synthetic-demo-token"},
                "secret_environment": {"TRAFFIC_API_TOKEN": "api-token"},
            }],
            "configuration": {"site_id": self.site_id(), "purpose": "jetson-central-plane-lifecycle-demo"},
        }
        schema = json.loads((ROOT / "solution-packs/schema/deployment-bundle.schema.json").read_text(encoding="utf-8"))
        errors = validate_bundle(bundle, schema)
        if errors:
            raise ValueError("generated bundle is invalid: " + "; ".join(errors))
        objects = render(bundle, "apexfabric")
        return {
            "bundle": bundle, "bundle_yaml": yaml.safe_dump(bundle, sort_keys=False), "objects": objects,
            "summary": [{"kind": item["kind"], "name": item["metadata"]["name"]} for item in objects],
        }

    def generate_traffic_runtime_bundle(self, request: dict[str, Any], solution_pack: str = "tvt-mills-pilot") -> dict[str, Any]:
        if solution_pack not in PACK_PROFILES:
            raise ValueError(f"solution_pack is unsupported: {solution_pack!r}")
        profile = PACK_PROFILES[solution_pack]
        catalog_name = profile["catalog_name"]
        allowed_apps = profile["allowed_apps"]
        max_streams = profile["max_streams"]
        default_deployment = profile["default_deployment"]
        default_repository = f"192.168.10.217:5000/apexfabric/{catalog_name}"
        default_tag = profile["default_tag"]
        default_version = profile["default_version"]
        contract_name = f"{solution_pack}-runtime-v1"
        deployment_id = self._dns_id(request.get("deployment_id", default_deployment), "deployment_id")
        if len(deployment_id) > 47:
            raise ValueError("deployment_id is too long for installation-owned Secret names")
        catalog_entry = None
        if request.get("catalog_id"):
            catalog_entry = self.catalog.get(str(request["catalog_id"]))
            if not catalog_entry:
                raise ValueError("catalog_id is unknown")
            if catalog_entry["name"] != catalog_name:
                raise ValueError(f"catalog_id is not a {catalog_name} image")
            if not catalog_entry["digest"]:
                raise ValueError("catalog image digest is unresolved; refresh the catalog after publishing the image")
            schema_apps = (catalog_entry.get("desired_state_schema") or {}).get("properties", {}).get("cameras", {}).get("items", {}).get("properties", {}).get("apps", {}).get("items", {}).get("enum")
            if isinstance(schema_apps, list) and schema_apps:
                allowed_apps = set(schema_apps)
        image_repository = request.get("image_repository", catalog_entry["image"]["repository"] if catalog_entry else default_repository)
        image_tag = request.get("image_tag", catalog_entry["tag"] if catalog_entry else default_tag)
        image_digest = request.get("image_digest", catalog_entry["digest"] if catalog_entry else None)
        if not isinstance(image_repository, str) or not IMAGE_RE.fullmatch(image_repository):
            raise ValueError("image_repository is invalid")
        if not isinstance(image_tag, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", image_tag):
            raise ValueError("image_tag is invalid")
        if image_digest is not None and (not isinstance(image_digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest)):
            raise ValueError("image_digest is invalid")
        version = request.get("version", catalog_entry["version"] if catalog_entry else default_version)
        if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?", version):
            raise ValueError("version must use semantic version syntax")
        pull_policy = request.get("pull_policy", "IfNotPresent")
        if pull_policy not in {"Never", "IfNotPresent", "Always"}:
            raise ValueError("pull_policy is invalid")
        edge_id = self._dns_id(request.get("edge_id", "intel-box-01"), "edge_id")
        desired_revision = self._positive_int(request.get("desired_revision", 1), "desired_revision", 2_147_483_647)
        configured_cameras = request.get("camera_configuration", [])
        if not isinstance(configured_cameras, list) or not configured_cameras:
            raise ValueError("camera_configuration must contain at least one camera")
        if len(configured_cameras) > max_streams:
            raise ValueError(f"camera_configuration exceeds the image contract maximum of {max_streams} streams")

        desired_cameras = []
        camera_ids = []
        for index, configured in enumerate(configured_cameras):
            if not isinstance(configured, dict):
                raise ValueError(f"camera_configuration[{index}] must be an object")
            allowed_fields = {"camera_id", "fps", "apps", "config"}
            unknown = set(configured) - allowed_fields
            if unknown:
                raise ValueError(f"camera_configuration[{index}] has unsupported fields: {sorted(unknown)}")
            camera_id = self._dns_id(configured.get("camera_id"), f"camera_configuration[{index}].camera_id")
            fps = configured.get("fps", 8)
            if isinstance(fps, bool) or not isinstance(fps, (int, float)) or fps <= 0 or fps > 120:
                raise ValueError(f"camera_configuration[{index}].fps must be greater than 0 and at most 120")
            apps = configured.get("apps")
            if not isinstance(apps, list) or not apps or any(app not in allowed_apps for app in apps) or len(set(apps)) != len(apps):
                raise ValueError(f"camera_configuration[{index}].apps must be a unique non-empty list of supported {solution_pack} apps")
            desired_camera = {
                "camera_id": camera_id,
                "source": f"file:/run/secrets/apexfabric/{camera_id}.rtsp",
                "solution_pack": solution_pack,
                "fps": fps,
                "apps": apps,
            }
            if "config" in configured:
                if not isinstance(configured["config"], dict):
                    raise ValueError(f"camera_configuration[{index}].config must be an object")
                desired_camera["config"] = configured["config"]
            elif "config" in profile.get("required_camera_fields", ()):
                desired_camera["config"] = {}
            desired_cameras.append(desired_camera)
            camera_ids.append(camera_id)
        if len(set(camera_ids)) != len(camera_ids):
            raise ValueError("camera_configuration camera IDs must be unique")

        cpu_request = self._positive_int(request.get("cpu_request", 8), "cpu_request", 64)
        cpu_limit = self._positive_int(request.get("cpu_limit", 16), "cpu_limit", 64)
        memory_request = self._positive_int(request.get("memory_request_gib", 16), "memory_request_gib", 256)
        memory_limit = self._positive_int(request.get("memory_limit_gib", 32), "memory_limit_gib", 256)
        if cpu_request > cpu_limit or memory_request > memory_limit:
            raise ValueError("resource requests cannot exceed limits")
        inference_mode = request.get("inference_mode", "cpu-compatible")
        if profile["restrict_inference_modes"] and inference_mode not in TRAFFIC_INFERENCE_MODES:
            raise ValueError("inference_mode is unsupported")

        desired_config_map = f"{deployment_id}-desired-state"
        camera_secret = f"{deployment_id}-camera-sources"
        external_mounts = [{
            "name": f"camera-{index + 1}",
            "mount_path": f"/run/secrets/apexfabric/{camera_id}.rtsp",
            "read_only": True,
            "source": {"type": "secret", "name": camera_secret, "key": f"{camera_id}.rtsp"},
        } for index, camera_id in enumerate(camera_ids)]
        desired_state = {"edge_id": edge_id, "revision": desired_revision, "cameras": desired_cameras}
        desired_schema_path = ROOT / "solution-packs" / "catalog" / profile["schema_directory"] / "desired-state.schema.json"
        try:
            jsonschema.Draft202012Validator(json.loads(desired_schema_path.read_text(encoding="utf-8"))).validate(desired_state)
        except jsonschema.ValidationError as error:
            raise ValueError(f"desired_state validation failed: {error.message}") from error
        bundle = {
            "api_version": "apexfabric.com/v1alpha1", "kind": "DeploymentBundle",
            "deployment_id": deployment_id,
            "solution": {"solution_id": f"{solution_pack}-edge", "version": version},
            "applications": [{
                "name": "runtime",
                "image": {
                    "repository": image_repository, "tag": image_tag, "pull_policy": pull_policy,
                    **({"digest": image_digest} if image_digest else {}),
                },
                "replicas": 1,
                "lifecycle": {"desired_state": "Running", "termination_grace_period_seconds": 60},
                "resources": {
                    "cpu": {"request": str(cpu_request), "limit": str(cpu_limit)},
                    "memory": {"request": f"{memory_request}Gi", "limit": f"{memory_limit}Gi"},
                    "accelerators": {"decoder": 0, "metis": 0},
                    "camera_streams": len(camera_ids),
                },
                "cameras": camera_ids,
                "camera_contract": {
                    "configuration_owner": "platform", "transport": "rtsp",
                    "required_for_readiness": False, "scheduling_mode": "runtime-connectivity",
                },
                "configuration": {
                    "desired_state_config_map": desired_config_map, "desired_state_key": "desired_state.json",
                    "models_delivery": "baked-in", "models_root": profile["models_root"],
                    "inference_mode": inference_mode,
                },
                "placement": {
                    "runtime_profile": "intel-285h-gpu-npu", "node_class": "cv", "architecture": "amd64",
                    "requires_qualified_node": True, "requires_camera_labels": False,
                    "characteristics": {"hardware-profile": "intel-285h"},
                },
                "external_mounts": external_mounts,
                **({"persistent_volumes": [{"name": "state", "mount_path": "/state", "size": f"{self._positive_int(request.get('storage_gib', 20), 'storage_gib', 2048)}Gi", "storage_class": "local-path"}]} if profile["needs_persistent_volume"] else {}),
                "plan_compiler": {
                    "type": "edge-agent-v1", "desired_state_config_map": desired_config_map,
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
                "environment": {"SOLUTION_PACK": solution_pack, **(TRAFFIC_INFERENCE_MODES[inference_mode] if profile["restrict_inference_modes"] else {})},
            }],
            "configuration": {
                "edge_id": edge_id, "image_contract": "apexfabric-v1",
                "secret_input_contract": contract_name, "models_delivery": "baked-in",
                "inference_mode": inference_mode,
            },
        }
        schema = json.loads((ROOT / "solution-packs/schema/deployment-bundle.schema.json").read_text(encoding="utf-8"))
        errors = validate_bundle(bundle, schema)
        if errors:
            raise ValueError("generated bundle is invalid: " + "; ".join(errors))
        objects = render(bundle, "apexfabric")
        return {
            "bundle": bundle, "bundle_yaml": yaml.safe_dump(bundle, sort_keys=False), "objects": objects,
            "desired_state": desired_state,
            "secret_requirements": {"camera_ids": camera_ids, "camera_secret": camera_secret},
            "configuration_requirements": {"desired_state_config_map": desired_config_map},
            "summary": [{"kind": item["kind"], "name": item["metadata"]["name"]} for item in objects],
        }

    def preview_bundle(self, bundle_yaml: str) -> dict[str, Any]:
        if not isinstance(bundle_yaml, str) or len(bundle_yaml.encode()) > 1_000_000:
            raise ValueError("bundle_yaml is required and must be at most 1 MB")
        bundle = yaml.safe_load(bundle_yaml)
        schema = json.loads((ROOT / "solution-packs/schema/deployment-bundle.schema.json").read_text(encoding="utf-8"))
        errors = validate_bundle(bundle, schema)
        if errors:
            raise ValueError("bundle validation failed: " + "; ".join(errors))
        objects = render(bundle, "apexfabric")
        return {
            "bundle": bundle, "bundle_yaml": yaml.safe_dump(bundle, sort_keys=False), "objects": objects,
            "summary": [{"kind": item["kind"], "name": item["metadata"]["name"]} for item in objects],
        }

    def provision(self, job: dict[str, Any], bootstrap: bool = False) -> dict[str, Any]:
        capabilities = self.state_dir / "node-capabilities.json"
        qualification = self.state_dir / "node-qualification.json"
        if bootstrap:
            self.log(job, "Applying host package and kernel prerequisites")
            result = self.runner.run([str(ROOT / "scripts/bootstrap-node.sh"), "--apply"], timeout=1800)
            self.log(job, result.stdout)
        self.log(job, "Discovering and auditing this node; the in-cluster controller owns labels")
        result = self.runner.run([str(ROOT / "scripts/discover-node.sh"), "--output", str(capabilities)])
        self.log(job, result.stdout)
        result = self.runner.run([
            str(ROOT / "scripts/check-node.sh"),
            "--capabilities", str(capabilities), "--output", str(qualification),
        ])
        self.log(job, result.stdout)
        return {
            "capabilities": json.loads(capabilities.read_text(encoding="utf-8")),
            "qualification": json.loads(qualification.read_text(encoding="utf-8")),
        }

    def deploy(self, job: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
        package = request.get("package", "fake-cv-test")
        bundle_yaml = request.get("bundle_yaml")
        if bundle_yaml:
            parsed = yaml.safe_load(bundle_yaml)
            deployment_id = parsed.get("deployment_id") if isinstance(parsed, dict) else None
            if not isinstance(deployment_id, str) or not DEPLOYMENT_ID_RE.fullmatch(deployment_id):
                raise ValueError("uploaded bundle requires a valid DNS-style deployment_id")
            package = deployment_id
            packages = self.state_dir / "packages"
            packages.mkdir(exist_ok=True)
            bundle = packages / f"{deployment_id}.yaml"
            bundle.write_text(bundle_yaml, encoding="utf-8")
        elif package in ALLOWED_PACKAGES:
            bundle = ALLOWED_PACKAGES[package]
        else:
            raise ValueError(f"unsupported package {package!r}")
        self.runner.run([str(ROOT / "scripts/validate-bundle.sh"), str(bundle)])
        configured_inputs = self._apply_bundle_inputs(parsed, request.get("secret_inputs")) if bundle_yaml else []
        if configured_inputs:
            self.log(job, f"Configured {len(configured_inputs)} installation-owned runtime inputs")

        if package == "fake-cv-test":
            self.log(job, "Preparing the local event receiver and fake CV images")
            for script in ("build-event-sink.sh", "build-fake-cv.sh"):
                result = self.runner.run([str(ROOT / "scripts" / script)], timeout=1800)
                self.log(job, result.stdout)
            self.runner.run([str(ROOT / "scripts/node-agent.sh"), str(ALLOWED_PACKAGES["event-sink-test"]), "--once"])

        self.log(job, f"Reconciling DeploymentBundle {package}")
        result = self.runner.run([str(ROOT / "scripts/node-agent.sh"), str(bundle), "--once"])
        report = json.loads(result.stdout)
        self.log(job, result.stdout)
        self.kubectl("apply", "-f", str(ROOT / "deploy/k8s/prometheus-demo.yaml"))
        for deployment in [item["name"] for item in report.get("observed", [])]:
            rollout = self.kubectl("rollout", "status", f"deployment/{deployment}", "-n", "apexfabric", "--timeout=180s")
            self.log(job, rollout.stdout)
        return {"package": package, "reconciliation": report, "configured_inputs": configured_inputs}

    def _apply_bundle_inputs(self, bundle: dict[str, Any], secret_inputs: Any) -> list[str]:
        """Validate and apply the live desired-state ConfigMap and camera Secret.

        Secret values intentionally never enter the DeploymentBundle, package store,
        job log, result, or audit record.
        """
        configuration = bundle.get("configuration", {})
        input_contract = configuration.get("secret_input_contract")
        contract_specs = {
            "tvt-mills-pilot-runtime-v1": ("tvt-mills-pilot", TVT_MILLS_APPS),
        }
        if input_contract not in contract_specs:
            if secret_inputs is not None:
                raise ValueError("secret_inputs are unsupported by this bundle")
            return []
        solution_pack, allowed_apps = contract_specs[input_contract]
        if not isinstance(secret_inputs, dict):
            raise ValueError(f"camera Secret values are required for {solution_pack.title()} Edge Runtime")
        desired_state = secret_inputs.get("desired_state")
        camera_sources = secret_inputs.get("camera_sources")
        camera_ids = secret_inputs.get("camera_ids")
        if not isinstance(desired_state, dict):
            raise ValueError("desired_state is required")

        applications = bundle.get("applications", [])
        if len(applications) != 1 or applications[0].get("name") != "runtime":
            raise ValueError(f"{input_contract} requires exactly one runtime application")
        app = applications[0]
        deployment_id = bundle["deployment_id"]
        expected_desired_config_map = f"{deployment_id}-desired-state"
        expected_camera_secret = f"{deployment_id}-camera-sources"
        compiler = app.get("plan_compiler", {})
        if compiler.get("desired_state_config_map") != expected_desired_config_map or compiler.get("desired_state_key") != "desired_state.json":
            raise ValueError("bundle desired-state ConfigMap contract is invalid")

        expected_cameras = app.get("cameras", [])
        if camera_sources is None and isinstance(camera_ids, list):
            if set(camera_ids) != set(expected_cameras) or len(camera_ids) != len(expected_cameras):
                raise ValueError("camera inventory IDs must exactly match the configured camera IDs")
            _, inventory_sources = self._camera_inventory_resources()
            camera_sources = {camera_id: inventory_sources[camera_id] for camera_id in camera_ids if camera_id in inventory_sources}
        if not isinstance(camera_sources, dict):
            raise ValueError("camera_sources or camera_ids are required")
        desired_cameras = desired_state.get("cameras")
        if desired_state.get("edge_id") != configuration.get("edge_id"):
            raise ValueError("desired_state edge_id does not match the bundle")
        revision = desired_state.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise ValueError("desired_state revision must be a positive integer")
        if not isinstance(desired_cameras, list) or [item.get("camera_id") for item in desired_cameras if isinstance(item, dict)] != expected_cameras:
            raise ValueError("desired_state cameras do not match the bundle")
        if set(camera_sources) != set(expected_cameras):
            raise ValueError("camera Secret values must exactly match the configured camera IDs")

        mounts = {mount.get("mount_path"): mount for mount in app.get("external_mounts", [])}
        for camera in desired_cameras:
            camera_id = camera["camera_id"]
            expected_path = f"/run/secrets/apexfabric/{camera_id}.rtsp"
            expected_source = f"file:{expected_path}"
            if (camera.get("source") != expected_source or camera.get("solution_pack") != solution_pack
                    or not isinstance(camera.get("apps"), list) or not camera["apps"]
                    or any(value not in allowed_apps for value in camera["apps"])):
                raise ValueError(f"desired_state camera {camera_id} violates the {solution_pack.title()} contract")
            mount_source = mounts.get(expected_path, {}).get("source", {})
            if mount_source != {"type": "secret", "name": expected_camera_secret, "key": f"{camera_id}.rtsp"}:
                raise ValueError(f"bundle camera Secret mount for {camera_id} is invalid")
            source = camera_sources[camera_id]
            parsed_source = urlparse(source) if isinstance(source, str) else None
            if (not isinstance(source, str) or len(source) > 4096 or any(ord(char) < 32 for char in source)
                    or parsed_source.scheme not in {"rtsp", "rtsps"} or not parsed_source.hostname):
                raise ValueError(f"camera Secret value for {camera_id} must be a valid RTSP URL")

        labels = {
            "app.kubernetes.io/managed-by": "apexfabric-control-plane",
            "apexfabric.com/deployment-id": deployment_id,
        }
        input_list = {
            "apiVersion": "v1", "kind": "List", "items": [
                {
                    "apiVersion": "v1", "kind": "ConfigMap",
                    "metadata": {
                        "name": expected_desired_config_map, "namespace": "apexfabric", "labels": labels,
                        "annotations": {"apexfabric.com/desired-revision": str(revision)},
                    },
                    "data": {"desired_state.json": json.dumps(desired_state, separators=(",", ":"))},
                },
                {
                    "apiVersion": "v1", "kind": "Secret",
                    "metadata": {"name": expected_camera_secret, "namespace": "apexfabric", "labels": labels},
                    "type": "Opaque", "stringData": {f"{camera_id}.rtsp": camera_sources[camera_id] for camera_id in expected_cameras},
                },
            ],
        }
        self.kubectl("apply", "-f", "-", input_text=json.dumps(input_list))
        return [expected_desired_config_map, expected_camera_secret]

    def workload_action(self, job: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
        name = self._dns_id(request.get("name"), "name")
        deployment = self.kubectl("get", "deployment", name, "-n", "apexfabric", "-o", "json")
        resource = json.loads(deployment.stdout)
        labels = resource.get("metadata", {}).get("labels", {})
        if labels.get("app.kubernetes.io/managed-by") != "apexfabric-node-agent":
            raise ValueError("operations are restricted to ApexFabric-managed deployments")
        action = request.get("action")
        if action == "restart":
            result = self.kubectl("rollout", "restart", f"deployment/{name}", "-n", "apexfabric")
            self.log(job, result.stdout)
            result = self.kubectl("rollout", "status", f"deployment/{name}", "-n", "apexfabric", "--timeout=180s")
            self.log(job, result.stdout)
        elif action == "scale":
            replicas = request.get("replicas")
            if isinstance(replicas, bool) or not isinstance(replicas, int) or replicas < 0 or replicas > 10:
                raise ValueError("replicas must be an integer from 0 through 10")
            result = self.kubectl("scale", "deployment", name, "-n", "apexfabric", f"--replicas={replicas}")
            self.log(job, result.stdout)
        elif action == "logs":
            result = self.kubectl("logs", f"deployment/{name}", "-n", "apexfabric", "--all-containers=true", "--tail=250", check=False)
            output = (result.stdout or result.stderr).strip()
            self.log(job, output)
            if result.returncode:
                raise CommandError(output)
        elif action == "describe":
            result = self.kubectl("describe", "deployment", name, "-n", "apexfabric")
            self.log(job, result.stdout)
        else:
            raise ValueError("unsupported workload action")
        return {"action": action, "deployment": name}

    def runtime_configuration(self, name: str) -> dict[str, Any]:
        """Return the non-secret desired state currently projected into a workload."""
        name = self._dns_id(name, "name")
        deployment = json.loads(self.kubectl("get", "deployment", name, "-n", "apexfabric", "-o", "json").stdout)
        labels = deployment.get("metadata", {}).get("labels", {})
        if labels.get("app.kubernetes.io/managed-by") != "apexfabric-node-agent":
            raise ValueError("configuration is restricted to ApexFabric-managed deployments")
        deployment_id = labels.get("apexfabric.com/deployment-id")
        if not isinstance(deployment_id, str):
            raise ValueError("deployment has no ApexFabric deployment ID")
        config_map_name = f"{deployment_id}-desired-state"
        resource = json.loads(self.kubectl("get", "configmap", config_map_name, "-n", "apexfabric", "-o", "json").stdout)
        desired_state = json.loads(resource.get("data", {}).get("desired_state.json", ""))
        return {"name": name, "config_map": config_map_name, "desired_state": desired_state}

    def update_runtime_configuration(self, job: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
        """Apply a newer desired-state revision without changing the Pod template."""
        current = self.runtime_configuration(request.get("name"))
        desired_state = request.get("desired_state")
        if not isinstance(desired_state, dict):
            raise ValueError("desired_state must be an object")
        old_state = current["desired_state"]
        packs = {camera.get("solution_pack") for camera in old_state.get("cameras", [])}
        schema_directories = {
            "tvt-mills-pilot": "tvt-mills-pilot-2026.09.18-v1",
        }
        if len(packs) != 1 or next(iter(packs), None) not in schema_directories:
            raise ValueError("the deployed solution has an unsupported or mixed solution_pack")
        solution_pack = next(iter(packs))
        catalog_entry = next((
            entry for entry in self.catalog.list()
            if (entry.get("desired_state_schema") or {}).get("properties", {}).get("cameras", {}).get("items", {}).get("properties", {}).get("solution_pack", {}).get("const") == solution_pack
        ), None)
        ui_apps = (catalog_entry or {}).get("contract", {}).get("ui", {}).get("camera", {}).get("apps", {})
        schema_path = ROOT / "solution-packs/catalog" / schema_directories[solution_pack] / "desired-state.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        try:
            jsonschema.Draft202012Validator(schema).validate(desired_state)
        except jsonschema.ValidationError as error:
            raise ValueError(f"desired_state validation failed: {error.message}") from error
        old_revision = old_state.get("revision")
        new_revision = desired_state.get("revision")
        if new_revision <= old_revision:
            raise ValueError(f"desired_state revision must be greater than {old_revision}")
        if desired_state.get("edge_id") != old_state.get("edge_id"):
            raise ValueError("edge_id cannot be changed by a live configuration update")
        old_ids = [camera.get("camera_id") for camera in old_state.get("cameras", [])]
        new_ids = [camera.get("camera_id") for camera in desired_state.get("cameras", [])]
        if new_ids != old_ids:
            raise ValueError("live configuration updates cannot add, remove, or reorder cameras; apply a Deployment update instead")
        for camera in desired_state["cameras"]:
            camera_id = camera["camera_id"]
            if camera.get("source") != f"file:/run/secrets/apexfabric/{camera_id}.rtsp":
                raise ValueError(f"desired_state camera {camera_id} has an invalid source")
            config = camera.get("config", {})
            for app_id in camera.get("apps", []):
                geometry = ui_apps.get(app_id, {}).get("geometry", {})
                if not geometry.get("required"):
                    continue
                group, key = geometry["configPath"].split(".")
                if not config.get(group, {}).get(key):
                    raise ValueError(f"desired_state camera {camera_id} requires config.{geometry['configPath']} ({geometry.get('label', app_id)}) when {app_id} is enabled")

        resource = {
            "apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {
                "name": current["config_map"], "namespace": "apexfabric",
                "annotations": {"apexfabric.com/desired-revision": str(new_revision)},
            },
            "data": {"desired_state.json": json.dumps(desired_state, separators=(",", ":"))},
        }
        self.kubectl("apply", "-f", "-", input_text=json.dumps(resource))
        self.log(job, f"Applied desired-state revision {new_revision} to ConfigMap/{current['config_map']}")
        return {"name": current["name"], "config_map": current["config_map"], "revision": new_revision, "pod_restarted": False}

    def start_enrollment(self, job: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
        """Temporarily switch a face_recognition camera to face_enrollment.

        tvt-mills-pilot has no dedicated enrollment camera -- see
        docs/contracts/tvt-mills-v1/README.md and
        apexfabric/control_plane/enrollment_windows.py. Reverts explicitly
        via stop_enrollment, or automatically once expires_at passes (see
        _session_sweep_supervisor).
        """
        from .enrollment_windows import start_window

        name = request.get("name")
        camera_id = request.get("camera_id")
        if not isinstance(camera_id, str) or not camera_id:
            raise ValueError("camera_id is required")
        duration_seconds = request.get("duration_seconds")
        if duration_seconds is not None:
            duration_seconds = self._positive_int(duration_seconds, "duration_seconds", 24 * 3600)
        current = self.runtime_configuration(name)
        old_state = current["desired_state"]
        camera = next((item for item in old_state.get("cameras", []) if item.get("camera_id") == camera_id), None)
        if camera is None:
            raise ValueError(f"camera {camera_id!r} is not in this deployment's desired state")
        apps = camera.get("apps", [])
        if apps == ["face_enrollment"]:
            raise ValueError(f"camera {camera_id!r} is already in enrollment mode")
        if "face_recognition" not in apps:
            raise ValueError(f"camera {camera_id!r} does not run face_recognition")
        started_at = time.time()
        with self.telemetry.lock, self.telemetry._connect() as connection:
            window_id = start_window(
                connection, name, camera_id, apps, camera.get("config", {}), started_at, duration_seconds,
            )
        new_state = json.loads(json.dumps(old_state))
        new_camera = next(item for item in new_state["cameras"] if item["camera_id"] == camera_id)
        new_camera["apps"] = ["face_enrollment"]
        new_camera["config"] = {}
        new_state["revision"] = old_state["revision"] + 1
        result = self.update_runtime_configuration(job, {"name": name, "desired_state": new_state})
        expires_at = started_at + duration_seconds if duration_seconds else None
        return {**result, "window_id": window_id, "camera_id": camera_id, "expires_at": expires_at}

    def stop_enrollment(self, job: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
        from .enrollment_windows import end_window, get_window

        name = request.get("name")
        window_id = request.get("window_id")
        if not isinstance(window_id, str) or not window_id:
            raise ValueError("window_id is required")
        with self.telemetry.lock, self.telemetry._connect() as connection:
            window = get_window(connection, window_id)
            if window is None or window["status"] != "active":
                raise ValueError(f"enrollment window {window_id!r} is not active")
            end_window(connection, window_id, time.time())
        current = self.runtime_configuration(name)
        old_state = current["desired_state"]
        new_state = json.loads(json.dumps(old_state))
        camera = next((item for item in new_state["cameras"] if item["camera_id"] == window["camera_id"]), None)
        if camera is None:
            raise ValueError(f"camera {window['camera_id']!r} is not in this deployment's desired state")
        camera["apps"] = window["prior_apps"]
        camera["config"] = window["prior_config"]
        new_state["revision"] = old_state["revision"] + 1
        result = self.update_runtime_configuration(job, {"name": name, "desired_state": new_state})
        return {**result, "window_id": window_id, "camera_id": window["camera_id"]}

    def enrollment_windows_for(self, name: str | None) -> list[dict[str, Any]]:
        from .enrollment_windows import list_windows

        with self.telemetry._connect() as connection:
            return list_windows(connection, name)

    def workload_telemetry(self, name: str) -> dict[str, Any]:
        context = self._workload_endpoint_context(name)
        resource = context["deployment"]
        contract = context["contract"]

        def fetch(endpoint: dict[str, Any]) -> subprocess.CompletedProcess[str] | None:
            if not endpoint.get("path") or not context.get("proxy_base"):
                return None
            return self.kubectl("get", "--raw", f"{context['proxy_base']}{endpoint['path']}", check=False)

        health_result = fetch(contract["health"])
        readiness_result = fetch(contract["readiness"])
        metrics_result = fetch(contract["metrics"])

        def json_payload(result: subprocess.CompletedProcess[str] | None) -> Any:
            if result is None or result.returncode != 0:
                return None
            try:
                return json.loads(result.stdout)
            except json.JSONDecodeError:
                return {"raw": result.stdout}

        health = json_payload(health_result)
        readiness = json_payload(readiness_result)
        metrics_payload = json_payload(metrics_result) if contract["metrics"].get("format") in {"json", "application/json"} else None
        required_results = [result for result in (readiness_result, metrics_result) if result is not None]
        available = bool(required_results) and all(result.returncode == 0 for result in required_results)
        failed = next((result for result in (readiness_result, health_result, metrics_result) if result is not None and result.returncode), None)
        deployment_status = resource.get("status", {})
        return {
            "deployment": name,
            "available": available,
            "contract": contract,
            "health": health,
            "readiness": readiness,
            "status": readiness,
            "metrics": metrics_result.stdout if metrics_result is not None and metrics_result.returncode == 0 else "",
            "metrics_payload": metrics_payload,
            "events_url": f"/api/workload-events?name={name}" if contract["events"].get("path") else None,
            "kubernetes": {
                "desired_replicas": resource.get("spec", {}).get("replicas", 0),
                "ready_replicas": deployment_status.get("readyReplicas", 0),
                "available_replicas": deployment_status.get("availableReplicas", 0),
                "conditions": deployment_status.get("conditions", []),
                "pod": context.get("pod_status"),
            },
            "error": "" if available else ((failed.stderr if failed else "") or context.get("proxy_error") or "workload endpoint unavailable").strip()[-1000:],
        }

    def _workload_endpoint_context(self, name: str) -> dict[str, Any]:
        name = self._dns_id(name, "name")
        deployment = self.kubectl("get", "deployment", name, "-n", "apexfabric", "-o", "json")
        resource = json.loads(deployment.stdout)
        resource_metadata = resource.get("metadata", {})
        labels = resource_metadata.get("labels", {})
        if labels.get("app.kubernetes.io/managed-by") != "apexfabric-node-agent":
            raise ValueError("telemetry is restricted to ApexFabric-managed deployments")
        service = self.kubectl("get", "service", name, "-n", "apexfabric", "-o", "json")
        ports = json.loads(service.stdout).get("spec", {}).get("ports", [])
        container_ports = {
            item.get("name"): item.get("containerPort")
            for container in resource.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
            for item in container.get("ports", [])
        }
        port_numbers = {}
        for item in ports:
            target = item.get("targetPort", item.get("port"))
            if isinstance(target, str):
                target = container_ports.get(target, item.get("port"))
            port_numbers[item.get("name")] = target
        available_ports = set(port_numbers)
        annotations = resource_metadata.get("annotations", {})
        contract_declared = "apexfabric.com/readiness-path" in annotations

        if contract_declared:
            contract = {
                "health": {
                    "path": annotations.get("apexfabric.com/health-path"),
                    "port": annotations.get("apexfabric.com/health-port"),
                },
                "readiness": {
                    "path": annotations.get("apexfabric.com/readiness-path"),
                    "port": annotations.get("apexfabric.com/readiness-port"),
                },
                "metrics": {
                    "path": annotations.get("apexfabric.com/metrics-path"),
                    "port": annotations.get("apexfabric.com/metrics-port"),
                    "format": annotations.get("apexfabric.com/metrics-format"),
                },
                "events": {
                    "path": annotations.get("apexfabric.com/events-path"),
                    "port": annotations.get("apexfabric.com/events-port"),
                    "protocol": annotations.get("apexfabric.com/events-protocol"),
                },
            }
        else:
            # Compatibility for bundles rendered before endpoint annotations
            # were introduced. New workloads always use their bundle contract.
            contract = {
                "health": {"path": "/status", "port": "http"},
                "readiness": {"path": "/status", "port": "http"},
                "metrics": {"path": "/metrics", "port": "http", "format": "prometheus"},
                "events": {"path": None, "port": None, "protocol": None},
            }

        for endpoint_name in ("health", "readiness", "metrics", "events"):
            endpoint = contract[endpoint_name]
            if endpoint.get("path") and endpoint.get("port") not in available_ports:
                raise ValueError(
                    f"workload {endpoint_name} endpoint references missing Service port {endpoint.get('port')!r}"
                )
        selector = resource.get("spec", {}).get("selector", {}).get("matchLabels", {})
        selector_text = ",".join(f"{key}={value}" for key, value in sorted(selector.items()))
        pods_result = self.kubectl("get", "pods", "-n", "apexfabric", "-l", selector_text, "-o", "json")
        pods = json.loads(pods_result.stdout).get("items", [])
        pods.sort(key=lambda item: item.get("metadata", {}).get("creationTimestamp", ""), reverse=True)
        pod = next((item for item in pods if item.get("status", {}).get("phase") == "Running"), pods[0] if pods else None)
        pod_status = None
        proxy_base = None
        proxy_error = "no workload Pod exists"
        if pod:
            pod_name = pod["metadata"]["name"]
            statuses = [*pod.get("status", {}).get("initContainerStatuses", []), *pod.get("status", {}).get("containerStatuses", [])]
            pod_status = {
                "name": pod_name, "phase": pod.get("status", {}).get("phase"),
                "node": pod.get("spec", {}).get("nodeName"),
                "containers": [{
                    "name": item.get("name"), "ready": item.get("ready", False),
                    "restarts": item.get("restartCount", 0), "state": item.get("state", {}),
                } for item in statuses],
            }
            endpoint_port = next((endpoint.get("port") for endpoint in contract.values() if endpoint.get("path")), None)
            port = port_numbers.get(endpoint_port)
            if isinstance(port, int):
                proxy_base = f"/api/v1/namespaces/apexfabric/pods/http:{pod_name}:{port}/proxy"
                proxy_error = ""
            else:
                proxy_error = f"Service port {endpoint_port!r} has no numeric targetPort"
        return {
            "deployment": resource, "contract": contract, "proxy_base": proxy_base,
            "proxy_error": proxy_error, "pod_status": pod_status,
        }

    def workload_event_command(self, name: str) -> list[str]:
        context = self._workload_endpoint_context(name)
        events = context["contract"]["events"]
        if events.get("protocol") != "sse" or not events.get("path"):
            raise ValueError("workload does not declare an SSE events endpoint")
        if not context.get("proxy_base"):
            raise ValueError(context.get("proxy_error") or "workload event endpoint unavailable")
        return ["k3s", "kubectl", "get", "--raw", f"{context['proxy_base']}{events['path']}"]

    def _telemetry_supervisor(self) -> None:
        while not self.telemetry_stop.is_set():
            try:
                result = self.kubectl("get", "deployments", "-n", "apexfabric", "-o", "json")
                deployments = json.loads(result.stdout).get("items", [])
                targets = {
                    item["metadata"]["name"] for item in deployments
                    if item.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/managed-by") == "apexfabric-node-agent"
                    and item.get("metadata", {}).get("annotations", {}).get("apexfabric.com/events-protocol") == "sse"
                    and item.get("metadata", {}).get("annotations", {}).get("apexfabric.com/events-path")
                }
                with self.lock:
                    self.telemetry_targets = targets
                    pending = targets - self.telemetry_collectors
                    self.telemetry_collectors.update(pending)
                for name in pending:
                    threading.Thread(target=self._collect_workload_events, args=(name,), name=f"telemetry-{name}", daemon=True).start()
                self.telemetry.enforce_retention()
            except Exception:
                pass
            self.telemetry_stop.wait(10)

    def _collect_workload_events(self, name: str) -> None:
        try:
            while not self.telemetry_stop.is_set():
                with self.lock:
                    if name not in self.telemetry_targets:
                        break
                process = None
                try:
                    process = subprocess.Popen(
                        self.workload_event_command(name), cwd=ROOT, text=True,
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    )
                    data_lines: list[str] = []
                    while process.poll() is None and not self.telemetry_stop.is_set():
                        line = process.stdout.readline() if process.stdout else ""
                        if not line:
                            print(f"telemetry collector for {name!r} disconnected; reconnecting", file=sys.stderr, flush=True)
                            break
                        line = line.rstrip("\r\n")
                        if not line:
                            if data_lines:
                                payload = json.loads("\n".join(data_lines))
                                event_type = payload.get("type") or payload.get("event_type") or payload.get("event") \
                                    if isinstance(payload, dict) else None
                                if isinstance(payload, dict) and event_type not in SUPPRESSED_TELEMETRY_EVENT_TYPES:
                                    # Snapshot fetches shell out to kubectl and must not
                                    # block draining the SSE pipe, or ingestion falls
                                    # behind the live stream. Insert the event inline
                                    # (fast, DB-only) and fetch its snapshots in the
                                    # background.
                                    event_id = self.telemetry.ingest(name, payload)
                                    self.telemetry_snapshot_executor.submit(
                                        self.telemetry.attach_snapshots, event_id, payload,
                                        lambda url, maximum: self._fetch_snapshot(name, url, maximum),
                                    )
                                data_lines.clear()
                        elif line.startswith("data:"):
                            data_lines.append(line[5:].lstrip())
                except (CommandError, ValueError, json.JSONDecodeError, OSError) as error:
                    print(f"telemetry collector for {name!r} reconnecting after error: {error}", file=sys.stderr, flush=True)
                finally:
                    if process and process.poll() is None:
                        process.terminate()
                self.telemetry_stop.wait(3)
        finally:
            with self.lock:
                self.telemetry_collectors.discard(name)

    def _fetch_snapshot(self, name: str, snapshot_url: str, maximum_bytes: int) -> tuple[bytes, str]:
        context = self._workload_endpoint_context(name)
        if not context.get("proxy_base"):
            raise ValueError(context.get("proxy_error") or "snapshot endpoint unavailable")
        process = subprocess.Popen(
            ["k3s", "kubectl", "get", "--raw", f"{context['proxy_base']}{snapshot_url}"],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        content = process.stdout.read(maximum_bytes + 1) if process.stdout else b""
        if len(content) > maximum_bytes:
            process.kill()
            process.wait()
            raise ValueError("snapshot exceeds the configured per-file limit")
        _, error = process.communicate()
        if process.returncode:
            raise CommandError(error.decode(errors="replace")[-1000:])
        suffix = PurePosixPath(snapshot_url).suffix.lower()
        content_type = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}.get(suffix, "application/octet-stream")
        return content, content_type

    def lifecycle_test(self, job: dict[str, Any], scenario: str) -> dict[str, Any]:
        allowed = {"all", "container", "pod", "readiness", "recovery", "resource", "update", "rollback"}
        if scenario not in allowed:
            raise ValueError(f"unsupported lifecycle scenario {scenario!r}")
        self.log(job, f"Running real Kubernetes lifecycle scenario: {scenario}")
        result = self.runner.run([str(ROOT / "scripts/test-k8s-lifecycle.sh"), scenario], timeout=1800)
        self.log(job, result.stdout)
        return {"scenario": scenario, "evidence": result.stdout.strip().splitlines()}

    def solution_upgrade_test(self, job: dict[str, Any]) -> dict[str, Any]:
        self.log(job, "Running Traffic Solution v1 -> v2 -> rollback -> unhealthy-v2 acceptance flow")
        result = self.runner.run([str(ROOT / "scripts/test-solution-upgrade.sh")], timeout=1800)
        self.log(job, result.stdout)
        return {"evidence": result.stdout.strip().splitlines()}

    def demo_control(self, job: dict[str, Any], action: str) -> dict[str, Any]:
        if action == "stop":
            changed = []
            for name in DEMO_DEPLOYMENTS:
                result = self.kubectl("scale", "deployment", name, "-n", "apexfabric", "--replicas=0", check=False)
                if result.returncode == 0:
                    changed.append(name)
                    self.log(job, result.stdout)
                elif "NotFound" not in result.stderr:
                    raise CommandError(result.stderr.strip())
            pending = self.kubectl("delete", "pod", "failure-resource-pending", "-n", "apexfabric", "--ignore-not-found", check=False)
            if pending.returncode:
                raise CommandError(pending.stderr.strip())
            self.log(job, pending.stdout)
            return {"action": action, "scaled_to_zero": changed, "removed_test_pod": bool(pending.stdout.strip())}
        if action == "reset":
            removed = []
            for kind, names in DEMO_RESOURCES.items():
                for name in names:
                    result = self.kubectl("delete", kind, name, "-n", "apexfabric", "--ignore-not-found", "--wait=true", check=False)
                    if result.returncode:
                        raise CommandError(result.stderr.strip())
                    if result.stdout.strip():
                        removed.append(f"{kind}/{name}")
                        self.log(job, result.stdout)
            self.log(job, "Recreating event sink, fake CV, and Prometheus from repository desired state")
            deployed = self.deploy(job, {"package": "fake-cv-test"})
            return {"action": action, "removed": removed, "deployment": deployed}
        raise ValueError(f"unsupported demo action {action!r}")

    def status(self) -> dict[str, Any]:
        result: dict[str, Any] = {"site_id": self.site_id(), "links": {
            "pipeline": "http://127.0.0.1:18080/status",
            "metrics": "http://127.0.0.1:18080/metrics",
            "prometheus": "http://127.0.0.1:19090",
            "targets": "http://127.0.0.1:19090/targets",
        }}
        for key, resource in (
            ("nodes", "nodes"), ("deployments", "deployments"), ("pods", "pods"),
            ("services", "services"), ("persistent_volume_claims", "persistentvolumeclaims"),
            ("replica_sets", "replicasets"), ("events", "events"),
        ):
            response = self.kubectl("get", resource, *( [] if key == "nodes" else ["-n", "apexfabric"]), "-o", "json", check=False)
            result[key] = json.loads(response.stdout).get("items", []) if response.returncode == 0 else []
        reports = self.kubectl("get", "apexnodestatuses", "-o", "json", check=False)
        result["node_reports"] = json.loads(reports.stdout).get("items", []) if reports.returncode == 0 else []
        self._sync_box_registry(result["nodes"], result["node_reports"])
        capabilities = self.state_dir / "node-capabilities.json"
        qualification = self.state_dir / "node-qualification.json"
        result["discovery"] = json.loads(capabilities.read_text()) if capabilities.exists() else None
        result["qualification"] = json.loads(qualification.read_text()) if qualification.exists() else None
        packages = self.state_dir / "packages"
        result["saved_bundles"] = sorted(path.stem for path in packages.glob("*.yaml")) if packages.exists() else []
        with self.lock:
            result["jobs"] = list(self.jobs.values())[-20:]
        return result

    def _sync_box_registry(self, nodes: list[dict[str, Any]], reports: list[dict[str, Any]]) -> None:
        reports_by_name = {item.get("metadata", {}).get("name"): item for item in reports}
        seen: set[str] = set()
        for node in nodes:
            metadata = node.get("metadata", {})
            node_id = metadata.get("name")
            if not isinstance(node_id, str):
                continue
            seen.add(node_id)
            labels = metadata.get("labels", {})
            conditions = node.get("status", {}).get("conditions", [])
            is_ready = any(item.get("type") == "Ready" and item.get("status") == "True" for item in conditions)
            pressure = [item.get("type") for item in conditions if item.get("type") in {"DiskPressure", "MemoryPressure", "PIDPressure"} and item.get("status") == "True"]
            report = reports_by_name.get(node_id, {})
            self.device_registry.upsert(node_id, "box", node_id, "online" if is_ready else "offline", {
                "node_uid": metadata.get("uid"),
                "role": "control-plane" if "node-role.kubernetes.io/control-plane" in labels else "edge",
                "architecture": node.get("status", {}).get("nodeInfo", {}).get("architecture"),
                "operating_system": node.get("status", {}).get("nodeInfo", {}).get("osImage"),
                "kubelet_version": node.get("status", {}).get("nodeInfo", {}).get("kubeletVersion"),
                "qualified": report.get("status", {}).get("accepted", labels.get("apexfabric.com/qualified") == "true"),
                "qualification_reason": report.get("status", {}).get("reason"),
                "pressure": pressure,
                "addresses": node.get("status", {}).get("addresses", []),
            }, seen=True)
        self.device_registry.mark_unseen_boxes_offline(seen)

    def site_id(self) -> str:
        path = self.state_dir / "site-id"
        if not path.exists():
            machine = Path("/etc/machine-id").read_text(encoding="utf-8").strip()
            path.write_text(f"site-{uuid.uuid5(uuid.NAMESPACE_OID, machine).hex[:12]}\n", encoding="utf-8")
        return path.read_text(encoding="utf-8").strip()


class Handler(BaseHTTPRequestHandler):
    controller: Controller

    def respond(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def json_response(self, status: int, payload: Any) -> None:
        self.respond(status, json.dumps(payload, sort_keys=True).encode(), "application/json")

    def read_json(self) -> dict[str, Any]:
        length = min(int(self.headers.get("Content-Length", "0")), 1_000_000)
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path in {"/apexfabricdashboard", "/apexfabricdashboard/"}:
            path = "/site"
        elif path.startswith("/apexfabricdashboard/"):
            path = path[len("/apexfabricdashboard"):]
        elif path in {"/dashboard/customer", "/dashboard/customer/"}:
            path = "/customer"
        elif path in {"/dashboard/alerts.js", "/dashboard/customer.js", "/dashboard/reports.js", "/dashboard/site.css"}:
            path = path[len("/dashboard"):]
        if path == "/site":
            self.respond(HTTPStatus.OK, (ROOT / "apexfabric/control_plane/static/site.html").read_bytes(), "text/html; charset=utf-8")
        elif path == "/site.js":
            self.respond(HTTPStatus.OK, (ROOT / "apexfabric/control_plane/static/site.js").read_bytes(), "text/javascript; charset=utf-8")
        elif path == "/site.css":
            self.respond(HTTPStatus.OK, (ROOT / "apexfabric/control_plane/static/site.css").read_bytes(), "text/css; charset=utf-8")
        elif path in {"/customer", "/customer/"}:
            self.respond(HTTPStatus.OK, (ROOT / "apexfabric/control_plane/static/customer.html").read_bytes(), "text/html; charset=utf-8")
        elif path == "/alerts.js":
            self.respond(HTTPStatus.OK, (ROOT / "apexfabric/control_plane/static/alerts.js").read_bytes(), "text/javascript; charset=utf-8")
        elif path == "/customer.js":
            self.respond(HTTPStatus.OK, (ROOT / "apexfabric/control_plane/static/customer.js").read_bytes(), "text/javascript; charset=utf-8")
        elif path == "/reports.js":
            self.respond(HTTPStatus.OK, (ROOT / "apexfabric/control_plane/static/reports.js").read_bytes(), "text/javascript; charset=utf-8")
        elif path == "/" or path in {"/dashboard", "/deployments", "/deployments/new", "/infrastructure/nodes", "/infrastructure/cluster", "/operations", "/events"} or path.startswith("/deployments/") or path.startswith("/infrastructure/nodes/"):
            self.respond(HTTPStatus.OK, (ROOT / "apexfabric/control_plane/static/index.html").read_bytes(), "text/html; charset=utf-8")
        elif path == "/enhancements.js":
            self.respond(HTTPStatus.OK, (ROOT / "apexfabric/control_plane/static/enhancements.js").read_bytes(), "text/javascript; charset=utf-8")
        elif path == "/api/status":
            try: self.json_response(HTTPStatus.OK, self.controller.status())
            except Exception as error: self.json_response(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)})
        elif path == "/api/catalog":
            self.json_response(HTTPStatus.OK, {"solutions": self.controller.catalog.list()})
        elif path == "/api/alert-rules":
            self.json_response(HTTPStatus.OK, {"rules": self.controller.alerts.rules()})
        elif path == "/api/alerts":
            self.json_response(HTTPStatus.OK, {"alerts": self.controller.alerts.list()})
        elif path == "/api/customer":
            try: self.json_response(HTTPStatus.OK, self.controller.customer_summary())
            except (ValueError, CommandError) as error:
                self.json_response(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Site data is unavailable"})
        elif path == "/api/cameras":
            try: self.json_response(HTTPStatus.OK, self.controller.camera_inventory())
            except (ValueError, json.JSONDecodeError, CommandError) as error: self.json_response(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        elif path == "/api/device-registry":
            self.json_response(HTTPStatus.OK, {
                "site_id": self.controller.site_id(),
                "database": str(self.controller.device_registry.database),
                "devices": self.controller.device_registry.list(),
            })
        elif path == "/api/cameras/snapshot":
            try:
                camera_id = parse_qs(parsed.query).get("camera_id", [""])[0]
                self.respond(HTTPStatus.OK, self.controller.camera_snapshot(camera_id), "image/jpeg")
            except (ValueError, CommandError) as error:
                self.json_response(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        elif path == "/api/telemetry/events":
            query = parse_qs(parsed.query)
            deployment_id = query.get("deployment_id", [None])[0]
            try:
                limit = int(query.get("limit", ["100"])[0])
                self.json_response(HTTPStatus.OK, {"events": self.controller.telemetry.recent_events(deployment_id, limit)})
            except ValueError as error:
                self.json_response(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        elif path == "/api/telemetry/storage":
            self.json_response(HTTPStatus.OK, self.controller.telemetry.stats())
        elif path == "/api/reports/attendance":
            from .reporting import attendance_report
            query = parse_qs(parsed.query)
            person_id = query.get("person_id", [None])[0]
            date = query.get("date", [None])[0]
            with self.controller.telemetry._connect() as connection:
                self.json_response(HTTPStatus.OK, attendance_report(connection, person_id, date))
        elif path == "/api/reports/vehicle-traffic":
            from .reporting import vehicle_traffic_report
            query = parse_qs(parsed.query)
            date = query.get("date", [None])[0]
            gate = query.get("gate", [None])[0]
            with self.controller.telemetry._connect() as connection:
                self.json_response(HTTPStatus.OK, vehicle_traffic_report(connection, date, gate))
        elif path == "/api/persons":
            status = parse_qs(parsed.query).get("status", [None])[0]
            self.json_response(HTTPStatus.OK, {"persons": self.controller.persons.list(status)})
        elif path == "/api/enrollment-windows":
            name = parse_qs(parsed.query).get("name", [None])[0]
            self.json_response(HTTPStatus.OK, {"windows": self.controller.enrollment_windows_for(name)})
        elif path.startswith("/api/telemetry/snapshots/"):
            snapshot_id = path.rsplit("/", 1)[-1]
            snapshot = self.controller.telemetry.snapshot(snapshot_id)
            if not snapshot or not snapshot[0].is_file():
                self.json_response(HTTPStatus.NOT_FOUND, {"error": "snapshot not found"})
            else:
                self.respond(HTTPStatus.OK, snapshot[0].read_bytes(), snapshot[1])
        elif path.startswith("/api/jobs/"):
            job = self.controller.jobs.get(path.rsplit("/", 1)[-1])
            self.json_response(HTTPStatus.OK if job else HTTPStatus.NOT_FOUND, job or {"error": "job not found"})
        elif path == "/api/workload-telemetry":
            try:
                name = parse_qs(parsed.query).get("name", [""])[0]
                self.json_response(HTTPStatus.OK, self.controller.workload_telemetry(name))
            except (ValueError, json.JSONDecodeError) as error:
                self.json_response(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        elif path == "/api/runtime-configuration":
            try:
                name = parse_qs(parsed.query).get("name", [""])[0]
                self.json_response(HTTPStatus.OK, self.controller.runtime_configuration(name))
            except (ValueError, json.JSONDecodeError, CommandError) as error:
                self.json_response(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        elif path == "/api/workload-events":
            process = None
            try:
                name = parse_qs(parsed.query).get("name", [""])[0]
                command = self.controller.workload_event_command(name)
            except ValueError as error:
                self.json_response(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                return
            try:
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                while process.poll() is None:
                    chunk = process.stdout.readline() if process.stdout else b""
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                if process and process.poll() is None:
                    process.terminate()
        else:
            self.json_response(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        try:
            request = self.read_json()
            path = urlparse(self.path).path
            if path.startswith("/apexfabricdashboard/"):
                path = path[len("/apexfabricdashboard"):]
            if path == "/api/alert-rules":
                action = request.get("action", "save")
                if action == "save":
                    self.json_response(HTTPStatus.OK, {"rule": self.controller.alerts.save(request.get("rule"))}); return
                if action == "delete" and isinstance(request.get("id"), str):
                    self.controller.alerts.delete(request["id"])
                    self.json_response(HTTPStatus.OK, {"ok": True}); return
                raise ValueError("invalid rule action")
            elif path == "/api/alerts/acknowledge":
                if not isinstance(request.get("id"), str):
                    raise ValueError("alert ID is required")
                self.controller.alerts.acknowledge(request["id"])
                self.json_response(HTTPStatus.OK, {"ok": True}); return
            elif path == "/api/persons/rename":
                if not isinstance(request.get("person_id"), str):
                    raise ValueError("person_id is required")
                self.controller.persons.rename(request["person_id"], request.get("display_name"))
                self.json_response(HTTPStatus.OK, {"ok": True}); return
            elif path == "/api/bundles/generate":
                self.json_response(HTTPStatus.OK, self.controller.generate_bundle(request)); return
            elif path == "/api/catalog/refresh":
                self.json_response(HTTPStatus.OK, {"solutions": self.controller.catalog.refresh()}); return
            elif path == "/api/cameras":
                action = request.get("action", "save")
                if action == "save":
                    self.json_response(HTTPStatus.OK, self.controller.save_camera(request)); return
                if action == "delete":
                    self.json_response(HTTPStatus.OK, self.controller.delete_camera(request)); return
                raise ValueError("invalid camera action")
            elif path == "/api/bundles/preview":
                self.json_response(HTTPStatus.OK, self.controller.preview_bundle(request.get("bundle_yaml"))); return
            elif path == "/api/provision":
                job_id = self.controller.submit("provision", self.controller.provision, bool(request.get("bootstrap", False)))
            elif path == "/api/deploy":
                job_id = self.controller.submit("deploy", self.controller.deploy, request)
            elif path == "/api/runtime-configuration":
                job_id = self.controller.submit("configuration:update", self.controller.update_runtime_configuration, request)
            elif path == "/api/enrollment/start":
                job_id = self.controller.submit("enrollment:start", self.controller.start_enrollment, request)
            elif path == "/api/enrollment/stop":
                job_id = self.controller.submit("enrollment:stop", self.controller.stop_enrollment, request)
            elif path == "/api/lifecycle-test":
                scenario = request.get("scenario", "all")
                if scenario not in {"all", "container", "pod", "readiness", "recovery", "resource", "update", "rollback"}:
                    raise ValueError("invalid lifecycle scenario")
                job_id = self.controller.submit(f"lifecycle:{scenario}", self.controller.lifecycle_test, scenario)
            elif path == "/api/demo-control":
                action = request.get("action")
                if action not in {"stop", "reset"}:
                    raise ValueError("invalid demo control action")
                job_id = self.controller.submit(f"demo:{action}", self.controller.demo_control, action)
            elif path == "/api/solution-upgrade-test":
                job_id = self.controller.submit("solution-upgrade", self.controller.solution_upgrade_test)
            elif path == "/api/workload-action":
                action = request.get("action")
                if action not in {"restart", "scale", "logs", "describe"}:
                    raise ValueError("invalid workload action")
                job_id = self.controller.submit(f"workload:{action}", self.controller.workload_action, request)
            else:
                self.json_response(HTTPStatus.NOT_FOUND, {"error": "not found"}); return
            self.json_response(HTTPStatus.ACCEPTED, {"job_id": job_id})
        except (ValueError, json.JSONDecodeError, yaml.YAMLError) as error:
            self.json_response(HTTPStatus.BAD_REQUEST, {"error": str(error)})

    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.address_string()} {format % args}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="ApexFabric local site controller")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    args = parser.parse_args()
    Handler.controller = Controller(args.state_dir, start_background=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
