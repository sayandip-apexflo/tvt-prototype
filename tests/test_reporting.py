import math
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from apexfabric.control_plane.identity import IdentityPolicy, PersonStore
from apexfabric.control_plane.reporting import (
    attendance_log,
    attendance_report,
    ensure_schema,
    sweep_stale_sessions,
    vehicle_traffic_report,
)
from apexfabric.control_plane.telemetry import RetentionPolicy, TelemetryStore


def normalized(vector):
    norm = math.sqrt(sum(component * component for component in vector))
    return [component / norm for component in vector]


FACE = normalized([1.0, 0.0, 0.0, 0.0])
FACE_NEAR = normalized([0.999, 0.02, 0.0, 0.0])
FACE_OTHER = normalized([0.0, 0.0, 1.0, 0.0])
BODY = normalized([0.2, 0.3, 0.4, 0.5])


def face_event(event_id, camera_id, face, zone_id=None, line_id=None, timestamp=None):
    payload = {
        "embeddings": {"face": face, "body": BODY},
        "subject": {"type": "face", "bbox": {"x1": 1, "y1": 1, "x2": 2, "y2": 2}},
        "snapshot_ref": "x", "snapshot_url": "/snapshots/x", "snapshot_content_type": "image/jpeg", "snapshot_assets": {},
    }
    if zone_id:
        payload["location"] = {"id": zone_id, "type": "zone"}
    if line_id:
        payload["line"] = {"id": line_id, "type": "line"}
    return {
        "schema_version": "1.0", "event_id": event_id,
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
        "camera_id": camera_id, "solution_pack": "surveillance", "application": "face_recognition",
        "event_type": "face_detection_event", "payload": payload,
    }


def plate_event(event_id, camera_id, plate_text, zone_id=None, line_id=None, confidence=0.95, timestamp=None):
    payload = {
        "vehicle_ref": f"{camera_id}:1", "vehicle_track_id": 1,
        "plate": {"text": plate_text, "confidence": confidence},
        "snapshot_ref": "x", "snapshot_url": "/snapshots/x", "snapshot_content_type": "image/jpeg", "snapshot_assets": {},
    }
    if zone_id:
        payload["location"] = {"id": zone_id, "type": "zone"}
    if line_id:
        payload["line"] = {"id": line_id, "type": "line"}
    return {
        "schema_version": "1.0", "event_id": event_id,
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
        "camera_id": camera_id, "solution_pack": "traffic", "application": "anpr",
        "event_type": "plate_read_event", "payload": payload,
    }


class ReportingSchemaUpgradeTests(unittest.TestCase):
    def test_existing_attendance_table_gets_directional_gate_columns(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.executescript(
            """
            CREATE TABLE events (event_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL);
            CREATE TABLE attendance_sessions (
              id TEXT PRIMARY KEY, person_id TEXT NOT NULL, gate TEXT NOT NULL,
              entry_event_id TEXT, entry_time REAL, entry_zone_id TEXT, entry_camera_id TEXT,
              exit_event_id TEXT, exit_time REAL, exit_zone_id TEXT, exit_camera_id TEXT,
              status TEXT NOT NULL, duration_seconds REAL
            );
            INSERT INTO attendance_sessions(
              id, person_id, gate, entry_time, status
            ) VALUES ("session-1", "person-1", "main", 100.0, "open");
            """
        )

        ensure_schema(connection)

        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(attendance_sessions)")
        }
        upgraded = connection.execute(
            "SELECT entry_gate, exit_gate FROM attendance_sessions WHERE id = ?", ("session-1",)
        ).fetchone()
        self.assertTrue({"entry_gate", "exit_gate"}.issubset(columns))
        self.assertEqual(upgraded, ("main", None))
        self.assertIsNotNone(
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type = ? AND name = ?",
                ("table", "vehicle_daily_spans"),
            ).fetchone()
        )


class AttendanceAggregationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        policy = IdentityPolicy(face_dim=4, body_dim=4, match_threshold=0.9)
        self.store = TelemetryStore(Path(self.directory.name), RetentionPolicy(minimum_free_bytes=1), identity_policy=policy)

    def sessions(self):
        with self.store._connect() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM attendance_sessions").fetchall()]

    def test_entry_then_exit_closes_session_with_positive_duration(self):
        self.store.ingest("dep1", face_event("e1", "main-1", FACE, "gate-1-face_entry"))
        time.sleep(0.02)
        self.store.ingest("dep1", face_event("e2", "main-1", FACE_NEAR, "gate-1-face_exit"))
        rows = self.sessions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "closed")
        self.assertGreater(rows[0]["duration_seconds"], 0)

    def test_lingering_reentry_does_not_open_a_second_session(self):
        self.store.ingest("dep1", face_event("e1", "main-1", FACE, "gate-1-face_entry"))
        self.store.ingest("dep1", face_event("e2", "main-1", FACE_NEAR, "gate-1-face_entry"))
        rows = self.sessions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["entry_event_id"], "dep1:e1", "the original entry_time/event must be kept, not overwritten")

    def test_exit_without_prior_entry_is_recorded_as_an_orphan(self):
        self.store.ingest("dep1", face_event("e1", "main-1", FACE, "gate-1-face_exit"))
        rows = self.sessions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "closed")
        self.assertIsNone(rows[0]["entry_time"])

    def test_non_gate_zone_is_ignored(self):
        self.store.ingest("dep1", face_event("e1", "main-1", FACE, "restricted-3-face_zone"))
        self.assertEqual(self.sessions(), [])

    def test_whole_frame_detection_with_no_location_is_ignored(self):
        self.store.ingest("dep1", face_event("e1", "main-1", FACE, zone_id=None))
        self.assertEqual(self.sessions(), [])

    def test_report_filters_by_date(self):
        self.store.ingest("dep1", face_event("e1", "main-1", FACE, "gate-1-face_entry"))
        self.store.ingest("dep1", face_event("e2", "main-1", FACE_NEAR, "gate-1-face_exit"))
        today = time.strftime("%Y-%m-%d", time.gmtime())
        yesterday = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 86400))
        with self.store._connect() as connection:
            self.assertEqual(len(attendance_report(connection, date=today)["sessions"]), 1)
            self.assertEqual(len(attendance_report(connection, date=yesterday)["sessions"]), 0)

    def test_line_crossing_convention_resolves_direction_like_a_zone(self):
        """tvt-mills-pilot has no payload.location for face events -- only
        payload.line, with the same _entry/_exit id suffix convention (see
        docs/contracts/tvt-mills-v1/README.md)."""
        self.store.ingest("dep1", face_event("e1", "main-1", FACE, line_id="gate-1-face_entry"))
        time.sleep(0.02)
        self.store.ingest("dep1", face_event("e2", "main-1", FACE_NEAR, line_id="gate-1-face_exit"))
        rows = self.sessions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "closed")
        self.assertGreater(rows[0]["duration_seconds"], 0)

    def test_entry_and_exit_at_different_gates_close_one_plant_visit(self):
        self.store.ingest(
            "dep1",
            face_event("e1", "main-entry", FACE, line_id="main_entry", timestamp="2026-09-18T09:00:00+05:30"),
        )
        self.store.ingest(
            "dep1",
            face_event("e2", "back-exit", FACE_NEAR, line_id="back_exit", timestamp="2026-09-18T10:30:00+05:30"),
        )
        row = self.sessions()[0]
        self.assertEqual(row["entry_gate"], "main")
        self.assertEqual(row["exit_gate"], "back")
        self.assertEqual(row["duration_seconds"], 90 * 60)

    def test_daily_report_totals_complete_visits_for_named_people_only(self):
        self.store.ingest(
            "dep1",
            face_event("e1", "main-entry", FACE, line_id="main_entry", timestamp="2026-09-18T09:00:00+05:30"),
        )
        self.store.ingest(
            "dep1",
            face_event("e2", "back-exit", FACE_NEAR, line_id="back_exit", timestamp="2026-09-18T11:00:00+05:30"),
        )
        person_id = self.sessions()[0]["person_id"]
        people = PersonStore(self.store)
        people.rename(person_id, "Asha Rao")
        self.store.ingest(
            "dep1",
            face_event(
                "registered-no-visit", "enrollment-camera", FACE_OTHER,
                timestamp="2026-09-18T10:00:00+05:30",
            ),
        )
        second_person_id = next(
            person["person_id"] for person in people.list() if person["person_id"] != person_id
        )
        people.rename(second_person_id, "Bina Shah")

        with self.store._connect() as connection:
            report = attendance_report(connection, date="2026-09-18")

        self.assertEqual(report["registered_person_count"], 2)
        by_name = {person["display_name"]: person for person in report["people"]}
        self.assertEqual(by_name["Asha Rao"]["visit_count"], 1)
        self.assertEqual(by_name["Asha Rao"]["total_duration_seconds"], 2 * 3600)
        self.assertEqual(by_name["Bina Shah"]["visit_count"], 0)
        self.assertEqual(by_name["Bina Shah"]["total_duration_seconds"], 0)
        self.assertEqual(report["incomplete_session_count"], 0)

    def test_open_session_is_reported_as_incomplete_without_duration(self):
        self.store.ingest("dep1", face_event("e1", "main-1", FACE, line_id="gate-1-face_entry"))
        person_id = self.sessions()[0]["person_id"]
        PersonStore(self.store).rename(person_id, "Asha Rao")
        with self.store._connect() as connection:
            report = attendance_report(connection)
            events = attendance_log(connection)
        self.assertEqual(len(report["sessions"]), 1)
        self.assertEqual(report["people"][0]["total_duration_seconds"], 0)
        self.assertEqual(report["people"][0]["incomplete_session_count"], 1)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["action"], "entry")
        self.assertEqual(events[0]["camera_id"], "main-1")

    def test_log_keeps_camera_id_after_source_event_retention(self):
        self.store.ingest("dep1", face_event("e1", "main-1", FACE, line_id="gate-1-face_entry"))
        with self.store._connect() as connection:
            connection.execute("DELETE FROM events")
            events = attendance_log(connection)
        self.assertEqual(events[0]["camera_id"], "main-1")


    def test_log_emits_one_row_per_crossing_not_per_session(self):
        self.store.ingest("dep1", face_event("e1", "main-1", FACE, line_id="gate-1-face_entry"))
        time.sleep(0.02)
        self.store.ingest("dep1", face_event("e2", "main-1", FACE_NEAR, line_id="gate-1-face_exit"))
        with self.store._connect() as connection:
            events = attendance_log(connection)
        self.assertEqual(len(events), 2, "a closed session is one entry crossing plus one exit crossing")
        self.assertEqual([e["action"] for e in events], ["exit", "entry"], "newest crossing first")

    def test_log_respects_limit(self):
        self.store.ingest("dep1", face_event("e1", "main-1", FACE, line_id="gate-1-face_entry"))
        time.sleep(0.01)
        self.store.ingest("dep1", face_event("e2", "main-1", FACE_NEAR, line_id="gate-1-face_exit"))
        time.sleep(0.01)
        self.store.ingest("dep1", face_event("e3", "main-1", FACE, line_id="gate-1-face_entry"))
        time.sleep(0.01)
        self.store.ingest("dep1", face_event("e4", "main-1", FACE_NEAR, line_id="gate-1-face_exit"))
        with self.store._connect() as connection:
            events = attendance_log(connection, limit=2)
        self.assertEqual(len(events), 2, "4 crossings recorded across 2 sessions, but limit caps the log at 2")


class VehicleTrafficAggregationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = TelemetryStore(Path(self.directory.name), RetentionPolicy(minimum_free_bytes=1))

    def sessions(self):
        with self.store._connect() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM vehicle_sessions").fetchall()]

    def test_entry_then_exit_closes_session(self):
        self.store.ingest("dep1", plate_event("e1", "gate-2-anpr", "KA05MN7788", "gate-2-anpr_entry"))
        self.store.ingest("dep1", plate_event("e2", "gate-2-anpr", "KA05MN7788", "gate-2-anpr_exit"))
        rows = self.sessions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "closed")

    def test_misread_plate_at_exit_is_treated_as_a_different_vehicle(self):
        self.store.ingest("dep1", plate_event("e1", "gate-2-anpr", "KA05MN7788", "gate-2-anpr_entry"))
        self.store.ingest("dep1", plate_event("e2", "gate-2-anpr", "KA05MNZ788", "gate-2-anpr_exit"))
        rows = self.sessions()
        self.assertEqual(len(rows), 2, "v1 is exact-match only; a misread must not silently pair with the original")

    def test_low_confidence_read_does_not_open_or_close_a_session(self):
        self.store.ingest("dep1", plate_event("e1", "gate-2-anpr", "KA05MN7788", "gate-2-anpr_entry", confidence=0.1))
        self.assertEqual(self.sessions(), [])

    def test_idling_vehicle_does_not_spawn_duplicate_open_sessions(self):
        self.store.ingest("dep1", plate_event("e1", "gate-2-anpr", "KA05MN7788", "gate-2-anpr_entry"))
        self.store.ingest("dep1", plate_event("e2", "gate-2-anpr", "KA05MN7788", "gate-2-anpr_entry"))
        self.assertEqual(len(self.sessions()), 1)

    def test_line_crossing_convention_resolves_direction_like_a_zone(self):
        self.store.ingest("dep1", plate_event("e1", "gate-2-anpr", "KA05MN7788", line_id="gate-2-anpr_entry"))
        self.store.ingest("dep1", plate_event("e2", "gate-2-anpr", "KA05MN7788", line_id="gate-2-anpr_exit"))
        rows = self.sessions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "closed")

    def test_report_counts_entered_and_exited(self):
        self.store.ingest("dep1", plate_event("e1", "gate-2-anpr", "KA05MN7788", "gate-2-anpr_entry"))
        self.store.ingest("dep1", plate_event("e2", "gate-2-anpr", "KA05MN1111", "gate-2-anpr_entry"))
        self.store.ingest("dep1", plate_event("e3", "gate-2-anpr", "KA05MN1111", "gate-2-anpr_exit"))
        with self.store._connect() as connection:
            report = vehicle_traffic_report(connection)
        self.assertEqual(report["entered_count"], 2)
        self.assertEqual(report["exited_count"], 1)

    def test_vehicle_duration_uses_first_and_last_normalized_plate_detection(self):
        self.store.ingest(
            "dep1",
            plate_event(
                "before-window", "yard-anpr", "KA05MN7788", zone_id="yard-zone",
                timestamp="2026-09-18T08:00:00+05:30",
            ),
        )
        self.store.ingest(
            "dep1",
            plate_event(
                "e1", "yard-anpr", "KA-05 MN 7788", zone_id="yard-zone",
                timestamp="2026-09-18T09:00:00+05:30",
            ),
        )
        self.store.ingest(
            "dep1",
            plate_event(
                "e2", "yard-anpr", "ka05mn7788", zone_id="yard-zone",
                timestamp="2026-09-18T11:30:00+05:30",
            ),
        )
        self.store.ingest(
            "dep1",
            plate_event(
                "after-window", "yard-anpr", "KA05MN7788", zone_id="yard-zone",
                timestamp="2026-09-18T19:00:00+05:30",
            ),
        )

        with self.store._connect() as connection:
            report = vehicle_traffic_report(connection, date="2026-09-18")

        self.assertEqual(report["vehicle_count"], 1)
        self.assertEqual(report["vehicles"][0]["detection_count"], 2)
        self.assertEqual(report["vehicles"][0]["duration_seconds"], 2.5 * 3600)
        self.assertEqual(report["vehicles"][0]["status"], "complete")

    def test_one_plate_detection_has_unknown_duration(self):
        self.store.ingest(
            "dep1",
            plate_event(
                "e1", "yard-anpr", "KA05MN7788", zone_id="yard-zone",
                timestamp="2026-09-18T09:00:00+05:30",
            ),
        )
        with self.store._connect() as connection:
            vehicle = vehicle_traffic_report(connection, date="2026-09-18")["vehicles"][0]
        self.assertIsNone(vehicle["duration_seconds"])
        self.assertEqual(vehicle["status"], "single_detection")


class SweepTests(unittest.TestCase):
    def test_stale_open_sessions_are_forced_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TelemetryStore(Path(directory), RetentionPolicy(minimum_free_bytes=1))
            store.ingest("dep1", plate_event("e1", "gate-2-anpr", "KA05MN7788", "gate-2-anpr_entry"))
            with store._connect() as connection:
                result = sweep_stale_sessions(connection, cutoff_time=time.time() + 1)
            self.assertEqual(result["vehicle_forced_closed"], 1)
            with store._connect() as connection:
                status = connection.execute("SELECT status FROM vehicle_sessions").fetchone()[0]
            self.assertEqual(status, "forced_closed")

    def test_sweep_leaves_recent_open_sessions_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TelemetryStore(Path(directory), RetentionPolicy(minimum_free_bytes=1))
            store.ingest("dep1", plate_event("e1", "gate-2-anpr", "KA05MN7788", "gate-2-anpr_entry"))
            with store._connect() as connection:
                result = sweep_stale_sessions(connection, cutoff_time=time.time() - 3600)
            self.assertEqual(result["vehicle_forced_closed"], 0)


if __name__ == "__main__":
    unittest.main()
