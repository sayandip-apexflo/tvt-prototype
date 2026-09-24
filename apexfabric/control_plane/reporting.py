"""Attendance and vehicle-traffic aggregation over resolved-identity and ANPR events.

Runs inside TelemetryStore.ingest()'s single locked connection, immediately
after identity.resolve_identity() (see telemetry.py). Vehicle-traffic
correlation is unaffected by embeddings/the vector DB -- vehicles are matched
by OCR plate text, not by embedding.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

SCHEMA = '''
CREATE TABLE IF NOT EXISTS attendance_sessions (
  id TEXT PRIMARY KEY, person_id TEXT NOT NULL, gate TEXT NOT NULL,
  entry_event_id TEXT, entry_time REAL, entry_zone_id TEXT, entry_camera_id TEXT,
  exit_event_id TEXT, exit_time REAL, exit_zone_id TEXT, exit_camera_id TEXT,
  status TEXT NOT NULL CHECK(status IN ('open','closed','forced_closed')),
  duration_seconds REAL
);
CREATE INDEX IF NOT EXISTS attendance_open ON attendance_sessions(person_id, gate, status);
CREATE INDEX IF NOT EXISTS attendance_entry_time ON attendance_sessions(entry_time);
CREATE TABLE IF NOT EXISTS vehicle_sessions (
  id TEXT PRIMARY KEY, plate_text TEXT NOT NULL, gate TEXT NOT NULL,
  entry_event_id TEXT, entry_time REAL, entry_zone_id TEXT,
  exit_event_id TEXT, exit_time REAL, exit_zone_id TEXT,
  status TEXT NOT NULL CHECK(status IN ('open','closed','forced_closed'))
);
CREATE INDEX IF NOT EXISTS vehicle_open ON vehicle_sessions(plate_text, gate, status);
CREATE INDEX IF NOT EXISTS vehicle_entry_time ON vehicle_sessions(entry_time);
'''

def ensure_schema(connection) -> None:
    """Create or additively upgrade reporting tables and retain camera identity."""
    connection.executescript(SCHEMA)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(attendance_sessions)")}
    camera_columns = (
        ("entry_event_id", "entry_camera_id"),
        ("exit_event_id", "exit_camera_id"),
    )
    for _event_column, camera_column in camera_columns:
        if camera_column not in columns:
            connection.execute(f"ALTER TABLE attendance_sessions ADD COLUMN {camera_column} TEXT")
    for event_column, camera_column in camera_columns:
        rows = connection.execute(
            f"SELECT id, {event_column} FROM attendance_sessions "
            f"WHERE {camera_column} IS NULL AND {event_column} IS NOT NULL"
        ).fetchall()
        for session_id, event_id in rows:
            retained = connection.execute(
                "SELECT payload_json FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if retained is None:
                continue
            payload = json.loads(retained[0])
            camera_id = payload.get("camera_id") if isinstance(payload, dict) else None
            if isinstance(camera_id, str) and camera_id:
                connection.execute(
                    f"UPDATE attendance_sessions SET {camera_column} = ? WHERE id = ?", (camera_id, session_id)
                )

PLATE_CONFIDENCE_FLOOR = float(os.getenv("APEXFABRIC_PLATE_CONFIDENCE_FLOOR", "0.5"))


def _gate_and_role(location: dict[str, Any] | None) -> tuple[str, str] | None:
    """`location` is whichever of payload.location (a zone) or payload.line
    (tvt-mills-pilot's face/ANPR line-crossing convention) the caller found --
    both share the same {id, name, type} shape and the same _entry/_exit id
    suffix convention, so one function handles either. See
    docs/contracts/tvt-mills-v1/README.md."""
    if not location or not isinstance(location.get("id"), str):
        return None
    zone_id = location["id"]
    if zone_id.endswith("_entry"):
        return zone_id[: -len("_entry")], "entry"
    if zone_id.endswith("_exit"):
        return zone_id[: -len("_exit")], "exit"
    return None


def _open_session(connection, table: str, key_column: str, key_value: str, gate: str) -> Any:
    return connection.execute(
        f"SELECT id FROM {table} WHERE {key_column} = ? AND gate = ? AND status = 'open' "
        "ORDER BY entry_time DESC LIMIT 1",
        (key_value, gate),
    ).fetchone()


def evaluate_attendance(
    connection,
    event_id: str,
    deployment_id: str,
    payload: dict[str, Any],
    resolved_person_id: str | None,
    received_at: float,
) -> None:
    if payload.get("event_type") != "face_detection_event" or not resolved_person_id:
        return
    inner = payload.get("payload") or {}
    location = inner.get("location") or inner.get("line")
    role = _gate_and_role(location)
    if role is None:
        return
    gate, direction = role
    zone_id = location["id"]
    camera_id_value = payload.get("camera_id")
    camera_id = camera_id_value if isinstance(camera_id_value, str) and camera_id_value else None
    # entry_time/exit_time are the numeric ingest clock (received_at), not the
    # payload's RFC3339 occurred-at string -- duration_seconds arithmetic and
    # the report date-range filter both need a real number, and telemetry.py's
    # own events table already treats occurred_at as an opaque, unparsed TEXT
    # field for the same reason.

    if direction == "entry":
        if _open_session(connection, "attendance_sessions", "person_id", resolved_person_id, gate):
            return
        connection.execute(
            "INSERT INTO attendance_sessions(id, person_id, gate, entry_event_id, entry_time, entry_zone_id, entry_camera_id, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'open')",
            (uuid.uuid4().hex, resolved_person_id, gate, event_id, received_at, zone_id, camera_id),
        )
    else:
        open_row = _open_session(connection, "attendance_sessions", "person_id", resolved_person_id, gate)
        if open_row:
            connection.execute(
                "UPDATE attendance_sessions SET exit_event_id=?, exit_time=?, exit_zone_id=?, exit_camera_id=?, status='closed', "
                "duration_seconds = ? - entry_time WHERE id = ?",
                (event_id, received_at, zone_id, camera_id, received_at, open_row[0]),
            )
        else:
            connection.execute(
                "INSERT INTO attendance_sessions(id, person_id, gate, exit_event_id, exit_time, exit_zone_id, exit_camera_id, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'closed')",
                (uuid.uuid4().hex, resolved_person_id, gate, event_id, received_at, zone_id, camera_id),
            )


def evaluate_vehicle_traffic(
    connection,
    event_id: str,
    deployment_id: str,
    payload: dict[str, Any],
    received_at: float,
) -> None:
    if payload.get("event_type") != "plate_read_event":
        return
    inner = payload.get("payload") or {}
    plate = inner.get("plate") or {}
    plate_text = plate.get("text")
    confidence = plate.get("confidence")
    if not isinstance(plate_text, str) or not plate_text:
        return
    if isinstance(confidence, (int, float)) and confidence < PLATE_CONFIDENCE_FLOOR:
        return
    location = inner.get("location") or inner.get("line")
    role = _gate_and_role(location)
    if role is None:
        return
    gate, direction = role
    zone_id = location["id"]

    if direction == "entry":
        if _open_session(connection, "vehicle_sessions", "plate_text", plate_text, gate):
            return
        connection.execute(
            "INSERT INTO vehicle_sessions(id, plate_text, gate, entry_event_id, entry_time, entry_zone_id, status) "
            "VALUES (?, ?, ?, ?, ?, ?, 'open')",
            (uuid.uuid4().hex, plate_text, gate, event_id, received_at, zone_id),
        )
    else:
        open_row = _open_session(connection, "vehicle_sessions", "plate_text", plate_text, gate)
        if open_row:
            connection.execute(
                "UPDATE vehicle_sessions SET exit_event_id=?, exit_time=?, exit_zone_id=?, status='closed' WHERE id = ?",
                (event_id, received_at, zone_id, open_row[0]),
            )
        else:
            connection.execute(
                "INSERT INTO vehicle_sessions(id, plate_text, gate, exit_event_id, exit_time, exit_zone_id, status) "
                "VALUES (?, ?, ?, ?, ?, ?, 'closed')",
                (uuid.uuid4().hex, plate_text, gate, event_id, received_at, zone_id),
            )


def sweep_stale_sessions(connection, cutoff_time: float) -> dict[str, int]:
    attendance = connection.execute(
        "UPDATE attendance_sessions SET status='forced_closed' WHERE status='open' AND entry_time < ?",
        (cutoff_time,),
    ).rowcount
    vehicles = connection.execute(
        "UPDATE vehicle_sessions SET status='forced_closed' WHERE status='open' AND entry_time < ?",
        (cutoff_time,),
    ).rowcount
    return {"attendance_forced_closed": attendance, "vehicle_forced_closed": vehicles}


def _day_bounds(date: str) -> tuple[float, float]:
    start = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    return start.timestamp(), end.timestamp()


def attendance_report(connection, person_id: str | None = None, date: str | None = None) -> dict[str, Any]:
    query = (
        "SELECT attendance_sessions.*, persons.display_name, persons.status AS person_status "
        "FROM attendance_sessions JOIN persons ON persons.person_id = attendance_sessions.person_id "
        "WHERE attendance_sessions.status IN ('closed', 'forced_closed')"
    )
    values: list[Any] = []
    if person_id:
        query += " AND attendance_sessions.person_id = ?"
        values.append(person_id)
    if date:
        start, end = _day_bounds(date)
        query += " AND attendance_sessions.entry_time >= ? AND attendance_sessions.entry_time < ?"
        values.extend([start, end])
    query += " ORDER BY attendance_sessions.entry_time DESC"
    rows = [dict(row) for row in connection.execute(query, values).fetchall()]
    total_duration = sum(row["duration_seconds"] or 0 for row in rows)
    return {"sessions": rows, "total_duration_seconds": total_duration}


def attendance_log(connection, limit: int = 10) -> list[dict[str, Any]]:
    """Last `limit` individual entry/exit crossings, newest first -- unlike
    attendance_report (closed sessions only, for duration accounting), this
    includes still-open sessions' entry crossings so a live activity feed
    doesn't wait for a paired exit to show anything."""
    limit = max(1, min(limit, 100))
    query = (
        "SELECT e.id AS session_id, e.person_id AS person_id, persons.display_name AS display_name, "
        "e.gate AS gate, e.action AS action, e.ts AS time, e.camera_id AS camera_id "
        "FROM ("
        "  SELECT id, person_id, gate, entry_camera_id AS camera_id, entry_time AS ts, 'entry' AS action "
        "  FROM attendance_sessions WHERE entry_time IS NOT NULL"
        "  UNION ALL "
        "  SELECT id, person_id, gate, exit_camera_id AS camera_id, exit_time AS ts, 'exit' AS action "
        "  FROM attendance_sessions WHERE exit_time IS NOT NULL"
        ") AS e "
        "JOIN persons ON persons.person_id = e.person_id "
        "ORDER BY e.ts DESC LIMIT ?"
    )
    return [dict(row) for row in connection.execute(query, [limit]).fetchall()]


def vehicle_traffic_report(connection, date: str | None = None, gate: str | None = None) -> dict[str, Any]:
    query = "SELECT * FROM vehicle_sessions WHERE 1=1"
    values: list[Any] = []
    if gate:
        query += " AND gate = ?"
        values.append(gate)
    if date:
        start, end = _day_bounds(date)
        query += " AND entry_time >= ? AND entry_time < ?"
        values.extend([start, end])
    query += " ORDER BY entry_time DESC"
    rows = [dict(row) for row in connection.execute(query, values).fetchall()]
    entered = {row["plate_text"] for row in rows if row["entry_time"] is not None}
    exited = {row["plate_text"] for row in rows if row["exit_time"] is not None}
    return {"sessions": rows, "entered_count": len(entered), "exited_count": len(exited)}
