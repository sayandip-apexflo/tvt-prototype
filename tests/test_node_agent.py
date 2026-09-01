import json
import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apexfabric.solution_management.renderer import reconcile, render, revision


class FakeResult:
    def __init__(self, stdout=""):
        self.stdout = stdout


class FakeKubectl:
    def __init__(self):
        self.calls = []

    def run(self, *args, input_text=None, check=True):
        self.calls.append((args, input_text))
        if args[0] == "apply":
            return FakeResult("objects applied\n")
        if args[0] == "get" and args[1].startswith("deployments,"):
            return FakeResult(json.dumps({"items": [{"kind": "ConfigMap", "metadata": {"name": "obsolete"}}]}))
        if args[0] == "get" and args[1] == "deployments":
            return FakeResult(json.dumps({"items": [{"metadata": {"name": "traffic-demo-local-traffic-cv"}, "spec": {"replicas": 1}, "status": {"readyReplicas": 1, "availableReplicas": 1}}]}))
        return FakeResult()


class NodeAgentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = yaml.safe_load((ROOT / "tests" / "fixtures" / "traffic-bundle.yaml").read_text())
        cls.objects = render(cls.bundle, "apexfabric")

    def find(self, kind):
        return next(item for item in self.objects if item["kind"] == kind)

    def test_renders_all_managed_resource_kinds(self):
        self.assertEqual({item["kind"] for item in self.objects}, {
            "Namespace", "ConfigMap", "Secret", "Deployment", "Service", "NetworkPolicy"
        })

    def test_deployment_delegates_placement_to_kubernetes(self):
        deployment = self.find("Deployment")
        pod = deployment["spec"]["template"]["spec"]
        self.assertNotIn("nodeName", pod)
        self.assertNotIn("schedulerName", pod)
        self.assertIn("nodeAffinity", pod["affinity"])
        container = pod["containers"][0]
        self.assertEqual(container["resources"]["requests"]["apexfabric.com/metis"], "1")
        self.assertEqual(container["resources"]["requests"]["apexfabric.com/decoder"], "2")
        env = {item["name"]: item for item in container["env"]}
        self.assertEqual(env["NODE_NAME"]["valueFrom"]["fieldRef"]["fieldPath"], "spec.nodeName")
        self.assertEqual(env["POD_NAME"]["valueFrom"]["fieldRef"]["fieldPath"], "metadata.name")

    def test_configuration_secret_probes_volumes_and_security_exist(self):
        config = self.find("ConfigMap")
        self.assertEqual(json.loads(config["data"]["application.json"])["confidence_threshold"], 0.5)
        secret = self.find("Secret")
        self.assertIn("demo-api-token", secret["stringData"])
        container = self.find("Deployment")["spec"]["template"]["spec"]["containers"][0]
        self.assertIn("readinessProbe", container)
        self.assertIn("livenessProbe", container)
        self.assertIn("startupProbe", container)
        self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
        self.assertEqual({item["name"] for item in container["volumeMounts"]}, {"configuration", "secrets"})

    def test_camera_labels_can_be_external_qualification_concern(self):
        bundle = json.loads(json.dumps(self.bundle))
        bundle["applications"][0]["placement"]["requires_camera_labels"] = False
        deployment = next(item for item in render(bundle, "apexfabric") if item["kind"] == "Deployment")
        expressions = deployment["spec"]["template"]["spec"]["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"][0]["matchExpressions"]
        self.assertFalse(any(item["key"].startswith("cameras.apexfabric.com/") for item in expressions))

    def test_intel_285h_profile_generates_retained_storage_and_reviewed_device_access(self):
        bundle = yaml.safe_load((ROOT / "solution-packs/traffic/traffic-pilot-people-285h.yaml").read_text())
        objects = render(bundle, "apexfabric")
        claim = next(item for item in objects if item["kind"] == "PersistentVolumeClaim")
        self.assertEqual(claim["metadata"]["name"], "traffic-pilot-people-285h-pipeline-data")
        self.assertEqual(claim["metadata"]["annotations"]["apexfabric.com/retention-policy"], "retain")
        self.assertEqual(claim["spec"]["storageClassName"], "local-path")
        self.assertEqual(claim["spec"]["resources"]["requests"]["storage"], "2Gi")

        deployment = next(item for item in objects if item["kind"] == "Deployment")
        self.assertEqual(deployment["spec"]["strategy"], {"type": "Recreate"})
        self.assertEqual(deployment["spec"]["replicas"], 1)
        self.assertEqual(deployment["spec"]["revisionHistoryLimit"], 3)
        pod = deployment["spec"]["template"]["spec"]
        self.assertTrue(pod["hostIPC"])
        self.assertEqual(pod["terminationGracePeriodSeconds"], 60)
        self.assertEqual(pod["securityContext"]["seccompProfile"], {"type": "Unconfined"})
        container = pod["containers"][0]
        self.assertEqual(container["securityContext"], {"privileged": True})
        mounts = {item["mountPath"]: item["name"] for item in container["volumeMounts"]}
        self.assertEqual(mounts["/data"], "persistent-data")
        self.assertEqual(mounts["/dev"], "host-devices")
        self.assertNotIn("apexfabric.com/metis", container["resources"]["requests"])
        expressions = pod["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"][0]["matchExpressions"]
        self.assertIn({"key": "apexfabric.com/metis", "operator": "In", "values": ["true"]}, expressions)
        self.assertIn({"key": "apexfabric.com/hardware-profile", "operator": "In", "values": ["intel-285h"]}, expressions)
        self.assertNotIn("prometheus.io/scrape", deployment["spec"]["template"]["metadata"]["annotations"])

    def test_intel_gpu_npu_runtime_renders_capacity_external_inputs_and_devices(self):
        bundle = yaml.safe_load((ROOT / "solution-packs/traffic/traffic-edge-runtime-intel-285h.yaml").read_text())
        objects = render(bundle, "apexfabric")
        claims = {item["metadata"]["name"] for item in objects if item["kind"] == "PersistentVolumeClaim"}
        self.assertEqual(claims, set())
        deployment = next(item for item in objects if item["kind"] == "Deployment")
        self.assertEqual(deployment["spec"]["strategy"], {"type": "Recreate"})
        pod = deployment["spec"]["template"]["spec"]
        self.assertEqual(pod["securityContext"]["supplementalGroups"], [44, 992])
        expressions = pod["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"][0]["matchExpressions"]
        self.assertIn(
            {"key": "apexfabric.com/gpu-npu-ready", "operator": "In", "values": ["true"]},
            expressions,
        )
        container = pod["containers"][0]
        self.assertEqual(container["resources"]["requests"]["apexfabric.com/camera-streams"], "2")
        self.assertEqual(container["resources"]["limits"]["apexfabric.com/camera-streams"], "2")
        self.assertEqual(container["securityContext"], {"privileged": True, "readOnlyRootFilesystem": False})
        mounts = {item["mountPath"]: item for item in container["volumeMounts"]}
        self.assertFalse(mounts["/plans"].get("readOnly", False))
        self.assertTrue(mounts["/configs/desired_state.json"]["readOnly"])
        self.assertNotIn("/models/traffic/openvino", mounts)
        self.assertIn("/dev/dri", mounts)
        self.assertIn("/dev/accel", mounts)
        volumes = {item["name"]: item for item in pod["volumes"]}
        self.assertEqual(volumes["desired-state"]["secret"]["secretName"], "traffic-edge-intel-285h-desired-state")
        self.assertEqual(volumes["desired-state"]["secret"]["defaultMode"], 0o444)
        self.assertEqual(volumes["external-cam4-source"]["secret"]["defaultMode"], 0o444)
        compiler = pod["initContainers"][0]
        self.assertEqual(compiler["name"], "plan-compiler")
        self.assertIn("edge_runtime.agent.edge_agent", compiler["command"])
        compiler_mounts = {item["mountPath"] for item in compiler["volumeMounts"]}
        self.assertIn("/configs/desired_state.json", compiler_mounts)
        self.assertNotIn("/models/traffic/openvino", compiler_mounts)
        self.assertIn("/run/secrets/apexfabric/cam4.rtsp", compiler_mounts)
        self.assertIn("/dev/dri", compiler_mounts)
        self.assertIn("/dev/accel", compiler_mounts)

    def test_lifecycle_stopped_scales_desired_state_to_zero(self):
        bundle = json.loads(json.dumps(self.bundle))
        bundle["applications"][0]["lifecycle"] = {
            "desired_state": "Stopped", "termination_grace_period_seconds": 45,
        }
        deployment = next(item for item in render(bundle, "apexfabric") if item["kind"] == "Deployment")
        self.assertEqual(deployment["spec"]["replicas"], 0)
        self.assertEqual(deployment["spec"]["template"]["spec"]["terminationGracePeriodSeconds"], 45)

    def test_revision_metadata_cannot_be_decoded_as_a_yaml_number(self):
        objects = render(self.bundle, "apexfabric")
        manifest = yaml.safe_dump_all(objects, sort_keys=False)
        decoded = list(yaml.safe_load_all(manifest))
        for item in decoded:
            labels = item.get("metadata", {}).get("labels", {})
            annotations = item.get("metadata", {}).get("annotations", {})
            self.assertTrue(all(isinstance(value, str) for value in labels.values()))
            self.assertTrue(all(isinstance(value, str) for value in annotations.values()))
            if "apexfabric.com/revision" in labels:
                self.assertTrue(labels["apexfabric.com/revision"].startswith("sha256-"))

    def test_prometheus_telemetry_adds_discovery_annotations(self):
        bundle = json.loads(json.dumps(self.bundle))
        bundle["applications"][0]["telemetry"] = {
            "metrics": {"path": "/metrics", "port": "http", "format": "prometheus"},
        }
        deployment = next(item for item in render(bundle, "apexfabric") if item["kind"] == "Deployment")
        annotations = deployment["spec"]["template"]["metadata"]["annotations"]
        self.assertEqual(annotations["prometheus.io/scrape"], "true")
        self.assertEqual(annotations["prometheus.io/path"], "/metrics")
        self.assertEqual(annotations["prometheus.io/port"], "8080")

    def test_bundle_health_and_telemetry_contract_is_discoverable(self):
        bundle = yaml.safe_load((ROOT / "solution-packs/traffic/traffic-edge-runtime-intel-285h.yaml").read_text())
        deployment = next(item for item in render(bundle, "apexfabric") if item["kind"] == "Deployment")
        annotations = deployment["metadata"]["annotations"]
        self.assertEqual(annotations["apexfabric.com/health-path"], "/healthz")
        self.assertEqual(annotations["apexfabric.com/health-port"], "management")
        self.assertEqual(annotations["apexfabric.com/readiness-path"], "/readyz")
        self.assertEqual(annotations["apexfabric.com/readiness-port"], "management")
        self.assertEqual(annotations["apexfabric.com/metrics-path"], "/metrics")
        self.assertEqual(annotations["apexfabric.com/metrics-port"], "management")
        self.assertEqual(annotations["apexfabric.com/metrics-format"], "json")
        self.assertEqual(annotations["apexfabric.com/events-path"], "/events")
        self.assertEqual(annotations["apexfabric.com/events-port"], "management")
        self.assertEqual(annotations["apexfabric.com/events-protocol"], "sse")

    def test_revision_is_deterministic_and_linked(self):
        expected = revision(self.bundle)
        self.assertEqual(expected, revision(yaml.safe_load(yaml.safe_dump(self.bundle))))
        for item in self.objects[1:]:
            self.assertEqual(item["metadata"]["annotations"]["apexfabric.com/revision"], f"sha256:{expected}")

    def test_reconcile_prunes_only_obsolete_managed_kind_and_observes(self):
        client = FakeKubectl()
        report = reconcile(self.bundle, "apexfabric", client)
        self.assertEqual(report["removed"], ["ConfigMap/obsolete"])
        self.assertEqual(report["observed"][0]["ready_replicas"], 1)
        apply = client.calls[0]
        self.assertIn("--server-side", apply[0])
        self.assertTrue(any(call[0][:3] == ("delete", "configmap", "obsolete") for call in client.calls))


if __name__ == "__main__": unittest.main()
