import asyncio
import json
import tempfile
import unittest
import uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import yaml
import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from tvt_edge.api import create_app
from tvt_edge.cli import parser as edge_parser, scheduled_sync_workers
from tvt_edge.observability.metrics import DEFAULT_ROUTES
from tvt_edge.cluster import ClusterStatusReader
from tvt_edge.cluster.sync import NodeImagePreflight, SyncWorker
from apexfabric.solution_management.renderer import render
from tvt_edge.db.models import (
    Base,
    AuditEvent,
    Camera,
    CameraApplicationAssignment,
    CameraDeploymentAssignment,
    CameraCredentialVersion,
    DeploymentAssignmentSet,
    DeploymentSyncAttempt,
    DeploymentSyncState,
    EnrollmentWindow,
    KubernetesResourceRef,
    LegacyImport,
    SolutionBundleRevision,
    SolutionCatalogEntry,
    SolutionDeployment,
    utc_now,
)
from tvt_edge.legacy import import_sqlite_lifecycle
from tvt_edge.security import CredentialKeyring
from tvt_edge.service import ManagementService
from tvt_runtime.state import DeploymentStore


ROOT = Path(__file__).resolve().parents[1]
CATALOG_DELIVERY = ROOT / "solution-packs/catalog/tvt-mills-pilot-2026.09.18-v1"
CATALOG_ID = "tvt-mills-pilot:2026.09.18-v1"
CATALOG_DIGEST = "sha256:" + "1" * 64


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
                            "metadata": {"name": "tvt-mills-edge-intel-285h-runtime"},
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
                / "solution-packs/traffic/tvt-mills-pilot-intel-285h.yaml"
            ).read_text(encoding="utf-8")
        )

    @staticmethod
    def route_handler(app, path, method="GET"):
        return next(
            route.endpoint
            for route in app.routes
            if route.path == path and method in (route.methods or set())
        )

    def test_every_registered_route_is_metrics_allowlisted(self):
        # A route missing here makes every request to it crash the HTTP
        # metrics middleware with MetricsContractError (500 Internal Server
        # Error), since it raises on any route outside the bounded allowlist.
        app = create_app(self.sessions, self.keyring)
        registered = {
            route.path for route in app.routes if hasattr(route, "path")
        }
        missing = registered - DEFAULT_ROUTES
        self.assertFalse(missing, f"routes missing from DEFAULT_ROUTES: {missing}")

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
            "tvt-mills-edge-intel-285h",
            [
                {
                    "camera_id": "camera-01",
                    "apps": ["face_recognition", "anpr"],
                    "fps": 8,
                }
            ],
            "test",
            "assignment",
            "assignment-1",
        )

    def catalog_service(self, digest=CATALOG_DIGEST):
        service = ManagementService(
            self.sessions, self.keyring, catalog_resolver=lambda *_args: digest
        )
        service.seed_solution_catalog(CATALOG_DELIVERY, "127.0.0.1:5000")
        service.refresh_solutions(actor="test", request_id="catalog-refresh")
        return service

    @staticmethod
    def catalog_request():
        return {
            "catalog_id": CATALOG_ID,
            "deployment_key": "tvt-mills-v1",
            "assignments": [
                {
                    "camera_id": "camera-01",
                    "apps": ["anpr"],
                    "fps": 8,
                    "config": {
                        "lines": [
                            {
                                "id": "camera-01_entry",
                                "name": "camera-01 gate (entry)",
                                "type": "line",
                                "points": [[0.1, 0.5], [0.9, 0.5]],
                                "accepted": ["A->B"],
                            }
                        ]
                    },
                }
            ],
            "inference_mode": "gpu-npu",
            "resources": {},
            "state_size": "50Gi",
            "namespace": "apexfabric",
        }

    def prepare_catalog_deployment(self):
        self.service.create_site(
            "plant-01", "edge-01", "Plant 01", "Asia/Kolkata", "test", "site"
        )
        self.onboard()
        service = self.catalog_service()
        request = self.catalog_request()
        preview = service.preview_catalog_deployment(**request)
        committed = service.commit_catalog_deployment(
            **request,
            preview_bundle_sha256=preview["bundle_sha256"],
            idempotency_key="catalog-commit-1",
            actor="test",
            request_id="catalog-commit-1",
        )
        return service, request, preview, committed

    def catalog_request_with_face_recognition(self):
        request = self.catalog_request()
        request["assignments"][0]["apps"] = ["face_recognition", "anpr"]
        return request

    def commit_face_recognition_deployment(self):
        self.service.create_site(
            "plant-01", "edge-01", "Plant 01", "Asia/Kolkata", "test", "site"
        )
        self.onboard()
        service = self.catalog_service()
        request = self.catalog_request_with_face_recognition()
        preview = service.preview_catalog_deployment(**request)
        service.commit_catalog_deployment(
            **request,
            preview_bundle_sha256=preview["bundle_sha256"],
            idempotency_key="enrollment-fixture-commit",
            actor="test",
            request_id="enrollment-fixture-commit",
        )
        return service, request

    def current_camera_assignment(self, service, deployment_key, camera_id):
        with service.sessions() as session:
            deployment = session.scalar(
                select(SolutionDeployment).where(SolutionDeployment.deployment_key == deployment_key)
            )
            _catalog_id, assignments, *_ = service._current_catalog_assignments(session, deployment)
        return next(item for item in assignments if item["camera_id"] == camera_id)

    def test_enrollment_start_then_stop_round_trips_apps_and_config(self):
        service, request = self.commit_face_recognition_deployment()
        deployment_key = request["deployment_key"]

        started = service.start_enrollment(
            deployment_key=deployment_key, camera_id="camera-01", duration_seconds=None,
            actor="test", request_id="enrollment-start-1",
        )
        self.assertIsNotNone(started["window_id"])
        self.assertIsNone(started["expires_at"])
        camera = self.current_camera_assignment(service, deployment_key, "camera-01")
        self.assertEqual(camera["apps"], ["face_enrollment"])
        self.assertEqual(camera["config"], {})

        stopped = service.stop_enrollment(
            deployment_key=deployment_key, window_id=started["window_id"],
            actor="test", request_id="enrollment-stop-1",
        )
        self.assertEqual(stopped["camera_id"], "camera-01")
        camera = self.current_camera_assignment(service, deployment_key, "camera-01")
        self.assertEqual(camera["apps"], ["face_recognition", "anpr"])

        windows = service.list_enrollment_windows(deployment_key)
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0]["status"], "reverted")
        self.assertEqual(windows[0]["camera_id"], "camera-01")

    def test_enrollment_rejects_camera_without_face_recognition(self):
        service, request = self.prepare_catalog_deployment()[:2]
        with self.assertRaisesRegex(ValueError, "does not run face_recognition"):
            service.start_enrollment(
                deployment_key=request["deployment_key"], camera_id="camera-01", duration_seconds=None,
                actor="test", request_id="enrollment-start-1",
            )

    def test_enrollment_rejects_concurrent_start_on_the_same_camera(self):
        service, request = self.commit_face_recognition_deployment()
        deployment_key = request["deployment_key"]
        service.start_enrollment(
            deployment_key=deployment_key, camera_id="camera-01", duration_seconds=None,
            actor="test", request_id="enrollment-start-1",
        )
        with self.assertRaisesRegex(ValueError, "already"):
            service.start_enrollment(
                deployment_key=deployment_key, camera_id="camera-01", duration_seconds=None,
                actor="test", request_id="enrollment-start-2",
            )

    def test_enrollment_sweep_reverts_expired_windows(self):
        service, request = self.commit_face_recognition_deployment()
        deployment_key = request["deployment_key"]
        started = service.start_enrollment(
            deployment_key=deployment_key, camera_id="camera-01", duration_seconds=1,
            actor="test", request_id="enrollment-start-1",
        )
        with service.sessions.begin() as session:
            window = session.get(EnrollmentWindow, uuid.UUID(started["window_id"]))
            window.expires_at = utc_now() - timedelta(seconds=1)

        results = service.sweep_expired_enrollment_windows()
        self.assertEqual(len(results), 1)
        self.assertNotIn("error", results[0])
        camera = self.current_camera_assignment(service, deployment_key, "camera-01")
        self.assertEqual(camera["apps"], ["face_recognition", "anpr"])
        windows = service.list_enrollment_windows(deployment_key)
        self.assertEqual(windows[0]["status"], "reverted")

    def test_enrollment_http_routes_start_stop_and_list(self):
        _service, request = self.commit_face_recognition_deployment()
        deployment_key = request["deployment_key"]
        app = create_app(self.sessions, self.keyring)

        async def exercise():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
                start = await client.post(
                    f"/api/v1/deployments/{deployment_key}/enrollment/start",
                    json={"camera_id": "camera-01"},
                )
                listing = await client.get(f"/api/v1/deployments/{deployment_key}/enrollment-windows")
                stop = await client.post(
                    f"/api/v1/deployments/{deployment_key}/enrollment/stop",
                    json={"window_id": start.json()["window_id"]},
                )
            return start, listing, stop

        start, listing, stop = asyncio.run(exercise())
        self.assertEqual(start.status_code, 200)
        self.assertEqual(listing.json()[0]["status"], "active")
        self.assertEqual(stop.status_code, 200)
        self.assertEqual(stop.json()["camera_id"], "camera-01")

    def test_camera_http_routes_cover_the_full_onboarding_lifecycle(self):
        self.service.create_site(
            "plant-01", "edge-01", "Plant 01", "Asia/Kolkata", "test", "site"
        )
        app = create_app(self.sessions, self.keyring)

        async def exercise():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
                created = await client.post("/api/v1/cameras", json={
                    "camera_id": "camera-01", "friendly_name": "Main entrance",
                    "manufacturer": "Example", "model": "C1",
                    "identifiers": [{"kind": "mac", "value": "00:11:22:33:44:55"}],
                })
                listed = await client.get("/api/v1/cameras")
                fetched = await client.get("/api/v1/cameras/camera-01")
                streamed = await client.put("/api/v1/cameras/camera-01/stream", json={
                    "scheme": "rtsp", "host": "192.0.2.10", "port": 554, "path": "/live/main",
                    "profile_token": "main", "transport": "tcp",
                })
                credentialed = await client.put("/api/v1/cameras/camera-01/credentials", json={
                    "username": "camera-user", "password": "camera-secret",
                })
                enabled = await client.patch("/api/v1/cameras/camera-01/enabled", json={"enabled": True})
                cleared = await client.delete("/api/v1/cameras/camera-01/credentials")
                deleted = await client.delete("/api/v1/cameras/camera-01")
                missing = await client.get("/api/v1/cameras/camera-01")
            return created, listed, fetched, streamed, credentialed, enabled, cleared, deleted, missing

        created, listed, fetched, streamed, credentialed, enabled, cleared, deleted, missing = asyncio.run(exercise())
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.json()["camera_id"], "camera-01")
        self.assertFalse(created.json()["credentials_configured"])
        self.assertEqual(len(listed.json()), 1)
        self.assertEqual(fetched.json()["identifiers"], [{"kind": "mac", "value": "00:11:22:33:44:55"}])
        self.assertEqual(streamed.json()["selected_profile"]["host"], "192.0.2.10")
        self.assertTrue(credentialed.json()["credentials_configured"])
        self.assertNotIn("username", credentialed.json())
        self.assertTrue(enabled.json()["enabled"])
        self.assertEqual(cleared.status_code, 204)
        self.assertEqual(deleted.status_code, 204)
        self.assertEqual(missing.status_code, 409)

    def test_camera_geometry_http_routes_cover_zone_line_and_role_lifecycle(self):
        self.service.create_site(
            "plant-01", "edge-01", "Plant 01", "Asia/Kolkata", "test", "site"
        )
        self.service.create_camera(
            camera_key="camera-01", friendly_name="Main entrance",
            manufacturer=None, model=None, identifiers=[],
            actor="test", request_id="camera",
        )
        app = create_app(self.sessions, self.keyring)

        async def exercise():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
                zone = await client.post("/api/v1/cameras/camera-01/zones", json={
                    "name": "ANPR capture area",
                    "points": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]],
                })
                bad_zone = await client.post("/api/v1/cameras/camera-01/zones", json={
                    "name": "degenerate",
                    "points": [[0.1, 0.1], [0.1, 0.1], [0.1, 0.1]],
                })
                line = await client.post("/api/v1/cameras/camera-01/lines", json={
                    "name": "Main entrance entry",
                    "points": [[0.0, 0.5], [1.0, 0.5]],
                    "role_key": "main-entrance", "direction": "entry", "inside_side": "b",
                })
                duplicate_line = await client.post("/api/v1/cameras/camera-01/lines", json={
                    "name": "Main entrance entry (again)",
                    "points": [[0.0, 0.6], [1.0, 0.6]],
                    "role_key": "main-entrance", "direction": "entry", "inside_side": "b",
                })
                updated_line = await client.put(
                    f"/api/v1/cameras/camera-01/geometry/{line.json()['shape_id']}",
                    json={
                        "name": "Updated entrance entry",
                        "points": [[0.1, 0.55], [0.9, 0.55]],
                        "role_key": "main-entrance",
                        "direction": "entry",
                        "inside_side": "a",
                    },
                )
                geometry = await client.get("/api/v1/cameras/camera-01/geometry")
                status = await client.get(
                    "/api/v1/cameras/camera-01/deployment-status"
                )
                role = await client.put("/api/v1/cameras/camera-01/role", json={
                    "role_key": "main-entrance", "display_name": "Main entrance",
                    "direction": "entry",
                })
                deleted = await client.delete(
                    f"/api/v1/cameras/camera-01/geometry/{zone.json()['shape_id']}"
                )
                after_delete = await client.get("/api/v1/cameras/camera-01/geometry")
            return (
                zone, bad_zone, line, duplicate_line, updated_line, geometry,
                status, role, deleted, after_delete,
            )

        (
            zone, bad_zone, line, duplicate_line, updated_line, geometry,
            status, role, deleted, after_delete,
        ) = asyncio.run(exercise())
        self.assertEqual(zone.status_code, 201)
        self.assertEqual(zone.json()["shape_key"], "anpr-capture-area")
        self.assertEqual(bad_zone.status_code, 409)
        self.assertEqual(line.status_code, 201)
        self.assertEqual(line.json()["shape_key"], "main-entrance_entry")
        self.assertEqual(duplicate_line.status_code, 409)
        self.assertEqual(updated_line.status_code, 200)
        self.assertEqual(updated_line.json()["name"], "Updated entrance entry")
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["overall_state"], "not_assigned")
        self.assertEqual(status.json()["geometry_revision"], 3)
        compiled = geometry.json()["compiled_config"]
        self.assertEqual(compiled["zones"]["anpr"][0]["id"], "anpr-capture-area")
        self.assertEqual(compiled["lines"][0]["id"], "main-entrance_entry")
        self.assertEqual(compiled["lines"][0]["accepted"], ["B->A"])
        self.assertEqual(len(geometry.json()["shapes"]), 2)
        self.assertEqual(role.json()["roles"][0]["role_key"], "main-entrance")
        self.assertEqual(deleted.status_code, 204)
        self.assertEqual(len(after_delete.json()["shapes"]), 1)

    def test_geometry_merge_preserves_non_geometry_configuration(self):
        merged = self.service._merge_camera_geometry(
            {
                "zones": {
                    "anpr": [{"id": "old"}],
                    "illegal_parking": [{"id": "keep"}],
                },
                "lines": [{"id": "old-entry"}],
            },
            {"zones": {"anpr": [{"id": "new"}]}},
        )
        self.assertEqual(merged["zones"]["anpr"], [{"id": "new"}])
        self.assertEqual(
            merged["zones"]["illegal_parking"], [{"id": "keep"}]
        )
        self.assertNotIn("lines", merged)

    def test_geometry_mutations_queue_latest_revision_and_live_reload(self):
        service, _request, _preview, _committed = self.prepare_catalog_deployment()
        SyncWorker(
            self.sessions, self.keyring, FakeKubectl(),
            worker_id="geometry-initial-worker",
            image_puller=lambda _reference: None,
        ).run_once()

        zone = service.create_camera_zone(
            "camera-01", "ANPR area",
            [[0.1, 0.1], [0.8, 0.1], [0.8, 0.8], [0.1, 0.8]],
            "test", "geometry-create",
        )
        after_create = service.get_camera_deployment_status("camera-01")
        self.assertEqual(after_create["geometry_revision"], 1)
        self.assertEqual(after_create["overall_state"], "pending")
        self.assertEqual(after_create["deployments"][0]["desired_geometry_revision"], 1)
        self.assertEqual(after_create["deployments"][0]["applied_geometry_revision"], 0)

        updated = service.update_camera_geometry(
            "camera-01", zone["shape_id"],
            name="Updated ANPR area",
            points=[[0.2, 0.2], [0.9, 0.2], [0.9, 0.9], [0.2, 0.9]],
            role_key=None, direction=None, inside_side=None,
            actor="test", request_id="geometry-update",
        )
        self.assertEqual(updated["shape_key"], zone["shape_key"])
        service.delete_camera_geometry(
            "camera-01", zone["shape_id"], "test", "geometry-delete"
        )

        pending = service.get_camera_deployment_status("camera-01")
        self.assertEqual(pending["geometry_revision"], 3)
        self.assertEqual(pending["deployments"][0]["desired_revision"], 4)
        self.assertEqual(pending["deployments"][0]["desired_geometry_revision"], 3)
        with self.sessions() as session:
            deployment = session.scalar(
                select(SolutionDeployment).where(
                    SolutionDeployment.deployment_key == "tvt-mills-v1"
                )
            )
            _catalog_id, assignments, *_ = service._current_catalog_assignments(
                session, deployment
            )
            self.assertEqual(assignments[0]["config"], {})
            snapshots = session.scalars(
                select(DeploymentAssignmentSet).where(
                    DeploymentAssignmentSet.deployment_id == deployment.id
                )
            ).all()
            self.assertEqual(len(snapshots), 4)

        class GeometryLiveReloadKubectl(FakeKubectl):
            def run(self, *arguments, input_text=None, check=True):
                if arguments[:2] == ("get", "pods"):
                    self.calls.append((arguments, input_text))

                    class Result:
                        stdout = json.dumps({"items": [{
                            "metadata": {
                                "name": "tvt-mills-v1-runtime-pod",
                                "creationTimestamp": "2026-09-24T00:00:00Z",
                            },
                            "status": {"phase": "Running"},
                        }]})

                    return Result()
                if arguments[:2] == ("get", "--raw"):
                    self.calls.append((arguments, input_text))

                    class Result:
                        stdout = json.dumps({"status": "ready", "revision": 4})

                    return Result()
                return super().run(*arguments, input_text=input_text, check=check)

        client = GeometryLiveReloadKubectl()
        SyncWorker(
            self.sessions, self.keyring, client,
            worker_id="geometry-live-reload-worker",
            live_reload_timeout=2,
            image_puller=lambda _reference: None,
        ).run_once()
        calls = [call[0] for call in client.calls]
        self.assertTrue(any(call[:2] == ("get", "--raw") for call in calls))
        self.assertFalse(any(call[:2] == ("rollout", "restart") for call in calls))
        applied = service.get_camera_deployment_status("camera-01")
        self.assertEqual(applied["overall_state"], "applied")
        self.assertEqual(applied["deployments"][0]["applied_geometry_revision"], 3)
        self.assertEqual(applied["deployments"][0]["phase"], "completed")

    def test_geometry_validation_rolls_back_and_unassigned_is_reported(self):
        self.service.create_site(
            "plant-01", "edge-01", "Plant 01", "Asia/Kolkata", "test", "site"
        )
        self.onboard()
        self.assertEqual(
            self.service.get_camera_deployment_status("camera-01")["overall_state"],
            "not_assigned",
        )
        service = self.catalog_service()
        request = self.catalog_request()
        preview = service.preview_catalog_deployment(**request)
        service.commit_catalog_deployment(
            **request,
            preview_bundle_sha256=preview["bundle_sha256"],
            idempotency_key="geometry-rollback-fixture",
            actor="test", request_id="geometry-rollback-fixture",
        )
        with self.sessions.begin() as session:
            session.get(
                SolutionCatalogEntry, CATALOG_ID
            ).desired_state_schema = {"not": {}}
        with self.assertRaisesRegex(ValueError, "invalid tvt-mills-pilot geometry"):
            service.create_camera_zone(
                "camera-01", "Rejected area",
                [[0.1, 0.1], [0.8, 0.1], [0.8, 0.8], [0.1, 0.8]],
                "test", "geometry-invalid",
            )
        with self.sessions() as session:
            camera = session.scalar(
                select(Camera).where(Camera.camera_key == "camera-01")
            )
            sync = session.scalar(select(DeploymentSyncState))
            desired = session.get(DeploymentAssignmentSet, sync.desired_assignment_set_id)
            self.assertEqual(camera.geometry_revision, 0)
            self.assertEqual(desired.desired_revision, 1)
        self.assertEqual(service.list_camera_geometry("camera-01"), [])


    def test_geometry_queues_every_compatible_deployment(self):
        service, _request, _preview, _committed = self.prepare_catalog_deployment()
        second = self.catalog_request()
        second["deployment_key"] = "tvt-mills-secondary"
        preview = service.preview_catalog_deployment(**second)
        service.commit_catalog_deployment(
            **second,
            preview_bundle_sha256=preview["bundle_sha256"],
            idempotency_key="geometry-second-deployment",
            actor="test",
            request_id="geometry-second-deployment",
        )

        service.create_camera_line(
            "camera-01",
            "Main gate entry",
            [[0.1, 0.5], [0.9, 0.5]],
            "main-gate",
            "entry",
            "b",
            "test",
            "geometry-multiple",
        )
        status = service.get_camera_deployment_status("camera-01")
        self.assertEqual(len(status["deployments"]), 2)
        self.assertEqual(
            {item["deployment_id"] for item in status["deployments"]},
            {"tvt-mills-v1", "tvt-mills-secondary"},
        )
        self.assertTrue(
            all(
                item["state"] == "pending"
                and item["desired_revision"] == 2
                and item["desired_geometry_revision"] == 1
                for item in status["deployments"]
            )
        )

    def test_unsupported_deployment_is_not_rewritten(self):
        committed = self.commit()
        self.service.create_camera_zone(
            "camera-01",
            "ANPR area",
            [[0.1, 0.1], [0.8, 0.1], [0.8, 0.8], [0.1, 0.8]],
            "test",
            "geometry-unsupported",
        )
        status = self.service.get_camera_deployment_status("camera-01")
        self.assertEqual(status["geometry_revision"], 1)
        self.assertEqual(status["overall_state"], "not_applicable")
        self.assertEqual(status["deployments"][0]["state"], "not_applicable")
        with self.sessions() as session:
            sync = session.scalar(select(DeploymentSyncState))
            desired = session.get(
                DeploymentAssignmentSet, sync.desired_assignment_set_id
            )
            self.assertEqual(desired.id, committed.id)
            self.assertEqual(desired.desired_revision, 1)


    def test_geometry_status_reports_applying_and_failed_without_secrets(self):
        service, _request, _preview, _committed = self.prepare_catalog_deployment()
        service.create_camera_zone(
            "camera-01",
            "ANPR area",
            [[0.1, 0.1], [0.8, 0.1], [0.8, 0.8], [0.1, 0.8]],
            "test",
            "geometry-status",
        )
        worker = SyncWorker(
            self.sessions,
            self.keyring,
            FakeKubectl(),
            worker_id="geometry-status-worker",
            image_puller=lambda _reference: None,
        )
        work = worker.claim()
        self.assertIsNotNone(work)
        with self.sessions.begin() as session:
            attempt = session.scalar(
                select(DeploymentSyncAttempt).where(
                    DeploymentSyncAttempt.desired_revision == 2
                )
            )
            attempt.phase = "waiting_runtime_configuration"

        applying = service.get_camera_deployment_status("camera-01")
        self.assertEqual(applying["overall_state"], "applying")
        self.assertEqual(
            applying["deployments"][0]["phase"], "waiting_runtime_configuration"
        )

        retry_at = utc_now() + timedelta(seconds=30)
        with self.sessions.begin() as session:
            sync = session.scalar(select(DeploymentSyncState))
            sync.state = "failed"
            sync.last_error_code = "RUNTIME_CONFIGURATION_FAILED"
            sync.next_attempt_at = retry_at
            attempt = session.scalar(
                select(DeploymentSyncAttempt).where(
                    DeploymentSyncAttempt.desired_revision == 2
                )
            )
            attempt.status = "retry"
            attempt.phase = "runtime_configuration"
            attempt.error_code = "RUNTIME_CONFIGURATION_FAILED"
            attempt.retry_at = retry_at

        failed = service.get_camera_deployment_status("camera-01")
        self.assertEqual(failed["overall_state"], "failed")
        self.assertEqual(
            failed["deployments"][0]["last_error_code"],
            "RUNTIME_CONFIGURATION_FAILED",
        )
        self.assertIsNotNone(failed["deployments"][0]["retry_at"])
        with self.sessions() as session:
            audit_details = [
                item.details
                for item in session.scalars(
                    select(AuditEvent).where(
                        AuditEvent.action.like("camera.geometry.%")
                    )
                ).all()
            ]
        bounded_output = json.dumps({"status": failed, "audits": audit_details})
        for forbidden in ("rtsp://", "camera-secret", "camera-user", "192.0.2.10"):
            self.assertNotIn(forbidden, bounded_output)

    def test_geometry_rollout_failure_keeps_prior_applied_snapshot(self):
        service, request, _preview, committed = self.prepare_catalog_deployment()
        SyncWorker(
            self.sessions,
            self.keyring,
            FakeKubectl(),
            worker_id="geometry-before-failure",
            image_puller=lambda _reference: None,
        ).run_once()
        zone = service.create_camera_zone(
            "camera-01",
            "ANPR area",
            [[0.1, 0.1], [0.8, 0.1], [0.8, 0.8], [0.1, 0.8]],
            "test",
            "geometry-rollout-failure",
        )

        class FailGeometryFallbackOnce(FakeKubectl):
            def __init__(self):
                super().__init__()
                self.failed = False

            def run(self, *arguments, input_text=None, check=True):
                if arguments[:2] == ("get", "pods"):
                    self.calls.append((arguments, input_text))

                    class Result:
                        stdout = json.dumps(
                            {
                                "items": [
                                    {
                                        "metadata": {
                                            "name": "tvt-mills-v1-runtime-pod",
                                            "creationTimestamp": "2026-09-24T00:00:00Z",
                                        },
                                        "status": {"phase": "Running"},
                                    }
                                ]
                            }
                        )

                    return Result()
                if arguments[:2] == ("get", "--raw"):
                    self.calls.append((arguments, input_text))

                    class Result:
                        stdout = json.dumps({"status": "ready", "revision": 1})

                    return Result()
                if arguments[:2] == ("rollout", "status") and not self.failed:
                    self.failed = True
                    self.calls.append((arguments, input_text))
                    raise ValueError("geometry fallback rollout failed")
                return super().run(
                    *arguments, input_text=input_text, check=check
                )

        client = FailGeometryFallbackOnce()
        with self.assertRaisesRegex(
            ValueError, "geometry fallback rollout failed"
        ):
            SyncWorker(
                self.sessions,
                self.keyring,
                client,
                worker_id="geometry-fallback-failure",
                live_reload_timeout=0.01,
                image_puller=lambda _reference: None,
            ).run_once()

        status = service.get_camera_deployment_status("camera-01")
        self.assertEqual(status["overall_state"], "failed")
        self.assertEqual(
            status["deployments"][0]["applied_geometry_revision"], 0
        )
        self.assertEqual(
            status["deployments"][0]["desired_geometry_revision"], 1
        )
        with self.sessions() as session:
            deployment = session.scalar(select(SolutionDeployment))
            sync = session.get(DeploymentSyncState, deployment.id)
            self.assertEqual(sync.applied_assignment_set_id, committed.id)
            applied_camera = session.scalar(
                select(CameraDeploymentAssignment).where(
                    CameraDeploymentAssignment.assignment_set_id == committed.id
                )
            )
            applied_config = session.scalar(
                select(CameraApplicationAssignment.configuration).where(
                    CameraApplicationAssignment.camera_assignment_id
                    == applied_camera.id
                )
            )
            self.assertEqual(
                applied_config, request["assignments"][0]["config"]
            )
            _catalog_id, desired_assignments, *_ = (
                service._current_catalog_assignments(session, deployment)
            )
            self.assertEqual(
                desired_assignments[0]["config"]["zones"]["anpr"][0]["id"],
                zone["shape_key"],
            )
        self.assertTrue(
            any(call[0][:2] == ("rollout", "status") for call in client.calls)
        )

    def test_camera_snapshot_and_reports_proxy_to_apex(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class FakeApexHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith("/api/cameras/snapshot"):
                    self.send_response(200)
                    self.send_header("Content-Type", "image/jpeg")
                    self.end_headers()
                    self.wfile.write(b"\xff\xd8\xff\xd9")
                elif self.path.startswith("/api/reports/attendance"):
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"sessions": [], "total_duration_seconds": 0}).encode())
                elif self.path.startswith("/api/reports/vehicle-traffic"):
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"sessions": [], "entered_count": 0, "exited_count": 0}).encode())
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), FakeApexHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        # addCleanup runs LIFO: join must be registered first so shutdown()
        # (which unblocks serve_forever) runs before we wait on the thread.
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)

        self.service.create_site(
            "plant-01", "edge-01", "Plant 01", "Asia/Kolkata", "test", "site"
        )
        self.service.create_camera(
            camera_key="camera-01", friendly_name="Main entrance",
            manufacturer=None, model=None, identifiers=[],
            actor="test", request_id="camera",
        )
        app = create_app(
            self.sessions, self.keyring,
            apex_url=f"http://127.0.0.1:{server.server_port}",
        )

        async def exercise():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
                snapshot = await client.get("/api/v1/cameras/camera-01/snapshot")
                attendance = await client.get("/api/v1/reports/attendance")
                vehicles = await client.get("/api/v1/reports/vehicle-traffic")
                missing_camera = await client.get("/api/v1/cameras/unknown-camera/snapshot")
            return snapshot, attendance, vehicles, missing_camera

        snapshot, attendance, vehicles, missing_camera = asyncio.run(exercise())
        self.assertEqual(snapshot.status_code, 200)
        self.assertEqual(snapshot.headers["content-type"], "image/jpeg")
        self.assertEqual(attendance.json(), {"sessions": [], "total_duration_seconds": 0})
        self.assertEqual(vehicles.json()["entered_count"], 0)
        self.assertEqual(missing_camera.status_code, 409)

    def test_camera_snapshot_returns_502_when_apex_is_unreachable(self):
        self.service.create_site(
            "plant-01", "edge-01", "Plant 01", "Asia/Kolkata", "test", "site"
        )
        self.service.create_camera(
            camera_key="camera-01", friendly_name="Main entrance",
            manufacturer=None, model=None, identifiers=[],
            actor="test", request_id="camera",
        )
        app = create_app(self.sessions, self.keyring, apex_url="http://127.0.0.1:1")

        async def exercise():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
                return await client.get("/api/v1/cameras/camera-01/snapshot")

        response = asyncio.run(exercise())
        self.assertEqual(response.status_code, 502)

    def test_catalog_preview_builds_digest_only_complete_runtime_bundle(self):
        self.service.create_site(
            "plant-01", "edge-01", "Plant 01", "Asia/Kolkata", "test", "site"
        )
        self.onboard()
        service = self.catalog_service()
        preview = service.preview_catalog_deployment(**self.catalog_request())
        bundle = preview["bundle"]
        self.assertIn("@sha256:", preview["image_reference"])
        self.assertEqual(
            bundle["configuration"]["catalog_id"], CATALOG_ID
        )
        self.assertNotIn("rtsp://", json.dumps(preview))
        objects = render(bundle, "apexfabric")
        pod = next(item for item in objects if item["kind"] == "Deployment")["spec"]["template"]["spec"]
        main = pod["containers"][0]
        compiler = pod["initContainers"][0]
        self.assertEqual(main["image"], compiler["image"])
        self.assertIn("@sha256:", main["image"])
        main_mounts = {item["mountPath"] for item in main["volumeMounts"]}
        compiler_mounts = {item["mountPath"] for item in compiler["volumeMounts"]}
        # k3s-prototype@5ada504 contract: /configs (ConfigMap desired-state) +
        # /plans + /dev/dri + /dev/accel; no /state or /tmp/apexfabric.
        self.assertTrue({"/configs", "/plans", "/dev/dri", "/dev/accel"}.issubset(main_mounts))
        self.assertTrue({"/configs", "/plans"}.issubset(compiler_mounts))
        self.assertFalse(any(path.startswith("/models/") for path in main_mounts | compiler_mounts))
        desired_camera = preview["desired_state"]["cameras"][0]
        self.assertEqual(desired_camera["config"]["lines"][0]["id"], "camera-01_entry")

    def test_catalog_commit_requires_matching_preview_and_available_digest(self):
        self.service.create_site(
            "plant-01", "edge-01", "Plant 01", "Asia/Kolkata", "test", "site"
        )
        self.onboard()
        service = self.catalog_service()
        request = self.catalog_request()
        with self.assertRaisesRegex(ValueError, "preview it again"):
            service.commit_catalog_deployment(
                **request,
                preview_bundle_sha256="0" * 64,
                idempotency_key="bad-preview",
                actor="test",
                request_id="bad-preview",
            )
        with self.sessions.begin() as session:
            entry = session.get(SolutionCatalogEntry, CATALOG_ID)
            entry.status = "unresolved"
            entry.resolved_digest = None
        with self.assertRaisesRegex(ValueError, "available catalog"):
            service.preview_catalog_deployment(**request)
        with self.sessions.begin() as session:
            entry = session.get(SolutionCatalogEntry, CATALOG_ID)
            entry.status = "available"
            entry.resolved_digest = CATALOG_DIGEST
            entry.tag = "latest"
        with self.assertRaisesRegex(ValueError, "mutable-only"):
            service.preview_catalog_deployment(**request)

    def test_catalog_preview_and_commit_are_the_public_deployment_api(self):
        self.service.create_site(
            "plant-01", "edge-01", "Plant 01", "Asia/Kolkata", "test", "site"
        )
        self.onboard()
        self.catalog_service()
        request = self.catalog_request()
        body = {**request, "deployment_id": request["deployment_key"]}
        del body["deployment_key"]
        app = create_app(self.sessions, self.keyring)
        from types import SimpleNamespace
        from tvt_edge.api.app import CatalogDeploymentCommit, CatalogDeploymentPreview

        preview_route = self.route_handler(
            app, "/api/v1/deployments/preview", "POST"
        )
        commit_route = self.route_handler(app, "/api/v1/deployments", "POST")
        preview = preview_route(CatalogDeploymentPreview(**body))
        commit = commit_route(
            CatalogDeploymentCommit(
                **body,
                preview_bundle_sha256=preview["bundle_sha256"],
                idempotency_key="api-catalog-commit",
            ),
            SimpleNamespace(state=SimpleNamespace(request_id="api:catalog")),
            None,
        )
        self.assertNotIn("rtsp://", json.dumps(preview))
        self.assertEqual(commit["state"], "pending")

    def test_catalog_sync_pulls_digest_before_any_kubernetes_mutation(self):
        _service, _request, _preview, _committed = self.prepare_catalog_deployment()
        client = FakeKubectl()
        pulls = []
        SyncWorker(
            self.sessions,
            self.keyring,
            client,
            worker_id="catalog-worker",
            image_puller=pulls.append,
        ).run_once()
        self.assertEqual(pulls, [f"127.0.0.1:5000/apexfabric/tvt-mills-pilot@{CATALOG_DIGEST}"])
        self.assertTrue(client.calls)

    def test_node_image_preflight_passes_when_digest_is_cached_on_a_node(self):
        reference = f"127.0.0.1:5000/apexfabric/tvt-mills-pilot@{CATALOG_DIGEST}"

        class CachedImageKubectl:
            def run(self, *arguments, input_text=None, check=True):
                assert arguments == ("get", "nodes", "-o", "json")

                class Result:
                    stdout = json.dumps(
                        {"items": [{"status": {"images": [{"names": [reference]}]}}]}
                    )

                return Result()

        NodeImagePreflight(CachedImageKubectl())(reference)

    def test_node_image_preflight_fails_fast_when_digest_is_not_cached(self):
        reference = f"127.0.0.1:5000/apexfabric/tvt-mills-pilot@{CATALOG_DIGEST}"

        class UncachedImageKubectl:
            def run(self, *arguments, input_text=None, check=True):
                class Result:
                    stdout = json.dumps({"items": [{"status": {"images": []}}]})

                return Result()

        with self.assertRaises(RuntimeError):
            NodeImagePreflight(UncachedImageKubectl())(reference)

    def test_catalog_pull_failure_preserves_applied_state_without_kubernetes_writes(self):
        _service, _request, _preview, _committed = self.prepare_catalog_deployment()
        client = FakeKubectl()

        def reject_pull(_reference):
            raise ValueError("containerd import is unavailable")

        with self.assertRaisesRegex(ValueError, "containerd import is unavailable"):
            SyncWorker(
                self.sessions,
                self.keyring,
                client,
                worker_id="pull-failure-worker",
                image_puller=reject_pull,
            ).run_once()
        self.assertEqual(client.calls, [])
        with self.sessions() as session:
            sync = session.scalar(select(DeploymentSyncState))
            self.assertIsNone(sync.applied_assignment_set_id)
            self.assertEqual(sync.state, "failed")

    def test_rollout_failure_restores_previous_complete_bundle_and_digest(self):
        service, request, first_preview, first = self.prepare_catalog_deployment()
        SyncWorker(
            self.sessions,
            self.keyring,
            FakeKubectl(),
            worker_id="first-worker",
            image_puller=lambda _reference: None,
        ).run_once()
        second_digest = "sha256:" + "2" * 64
        with self.sessions.begin() as session:
            entry = session.get(SolutionCatalogEntry, CATALOG_ID)
            entry.resolved_digest = second_digest
            entry.status = "available"
        second_preview = service.preview_catalog_deployment(**request)
        service.commit_catalog_deployment(
            **request,
            preview_bundle_sha256=second_preview["bundle_sha256"],
            idempotency_key="catalog-commit-2",
            actor="test",
            request_id="catalog-commit-2",
        )

        class FailNewRolloutOnce(FakeKubectl):
            def __init__(self):
                super().__init__()
                self.failed = False

            def run(self, *arguments, input_text=None, check=True):
                if arguments[:2] == ("rollout", "status") and not self.failed:
                    self.failed = True
                    self.calls.append((arguments, input_text))
                    raise ValueError("new image rollout failed")
                return super().run(*arguments, input_text=input_text, check=check)

        client = FailNewRolloutOnce()
        with self.assertRaisesRegex(ValueError, "new image rollout failed"):
            SyncWorker(
                self.sessions,
                self.keyring,
                client,
                worker_id="second-worker",
                image_puller=lambda _reference: None,
            ).run_once()
        manifests = "\n".join(text or "" for _args, text in client.calls)
        self.assertIn(f"@{second_digest}", manifests)
        self.assertIn(f"@{CATALOG_DIGEST}", manifests)
        with self.sessions() as session:
            sync = session.scalar(select(DeploymentSyncState))
            applied = session.get(DeploymentAssignmentSet, sync.applied_assignment_set_id)
            attempts = session.scalars(
                select(DeploymentSyncAttempt).order_by(DeploymentSyncAttempt.started_at)
            ).all()
            self.assertEqual(applied.id, first.id)
            self.assertTrue(attempts[-1].safe_detail["previous_applied_bundle_restored"])
        rollback = service.rollback(
            "tvt-mills-v1", first_preview["bundle_sha256"], "test", "explicit-rollback"
        )
        with self.sessions() as session:
            revision = session.get(SolutionBundleRevision, rollback.bundle_revision_id)
            self.assertEqual(
                revision.canonical_bundle["applications"][0]["image"]["digest"],
                CATALOG_DIGEST,
            )

    def test_credentials_are_write_only_and_assignment_revision_is_immutable(self):
        committed = self.commit()
        self.assertEqual(committed.desired_revision, 1)
        camera = self.service.get_camera("camera-01")
        self.assertTrue(camera["credentials_configured"])
        self.assertNotIn("username", camera)
        self.assertNotIn("password", camera)
        self.assertEqual(camera["selected_profile"]["host"], "192.0.2.10")
        self.assertEqual(camera["assignments"][0]["deployment_id"], "tvt-mills-edge-intel-285h")
        self.assertEqual(camera["assignments"][0]["apps"], ["anpr", "face_recognition"])
        with self.sessions() as session:
            stored = session.get(DeploymentAssignmentSet, committed.id)
            self.assertEqual(stored.desired_revision, 1)

    def test_camera_cannot_be_enabled_without_a_configured_stream(self):
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
        with self.assertRaisesRegex(ValueError, "selected stream profile"):
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

    def test_credential_rotation_restarts_once_instead_of_live_reloading(self):
        service, _request, _preview, _committed = self.prepare_catalog_deployment()
        SyncWorker(
            self.sessions,
            self.keyring,
            FakeKubectl(),
            worker_id="credential-initial-worker",
            image_puller=lambda _reference: None,
        ).run_once()
        service.rotate_credentials(
            "camera-01",
            {"username": "camera-user", "password": "replacement"},
            "test",
            "credential-live-reload-guard",
        )
        client = FakeKubectl()
        SyncWorker(
            self.sessions,
            self.keyring,
            client,
            worker_id="credential-update-worker",
            live_reload_timeout=2,
            image_puller=lambda _reference: None,
        ).run_once()
        restarts = [
            call for call in client.calls if call[0][:2] == ("rollout", "restart")
        ]
        self.assertEqual(len(restarts), 1)
        statuses = [
            call for call in client.calls if call[0][:2] == ("rollout", "status")
        ]
        self.assertEqual(len(statuses), 1)
        self.assertFalse(any(call[0][:2] == ("get", "--raw") for call in client.calls))

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
        self.assertFalse(
            any(call[0][:2] == ("rollout", "restart") for call in client.calls)
        )
        self.assertTrue(
            any(call[0][:2] == ("rollout", "status") for call in client.calls)
        )
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

    def test_audit_history_is_managed(self):
        self.service.create_site(
            "plant-01", "edge-01", "Plant 01", "Asia/Kolkata", "test", "site"
        )
        self.service.create_camera(
            camera_key="camera-01",
            friendly_name="Main entrance",
            manufacturer=None,
            model=None,
            identifiers=[],
            actor="operator",
            request_id="camera-create",
        )
        self.assertEqual(
            self.service.list_audit_events()[0]["action"], "camera.create"
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
                    "/internal/v1/sites",
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
            "tvt-mills-edge-intel-285h",
        )

        health = self.route_handler(app, "/api/v1/health")()
        self.assertEqual(health["status"], "healthy")
        self.assertEqual(
            health["components"]["camera_validation"]["configured"], 1
        )
        self.assertEqual(health["components"]["k3s_api"]["status"], "healthy")

    def test_camera_workload_names_reflects_the_running_bundle_application(self):
        self.commit()
        self.assertEqual(
            self.service.camera_workload_names("camera-01"),
            ["tvt-mills-edge-intel-285h-runtime"],
        )

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

    def test_fast_deployment_sync_keeps_camera_inventory_on_bounded_cadence(self):
        sync_worker = object()
        camera_worker = object()
        first, next_inventory = scheduled_sync_workers(
            sync_worker,
            camera_worker,
            now=100.0,
            next_camera_inventory=0.0,
            camera_inventory_interval=15,
            once=False,
        )
        self.assertEqual([name for name, _worker in first], ["sync", "camera-inventory-sync"])
        second, unchanged = scheduled_sync_workers(
            sync_worker,
            camera_worker,
            now=101.0,
            next_camera_inventory=next_inventory,
            camera_inventory_interval=15,
            once=False,
        )
        self.assertEqual([name for name, _worker in second], ["sync"])
        self.assertEqual(unchanged, next_inventory)

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
    parts = []
    for item in value["items"]:
        if item.get("kind") == "Secret":
            for encoded in item.get("data", {}).values():
                parts.append(base64.b64decode(encoded).decode())
        else:
            # ConfigMap desired-state is plain (non-secret) data.
            for plain in item.get("data", {}).values():
                parts.append(plain)
    return " ".join(parts)


if __name__ == "__main__":
    unittest.main()
