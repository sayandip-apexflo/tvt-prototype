"""Host management and synchronization entry point."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

from alembic import command
from alembic.config import Config

from tvt_edge.api import create_app
from tvt_edge.cluster import SyncWorker
from tvt_edge.db.session import build_engine, build_session_factory
from tvt_edge.legacy import import_sqlite_lifecycle
from tvt_edge.security import CredentialKeyring
from tvt_edge.service import ManagementService
from tvt_edge.settings import Settings
from tvt_runtime.cli import kubectl_client


ROOT = Path(__file__).resolve().parents[1]


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="TVT durable edge management plane")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="upgrade the PostgreSQL schema")
    commands.add_parser("check", help="verify database and credential keys")
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
    if args.command == "check":
        from sqlalchemy import text

        with sessions() as session:
            session.execute(text("SELECT 1"))
        print(json.dumps({"database": "healthy", "credential_keys": "healthy"}))
        return 0
    if args.command == "init-site":
        service = ManagementService(sessions, keyring)
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
        result = ManagementService(sessions, keyring).apply_retention()
        print(json.dumps(result, sort_keys=True))
        return 0
    if args.command == "api":
        import uvicorn

        host = args.host or settings.listen_host
        port = args.port or settings.listen_port
        if host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("the Slice 3 API must bind to loopback")
        uvicorn.run(
            create_app(sessions, keyring, settings.sync_namespace),
            host=host,
            port=port,
        )
        return 0
    worker = SyncWorker(
        sessions,
        keyring,
        kubectl_client(settings.kubeconfig),
        worker_id=settings.sync_worker_id,
        rollout_timeout=settings.rollout_timeout,
    )
    while True:
        try:
            result = worker.run_once()
            if result is not None:
                print(json.dumps(result, sort_keys=True), flush=True)
        except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
            # The worker has already persisted a redacted failure. Do not print
            # subprocess input or any secret-bearing object.
            print(json.dumps({"outcome": "failed", "error": type(error).__name__}), flush=True)
            if args.once:
                return 1
        if args.once:
            return 0
        time.sleep(max(1, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
