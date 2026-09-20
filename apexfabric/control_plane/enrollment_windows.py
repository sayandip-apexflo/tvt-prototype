"""Temporary face-enrollment camera reassignment.

tvt-mills-pilot has no dedicated enrollment camera (see docs/contracts/
tvt-mills-v1/README.md): any face_recognition camera can be switched to
`apps: ["face_enrollment"]` for a bounded window, then switched back. This
module is pure bookkeeping -- it snapshots the camera's prior apps/config and
records the window's lifecycle so it can be restored; the actual desired-state
push goes through the existing apexfabric/control_plane/server.py
Controller.update_runtime_configuration() revision-bump path (see
start_enrollment/stop_enrollment there), the same way alerts.py and
reporting.py sit alongside TelemetryStore rather than owning their own
storage or transactions.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

SCHEMA = '''
CREATE TABLE IF NOT EXISTS enrollment_windows (
  id TEXT PRIMARY KEY,
  deployment_id TEXT NOT NULL,
  camera_id TEXT NOT NULL,
  prior_apps TEXT NOT NULL,
  prior_config TEXT NOT NULL,
  started_at REAL NOT NULL,
  expires_at REAL,
  ended_at REAL,
  status TEXT NOT NULL CHECK(status IN ('active','reverted'))
);
CREATE INDEX IF NOT EXISTS enrollment_windows_active ON enrollment_windows(deployment_id, camera_id, status);
'''


def _row(row: Any) -> dict[str, Any]:
    result = dict(row)
    result["prior_apps"] = json.loads(result["prior_apps"])
    result["prior_config"] = json.loads(result["prior_config"])
    return result


def active_window(connection, deployment_id: str, camera_id: str) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT * FROM enrollment_windows WHERE deployment_id = ? AND camera_id = ? AND status = 'active'",
        (deployment_id, camera_id),
    ).fetchone()
    return _row(row) if row else None


def start_window(
    connection,
    deployment_id: str,
    camera_id: str,
    prior_apps: list[str],
    prior_config: dict[str, Any],
    started_at: float,
    duration_seconds: float | None,
) -> str:
    if active_window(connection, deployment_id, camera_id):
        raise ValueError(f"camera {camera_id!r} already has an active enrollment window")
    window_id = uuid.uuid4().hex
    expires_at = started_at + duration_seconds if duration_seconds else None
    connection.execute(
        "INSERT INTO enrollment_windows(id, deployment_id, camera_id, prior_apps, prior_config, "
        "started_at, expires_at, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'active')",
        (
            window_id, deployment_id, camera_id,
            json.dumps(prior_apps), json.dumps(prior_config),
            started_at, expires_at,
        ),
    )
    return window_id


def get_window(connection, window_id: str) -> dict[str, Any] | None:
    row = connection.execute("SELECT * FROM enrollment_windows WHERE id = ?", (window_id,)).fetchone()
    return _row(row) if row else None


def end_window(connection, window_id: str, ended_at: float) -> None:
    changed = connection.execute(
        "UPDATE enrollment_windows SET status = 'reverted', ended_at = ? WHERE id = ? AND status = 'active'",
        (ended_at, window_id),
    ).rowcount
    if not changed:
        raise ValueError(f"enrollment window {window_id!r} is not active")


def expired_windows(connection, cutoff_time: float) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT * FROM enrollment_windows WHERE status = 'active' AND expires_at IS NOT NULL AND expires_at < ?",
        (cutoff_time,),
    ).fetchall()
    return [_row(row) for row in rows]


def list_windows(connection, deployment_id: str | None = None) -> list[dict[str, Any]]:
    query = "SELECT * FROM enrollment_windows"
    values: list[Any] = []
    if deployment_id:
        query += " WHERE deployment_id = ?"
        values.append(deployment_id)
    query += " ORDER BY started_at DESC"
    return [_row(row) for row in connection.execute(query, values).fetchall()]
