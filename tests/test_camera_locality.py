import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apexfabric.solution_management.camera_locality import camera_affinity, label_key


class CameraLocalityTests(unittest.TestCase):
    def test_each_camera_becomes_required_affinity(self):
        affinity = camera_affinity(["camera-02", "camera-01", "camera-01"])
        expressions = affinity["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"][0]["matchExpressions"]
        self.assertEqual([item["key"] for item in expressions], [
            "cameras.apexfabric.com/camera-01", "cameras.apexfabric.com/camera-02"
        ])
        self.assertTrue(all(item["operator"] == "In" and item["values"] == ["true"] for item in expressions))

    def test_invalid_camera_id_is_rejected(self):
        with self.assertRaises(ValueError):
            label_key("Camera 01")

if __name__ == "__main__":
    unittest.main()
