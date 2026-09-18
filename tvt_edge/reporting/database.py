"""Private SQLite business store for compact ANPR daily aggregates."""

from __future__ import annotations

import os
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS consumed_events (
    event_id TEXT PRIMARY KEY,
    received_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS consumed_events_received_at ON consumed_events(received_at);
CREATE TABLE IF NOT EXISTS daily_plate_observations (
    report_date TEXT NOT NULL,
    plate_key TEXT NOT NULL,
    vehicle_ref TEXT NOT NULL,
    plate_text TEXT NOT NULL,
    first_seen_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    first_camera_id TEXT NOT NULL,
    last_camera_id TEXT NOT NULL,
    read_count INTEGER NOT NULL CHECK(read_count > 0),
    PRIMARY KEY(report_date, plate_key)
);
CREATE INDEX IF NOT EXISTS observations_report_date
    ON daily_plate_observations(report_date, first_seen_at);
CREATE TABLE IF NOT EXISTS daily_reports (
    report_date TEXT PRIMARY KEY,
    window_start REAL NOT NULL,
    window_end REAL NOT NULL,
    generated_at REAL NOT NULL,
    row_count INTEGER NOT NULL,
    total_duration_seconds REAL NOT NULL,
    csv_text TEXT NOT NULL,
    message_id TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK(state IN ('pending', 'sending', 'sent', 'failed')),
    attempted_at REAL,
    sent_at REAL,
    failure_code TEXT
);
"""


@dataclass(frozen=True)
class Observation:
    report_date: str
    plate_key: str
    vehicle_ref: str
    plate_text: str
    first_seen_at: float
    last_seen_at: float
    first_camera_id: str
    last_camera_id: str
    read_count: int

    @property
    def duration_seconds(self) -> float:
        return max(0.0, self.last_seen_at - self.first_seen_at)


@dataclass(frozen=True)
class StoredReport:
    report_date: str
    window_start: float
    window_end: float
    row_count: int
    total_duration_seconds: float
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
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def record_observation(
        self,
        *,
        event_id: str,
        received_at: float,
        report_date: date,
        plate_key: str,
        plate_text: str,
        occurred_at: float,
        camera_id: str,
    ) -> bool:
        with self._connect() as connection:
            inserted = connection.execute(
                "INSERT OR IGNORE INTO consumed_events(event_id, received_at) VALUES (?, ?)",
                (event_id, received_at),
            ).rowcount
            if not inserted:
                return False
            row = connection.execute(
                """SELECT first_seen_at, last_seen_at FROM daily_plate_observations
                   WHERE report_date = ? AND plate_key = ?""",
                (report_date.isoformat(), plate_key),
            ).fetchone()
            if row is None:
                connection.execute(
                    """INSERT INTO daily_plate_observations(
                           report_date, plate_key, vehicle_ref, plate_text, first_seen_at, last_seen_at,
                           first_camera_id, last_camera_id, read_count
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                    (
                        report_date.isoformat(), plate_key, secrets.token_hex(8), plate_text, occurred_at,
                        occurred_at, camera_id, camera_id,
                    ),
                )
            else:
                first_camera = camera_id if occurred_at < row["first_seen_at"] else None
                last_camera = camera_id if occurred_at > row["last_seen_at"] else None
                connection.execute(
                    """UPDATE daily_plate_observations
                       SET first_seen_at = MIN(first_seen_at, ?),
                           last_seen_at = MAX(last_seen_at, ?),
                           first_camera_id = COALESCE(?, first_camera_id),
                           last_camera_id = COALESCE(?, last_camera_id),
                           read_count = read_count + 1
                       WHERE report_date = ? AND plate_key = ?""",
                    (
                        occurred_at, occurred_at, first_camera, last_camera,
                        report_date.isoformat(), plate_key,
                    ),
                )
        return True

    def observations(self, report_date: date) -> list[Observation]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT report_date, plate_key, vehicle_ref, plate_text, first_seen_at, last_seen_at,
                          first_camera_id, last_camera_id, read_count
                   FROM daily_plate_observations
                   WHERE report_date = ?
                   ORDER BY first_seen_at, plate_text""",
                (report_date.isoformat(),),
            ).fetchall()
        return [Observation(**dict(row)) for row in rows]

    def report(self, report_date: date) -> StoredReport | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT report_date, window_start, window_end, row_count,
                          total_duration_seconds, csv_text, message_id, state
                   FROM daily_reports WHERE report_date = ?""",
                (report_date.isoformat(),),
            ).fetchone()
        return StoredReport(**dict(row)) if row else None

    def create_report(
        self,
        *,
        report_date: date,
        window_start: float,
        window_end: float,
        row_count: int,
        total_duration_seconds: float,
        csv_text: str,
        message_id: str,
        generated_at: float,
    ) -> StoredReport:
        with self._connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO daily_reports(
                       report_date, window_start, window_end, generated_at, row_count,
                       total_duration_seconds, csv_text, message_id, state
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
                (
                    report_date.isoformat(), window_start, window_end, generated_at,
                    row_count, total_duration_seconds, csv_text, message_id,
                ),
            )
        result = self.report(report_date)
        if result is None:  # pragma: no cover - protected by the insert/read transaction boundary
            raise RuntimeError("daily report could not be persisted")
        return result

    def mark_sent(self, report_date: date, sent_at: float) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE daily_reports
                   SET state = 'sent', attempted_at = ?, sent_at = ?, failure_code = NULL
                   WHERE report_date = ? AND state = 'sending'""",
                (sent_at, sent_at, report_date.isoformat()),
            )

    def claim_delivery(self, report_date: date, attempted_at: float) -> bool:
        """Atomically consume the date's single delivery attempt."""

        with self._connect() as connection:
            changed = connection.execute(
                """UPDATE daily_reports
                   SET state = 'sending', attempted_at = ?
                   WHERE report_date = ? AND state = 'pending'""",
                (attempted_at, report_date.isoformat()),
            ).rowcount
        return bool(changed)

    def mark_failed(self, report_date: date, failure_code: str, attempted_at: float) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE daily_reports
                   SET state = 'failed', attempted_at = ?, failure_code = ?
                   WHERE report_date = ? AND state = 'sending'""",
                (attempted_at, failure_code[:64], report_date.isoformat()),
            )

    def prune(self, *, retention_days: int, now: datetime | None = None) -> None:
        current = now or datetime.now(timezone.utc)
        event_cutoff = (current - timedelta(days=7)).timestamp()
        report_cutoff = (current.date() - timedelta(days=retention_days)).isoformat()
        with self._connect() as connection:
            connection.execute("DELETE FROM consumed_events WHERE received_at < ?", (event_cutoff,))
            connection.execute("DELETE FROM daily_reports WHERE report_date < ?", (report_cutoff,))
            connection.execute(
                "DELETE FROM daily_plate_observations WHERE report_date < ?", (report_cutoff,)
            )
