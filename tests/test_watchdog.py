import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from prometheus_client import CollectorRegistry, generate_latest

from tvt_edge.observability.metrics import WatchdogMetricsCollector
from tvt_edge.watchdog import (
    API_READY_COMMAND,
    RESTART_COMMAND,
    SERVICE_ACTIVE_COMMAND,
    K3sWatchdog,
    WatchdogStatusReader,
    main,
)


class FakeCommands:
    def __init__(self, *, active=True, ready=False, restart_succeeds=True):
        self.active = active
        self.ready = ready
        self.restart_succeeds = restart_succeeds
        self.calls = []

    def __call__(self, command, **_kwargs):
        command = tuple(command)
        self.calls.append(command)
        if command == SERVICE_ACTIVE_COMMAND:
            return subprocess.CompletedProcess(command, 0 if self.active else 3, "", "")
        if command == API_READY_COMMAND:
            return subprocess.CompletedProcess(command, 0 if self.ready else 1, "ok\n" if self.ready else "", "")
        if command == RESTART_COMMAND:
            return subprocess.CompletedProcess(command, 0 if self.restart_succeeds else 1, "", "")
        raise AssertionError(f"unexpected command: {command}")


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state_path = self.root / "state.json"
        self.boot_id_path = self.root / "boot_id"
        self.boot_id_path.write_text("boot-1\n", encoding="ascii")
        self.clock = [0.0]

    def tearDown(self):
        self.temporary.cleanup()

    def watchdog(self, commands):
        return K3sWatchdog(
            state_path=self.state_path,
            boot_id_path=self.boot_id_path,
            now=lambda: self.clock[0],
            run=commands,
        )

    def test_restarts_once_after_sustained_failure_then_observes_cooldown(self):
        commands = FakeCommands()
        watchdog = self.watchdog(commands)
        for now in (0, 30, 60, 90):
            self.clock[0] = now
            watchdog.run_once()
        self.assertNotIn(RESTART_COMMAND, commands.calls)

        self.clock[0] = 120
        state = watchdog.run_once()
        self.assertEqual(commands.calls.count(RESTART_COMMAND), 1)
        self.assertEqual(state["cooldown_until"], 720)

        self.clock[0] = 690
        watchdog.run_once()
        self.assertEqual(commands.calls.count(RESTART_COMMAND), 1)
        self.clock[0] = 720
        watchdog.run_once()
        self.assertEqual(commands.calls.count(RESTART_COMMAND), 2)

    def test_healthy_check_resets_failure_and_inactive_service_is_not_restarted(self):
        commands = FakeCommands(active=False)
        watchdog = self.watchdog(commands)
        for now in (0, 300, 900):
            self.clock[0] = now
            state = watchdog.run_once()
        self.assertEqual(state["last_check_result"], "service_inactive")
        self.assertNotIn(RESTART_COMMAND, commands.calls)

        commands.active = True
        commands.ready = True
        self.clock[0] = 930
        state = watchdog.run_once()
        self.assertEqual(state["last_check_result"], "healthy")
        self.assertIsNone(state["failure_since"])
        self.assertEqual(state["last_success_at"], 930)

    def test_reader_and_metrics_expose_only_bounded_persistent_state(self):
        commands = FakeCommands(ready=True)
        self.clock[0] = 42
        self.watchdog(commands).run_once()
        reader = WatchdogStatusReader(self.state_path)
        self.assertEqual(reader.snapshot()["status"], "healthy")

        registry = CollectorRegistry()
        registry.register(WatchdogMetricsCollector(reader))
        rendered = generate_latest(registry).decode()
        self.assertIn("tvt_k3s_api_ready 1.0", rendered)
        self.assertIn('tvt_host_watchdog_checks_total{result="healthy"} 1.0', rendered)

        document = json.loads(self.state_path.read_text())
        document["last_check_result"] = "rtsp://user:secret@camera/live"
        self.state_path.write_text(json.dumps(document))
        self.assertEqual(reader.snapshot(), {"status": "unconfigured"})

    def test_command_line_rejects_all_control_arguments(self):
        self.assertEqual(main(["restart", "another.service"]), 2)


if __name__ == "__main__":
    unittest.main()
