"""Private SQLite delivery-state store for daily report emails.

Report content itself (vehicle entry/exit sessions, attendance sessions) is
sourced live from apexfabric-control at generation time (see
tvt_edge/apex_client.py and apexfabric/control_plane/reporting.py) --
apexfabric already persists and aggregates it from gate-crossing events. This
store only tracks the once-daily delivery state per (report_date,
report_kind) so a retry never double-sends.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS daily_reports (
    report_date TEXT NOT NULL,
    report_kind TEXT NOT NULL CHECK(report_kind IN ('vehicle_traffic', 'attendance')),
    window_start REAL NOT NULL,
    window_end REAL NOT NULL,
    generated_at REAL NOT NULL,
    row_count INTEGER NOT NULL,
    summary TEXT NOT NULL,
    csv_text TEXT NOT NULL,
    message_id TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK(state IN ('pending', 'sending', 'sent', 'failed')),
    attempted_at REAL,
    sent_at REAL,
    failure_code TEXT,
    PRIMARY KEY(report_date, report_kind)
);
"""


@dataclass(frozen=True)
class StoredReport:
    report_date: str
    report_kind: str
    window_start: float
    window_end: float
    row_count: int
    summary: dict[str, Any]
    csv_text: str
    message_id: str
    state: str


class ReportingStore:
    def __init__(self, database_path: Path):
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        os.chmod(self.database_path.parent, 0o750)
        with self._connect() as connection:
            connection.executescript(SCHEMA)
        os.chmod(self.database_path, 0o640)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    @staticmethod
    def _row_to_report(row: sqlite3.Row) -> StoredReport:
        data = dict(row)
        data["summary"] = json.loads(data["summary"])
        return StoredReport(**{key: data[key] for key in StoredReport.__dataclass_fields__})

    def report(self, report_date: date, report_kind: str) -> StoredReport | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT report_date, report_kind, window_start, window_end, row_count,
                          summary, csv_text, message_id, state
                   FROM daily_reports WHERE report_date = ? AND report_kind = ?""",
                (report_date.isoformat(), report_kind),
            ).fetchone()
        return self._row_to_report(row) if row else None

    def create_report(
        self,
        *,
        report_date: date,
        report_kind: str,
        window_start: float,
        window_end: float,
        row_count: int,
        summary: dict[str, Any],
        csv_text: str,
        message_id: str,
        generated_at: float,
    ) -> StoredReport:
        with self._connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO daily_reports(
                       report_date, report_kind, window_start, window_end, generated_at,
                       row_count, summary, csv_text, message_id, state
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
                (
                    report_date.isoformat(), report_kind, window_start, window_end, generated_at,
                    row_count, json.dumps(summary, sort_keys=True), csv_text, message_id,
                ),
            )
        result = self.report(report_date, report_kind)
        if result is None:  # pragma: no cover - protected by the insert/read transaction boundary
            raise RuntimeError("daily report could not be persisted")
        return result

    def mark_sent(self, report_date: date, report_kind: str, sent_at: float) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE daily_reports
                   SET state = 'sent', attempted_at = ?, sent_at = ?, failure_code = NULL
                   WHERE report_date = ? AND report_kind = ? AND state = 'sending'""",
                (sent_at, sent_at, report_date.isoformat(), report_kind),
            )

    def claim_delivery(self, report_date: date, report_kind: str, attempted_at: float) -> bool:
        """Atomically consume the date's single delivery attempt for this report kind."""

        with self._connect() as connection:
            changed = connection.execute(
                """UPDATE daily_reports
                   SET state = 'sending', attempted_at = ?
                   WHERE report_date = ? AND report_kind = ? AND state = 'pending'""",
                (attempted_at, report_date.isoformat(), report_kind),
            ).rowcount
        return bool(changed)

    def mark_failed(
        self, report_date: date, report_kind: str, failure_code: str, attempted_at: float
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE daily_reports
                   SET state = 'failed', attempted_at = ?, failure_code = ?
                   WHERE report_date = ? AND report_kind = ? AND state = 'sending'""",
                (attempted_at, failure_code[:64], report_date.isoformat(), report_kind),
            )

    def prune(self, *, retention_days: int, now: datetime | None = None) -> None:
        current = now or datetime.now(timezone.utc)
        report_cutoff = (current.date() - timedelta(days=retention_days)).isoformat()
        with self._connect() as connection:
            connection.execute("DELETE FROM daily_reports WHERE report_date < ?", (report_cutoff,))
