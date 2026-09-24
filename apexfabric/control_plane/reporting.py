"""Attendance and vehicle-traffic aggregation over resolved-identity and ANPR events.

Runs inside TelemetryStore.ingest()'s single locked connection, immediately
after identity.resolve_identity() (see telemetry.py). Vehicle-traffic
correlation is unaffected by embeddings/the vector DB -- vehicles are matched
by OCR plate text, not by embedding.
"""

from __future__ import annotations

import json
import math
import os
import uuid
from datetime import datetime, time as clock_time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

SCHEMA = '''
CREATE TABLE IF NOT EXISTS attendance_sessions (
  id TEXT PRIMARY KEY, person_id TEXT NOT NULL, gate TEXT NOT NULL,
  entry_event_id TEXT, entry_time REAL, entry_zone_id TEXT, entry_camera_id TEXT, entry_gate TEXT,
  exit_event_id TEXT, exit_time REAL, exit_zone_id TEXT, exit_camera_id TEXT, exit_gate TEXT,
  status TEXT NOT NULL CHECK(status IN ('open','closed','forced_closed')),
  duration_seconds REAL
);
CREATE INDEX IF NOT EXISTS attendance_open ON attendance_sessions(person_id, gate, status);
CREATE INDEX IF NOT EXISTS attendance_open_person ON attendance_sessions(person_id, status);
CREATE INDEX IF NOT EXISTS attendance_entry_time ON attendance_sessions(entry_time);
CREATE TABLE IF NOT EXISTS vehicle_sessions (
  id TEXT PRIMARY KEY, plate_text TEXT NOT NULL, gate TEXT NOT NULL,
  entry_event_id TEXT, entry_time REAL, entry_zone_id TEXT,
  exit_event_id TEXT, exit_time REAL, exit_zone_id TEXT,
  status TEXT NOT NULL CHECK(status IN ('open','closed','forced_closed'))
);
CREATE INDEX IF NOT EXISTS vehicle_open ON vehicle_sessions(plate_text, gate, status);
CREATE INDEX IF NOT EXISTS vehicle_entry_time ON vehicle_sessions(entry_time);
CREATE TABLE IF NOT EXISTS vehicle_daily_spans (
  report_date TEXT NOT NULL,
  plate_key TEXT NOT NULL,
  plate_text TEXT NOT NULL,
  first_event_id TEXT NOT NULL,
  first_detection_time REAL NOT NULL,
  first_gate TEXT,
  last_event_id TEXT NOT NULL,
  last_detection_time REAL NOT NULL,
  last_gate TEXT,
  detection_count INTEGER NOT NULL,
  PRIMARY KEY(report_date, plate_key)
);
CREATE INDEX IF NOT EXISTS vehicle_daily_span_time
  ON vehicle_daily_spans(report_date, first_detection_time);
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
    for gate_column in ("entry_gate", "exit_gate"):
        if gate_column not in columns:
            connection.execute(f"ALTER TABLE attendance_sessions ADD COLUMN {gate_column} TEXT")
    connection.execute(
        "UPDATE attendance_sessions SET entry_gate = gate "
        "WHERE entry_gate IS NULL AND entry_time IS NOT NULL"
    )
    connection.execute(
        "UPDATE attendance_sessions SET exit_gate = gate "
        "WHERE exit_gate IS NULL AND exit_time IS NOT NULL"
    )

PLATE_CONFIDENCE_FLOOR = float(os.getenv("APEXFABRIC_PLATE_CONFIDENCE_FLOOR", "0.5"))
REPORT_TIMEZONE = ZoneInfo(os.getenv("APEXFABRIC_REPORT_TIMEZONE", "Asia/Kolkata"))


def _report_clock(name: str, default: str) -> clock_time:
    value = os.getenv(name, default)
    try:
        hour_text, minute_text = value.split(":", 1)
        return clock_time(hour=int(hour_text), minute=int(minute_text))
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must use HH:MM") from error


REPORT_WINDOW_START = _report_clock("APEXFABRIC_REPORT_WINDOW_START", "09:00")
REPORT_WINDOW_END = _report_clock("APEXFABRIC_REPORT_WINDOW_END", "18:00")
if REPORT_WINDOW_START >= REPORT_WINDOW_END:
    raise ValueError("the ApexFabric reporting window must begin before it ends")


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


def _event_time(payload: dict[str, Any], received_at: float) -> float:
    """Use the camera event clock when it is a valid absolute timestamp."""
    value = payload.get("time") or payload.get("occurred_at") or payload.get("timestamp")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        parsed = float(value)
        return parsed if math.isfinite(parsed) else received_at
    if isinstance(value, str):
        try:
            parsed_datetime = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return received_at
        if parsed_datetime.tzinfo is not None:
            parsed = parsed_datetime.timestamp()
            return parsed if math.isfinite(parsed) else received_at
    return received_at


def _open_session(
    connection,
    table: str,
    key_column: str,
    key_value: str,
    gate: str | None = None,
) -> Any:
    query = f"SELECT id, entry_time FROM {table} WHERE {key_column} = ? AND status = 'open'"
    values: list[Any] = [key_value]
    if gate is not None:
        query += " AND gate = ?"
        values.append(gate)
    query += " ORDER BY entry_time DESC LIMIT 1"
    return connection.execute(query, values).fetchone()


def _plate_key(plate_text: str) -> str:
    return "".join(character for character in plate_text.upper() if character.isalnum())


def _report_date(observed_at: float) -> str:
    return datetime.fromtimestamp(observed_at, timezone.utc).astimezone(REPORT_TIMEZONE).date().isoformat()


def _within_report_window(observed_at: float) -> bool:
    local_time = datetime.fromtimestamp(observed_at, timezone.utc).astimezone(REPORT_TIMEZONE).time()
    return REPORT_WINDOW_START <= local_time.replace(tzinfo=None) < REPORT_WINDOW_END


def _record_vehicle_detection(
    connection,
    *,
    event_id: str,
    plate_key: str,
    plate_text: str,
    observed_at: float,
    gate: str | None,
) -> None:
    connection.execute(
        """
        INSERT INTO vehicle_daily_spans(
          report_date, plate_key, plate_text,
          first_event_id, first_detection_time, first_gate,
          last_event_id, last_detection_time, last_gate, detection_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        ON CONFLICT(report_date, plate_key) DO UPDATE SET
          first_event_id = CASE
            WHEN excluded.first_detection_time < first_detection_time THEN excluded.first_event_id
            ELSE first_event_id END,
          first_detection_time = MIN(first_detection_time, excluded.first_detection_time),
          first_gate = CASE
            WHEN excluded.first_detection_time < first_detection_time THEN excluded.first_gate
            ELSE first_gate END,
          last_event_id = CASE
            WHEN excluded.last_detection_time > last_detection_time THEN excluded.last_event_id
            ELSE last_event_id END,
          last_detection_time = MAX(last_detection_time, excluded.last_detection_time),
          last_gate = CASE
            WHEN excluded.last_detection_time > last_detection_time THEN excluded.last_gate
            ELSE last_gate END,
          detection_count = detection_count + 1
        """,
        (
            _report_date(observed_at), plate_key, plate_text,
            event_id, observed_at, gate,
            event_id, observed_at, gate,
        ),
    )


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
    observed_at = _event_time(payload, received_at)

    if direction == "entry":
        if _open_session(connection, "attendance_sessions", "person_id", resolved_person_id):
            return
        connection.execute(
            "INSERT INTO attendance_sessions(id, person_id, gate, entry_event_id, entry_time, "
            "entry_zone_id, entry_camera_id, entry_gate, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open')",
            (uuid.uuid4().hex, resolved_person_id, gate, event_id, observed_at, zone_id, camera_id, gate),
        )
    else:
        open_row = _open_session(connection, "attendance_sessions", "person_id", resolved_person_id)
        if open_row and observed_at >= open_row["entry_time"]:
            connection.execute(
                "UPDATE attendance_sessions SET exit_event_id=?, exit_time=?, exit_zone_id=?, "
                "exit_camera_id=?, exit_gate=?, status='closed', "
                "duration_seconds = ? - entry_time WHERE id = ?",
                (event_id, observed_at, zone_id, camera_id, gate, observed_at, open_row["id"]),
            )
        else:
            connection.execute(
                "INSERT INTO attendance_sessions(id, person_id, gate, exit_event_id, exit_time, "
                "exit_zone_id, exit_camera_id, exit_gate, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'closed')",
                (uuid.uuid4().hex, resolved_person_id, gate, event_id, observed_at, zone_id, camera_id, gate),
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
    normalized_plate = _plate_key(plate_text)
    if not normalized_plate:
        return
    location = inner.get("location") or inner.get("line")
    role = _gate_and_role(location)
    observed_at = _event_time(payload, received_at)
    if _within_report_window(observed_at):
        _record_vehicle_detection(
            connection,
            event_id=event_id,
            plate_key=normalized_plate,
            plate_text=plate_text,
            observed_at=observed_at,
            gate=role[0] if role else None,
        )
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
            (uuid.uuid4().hex, plate_text, gate, event_id, observed_at, zone_id),
        )
    else:
        open_row = _open_session(connection, "vehicle_sessions", "plate_text", plate_text, gate)
        if open_row:
            connection.execute(
                "UPDATE vehicle_sessions SET exit_event_id=?, exit_time=?, exit_zone_id=?, status='closed' WHERE id = ?",
                (event_id, observed_at, zone_id, open_row["id"]),
            )
        else:
            connection.execute(
                "INSERT INTO vehicle_sessions(id, plate_text, gate, exit_event_id, exit_time, exit_zone_id, status) "
                "VALUES (?, ?, ?, ?, ?, ?, 'closed')",
                (uuid.uuid4().hex, plate_text, gate, event_id, observed_at, zone_id),
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
    start = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=REPORT_TIMEZONE)
    end = start + timedelta(days=1)
    return start.timestamp(), end.timestamp()


def aggregate_attendance(
    sessions: list[dict[str, Any]],
    registered_people: list[dict[str, Any]],
    *,
    window_start: float | None = None,
    window_end: float | None = None,
) -> list[dict[str, Any]]:
    """Return one row per named person, including registered people with no visits."""
    people = {
        item["person_id"]: {
            "person_id": item["person_id"],
            "display_name": item.get("display_name"),
            "first_entry_time": None,
            "last_exit_time": None,
            "visit_count": 0,
            "total_duration_seconds": 0.0,
            "incomplete_session_count": 0,
        }
        for item in registered_people
        if item.get("person_id") and item.get("display_name")
    }
    for session in sessions:
        person = people.get(session.get("person_id"))
        if person is None:
            continue
        entry_time = session.get("entry_time")
        exit_time = session.get("exit_time")
        complete = (
            session.get("status") == "closed"
            and isinstance(entry_time, (int, float))
            and isinstance(exit_time, (int, float))
            and exit_time >= entry_time
        )
        if not complete:
            person["incomplete_session_count"] += 1
            continue
        clipped_entry = max(entry_time, window_start) if window_start is not None else entry_time
        clipped_exit = min(exit_time, window_end) if window_end is not None else exit_time
        if clipped_exit <= clipped_entry:
            continue
        person["visit_count"] += 1
        person["total_duration_seconds"] += clipped_exit - clipped_entry
        current_first = person["first_entry_time"]
        current_last = person["last_exit_time"]
        person["first_entry_time"] = clipped_entry if current_first is None else min(current_first, clipped_entry)
        person["last_exit_time"] = clipped_exit if current_last is None else max(current_last, clipped_exit)
    return sorted(people.values(), key=lambda item: (item["display_name"].casefold(), item["person_id"]))


def attendance_report(connection, person_id: str | None = None, date: str | None = None) -> dict[str, Any]:
    query = (
        "SELECT attendance_sessions.*, persons.display_name, persons.status AS person_status "
        "FROM attendance_sessions JOIN persons ON persons.person_id = attendance_sessions.person_id "
        "WHERE attendance_sessions.status IN ('open', 'closed', 'forced_closed')"
    )
    values: list[Any] = []
    if person_id:
        query += " AND attendance_sessions.person_id = ?"
        values.append(person_id)
    if date:
        start, end = _day_bounds(date)
        query += (
            " AND ((attendance_sessions.entry_time IS NOT NULL AND attendance_sessions.exit_time IS NOT NULL "
            "AND attendance_sessions.entry_time < ? AND attendance_sessions.exit_time >= ?) "
            "OR (attendance_sessions.entry_time IS NOT NULL AND attendance_sessions.exit_time IS NULL "
            "AND attendance_sessions.entry_time >= ? AND attendance_sessions.entry_time < ?) "
            "OR (attendance_sessions.entry_time IS NULL AND attendance_sessions.exit_time IS NOT NULL "
            "AND attendance_sessions.exit_time >= ? AND attendance_sessions.exit_time < ?))"
        )
        values.extend([end, start, start, end, start, end])
    query += " ORDER BY attendance_sessions.entry_time DESC"
    rows = [dict(row) for row in connection.execute(query, values).fetchall()]
    people_query = "SELECT person_id, display_name FROM persons WHERE status = 'named'"
    people_values: list[Any] = []
    if person_id:
        people_query += " AND person_id = ?"
        people_values.append(person_id)
    registered_people = [dict(row) for row in connection.execute(people_query, people_values).fetchall()]
    people = aggregate_attendance(
        rows,
        registered_people,
        window_start=start if date else None,
        window_end=end if date else None,
    )
    total_duration = sum(row["total_duration_seconds"] for row in people)
    return {
        "sessions": rows,
        "people": people,
        "registered_person_count": len(people),
        "incomplete_session_count": sum(row["incomplete_session_count"] for row in people),
        "total_duration_seconds": total_duration,
    }


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
    span_query = "SELECT * FROM vehicle_daily_spans WHERE 1=1"
    span_values: list[Any] = []
    if date:
        span_query += " AND report_date = ?"
        span_values.append(date)
    if gate:
        span_query += " AND (first_gate = ? OR last_gate = ?)"
        span_values.extend([gate, gate])
    span_query += " ORDER BY first_detection_time DESC"
    vehicles = []
    for row in connection.execute(span_query, span_values).fetchall():
        item = dict(row)
        detection_count = int(item["detection_count"])
        item["duration_seconds"] = (
            max(0.0, item["last_detection_time"] - item["first_detection_time"])
            if detection_count >= 2 else None
        )
        item["status"] = "complete" if detection_count >= 2 else "single_detection"
        vehicles.append(item)
    return {
        "sessions": rows,
        "vehicles": vehicles,
        "vehicle_count": len(vehicles),
        "entered_count": len(entered),
        "exited_count": len(exited),
    }
