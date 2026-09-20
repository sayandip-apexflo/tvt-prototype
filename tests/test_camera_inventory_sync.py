import base64
import json
import unittest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from tvt_edge.cluster.camera_inventory_sync import CameraInventorySyncWorker
from tvt_edge.db.models import AuditEvent, Base
from tvt_edge.security import CredentialKeyring
from tvt_edge.service import ManagementService


class FakeInventoryKubectl:
    def __init__(self, cameras=None, sources=None):
        self.cameras = cameras
        self.sources = sources
        self.calls = []

    def run(self, *arguments, input_text=None, check=True):
        self.calls.append(arguments)

        class Result:
            returncode = 0
            stdout = ""

        result = Result()
        if arguments[:3] == ("get", "configmap", "apexfabric-camera-inventory"):
            if self.cameras is None:
                result.returncode = 1
            else:
                result.stdout = json.dumps(
                    {"data": {"cameras.json": json.dumps(self.cameras)}}
                )
        elif arguments[:3] == ("get", "secret", "apexfabric-camera-sources"):
            if self.sources is None:
                result.returncode = 1
            else:
                encoded = {
                    f"{camera_id}.rtsp": base64.b64encode(url.encode()).decode()
                    for camera_id, url in self.sources.items()
                }
                result.stdout = json.dumps({"data": encoded})
        else:
            result.returncode = 1
        return result


class CameraInventorySyncTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.keyring = CredentialKeyring.generate_for_test()
        self.service = ManagementService(self.sessions, self.keyring)
        self.service.create_site(
            "plant-01", "edge-01", "Plant 01", "Asia/Kolkata", "test", "site"
        )

    def audit_count(self, action, target_id):
        with self.sessions() as session:
            return len(
                session.scalars(
                    select(AuditEvent).where(
                        AuditEvent.action == action, AuditEvent.target_id == target_id
                    )
                ).all()
            )

    def test_new_camera_is_created_configured_and_enabled(self):
        kubectl = FakeInventoryKubectl(
            cameras=[{"camera_id": "cam-01", "name": "Front gate"}],
            sources={"cam-01": "rtsp://viewer:s3cret@192.0.2.20:8554/live?token=abc"},
        )
        worker = CameraInventorySyncWorker(self.sessions, self.keyring, kubectl)

        result = worker.run_once()

        self.assertEqual(result["created"], 1)
        self.assertEqual(result["configured"], 1)
        self.assertEqual(result["credentials_rotated"], 1)
        self.assertEqual(result["enabled"], 1)

        view = self.service.get_camera("cam-01")
        self.assertEqual(view["friendly_name"], "Front gate")
        self.assertTrue(view["enabled"])
        self.assertTrue(view["credentials_configured"])
        self.assertEqual(
            view["selected_profile"],
            {
                "profile_id": view["selected_profile"]["profile_id"],
                "profile_token": "apexfabric-inventory",
                "scheme": "rtsp",
                "host": "192.0.2.20",
                "port": 8554,
                "path": "/live",
                "transport": "tcp",
                "codec": None,
                "width": None,
                "height": None,
                "fps": None,
            },
        )

    def test_unchanged_inventory_does_not_rewrite_stream_or_credentials(self):
        kubectl = FakeInventoryKubectl(
            cameras=[{"camera_id": "cam-01", "name": "Front gate"}],
            sources={"cam-01": "rtsp://viewer:s3cret@192.0.2.20/live"},
        )
        worker = CameraInventorySyncWorker(self.sessions, self.keyring, kubectl)
        worker.run_once()

        second = worker.run_once()

        self.assertEqual(second["created"], 0)
        self.assertEqual(second["configured"], 0)
        self.assertEqual(second["enabled"], 0)
        # rotate_credentials is called every cycle but is idempotent by
        # content hash, so a second identical cycle must not add a new
        # audit row or credential version.
        self.assertEqual(
            self.audit_count("camera.credentials.rotate", "cam-01"), 1
        )
        self.assertEqual(self.audit_count("camera.stream.configure", "cam-01"), 1)

    def test_changed_source_rotates_credentials_and_reconfigures_stream(self):
        kubectl = FakeInventoryKubectl(
            cameras=[{"camera_id": "cam-01", "name": "Front gate"}],
            sources={"cam-01": "rtsp://viewer:s3cret@192.0.2.20/live"},
        )
        worker = CameraInventorySyncWorker(self.sessions, self.keyring, kubectl)
        worker.run_once()

        kubectl.sources["cam-01"] = "rtsp://viewer:newpass@192.0.2.21/live"
        second = worker.run_once()

        self.assertEqual(second["configured"], 1)
        self.assertEqual(self.audit_count("camera.credentials.rotate", "cam-01"), 2)
        self.assertEqual(self.audit_count("camera.stream.configure", "cam-01"), 2)

    def test_camera_removed_from_inventory_is_deleted_when_unused(self):
        kubectl = FakeInventoryKubectl(
            cameras=[{"camera_id": "cam-01", "name": "Front gate"}],
            sources={"cam-01": "rtsp://192.0.2.20/live"},
        )
        worker = CameraInventorySyncWorker(self.sessions, self.keyring, kubectl)
        worker.run_once()

        kubectl.cameras = []
        result = worker.run_once()

        self.assertEqual(result["deleted"], 1)
        with self.assertRaises(ValueError):
            self.service.get_camera("cam-01")

    def test_camera_removed_from_inventory_is_kept_while_assigned(self):
        kubectl = FakeInventoryKubectl(
            cameras=[{"camera_id": "cam-01", "name": "Front gate"}],
            sources={"cam-01": "rtsp://192.0.2.20/live"},
        )
        worker = CameraInventorySyncWorker(self.sessions, self.keyring, kubectl)
        worker.run_once()

        import yaml
        from pathlib import Path

        bundle = yaml.safe_load(
            (
                Path(__file__).resolve().parents[1]
                / "solution-packs/traffic/tvt-mills-pilot-intel-285h.yaml"
            ).read_text(encoding="utf-8")
        )
        self.service.register_deployment(
            bundle, "apexfabric", "registry.local:5000", "test", "deployment"
        )
        self.service.commit_assignments(
            "tvt-mills-edge-intel-285h",
            [{"camera_id": "cam-01", "apps": ["anpr"], "fps": 8}],
            "test",
            "assignment",
            "assignment-1",
        )

        kubectl.cameras = []
        result = worker.run_once()

        self.assertEqual(result["deleted"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertTrue(self.service.get_camera("cam-01")["enabled"])

    def test_missing_apexfabric_resources_is_a_quiet_noop(self):
        kubectl = FakeInventoryKubectl(cameras=None, sources=None)
        worker = CameraInventorySyncWorker(self.sessions, self.keyring, kubectl)

        result = worker.run_once()

        self.assertEqual(result, {
            "observed": 0,
            "created": 0,
            "configured": 0,
            "credentials_rotated": 0,
            "enabled": 0,
            "deleted": 0,
            "skipped": 0,
        })

    def test_malformed_source_is_skipped_without_crashing_the_cycle(self):
        kubectl = FakeInventoryKubectl(
            cameras=[
                {"camera_id": "cam-bad", "name": "Broken"},
                {"camera_id": "cam-01", "name": "Front gate"},
            ],
            sources={
                "cam-bad": "http://not-rtsp/live",
                "cam-01": "rtsp://192.0.2.20/live",
            },
        )
        worker = CameraInventorySyncWorker(self.sessions, self.keyring, kubectl)

        result = worker.run_once()

        self.assertEqual(result["skipped"], 1)
        self.assertTrue(self.service.get_camera("cam-01")["enabled"])


if __name__ == "__main__":
    unittest.main()
