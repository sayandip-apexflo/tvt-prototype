"""Non-secret deployment history for the local TVT Solution Pack runtime."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from apexfabric.solution_management.renderer import revision


DEFAULT_STATE_DIR = Path(os.getenv("TVT_STATE_DIR", "/var/lib/tvt/runtime"))
RTSP_URL = re.compile(r"rtsps?://[^\s\"']+", re.IGNORECASE)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_bundle_has_no_inline_secrets(bundle: dict[str, Any]) -> None:
    """Reject secret-bearing bundles before they can enter revision history."""

    for index, application in enumerate(bundle.get("applications", [])):
        if application.get("secrets"):
            raise ValueError(
                f"applications[{index}].secrets is unsupported by TVT; "
                "supply credentials as ephemeral secret_inputs"
            )


class DeploymentStore:
    def __init__(self, state_dir: Path = DEFAULT_STATE_DIR):
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.state_dir, 0o700)
        self.database = self.state_dir / "runtime.sqlite3"
        self._initialize()
        os.chmod(self.database, 0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS deployments (
                    deployment_id TEXT PRIMARY KEY,
                    namespace TEXT NOT NULL,
                    active_revision TEXT NOT NULL,
                    desired_state TEXT NOT NULL,
                    registry TEXT NOT NULL,
                    status_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS revisions (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    deployment_id TEXT NOT NULL,
                    revision TEXT NOT NULL,
                    bundle_json TEXT NOT NULL,
                    registry TEXT NOT NULL,
                    action TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(deployment_id, revision),
                    FOREIGN KEY(deployment_id) REFERENCES deployments(deployment_id)
                        DEFERRABLE INITIALLY DEFERRED
                );
                CREATE TABLE IF NOT EXISTS lifecycle_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    deployment_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    revision TEXT,
                    outcome TEXT NOT NULL,
                    detail_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def record_success(
        self,
        bundle: dict[str, Any],
        namespace: str,
        registry: str,
        action: str,
        status: dict[str, Any],
    ) -> str:
        ensure_bundle_has_no_inline_secrets(bundle)
        digest = revision(bundle)
        deployment_id = bundle["deployment_id"]
        desired_state = "Stopped" if all(
            application.get("lifecycle", {}).get("desired_state", "Running")
            == "Stopped"
            for application in bundle["applications"]
        ) else "Running"
        bundle_json = json.dumps(bundle, sort_keys=True, separators=(",", ":"))
        status_json = json.dumps(status, sort_keys=True, separators=(",", ":"))
        timestamp = utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO deployments (
                    deployment_id, namespace, active_revision, desired_state,
                    registry, status_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(deployment_id) DO UPDATE SET
                    namespace=excluded.namespace,
                    active_revision=excluded.active_revision,
                    desired_state=excluded.desired_state,
                    registry=excluded.registry,
                    status_json=excluded.status_json,
                    updated_at=excluded.updated_at
                """,
                (
                    deployment_id,
                    namespace,
                    digest,
                    desired_state,
                    registry,
                    status_json,
                    timestamp,
                ),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO revisions (
                    deployment_id, revision, bundle_json, registry, action,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (deployment_id, digest, bundle_json, registry, action, timestamp),
            )
            connection.execute(
                """
                INSERT INTO lifecycle_events (
                    deployment_id, action, revision, outcome, detail_json,
                    created_at
                ) VALUES (?, ?, ?, 'succeeded', ?, ?)
                """,
                (deployment_id, action, digest, status_json, timestamp),
            )
        return digest

    def record_failure(
        self,
        deployment_id: str,
        action: str,
        detail: str,
        attempted_revision: str | None = None,
        secret_update_attempted: bool = False,
    ) -> None:
        safe_detail = RTSP_URL.sub("[REDACTED_RTSP_URL]", detail)
        safe_detail = safe_detail.replace("\n", " ")[-1000:]
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO lifecycle_events (
                    deployment_id, action, revision, outcome, detail_json,
                    created_at
                ) VALUES (?, ?, ?, 'failed', ?, ?)
                """,
                (
                    deployment_id,
                    action,
                    attempted_revision,
                    json.dumps(
                        {
                            "error": safe_detail,
                            "secret_update_attempted": secret_update_attempted,
                        },
                        separators=(",", ":"),
                    ),
                    utc_now(),
                ),
            )

    def list_deployments(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM deployments ORDER BY deployment_id"
            ).fetchall()
        return [self._deployment_row(row) for row in rows]

    def get_deployment(self, deployment_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM deployments WHERE deployment_id = ?",
                (deployment_id,),
            ).fetchone()
            event = connection.execute(
                """
                SELECT action, revision, outcome, detail_json, created_at
                FROM lifecycle_events WHERE deployment_id = ?
                ORDER BY sequence DESC LIMIT 1
                """,
                (deployment_id,),
            ).fetchone()
        if row is None:
            return None
        result = self._deployment_row(row)
        if event is not None:
            result["last_event"] = dict(event)
            result["last_event"]["detail"] = json.loads(
                result["last_event"].pop("detail_json")
            )
        return result

    @staticmethod
    def _deployment_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["status"] = json.loads(result.pop("status_json"))
        return result

    def get_bundle(
        self, deployment_id: str, selected_revision: str | None = None
    ) -> dict[str, Any]:
        deployment = self.get_deployment(deployment_id)
        if deployment is None:
            raise ValueError(f"unknown deployment {deployment_id!r}")
        selected_revision = selected_revision or deployment["active_revision"]
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT bundle_json FROM revisions
                WHERE deployment_id = ? AND revision = ?
                """,
                (deployment_id, selected_revision),
            ).fetchone()
        if row is None:
            raise ValueError(
                f"unknown revision {selected_revision!r} for {deployment_id!r}"
            )
        return json.loads(row["bundle_json"])

    def history(self, deployment_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT revision, action, created_at FROM revisions
                WHERE deployment_id = ? ORDER BY sequence DESC
                """,
                (deployment_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def previous_revision(self, deployment_id: str) -> str:
        deployment = self.get_deployment(deployment_id)
        if deployment is None:
            raise ValueError(f"unknown deployment {deployment_id!r}")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT revision FROM revisions
                WHERE deployment_id = ? AND revision != ?
                ORDER BY sequence DESC LIMIT 1
                """,
                (deployment_id, deployment["active_revision"]),
            ).fetchone()
        if row is None:
            raise ValueError(f"deployment {deployment_id!r} has no prior revision")
        return row["revision"]
