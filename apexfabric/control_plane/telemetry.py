"""Durable server-side analytics and snapshot retention."""

from __future__ import annotations

import logging

from .alerts import SCHEMA as ALERT_SCHEMA, evaluate as evaluate_alerts
from .identity import (
    SCHEMA as IDENTITY_SCHEMA,
    IdentityPolicy,
    ensure_vector_tables,
    load_extension as load_identity_extension,
    resolve_identity,
)
from .reporting import (
    SCHEMA as REPORTING_SCHEMA,
    evaluate_attendance,
    evaluate_vehicle_traffic,
)

import hashlib
import json
import os
import shutil
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable


@dataclass(frozen=True)
class RetentionPolicy:
    maximum_bytes: int = 40 * 1024**2
    high_watermark: float = 0.90
    low_watermark: float = 0.80
    maximum_age_seconds: int = 14 * 24 * 60 * 60
    maximum_events: int = 1_000_000
    maximum_snapshot_bytes: int = 20 * 1024**2
    maximum_event_bytes: int = 1024**2
    maximum_snapshots_per_event: int = 16
    minimum_free_bytes: int = 5 * 1024**3

    def __post_init__(self) -> None:
        positive = (
            self.maximum_bytes, self.maximum_events, self.maximum_age_seconds,
            self.maximum_snapshot_bytes, self.maximum_event_bytes,
            self.maximum_snapshots_per_event, self.minimum_free_bytes,
        )
        if any(value < 1 for value in positive):
            raise ValueError("retention limits must be positive")
        if not 0 < self.low_watermark < self.high_watermark <= 1:
            raise ValueError("retention watermarks must satisfy 0 < low < high <= 1")

    @classmethod
    def from_environment(cls) -> "RetentionPolicy":
        return cls(
            maximum_bytes=int(os.getenv("APEXFABRIC_TELEMETRY_MAX_BYTES", str(40 * 1024**2))),
            high_watermark=float(os.getenv("APEXFABRIC_TELEMETRY_HIGH_WATERMARK", "0.90")),
            low_watermark=float(os.getenv("APEXFABRIC_TELEMETRY_LOW_WATERMARK", "0.80")),
            maximum_age_seconds=int(os.getenv("APEXFABRIC_TELEMETRY_MAX_AGE_SECONDS", str(14 * 24 * 60 * 60))),
            maximum_events=int(os.getenv("APEXFABRIC_TELEMETRY_MAX_EVENTS", "1000000")),
            maximum_snapshot_bytes=int(os.getenv("APEXFABRIC_TELEMETRY_MAX_SNAPSHOT_BYTES", str(20 * 1024**2))),
            maximum_event_bytes=int(os.getenv("APEXFABRIC_TELEMETRY_MAX_EVENT_BYTES", str(1024**2))),
            maximum_snapshots_per_event=int(os.getenv("APEXFABRIC_TELEMETRY_MAX_SNAPSHOTS_PER_EVENT", "16")),
            minimum_free_bytes=int(os.getenv("APEXFABRIC_TELEMETRY_MIN_FREE_BYTES", str(5 * 1024**3))),
        )


def snapshot_urls(payload: Any) -> list[str]:
    found: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)
        elif isinstance(value, str) and value.startswith("/snapshots/"):
            path = PurePosixPath(value)
            if ".." not in path.parts and not value.startswith("//"):
                found.add(value)

    visit(payload)
    return sorted(found)


class TelemetryStore:
    def __init__(
        self,
        root: Path,
        policy: RetentionPolicy | None = None,
        identity_policy: IdentityPolicy | None = None,
    ):
        self.root = root
        self.snapshot_root = root / "snapshots"
        self.database = root / "telemetry.sqlite3"
        self.policy = policy or RetentionPolicy.from_environment()
        self.identity_policy = identity_policy or IdentityPolicy.from_environment()
        self.root.mkdir(parents=True, exist_ok=True)
        self.snapshot_root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self._connect() as connection:
            connection.executescript("""
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    deployment_id TEXT NOT NULL,
                    occurred_at TEXT,
                    received_at REAL NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_bytes INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_received_at ON events(received_at);
                CREATE INDEX IF NOT EXISTS events_deployment_received ON events(deployment_id, received_at);
                CREATE TABLE IF NOT EXISTS snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    relative_path TEXT NOT NULL UNIQUE,
                    content_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS event_snapshots (
                    event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE CASCADE,
                    snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id) ON DELETE CASCADE,
                    source_url TEXT NOT NULL,
                    PRIMARY KEY(event_id, snapshot_id, source_url)
                );
            """)

            connection.executescript(ALERT_SCHEMA)
            connection.executescript(IDENTITY_SCHEMA)
            connection.executescript(REPORTING_SCHEMA)
            if self.identity_policy is not None:
                load_identity_extension(connection)
                ensure_vector_tables(connection, self.identity_policy)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        if self.identity_policy is not None:
            load_identity_extension(connection)
        return connection

    @staticmethod
    def _event_id(deployment_id: str, payload: dict[str, Any]) -> str:
        supplied = payload.get("event_id") or payload.get("id")
        if isinstance(supplied, str) and supplied:
            return f"{deployment_id}:{supplied}"
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return f"{deployment_id}:sha256:{hashlib.sha256(canonical).hexdigest()}"

    def ingest(
        self,
        deployment_id: str,
        payload: dict[str, Any],
        fetch_snapshot: Callable[[str, int], tuple[bytes, str]] | None = None,
    ) -> str:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        if len(canonical.encode()) > self.policy.maximum_event_bytes:
            raise ValueError(f"event exceeds {self.policy.maximum_event_bytes} bytes")
        event_id = self._event_id(deployment_id, payload)
        with self._connect() as connection:
            if connection.execute("SELECT 1 FROM events WHERE event_id = ?", (event_id,)).fetchone():
                return event_id
        received_at = time.time()
        occurred_at = payload.get("time") or payload.get("occurred_at") or payload.get("timestamp")
        # Events and alerts commit together before fetching optional media.
        with self.lock, self._connect() as connection:
            inserted = connection.execute(
                "INSERT OR IGNORE INTO events VALUES (?, ?, ?, ?, ?, ?)",
                (event_id, deployment_id, occurred_at, received_at, canonical, len(canonical.encode())),
            ).rowcount
            if inserted:
                evaluate_alerts(connection, event_id, deployment_id, payload, received_at)
                resolved_person_id = resolve_identity(
                    connection, event_id, deployment_id, payload, self.identity_policy, received_at
                )
                evaluate_attendance(connection, event_id, deployment_id, payload, resolved_person_id, received_at)
                evaluate_vehicle_traffic(connection, event_id, deployment_id, payload, received_at)
        if inserted and fetch_snapshot:
            urls = snapshot_urls(payload)
            for source_url in urls[:self.policy.maximum_snapshots_per_event]:
                try:
                    content, content_type = fetch_snapshot(source_url, self.policy.maximum_snapshot_bytes)
                    if len(content) > self.policy.maximum_snapshot_bytes:
                        raise ValueError("snapshot exceeds maximum size")
                    snapshot_id = hashlib.sha256(content).hexdigest()
                    suffix = ".jpg" if content_type in {"image/jpeg", "image/jpg"} else ".bin"
                    relative = f"{snapshot_id[:2]}/{snapshot_id}{suffix}"
                    target = self.snapshot_root / relative
                    with self.lock, self._connect() as connection:
                        if not connection.execute("SELECT 1 FROM events WHERE event_id=?", (event_id,)).fetchone():
                            break
                        target.parent.mkdir(parents=True, exist_ok=True)
                        if not target.exists():
                            temporary = target.with_suffix(target.suffix + ".tmp")
                            temporary.write_bytes(content)
                            os.replace(temporary, target)
                        connection.execute("INSERT OR IGNORE INTO snapshots VALUES (?, ?, ?, ?, ?)",
                                           (snapshot_id, relative, content_type, len(content), received_at))
                        connection.execute("INSERT OR IGNORE INTO event_snapshots VALUES (?, ?, ?)",
                                           (event_id, snapshot_id, source_url))
                except Exception:
                    logging.getLogger(__name__).warning("Snapshot unavailable for event %s; event and alerts retained", event_id)
        self.enforce_retention()
        return event_id

    def _logical_bytes(self, connection: sqlite3.Connection) -> int:
        event_bytes = connection.execute("SELECT COALESCE(SUM(payload_bytes), 0) FROM events").fetchone()[0]
        snapshot_bytes = connection.execute("SELECT COALESCE(SUM(size_bytes), 0) FROM snapshots").fetchone()[0]
        return int(event_bytes) + int(snapshot_bytes)

    def _delete_events(self, connection: sqlite3.Connection, event_ids: list[str]) -> int:
        if not event_ids:
            return 0
        placeholders = ",".join("?" for _ in event_ids)
        connection.execute(f"DELETE FROM events WHERE event_id IN ({placeholders})", event_ids)
        orphans = connection.execute("""
            SELECT snapshot_id, relative_path FROM snapshots
            WHERE NOT EXISTS (
                SELECT 1 FROM event_snapshots WHERE event_snapshots.snapshot_id = snapshots.snapshot_id
            )
        """).fetchall()
        for orphan in orphans:
            (self.snapshot_root / orphan["relative_path"]).unlink(missing_ok=True)
            connection.execute("DELETE FROM snapshots WHERE snapshot_id = ?", (orphan["snapshot_id"],))
        return len(event_ids)

    @staticmethod
    def _oldest_batch(connection: sqlite3.Connection, maximum: int = 500) -> list[sqlite3.Row]:
        """Return an efficient eviction batch while preserving the newest event."""
        count = int(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0])
        if not count:
            return []
        limit = 1 if count == 1 else min(maximum, count - 1)
        return connection.execute(
            "SELECT event_id FROM events ORDER BY received_at LIMIT ?", (limit,)
        ).fetchall()

    def enforce_retention(self, now: float | None = None) -> dict[str, int]:
        current_time = now if now is not None else time.time()
        removed = 0
        with self.lock, self._connect() as connection:
            expired = connection.execute(
                "SELECT event_id FROM events WHERE received_at < ? ORDER BY received_at",
                (current_time - self.policy.maximum_age_seconds,),
            ).fetchall()
            removed += self._delete_events(connection, [row["event_id"] for row in expired])
            count = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            if count > self.policy.maximum_events:
                excess = connection.execute(
                    "SELECT event_id FROM events ORDER BY received_at LIMIT ?",
                    (count - self.policy.maximum_events,),
                ).fetchall()
                removed += self._delete_events(connection, [row["event_id"] for row in excess])
            high = int(self.policy.maximum_bytes * self.policy.high_watermark)
            low = int(self.policy.maximum_bytes * self.policy.low_watermark)
            while self._logical_bytes(connection) >= high:
                oldest = self._oldest_batch(connection)
                if not oldest:
                    break
                removed += self._delete_events(connection, [row["event_id"] for row in oldest])
                if self._logical_bytes(connection) <= low:
                    break
            while shutil.disk_usage(self.root).free < self.policy.minimum_free_bytes:
                oldest = self._oldest_batch(connection)
                if not oldest:
                    break
                removed += self._delete_events(connection, [row["event_id"] for row in oldest])
            logical_bytes = self._logical_bytes(connection)
            events = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            snapshots = connection.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        return {"removed_events": removed, "events": events, "snapshots": snapshots, "logical_bytes": logical_bytes}

    def recent_events(self, deployment_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 1000))
        query = "SELECT * FROM events"
        values: list[Any] = []
        if deployment_id:
            query += " WHERE deployment_id = ?"
            values.append(deployment_id)
        query += " ORDER BY received_at DESC LIMIT ?"
        values.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, values).fetchall()
            result = []
            for row in rows:
                snapshots = connection.execute(
                    "SELECT snapshot_id, source_url FROM event_snapshots WHERE event_id = ?",
                    (row["event_id"],),
                ).fetchall()
                result.append({
                    "event_id": row["event_id"], "deployment_id": row["deployment_id"],
                    "occurred_at": row["occurred_at"], "received_at": row["received_at"],
                    "payload": json.loads(row["payload_json"]),
                    "snapshots": [{"snapshot_id": item["snapshot_id"], "source_url": item["source_url"], "url": f"/api/telemetry/snapshots/{item['snapshot_id']}"} for item in snapshots],
                })
        return result

    def snapshot(self, snapshot_id: str) -> tuple[Path, str] | None:
        if not re_full_sha256(snapshot_id):
            return None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT relative_path, content_type FROM snapshots WHERE snapshot_id = ?", (snapshot_id,)
            ).fetchone()
        return (self.snapshot_root / row["relative_path"], row["content_type"]) if row else None

    def stats(self) -> dict[str, Any]:
        retained = self.enforce_retention()
        retained["policy"] = {
            "maximum_bytes": self.policy.maximum_bytes,
            "high_watermark": self.policy.high_watermark,
            "low_watermark": self.policy.low_watermark,
            "maximum_age_seconds": self.policy.maximum_age_seconds,
            "maximum_events": self.policy.maximum_events,
            "maximum_snapshot_bytes": self.policy.maximum_snapshot_bytes,
            "maximum_event_bytes": self.policy.maximum_event_bytes,
            "maximum_snapshots_per_event": self.policy.maximum_snapshots_per_event,
            "minimum_free_bytes": self.policy.minimum_free_bytes,
        }
        retained["database_bytes"] = self.database.stat().st_size if self.database.exists() else 0
        return retained


def re_full_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)
