import json
import tempfile
import time
import unittest
from pathlib import Path

from apexfabric.control_plane.server import Controller, DEMO_DEPLOYMENTS


class FakeRunner:
    def __init__(self):
        self.calls = []
        self.inputs = []

    def run(self, command, timeout=900, check=True, input_text=None):
        self.calls.append(command)
        self.inputs.append(input_text)
        class Result:
            returncode = 0
            stderr = ""
            stdout = '{"observed": [], "applied": [], "removed": []}' if "node-agent.sh" in command[0] else "ok"
        return Result()


class WorkloadRunner(FakeRunner):
    def __init__(self, managed=True, contract=True):
        super().__init__()
        self.managed = managed
        self.contract = contract

    def run(self, command, timeout=900, check=True):
        self.calls.append(command)
        class Result:
            returncode = 0
            stderr = ""
            stdout = "ok"
        if command[2:4] == ["get", "deployment"]:
            annotations = {}
            if self.contract:
                annotations = {
                    "apexfabric.com/health-path": "/healthz", "apexfabric.com/health-port": "management",
                    "apexfabric.com/readiness-path": "/readyz", "apexfabric.com/readiness-port": "management",
                    "apexfabric.com/metrics-path": "/metrics", "apexfabric.com/metrics-port": "management",
                    "apexfabric.com/metrics-format": "json",
                    "apexfabric.com/events-path": "/events", "apexfabric.com/events-port": "management",
                    "apexfabric.com/events-protocol": "sse",
                }
            Result.stdout = json.dumps({
                "metadata": {
                    "labels": {"app.kubernetes.io/managed-by": "apexfabric-node-agent" if self.managed else "someone-else"},
                    "annotations": annotations,
                },
                "spec": {
                    "replicas": 1,
                    "selector": {"matchLabels": {"apexfabric.com/deployment-id": "demo"}},
                    "template": {"spec": {"containers": [{"name": "runtime", "ports": [{"name": "management", "containerPort": 8080}]}]}},
                },
                "status": {"readyReplicas": 1, "availableReplicas": 1, "conditions": []},
            })
        elif command[2:4] == ["get", "service"]:
            port_name = "management" if self.contract else "http"
            Result.stdout = json.dumps({"spec": {"ports": [{"name": port_name, "port": 8080, "targetPort": port_name}]}})
        elif command[2:4] == ["get", "pods"]:
            Result.stdout = json.dumps({"items": [{
                "metadata": {"name": "demo-pod", "creationTimestamp": "2026-08-20T00:00:00Z"},
                "spec": {"nodeName": "compute-a"},
                "status": {"phase": "Running", "containerStatuses": [{"name": "runtime", "ready": True, "restartCount": 0, "state": {"running": {}}}]},
            }]})
        elif command[2:4] == ["get", "--raw"]:
            if command[-1].endswith("/healthz"):
                Result.stdout = json.dumps({"status": "ok", "child_running": True})
            elif command[-1].endswith("/readyz"):
                Result.stdout = json.dumps({"ready": True, "configured_cameras": 2})
            elif command[-1].endswith("/status"):
                Result.stdout = json.dumps({"node_name": "orin", "pipeline_state": "running", "recent_events": []})
            else:
                Result.stdout = "apexfabric_pipeline_ready 1\n"
        return Result()


class RuntimeConfigurationRunner(FakeRunner):
    def __init__(self, desired_state):
        super().__init__()
        self.desired_state = desired_state

    def run(self, command, timeout=900, check=True, input_text=None):
        self.calls.append(command)
        self.inputs.append(input_text)
        class Result:
            returncode = 0
            stderr = ""
            stdout = "ok"
        if command[2:4] == ["get", "deployment"]:
            Result.stdout = json.dumps({"metadata": {"labels": {
                "app.kubernetes.io/managed-by": "apexfabric-node-agent",
                "apexfabric.com/deployment-id": "traffic-demo",
            }}})
        elif command[2:4] == ["get", "configmap"]:
            Result.stdout = json.dumps({"data": {"desired_state.json": json.dumps(self.desired_state)}})
        return Result()


class ControlPlaneTests(unittest.TestCase):
    def test_product_intent_generates_valid_285h_bundle_and_objects(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = Controller(Path(directory), FakeRunner())
            generated = controller.generate_bundle({
                "deployment_id": "entrance-people", "version": "2.1.0",
                "image_repository": "registry.local/people-pilot", "image_tag": "2.1.0",
                "pull_policy": "IfNotPresent", "cameras": ["entrance-1"], "storage_gib": 8,
            })
            self.assertEqual(generated["bundle"]["deployment_id"], "entrance-people")
            app = generated["bundle"]["applications"][0]
            self.assertEqual(app["placement"]["runtime_profile"], "intel-285h-metis")
            self.assertFalse(app["placement"]["requires_camera_labels"])
            self.assertEqual(app["camera_contract"]["scheduling_mode"], "runtime-connectivity")
            self.assertEqual(app["telemetry"]["metrics"]["format"], "json")
            self.assertEqual(app["persistent_volumes"][0]["size"], "8Gi")
            self.assertIn("PersistentVolumeClaim", {item["kind"] for item in generated["objects"]})
            self.assertEqual(controller.preview_bundle(generated["bundle_yaml"])["bundle"], generated["bundle"])

    def test_product_intent_rejects_unsafe_or_inconsistent_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = Controller(Path(directory), FakeRunner())
            with self.assertRaisesRegex(ValueError, "deployment_id"):
                controller.generate_bundle({"deployment_id": "../../escape"})
            with self.assertRaisesRegex(ValueError, "requests cannot exceed"):
                controller.generate_bundle({"cpu_request": 9, "cpu_limit": 8})
            with self.assertRaisesRegex(ValueError, "confidence"):
                controller.generate_bundle({"confidence": 2})

    def test_product_intent_generates_jetson_event_simulator(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = Controller(Path(directory), FakeRunner())
            generated = controller.generate_bundle({
                "solution_type": "jetson-event-simulator",
                "deployment_id": "jetson-event-demo",
                "image_repository": "registry.local/apexfabric/event-simulator",
                "image_tag": "0.1.0", "cameras": ["demo-camera"],
            })
            app = generated["bundle"]["applications"][0]
            self.assertEqual(app["placement"]["architecture"], "arm64")
            self.assertEqual(app["placement"]["characteristics"]["hardware-profile"], "jetson-orin")
            self.assertEqual(app["telemetry"]["metrics"]["format"], "prometheus")
            self.assertEqual(app["resources"]["cpu"]["request"], "100m")
            self.assertNotIn("PersistentVolumeClaim", {item["kind"] for item in generated["objects"]})

    def test_traffic_runtime_generator_uses_baked_models_and_secret_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = Controller(Path(directory), FakeRunner())
            generated = controller.generate_bundle({
                "solution_type": "traffic-edge-runtime",
                "deployment_id": "traffic-demo", "edge_id": "intel-box-01",
                "image_repository": "registry.local/apexfabric/traffic-edge-runtime",
                "image_tag": "intel-285h-2026.08.20",
                "camera_configuration": [
                    {"camera_id": "traffic-1", "fps": 8, "apps": ["anpr", "vehicle_counting"]},
                    {"camera_id": "traffic-2", "fps": 8, "apps": ["wrong_way"]},
                ],
            })
            app = generated["bundle"]["applications"][0]
            self.assertEqual(app["configuration"]["models_delivery"], "baked-in")
            self.assertEqual(app["configuration"]["inference_mode"], "cpu-compatible")
            self.assertEqual(app["environment"]["VEHICLE_DEVICE"], "CPU")
            self.assertEqual(app["environment"]["PLATE_DEVICE"], "CPU")
            self.assertEqual(app["environment"]["OCR_DEVICE"], "CPU")
            self.assertNotIn("persistent_volumes", app)
            self.assertNotIn("PersistentVolumeClaim", {item["kind"] for item in generated["objects"]})
            self.assertEqual(app["resources"]["camera_streams"], 2)
            self.assertEqual(generated["desired_state"]["cameras"][0]["source"], "file:/run/secrets/apexfabric/traffic-1.rtsp")
            deployment = next(item for item in generated["objects"] if item["kind"] == "Deployment")
            volumes = {volume["name"]: volume for volume in deployment["spec"]["template"]["spec"]["volumes"]}
            self.assertEqual(volumes["desired-state"]["configMap"]["name"], "traffic-demo-desired-state")
            runtime_mount = next(mount for mount in deployment["spec"]["template"]["spec"]["containers"][0]["volumeMounts"] if mount["name"] == "desired-state")
            self.assertEqual(runtime_mount["mountPath"], "/configs")
            self.assertNotIn("subPath", runtime_mount)
            compiler_mounts = {mount["mountPath"] for mount in deployment["spec"]["template"]["spec"]["initContainers"][0]["volumeMounts"]}
            self.assertIn("/run/secrets/apexfabric/traffic-1.rtsp", compiler_mounts)
            self.assertNotIn("/models/traffic/openvino", compiler_mounts)

    def test_traffic_runtime_generator_persists_accelerated_device_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = Controller(Path(directory), FakeRunner())
            generated = controller.generate_bundle({
                "solution_type": "traffic-edge-runtime", "inference_mode": "intel-gpu-npu",
                "camera_configuration": [{"camera_id": "traffic-1", "fps": 8, "apps": ["anpr"]}],
            })
            app = generated["bundle"]["applications"][0]
            self.assertEqual(app["environment"]["VEHICLE_DEVICE"], "GPU")
            self.assertEqual(app["environment"]["PLATE_DEVICE"], "NPU")
            self.assertEqual(app["environment"]["OCR_DEVICE"], "MULTI:GPU,NPU")
            deployment = next(item for item in generated["objects"] if item["kind"] == "Deployment")
            environment = {item["name"]: item.get("value") for item in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
            self.assertEqual(environment["VEHICLE_DEVICE"], "GPU")

            with self.assertRaisesRegex(ValueError, "inference_mode"):
                controller.generate_bundle({
                    "solution_type": "traffic-edge-runtime", "inference_mode": "arbitrary",
                    "camera_configuration": [{"camera_id": "traffic-1", "fps": 8, "apps": ["anpr"]}],
                })

    def test_surveillance_runtime_generator_exposes_apps_and_persistent_state(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = Controller(Path(directory), FakeRunner())
            generated = controller.generate_bundle({
                "solution_type": "surveillance-edge-runtime",
                "deployment_id": "surveillance-demo",
                "image_repository": "registry.local/apexfabric/surveillance-edge-runtime",
                "image_tag": "intel-285h-2026.08.24-v3",
                "storage_gib": 40,
                "camera_configuration": [{
                    "camera_id": "lobby-1", "fps": 8,
                    "apps": ["reid", "face_recognition", "intrusion", "people_counting"],
                    "config": {
                        "zones": {"intrusion": [{"name": "lobby", "poly": [[0.1, 0.1], [0.9, 0.1], [0.5, 0.9]]}]},
                        "lines": {"people_counting": [{"name": "entry", "a": [0.1, 0.5], "b": [0.9, 0.5], "in_side": "right"}]},
                    },
                }],
            })
            app = generated["bundle"]["applications"][0]
            self.assertEqual(generated["desired_state"]["cameras"][0]["solution_pack"], "surveillance")
            self.assertEqual(app["configuration"]["models_root"], "/models/surveillance")
            self.assertEqual(app["environment"], {"SOLUTION_PACK": "surveillance"})
            self.assertEqual(app["persistent_volumes"], [{
                "name": "state", "mount_path": "/state", "size": "40Gi", "storage_class": "local-path",
            }])
            self.assertIn("PersistentVolumeClaim", {item["kind"] for item in generated["objects"]})
            deployment = next(item for item in generated["objects"] if item["kind"] == "Deployment")
            mounts = deployment["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
            self.assertIn("/state", {mount["mountPath"] for mount in mounts})

            with self.assertRaisesRegex(ValueError, "supported surveillance apps"):
                controller.generate_bundle({
                    "solution_type": "surveillance-edge-runtime",
                    "camera_configuration": [{"camera_id": "lobby-1", "apps": ["anpr"]}],
                })

    def test_traffic_runtime_apply_creates_secrets_without_persisting_values(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = FakeRunner()
            controller = Controller(Path(directory), runner)
            generated = controller.generate_bundle({
                "solution_type": "traffic-edge-runtime", "deployment_id": "traffic-demo",
                "camera_configuration": [{"camera_id": "traffic-1", "fps": 8, "apps": ["anpr"]}],
            })
            rtsp = "rtsp://camera.example:8554/traffic1"
            job = {"log": []}
            result = controller.deploy(job, {
                "bundle_yaml": generated["bundle_yaml"],
                "secret_inputs": {"desired_state": generated["desired_state"], "camera_sources": {"traffic-1": rtsp}},
            })
            secret_call = next(index for index, call in enumerate(runner.calls) if call[-3:] == ["apply", "-f", "-"])
            secret_list = json.loads(runner.inputs[secret_call])
            self.assertEqual(secret_list["items"][1]["stringData"]["traffic-1.rtsp"], rtsp)
            self.assertNotIn(rtsp, (Path(directory) / "packages" / "traffic-demo.yaml").read_text())
            self.assertNotIn(rtsp, json.dumps(job))
            self.assertNotIn(rtsp, json.dumps(result))
            self.assertEqual(result["configured_inputs"], ["traffic-demo-desired-state", "traffic-demo-camera-sources"])
            self.assertEqual(secret_list["items"][0]["kind"], "ConfigMap")
            self.assertEqual(secret_list["items"][0]["metadata"]["annotations"]["apexfabric.com/desired-revision"], "1")

    def test_traffic_runtime_apply_requires_exact_camera_secret_values(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = Controller(Path(directory), FakeRunner())
            generated = controller.generate_bundle({
                "solution_type": "traffic-edge-runtime",
                "camera_configuration": [{"camera_id": "traffic-1", "fps": 8, "apps": ["anpr"]}],
            })
            with self.assertRaisesRegex(ValueError, "exactly match"):
                controller.deploy({"log": []}, {
                    "bundle_yaml": generated["bundle_yaml"],
                    "secret_inputs": {"desired_state": generated["desired_state"], "camera_sources": {"wrong-camera": "rtsp://camera/x"}},
                })

    def test_live_runtime_configuration_applies_new_configmap_revision_without_restart(self):
        current = {
            "edge_id": "intel-box-01", "revision": 3,
            "cameras": [{
                "camera_id": "traffic-1", "source": "file:/run/secrets/apexfabric/traffic-1.rtsp",
                "solution_pack": "traffic", "fps": 8, "apps": ["anpr"],
            }],
        }
        updated = json.loads(json.dumps(current))
        updated["revision"] = 4
        updated["cameras"][0]["apps"] = ["anpr", "vehicle_counting"]
        with tempfile.TemporaryDirectory() as directory:
            runner = RuntimeConfigurationRunner(current)
            controller = Controller(Path(directory), runner)
            result = controller.update_runtime_configuration({"log": []}, {
                "name": "traffic-demo-runtime", "desired_state": updated,
            })
            applied = json.loads(runner.inputs[-1])
            self.assertEqual(applied["kind"], "ConfigMap")
            self.assertEqual(applied["metadata"]["annotations"]["apexfabric.com/desired-revision"], "4")
            self.assertFalse(result["pod_restarted"])
            self.assertFalse(any("rollout" in call for call in runner.calls))

            with self.assertRaisesRegex(ValueError, "greater than 3"):
                controller.update_runtime_configuration({"log": []}, {
                    "name": "traffic-demo-runtime", "desired_state": current,
                })

            missing_geometry = json.loads(json.dumps(current))
            missing_geometry["revision"] = 4
            missing_geometry["cameras"][0]["apps"] = ["anpr", "wrong_way"]
            with self.assertRaisesRegex(ValueError, "requires config.lines.wrong_way"):
                controller.update_runtime_configuration({"log": []}, {
                    "name": "traffic-demo-runtime", "desired_state": missing_geometry,
                })

    def test_live_runtime_configuration_removes_disabled_app(self):
        current = {
            "edge_id": "intel-box-01", "revision": 8,
            "cameras": [{
                "camera_id": "traffic-1", "source": "file:/run/secrets/apexfabric/traffic-1.rtsp",
                "solution_pack": "traffic", "fps": 8, "apps": ["anpr", "illegal_parking"],
                "config": {"zones": {"illegal_parking": [{
                    "id": "parking-zone", "name": "parking", "poly": [[0.1, 0.1], [0.8, 0.1], [0.8, 0.8]],
                }]}},
            }],
        }
        updated = json.loads(json.dumps(current))
        updated["revision"] = 9
        updated["cameras"][0]["apps"] = ["anpr"]
        with tempfile.TemporaryDirectory() as directory:
            runner = RuntimeConfigurationRunner(current)
            Controller(Path(directory), runner).update_runtime_configuration({"log": []}, {
                "name": "traffic-demo-runtime", "desired_state": updated,
            })
            applied = json.loads(runner.inputs[-1])
            stored = json.loads(applied["data"]["desired_state.json"])
            self.assertEqual(stored["revision"], 9)
            self.assertEqual(stored["cameras"][0]["apps"], ["anpr"])

    def test_site_identity_is_stable(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = Controller(Path(directory), FakeRunner())
            self.assertEqual(controller.site_id(), controller.site_id())
            self.assertTrue(controller.site_id().startswith("site-"))

    def test_jobs_record_success_and_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = Controller(Path(directory), FakeRunner())
            job_id = controller.submit("test", lambda job: {"value": 1})
            for _ in range(100):
                if controller.jobs[job_id]["state"] == "succeeded":
                    break
                time.sleep(0.01)
            self.assertEqual(controller.jobs[job_id]["result"], {"value": 1})
            record = json.loads((Path(directory) / "audit.jsonl").read_text())
            self.assertEqual(record["action"], "test")

    def test_uploaded_bundle_rejects_path_like_deployment_id(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = Controller(Path(directory), FakeRunner())
            with self.assertRaisesRegex(ValueError, "deployment_id"):
                controller.deploy({"log": []}, {"bundle_yaml": "deployment_id: ../../escape"})
            self.assertFalse((Path(directory) / "packages").exists())

    def test_lifecycle_test_invokes_only_allowlisted_scenario(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = FakeRunner()
            controller = Controller(Path(directory), runner)
            result = controller.lifecycle_test({"log": []}, "pod")
            self.assertEqual(result["scenario"], "pod")
            self.assertEqual(runner.calls[-1][-1], "pod")
            with self.assertRaisesRegex(ValueError, "unsupported"):
                controller.lifecycle_test({"log": []}, "arbitrary-command")

    def test_stop_scales_only_allowlisted_demo_deployments(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = FakeRunner()
            controller = Controller(Path(directory), runner)
            result = controller.demo_control({"log": []}, "stop")
            self.assertEqual(tuple(result["scaled_to_zero"]), DEMO_DEPLOYMENTS)
            scale_calls = [call for call in runner.calls if "scale" in call]
            self.assertEqual(len(scale_calls), len(DEMO_DEPLOYMENTS))
            self.assertTrue(all("--replicas=0" in call for call in scale_calls))
            self.assertFalse(any("node-reporter" in call or "node-status-controller" in call for call in scale_calls))

    def test_demo_control_rejects_unknown_action(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = Controller(Path(directory), FakeRunner())
            with self.assertRaisesRegex(ValueError, "unsupported"):
                controller.demo_control({"log": []}, "delete-everything")

    def test_solution_upgrade_invokes_fixed_acceptance_script(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = FakeRunner()
            controller = Controller(Path(directory), runner)
            controller.solution_upgrade_test({"log": []})
            self.assertTrue(runner.calls[-1][0].endswith("scripts/test-solution-upgrade.sh"))

    def test_workload_actions_are_allowlisted_and_managed_only(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = WorkloadRunner()
            controller = Controller(Path(directory), runner)
            result = controller.workload_action({"log": []}, {
                "name": "traffic-pilot-people-285h-pipeline", "action": "scale", "replicas": 0,
            })
            self.assertEqual(result["action"], "scale")
            self.assertIn("--replicas=0", runner.calls[-1])
            with self.assertRaisesRegex(ValueError, "unsupported"):
                controller.workload_action({"log": []}, {
                    "name": "traffic-pilot-people-285h-pipeline", "action": "exec",
                })

    def test_workload_telemetry_uses_cluster_service_proxy(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = WorkloadRunner()
            result = Controller(Path(directory), runner).workload_telemetry("jetson-event-demo-pipeline")
            self.assertTrue(result["available"])
            self.assertEqual(result["health"]["status"], "ok")
            self.assertTrue(result["readiness"]["ready"])
            self.assertEqual(result["contract"]["events"]["protocol"], "sse")
            self.assertIn("apexfabric_pipeline_ready 1", result["metrics"])
            self.assertEqual(result["kubernetes"]["pod"]["node"], "compute-a")
            self.assertEqual(result["events_url"], "/api/workload-events?name=jetson-event-demo-pipeline")
            raw_calls = [call for call in runner.calls if call[2:4] == ["get", "--raw"]]
            self.assertEqual(len(raw_calls), 3)
            self.assertTrue(all("pods/http:demo-pod:8080/proxy" in call[-1] for call in raw_calls))
            self.assertIn("/events", Controller(Path(directory), runner).workload_event_command("jetson-event-demo-pipeline")[-1])

            legacy = Controller(Path(directory), WorkloadRunner(contract=False)).workload_telemetry("legacy-demo")
            self.assertTrue(legacy["available"])
            self.assertEqual(legacy["contract"]["metrics"]["port"], "http")

            untrusted = Controller(Path(directory), WorkloadRunner(managed=False))
            with self.assertRaisesRegex(ValueError, "restricted"):
                untrusted.workload_action({"log": []}, {
                    "name": "kube-system-component", "action": "restart",
                })

    def test_ui_maps_unmanaged_deployments_by_pod_selector(self):
        javascript = (Path(__file__).resolve().parents[1] / "apexfabric/control_plane/static/enhancements.js").read_text()
        self.assertIn("d.spec?.selector?.matchLabels", javascript)
        self.assertIn("entries.every(([k,v])=>p.metadata?.labels?.[k]===v)", javascript)
        self.assertNotIn(
            "p.metadata?.labels?.['apexfabric.com/deployment-id']===d.metadata?.labels?.['apexfabric.com/deployment-id']",
            javascript,
        )

    def test_node_workloads_separate_solution_deployments_and_system_pods(self):
        javascript = (Path(__file__).resolve().parents[1] / "apexfabric/control_plane/static/enhancements.js").read_text()
        self.assertIn("function systemWorkloadTable(nodeName)", javascript)
        self.assertIn("owner.kind||'Pod'", javascript)
        self.assertIn("panel('Solution workloads'", javascript)
        self.assertIn("<details><summary>System workloads · ${systemCount}</summary>", javascript)
        self.assertIn("S.tab==='workloads'?nodeWorkloads(name)", javascript)

    def test_all_details_preserve_state_across_periodic_refresh(self):
        javascript = (Path(__file__).resolve().parents[1] / "apexfabric/control_plane/static/enhancements.js").read_text()
        self.assertIn("const detailsState=new Map", javascript)
        self.assertIn("document.addEventListener('toggle'", javascript)
        self.assertIn("new MutationObserver(restoreDetails)", javascript)
        self.assertIn("document.querySelectorAll('#app details')", javascript)


if __name__ == "__main__":
    unittest.main()
