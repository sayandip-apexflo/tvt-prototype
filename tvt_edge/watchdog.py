"""Fixed, bounded K3s API recovery for the root-owned host watchdog."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable


STATE_PATH = Path("/var/lib/tvt-k3s-watchdog/state.json")
LOCK_PATH = Path("/run/tvt-k3s-watchdog/lock")
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
FAILURE_THRESHOLD_SECONDS = 120
COOLDOWN_SECONDS = 600

SERVICE_ACTIVE_COMMAND = ("/usr/bin/systemctl", "is-active", "--quiet", "k3s.service")
API_READY_COMMAND = (
    "/usr/local/bin/k3s",
    "kubectl",
    "get",
    "--raw=/readyz",
    "--request-timeout=5s",
)
RESTART_COMMAND = ("/usr/bin/systemctl", "restart", "k3s.service")

CHECK_RESULTS = frozenset({"healthy", "unhealthy", "service_inactive"})
ACTION_RESULTS = frozenset({"succeeded", "failed"})


def _new_state(boot_id: str) -> dict[str, Any]:
    return {
        "version": 1,
        "boot_id": boot_id,
        "failure_since": None,
        "cooldown_until": None,
        "last_check_at": None,
        "last_check_result": None,
        "last_success_at": None,
        "last_action_at": None,
        "last_action": None,
        "last_action_result": None,
        "checks_total": {result: 0 for result in sorted(CHECK_RESULTS)},
        "actions_total": {
            "restart": {result: 0 for result in sorted(ACTION_RESULTS)}
        },
    }


def _nonnegative_number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return float(value)
    return None


def read_watchdog_state(path: Path = STATE_PATH) -> dict[str, Any] | None:
    """Read only the bounded fields the UI and metrics are allowed to expose."""

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict) or document.get("version") != 1:
        return None

    check_result = document.get("last_check_result")
    action = document.get("last_action")
    action_result = document.get("last_action_result")
    if check_result not in CHECK_RESULTS | {None}:
        return None
    if action not in {"restart", None} or action_result not in ACTION_RESULTS | {None}:
        return None

    checks = document.get("checks_total")
    actions = document.get("actions_total")
    if not isinstance(checks, dict) or not isinstance(actions, dict):
        return None
    restart_actions = actions.get("restart")
    if not isinstance(restart_actions, dict):
        return None

    safe_checks: dict[str, int] = {}
    safe_actions: dict[str, int] = {}
    for result in CHECK_RESULTS:
        value = checks.get(result, 0)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return None
        safe_checks[result] = value
    for result in ACTION_RESULTS:
        value = restart_actions.get(result, 0)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return None
        safe_actions[result] = value

    return {
        "version": 1,
        "failure_since": _nonnegative_number(document.get("failure_since")),
        "cooldown_until": _nonnegative_number(document.get("cooldown_until")),
        "last_check_at": _nonnegative_number(document.get("last_check_at")),
        "last_check_result": check_result,
        "last_success_at": _nonnegative_number(document.get("last_success_at")),
        "last_action_at": _nonnegative_number(document.get("last_action_at")),
        "last_action": action,
        "last_action_result": action_result,
        "checks_total": safe_checks,
        "actions_total": {"restart": safe_actions},
    }


class WatchdogStatusReader:
    """Convert the root-owned state file into a safe management-plane view."""

    def __init__(self, path: Path = STATE_PATH) -> None:
        self.path = path

    def snapshot(self) -> dict[str, Any]:
        state = read_watchdog_state(self.path)
        if state is None:
            return {"status": "unconfigured"}
        result = state["last_check_result"]
        return {
            "status": "healthy" if result == "healthy" else "degraded",
            **state,
        }


class K3sWatchdog:
    """Run one fixed health check and, when permitted, one fixed restart."""

    def __init__(
        self,
        *,
        state_path: Path = STATE_PATH,
        boot_id_path: Path = BOOT_ID_PATH,
        now: Callable[[], float] = time.time,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.state_path = state_path
        self.boot_id_path = boot_id_path
        self.now = now
        self.run_command = run

    def _boot_id(self) -> str:
        try:
            return self.boot_id_path.read_text(encoding="ascii").strip()
        except OSError:
            return "unknown"

    def _load(self, boot_id: str) -> dict[str, Any]:
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return _new_state(boot_id)
        if not isinstance(state, dict) or state.get("version") != 1:
            return _new_state(boot_id)

        clean = _new_state(boot_id)
        checks = state.get("checks_total", {})
        restart_actions = state.get("actions_total", {}).get("restart", {})
        for result in CHECK_RESULTS:
            value = checks.get(result, 0) if isinstance(checks, dict) else 0
            clean["checks_total"][result] = (
                value
                if isinstance(value, int)
                and not isinstance(value, bool)
                and value >= 0
                else 0
            )
        for result in ACTION_RESULTS:
            value = (
                restart_actions.get(result, 0)
                if isinstance(restart_actions, dict)
                else 0
            )
            clean["actions_total"]["restart"][result] = (
                value
                if isinstance(value, int)
                and not isinstance(value, bool)
                and value >= 0
                else 0
            )

        if state.get("boot_id") == boot_id:
            for field in (
                "failure_since",
                "cooldown_until",
                "last_check_at",
                "last_success_at",
                "last_action_at",
            ):
                clean[field] = _nonnegative_number(state.get(field))
            if state.get("last_check_result") in CHECK_RESULTS:
                clean["last_check_result"] = state["last_check_result"]
            if state.get("last_action") == "restart":
                clean["last_action"] = "restart"
            if state.get("last_action_result") in ACTION_RESULTS:
                clean["last_action_result"] = state["last_action_result"]
        return clean

    def _write(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".state.", suffix=".json", dir=self.state_path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(state, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_name, 0o640)
            os.replace(temporary_name, self.state_path)
        finally:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass

    def _run(
        self, command: tuple[str, ...], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        return self.run_command(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin"},
        )

    def _check(self) -> str:
        try:
            active = self._run(SERVICE_ACTIVE_COMMAND, 5)
            if active.returncode != 0:
                return "service_inactive"
            readiness = self._run(API_READY_COMMAND, 10)
        except (OSError, subprocess.TimeoutExpired):
            return "unhealthy"
        return (
            "healthy"
            if readiness.returncode == 0 and readiness.stdout.strip() == "ok"
            else "unhealthy"
        )

    def run_once(self) -> dict[str, Any]:
        now = max(0.0, float(self.now()))
        state = self._load(self._boot_id())
        result = self._check()
        state["last_check_at"] = now
        state["last_check_result"] = result
        state["checks_total"][result] += 1

        if result == "healthy":
            state["failure_since"] = None
            state["last_success_at"] = now
        elif result == "service_inactive":
            # Ordinary process recovery belongs to k3s.service, not this watchdog.
            state["failure_since"] = None
        else:
            failure_since = state.get("failure_since")
            if failure_since is None or failure_since > now:
                failure_since = now
                state["failure_since"] = now
            cooldown_until = state.get("cooldown_until") or 0.0
            if (
                now - failure_since >= FAILURE_THRESHOLD_SECONDS
                and now >= cooldown_until
            ):
                try:
                    restarted = self._run(RESTART_COMMAND, 120)
                    action_result = "succeeded" if restarted.returncode == 0 else "failed"
                except (OSError, subprocess.TimeoutExpired):
                    action_result = "failed"
                state["last_action_at"] = now
                state["last_action"] = "restart"
                state["last_action_result"] = action_result
                state["actions_total"]["restart"][action_result] += 1
                state["cooldown_until"] = now + COOLDOWN_SECONDS

        self._write(state)
        return state


def main(argv: list[str] | None = None) -> int:
    """Run once. Deliberately reject all command-line control input."""

    arguments = sys.argv[1:] if argv is None else argv
    if arguments:
        print("tvt-k3s-watchdog accepts no arguments", file=sys.stderr)
        return 2
    lock_descriptor = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        state = K3sWatchdog().run_once()
    finally:
        os.close(lock_descriptor)
    result = state["last_check_result"]
    action_occurred = state["last_action_at"] == state["last_check_at"]
    action = state["last_action"] if action_occurred else "none"
    action_result = state["last_action_result"] if action_occurred else "none"
    print(f"k3s_watchdog check={result} action={action} result={action_result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
