import tempfile
import unittest
from pathlib import Path
from unittest import mock

from apexfabric.control_plane.telemetry import RetentionPolicy, TelemetryStore, snapshot_urls


class TelemetryStoreTests(unittest.TestCase):
    def policy(self, **overrides):
        values = {
            "maximum_bytes": 1024 * 1024,
            "high_watermark": 0.90,
            "low_watermark": 0.80,
            "maximum_age_seconds": 3600,
            "maximum_events": 100,
            "maximum_snapshot_bytes": 1024,
            "maximum_event_bytes": 1024,
            "maximum_snapshots_per_event": 16,
            "minimum_free_bytes": 1,
        }
        values.update(overrides)
        return RetentionPolicy(**values)

    def test_default_server_budget_is_forty_mibibytes(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(RetentionPolicy.from_environment().maximum_bytes, 40 * 1024 * 1024)

    def test_ingests_event_and_downloads_content_addressed_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TelemetryStore(Path(directory), self.policy())
            requested = []

            def fetch(url, maximum):
                requested.append((url, maximum))
                return b"jpeg-content", "image/jpeg"

            event_id = store.ingest("traffic-runtime", {
                "event_id": "event-1", "timestamp": "2026-08-21T00:00:00Z",
                "snapshot_url": "/snapshots/wrong-way/frame.jpg",
            }, fetch)
            events = store.recent_events()
            self.assertEqual(event_id, "traffic-runtime:event-1")
            self.assertEqual(requested, [("/snapshots/wrong-way/frame.jpg", 1024)])
            self.assertEqual(len(events), 1)
            self.assertEqual(len(events[0]["snapshots"]), 1)
            snapshot_id = events[0]["snapshots"][0]["snapshot_id"]
            snapshot = store.snapshot(snapshot_id)
            self.assertEqual(snapshot[0].read_bytes(), b"jpeg-content")
            self.assertEqual(snapshot[1], "image/jpeg")

    def test_maximum_event_count_deletes_oldest_and_orphan_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TelemetryStore(Path(directory), self.policy(maximum_events=1))
            store.ingest("runtime", {"event_id": "old", "snapshot_url": "/snapshots/old.jpg"}, lambda *_: (b"old", "image/jpeg"))
            old_snapshot = store.recent_events()[0]["snapshots"][0]["snapshot_id"]
            store.ingest("runtime", {"event_id": "new"})
            events = store.recent_events()
            self.assertEqual([item["event_id"] for item in events], ["runtime:new"])
            self.assertIsNone(store.snapshot(old_snapshot))

    def test_byte_budget_evicts_oldest_snapshot_and_event(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TelemetryStore(Path(directory), self.policy(
                maximum_bytes=1000, high_watermark=0.90, low_watermark=0.80,
                maximum_snapshot_bytes=800,
            ))
            store.ingest("runtime", {"event_id": "old", "snapshot_url": "/snapshots/old.jpg"}, lambda *_: (b"a" * 700, "image/jpeg"))
            old_snapshot = store.recent_events()[0]["snapshots"][0]["snapshot_id"]
            store.ingest("runtime", {"event_id": "new", "snapshot_url": "/snapshots/new.jpg"}, lambda *_: (b"b" * 700, "image/jpeg"))
            self.assertEqual([event["event_id"] for event in store.recent_events()], ["runtime:new"])
            self.assertIsNone(store.snapshot(old_snapshot))
            self.assertLessEqual(store.stats()["logical_bytes"], 800)

    def test_age_retention_removes_expired_events(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TelemetryStore(Path(directory), self.policy(maximum_age_seconds=10))
            store.ingest("runtime", {"event_id": "event"})
            received = store.recent_events()[0]["received_at"]
            result = store.enforce_retention(now=received + 11)
            self.assertEqual(result["removed_events"], 1)
            self.assertEqual(store.recent_events(), [])

    def test_snapshot_url_discovery_rejects_parent_traversal(self):
        payload = {"one": "/snapshots/a.jpg", "nested": [{"two": "/snapshots/../secret"}]}
        self.assertEqual(snapshot_urls(payload), ["/snapshots/a.jpg"])


if __name__ == "__main__":
    unittest.main()
