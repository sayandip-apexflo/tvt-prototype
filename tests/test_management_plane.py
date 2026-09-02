import asyncio
import json
import tempfile
import unittest
import uuid
from datetime import timedelta
from pathlib import Path

import yaml
import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from tvt_edge.api import create_app
from tvt_edge.cli import parser as edge_parser
from tvt_edge.cluster import ClusterStatusReader
from tvt_edge.cluster.sync import SyncWorker
from tvt_edge.db.models import (
    Base,
    AuditEvent,
    CameraCredentialVersion,
    DeploymentAssignmentSet,
    DeploymentSyncAttempt,
    DeploymentSyncState,
    KubernetesResourceRef,
    LegacyImport,
    SolutionBundleRevision,
    utc_now,
)
from tvt_edge.legacy import import_sqlite_lifecycle
from tvt_edge.security import CredentialKeyring
from tvt_edge.service import ManagementService
from tvt_runtime.state import DeploymentStore


ROOT = Path(__file__).resolve().parents[1]


class FakeKubectl:
    def __init__(self, fail_rollout=False):
        self.calls = []
        self.fail_rollout = fail_rollout

    def run(self, *arguments, input_text=None, check=True):
        self.calls.append((arguments, input_text))
        if self.fail_rollout and arguments[:2] == ("rollout", "status"):
            raise ValueError("rollout failed for rtsp://user:secret@example/live")

        class Result:
            stdout = ""

        result = Result()
        if arguments[:2] == ("get", "nodes"):
            result.stdout = json.dumps(
                {
                    "items": [
                        {
                            "kind": "Node",
                            "metadata": {
                                "name": "edge-01",
                                "labels": {
                                    "kubernetes.io/arch": "amd64",
                                    "apexfabric.com/qualified": "true",
                                    "apexfabric.com/hardware-profile": "intel-285h",
                                },
                            },
                            "status": {
                                "conditions": [{"type": "Ready", "status": "True"}],
                                "capacity": {"apexfabric.com/camera-streams": "30"},
                                "allocatable": {"apexfabric.com/camera-streams": "29"},
                            },
                        }
                    ]
                }
            )
        elif arguments[:2] == ("get", "deployments,pods"):
            result.stdout = json.dumps(
                {
                    "items": [
                        {
                            "kind": "Deployment",
                            "metadata": {
                                "name": "traffic-runtime",
                                "generation": 2,
                                "labels": {
                                    "apexfabric.com/deployment-id": "traffic",
                                    "apexfabric.com/application": "runtime",
                                },
                            },
                            "spec": {"replicas": 1},
                            "status": {
                                "readyReplicas": 1,
                                "availableReplicas": 1,
                                "observedGeneration": 2,
                            },
                        },
                        {
                            "kind": "Pod",
                            "metadata": {
                                "name": "traffic-runtime-1",
                                "labels": {
                                    "apexfabric.com/deployment-id": "traffic",
                                    "apexfabric.com/application": "runtime",
                                },
                            },
                            "spec": {"nodeName": "edge-01"},
                            "status": {
                                "phase": "Running",
                                "conditions": [{"type": "Ready", "status": "True"}],
                                "containerStatuses": [{"restartCount": 0}],
                            },
                        },
                    ]
                }
            )
        elif arguments[:2] == ("get", "deployments,configmaps,secrets,services,networkpolicies,persistentvolumeclaims"):
            result.stdout = json.dumps({"items": []})
        elif arguments[:2] == ("get", "deployments"):
            result.stdout = json.dumps(
                {
                    "items": [
                        {
                            "metadata": {"name": "traffic-edge-intel-285h-runtime"},
                            "spec": {"replicas": 1},
                            "status": {"readyReplicas": 1, "availableReplicas": 1},
                        }
                    ]
                }
            )
        elif arguments and arguments[0] == "apply":
            result.stdout = "objects applied"
        return result


class ManagementPlaneTests(unittest.TestCase):
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
        self.bundle = yaml.safe_load(
            (
                ROOT
                / "solution-packs/traffic/traffic-edge-runtime-intel-285h.yaml"
            ).read_text(encoding="utf-8")
        )

    @staticmethod
    def route_handler(app, path, method="GET"):
        return next(
            route.endpoint
            for route in app.routes
            if route.path == path and method in (route.methods or set())
        )

    def onboard(self, camera_id="camera-01", password="camera-secret"):
        self.service.create_camera(
            camera_key=camera_id,
            friendly_name="Main entrance",
            manufacturer="Example",
            model="C1",
            identifiers=[{"kind": "mac", "value": "00:11:22:33:44:55"}],
            actor="test",
            request_id=f"camera:{camera_id}",
        )
        self.service.configure_stream(
            camera_id,
            scheme="rtsp",
            host="192.0.2.10",
            port=554,
            path="/live/main",
            profile_token="profile-main",
            transport="tcp",
            codec="h264",
            width=1920,
            height=1080,
            fps=15,
            actor="test",
            request_id=f"stream:{camera_id}",
        )
        self.service.rotate_credentials(
            camera_id,
            {"username": "camera-user", "password": password},
            "test",
            f"credential:{camera_id}:{password}",
        )
        attempt = self.service.queue_validation(
            camera_id, "test", "test", f"validate:{camera_id}"
        )
        self.service.record_validation_result(
            attempt.id,
            result_code="OK",
            safe_result={"codec": "h264", "keyframe": True},
            actor="probe",
            request_id=f"validation-result:{camera_id}",
        )
        self.service.set_camera_enabled(
            camera_id, True, "test", f"enable:{camera_id}"
        )

    def commit(self):
        self.service.create_site(
            "plant-01", "edge-01", "Plant 01", "Asia/Kolkata", "test", "site"
        )
        self.onboard()
        self.service.register_deployment(
            self.bundle,
            "apexfabric",
            "registry.local:5000",
            "test",
            "deployment",
        )
        return self.service.commit_assignments(
            "traffic-edge-intel-285h",
            [
                {
                    "camera_id": "camera-01",
                    "apps": ["anpr", "vehicle_counting"],
                    "fps": 8,
                }
            ],
            "test",
            "assignment",
            "assignment-1",
        )

    def test_credentials_are_write_only_and_assignment_revision_is_immutable(self):
        committed = self.commit()
        self.assertEqual(committed.desired_revision, 1)
        camera = self.service.get_camera("camera-01")
        self.assertTrue(camera["credentials_configured"])
        self.assertNotIn("username", camera)
        self.assertNotIn("password", camera)
        with self.sessions() as session:
            stored = session.get(DeploymentAssignmentSet, committed.id)
            self.assertEqual(stored.desired_revision, 1)

    def test_camera_cannot_be_enabled_before_current_stream_is_validated(self):
        self.service.create_site(
            "plant-01", "edge-01", "Plant 01", "Asia/Kolkata", "test", "site"
        )
        self.service.create_camera(
            camera_key="camera-01",
            friendly_name="Main entrance",
            manufacturer=None,
            model=None,
            identifiers=[],
            actor="test",
            request_id="camera",
        )
        self.service.configure_stream(
            "camera-01",
            scheme="rtsp",
            host="192.0.2.10",
            port=554,
            path="/live/main",
            profile_token="profile-main",
            transport="tcp",
            codec="h264",
            width=1920,
            height=1080,
            fps=15,
            actor="test",
            request_id="stream",
        )
        with self.assertRaisesRegex(ValueError, "successful validation"):
            self.service.set_camera_enabled("camera-01", True, "test", "enable")

    def test_credential_rotation_queues_new_revision(self):
        first = self.commit()
        self.service.rotate_credentials(
            "camera-01",
            {"username": "camera-user", "password": "new-secret"},
            "test",
            "rotation-2",
        )
        with self.sessions() as session:
            sync = session.scalar(select(DeploymentSyncState))
            desired = session.get(
                DeploymentAssignmentSet, sync.desired_assignment_set_id
            )
            self.assertEqual(first.desired_revision, 1)
            self.assertEqual(desired.desired_revision, 2)
            self.assertEqual(sync.state, "pending")

    def test_sync_materializes_secret_only_in_kubectl_input(self):
        self.commit()
        client = FakeKubectl()
        result = SyncWorker(
            self.sessions,
            self.keyring,
            client,
            worker_id="test-worker",
        ).run_once()
        self.assertEqual(result["outcome"], "succeeded")
        secret_call = next(
            call for call in client.calls if "tvt-camera-sync" in " ".join(call[0])
        )
        self.assertIn("camera-secret", base64_decode_manifest(secret_call[1]))
        self.assertNotIn("last-applied-configuration", secret_call[1])
        with self.sessions() as session:
            sync = session.scalar(select(DeploymentSyncState))
            desired = session.get(
                DeploymentAssignmentSet, sync.desired_assignment_set_id
            )
            applied = session.get(
                DeploymentAssignmentSet, sync.applied_assignment_set_id
            )
            self.assertEqual(sync.state, "applied")
            self.assertEqual(applied.id, desired.id)
            refs = session.scalars(select(KubernetesResourceRef)).all()
            self.assertTrue(any(item.is_secret for item in refs))
            self.assertTrue(all(not hasattr(item, "data") for item in refs))

    def test_failed_sync_is_redacted_and_retryable(self):
        self.commit()
        worker = SyncWorker(
            self.sessions,
            self.keyring,
            FakeKubectl(fail_rollout=True),
            worker_id="test-worker",
        )
        with self.assertRaisesRegex(ValueError, "rollout failed"):
            worker.run_once()
        with self.sessions() as session:
            sync = session.scalar(select(DeploymentSyncState))
            attempt = session.scalar(select(DeploymentSyncAttempt))
            self.assertEqual(sync.state, "failed")
            self.assertIsNone(sync.applied_assignment_set_id)
            self.assertNotIn("camera-secret", json.dumps(attempt.safe_detail))
            self.assertIn("REDACTED_RTSP_URL", json.dumps(attempt.safe_detail))

    def test_loopback_api_exposes_write_only_credential_route(self):
        app = create_app(self.sessions, self.keyring)
        routes = {(route.path, tuple(sorted(route.methods or ()))) for route in app.routes}
        self.assertIn(
            ("/api/v1/cameras/{camera_id}/credentials", ("PUT",)), routes
        )
        self.assertNotIn(
            ("/api/v1/cameras/{camera_id}/credentials", ("GET",)), routes
        )

    def test_api_rejects_untrusted_hosts_and_disables_schema_exposure(self):
        app = create_app(self.sessions, self.keyring)
        async def exercise():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1"
            ) as client:
                response = await client.get("/docs")
                rejected = await client.get(
                    "/api/v1/health", headers={"Host": "management.example.com"}
                )
            return response, rejected

        response, rejected = asyncio.run(exercise())
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(rejected.status_code, 400)

    def test_api_rejects_oversized_declared_request_bodies(self):
        app = create_app(self.sessions, self.keyring)
        async def exercise():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1"
            ) as client:
                return await client.post(
                    "/api/v1/discovery-runs",
                    content=b"x",
                    headers={"Content-Length": str(1024 * 1024 + 1)},
                )

        response = asyncio.run(exercise())
        self.assertEqual(response.status_code, 413)

    def test_cluster_and_health_aggregate_independent_components(self):
        self.commit()
        SyncWorker(
            self.sessions,
            self.keyring,
            FakeKubectl(),
            worker_id="test-worker",
        ).run_once()
        app = create_app(self.sessions, self.keyring, kubectl=FakeKubectl())
        cluster = self.route_handler(app, "/api/v1/cluster")()
        self.assertEqual(cluster["status"], "healthy")
        self.assertTrue(cluster["nodes"]["items"][0]["qualified"])
        self.assertTrue(cluster["workloads"]["deployments"]["items"][0]["ready"])
        self.assertEqual(cluster["synchronization"]["by_state"], {"applied": 1})
        self.assertEqual(
            cluster["synchronization"]["items"][0]["deployment_id"],
            "traffic-edge-intel-285h",
        )

        health = self.route_handler(app, "/api/v1/health")()
        self.assertEqual(health["status"], "healthy")
        self.assertEqual(
            health["components"]["camera_validation"]["validated_online"], 1
        )
        self.assertEqual(health["components"]["k3s_api"]["status"], "healthy")

    def test_discovery_progress_and_camera_detail_are_bounded_and_non_secret(self):
        self.service.create_site(
            "plant-01", "edge-01", "Plant 01", "Asia/Kolkata", "test", "site"
        )
        run = self.service.queue_discovery("test", "test", "discovery")
        app = create_app(self.sessions, self.keyring)
        response = self.route_handler(
            app, "/api/v1/discovery-runs/{operation_id}"
        )(run.id)
        self.assertEqual(response["operation_id"], str(run.id))
        self.assertEqual(response["observations"], [])

    def test_cluster_reader_degrades_without_exposing_command_error(self):
        class FailedKubectl:
            def run(self, *arguments, **kwargs):
                raise ValueError("rtsp://operator:secret@camera/live")

        result = ClusterStatusReader(FailedKubectl()).snapshot()
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("secret", json.dumps(result))

    def test_management_cli_exposes_status_and_camera_orchestration_commands(self):
        self.assertEqual(edge_parser().parse_args(["status"]).command, "status")
        self.assertEqual(edge_parser().parse_args(["cluster"]).command, "cluster")
        self.assertEqual(edge_parser().parse_args(["cameras"]).command, "cameras")
        self.assertEqual(edge_parser().parse_args(["discover"]).command, "discover")
        validation = edge_parser().parse_args(["validate", "camera-01"])
        self.assertEqual(validation.camera_id, "camera-01")

    def test_retention_keeps_applied_credential_until_replacement_is_applied(self):
        self.commit()
        first_worker = SyncWorker(
            self.sessions,
            self.keyring,
            FakeKubectl(),
            worker_id="test-worker-1",
        )
        first_worker.run_once()
        self.service.rotate_credentials(
            "camera-01",
            {"username": "camera-user", "password": "replacement"},
            "test",
            "rotation-retention",
        )
        retention_time = utc_now()
        with self.sessions.begin() as session:
            old = session.scalar(
                select(CameraCredentialVersion).where(
                    CameraCredentialVersion.state == "superseded"
                )
            )
            old.purge_after = retention_time - timedelta(seconds=1)
            old_id = old.id
        first_result = self.service.apply_retention(retention_time)
        self.assertEqual(first_result["credential_material_destroyed"], 0)
        with self.sessions() as session:
            self.assertIsNotNone(session.get(CameraCredentialVersion, old_id).ciphertext)

        SyncWorker(
            self.sessions,
            self.keyring,
            FakeKubectl(),
            worker_id="test-worker-2",
        ).run_once()
        second_result = self.service.apply_retention(retention_time)
        self.assertEqual(second_result["credential_material_destroyed"], 1)
        with self.sessions() as session:
            old = session.get(CameraCredentialVersion, old_id)
            self.assertIsNone(old.ciphertext)
            self.assertIsNone(old.nonce)

    def test_legacy_sqlite_import_is_read_only_and_idempotent(self):
        self.service.create_site(
            "plant-01", "edge-01", "Plant 01", "Asia/Kolkata", "test", "site"
        )
        with tempfile.TemporaryDirectory() as directory:
            store = DeploymentStore(Path(directory))
            store.record_success(
                self.bundle,
                "apexfabric",
                "registry.local:5000",
                "apply",
                {"outcome": "succeeded"},
            )
            before = store.database.read_bytes()
            first = import_sqlite_lifecycle(self.sessions, store.database)
            second = import_sqlite_lifecycle(self.sessions, store.database)
            self.assertEqual(first, second)
            self.assertEqual(store.database.read_bytes(), before)
        with self.sessions() as session:
            self.assertEqual(len(session.scalars(select(LegacyImport)).all()), 1)
            self.assertEqual(
                len(session.scalars(select(SolutionBundleRevision)).all()), 1
            )
            legacy_events = session.scalars(
                select(AuditEvent).where(AuditEvent.actor == "legacy-import")
            ).all()
            self.assertEqual(len(legacy_events), 1)


def base64_decode_manifest(manifest_text):
    import base64

    value = json.loads(manifest_text)
    return " ".join(
        base64.b64decode(encoded).decode()
        for item in value["items"]
        for encoded in item["data"].values()
    )


if __name__ == "__main__":
    unittest.main()
