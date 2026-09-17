import math
import tempfile
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


class IdentityResolutionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.policy = IdentityPolicy(face_dim=4, body_dim=4, match_threshold=0.9)
        self.store = TelemetryStore(Path(self.directory.name), RetentionPolicy(minimum_free_bytes=1), identity_policy=self.policy)

    def persons(self):
        with self.store._connect() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM persons").fetchall()]

    def test_unmatched_face_auto_enrolls_and_a_near_duplicate_matches(self):
        self.store.ingest("dep1", face_event("e1", "main-1", FACE_A, BODY_A))
        rows = self.persons()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "auto_enrolled")
        self.assertIsNone(rows[0]["display_name"])

        self.store.ingest("dep1", face_event("e2", "main-1", FACE_A_NEAR, BODY_A))
        self.assertEqual(len(self.persons()), 1, "a near-duplicate embedding must reuse the existing person, not enroll a second one")

        self.store.ingest("dep1", face_event("e3", "main-2", FACE_B, BODY_A))
        self.assertEqual(len(self.persons()), 2, "a clearly different face must enroll as a distinct person")

    def test_dimension_mismatch_is_rejected_without_raising(self):
        envelope = {
            "event_type": "face_detection_event", "camera_id": "cam-1",
            "payload": {"embeddings": {"face": [1.0, 0.0, 0.0]}},  # 3 components, policy expects 4
        }
        with self.store._connect() as connection:
            person_id = resolve_identity(connection, "e1", "dep1", envelope, self.policy, 0.0)
        self.assertIsNone(person_id)
        self.assertEqual(self.persons(), [])

    def test_non_unit_norm_vector_is_rejected(self):
        event = face_event("e1", "cam-1", [1.0, 1.0, 1.0, 1.0], [1.0, 1.0, 1.0, 1.0])
        self.store.ingest("dep1", event)
        self.assertEqual(self.persons(), [])

    def test_enrollment_capture_event_requires_no_body_embedding(self):
        event = face_event("e1", "kiosk-1", FACE_A, body=None, event_type="enrollment_capture_event", application="face_enrollment", quality={"sharpness": 0.9})
        self.store.ingest("dep1", event)
        self.assertEqual(len(self.persons()), 1)

    def test_concurrent_unmatched_sightings_of_the_same_person_resolve_to_one_person_id(self):
        events = [face_event(f"e{i}", f"cam-{i % 5}", FACE_A, BODY_A) for i in range(24)]
        with ThreadPoolExecutor(max_workers=8) as workers:
            list(workers.map(lambda event: self.store.ingest("dep1", event), events))
        self.assertEqual(len(self.persons()), 1)


class PersonStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.policy = IdentityPolicy(face_dim=4, body_dim=4, match_threshold=0.9)
        self.store = TelemetryStore(Path(self.directory.name), RetentionPolicy(minimum_free_bytes=1), identity_policy=self.policy)
        self.persons = PersonStore(self.store)
        self.store.ingest("dep1", face_event("e1", "cam-1", FACE_A, BODY_A))
        self.person_id = self.persons.list()[0]["person_id"]

    def test_rename_sets_display_name_and_status(self):
        self.persons.rename(self.person_id, "Jane Doe")
        row = self.persons.list()[0]
        self.assertEqual(row["display_name"], "Jane Doe")
        self.assertEqual(row["status"], "named")

    def test_rename_rejects_empty_name_and_unknown_person(self):
        with self.assertRaises(ValueError):
            self.persons.rename(self.person_id, "")
        with self.assertRaises(ValueError):
            self.persons.rename("unknown-person", "Someone")

    def test_list_filters_by_status(self):
        self.assertEqual(len(self.persons.list(status="auto_enrolled")), 1)
        self.assertEqual(len(self.persons.list(status="named")), 0)


if __name__ == "__main__":
    unittest.main()
