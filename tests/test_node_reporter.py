import os
import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from apexfabric.common.kube import ApiError
from apexfabric.node_management.reporter import reporter


class FakeApi:
    def __init__(self, profile="intel-285h", fail_capacity=False):
        self.calls = []
        self.profile = profile
        self.fail_capacity = fail_capacity
    def get(self, path):
        self.calls.append(("get", path, None))
        return {"metadata": {"labels": {"apexfabric.com/hardware-profile": self.profile}}}
    def patch(self, path, payload, content_type):
        self.calls.append(("patch", path, payload))
        if path.endswith("/status"):
            if self.fail_capacity:
                raise ApiError("HTTP 500 capacity failure")
            return payload
        raise ApiError("HTTP 404")
    def post(self, path, payload): self.calls.append(("post", path, payload)); return payload


class NodeReporterTests(unittest.TestCase):
    def test_reporter_creates_report_after_patch_not_found(self):
        api = FakeApi()
        with patch.object(reporter, "NODE_NAME", "node-01"), patch.object(reporter, "discover", return_value={"hardware": {}}):
            instance = reporter.Reporter(api, {"intel-285h": 30, "jetson-orin": 25})
            payload = instance.report_once()
        self.assertEqual(payload["spec"]["nodeName"], "node-01")
        self.assertEqual([call[0] for call in api.calls], ["patch", "post"])
        self.assertTrue(instance.healthy)
        self.assertIn("apexfabric_node_reporter_up 1", instance.metrics())

    def test_reporter_rejects_missing_node_identity(self):
        with patch.object(reporter, "NODE_NAME", ""):
            with self.assertRaisesRegex(ValueError, "NODE_NAME"):
                reporter.Reporter(FakeApi())

    def test_profile_configuration_has_benchmarked_capacity_values(self):
        path = Path(__file__).resolve().parents[1] / "deploy/config/hardware-profiles.json"
        self.assertEqual(reporter.load_camera_capacities(path), {"intel-285h": 30, "jetson-orin": 25})

    def test_generic_profile_does_not_invent_camera_capacity(self):
        path = Path(__file__).resolve().parents[1] / "deploy/config/hardware-profiles.json"
        self.assertNotIn("generic-amd64", reporter.load_camera_capacities(path))

    def test_capacity_matches_each_hardware_profile(self):
        for profile, expected in (("intel-285h", 30), ("jetson-orin", 25)):
            with self.subTest(profile=profile), patch.object(reporter, "NODE_NAME", "node-01"):
                api = FakeApi(profile)
                instance = reporter.Reporter(api, {"intel-285h": 30, "jetson-orin": 25})
                self.assertEqual(instance.advertise_camera_capacity(), expected)
                status_patch = [call for call in api.calls if call[0] == "patch" and call[1].endswith("/status")][-1]
                self.assertEqual(status_patch[2], {"status": {
                    "capacity": {"apexfabric.com/camera-streams": str(expected)},
                    "allocatable": {"apexfabric.com/camera-streams": str(expected)},
                }})

    def test_each_report_cycle_reapplies_capacity(self):
        api = FakeApi("jetson-orin")
        with patch.object(reporter, "NODE_NAME", "node-01"), patch.object(reporter, "discover", return_value={"hardware": {}}):
            instance = reporter.Reporter(api, {"jetson-orin": 25})
            instance.cycle()
            instance.cycle()
        status_patches = [call for call in api.calls if call[0] == "patch" and call[1].endswith("/status")]
        self.assertEqual(len(status_patches), 2)
        self.assertEqual(instance.capacity_updates, 2)

    def test_capacity_patch_failure_is_logged_without_crashing_reporting(self):
        api = FakeApi("intel-285h", fail_capacity=True)
        output = io.StringIO()
        with patch.object(reporter, "NODE_NAME", "node-01"), patch.object(reporter, "discover", return_value={"hardware": {}}), redirect_stdout(output):
            instance = reporter.Reporter(api, {"intel-285h": 30})
            instance.cycle()
        self.assertTrue(instance.healthy)
        self.assertEqual(instance.cycles, 1)
        self.assertIn("capacity failure", instance.capacity_last_error)
        self.assertIn('"operation": "camera-capacity"', output.getvalue())


if __name__ == "__main__": unittest.main()
