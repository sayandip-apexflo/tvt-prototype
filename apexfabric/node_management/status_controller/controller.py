#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any

from apexfabric.common.kube import KubeApi

REPORTS = "/apis/apexfabric.com/v1alpha1/apexnodestatuses"
NODES = "/api/v1/nodes"
INTERVAL = max(int(os.getenv("RECONCILE_INTERVAL_SECONDS", "15")), 5)
STALE_SECONDS = max(int(os.getenv("REPORT_STALE_SECONDS", "120")), 30)


def report_labels(report: dict[str, Any], node: dict[str, Any], now: datetime | None = None) -> tuple[dict[str, str], bool, str]:
    now = now or datetime.now(timezone.utc)
    spec = report.get("spec", {})
    capabilities = spec.get("capabilities", {})
    reasons = []
    if spec.get("nodeName") != node.get("metadata", {}).get("name"):
        reasons.append("NodeNameMismatch")
    try:
        observed = datetime.fromisoformat(spec["observedAt"].replace("Z", "+00:00"))
        if (now - observed).total_seconds() > STALE_SECONDS:
            reasons.append("ReportStale")
    except (KeyError, TypeError, ValueError):
        reasons.append("InvalidObservedAt")
    ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in node.get("status", {}).get("conditions", []))
    if not ready:
        reasons.append("NodeNotReady")
    cpu_arch = capabilities.get("hardware", {}).get("cpu", {}).get("architecture", "unknown").lower()
    arch = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(cpu_arch, cpu_arch)
    cluster_arch = node.get("metadata", {}).get("labels", {}).get("kubernetes.io/arch")
    if arch not in {"amd64", "arm64"} or arch != cluster_arch:
        reasons.append("ArchitectureMismatch")
    profile = node.get("metadata", {}).get("labels", {}).get("apexfabric.com/hardware-profile")
    if profile == "intel-285h":
        gpu = capabilities.get("accelerators", {}).get("gpu", {})
        npu = capabilities.get("accelerators", {}).get("npu", {})
        if not gpu.get("present") or not any("renderD" in path for path in gpu.get("device_nodes", [])):
            reasons.append("IntelGpuDeviceUnavailable")
        if not capabilities.get("decoder", {}).get("va_api", {}).get("available"):
            reasons.append("IntelVaApiUnavailable")
        if not npu.get("present") or not npu.get("driver", {}).get("loaded"):
            reasons.append("IntelNpuDriverUnavailable")
    metis = str(bool(capabilities.get("accelerators", {}).get("metis", {}).get("present"))).lower()
    decoder = "vaapi" if capabilities.get("decoder", {}).get("va_api", {}).get("available") else "none"
    labels = {
        "apexfabric.com/node-class": "cv",
        "apexfabric.com/architecture": arch or "unknown",
        "apexfabric.com/metis": metis,
        "apexfabric.com/decoder": decoder,
        "apexfabric.com/reporter-version": str(spec.get("reporterVersion", "unknown")).replace("+", "_"),
    }
    return labels, not reasons, ",".join(reasons) if reasons else "Accepted"


class Controller:
    def __init__(self, api: KubeApi):
        self.api = api

    def reconcile(self) -> list[dict[str, Any]]:
        reports = self.api.get(REPORTS).get("items", [])
        nodes = {item["metadata"]["name"]: item for item in self.api.get(NODES).get("items", [])}
        results = []
        for report in reports:
            name = report["metadata"]["name"]
            node = nodes.get(name)
            if not node:
                results.append({"node": name, "accepted": False, "reason": "NodeNotFound"}); continue
            labels, accepted, reason = report_labels(report, node)
            labels["apexfabric.com/qualified"] = str(accepted).lower()
            self.api.patch(f"{NODES}/{name}", {"metadata": {"labels": labels}})
            condition = {"type": "Accepted", "status": "True" if accepted else "False", "reason": reason, "lastTransitionTime": datetime.now(timezone.utc).isoformat()}
            self.api.patch(f"{REPORTS}/{name}/status", {"status": {"accepted": accepted, "reason": reason, "conditions": [condition]}})
            results.append({"node": name, "accepted": accepted, "reason": reason, "labels": labels})
        return results

    def run(self) -> None:
        while True:
            try:
                print(json.dumps({"reconciled": self.reconcile()}, sort_keys=True), flush=True)
            except Exception as error:
                print(json.dumps({"error": str(error)[-2000:]}, sort_keys=True), flush=True)
            time.sleep(INTERVAL)


if __name__ == "__main__":
    Controller(KubeApi()).run()
