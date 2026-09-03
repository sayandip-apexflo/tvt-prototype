"""Authenticated host-local Alertmanager receiver and dispatcher health API."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from tvt_edge import __version__
from tvt_edge.alerting.contracts import parse_alertmanager_payload
from tvt_edge.alerting.outbox import OutboxWorker
from tvt_edge.alerting.service import AlertingService
from tvt_edge.db.models import AlertInstance, NotificationOutbox
from tvt_edge.db.models import utc_now
from tvt_edge.observability import (
    AlertDispatcherMetrics,
    HttpMetrics,
    bind_log_context,
    render_metrics,
    request_id_or_new,
    reset_log_context,
)


MAX_WEBHOOK_BYTES = 256 * 1024
LOGGER = logging.getLogger("tvt-alert-dispatcher")


def create_alert_app(
    sessions: sessionmaker[Session],
    bearer_token: str,
    *,
    worker: OutboxWorker | None = None,
    metrics: AlertDispatcherMetrics | None = None,
    poll_interval: float = 5.0,
) -> FastAPI:
    if len(bearer_token) < 24:
        raise ValueError("alert receiver bearer token must contain at least 24 characters")
    service = AlertingService(sessions)
    metrics = metrics or AlertDispatcherMetrics()
    http_metrics = HttpMetrics("alert-dispatcher", registry=metrics.registry)
    http_metrics.set_build(__version__)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        task: asyncio.Task[None] | None = None
        if worker is not None:

            async def run_worker() -> None:
                while True:
                    try:
                        await asyncio.to_thread(worker.run_once)
                    except Exception as error:
                        # Keep the dispatcher alive; persistence and bounded error
                        # categories are handled by the worker where possible.
                        LOGGER.error(
                            "alert worker cycle failed",
                            extra={"error_category": type(error).__name__},
                        )
                    await asyncio.sleep(max(0.25, poll_interval))

            task = asyncio.create_task(run_worker())
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
        title="TVT alert dispatcher",
        version="1",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.middleware("http")
    async def request_observability(request: Request, call_next):
        request_id = request_id_or_new(request.headers.get("X-Request-ID"))
        token = bind_log_context(request_id=request_id)
        started = time.monotonic()
        status_code = 500
        http_metrics.http_started()
        try:
            response: Response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-ID"] = request_id
            return response
        except Exception:
            http_metrics.application_error("INTERNAL_ERROR")
            LOGGER.exception(
                "Alert dispatcher request failed",
                extra={"event": "http_request_failed", "error_code": "INTERNAL_ERROR"},
            )
            raise
        finally:
            route = getattr(request.scope.get("route"), "path", None)
            duration = time.monotonic() - started
            http_metrics.http_finished(request.method, route, status_code, duration)
            LOGGER.info(
                "Alert dispatcher request completed",
                extra={
                    "event": "http_request_completed",
                    "duration_seconds": duration,
                    "result": http_metrics.policy.status_class(status_code),
                },
            )
            reset_log_context(token)

    @app.post("/internal/v1/alerts/alertmanager", status_code=202)
    async def alertmanager(request: Request) -> dict[str, Any]:
        authorization = request.headers.get("Authorization", "")
        supplied = authorization[7:] if authorization.startswith("Bearer ") else ""
        if not supplied or not hmac.compare_digest(supplied, bearer_token):
            metrics.event("alertmanager", "unknown", "rejected")
            raise HTTPException(status_code=401, detail="invalid receiver credential")
        content_type = request.headers.get("Content-Type", "").split(";", 1)[0].lower()
        if content_type != "application/json":
            metrics.event("alertmanager", "unknown", "rejected")
            raise HTTPException(
                status_code=415, detail="content type must be application/json"
            )
        content_length = request.headers.get("Content-Length")
        if content_length:
            try:
                if int(content_length) > MAX_WEBHOOK_BYTES:
                    metrics.event("alertmanager", "unknown", "rejected")
                    raise HTTPException(status_code=413, detail="webhook body is too large")
            except ValueError as error:
                metrics.event("alertmanager", "unknown", "rejected")
                raise HTTPException(status_code=400, detail="invalid content length") from error
        body = await request.body()
        if len(body) > MAX_WEBHOOK_BYTES:
            metrics.event("alertmanager", "unknown", "rejected")
            raise HTTPException(status_code=413, detail="webhook body is too large")
        try:
            payload = json.loads(body)
            events = parse_alertmanager_payload(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            metrics.event("alertmanager", "unknown", "rejected")
            raise HTTPException(status_code=422, detail="invalid alert webhook") from error
        try:
            results = service.ingest_many(events)
        except SQLAlchemyError as error:
            for event in events:
                metrics.event("alertmanager", event["status"], "persistence_error")
            raise HTTPException(status_code=503, detail="alert persistence unavailable") from error
        for event in events:
            metrics.event("alertmanager", event["status"], "accepted")
        return {
            "accepted": len(results),
            "duplicates": sum(1 for result in results if result["duplicate"]),
            "notifications_queued": sum(
                result["notifications_queued"] for result in results
            ),
        }

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "healthy"}

    @app.get("/readyz")
    def readyz() -> dict[str, str]:
        try:
            with sessions() as session:
                session.execute(text("SELECT 1"))
        except SQLAlchemyError as error:
            raise HTTPException(status_code=503, detail="database unavailable") from error
        return {"status": "ready"}

    @app.get("/metrics")
    def prometheus_metrics() -> Response:
        try:
            with sessions() as session:
                active = dict(
                    session.execute(
                        select(AlertInstance.severity, func.count())
                        .where(AlertInstance.state != "resolved")
                        .group_by(AlertInstance.severity)
                    ).all()
                )
                outbox = dict(
                    session.execute(
                        select(NotificationOutbox.state, func.count()).group_by(
                            NotificationOutbox.state
                        )
                    ).all()
                )
                oldest = session.scalar(
                    select(func.min(NotificationOutbox.created_at)).where(
                        NotificationOutbox.state.in_(("pending", "delivering"))
                    )
                )
                if oldest is None:
                    oldest_age = 0.0
                else:
                    if oldest.tzinfo is None:
                        oldest = oldest.replace(tzinfo=timezone.utc)
                    oldest_age = max(0.0, (utc_now() - oldest).total_seconds())
        except SQLAlchemyError:
            active, outbox, oldest_age = {}, {}, 0.0
        metrics.snapshot(active=active, outbox=outbox, oldest_pending_age=oldest_age)
        body, content_type = render_metrics(metrics.registry)
        return Response(content=body, media_type=content_type)

    return app
