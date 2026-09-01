#!/usr/bin/env python3
"""ApexFabric desired-state reconciler. Kubernetes remains the scheduler."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
from apexfabric.solution_management.camera_locality import camera_affinity
from apexfabric.solution_management.validation import load_yaml, validate_bundle

MANAGED_BY = "apexfabric-node-agent"
PRUNABLE = {"Deployment", "ConfigMap", "Secret", "Service", "NetworkPolicy"}
INTEL_285H_METIS_PROFILE = "intel-285h-metis"
INTEL_285H_GPU_NPU_PROFILE = "intel-285h-gpu-npu"
INTEL_285H_DEVICE_GROUPS = [44, 992]


def revision(bundle: dict[str, Any]) -> str:
    canonical = json.dumps(bundle, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def metadata(name: str, namespace: str, deployment_id: str, app: str, digest: str) -> dict[str, Any]:
    # A bare hexadecimal digest can resemble YAML scientific notation (for
    # example, ``145230c816e5``). Prefix it so Kubernetes' YAML decoder always
    # receives strings for labels and annotations.
    revision_label = f"sha256-{digest[:12]}"
    revision_annotation = f"sha256:{digest}"
    return {
        "name": name,
        "namespace": namespace,
        "labels": {
            "app.kubernetes.io/managed-by": MANAGED_BY,
            "app.kubernetes.io/part-of": "apexfabric",
            "apexfabric.com/deployment-id": deployment_id,
            "apexfabric.com/application": app,
            "apexfabric.com/revision": revision_label,
        },
        "annotations": {
            "apexfabric.com/deployment-id": deployment_id,
            "apexfabric.com/revision": revision_annotation,
        },
    }


def probe(spec: dict[str, Any]) -> dict[str, Any]:
    result = {"httpGet": {"path": spec["path"], "port": spec["port"]}}
    mapping = {
        "initial_delay_seconds": "initialDelaySeconds", "period_seconds": "periodSeconds",
        "timeout_seconds": "timeoutSeconds", "failure_threshold": "failureThreshold",
    }
    result.update({target: spec[source] for source, target in mapping.items() if source in spec})
    return result


def placement(app: dict[str, Any]) -> dict[str, Any]:
    requirements: list[dict[str, Any]] = []
    configured = app.get("placement", {})
    if configured.get("requires_qualified_node", True):
        requirements.append({"key": "apexfabric.com/qualified", "operator": "In", "values": ["true"]})
    if configured.get("runtime_profile") == INTEL_285H_GPU_NPU_PROFILE:
        requirements.append(
            {"key": "apexfabric.com/gpu-npu-ready", "operator": "In", "values": ["true"]}
        )
    if configured.get("node_class"):
        requirements.append({"key": "apexfabric.com/node-class", "operator": "In", "values": [configured["node_class"]]})
    if configured.get("architecture"):
        requirements.append({"key": "kubernetes.io/arch", "operator": "In", "values": [configured["architecture"]]})
    for key, value in sorted(configured.get("characteristics", {}).items()):
        requirements.append({"key": f"apexfabric.com/{key}", "operator": "In", "values": [value]})
    if configured.get("requires_camera_labels", True):
        camera_expressions = camera_affinity(app.get("cameras", []))["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"][0]["matchExpressions"]
        requirements.extend(camera_expressions)
    return {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [{"matchExpressions": requirements}]}}}


def render(bundle: dict[str, Any], namespace: str) -> list[dict[str, Any]]:
    deployment_id = bundle["deployment_id"]
    digest = revision(bundle)
    objects: list[dict[str, Any]] = [{
        "apiVersion": "v1", "kind": "Namespace",
        "metadata": {"name": namespace, "labels": {"app.kubernetes.io/part-of": "apexfabric"}},
    }]
    for app in bundle["applications"]:
        app_name = app["name"]
        name = f"{deployment_id}-{app_name}"
        base_meta = metadata(name, namespace, deployment_id, app_name, digest)
        config_name, secret_name = f"{name}-config", f"{name}-secret"
        config = {
            "deployment.json": json.dumps(bundle.get("configuration", {}), sort_keys=True),
            "application.json": json.dumps(app.get("configuration", {}), sort_keys=True),
            "cameras.json": json.dumps(app.get("cameras", []), sort_keys=True),
        }
        objects.append({"apiVersion": "v1", "kind": "ConfigMap", "metadata": metadata(config_name, namespace, deployment_id, app_name, digest), "data": config})
        secrets = app.get("secrets", {})
        if secrets:
            objects.append({"apiVersion": "v1", "kind": "Secret", "metadata": metadata(secret_name, namespace, deployment_id, app_name, digest), "type": "Opaque", "stringData": secrets})

        requested = app["resources"]
        runtime_profile = app.get("placement", {}).get("runtime_profile", "standard")
        requests = {"cpu": requested["cpu"]["request"], "memory": requested["memory"]["request"]}
        limits = {"cpu": requested["cpu"]["limit"], "memory": requested["memory"]["limit"]}
        if requested.get("camera_streams", 0):
            requests["apexfabric.com/camera-streams"] = str(requested["camera_streams"])
            limits["apexfabric.com/camera-streams"] = str(requested["camera_streams"])
        for resource, amount in requested["accelerators"].items():
            if amount:
                # The prototype profile passes Metis devices through directly.
                # A production device plugin must replace this exception so the
                # scheduler can account for and exclusively allocate each card.
                if runtime_profile == INTEL_285H_METIS_PROFILE and resource == "metis":
                    continue
                key = f"apexfabric.com/{resource}"
                requests[key] = str(amount)
                limits[key] = str(amount)
        env = [{"name": key, "value": value} for key, value in sorted(app.get("environment", {}).items())]
        for key, secret_key in sorted(app.get("secret_environment", {}).items()):
            env.append({"name": key, "valueFrom": {"secretKeyRef": {"name": secret_name, "key": secret_key}}})
        env.extend([
            {"name": "POD_NAME", "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}}},
            {"name": "POD_NAMESPACE", "valueFrom": {"fieldRef": {"fieldPath": "metadata.namespace"}}},
            {"name": "NODE_NAME", "valueFrom": {"fieldRef": {"fieldPath": "spec.nodeName"}}},
            {"name": "APPLICATION_VERSION", "value": bundle["solution"]["version"]},
            {"name": "APEXFABRIC_DEPLOYMENT_REVISION", "value": digest},
            {"name": "APEXFABRIC_DEPLOYMENT_ID", "value": deployment_id},
            {"name": "APEXFABRIC_SOLUTION_ID", "value": bundle["solution"]["solution_id"]},
            {"name": "APEXFABRIC_APPLICATION", "value": app_name},
        ])
        health = app["health"]
        port_numbers = {item["name"]: item["container_port"] for item in app["ports"]}
        image = app["image"]
        image_reference = f"{image['repository']}@{image['digest']}" if image.get("digest") else f"{image['repository']}:{image['tag']}"
        container = {
            "name": app_name,
            "image": image_reference,
            "imagePullPolicy": app["image"].get("pull_policy", "IfNotPresent"),
            "ports": [{"name": item["name"], "containerPort": item["container_port"], "protocol": item.get("protocol", "TCP")} for item in app["ports"]],
            "resources": {"requests": requests, "limits": limits},
            "env": env,
            "readinessProbe": probe(health["readiness"]),
            "livenessProbe": probe(health["liveness"]),
            "volumeMounts": [{"name": "configuration", "mountPath": "/etc/apexfabric/config", "readOnly": True}],
            "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "runAsNonRoot": True, "capabilities": {"drop": ["ALL"]}},
        }
        if "startup" in health:
            container["startupProbe"] = probe(health["startup"])
        volumes = [{"name": "configuration", "configMap": {"name": config_name}}]
        if secrets:
            container["volumeMounts"].append({"name": "secrets", "mountPath": "/etc/apexfabric/secrets", "readOnly": True})
            volumes.append({"name": "secrets", "secret": {"secretName": secret_name, "defaultMode": 0o400}})
        for volume in app.get("persistent_volumes", []):
            volume_name = f"persistent-{volume['name']}"
            claim_name = f"{name}-{volume['name']}"
            claim_meta = metadata(claim_name, namespace, deployment_id, app_name, digest)
            claim_meta["annotations"]["apexfabric.com/retention-policy"] = "retain"
            claim_spec = {
                "accessModes": ["ReadWriteOnce"],
                "resources": {"requests": {"storage": volume["size"]}},
            }
            if volume.get("storage_class"):
                claim_spec["storageClassName"] = volume["storage_class"]
            objects.append({"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": claim_meta, "spec": claim_spec})
            container["volumeMounts"].append({"name": volume_name, "mountPath": volume["mount_path"]})
            volumes.append({"name": volume_name, "persistentVolumeClaim": {"claimName": claim_name}})
        for mount in app.get("external_mounts", []):
            volume_name = f"external-{mount['name']}"
            source = mount["source"]
            volume_mount = {
                "name": volume_name,
                "mountPath": mount["mount_path"],
                "readOnly": mount["read_only"],
            }
            if source["type"] == "secret":
                item_path = "payload"
                volume_mount["subPath"] = item_path
                volumes.append({
                    "name": volume_name,
                    "secret": {
                        "secretName": source["name"],
                        "items": [{"key": source["key"], "path": item_path}],
                        # The V1 image runs as a non-root UID. The volume is
                        # still mounted read-only and isolated to this Pod.
                        "defaultMode": 0o444,
                    },
                })
            else:
                volumes.append({
                    "name": volume_name,
                    "persistentVolumeClaim": {"claimName": source["name"]},
                })
            container["volumeMounts"].append(volume_mount)
        plan_compiler = app.get("plan_compiler")
        if plan_compiler:
            volumes.extend([
                {
                    "name": "desired-state",
                    "secret": {
                        "secretName": plan_compiler["desired_state_secret"],
                        "items": [{"key": plan_compiler["desired_state_key"], "path": "desired_state.json"}],
                        "defaultMode": 0o444,
                    },
                },
                {"name": "compiled-plans", "emptyDir": {}},
            ])
            # The contracted main entrypoint recompiles and atomically updates
            # generated plan files before launching the solution runtime.
            container["volumeMounts"].append({"name": "compiled-plans", "mountPath": "/plans"})
            # The contracted solution-image entrypoint validates and compiles
            # desired state before it launches the runtime as well.
            container["volumeMounts"].append({
                "name": "desired-state", "mountPath": "/configs/desired_state.json",
                "subPath": "desired_state.json", "readOnly": True,
            })
        selector = {"apexfabric.com/deployment-id": deployment_id, "apexfabric.com/application": app_name}
        lifecycle = app.get("lifecycle", {})
        contract_annotations = {
            "apexfabric.com/health-path": health["liveness"]["path"],
            "apexfabric.com/health-port": health["liveness"]["port"],
            "apexfabric.com/readiness-path": health["readiness"]["path"],
            "apexfabric.com/readiness-port": health["readiness"]["port"],
        }
        telemetry = app.get("telemetry", {})
        metrics = telemetry.get("metrics")
        if metrics:
            contract_annotations.update({
                "apexfabric.com/metrics-path": metrics["path"],
                "apexfabric.com/metrics-port": metrics["port"],
                "apexfabric.com/metrics-format": metrics["format"],
            })
        events = telemetry.get("events")
        if events:
            contract_annotations.update({
                "apexfabric.com/events-path": events["path"],
                "apexfabric.com/events-port": events["port"],
                "apexfabric.com/events-protocol": events["protocol"],
            })
        pod_annotations = {**base_meta["annotations"], **contract_annotations}
        if metrics and metrics["format"] == "prometheus":
            pod_annotations.update({
                "prometheus.io/scrape": "true",
                "prometheus.io/path": metrics["path"],
                "prometheus.io/port": str(port_numbers[metrics["port"]]),
            })
        pod_spec = {
            "automountServiceAccountToken": False, "affinity": placement(app),
            "securityContext": {"runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}},
            "containers": [container], "volumes": volumes,
            "terminationGracePeriodSeconds": lifecycle.get("termination_grace_period_seconds", 30),
        }
        strategy = None
        if runtime_profile == INTEL_285H_METIS_PROFILE:
            container["securityContext"] = {"privileged": True}
            container["volumeMounts"].append({"name": "host-devices", "mountPath": "/dev"})
            volumes.append({"name": "host-devices", "hostPath": {"path": "/dev", "type": "Directory"}})
            pod_spec["securityContext"] = {"seccompProfile": {"type": "Unconfined"}}
            pod_spec["hostIPC"] = True
            strategy = {"type": "Recreate"}
        elif runtime_profile == INTEL_285H_GPU_NPU_PROFILE:
            # Temporary qualified profile for the supplied Intel runtime image.
            # Replace privileged device passthrough with GPU/NPU device plugins
            # before treating this as a production isolation boundary.
            container["securityContext"] = {"privileged": True, "readOnlyRootFilesystem": False}
            container["volumeMounts"].extend([
                {"name": "host-dri", "mountPath": "/dev/dri"},
                {"name": "host-accel", "mountPath": "/dev/accel"},
            ])
            volumes.extend([
                {"name": "host-dri", "hostPath": {"path": "/dev/dri", "type": "Directory"}},
                {"name": "host-accel", "hostPath": {"path": "/dev/accel", "type": "Directory"}},
            ])
            pod_spec["securityContext"] = {
                "seccompProfile": {"type": "Unconfined"},
                "supplementalGroups": INTEL_285H_DEVICE_GROUPS,
            }
            strategy = {"type": "Recreate"}
        if plan_compiler:
            compiler_mounts = [
                {"name": "desired-state", "mountPath": "/configs/desired_state.json", "subPath": "desired_state.json", "readOnly": True},
                {"name": "compiled-plans", "mountPath": "/plans"},
            ]
            compiler_mounts.extend(
                dict(mount)
                for mount in container["volumeMounts"]
                if (mount["mountPath"].startswith("/models/")
                    or mount["mountPath"].startswith("/run/secrets/apexfabric/")
                    or mount["mountPath"] in {"/dev/dri", "/dev/accel"})
            )
            pod_spec["initContainers"] = [{
                "name": "plan-compiler",
                "image": container["image"],
                "imagePullPolicy": container["imagePullPolicy"],
                "command": [
                    "python", "-m", "edge_runtime.agent.edge_agent",
                    "--desired-state", "/configs/desired_state.json",
                    "--output-dir", "/plans",
                    "--models-root", "/models",
                ],
                "resources": {"requests": {"cpu": "100m", "memory": "256Mi"}, "limits": {"cpu": "1", "memory": "1Gi"}},
                "securityContext": container["securityContext"],
                "volumeMounts": compiler_mounts,
            }]
        deployment_meta = {**base_meta, "annotations": {**base_meta["annotations"], **contract_annotations}}
        deployment = {
            "apiVersion": "apps/v1", "kind": "Deployment", "metadata": deployment_meta,
            "spec": {"replicas": 0 if lifecycle.get("desired_state", "Running") == "Stopped" else app.get("replicas", 1), "revisionHistoryLimit": 3, "selector": {"matchLabels": selector}, "template": {
                "metadata": {"labels": {**base_meta["labels"], **selector}, "annotations": pod_annotations},
                "spec": pod_spec,
            }},
        }
        if strategy:
            deployment["spec"]["strategy"] = strategy
        objects.append(deployment)
        if app["ports"]:
            objects.append({
                "apiVersion": "v1", "kind": "Service", "metadata": base_meta,
                "spec": {"selector": selector, "ports": [{"name": p["name"], "port": p["container_port"], "targetPort": p["name"], "protocol": p.get("protocol", "TCP")} for p in app["ports"]]},
            })
            objects.append({
                "apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": base_meta,
                "spec": {
                    "podSelector": {"matchLabels": selector},
                    "policyTypes": ["Ingress"],
                    "ingress": [{
                        # The management endpoints are reached through the
                        # host-based local controller/Kubernetes Pod proxy as
                        # well as in-namespace monitoring. They are still only
                        # Pod-network endpoints: no NodePort or hostPort is
                        # rendered. Production should replace this demo rule
                        # with authenticated in-cluster telemetry collection.
                        "ports": [{"port": p["container_port"], "protocol": p.get("protocol", "TCP")} for p in app["ports"]],
                    }],
                },
            })
    return objects


class Kubectl:
    def __init__(self, command: list[str]): self.command = command
    def run(self, *args: str, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run([*self.command, *args], input=input_text, text=True, capture_output=True, check=check)


def key(obj: dict[str, Any]) -> tuple[str, str]: return obj["kind"], obj["metadata"]["name"]


def reconcile(bundle: dict[str, Any], namespace: str, kubectl: Kubectl, dry_run: bool = False) -> dict[str, Any]:
    desired = render(bundle, namespace)
    if dry_run:
        return {"desired": desired, "applied": [], "removed": [], "observed": []}
    manifest = yaml.safe_dump_all(desired, sort_keys=False)
    applied = kubectl.run("apply", "--server-side", "--field-manager", MANAGED_BY, "-f", "-", input_text=manifest).stdout.strip().splitlines()
    deployment_id = bundle["deployment_id"]
    selector = f"app.kubernetes.io/managed-by={MANAGED_BY},apexfabric.com/deployment-id={deployment_id}"
    observed_result = kubectl.run("get", "deployments,configmaps,secrets,services,networkpolicies,persistentvolumeclaims", "-n", namespace, "-l", selector, "-o", "json")
    observed_objects = json.loads(observed_result.stdout).get("items", [])
    desired_keys = {key(item) for item in desired if item["kind"] in PRUNABLE}
    removed = []
    for item in observed_objects:
        item_key = key(item)
        if item_key[0] in PRUNABLE and item_key not in desired_keys:
            kubectl.run("delete", item_key[0].lower(), item_key[1], "-n", namespace)
            removed.append(f"{item_key[0]}/{item_key[1]}")
    observed = kubectl.run("get", "deployments", "-n", namespace, "-l", f"apexfabric.com/deployment-id={deployment_id}", "-o", "json").stdout
    statuses = [{
        "name": item["metadata"]["name"], "desired_replicas": item.get("spec", {}).get("replicas", 0),
        "ready_replicas": item.get("status", {}).get("readyReplicas", 0),
        "available_replicas": item.get("status", {}).get("availableReplicas", 0),
    } for item in json.loads(observed).get("items", [])]
    return {"revision": revision(bundle), "applied": applied, "removed": removed, "observed": statuses}


def main() -> int:
    parser = argparse.ArgumentParser(description="ApexFabric Node Agent")
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--namespace", default="apexfabric")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=15)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--kubeconfig")
    args = parser.parse_args()
    schema = json.loads((ROOT / "solution-packs/schema/deployment-bundle.schema.json").read_text())
    command = ["kubectl"] + (["--kubeconfig", args.kubeconfig] if args.kubeconfig else [])
    if not args.kubeconfig and Path("/usr/local/bin/k3s").exists(): command = ["k3s", "kubectl"]
    client = Kubectl(command)
    while True:
        try:
            bundle = load_yaml(args.bundle)
            errors = validate_bundle(bundle, schema)
            if errors:
                print(f"INVALID: {args.bundle} ({len(errors)} error(s))", file=sys.stderr)
                for error in errors: print(f"  - {error}", file=sys.stderr)
                if args.once: return 1
            else:
                report = reconcile(bundle, args.namespace, client, args.dry_run)
                print(json.dumps(report, indent=2, sort_keys=True))
        except (OSError, ValueError, subprocess.CalledProcessError, json.JSONDecodeError) as error:
            detail = error.stderr.strip() if isinstance(error, subprocess.CalledProcessError) and error.stderr else str(error)
            print(f"RECONCILE FAILED: {detail}", file=sys.stderr)
            if args.once: return 1
        if args.once: return 0
        time.sleep(max(args.interval, 1))


if __name__ == "__main__": raise SystemExit(main())
