"""Bounded, read-only K3s status collection for the management plane."""

from __future__ import annotations

import json
import subprocess
from typing import Any

from apexfabric.solution_management.renderer import Kubectl


MANAGED_SELECTOR = "app.kubernetes.io/part-of=apexfabric"
MAX_CLUSTER_ITEMS = 100
KUBERNETES_REQUEST_TIMEOUT = "5s"


def _condition_status(item: dict[str, Any], condition_type: str) -> str | None:
    for condition in item.get("status", {}).get("conditions", []):
        if condition.get("type") == condition_type:
            return condition.get("status")
    return None


def _pod_ready(item: dict[str, Any]) -> bool:
    return _condition_status(item, "Ready") == "True"


class ClusterStatusReader:
    """Collect the allowlisted subset of cluster state used by the UI and CLI."""

    def __init__(
        self,
        kubectl: Kubectl | None,
        namespace: str = "apexfabric",
        *,
        max_items: int = MAX_CLUSTER_ITEMS,
    ) -> None:
        self.kubectl = kubectl
        self.namespace = namespace
        self.max_items = max(1, min(max_items, MAX_CLUSTER_ITEMS))

    @staticmethod
    def _items(stdout: str) -> list[dict[str, Any]]:
        document = json.loads(stdout)
        if not isinstance(document, dict):
            raise ValueError("Kubernetes list response must be an object")
        items = document.get("items", [])
        if not isinstance(items, list):
            raise ValueError("Kubernetes list response has invalid items")
        return [item for item in items if isinstance(item, dict)]

    @staticmethod
    def _unavailable(error: Exception | None = None) -> dict[str, Any]:
        if isinstance(error, subprocess.CalledProcessError):
            error_code = "KUBERNETES_COMMAND_FAILED"
        elif isinstance(error, json.JSONDecodeError):
            error_code = "KUBERNETES_RESPONSE_INVALID"
        elif error is None:
            error_code = "KUBERNETES_CLIENT_NOT_CONFIGURED"
        else:
            error_code = "KUBERNETES_UNAVAILABLE"
        return {
            "status": "unavailable",
            "api": {"status": "unavailable", "error_code": error_code},
            "nodes": {"status": "unavailable", "total": 0, "items": []},
            "workloads": {
                "status": "unavailable",
                "deployments": {"total": 0, "items": []},
                "pods": {"total": 0, "items": []},
            },
        }

    def snapshot(self) -> dict[str, Any]:
        if self.kubectl is None:
            return self._unavailable()
        try:
            nodes = self._items(
                self.kubectl.run(
                    "get",
                    "nodes",
                    f"--request-timeout={KUBERNETES_REQUEST_TIMEOUT}",
                    "-o",
                    "json",
                ).stdout
            )
            resources = self._items(
                self.kubectl.run(
                    "get",
                    "deployments,pods",
                    "-n",
                    self.namespace,
                    "-l",
                    MANAGED_SELECTOR,
                    f"--request-timeout={KUBERNETES_REQUEST_TIMEOUT}",
                    "-o",
                    "json",
                ).stdout
            )
        except (
            OSError,
            ValueError,
            subprocess.CalledProcessError,
            json.JSONDecodeError,
        ) as error:
            return self._unavailable(error)

        deployment_items = [
            item for item in resources if item.get("kind") == "Deployment"
        ]
        pod_items = [item for item in resources if item.get("kind") == "Pod"]
        node_views = [self._node_view(item) for item in nodes[: self.max_items]]
        deployment_views = [
            self._deployment_view(item) for item in deployment_items[: self.max_items]
        ]
        pod_views = [self._pod_view(item) for item in pod_items[: self.max_items]]

        nodes_healthy = len(nodes) == 1 and all(
            item["ready"] and item["qualified"] for item in node_views
        )
        workloads_healthy = all(item["ready"] for item in deployment_views) and all(
            item["phase"] == "Running" and item["ready"] for item in pod_views
        )
        node_status = "healthy" if nodes_healthy else "degraded"
        workload_status = (
            "unconfigured"
            if not deployment_items and not pod_items
            else "healthy" if workloads_healthy else "degraded"
        )
        overall = (
            "healthy"
            if node_status == "healthy"
            and workload_status in {"healthy", "unconfigured"}
            else "degraded"
        )
        return {
            "status": overall,
            "api": {"status": "healthy"},
            "nodes": {
                "status": node_status,
                "total": len(nodes),
                "items": node_views,
                "truncated": len(nodes) > self.max_items,
            },
            "workloads": {
                "status": workload_status,
                "deployments": {
                    "total": len(deployment_items),
                    "items": deployment_views,
                    "truncated": len(deployment_items) > self.max_items,
                },
                "pods": {
                    "total": len(pod_items),
                    "items": pod_views,
                    "truncated": len(pod_items) > self.max_items,
                },
            },
        }

    @staticmethod
    def _node_view(item: dict[str, Any]) -> dict[str, Any]:
        metadata = item.get("metadata", {})
        labels = metadata.get("labels", {})
        status = item.get("status", {})
        return {
            "name": metadata.get("name"),
            "ready": _condition_status(item, "Ready") == "True",
            "qualified": labels.get("apexfabric.com/qualified") == "true",
            "architecture": labels.get("kubernetes.io/arch"),
            "hardware_profile": labels.get("apexfabric.com/hardware-profile"),
            "camera_streams": {
                "capacity": status.get("capacity", {}).get(
                    "apexfabric.com/camera-streams"
                ),
                "allocatable": status.get("allocatable", {}).get(
                    "apexfabric.com/camera-streams"
                ),
            },
        }

    @staticmethod
    def _deployment_view(item: dict[str, Any]) -> dict[str, Any]:
        metadata = item.get("metadata", {})
        labels = metadata.get("labels", {})
        spec = item.get("spec", {})
        status = item.get("status", {})
        desired = int(spec.get("replicas") or 0)
        ready = int(status.get("readyReplicas") or 0)
        available = int(status.get("availableReplicas") or 0)
        generation = metadata.get("generation")
        observed = status.get("observedGeneration")
        generation_current = (
            generation is None or observed is None or observed >= generation
        )
        return {
            "name": metadata.get("name"),
            "deployment_id": labels.get("apexfabric.com/deployment-id"),
            "application": labels.get("apexfabric.com/application"),
            "desired_replicas": desired,
            "ready_replicas": ready,
            "available_replicas": available,
            "ready": ready >= desired and available >= desired and generation_current,
        }

    @staticmethod
    def _pod_view(item: dict[str, Any]) -> dict[str, Any]:
        metadata = item.get("metadata", {})
        labels = metadata.get("labels", {})
        status = item.get("status", {})
        restarts = sum(
            int(container.get("restartCount") or 0)
            for container in status.get("containerStatuses", [])
        )
        return {
            "name": metadata.get("name"),
            "deployment_id": labels.get("apexfabric.com/deployment-id"),
            "application": labels.get("apexfabric.com/application"),
            "node": item.get("spec", {}).get("nodeName"),
            "phase": status.get("phase", "Unknown"),
            "ready": _pod_ready(item),
            "restart_count": restarts,
        }


__all__ = ["ClusterStatusReader", "MAX_CLUSTER_ITEMS"]
