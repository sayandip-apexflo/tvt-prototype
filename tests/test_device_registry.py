import tempfile
import unittest
from pathlib import Path

from apexfabric.control_plane.device_registry import DeviceRegistry


class DeviceRegistryTests(unittest.TestCase):
    def test_devices_are_isolated_by_site_and_metadata_round_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "devices.sqlite3"
            first = DeviceRegistry(database, "site-one")
            second = DeviceRegistry(database, "site-two")
            first.upsert("camera-1", "camera", "Entrance", "configured", {"has_source": True})
            second.upsert("camera-1", "camera", "Other entrance", "configured", {"has_source": True})
            self.assertEqual(first.list("camera")[0]["display_name"], "Entrance")
            self.assertEqual(second.list("camera")[0]["display_name"], "Other entrance")
            self.assertNotIn("rtsp", str(first.list()))

    def test_box_sync_preserves_record_and_marks_missing_box_offline(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = DeviceRegistry(Path(directory) / "devices.sqlite3", "site-one")
            registry.upsert("compute-a", "box", "compute-a", "online", {"role": "edge"}, seen=True)
            created = registry.list("box")[0]["created_at"]
            registry.upsert("compute-a", "box", "compute-a", "online", {"role": "edge", "qualified": True}, seen=True)
            self.assertEqual(registry.list("box")[0]["created_at"], created)
            registry.mark_unseen_boxes_offline(set())
            self.assertEqual(registry.list("box")[0]["status"], "offline")

    def test_camera_can_be_deleted_without_affecting_boxes(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = DeviceRegistry(Path(directory) / "devices.sqlite3", "site-one")
            registry.upsert("camera-1", "camera", "Entrance", "configured", {})
            registry.upsert("compute-a", "box", "compute-a", "online", {}, seen=True)
            self.assertTrue(registry.delete("camera-1", "camera"))
            self.assertEqual(registry.list("camera"), [])
            self.assertEqual(len(registry.list("box")), 1)


if __name__ == "__main__":
    unittest.main()
