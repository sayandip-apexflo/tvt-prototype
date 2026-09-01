"""Small loopback-only management API for Slice 3 state mutations."""

from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import FastAPI, Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tvt_edge.security import CredentialKeyring, redact_text
from tvt_edge.service import ManagementService


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


class AssignmentCommit(StrictModel):
    assignments: list[AssignmentInput]
    idempotency_key: str


class RollbackInput(StrictModel):
    bundle_sha256: str


def create_app(
    sessions: sessionmaker[Session],
    keyring: CredentialKeyring,
    allowed_namespace: str = "apexfabric",
) -> FastAPI:
    app = FastAPI(title="TVT edge management", version="1")
    service = ManagementService(sessions, keyring)

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = request_id
        response: Response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

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
        return {"service": "healthy", "database": database}

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

    @app.get("/api/v1/cameras/{camera_id}")
    def get_camera(camera_id: str) -> dict[str, Any]:
        return service.get_camera(camera_id)

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

    @app.post("/api/v1/deployments", status_code=201)
    def register_deployment(
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

    @app.get("/api/v1/deployments")
    def list_deployments() -> list[dict[str, Any]]:
        return service.list_deployments()

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

    return app
