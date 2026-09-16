"""Site-local, non-secret device inventory backed by SQLite."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class DeviceRegistry:
    def __init__(self, database: Path, site_id: str):
        self.database = database
        self.site_id = site_id
        database.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS devices (
                    site_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('box', 'camera')),
                    display_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_seen_at TEXT,
                    PRIMARY KEY (site_id, kind, device_id)
                )
            """)
            connection.execute("CREATE INDEX IF NOT EXISTS devices_site_kind ON devices(site_id, kind)")
            connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS devices_site_kind_id ON devices(site_id, kind, device_id)")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["metadata"] = json.loads(result.pop("metadata_json"))
        return result

    def list(self, kind: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM devices WHERE site_id=?"
        values: list[Any] = [self.site_id]
        if kind is not None:
            query += " AND kind=?"
            values.append(kind)
        query += " ORDER BY kind, display_name"
        with self._connect() as connection:
            return [self._row(row) for row in connection.execute(query, values).fetchall()]

    def upsert(self, device_id: str, kind: str, display_name: str, status: str,
               metadata: dict[str, Any], seen: bool = False) -> dict[str, Any]:
        if kind not in {"box", "camera"}:
            raise ValueError("device kind is invalid")
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute("""
                INSERT INTO devices (
                    site_id, device_id, kind, display_name, status, metadata_json,
                    created_at, updated_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(site_id, kind, device_id) DO UPDATE SET
                    kind=excluded.kind, display_name=excluded.display_name,
                    status=excluded.status, metadata_json=excluded.metadata_json,
                    updated_at=excluded.updated_at,
                    last_seen_at=COALESCE(excluded.last_seen_at, devices.last_seen_at)
            """, (self.site_id, device_id, kind, display_name, status,
                  json.dumps(metadata, sort_keys=True, separators=(",", ":")),
                  now, now, now if seen else None))
        return next(item for item in self.list(kind) if item["device_id"] == device_id)

    def delete(self, device_id: str, kind: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM devices WHERE site_id=? AND device_id=? AND kind=?",
                (self.site_id, device_id, kind),
            )
        return bool(cursor.rowcount)

    def mark_unseen_boxes_offline(self, seen_ids: set[str]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT device_id FROM devices WHERE site_id=? AND kind='box'", (self.site_id,),
            ).fetchall()
            for row in rows:
                if row["device_id"] not in seen_ids:
                    connection.execute(
                        "UPDATE devices SET status='offline', updated_at=? WHERE site_id=? AND device_id=?",
                        (now, self.site_id, row["device_id"]),
                    )
