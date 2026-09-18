import asyncio
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

import yaml
import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from tvt_edge.api import create_app
from tvt_edge.cli import parser as edge_parser
from tvt_edge.observability.metrics import DEFAULT_ROUTES
from tvt_edge.cluster import ClusterStatusReader
from tvt_edge.cluster.sync import NodeImagePreflight, SyncWorker
from apexfabric.solution_management.renderer import render
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
    SolutionCatalogEntry,
    utc_now,
)
from tvt_edge.legacy import import_sqlite_lifecycle
from tvt_edge.security import CredentialKeyring
from tvt_edge.service import ManagementService
from tvt_runtime.state import DeploymentStore


ROOT = Path(__file__).resolve().parents[1]
CATALOG_DELIVERY = ROOT / "solution-packs/catalog/traffic-edge-runtime-2026.08.21-v4"
CATALOG_ID = "traffic-edge-runtime:2026.08.21-v4"
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
            "deployment_key": "traffic-v4",
            "assignments": [
                {
                    "camera_id": "camera-01",
                    "apps": ["wrong_way", "vehicle_counting"],
                    "fps": 8,
                    "config": {
                        "lines": {
                            "wrong_way": [
                                {
                                    "name": "direction",
                                    "a": [0.1, 0.5],
                                    "b": [0.9, 0.5],
                                    "direction": "a_to_b",
                                }
                            ]
                        }
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
        self.assertIn("wrong_way", desired_camera["config"]["lines"])

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
        self.assertEqual(pulls, [f"127.0.0.1:5000/apexfabric/traffic-edge-runtime@{CATALOG_DIGEST}"])
        self.assertTrue(client.calls)

    def test_node_image_preflight_passes_when_digest_is_cached_on_a_node(self):
        reference = f"127.0.0.1:5000/apexfabric/traffic-edge-runtime@{CATALOG_DIGEST}"

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
        reference = f"127.0.0.1:5000/apexfabric/traffic-edge-runtime@{CATALOG_DIGEST}"

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
            "traffic-v4", first_preview["bundle_sha256"], "test", "explicit-rollback"
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
        self.assertEqual(camera["assignments"][0]["deployment_id"], "traffic-edge-intel-285h")
        self.assertEqual(camera["assignments"][0]["apps"], ["anpr", "vehicle_counting"])
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

    def test_edge_api_serves_packaged_react_application(self):
        app = create_app(self.sessions, self.keyring)

        async def exercise():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1"
            ) as client:
                index = await client.get("/")
                fallback = await client.get("/cluster")
                missing_api = await client.get("/api/v1/not-a-route")
            return index, fallback, missing_api

        index, fallback, missing_api = asyncio.run(exercise())
        self.assertEqual(index.status_code, 200)
        self.assertIn('<div id="root"></div>', index.text)
        self.assertEqual(fallback.text, index.text)
        self.assertEqual(missing_api.status_code, 404)

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
                    "/api/v1/cameras",
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
            health["components"]["camera_validation"]["configured"], 1
        )
        self.assertEqual(health["components"]["k3s_api"]["status"], "healthy")

    def test_camera_detail_is_bounded_and_non_secret(self):
        self.commit()
        app = create_app(self.sessions, self.keyring)
        response = self.route_handler(app, "/api/v1/cameras/{camera_id}")(
            "camera-01"
        )
        self.assertEqual(response["camera_id"], "camera-01")
        self.assertNotIn("password", json.dumps(response))

    def test_camera_workload_names_reflects_the_running_bundle_application(self):
        self.commit()
        self.assertEqual(
            self.service.camera_workload_names("camera-01"),
            ["traffic-edge-intel-285h-runtime"],
        )

    def test_camera_live_feed_proxies_and_scopes_to_the_requested_camera(self):
        self.commit()
        app = create_app(self.sessions, self.keyring)

        class FakeJsonResponse:
            def __init__(self, body):
                self._body = json.dumps(body).encode()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return self._body

        upstream = {
            "events": [
                {
                    "event_id": "d:1",
                    "deployment_id": "traffic-edge-intel-285h-runtime",
                    "occurred_at": "2026-01-01T00:00:00Z",
                    "received_at": 2.0,
                    "payload": {"camera_id": "camera-01", "event_type": "vehicle_count_event"},
                    "snapshots": [{
                        "snapshot_id": "a" * 64,
                        "source_url": "/snapshots/x.jpg",
                        "url": "/api/telemetry/snapshots/" + "a" * 64,
                    }],
                },
                {
                    "event_id": "d:2",
                    "deployment_id": "traffic-edge-intel-285h-runtime",
                    "occurred_at": "2026-01-01T00:00:01Z",
                    "received_at": 1.0,
                    "payload": {"camera_id": "some-other-camera", "event_type": "vehicle_count_event"},
                    "snapshots": [],
                },
            ]
        }
        with patch(
            "tvt_edge.api.app.urllib.request.urlopen",
            return_value=FakeJsonResponse(upstream),
        ) as mocked:
            result = self.route_handler(
                app, "/api/v1/cameras/{camera_id}/live-feed"
            )("camera-01")
        self.assertTrue(result["available"])
        self.assertEqual(len(result["events"]), 1)
        self.assertEqual(result["events"][0]["payload"]["camera_id"], "camera-01")
        self.assertEqual(
            result["events"][0]["snapshots"][0]["url"],
            "/api/v1/live-feed/snapshots/" + "a" * 64,
        )
        self.assertIn(
            "deployment_id=traffic-edge-intel-285h-runtime", mocked.call_args[0][0]
        )

    def test_camera_live_feed_degrades_when_apexfabric_control_is_unreachable(self):
        self.commit()
        app = create_app(self.sessions, self.keyring)
        with patch(
            "tvt_edge.api.app.urllib.request.urlopen",
            side_effect=URLError("connection refused"),
        ):
            result = self.route_handler(
                app, "/api/v1/cameras/{camera_id}/live-feed"
            )("camera-01")
        self.assertFalse(result["available"])
        self.assertIn("apexfabric-control is unavailable", result["error"])
        self.assertEqual(result["events"], [])

    def test_camera_live_feed_reports_unavailable_for_a_camera_without_a_deployment(self):
        self.service.create_site(
            "plant-01", "edge-01", "Plant 01", "Asia/Kolkata", "test", "site"
        )
        self.onboard()
        app = create_app(self.sessions, self.keyring)
        result = self.route_handler(
            app, "/api/v1/cameras/{camera_id}/live-feed"
        )("camera-01")
        self.assertFalse(result["available"])
        self.assertEqual(result["events"], [])

    def test_live_feed_snapshot_proxies_bytes_and_rejects_malformed_ids(self):
        app = create_app(self.sessions, self.keyring)

        class FakeImageResponse:
            def __init__(self, body, content_type):
                self._body = body
                self.headers = {"Content-Type": content_type}

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return self._body

        with patch(
            "tvt_edge.api.app.urllib.request.urlopen",
            return_value=FakeImageResponse(b"\xff\xd8", "image/jpeg"),
        ):
            response = self.route_handler(
                app, "/api/v1/live-feed/snapshots/{snapshot_id}"
            )("a" * 64)
        self.assertEqual(response.body, b"\xff\xd8")
        self.assertEqual(response.media_type, "image/jpeg")

        rejected = self.route_handler(
            app, "/api/v1/live-feed/snapshots/{snapshot_id}"
        )("not-a-hash")
        self.assertEqual(rejected.status_code, 404)

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
