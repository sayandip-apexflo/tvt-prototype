"""Host management and synchronization entry point."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
import uuid
from pathlib import Path

from alembic import command
from alembic.config import Config

from tvt_edge.api import create_app
from tvt_edge.camera import DiscoveryWorker, ValidationWorker
from tvt_edge.cluster import ClusterStatusReader, SyncWorker
from tvt_edge.db.session import build_engine, build_session_factory
from tvt_edge.legacy import import_sqlite_lifecycle
from tvt_edge.security import CredentialKeyring
from tvt_edge.service import ManagementService
from tvt_edge.settings import Settings
from tvt_edge.status import aggregate_health
from tvt_runtime.cli import kubectl_client


ROOT = Path(__file__).resolve().parents[1]


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="TVT durable edge management plane")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="upgrade the PostgreSQL schema")
    commands.add_parser("check", help="verify database and credential keys")
    commands.add_parser(
        "status", aliases=["health"], help="show aggregated management health"
    )
    commands.add_parser(
        "cluster", aliases=["cluster-status"], help="show bounded K3s status"
    )
    commands.add_parser(
        "cameras",
        aliases=["camera-list"],
        help="list camera configuration and health",
    )
    commands.add_parser("discover", help="queue a bounded camera discovery run")
    discovery_runs = commands.add_parser(
        "discovery-runs", help="list or inspect camera discovery operations"
    )
    discovery_runs.add_argument("operation_id", nargs="?", type=uuid.UUID)
    discovery_runs.add_argument("--limit", type=int, default=20)
    validate = commands.add_parser("validate", help="queue camera RTSP validation")
    validate.add_argument("camera_id")
    api = commands.add_parser("api", help="serve the loopback management API")
    api.add_argument("--host")
    api.add_argument("--port", type=int)
    sync = commands.add_parser("sync", help="reconcile committed revisions into K3s")
    sync.add_argument("--once", action="store_true")
    sync.add_argument("--interval", type=int, default=15)
    site = commands.add_parser("init-site", help="create the single V1 site record")
    site.add_argument("site_key")
    site.add_argument("edge_id")
    site.add_argument("display_name")
    site.add_argument("--timezone", default="UTC")
    legacy = commands.add_parser("import-sqlite", help="import Slice 2 lifecycle state")
    legacy.add_argument("database", type=Path)
    legacy.add_argument("--archive-read-only", action="store_true")
    commands.add_parser("retention", help="apply bounded management-history retention")
    return root


def alembic_config(database_url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    settings = Settings.from_environment()
    if args.command == "migrate":
        command.upgrade(alembic_config(settings.database_url), "head")
        return 0
    engine = build_engine(settings.database_url)
    sessions = build_session_factory(engine)
    keyring = CredentialKeyring.from_directory(settings.credential_key_dir)
    service = ManagementService(sessions, keyring)
    cluster_status = ClusterStatusReader(
        kubectl_client(settings.kubeconfig), settings.sync_namespace
    )
    if args.command == "check":
        from sqlalchemy import text

        with sessions() as session:
            session.execute(text("SELECT 1"))
        print(json.dumps({"database": "healthy", "credential_keys": "healthy"}))
        return 0
    if args.command in {"status", "health"}:
        try:
            from sqlalchemy import text

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
        result = aggregate_health(
            management, cluster_status.snapshot(), database_status=database
        )
        print(json.dumps(result, sort_keys=True))
        return 0 if result["status"] == "healthy" else 1
    if args.command in {"cluster", "cluster-status"}:
        result = cluster_status.snapshot()
        try:
            result["synchronization"] = service.synchronization_status()
        except Exception:
            result["synchronization"] = {"status": "unavailable"}
            if result["status"] == "healthy":
                result["status"] = "degraded"
        print(json.dumps(result, sort_keys=True))
        return 0 if result["status"] == "healthy" else 1
    if args.command in {"cameras", "camera-list"}:
        print(json.dumps(service.list_cameras(), sort_keys=True))
        return 0
    if args.command == "discover":
        run = service.queue_discovery(
            "operator", "local-operator", f"cli:discovery:{uuid.uuid4()}"
        )
        print(
            json.dumps(
                {"operation_id": str(run.id), "status": run.status}, sort_keys=True
            )
        )
        return 0
    if args.command == "discovery-runs":
        if args.operation_id is not None:
            result = service.get_discovery_run(args.operation_id)
        else:
            result = service.list_discovery_runs(args.limit)
        print(json.dumps(result, sort_keys=True))
        return 0
    if args.command == "validate":
        attempt = service.queue_validation(
            args.camera_id,
            "operator",
            "local-operator",
            f"cli:validation:{uuid.uuid4()}",
        )
        print(
            json.dumps(
                {
                    "validation_attempt_id": str(attempt.id),
                    "status": attempt.status,
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "init-site":
        site = service.create_site(
            args.site_key,
            args.edge_id,
            args.display_name,
            args.timezone,
            "installer",
            "installer:init-site",
        )
        print(json.dumps({"site_id": site.site_key, "edge_id": site.edge_id}))
        return 0
    if args.command == "import-sqlite":
        result = import_sqlite_lifecycle(
            sessions, args.database, archive_read_only=args.archive_read_only
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    if args.command == "retention":
        result = service.apply_retention()
        print(json.dumps(result, sort_keys=True))
        return 0
    if args.command == "api":
        import uvicorn

        host = args.host or settings.listen_host
        port = args.port or settings.listen_port
        if host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("the Slice 3 API must bind to loopback")
        uvicorn.run(
            create_app(
                sessions,
                keyring,
                settings.sync_namespace,
                kubectl_client(settings.kubeconfig),
            ),
            host=host,
            port=port,
        )
        return 0
    sync_worker = SyncWorker(
        sessions,
        keyring,
        kubectl_client(settings.kubeconfig),
        worker_id=settings.sync_worker_id,
        rollout_timeout=settings.rollout_timeout,
    )
    discovery_worker = DiscoveryWorker(
        sessions,
        onvif_timeout=settings.discovery_onvif_timeout,
        tcp_timeout=settings.discovery_tcp_timeout,
    )
    validation_worker = ValidationWorker(sessions, keyring)
    while True:
        had_error = False
        for name, worker in (
            ("discovery", discovery_worker),
            ("validation", validation_worker),
            ("sync", sync_worker),
        ):
            try:
                result = worker.run_once()
                if result is not None:
                    if hasattr(result, "items"):
                        payload = {"worker": name, **result}
                    else:
                        payload = {"worker": name, "result_id": str(result)}
                    print(json.dumps(payload, sort_keys=True), flush=True)
            except (
                OSError,
                ValueError,
                RuntimeError,
                subprocess.CalledProcessError,
            ) as error:
                had_error = True
                # The worker has already persisted a redacted failure. Do not print
                # subprocess input or any secret-bearing object.
                print(
                    json.dumps(
                        {
                            "worker": name,
                            "outcome": "failed",
                            "error": type(error).__name__,
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
        if had_error:
            if args.once:
                return 1
        if args.once:
            return 0
        time.sleep(max(1, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
