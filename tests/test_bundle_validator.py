import json
import subprocess
import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apexfabric.solution_management.validation import validate_bundle


class BundleValidatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads((ROOT / "solution-packs/schema/deployment-bundle.schema.json").read_text())

    def test_registry_backed_traffic_fixture_is_valid(self):
        bundle = yaml.safe_load((ROOT / "tests" / "fixtures" / "traffic-bundle.yaml").read_text())
        self.assertEqual(validate_bundle(bundle, self.schema), [])

    def test_invalid_bundle_reports_multiple_paths(self):
        bundle = yaml.safe_load((ROOT / "tests" / "fixtures" / "invalid-bundle.yaml").read_text())
        errors = validate_bundle(bundle, self.schema)
        self.assertGreaterEqual(len(errors), 8)
        rendered = "\n".join(errors)
        self.assertIn("$.api_version", rendered)
        self.assertIn("$.solution", rendered)
        self.assertIn("$.applications[0].resources", rendered)
        self.assertIn("references undefined port", rendered)

    def test_cli_returns_nonzero_and_clear_summary(self):
        result = subprocess.run(
            [str(ROOT / "scripts" / "validate-bundle.sh"), str(ROOT / "tests" / "fixtures" / "invalid-bundle.yaml")],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("INVALID:", result.stderr)
        self.assertIn("error(s)", result.stderr)

    def test_intel_285h_bundle_and_profile_constraints(self):
        bundle = yaml.safe_load((ROOT / "solution-packs/traffic/traffic-pilot-people-285h.yaml").read_text())
        self.assertEqual(validate_bundle(bundle, self.schema), [])

        bundle["applications"][0]["placement"]["architecture"] = "arm64"
        bundle["applications"][0]["persistent_volumes"] = []
        errors = "\n".join(validate_bundle(bundle, self.schema))
        self.assertIn("requires 'amd64'", errors)
        self.assertIn("requires a volume mounted at '/data'", errors)

    def test_intel_gpu_npu_runtime_bundle_is_valid_and_constrained(self):
        bundle = yaml.safe_load((ROOT / "solution-packs/traffic/traffic-edge-runtime-intel-285h.yaml").read_text())
        self.assertEqual(validate_bundle(bundle, self.schema), [])

        app = bundle["applications"][0]
        app["placement"]["architecture"] = "arm64"
        app["resources"]["camera_streams"] = 0
        app["external_mounts"][1]["read_only"] = False
        errors = "\n".join(validate_bundle(bundle, self.schema))
        self.assertIn("intel-285h-gpu-npu requires 'amd64'", errors)
        self.assertIn("requires at least one camera stream", errors)
        self.assertIn("Secret mounts must be read-only", errors)

    def test_runtime_camera_contract_cannot_require_camera_labels(self):
        bundle = yaml.safe_load((ROOT / "tests" / "fixtures" / "traffic-bundle.yaml").read_text())
        app = bundle["applications"][0]
        app["camera_contract"] = {
            "configuration_owner": "application-api", "transport": "rtsp",
            "required_for_readiness": True, "scheduling_mode": "runtime-connectivity",
        }
        errors = "\n".join(validate_bundle(bundle, self.schema))
        self.assertIn("runtime-connectivity cameras must not be used as node labels", errors)

    def test_telemetry_endpoint_must_reference_declared_port(self):
        bundle = yaml.safe_load((ROOT / "tests" / "fixtures" / "traffic-bundle.yaml").read_text())
        bundle["applications"][0]["telemetry"] = {
            "metrics": {"path": "/metrics", "port": "missing", "format": "prometheus"},
        }
        errors = "\n".join(validate_bundle(bundle, self.schema))
        self.assertIn("telemetry.metrics.port: references undefined port", errors)


if __name__ == "__main__":
    unittest.main()
