"""Fail over ApexFabric workloads that use retained node-local storage."""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from typing import Any, Callable

FAILOVER_POLICY = "apexfabric.com/local-storage-failover"
FAILOVER_AFTER = "apexfabric.com/failover-after-seconds"
ACTIVE_CLAIMS = "apexfabric.com/active-local-claims"


def _ready(node: dict[str, Any]) -> bool:
    conditions = node.get("status", {}).get("conditions", [])
    return any(item.get("type") == "Ready" and item.get("status") == "True" for item in conditions)


def _unavailable_for(node: dict[str, Any], now: float) -> float:
    condition = next((item for item in node.get("status", {}).get("conditions", []) if item.get("type") == "Ready"), {})
    if condition.get("status") == "True":
        return 0
    try:
        changed = datetime.fromisoformat(condition["lastTransitionTime"].replace("Z", "+00:00")).timestamp()
    except (KeyError, TypeError, ValueError):
        return 0
    return max(0, now - changed)


def _matches_requirement(labels: dict[str, str], requirement: dict[str, Any]) -> bool:
    key, operator, values = requirement.get("key"), requirement.get("operator"), requirement.get("values", [])
    if operator == "In":
        return key in labels and labels[key] in values
    if operator == "NotIn":
        return key in labels and labels[key] not in values
    if operator == "Exists":
        return key in labels
    if operator == "DoesNotExist":
        return key not in labels
    return False


def _eligible(node: dict[str, Any], deployment: dict[str, Any]) -> bool:
    if not _ready(node) or node.get("spec", {}).get("unschedulable"):
        return False
    labels = node.get("metadata", {}).get("labels", {})
    if labels.get("apexfabric.com/qualified") != "true":
        return False
    terms = (deployment.get("spec", {}).get("template", {}).get("spec", {}).get("affinity", {})
             .get("nodeAffinity", {}).get("requiredDuringSchedulingIgnoredDuringExecution", {})
             .get("nodeSelectorTerms", []))
    return not terms or any(all(_matches_requirement(labels, req) for req in term.get("matchExpressions", [])) for term in terms)


def _claim_name(original: str, generation: int) -> str:
    suffix = f"-fo-{generation}"
    return f"{original[:63-len(suffix)].rstrip('-')}{suffix}"


def reconcile_local_storage_failover(
    kubectl: Callable[..., Any], namespace: str = "apexfabric", now: float | None = None,
) -> list[dict[str, str]]:
    """Perform one failover pass and return the actions taken."""
    now = time.time() if now is None else now
    nodes = json.loads(kubectl("get", "nodes", "-o", "json").stdout).get("items", [])
    node_by_name = {item["metadata"]["name"]: item for item in nodes}
    deployments = json.loads(kubectl("get", "deployments", "-n", namespace, "-o", "json").stdout).get("items", [])
    pods = json.loads(kubectl("get", "pods", "-n", namespace, "-o", "json").stdout).get("items", [])
    pvcs = json.loads(kubectl("get", "persistentvolumeclaims", "-n", namespace, "-o", "json").stdout).get("items", [])
    pvc_by_name = {item["metadata"]["name"]: item for item in pvcs}
    actions: list[dict[str, str]] = []

    for deployment in deployments:
        annotations = deployment.get("metadata", {}).get("annotations", {})
        if annotations.get(FAILOVER_POLICY) != "retain-and-recreate":
            continue
        grace = max(30, int(annotations.get(FAILOVER_AFTER, "120")))
        labels = deployment.get("metadata", {}).get("labels", {})
        deployment_id, application = labels.get("apexfabric.com/deployment-id"), labels.get("apexfabric.com/application")
        stranded = [pod for pod in pods if
                    pod.get("metadata", {}).get("labels", {}).get("apexfabric.com/deployment-id") == deployment_id and
                    pod.get("metadata", {}).get("labels", {}).get("apexfabric.com/application") == application and
                    pod.get("spec", {}).get("nodeName") in node_by_name and
                    _unavailable_for(node_by_name[pod["spec"]["nodeName"]], now) >= grace]
        if not stranded:
            continue
        failed_nodes = {pod["spec"]["nodeName"] for pod in stranded}
        if not any(node["metadata"]["name"] not in failed_nodes and _eligible(node, deployment) for node in nodes):
            continue

        template = deployment["spec"]["template"]
        replacements: dict[str, str] = {}
        generation = int(now)
        for volume in template.get("spec", {}).get("volumes", []):
            old_name = volume.get("persistentVolumeClaim", {}).get("claimName")
            old_claim = pvc_by_name.get(old_name)
            if not old_claim or old_claim.get("spec", {}).get("storageClassName") != "local-path":
                continue
            new_name = _claim_name(re.sub(r"-fo-\d+$", "", old_name), generation)
            claim = {
                "apiVersion": "v1", "kind": "PersistentVolumeClaim",
                "metadata": {
                    "name": new_name, "namespace": namespace,
                    "labels": old_claim.get("metadata", {}).get("labels", {}),
                    "annotations": {
                        "apexfabric.com/retention-policy": "retain",
                        "apexfabric.com/replaces-claim": old_name,
                        "apexfabric.com/failed-node": sorted(failed_nodes)[0],
                    },
                },
                "spec": {key: value for key, value in old_claim.get("spec", {}).items()
                         if key in {"accessModes", "resources", "storageClassName", "volumeMode"}},
            }
            kubectl("apply", "-f", "-", input_text=json.dumps(claim))
            volume["persistentVolumeClaim"]["claimName"] = new_name
            replacements[volume["name"]] = new_name
        if not replacements:
            continue

        template.setdefault("metadata", {}).setdefault("annotations", {})[ACTIVE_CLAIMS] = json.dumps(replacements, sort_keys=True)
        patch = {
            "metadata": {"annotations": {ACTIVE_CLAIMS: json.dumps(replacements, sort_keys=True)}},
            "spec": {"template": template},
        }
        name = deployment["metadata"]["name"]
        kubectl("patch", "deployment", name, "-n", namespace, "--type=merge", "-p", json.dumps(patch))
        for pod in stranded:
            kubectl("delete", "pod", pod["metadata"]["name"], "-n", namespace,
                    "--grace-period=0", "--force", "--wait=false", check=False)
        actions.append({"deployment": name, "failed_node": sorted(failed_nodes)[0], "claims": json.dumps(replacements, sort_keys=True)})
    return actions
