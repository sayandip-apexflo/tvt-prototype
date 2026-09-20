import math
import tempfile
import time
import unittest
from pathlib import Path

from apexfabric.control_plane.identity import IdentityPolicy
from apexfabric.control_plane.reporting import attendance_report, sweep_stale_sessions, vehicle_traffic_report
from apexfabric.control_plane.telemetry import RetentionPolicy, TelemetryStore


def normalized(vector):
    norm = math.sqrt(sum(component * component for component in vector))
    return [component / norm for component in vector]


FACE = normalized([1.0, 0.0, 0.0, 0.0])
FACE_NEAR = normalized([0.999, 0.02, 0.0, 0.0])
BODY = normalized([0.2, 0.3, 0.4, 0.5])


def face_event(event_id, camera_id, face, zone_id=None, line_id=None):
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
        "schema_version": "1.0", "event_id": event_id, "timestamp": "2026-09-17T09:00:00Z",
        "camera_id": camera_id, "solution_pack": "surveillance", "application": "face_recognition",
        "event_type": "face_detection_event", "payload": payload,
    }


def plate_event(event_id, camera_id, plate_text, zone_id=None, line_id=None, confidence=0.95):
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
        "schema_version": "1.0", "event_id": event_id, "timestamp": "2026-09-17T09:00:00Z",
        "camera_id": camera_id, "solution_pack": "traffic", "application": "anpr",
        "event_type": "plate_read_event", "payload": payload,
    }


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
