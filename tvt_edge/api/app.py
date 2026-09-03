"""Small loopback-only management API for Slice 3 state mutations."""

from __future__ import annotations

import json
import mimetypes
import time
import uuid
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
from tvt_edge.cluster import ClusterStatusReader
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


class IdentifierInput(StrictModel):
    kind: str
    value: str
    source: str = "operator"
    confidence: str = "asserted"


class CameraCreate(StrictModel):
    camera_id: str
    friendly_name: str
    manufacturer: str | None = None
    model: str | None = None
    identifiers: list[IdentifierInput] = Field(default_factory=list)


class DiscoveryScopeInput(StrictModel):
    interface_name: str
    cidr: str
    rtsp_ports: list[int] = Field(default_factory=lambda: [554])
    enabled: bool = True


class StreamInput(StrictModel):
    scheme: str = "rtsp"
    host: str
    port: int = 554
    path: str
    profile_token: str
    transport: str = "tcp"
    codec: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None


class CredentialInput(StrictModel):
    username: str | None = None
    password: str | None = None
    query: dict[str, str] = Field(default_factory=dict)
    path_suffix: str | None = None


class CameraEnabledInput(StrictModel):
    enabled: bool


class CameraRoleInput(StrictModel):
    role_key: str
    display_name: str
    direction: str = "unknown"
    ordinal: int | None = None


class ValidationResultInput(StrictModel):
    result_code: str
    safe_result: dict[str, Any] = Field(default_factory=dict)


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
    static_dir: Path | None = None,
) -> FastAPI:
    app = FastAPI(
        title="TVT edge management",
        version="1",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    service = ManagementService(sessions, keyring)
    alerting = AlertingService(sessions)
    cluster_status = ClusterStatusReader(kubectl, allowed_namespace)
    metrics = EdgeMetrics("edge-management")
    metrics.set_build(__version__)
    watchdog = WatchdogStatusReader(watchdog_state_path)
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

    @app.post("/api/v1/cameras", status_code=201)
    def create_camera(
        body: CameraCreate,
        request: Request,
        x_tvt_actor: str | None = Header(default=None),
    ) -> dict[str, Any]:
        actor, request_id = identity(request, x_tvt_actor)
        camera = service.create_camera(
            camera_key=body.camera_id,
            friendly_name=body.friendly_name,
            manufacturer=body.manufacturer,
            model=body.model,
            identifiers=[item.model_dump() for item in body.identifiers],
            actor=actor,
            request_id=request_id,
        )
        return {"camera_id": camera.camera_key, "state": camera.onboarding_state}

    @app.get("/api/v1/cameras")
    def list_cameras() -> list[dict[str, Any]]:
        return service.list_cameras()

    @app.get("/api/v1/discovery-runs")
    def list_discovery_runs(limit: int = 20) -> list[dict[str, Any]]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        return service.list_discovery_runs(limit)

    @app.get("/api/v1/discovery-scopes")
    def list_discovery_scopes() -> list[dict[str, Any]]:
        return service.list_discovery_scopes()

    @app.post("/api/v1/discovery-scopes", status_code=201)
    def create_discovery_scope(
        body: DiscoveryScopeInput,
        request: Request,
        x_tvt_actor: str | None = Header(default=None),
    ) -> dict[str, Any]:
        actor, request_id = identity(request, x_tvt_actor)
        return service.create_discovery_scope(
            **body.model_dump(), actor=actor, request_id=request_id
        )

    @app.delete("/api/v1/discovery-scopes/{scope_id}", status_code=204)
    def delete_discovery_scope(
        scope_id: uuid.UUID,
        request: Request,
        x_tvt_actor: str | None = Header(default=None),
    ) -> Response:
        actor, request_id = identity(request, x_tvt_actor)
        service.delete_discovery_scope(scope_id, actor, request_id)
        return Response(status_code=204)

    @app.get("/api/v1/discovery-runs/{operation_id}")
    def get_discovery_run(
        operation_id: uuid.UUID, observation_limit: int = 100
    ) -> dict[str, Any]:
        return service.get_discovery_run(operation_id, observation_limit)

    @app.get("/api/v1/cameras/{camera_id}")
    def get_camera(camera_id: str) -> dict[str, Any]:
        return service.get_camera(camera_id)

    @app.get("/api/v1/cameras/{camera_id}/validation-attempts")
    def list_validation_attempts(
        camera_id: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        return service.list_validation_attempts(camera_id, limit)

    @app.put("/api/v1/cameras/{camera_id}/stream")
    def configure_stream(
        camera_id: str,
        body: StreamInput,
        request: Request,
        x_tvt_actor: str | None = Header(default=None),
    ) -> dict[str, Any]:
        actor, request_id = identity(request, x_tvt_actor)
        profile = service.configure_stream(
            camera_id, **body.model_dump(), actor=actor, request_id=request_id
        )
        return {"profile_id": str(profile.id), "selected": True}

    @app.put("/api/v1/cameras/{camera_id}/credentials", status_code=204)
    def rotate_credentials(
        camera_id: str,
        body: CredentialInput,
        request: Request,
        x_tvt_actor: str | None = Header(default=None),
    ) -> Response:
        actor, request_id = identity(request, x_tvt_actor)
        service.rotate_credentials(
            camera_id,
            body.model_dump(exclude_none=True),
            actor,
            request_id,
        )
        return Response(status_code=204)

    @app.delete("/api/v1/cameras/{camera_id}/credentials", status_code=204)
    def clear_credentials(
        camera_id: str,
        request: Request,
        x_tvt_actor: str | None = Header(default=None),
    ) -> Response:
        actor, request_id = identity(request, x_tvt_actor)
        service.clear_credentials(camera_id, actor, request_id)
        return Response(status_code=204)

    @app.post("/api/v1/cameras/{camera_id}/roles", status_code=201)
    def assign_role(
        camera_id: str,
        body: CameraRoleInput,
        request: Request,
        x_tvt_actor: str | None = Header(default=None),
    ) -> dict[str, Any]:
        actor, request_id = identity(request, x_tvt_actor)
        assignment = service.assign_camera_role(
            camera_id,
            body.role_key,
            body.display_name,
            body.direction,
            body.ordinal,
            actor,
            request_id,
        )
        return {"role_assignment_id": str(assignment.id)}

    @app.patch("/api/v1/cameras/{camera_id}/enabled")
    def set_camera_enabled(
        camera_id: str,
        body: CameraEnabledInput,
        request: Request,
        x_tvt_actor: str | None = Header(default=None),
    ) -> dict[str, Any]:
        actor, request_id = identity(request, x_tvt_actor)
        camera = service.set_camera_enabled(
            camera_id, body.enabled, actor, request_id
        )
        return {"camera_id": camera.camera_key, "enabled": camera.enabled}

    @app.post("/api/v1/cameras/{camera_id}/validate", status_code=202)
    def queue_validation(
        camera_id: str,
        request: Request,
        x_tvt_actor: str | None = Header(default=None),
    ) -> dict[str, Any]:
        actor, request_id = identity(request, x_tvt_actor)
        attempt = service.queue_validation(
            camera_id, "operator", actor, request_id
        )
        return {"validation_attempt_id": str(attempt.id), "status": attempt.status}

    @app.post("/internal/v1/validation-attempts/{attempt_id}/result")
    def validation_result(
        attempt_id: uuid.UUID,
        body: ValidationResultInput,
        request: Request,
        x_tvt_actor: str | None = Header(default=None),
    ) -> dict[str, Any]:
        actor, request_id = identity(request, x_tvt_actor)
        attempt = service.record_validation_result(
            attempt_id,
            result_code=body.result_code,
            safe_result=body.safe_result,
            actor=actor,
            request_id=request_id,
        )
        return {"validation_attempt_id": str(attempt.id), "status": attempt.status}

    @app.delete("/api/v1/cameras/{camera_id}", status_code=204)
    def delete_camera(
        camera_id: str,
        request: Request,
        x_tvt_actor: str | None = Header(default=None),
    ) -> Response:
        actor, request_id = identity(request, x_tvt_actor)
        service.delete_camera(camera_id, actor, request_id)
        return Response(status_code=204)

    @app.post("/api/v1/discovery-runs", status_code=202)
    def queue_discovery(
        request: Request,
        x_tvt_actor: str | None = Header(default=None),
    ) -> dict[str, Any]:
        actor, request_id = identity(request, x_tvt_actor)
        run = service.queue_discovery("operator", actor, request_id)
        return {"operation_id": str(run.id), "status": run.status}

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

    ui_root = static_dir or Path(__file__).resolve().parents[1] / "static"
    ui_index = ui_root / "index.html"
    assets = ui_root / "assets"
    if ui_index.is_file():
        ui_document = ui_index.read_bytes()
        ui_assets = {
            path.relative_to(assets).as_posix(): path.read_bytes()
            for path in assets.rglob("*")
            if path.is_file()
        } if assets.is_dir() else {}

        @app.get("/assets/{asset_path:path}", include_in_schema=False)
        async def ui_asset(asset_path: str) -> Response:
            content = ui_assets.get(asset_path)
            if content is None:
                return Response(
                    content='{"detail":"not found"}',
                    status_code=404,
                    media_type="application/json",
                )
            media_type = mimetypes.guess_type(asset_path)[0] or "application/octet-stream"
            return Response(content=content, media_type=media_type)

        @app.get("/", include_in_schema=False)
        async def ui_index_page() -> Response:
            return Response(content=ui_document, media_type="text/html")

        @app.get("/{ui_path:path}", include_in_schema=False)
        async def ui_fallback(ui_path: str) -> Response:
            if ui_path in {"docs", "redoc", "openapi.json", "metrics"} or ui_path.startswith(("api/", "internal/")):
                return Response(
                    content='{"detail":"not found"}',
                    status_code=404,
                    media_type="application/json",
                )
            return Response(content=ui_document, media_type="text/html")

    return app
