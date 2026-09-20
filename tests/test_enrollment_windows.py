import json
import tempfile
import unittest
from pathlib import Path

from apexfabric.control_plane.server import Controller


class StatefulRuntimeConfigurationRunner:
    """Tracks the last-applied ConfigMap so successive Controller calls
    (start_enrollment then stop_enrollment) see each other's writes, unlike
    test_control_plane.py's RuntimeConfigurationRunner fixture, which always
    returns its constructor's fixed snapshot."""

    def __init__(self, desired_state):
        self.desired_state = desired_state
        self.calls: list[list[str]] = []
        self.inputs: list[str | None] = []

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
                "apexfabric.com/deployment-id": "tvt-mills-demo",
            }}})
        elif command[2:4] == ["get", "configmap"]:
            Result.stdout = json.dumps({"data": {"desired_state.json": json.dumps(self.desired_state)}})
        elif command[:2] == ["k3s", "kubectl"] and "apply" in command and input_text:
            applied = json.loads(input_text)
            if applied.get("kind") == "ConfigMap":
                self.desired_state = json.loads(applied["data"]["desired_state.json"])
        return Result()


def desired_state(camera_apps=("face_recognition", "anpr")):
    return {
        "edge_id": "intel-box-01",
        "revision": 1,
        "cameras": [{
            "camera_id": "cam-1",
            "source": "file:/run/secrets/apexfabric/cam-1.rtsp",
            "solution_pack": "tvt-mills-pilot",
            "fps": 8,
            "apps": list(camera_apps),
            "config": {"lines": [{
                "id": "cam-1_entry", "name": "cam-1 gate (entry)", "type": "line",
                "points": [[0.1, 0.5], [0.9, 0.5]], "accepted": ["A->B"],
            }]},
        }],
    }


class EnrollmentWindowTests(unittest.TestCase):
    def controller(self, runner):
        directory = tempfile.mkdtemp()
        self.addCleanup(lambda: None)
        return Controller(Path(directory), runner)

    def test_start_then_stop_round_trips_apps_and_config(self):
        runner = StatefulRuntimeConfigurationRunner(desired_state())
        controller = self.controller(runner)
        started = controller.start_enrollment({"log": []}, {
            "name": "tvt-mills-demo-runtime", "camera_id": "cam-1",
        })
        self.assertEqual(started["revision"], 2)
        self.assertIsNone(started["expires_at"])
        current = controller.runtime_configuration("tvt-mills-demo-runtime")
        camera = current["desired_state"]["cameras"][0]
        self.assertEqual(camera["apps"], ["face_enrollment"])
        self.assertEqual(camera["config"], {})

        stopped = controller.stop_enrollment({"log": []}, {
            "name": "tvt-mills-demo-runtime", "window_id": started["window_id"],
        })
        self.assertEqual(stopped["revision"], 3)
        reverted = controller.runtime_configuration("tvt-mills-demo-runtime")
        camera = reverted["desired_state"]["cameras"][0]
        self.assertEqual(camera["apps"], ["face_recognition", "anpr"])
        self.assertEqual(camera["config"]["lines"][0]["id"], "cam-1_entry")

        windows = controller.enrollment_windows_for("tvt-mills-demo-runtime")
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0]["status"], "reverted")

    def test_concurrent_start_on_the_same_camera_is_rejected(self):
        runner = StatefulRuntimeConfigurationRunner(desired_state())
        controller = self.controller(runner)
        controller.start_enrollment({"log": []}, {"name": "tvt-mills-demo-runtime", "camera_id": "cam-1"})
        with self.assertRaisesRegex(ValueError, "already in enrollment mode"):
            controller.start_enrollment({"log": []}, {"name": "tvt-mills-demo-runtime", "camera_id": "cam-1"})

    def test_camera_without_face_recognition_is_rejected(self):
        runner = StatefulRuntimeConfigurationRunner(desired_state(camera_apps=("anpr",)))
        controller = self.controller(runner)
        with self.assertRaisesRegex(ValueError, "does not run face_recognition"):
            controller.start_enrollment({"log": []}, {"name": "tvt-mills-demo-runtime", "camera_id": "cam-1"})

    def test_stopping_an_already_reverted_window_is_rejected(self):
        runner = StatefulRuntimeConfigurationRunner(desired_state())
        controller = self.controller(runner)
        started = controller.start_enrollment({"log": []}, {"name": "tvt-mills-demo-runtime", "camera_id": "cam-1"})
        controller.stop_enrollment({"log": []}, {"name": "tvt-mills-demo-runtime", "window_id": started["window_id"]})
        with self.assertRaisesRegex(ValueError, "is not active"):
            controller.stop_enrollment({"log": []}, {"name": "tvt-mills-demo-runtime", "window_id": started["window_id"]})

    def test_expired_window_auto_reverts_via_sweep(self):
        runner = StatefulRuntimeConfigurationRunner(desired_state())
        controller = self.controller(runner)
        started = controller.start_enrollment({"log": []}, {
            "name": "tvt-mills-demo-runtime", "camera_id": "cam-1", "duration_seconds": 1,
        })
        with controller.telemetry.lock, controller.telemetry._connect() as connection:
            connection.execute(
                "UPDATE enrollment_windows SET expires_at = 0 WHERE id = ?", (started["window_id"],)
            )
        controller._revert_expired_enrollment_windows()
        reverted = controller.runtime_configuration("tvt-mills-demo-runtime")
        camera = reverted["desired_state"]["cameras"][0]
        self.assertEqual(camera["apps"], ["face_recognition", "anpr"])
        windows = controller.enrollment_windows_for("tvt-mills-demo-runtime")
        self.assertEqual(windows[0]["status"], "reverted")


if __name__ == "__main__":
    unittest.main()
