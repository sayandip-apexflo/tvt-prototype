import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

import yaml

from apexfabric.node_management.discovery.discovery import discover
from apexfabric.solution_management.renderer import render
from apexfabric.solution_management.validation import validate_bundle
from tvt_runtime.camera_secrets import build_camera_secret_list, secret_names
from tvt_runtime.cli import main


ROOT = Path(__file__).resolve().parents[1]


class TvtRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle_path = (
            ROOT / "solution-packs/traffic/traffic-edge-runtime-intel-285h.yaml"
        )
        cls.bundle = yaml.safe_load(cls.bundle_path.read_text(encoding="utf-8"))
        cls.schema = json.loads(
            (ROOT / "solution-packs/schema/deployment-bundle.schema.json").read_text(
                encoding="utf-8"
            )
        )
        cls.desired_state = json.loads(
            (
                ROOT
                / "solution-packs/traffic/traffic.desired_state.example.json"
            ).read_text(encoding="utf-8")
        )

    def secret_inputs(self):
        return {
            "desired_state": copy.deepcopy(self.desired_state),
            "camera_sources": {
                "cam4": "rtsp://camera-user:camera-pass@192.0.2.14/live",
                "cam5": "rtsps://camera-user:camera-pass@192.0.2.15/live",
            },
        }

    def test_reference_traffic_pack_validates_unchanged(self):
        self.assertEqual(validate_bundle(self.bundle, self.schema), [])

    def test_renderer_mounts_direct_camera_urls_without_camera_affinity(self):
        deployment = next(
            item for item in render(self.bundle, "apexfabric")
            if item["kind"] == "Deployment"
        )
        pod = deployment["spec"]["template"]["spec"]
        mounts = {
            mount["mountPath"]: mount
            for mount in pod["containers"][0]["volumeMounts"]
        }
        self.assertEqual(
            mounts["/run/secrets/apexfabric/cam4.rtsp"]["subPath"], "payload"
        )
        expressions = pod["affinity"]["nodeAffinity"][
            "requiredDuringSchedulingIgnoredDuringExecution"
        ]["nodeSelectorTerms"][0]["matchExpressions"]
        self.assertFalse(
            any(item["key"].startswith("cameras.apexfabric.com/") for item in expressions)
        )

    def test_direct_camera_inputs_create_only_bundle_named_secrets(self):
        result = build_camera_secret_list(self.bundle, self.secret_inputs())
        self.assertEqual(
            secret_names(result),
            [
                "traffic-edge-intel-285h-desired-state",
                "traffic-edge-intel-285h-camera-sources",
            ],
        )
        sources = result["items"][1]["stringData"]
        self.assertEqual(set(sources), {"cam4.rtsp", "cam5.rtsp"})

    def test_direct_camera_inputs_reject_missing_or_non_rtsp_sources(self):
        inputs = self.secret_inputs()
        inputs["camera_sources"].pop("cam5")
        with self.assertRaisesRegex(ValueError, "exactly match"):
            build_camera_secret_list(self.bundle, inputs)

        inputs = self.secret_inputs()
        inputs["camera_sources"]["cam4"] = "https://camera.invalid/live"
        with self.assertRaisesRegex(ValueError, "valid RTSP URL"):
            build_camera_secret_list(self.bundle, inputs)

    def test_apply_dry_run_validates_secrets_without_printing_them(self):
        with tempfile.TemporaryDirectory() as directory:
            inputs_path = Path(directory) / "inputs.json"
            inputs_path.write_text(json.dumps(self.secret_inputs()), encoding="utf-8")
            with patch("builtins.print") as output:
                result = main(
                    [
                        "apply",
                        str(self.bundle_path),
                        "--secret-inputs",
                        str(inputs_path),
                        "--registry",
                        "registry.local:5000",
                        "--dry-run",
                    ]
                )
            self.assertEqual(result, 0)
            rendered_output = " ".join(str(call) for call in output.call_args_list)
            self.assertNotIn("camera-pass", rendered_output)
            self.assertIn("traffic-edge-intel-285h-camera-sources", rendered_output)

    def test_render_and_apply_require_a_real_registry_address(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(["render", str(self.bundle_path)])

    def test_node_discovery_has_no_gstreamer_contract(self):
        capabilities = discover()
        self.assertIn("va_api", capabilities["decoder"])
        self.assertNotIn("gstreamer", capabilities["decoder"])

    def test_node_management_manifest_has_no_camera_config_gateway_contract(self):
        resources = list(
            yaml.safe_load_all(
                (
                    ROOT / "deploy/k8s/apexfabric-node-management.yaml"
                ).read_text(encoding="utf-8")
            )
        )
        daemon_set = next(item for item in resources if item["kind"] == "DaemonSet")
        pod = daemon_set["spec"]["template"]["spec"]
        self.assertNotIn(
            "camera-config", {volume["name"] for volume in pod.get("volumes", [])}
        )


if __name__ == "__main__":
    unittest.main()
