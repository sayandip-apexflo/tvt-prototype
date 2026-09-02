"""Bounded, read-only K3s status collection for the management plane."""

from __future__ import annotations

import json
import re
import subprocess
from typing import Any

from apexfabric.solution_management.renderer import Kubectl


MANAGED_SELECTOR = "app.kubernetes.io/part-of=apexfabric"
MANAGED_BY = "apexfabric-node-agent"
MAX_CLUSTER_ITEMS = 100
MAX_TELEMETRY_BYTES = 128 * 1024
KUBERNETES_REQUEST_TIMEOUT = "5s"
DNS_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")


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
                "services": {"total": 0, "items": []},
                "replica_sets": {"total": 0, "items": []},
                "persistent_volume_claims": {"total": 0, "items": []},
                "events": {"total": 0, "items": []},
            },
        }

    def _optional_items(self, *arguments: str) -> list[dict[str, Any]]:
        """Return an empty bounded view when an optional API/CRD is absent."""
        if self.kubectl is None:
            return []
        try:
            return self._items(self.kubectl.run(*arguments).stdout)
        except (OSError, ValueError, subprocess.CalledProcessError, json.JSONDecodeError):
            return []

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

        supporting = self._optional_items(
            "get",
            "services,replicasets,persistentvolumeclaims",
            "-n",
            self.namespace,
            "-l",
            MANAGED_SELECTOR,
            f"--request-timeout={KUBERNETES_REQUEST_TIMEOUT}",
            "-o",
            "json",
        )
        events = self._optional_items(
            "get",
            "events",
            "-n",
            self.namespace,
            f"--request-timeout={KUBERNETES_REQUEST_TIMEOUT}",
            "-o",
            "json",
        )
        reports = self._optional_items(
            "get",
            "apexnodestatuses.apexfabric.com",
            f"--request-timeout={KUBERNETES_REQUEST_TIMEOUT}",
            "-o",
            "json",
        )
        reports_by_node = {
            item.get("spec", {}).get("nodeName"): item
            for item in reports
            if item.get("spec", {}).get("nodeName")
        }

        deployment_items = [
            item for item in resources if item.get("kind") == "Deployment"
        ]
        pod_items = [item for item in resources if item.get("kind") == "Pod"]
        service_items = [item for item in supporting if item.get("kind") == "Service"]
        replica_set_items = [
            item for item in supporting if item.get("kind") == "ReplicaSet"
        ]
        pvc_items = [
            item for item in supporting if item.get("kind") == "PersistentVolumeClaim"
        ]
        node_views = [
            self._node_view(item, reports_by_node.get(item.get("metadata", {}).get("name")))
            for item in nodes[: self.max_items]
        ]
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
                "services": {
                    "total": len(service_items),
                    "items": [
                        self._service_view(item)
                        for item in service_items[: self.max_items]
                    ],
                    "truncated": len(service_items) > self.max_items,
                },
                "replica_sets": {
                    "total": len(replica_set_items),
                    "items": [
                        self._replica_set_view(item)
                        for item in replica_set_items[: self.max_items]
                    ],
                    "truncated": len(replica_set_items) > self.max_items,
                },
                "persistent_volume_claims": {
                    "total": len(pvc_items),
                    "items": [self._pvc_view(item) for item in pvc_items[: self.max_items]],
                    "truncated": len(pvc_items) > self.max_items,
                },
                "events": {
                    "total": len(events),
                    "items": [
                        self._event_view(item)
                        for item in sorted(
                            events,
                            key=lambda value: value.get("lastTimestamp")
                            or value.get("eventTime")
                            or value.get("metadata", {}).get("creationTimestamp", ""),
                            reverse=True,
                        )[: self.max_items]
                    ],
                    "truncated": len(events) > self.max_items,
                },
            },
        }

    @staticmethod
    def _node_view(
        item: dict[str, Any], report: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        metadata = item.get("metadata", {})
        labels = metadata.get("labels", {})
        status = item.get("status", {})
        report = report or {}
        report_spec = report.get("spec", {})
        report_status = report.get("status", {})
        role_prefix = "node-role.kubernetes.io/"
        return {
            "name": metadata.get("name"),
            "ready": _condition_status(item, "Ready") == "True",
            "qualified": labels.get("apexfabric.com/qualified") == "true",
            "architecture": labels.get("kubernetes.io/arch"),
            "hardware_profile": labels.get("apexfabric.com/hardware-profile"),
            "roles": sorted(
                key.removeprefix(role_prefix)
                for key in labels
                if key.startswith(role_prefix)
            ),
            "qualification_reason": report_status.get("reason"),
            "reporter_observed_at": report_spec.get("observedAt"),
            "capabilities": report_spec.get("capabilities", {}),
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
            "namespace": metadata.get("namespace"),
            "deployment_id": labels.get("apexfabric.com/deployment-id"),
            "application": labels.get("apexfabric.com/application"),
            "desired_replicas": desired,
            "ready_replicas": ready,
            "available_replicas": available,
            "ready": ready >= desired and available >= desired and generation_current,
            "image": next(
                (
                    container.get("image")
                    for container in spec.get("template", {})
                    .get("spec", {})
                    .get("containers", [])
                    if container.get("image")
                ),
                None,
            ),
        }

    @staticmethod
    def _pod_view(item: dict[str, Any]) -> dict[str, Any]:
        metadata = item.get("metadata", {})
        labels = metadata.get("labels", {})
        status = item.get("status", {})
        statuses = [
            *status.get("initContainerStatuses", []),
            *status.get("containerStatuses", []),
        ]
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
            "created_at": metadata.get("creationTimestamp"),
            "containers": [
                {
                    "name": container.get("name"),
                    "ready": bool(container.get("ready")),
                    "restarts": int(container.get("restartCount") or 0),
                    "state": next(iter(container.get("state", {})), "unknown"),
                }
                for container in statuses
            ],
        }

    @staticmethod
    def _service_view(item: dict[str, Any]) -> dict[str, Any]:
        metadata = item.get("metadata", {})
        spec = item.get("spec", {})
        ports = []
        for port in spec.get("ports", []):
            value = str(port.get("port", "?"))
            if port.get("name"):
                value = f"{port['name']}:{value}"
            if port.get("targetPort") is not None:
                value += f" → {port['targetPort']}"
            ports.append(f"{value}/{port.get('protocol', 'TCP')}")
        return {
            "name": metadata.get("name"),
            "type": spec.get("type", "ClusterIP"),
            "cluster_ip": spec.get("clusterIP"),
            "ports": ports,
        }

    @staticmethod
    def _replica_set_view(item: dict[str, Any]) -> dict[str, Any]:
        metadata = item.get("metadata", {})
        status = item.get("status", {})
        return {
            "name": metadata.get("name"),
            "deployment_id": metadata.get("labels", {}).get(
                "apexfabric.com/deployment-id"
            ),
            "desired": int(item.get("spec", {}).get("replicas") or 0),
            "ready": int(status.get("readyReplicas") or 0),
            "available": int(status.get("availableReplicas") or 0),
        }

    @staticmethod
    def _pvc_view(item: dict[str, Any]) -> dict[str, Any]:
        metadata = item.get("metadata", {})
        return {
            "name": metadata.get("name"),
            "phase": item.get("status", {}).get("phase", "Unknown"),
            "capacity": item.get("status", {}).get("capacity", {}).get("storage"),
            "storage_class": item.get("spec", {}).get("storageClassName"),
            "retention": metadata.get("annotations", {}).get(
                "apexfabric.com/retention"
            ),
        }

    @staticmethod
    def _event_view(item: dict[str, Any]) -> dict[str, Any]:
        involved = item.get("involvedObject", {})
        return {
            "type": item.get("type", "Normal"),
            "object": f"{involved.get('kind', 'Object')}/{involved.get('name', 'unknown')}",
            "reason": item.get("reason"),
            "message": item.get("message"),
            "count": int(item.get("count") or 1),
            "last_seen": item.get("lastTimestamp")
            or item.get("eventTime")
            or item.get("metadata", {}).get("creationTimestamp"),
        }

    def telemetry(self, deployment_name: str) -> dict[str, Any]:
        if self.kubectl is None:
            return {"deployment": deployment_name, "available": False, "error": "Kubernetes client is not configured"}
        if not DNS_NAME.fullmatch(deployment_name):
            raise ValueError("deployment name must be a DNS-safe identifier")
        try:
            resource = json.loads(
                self.kubectl.run(
                    "get", "deployment", deployment_name, "-n", self.namespace,
                    f"--request-timeout={KUBERNETES_REQUEST_TIMEOUT}", "-o", "json"
                ).stdout
            )
            labels = resource.get("metadata", {}).get("labels", {})
            if labels.get("app.kubernetes.io/managed-by") != MANAGED_BY:
                raise ValueError("telemetry is restricted to ApexFabric-managed deployments")
            service = json.loads(
                self.kubectl.run(
                    "get", "service", deployment_name, "-n", self.namespace,
                    f"--request-timeout={KUBERNETES_REQUEST_TIMEOUT}", "-o", "json"
                ).stdout
            )
            context = self._telemetry_context(resource, service)
            selector = ",".join(
                f"{key}={value}" for key, value in sorted(
                    resource.get("spec", {}).get("selector", {}).get("matchLabels", {}).items()
                )
            )
            pods = self._items(
                self.kubectl.run(
                    "get", "pods", "-n", self.namespace, "-l", selector,
                    f"--request-timeout={KUBERNETES_REQUEST_TIMEOUT}", "-o", "json"
                ).stdout
            )
        except (OSError, subprocess.CalledProcessError, json.JSONDecodeError, TypeError) as error:
            return {"deployment": deployment_name, "available": False, "error": self._safe_telemetry_error(error)}

        pods.sort(key=lambda item: item.get("metadata", {}).get("creationTimestamp", ""), reverse=True)
        pod = next((item for item in pods if item.get("status", {}).get("phase") == "Running"), pods[0] if pods else None)
        pod_status = self._pod_view(pod) if pod else None
        proxy_base = None
        proxy_error = "no workload Pod exists"
        endpoint_port = next((value.get("port") for value in context["contract"].values() if value.get("path")), None)
        port = context["port_numbers"].get(endpoint_port)
        if pod and isinstance(port, int):
            proxy_base = f"/api/v1/namespaces/{self.namespace}/pods/http:{pod['metadata']['name']}:{port}/proxy"
            proxy_error = ""
        elif pod:
            proxy_error = f"Service port {endpoint_port!r} has no numeric targetPort"

        results: dict[str, Any] = {}
        for name in ("health", "readiness", "metrics"):
            endpoint = context["contract"][name]
            if not endpoint.get("path") or not proxy_base:
                results[name] = None
                continue
            results[name] = self.kubectl.run(
                "get", "--raw", f"{proxy_base}{endpoint['path']}",
                f"--request-timeout={KUBERNETES_REQUEST_TIMEOUT}", check=False
            )

        def payload(result: Any) -> Any:
            if result is None or getattr(result, "returncode", 0) != 0:
                return None
            body = str(getattr(result, "stdout", ""))[:MAX_TELEMETRY_BYTES]
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                return {"raw": body}

        health = payload(results["health"])
        readiness = payload(results["readiness"])
        metrics_result = results["metrics"]
        metrics = ""
        if metrics_result is not None and getattr(metrics_result, "returncode", 0) == 0:
            metrics = str(getattr(metrics_result, "stdout", ""))[:MAX_TELEMETRY_BYTES]
        required = [results[name] for name in ("readiness", "metrics") if results[name] is not None]
        available = bool(required) and all(getattr(result, "returncode", 0) == 0 for result in required)
        failed = next((result for result in results.values() if result is not None and getattr(result, "returncode", 0) != 0), None)
        deployment_status = resource.get("status", {})
        error = "" if available else (
            self._safe_telemetry_error(getattr(failed, "stderr", "")) if failed
            else proxy_error or "workload endpoint unavailable"
        )
        return {
            "deployment": deployment_name,
            "available": available,
            "contract": context["contract"],
            "health": health,
            "readiness": readiness,
            "metrics": metrics,
            "kubernetes": {
                "desired_replicas": resource.get("spec", {}).get("replicas", 0),
                "ready_replicas": deployment_status.get("readyReplicas", 0),
                "available_replicas": deployment_status.get("availableReplicas", 0),
                "conditions": deployment_status.get("conditions", []),
                "pod": pod_status,
            },
            "error": error,
        }

    @staticmethod
    def _telemetry_context(resource: dict[str, Any], service: dict[str, Any]) -> dict[str, Any]:
        annotations = resource.get("metadata", {}).get("annotations", {})
        contract = {
            "health": {"path": annotations.get("apexfabric.com/health-path", "/status"), "port": annotations.get("apexfabric.com/health-port", "http")},
            "readiness": {"path": annotations.get("apexfabric.com/readiness-path", "/status"), "port": annotations.get("apexfabric.com/readiness-port", "http")},
            "metrics": {"path": annotations.get("apexfabric.com/metrics-path", "/metrics"), "port": annotations.get("apexfabric.com/metrics-port", "http"), "format": annotations.get("apexfabric.com/metrics-format", "prometheus")},
            "events": {"path": annotations.get("apexfabric.com/events-path"), "port": annotations.get("apexfabric.com/events-port"), "protocol": annotations.get("apexfabric.com/events-protocol")},
        }
        container_ports = {
            port.get("name"): port.get("containerPort")
            for container in resource.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
            for port in container.get("ports", [])
        }
        port_numbers: dict[Any, Any] = {}
        for item in service.get("spec", {}).get("ports", []):
            target = item.get("targetPort", item.get("port"))
            if isinstance(target, str):
                target = container_ports.get(target, item.get("port"))
            port_numbers[item.get("name")] = target
        available_ports = set(port_numbers)
        for name, endpoint in contract.items():
            if endpoint.get("path") and endpoint.get("port") not in available_ports:
                raise ValueError(f"workload {name} endpoint references a missing Service port")
        return {"contract": contract, "port_numbers": port_numbers}

    @staticmethod
    def _safe_telemetry_error(error: Any) -> str:
        if isinstance(error, subprocess.CalledProcessError):
            return "Kubernetes command failed"
        value = str(error)
        if "://" in value or "@" in value:
            return "workload endpoint unavailable"
        return value[-500:] or "workload endpoint unavailable"


__all__ = ["ClusterStatusReader", "MAX_CLUSTER_ITEMS", "MAX_TELEMETRY_BYTES"]
