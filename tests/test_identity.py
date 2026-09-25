import math
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from apexfabric.control_plane.identity import IdentityPolicy, PersonStore, resolve_identity
from apexfabric.control_plane.telemetry import RetentionPolicy, TelemetryStore


def normalized(vector):
    norm = math.sqrt(sum(component * component for component in vector))
    return [component / norm for component in vector]


FACE_A = normalized([1.0, 0.01, 0.0, 0.0])
FACE_A_NEAR = normalized([1.0, 0.02, 0.0, 0.0])
FACE_B = normalized([0.0, 0.0, 1.0, 0.01])
BODY_A = normalized([0.3, 0.1, 0.2, 0.4])


def face_event(event_id, camera_id, face, body=None, event_type="face_detection_event", application="face_recognition", location=None, quality=None):
    payload = {"embeddings": {"face": face}}
    if body is not None:
        payload["embeddings"]["body"] = body
    payload["subject"] = {"type": "face", "bbox": {"x1": 1, "y1": 1, "x2": 2, "y2": 2}}
    if location:
        payload["location"] = location
    if quality:
        payload["quality"] = quality
    payload.update(snapshot_ref="x", snapshot_url="/snapshots/x", snapshot_content_type="image/jpeg", snapshot_assets={})
    return {
        "schema_version": "1.0", "event_id": event_id, "timestamp": "2026-09-17T09:00:00Z",
        "camera_id": camera_id, "solution_pack": "surveillance", "application": application,
        "event_type": event_type, "payload": payload,
    }


class IdentityDisabledTests(unittest.TestCase):
    def test_identity_resolution_is_disabled_when_unconfigured(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TelemetryStore(Path(directory), RetentionPolicy(minimum_free_bytes=1))
            self.assertIsNone(store.identity_policy)
            store.ingest("dep1", face_event("e1", "cam-1", FACE_A, BODY_A))
            with store._connect() as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM persons").fetchone()[0], 0)

    def test_partial_environment_configuration_raises(self):
        import os
        os.environ["APEXFABRIC_FACE_EMBEDDING_DIM"] = "4"
        self.addCleanup(os.environ.pop, "APEXFABRIC_FACE_EMBEDDING_DIM", None)
        with self.assertRaises(ValueError):
            IdentityPolicy.from_environment()


def enrollment_event(event_id, camera_id, face):
    return face_event(event_id, camera_id, face, event_type="enrollment_capture_event", application="face_enrollment", quality={"sharpness": 0.9})


class IdentityResolutionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.policy = IdentityPolicy(face_dim=4, body_dim=4, match_threshold=0.9)
        self.store = TelemetryStore(Path(self.directory.name), RetentionPolicy(minimum_free_bytes=1), identity_policy=self.policy)
        self.people = PersonStore(self.store)

    def persons(self):
        with self.store._connect() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM persons").fetchall()]

    def face_vector_count(self):
        with self.store._connect() as connection:
            return connection.execute("SELECT COUNT(*) FROM person_face_embedding_meta").fetchone()[0]

    def enroll(self, name, face, event_id="enrol-1", camera_id="kiosk-1"):
        self.store.ingest("dep1", enrollment_event(event_id, camera_id, face))
        return self.people.enroll([f"dep1:{event_id}"], name)

    def test_unmatched_face_detection_creates_no_person(self):
        self.store.ingest("dep1", face_event("e1", "main-1", FACE_A, BODY_A))
        self.store.ingest("dep1", face_event("e2", "main-2", FACE_B, BODY_A))
        self.assertEqual(self.persons(), [])
        self.assertEqual(self.face_vector_count(), 0)

    def test_face_detection_matches_a_named_person_without_growing_the_gallery(self):
        person_id = self.enroll("Jane Doe", FACE_A)
        self.assertEqual(self.face_vector_count(), 1)
        with self.store._connect() as connection:
            matched = resolve_identity(connection, "e2", "dep1", face_event("e2", "main-1", FACE_A_NEAR), self.policy, 5.0)
            other = resolve_identity(connection, "e3", "dep1", face_event("e3", "main-1", FACE_B), self.policy, 6.0)
        self.assertEqual(matched, person_id)
        self.assertIsNone(other)
        self.assertEqual(self.face_vector_count(), 1, "recognition must never add vectors to a named person")
        self.assertEqual(len(self.persons()), 1)

    def test_dimension_mismatch_is_rejected_without_raising(self):
        envelope = {
            "event_type": "face_detection_event", "camera_id": "cam-1",
            "payload": {"embeddings": {"face": [1.0, 0.0, 0.0]}},  # 3 components, policy expects 4
        }
        with self.store._connect() as connection:
            person_id = resolve_identity(connection, "e1", "dep1", envelope, self.policy, 0.0)
        self.assertIsNone(person_id)
        self.assertEqual(self.persons(), [])

    def test_non_unit_norm_enrollment_capture_is_not_staged(self):
        self.store.ingest("dep1", enrollment_event("e1", "kiosk-1", [1.0, 1.0, 1.0, 1.0]))
        self.assertEqual(self.people.enrollment_captures(["dep1:e1"]), [])

    def test_enrollment_capture_is_staged_not_enrolled(self):
        self.store.ingest("dep1", enrollment_event("e1", "kiosk-1", FACE_A))
        self.assertEqual(self.persons(), [])
        captures = self.people.enrollment_captures(["dep1:e1"])
        self.assertEqual(len(captures), 1)
        self.assertEqual(captures[0]["camera_id"], "kiosk-1")
        self.assertIsNone(captures[0]["matched_person_id"])
        self.assertNotIn("face_embedding", captures[0])

    def test_enroll_creates_one_named_person_from_all_captures_and_consumes_them(self):
        self.store.ingest("dep1", enrollment_event("e1", "kiosk-1", FACE_A))
        self.store.ingest("dep1", enrollment_event("e2", "kiosk-1", FACE_A_NEAR))
        person_id = self.people.enroll(["dep1:e1", "dep1:e2"], "  Jane Doe ")
        rows = self.persons()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["person_id"], rows[0]["display_name"], rows[0]["status"]), (person_id, "Jane Doe", "named"))
        self.assertEqual(self.face_vector_count(), 2)
        self.assertEqual(self.people.enrollment_captures(["dep1:e1", "dep1:e2"]), [])

    def test_enroll_rejects_a_face_that_matches_a_named_person(self):
        from apexfabric.control_plane.identity import DuplicatePersonError
        person_id = self.enroll("Jane Doe", FACE_A)
        self.store.ingest("dep1", enrollment_event("e2", "kiosk-1", FACE_A_NEAR))
        self.assertEqual(self.people.enrollment_captures(["dep1:e2"])[0]["matched_person_id"], person_id)
        with self.assertRaises(DuplicatePersonError) as raised:
            self.people.enroll(["dep1:e2"], "Someone Else")
        self.assertEqual(raised.exception.person_id, person_id)
        self.assertIn("Jane Doe", str(raised.exception))
        self.assertEqual(len(self.persons()), 1)

    def test_enroll_rejects_unknown_or_expired_captures_and_empty_names(self):
        with self.assertRaises(ValueError):
            self.people.enroll(["dep1:missing"], "Jane Doe")
        self.store.ingest("dep1", enrollment_event("e1", "kiosk-1", FACE_A))
        with self.assertRaises(ValueError):
            self.people.enroll(["dep1:e1"], "   ")
        with self.assertRaises(ValueError):
            self.people.enroll([], "Jane Doe")
        self.assertEqual(self.persons(), [])

    def test_discard_and_expiry_remove_staged_captures(self):
        from apexfabric.control_plane.identity import ENROLLMENT_CAPTURE_MAX_AGE_SECONDS, purge_expired_captures
        self.store.ingest("dep1", enrollment_event("e1", "kiosk-1", FACE_A))
        self.store.ingest("dep1", enrollment_event("e2", "kiosk-1", FACE_B))
        self.assertEqual(self.people.discard_captures(["dep1:e1"]), 1)
        with self.store._connect() as connection:
            purge_expired_captures(connection, time.time() + ENROLLMENT_CAPTURE_MAX_AGE_SECONDS + 1)
        self.assertEqual(self.people.enrollment_captures(["dep1:e1", "dep1:e2"]), [])
        self.assertEqual(self.persons(), [])

    def test_concurrent_unmatched_sightings_create_nothing(self):
        events = [face_event(f"e{i}", f"cam-{i % 5}", FACE_A, BODY_A) for i in range(24)]
        with ThreadPoolExecutor(max_workers=8) as workers:
            list(workers.map(lambda event: self.store.ingest("dep1", event), events))
        self.assertEqual(self.persons(), [])


class PersonStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.policy = IdentityPolicy(face_dim=4, body_dim=4, match_threshold=0.9)
        self.store = TelemetryStore(Path(self.directory.name), RetentionPolicy(minimum_free_bytes=1), identity_policy=self.policy)
        self.persons = PersonStore(self.store)
        self.store.ingest("dep1", enrollment_event("e1", "cam-1", FACE_A))
        self.person_id = self.persons.enroll(["dep1:e1"], "Jane Doe")

    def test_rename_corrects_a_named_person(self):
        self.persons.rename(self.person_id, "Jane Q. Doe")
        row = self.persons.list()[0]
        self.assertEqual(row["display_name"], "Jane Q. Doe")
        self.assertEqual(row["status"], "named")

    def test_rename_rejects_empty_name_unknown_and_unnamed_person(self):
        with self.assertRaises(ValueError):
            self.persons.rename(self.person_id, "")
        with self.assertRaises(ValueError):
            self.persons.rename("unknown-person", "Someone")
        with self.store._connect() as connection:
            connection.execute(
                "INSERT INTO persons(person_id, status, first_seen, last_seen, enrollment_source_camera_id, "
                "enrollment_source_event_id) VALUES ('legacy', 'auto_enrolled', 0, 0, 'cam-1', 'x')"
            )
        with self.assertRaises(ValueError):
            self.persons.rename("legacy", "Someone")

    def test_list_filters_by_status(self):
        self.assertEqual(len(self.persons.list(status="named")), 1)
        self.assertEqual(len(self.persons.list(status="auto_enrolled")), 0)


class PurgeUnnamedPersonsTests(unittest.TestCase):
    def test_purge_removes_only_unnamed_persons_vectors_and_attendance(self):
        from apexfabric.control_plane.identity import _insert_vector, _pack, main
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        policy = IdentityPolicy(face_dim=4, body_dim=4, match_threshold=0.9)
        store = TelemetryStore(Path(directory.name) / "telemetry", RetentionPolicy(minimum_free_bytes=1), identity_policy=policy)
        people = PersonStore(store)
        store.ingest("dep1", enrollment_event("e1", "cam-1", FACE_A))
        named_id = people.enroll(["dep1:e1"], "Jane Doe")
        with store._connect() as connection:
            connection.execute(
                "INSERT INTO persons(person_id, status, first_seen, last_seen, enrollment_source_camera_id, "
                "enrollment_source_event_id) VALUES ('legacy', 'auto_enrolled', 0, 0, 'cam-1', 'x')"
            )
            _insert_vector(connection, "person_face_embeddings", "person_face_embedding_meta", "legacy", _pack(FACE_B), "x", 0)
            _insert_vector(connection, "person_body_embeddings", "person_body_embedding_meta", "legacy", _pack(BODY_A), "x", 0)
            for person in ("legacy", named_id):
                connection.execute(
                    "INSERT INTO attendance_sessions(id, person_id, gate, status) VALUES (?, ?, 'main', 'open')",
                    (f"s-{person}", person),
                )

        self.assertEqual(main(["purge-unnamed", "--state-dir", directory.name, "--dry-run"]), 0)
        self.assertEqual(len(people.list()), 2)
        self.assertEqual(main(["purge-unnamed", "--state-dir", directory.name]), 0)

        self.assertEqual([row["person_id"] for row in people.list()], [named_id])
        with store._connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM person_face_embeddings").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM person_body_embeddings").fetchone()[0], 0)
            self.assertEqual(
                [row[0] for row in connection.execute("SELECT person_id FROM attendance_sessions")], [named_id]
            )


if __name__ == "__main__":
    unittest.main()
