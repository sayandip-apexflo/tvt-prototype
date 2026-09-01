#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from apexfabric.common.kube import ApiError, KubeApi
from apexfabric.node_management.discovery.discovery import discover

VERSION = os.getenv("REPORTER_VERSION", "0.1.0")
NODE_NAME = os.getenv("NODE_NAME", "")
INTERVAL = max(int(os.getenv("REPORT_INTERVAL_SECONDS", "30")), 10)
RESOURCE_PATH = "/apis/apexfabric.com/v1alpha1/apexnodestatuses"
NODE_PATH = "/api/v1/nodes"
CAMERA_STREAM_RESOURCE = "apexfabric.com/camera-streams"
PROFILE_CONFIG = Path(os.getenv("APEXFABRIC_HARDWARE_PROFILES", "/app/config/hardware-profiles.json"))


def load_camera_capacities(path: Path = PROFILE_CONFIG) -> dict[str, int]:
    profiles = json.loads(path.read_text(encoding="utf-8"))
    capacities = {
        name: details["camera_streams"]
        for name, details in profiles.items()
        if "camera_streams" in details
    }
    if any(not isinstance(value, int) or value < 1 for value in capacities.values()):
        raise ValueError("hardware profile camera_streams values must be positive integers")
    return capacities


class Reporter:
    def __init__(self, api: KubeApi, camera_capacities: dict[str, int] | None = None):
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", NODE_NAME):
            raise ValueError(f"invalid or missing NODE_NAME: {NODE_NAME!r}")
        self.api = api
        self.last_success: str | None = None
        self.last_error: str | None = None
        self.cycles = 0
        self.camera_capacities = camera_capacities if camera_capacities is not None else load_camera_capacities()
        self.capacity_updates = 0
        self.capacity_last_error: str | None = None

    def payload(self, capabilities: dict[str, Any]) -> dict[str, Any]:
        return {
            "apiVersion": "apexfabric.com/v1alpha1", "kind": "ApexNodeStatus",
            "metadata": {"name": NODE_NAME, "labels": {"apexfabric.com/node-name": NODE_NAME}},
            "spec": {
                "nodeName": NODE_NAME,
                "reporterVersion": VERSION,
                "observedAt": datetime.now(timezone.utc).isoformat(),
                "capabilities": capabilities,
            },
        }

    def report_once(self) -> dict[str, Any]:
        payload = self.payload(discover())
        try:
            self.api.patch(f"{RESOURCE_PATH}/{NODE_NAME}", payload, "application/merge-patch+json")
        except ApiError as error:
            if "HTTP 404" not in str(error):
                raise
            self.api.post(RESOURCE_PATH, payload)
        self.last_success = payload["spec"]["observedAt"]
        self.last_error = None
        self.cycles += 1
        return payload

    def advertise_camera_capacity(self) -> int | None:
        node = self.api.get(f"{NODE_PATH}/{NODE_NAME}")
        profile = node.get("metadata", {}).get("labels", {}).get("apexfabric.com/hardware-profile")
        if profile not in self.camera_capacities:
            return None
        capacity = self.camera_capacities[profile]
        quantity = str(capacity)
        self.api.patch(
            f"{NODE_PATH}/{NODE_NAME}/status",
            {"status": {
                "capacity": {CAMERA_STREAM_RESOURCE: quantity},
                "allocatable": {CAMERA_STREAM_RESOURCE: quantity},
            }},
            "application/merge-patch+json",
        )
        self.capacity_updates += 1
        self.capacity_last_error = None
        return capacity

    def cycle(self) -> None:
        try:
            self.report_once()
        except Exception as error:
            self.last_error = str(error)[-2000:]
            print(json.dumps({"level": "error", "operation": "capability-report", "error": self.last_error}), flush=True)
        try:
            self.advertise_camera_capacity()
        except Exception as error:
            self.capacity_last_error = str(error)[-2000:]
            print(json.dumps({"level": "error", "operation": "camera-capacity", "error": self.capacity_last_error}), flush=True)

    def run(self) -> None:
        while True:
            time.sleep(INTERVAL)
            self.cycle()

    @property
    def healthy(self) -> bool:
        return self.last_success is not None and self.last_error is None

    def metrics(self) -> str:
        return (
            "# TYPE apexfabric_node_reporter_up gauge\n"
            f"apexfabric_node_reporter_up {1 if self.healthy else 0}\n"
            "# TYPE apexfabric_node_report_cycles_total counter\n"
            f"apexfabric_node_report_cycles_total {self.cycles}\n"
            "# TYPE apexfabric_node_reporter_capacity_updates_total counter\n"
            f"apexfabric_node_reporter_capacity_updates_total {self.capacity_updates}\n"
        )


REPORTER: Reporter


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            # Liveness means the process and HTTP loop are alive. A reporting
            # failure is a readiness condition and must not erase diagnostics
            # by repeatedly restarting the container.
            status = HTTPStatus.OK
            body = json.dumps({"alive": True, "node": NODE_NAME, "lastSuccess": REPORTER.last_success, "error": REPORTER.last_error}).encode()
            content_type = "application/json"
        elif self.path == "/ready":
            status = HTTPStatus.OK if REPORTER.healthy else HTTPStatus.SERVICE_UNAVAILABLE
            body = json.dumps({"healthy": REPORTER.healthy, "node": NODE_NAME, "lastSuccess": REPORTER.last_success, "error": REPORTER.last_error}).encode()
            content_type = "application/json"
        elif self.path == "/metrics":
            status, body, content_type = HTTPStatus.OK, REPORTER.metrics().encode(), "text/plain; version=0.0.4"
        else:
            status, body, content_type = HTTPStatus.NOT_FOUND, b'{"error":"not_found"}', "application/json"
        self.send_response(status); self.send_header("Content-Type", content_type); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


def main() -> None:
    global REPORTER
    REPORTER = Reporter(KubeApi())
    REPORTER.cycle()
    threading.Thread(target=REPORTER.run, daemon=True, name="report-loop").start()
    ThreadingHTTPServer(("0.0.0.0", 9100), Handler).serve_forever()


if __name__ == "__main__":
    main()
