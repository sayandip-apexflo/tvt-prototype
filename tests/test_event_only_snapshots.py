import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from apexfabric.control_plane.server import Controller
from tvt_edge.bundles import BundleCamera, catalog_traffic_bundle
from tvt_edge.delivery_metadata import load_delivery_metadata


ROOT = Path(__file__).resolve().parents[1]


class EventOnlySnapshotBundleTests(unittest.TestCase):
    def test_catalog_bundle_disables_periodic_camera_snapshots(self):
        delivery = load_delivery_metadata(
            ROOT / "solution-packs/catalog/tvt-mills-pilot-2026.09.18-v2-ungated-enroll"
        )
        catalog = {
            **delivery,
            "status": "available",
            "image": {
                "registry": "registry.local:5000",
                "repository": delivery["repository"],
                "tag": delivery["tag"],
                "digest": "sha256:" + "a" * 64,
            },
        }
        bundle = catalog_traffic_bundle(
            catalog, "snapshot-test", "edge-01",
            [BundleCamera("camera-01", 5, ("anpr",))],
            inference_mode="cpu-compatible", cpu_request="4", cpu_limit="16",
            memory_request="8Gi", memory_limit="24Gi", state_size="20Gi",
        )
        environment = bundle["applications"][0]["environment"]
        self.assertEqual(environment["TVT_SNAPSHOT_INTERVAL_S"], "0")
        self.assertEqual(environment["SOLUTION_PACK"], "tvt-mills-pilot")


SSE_SCRIPT = """
import json, sys, time
for event in (
    {"event_type": "camera_snapshot_event", "camera_id": "cam-01", "snapshot_url": "/snapshots/frame.jpg"},
    {"event_type": "face_detection_event", "camera_id": "cam-01", "snapshot_url": "/snapshots/face.jpg"},
):
    sys.stdout.write("data: " + json.dumps(event) + "\\n\\n")
sys.stdout.flush()
time.sleep(1)
"""


class FakeRunner:
    def run(self, command, timeout=900, check=True, input_text=None):
        class Result:
            returncode = 0
            stderr = ""
            stdout = "ok"
        return Result()


class EventOnlySnapshotCollectorTests(unittest.TestCase):
    def test_collector_drops_camera_snapshot_events(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            controller = Controller(Path(tmpdir), FakeRunner())
            controller.telemetry_targets = {"tvt-mills-demo"}
            ingested: list[dict] = []
            fetched: list[dict] = []

            def ingest(name, payload):
                ingested.append(payload)
                return len(ingested)

            def submit(function, event_id, payload, fetcher):
                fetched.append(payload)

            with patch.object(controller, "workload_event_command", return_value=[sys.executable, "-c", SSE_SCRIPT]), \
                    patch.object(controller.telemetry, "ingest", side_effect=ingest), \
                    patch.object(controller.telemetry_snapshot_executor, "submit", side_effect=submit), \
                    patch.object(controller.telemetry_stop, "wait", side_effect=lambda timeout=None: controller.telemetry_stop.set()):
                controller._collect_workload_events("tvt-mills-demo")
            controller.telemetry_snapshot_executor.shutdown(wait=False)

        self.assertEqual([event["event_type"] for event in ingested], ["face_detection_event"])
        self.assertEqual([event["event_type"] for event in fetched], ["face_detection_event"])


if __name__ == "__main__":
    unittest.main()
