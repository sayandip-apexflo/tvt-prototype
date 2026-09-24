"""Small loopback-only management API for Slice 3 state mutations."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from apexfabric.solution_management.renderer import Kubectl
from tvt_edge import __version__
from tvt_edge.alerting import AlertingService
from tvt_edge.apex_client import ApexClient, ApexUnavailableError
from tvt_edge.cluster import ClusterStatusReader
from tvt_edge.enrollment import EnrollmentReconciler
from tvt_edge.observability import (
    EdgeMetrics,
    WatchdogMetricsCollector,
    bind_log_context,
    get_logger,
    render_metrics,
    request_id_or_new,
    reset_log_context,
)
from tvt_edge.security import CredentialKeyring, redact_text
from tvt_edge.service import ManagementService
from tvt_edge.status import aggregate_health
from tvt_edge.watchdog import STATE_PATH, WatchdogStatusReader


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SiteCreate(StrictModel):
    site_key: str
    edge_id: str
    display_name: str
    timezone: str = "UTC"


class DeploymentInput(StrictModel):
    bundle: dict[str, Any]
    namespace: str = "apexfabric"
    registry: str


class AssignmentInput(StrictModel):
    camera_id: str
    apps: list[str]
    fps: int = 8
    bundle_application: str = "runtime"
    config: dict[str, Any] = Field(default_factory=dict)


class DeploymentResourcesInput(StrictModel):
    cpu_request: str = "8"
    cpu_limit: str = "16"
    memory_request: str = "16Gi"
    memory_limit: str = "32Gi"


class CatalogDeploymentPreview(StrictModel):
    catalog_id: str
    deployment_id: str
    namespace: str = "apexfabric"
    inference_mode: str = "cpu-compatible"
    resources: DeploymentResourcesInput = Field(default_factory=DeploymentResourcesInput)
    state_size: str = "50Gi"
    assignments: list[AssignmentInput]


class CatalogDeploymentCommit(CatalogDeploymentPreview):
    preview_bundle_sha256: str
    idempotency_key: str


class AssignmentCommit(StrictModel):
    assignments: list[AssignmentInput]
    idempotency_key: str


class RollbackInput(StrictModel):
    bundle_sha256: str


class EnrollmentStartInput(StrictModel):
    camera_id: str
    duration_seconds: int | None = None


class EnrollmentStopInput(StrictModel):
    window_id: str


class EnrollmentCameraDesignationInput(StrictModel):
    camera_id: str


class EnrollmentSessionStartInput(StrictModel):
    capture_window_seconds: int | None = None


class PersonNameInput(StrictModel):
    display_name: str


class CameraIdentifierInput(StrictModel):
    kind: str
    value: str


class CameraCreate(StrictModel):
    camera_id: str
    friendly_name: str
    manufacturer: str | None = None
    model: str | None = None
    identifiers: list[CameraIdentifierInput] = Field(default_factory=list)
    rtsp_url: str | None = None


class CameraEnabledInput(StrictModel):
    enabled: bool


class CameraStreamInput(StrictModel):
    scheme: str
    host: str
    port: int
    path: str
    profile_token: str
    transport: str
    codec: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None


class CameraCredentialsInput(StrictModel):
    username: str | None = None
    password: str | None = None
    query: dict[str, str] = Field(default_factory=dict)
    path_suffix: str | None = None


class CameraZoneInput(StrictModel):
    name: str
    points: list[list[float]]


class CameraLineInput(StrictModel):
    name: str
    points: list[list[float]]
    role_key: str
    direction: str
    inside_side: str


class CameraGeometryUpdateInput(StrictModel):
    name: str
    points: list[list[float]]
    role_key: str | None = None
    direction: str | None = None
    inside_side: str | None = None


class CameraRoleInput(StrictModel):
    role_key: str
    display_name: str
    direction: str = "unknown"
    ordinal: int | None = None


MAX_API_REQUEST_BYTES = 1024 * 1024


def _trusted_loopback_host(value: str) -> bool:
    if (
        not value
        or value != value.strip()
        or any(character.isspace() for character in value)
        or any(character in value for character in "/?#@")
    ):
        return False
    try:
        parsed = urlsplit(f"//{value}")
        hostname = parsed.hostname
        parsed.port
    except ValueError:
        return False
    return hostname in {"127.0.0.1", "::1", "localhost"}


def _security_headers(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    return response


def create_app(
    sessions: sessionmaker[Session],
    keyring: CredentialKeyring,
    allowed_namespace: str = "apexfabric",
    kubectl: Kubectl | None = None,
    watchdog_state_path: Path = STATE_PATH,
    apex_url: str = "http://127.0.0.1:8088",
    enrollment_reconcile_interval_seconds: float | None = None,
) -> FastAPI:
    """`enrollment_reconcile_interval_seconds` starts a bounded background
    task (tvt_edge.enrollment.EnrollmentReconciler) inside this process's
    event loop that advances non-terminal enrollment sessions -- see
    ManagementService.reconcile_enrollment_sessions. It defaults to disabled
    (None) so importing/constructing this app in tests never starts a real
    background loop; tvt_edge/cli.py's `api` command is the only caller that
    passes a positive interval in production. Mirrors the worker lifespan
    task tvt_edge/alerting/receiver.py's create_alert_app already uses."""

    service = ManagementService(sessions, keyring)
    alerting = AlertingService(sessions)
    apex = ApexClient(apex_url)
    cluster_status = ClusterStatusReader(kubectl, allowed_namespace)
    metrics = EdgeMetrics("edge-management")
    metrics.set_build(__version__)
    watchdog = WatchdogStatusReader(watchdog_state_path)
    enrollment_logger = get_logger("tvt_edge.enrollment")
    reconciler = (
        EnrollmentReconciler(
            service,
            apex,
            metrics=metrics,
            logger=enrollment_logger,
            interval_seconds=enrollment_reconcile_interval_seconds,
        )
        if enrollment_reconcile_interval_seconds and enrollment_reconcile_interval_seconds > 0
        else None
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        task: asyncio.Task[None] | None = None
        if reconciler is not None:

            async def run_reconciler() -> None:
                while True:
                    try:
                        await asyncio.to_thread(reconciler.run_once)
                    except Exception:
                        enrollment_logger.exception(
                            "Enrollment reconciler cycle failed",
                            extra={"event": "enrollment_reconciler_failed", "error_code": "INTERNAL_ERROR"},
                        )
                        metrics.application_error("INTERNAL_ERROR")
                    await asyncio.sleep(reconciler.interval_seconds)

            task = asyncio.create_task(run_reconciler())
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    app = FastAPI(
        title="TVT edge management",
        version="1",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    metrics.registry.register(WatchdogMetricsCollector(watchdog))
    logger = get_logger("tvt_edge.http")
    app.state.metrics = metrics

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        if not _trusted_loopback_host(request.headers.get("Host", "")):
            return _security_headers(
                Response(
                    content='{"detail":"invalid host header"}',
                    status_code=400,
                    media_type="application/json",
                )
            )
        if request.headers.get("Transfer-Encoding"):
            return _security_headers(
                Response(
                    content='{"detail":"content length is required"}',
                    status_code=411,
                    media_type="application/json",
                )
            )
        content_length = request.headers.get("Content-Length")
        if content_length is not None:
            try:
                declared_size = int(content_length)
            except ValueError:
                return _security_headers(
                    Response(
                        content='{"detail":"invalid content length"}',
                        status_code=400,
                        media_type="application/json",
                    )
                )
            if declared_size < 0 or declared_size > MAX_API_REQUEST_BYTES:
                return _security_headers(
                    Response(
                        content='{"detail":"request body is too large"}',
                        status_code=413,
                        media_type="application/json",
                    )
                )
        request_id = request_id_or_new(request.headers.get("X-Request-ID"))
        request.state.request_id = request_id
        token = bind_log_context(request_id=request_id)
        started = time.monotonic()
        status_code = 500
        metrics.http_started()
        try:
            response: Response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-ID"] = request_id
            return _security_headers(response)
        except Exception:
            metrics.application_error("INTERNAL_ERROR")
            logger.exception(
                "HTTP request failed",
                extra={"event": "http_request_failed", "error_code": "INTERNAL_ERROR"},
            )
            raise
        finally:
            route_object = request.scope.get("route")
            route = getattr(route_object, "path", None)
            duration = time.monotonic() - started
            metrics.http_finished(request.method, route, status_code, duration)
            logger.info(
                "HTTP request completed",
                extra={
                    "event": "http_request_completed",
                    "duration_seconds": duration,
                    "result": metrics.policy.status_class(status_code),
                },
            )
            reset_log_context(token)

    @app.exception_handler(ValueError)
    async def value_error_handler(_request: Request, error: ValueError):
        return Response(
            content=json.dumps({"detail": redact_text(str(error))}),
            status_code=409,
            media_type="application/json",
        )

    def identity(request: Request, actor: str | None) -> tuple[str, str]:
        return actor or "local-operator", request.state.request_id

    @app.get("/api/v1/health")
    def health() -> dict[str, Any]:
        try:
            with sessions() as session:
                session.execute(text("SELECT 1"))
            database = "healthy"
        except Exception:
            database = "unavailable"
        try:
            management = (
                service.management_status() if database == "healthy" else None
            )
        except Exception:
            management = None
        return aggregate_health(
            management,
            cluster_status.snapshot(),
            database_status=database,
            watchdog=watchdog.snapshot(),
        )

    @app.get("/metrics", include_in_schema=False)
    def prometheus_metrics() -> Response:
        body, content_type = render_metrics(metrics.registry)
        return Response(content=body, media_type=content_type)

    @app.get("/api/v1/cluster")
    def cluster() -> dict[str, Any]:
        result = cluster_status.snapshot()
        try:
            result["synchronization"] = service.synchronization_status()
        except Exception:
            result["synchronization"] = {"status": "unavailable"}
            if result["status"] == "healthy":
                result["status"] = "degraded"
        return result

    @app.get("/api/v1/solutions")
    def list_solutions() -> list[dict[str, Any]]:
        return service.list_solutions()

    @app.post("/api/v1/solutions/refresh")
    def refresh_solutions(
        request: Request,
        x_tvt_actor: str | None = Header(default=None),
    ) -> list[dict[str, Any]]:
        actor, request_id = identity(request, x_tvt_actor)
        return service.refresh_solutions(actor=actor, request_id=request_id)

    @app.get("/api/v1/cluster/workloads/{deployment_name}/telemetry")
    def workload_telemetry(deployment_name: str) -> dict[str, Any]:
        return cluster_status.telemetry(deployment_name)

    @app.get("/api/v1/alerts")
    def list_alerts(
        limit: int = 100, include_resolved: bool = True
    ) -> list[dict[str, Any]]:
        return alerting.list_alerts(limit=limit, include_resolved=include_resolved)

    @app.post("/api/v1/alerts/{alert_id}/acknowledge")
    def acknowledge_alert(
        alert_id: uuid.UUID,
        request: Request,
        x_tvt_actor: str | None = Header(default=None),
    ) -> dict[str, Any]:
        actor, request_id = identity(request, x_tvt_actor)
        return alerting.acknowledge(alert_id, actor, request_id)

    @app.get("/api/v1/alerts/{alert_id}/notifications")
    def alert_notifications(
        alert_id: uuid.UUID, limit: int = 100
    ) -> list[dict[str, Any]]:
        return alerting.notifications(alert_id, limit=limit)

    @app.post("/internal/v1/sites", status_code=201)
    def create_site(
        body: SiteCreate,
        request: Request,
        x_tvt_actor: str | None = Header(default=None),
    ) -> dict[str, Any]:
        actor, request_id = identity(request, x_tvt_actor)
        site = service.create_site(
            body.site_key,
            body.edge_id,
            body.display_name,
            body.timezone,
            actor,
            request_id,
        )
        return {"site_id": site.site_key, "edge_id": site.edge_id}

    @app.get("/api/v1/site")
    def get_site() -> dict[str, Any]:
        site = service.current_site()
        return {
            "site_id": site.site_key,
            "edge_id": site.edge_id,
            "display_name": site.display_name,
            "timezone": site.timezone_name,
            "config_revision": site.config_revision,
        }

    @app.get("/api/v1/cameras")
    def list_cameras() -> list[dict[str, Any]]:
        return service.list_cameras()

    @app.post("/api/v1/cameras", status_code=201)
    def create_camera(
        body: CameraCreate,
        request: Request,
        x_tvt_actor: str | None = Header(default=None),
    ) -> dict[str, Any]:
        actor, request_id = identity(request, x_tvt_actor)
        service.create_camera(
            camera_key=body.camera_id,
            friendly_name=body.friendly_name,
            manufacturer=body.manufacturer,
            model=body.model,
            identifiers=[item.model_dump() for item in body.identifiers],
            rtsp_url=body.rtsp_url,
            actor=actor,
            request_id=request_id,
        )
        return service.get_camera(body.camera_id)

    @app.get("/api/v1/cameras/{camera_id}")
    def get_camera(camera_id: str) -> dict[str, Any]:
        return service.get_camera(camera_id)

    @app.get("/api/v1/cameras/{camera_id}/deployment-status")
    def get_camera_deployment_status(camera_id: str) -> dict[str, Any]:
        return service.get_camera_deployment_status(camera_id)

    @app.patch("/api/v1/cameras/{camera_id}/enabled")
    def set_camera_enabled(
        camera_id: str,
        body: CameraEnabledInput,
        request: Request,
        x_tvt_actor: str | None = Header(default=None),
    ) -> dict[str, Any]:
        actor, request_id = identity(request, x_tvt_actor)
        service.set_camera_enabled(camera_id, body.enabled, actor, request_id)
        return service.get_camera(camera_id)

    @app.put("/api/v1/cameras/{camera_id}/stream")
    def configure_stream(
        camera_id: str,
        body: CameraStreamInput,
        request: Request,
        x_tvt_actor: str | None = Header(default=None),
    ) -> dict[str, Any]:
        actor, request_id = identity(request, x_tvt_actor)
        service.configure_stream(
            camera_id,
            scheme=body.scheme, host=body.host, port=body.port, path=body.path,
            profile_token=body.profile_token, transport=body.transport, codec=body.codec,
            width=body.width, height=body.height, fps=body.fps,
            actor=actor, request_id=request_id,
        )
        return service.get_camera(camera_id)

    @app.put("/api/v1/cameras/{camera_id}/credentials")
    def rotate_credentials(
        camera_id: str,
        body: CameraCredentialsInput,
        request: Request,
        x_tvt_actor: str | None = Header(default=None),
    ) -> dict[str, Any]:
        actor, request_id = identity(request, x_tvt_actor)
        document = {
            key: value for key, value in body.model_dump().items()
            if key != "query" and value is not None
        }
        if body.query:
            document["query"] = body.query
        service.rotate_credentials(camera_id, document, actor, request_id)
        return service.get_camera(camera_id)

    @app.delete("/api/v1/cameras/{camera_id}/credentials", status_code=204)
    def clear_credentials(
        camera_id: str,
        request: Request,
        x_tvt_actor: str | None = Header(default=None),
    ) -> Response:
        actor, request_id = identity(request, x_tvt_actor)
        service.clear_credentials(camera_id, actor, request_id)
        return Response(status_code=204)

    @app.get("/api/v1/cameras/{camera_id}/geometry")
    def list_camera_geometry(camera_id: str) -> dict[str, Any]:
        camera = service.get_camera(camera_id)
        return {
            "shapes": service.list_camera_geometry(camera_id),
            "compiled_config": service.camera_geometry_config(camera_id),
            "geometry_revision": camera["geometry_revision"],
        }

    @app.post("/api/v1/cameras/{camera_id}/zones", status_code=201)
    def create_camera_zone(
        camera_id: str,
        body: CameraZoneInput,
        request: Request,
        x_tvt_actor: str | None = Header(default=None),
    ) -> dict[str, Any]:
        actor, request_id = identity(request, x_tvt_actor)
        return service.create_camera_zone(
            camera_id, body.name, body.points, actor, request_id
        )

    @app.post("/api/v1/cameras/{camera_id}/lines", status_code=201)
    def create_camera_line(
        camera_id: str,
        body: CameraLineInput,
        request: Request,
        x_tvt_actor: str | None = Header(default=None),
    ) -> dict[str, Any]:
        actor, request_id = identity(request, x_tvt_actor)
        return service.create_camera_line(
            camera_id,
            body.name,
            body.points,
            body.role_key,
            body.direction,
            body.inside_side,
            actor,
            request_id,
        )

    @app.put("/api/v1/cameras/{camera_id}/geometry/{shape_id}")
    def update_camera_geometry(
        camera_id: str,
        shape_id: str,
        body: CameraGeometryUpdateInput,
        request: Request,
        x_tvt_actor: str | None = Header(default=None),
    ) -> dict[str, Any]:
        actor, request_id = identity(request, x_tvt_actor)
        return service.update_camera_geometry(
            camera_id,
            shape_id,
            name=body.name,
            points=body.points,
            role_key=body.role_key,
            direction=body.direction,
            inside_side=body.inside_side,
            actor=actor,
            request_id=request_id,
        )

    @app.delete("/api/v1/cameras/{camera_id}/geometry/{shape_id}", status_code=204)
    def delete_camera_geometry(
        camera_id: str,
        shape_id: str,
        request: Request,
        x_tvt_actor: str | None = Header(default=None),
    ) -> Response:
        actor, request_id = identity(request, x_tvt_actor)
        service.delete_camera_geometry(camera_id, shape_id, actor, request_id)
        return Response(status_code=204)

    @app.put("/api/v1/cameras/{camera_id}/role")
    def assign_camera_role(
        camera_id: str,
        body: CameraRoleInput,
        request: Request,
        x_tvt_actor: str | None = Header(default=None),
    ) -> dict[str, Any]:
        actor, request_id = identity(request, x_tvt_actor)
        service.assign_camera_role(
            camera_id,
            body.role_key,
            body.display_name,
            body.direction,
            body.ordinal,
            actor,
            request_id,
        )
        return service.get_camera(camera_id)

    @app.get("/api/v1/cameras/{camera_id}/snapshot")
    def camera_snapshot(camera_id: str) -> Response:
        # Not credential-bearing: this is a single rendered JPEG frame from
        # apexfabric-control's existing ffmpeg-backed snapshot endpoint, never
        # the RTSP URL itself (AGENTS.md security invariants).
        service.get_camera(camera_id)  # 404s cleanly if the camera is unknown
        try:
            result = apex.get("/api/cameras/snapshot", {"camera_id": camera_id})
        except ApexUnavailableError:
            return Response(
                content='{"detail":"camera preview is unavailable"}',
                status_code=502,
                media_type="application/json",
            )
        if result.status != 200:
            return Response(
                content='{"detail":"camera preview is unavailable"}',
                status_code=502,
                media_type="application/json",
            )
        return Response(content=result.body, media_type="image/jpeg")

    @app.delete("/api/v1/cameras/{camera_id}", status_code=204)
    def delete_camera(
        camera_id: str,
        request: Request,
        x_tvt_actor: str | None = Header(default=None),
    ) -> Response:
        actor, request_id = identity(request, x_tvt_actor)
        service.delete_camera(camera_id, actor, request_id)
        return Response(status_code=204)

    @app.post("/internal/v1/deployments/bundles", status_code=201)
    def register_trusted_bundle(
        body: DeploymentInput,
        request: Request,
        x_tvt_actor: str | None = Header(default=None),
    ) -> dict[str, Any]:
        actor, request_id = identity(request, x_tvt_actor)
        if body.namespace != allowed_namespace:
            raise ValueError(
                f"deployment namespace must be {allowed_namespace!r} on this edge"
            )
        deployment = service.register_deployment(
            body.bundle,
            body.namespace,
            body.registry,
            actor,
            request_id,
        )
        return {"deployment_id": deployment.deployment_key}

    @app.post("/api/v1/deployments/preview")
    def preview_deployment(body: CatalogDeploymentPreview) -> dict[str, Any]:
        if body.namespace != allowed_namespace:
            raise ValueError(
                f"deployment namespace must be {allowed_namespace!r} on this edge"
            )
        return service.preview_catalog_deployment(
            catalog_id=body.catalog_id,
            deployment_key=body.deployment_id,
            assignments=[item.model_dump() for item in body.assignments],
            inference_mode=body.inference_mode,
            resources=body.resources.model_dump(),
            state_size=body.state_size,
            namespace=body.namespace,
        )

    @app.post("/api/v1/deployments", status_code=201)
    def commit_catalog_deployment(
        body: CatalogDeploymentCommit,
        request: Request,
        x_tvt_actor: str | None = Header(default=None),
    ) -> dict[str, Any]:
        actor, request_id = identity(request, x_tvt_actor)
        if body.namespace != allowed_namespace:
            raise ValueError(
                f"deployment namespace must be {allowed_namespace!r} on this edge"
            )
        assignment_set = service.commit_catalog_deployment(
            catalog_id=body.catalog_id,
            deployment_key=body.deployment_id,
            assignments=[item.model_dump() for item in body.assignments],
            inference_mode=body.inference_mode,
            resources=body.resources.model_dump(),
            state_size=body.state_size,
            namespace=body.namespace,
            preview_bundle_sha256=body.preview_bundle_sha256,
            idempotency_key=body.idempotency_key,
            actor=actor,
            request_id=request_id,
        )
        return {
            "deployment_id": body.deployment_id,
            "desired_revision": assignment_set.desired_revision,
            "state": "pending",
        }

    @app.get("/api/v1/deployments")
    def list_deployments() -> list[dict[str, Any]]:
        return service.list_deployments()

    @app.get("/api/v1/audit-events")
    def list_audit_events(limit: int = 200) -> list[dict[str, Any]]:
        return service.list_audit_events(limit)

    @app.post("/api/v1/deployments/{deployment_id}/assignments")
    def commit_assignments(
        deployment_id: str,
        body: AssignmentCommit,
        request: Request,
        x_tvt_actor: str | None = Header(default=None),
    ) -> dict[str, Any]:
        actor, request_id = identity(request, x_tvt_actor)
        assignment_set = service.commit_assignments(
            deployment_id,
            [item.model_dump() for item in body.assignments],
            actor,
            request_id,
            body.idempotency_key,
        )
        return {"desired_revision": assignment_set.desired_revision, "state": "pending"}

    @app.post("/api/v1/deployments/{deployment_id}/start")
    def start(
        deployment_id: str,
        request: Request,
        x_tvt_actor: str | None = Header(default=None),
    ) -> dict[str, Any]:
        actor, request_id = identity(request, x_tvt_actor)
        value = service.set_lifecycle(
            deployment_id, "Running", actor, request_id
        )
        return {"desired_revision": value.desired_revision, "state": "pending"}

    @app.post("/api/v1/deployments/{deployment_id}/stop")
    def stop(
        deployment_id: str,
        request: Request,
        x_tvt_actor: str | None = Header(default=None),
    ) -> dict[str, Any]:
        actor, request_id = identity(request, x_tvt_actor)
        value = service.set_lifecycle(
            deployment_id, "Stopped", actor, request_id
        )
        return {"desired_revision": value.desired_revision, "state": "pending"}

    @app.post("/api/v1/deployments/{deployment_id}/rollback")
    def rollback(
        deployment_id: str,
        body: RollbackInput,
        request: Request,
        x_tvt_actor: str | None = Header(default=None),
    ) -> dict[str, Any]:
        actor, request_id = identity(request, x_tvt_actor)
        value = service.rollback(
            deployment_id, body.bundle_sha256, actor, request_id
        )
        return {"desired_revision": value.desired_revision, "state": "pending"}

    @app.post("/api/v1/deployments/{deployment_id}/enrollment/start")
    def start_enrollment(
        deployment_id: str,
        body: EnrollmentStartInput,
        request: Request,
        x_tvt_actor: str | None = Header(default=None),
    ) -> dict[str, Any]:
        actor, request_id = identity(request, x_tvt_actor)
        return service.start_enrollment(
            deployment_key=deployment_id,
            camera_id=body.camera_id,
            duration_seconds=body.duration_seconds,
            actor=actor,
            request_id=request_id,
        )

    @app.post("/api/v1/deployments/{deployment_id}/enrollment/stop")
    def stop_enrollment(
        deployment_id: str,
        body: EnrollmentStopInput,
        request: Request,
        x_tvt_actor: str | None = Header(default=None),
    ) -> dict[str, Any]:
        actor, request_id = identity(request, x_tvt_actor)
        return service.stop_enrollment(
            deployment_key=deployment_id,
            window_id=body.window_id,
            actor=actor,
            request_id=request_id,
        )

    @app.get("/api/v1/deployments/{deployment_id}/enrollment-windows")
    def enrollment_windows(deployment_id: str) -> list[dict[str, Any]]:
        return service.list_enrollment_windows(deployment_id)

    @app.post("/api/v1/deployments/{deployment_id}/enrollment/camera")
    def designate_enrollment_camera(
        deployment_id: str,
        body: EnrollmentCameraDesignationInput,
        request: Request,
        x_tvt_actor: str | None = Header(default=None),
    ) -> dict[str, Any]:
        actor, request_id = identity(request, x_tvt_actor)
        return service.designate_enrollment_camera(
            deployment_key=deployment_id, camera_id=body.camera_id, actor=actor, request_id=request_id,
        )

    @app.get("/api/v1/deployments/{deployment_id}/enrollment/camera")
    def enrollment_camera(deployment_id: str) -> dict[str, Any]:
        return service.get_enrollment_designation(deployment_id) or {
            "deployment_key": deployment_id,
            "camera_id": None,
        }

    @app.post("/api/v1/deployments/{deployment_id}/enrollment/sessions")
    def start_enrollment_session(
        deployment_id: str,
        body: EnrollmentSessionStartInput,
        request: Request,
        x_tvt_actor: str | None = Header(default=None),
    ) -> dict[str, Any]:
        actor, request_id = identity(request, x_tvt_actor)
        return service.start_enrollment_session(
            deployment_key=deployment_id,
            actor=actor,
            request_id=request_id,
            capture_window_seconds=body.capture_window_seconds,
        )

    @app.get("/api/v1/deployments/{deployment_id}/enrollment/sessions")
    def enrollment_sessions(deployment_id: str, limit: int = 20) -> list[dict[str, Any]]:
        return service.list_enrollment_sessions(deployment_id, limit)

    @app.get("/api/v1/deployments/{deployment_id}/enrollment/status")
    def enrollment_status(deployment_id: str) -> dict[str, Any]:
        return service.get_enrollment_status(deployment_id)

    @app.post("/api/v1/deployments/{deployment_id}/enrollment/sessions/{session_id}/cancel")
    def cancel_enrollment_session(
        deployment_id: str,
        session_id: str,
        request: Request,
        x_tvt_actor: str | None = Header(default=None),
    ) -> dict[str, Any]:
        actor, request_id = identity(request, x_tvt_actor)
        return service.cancel_enrollment_session(
            deployment_key=deployment_id, session_id=session_id, actor=actor, request_id=request_id,
        )

    @app.get("/api/v1/enrollment/people")
    def enrollment_people(deployment_id: str | None = None) -> list[dict[str, Any]]:
        return service.list_people_awaiting_names(deployment_id)

    @app.post("/api/v1/enrollment/people/{person_id}/name")
    def set_person_display_name(
        person_id: str,
        body: PersonNameInput,
        request: Request,
        x_tvt_actor: str | None = Header(default=None),
    ) -> dict[str, Any]:
        actor, request_id = identity(request, x_tvt_actor)
        try:
            return service.set_person_display_name(
                person_id=person_id,
                display_name=body.display_name,
                apex=apex,
                actor=actor,
                request_id=request_id,
            )
        except ApexUnavailableError as error:
            metrics.application_error("DATABASE_UNAVAILABLE")
            logger.error(
                "Enrollment naming failed: apex unavailable",
                extra={"event": "enrollment_naming_failed", "error_code": "DATABASE_UNAVAILABLE"},
            )
            raise ValueError("naming is temporarily unavailable") from error

    def _proxy_report(path: str, query: dict[str, str | None]) -> Response:
        # Thin proxy to apexfabric-control's already-implemented, already-tested
        # attendance/vehicle-traffic aggregation (apexfabric/control_plane/reporting.py).
        # Returns plate text and person id/display name, matching what that
        # endpoint already returns -- no new PII exposure.
        try:
            result = apex.get(path, query)
        except ApexUnavailableError:
            return Response(
                content='{"detail":"reporting is unavailable"}',
                status_code=502,
                media_type="application/json",
            )
        return Response(
            content=result.body, status_code=result.status, media_type=result.content_type
        )

    @app.get("/api/v1/reports/attendance")
    def attendance_report(person_id: str | None = None, date: str | None = None) -> Response:
        return _proxy_report(
            "/api/reports/attendance", {"person_id": person_id, "date": date}
        )

    @app.get("/api/v1/reports/attendance-log")
    def attendance_log_report(limit: int = 10) -> Response:
        return _proxy_report("/api/reports/attendance-log", {"limit": str(limit)})

    @app.post("/api/v1/reports/people/{person_id}/name")
    def set_report_person_display_name(
        person_id: str,
        body: PersonNameInput,
        request: Request,
        x_tvt_actor: str | None = Header(default=None),
    ) -> dict[str, Any]:
        actor, request_id = identity(request, x_tvt_actor)
        try:
            return service.set_report_person_display_name(
                person_id=person_id,
                display_name=body.display_name,
                apex=apex,
                actor=actor,
                request_id=request_id,
            )
        except ApexUnavailableError as error:
            metrics.application_error("DATABASE_UNAVAILABLE")
            logger.error(
                "Report naming failed: apex unavailable",
                extra={"event": "report_naming_failed", "error_code": "DATABASE_UNAVAILABLE"},
            )
            raise ValueError("naming is temporarily unavailable") from error

    @app.get("/api/v1/reports/vehicle-traffic")
    def vehicle_traffic_report(date: str | None = None, gate: str | None = None) -> Response:
        return _proxy_report(
            "/api/reports/vehicle-traffic", {"date": date, "gate": gate}
        )

    return app
