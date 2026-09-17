"""Face/body embedding identity resolution and auto-enrollment.

Runs inside TelemetryStore.ingest()'s single locked connection, the same way
alerts.evaluate() does (see telemetry.py) -- there is never a second writer to
race against inside this process, so no extra transaction is needed here.
"""

from __future__ import annotations

import logging
import math
import os
import struct
import time
import uuid
from dataclasses import dataclass
from typing import Any

SCHEMA = '''
CREATE TABLE IF NOT EXISTS persons (
  person_id TEXT PRIMARY KEY,
  display_name TEXT,
  status TEXT NOT NULL CHECK(status IN ('auto_enrolled','named')) DEFAULT 'auto_enrolled',
  first_seen REAL NOT NULL,
  last_seen REAL NOT NULL,
  enrollment_source_camera_id TEXT NOT NULL,
  enrollment_source_event_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS person_face_embedding_meta (
  rowid INTEGER PRIMARY KEY,
  person_id TEXT NOT NULL REFERENCES persons(person_id) ON DELETE CASCADE,
  source_event_id TEXT NOT NULL,
  captured_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS person_body_embedding_meta (
  rowid INTEGER PRIMARY KEY,
  person_id TEXT NOT NULL REFERENCES persons(person_id) ON DELETE CASCADE,
  source_event_id TEXT NOT NULL,
  captured_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS person_face_meta_person ON person_face_embedding_meta(person_id);
CREATE INDEX IF NOT EXISTS person_body_meta_person ON person_body_embedding_meta(person_id);
'''

NORM_TOLERANCE = 1e-2
DISPLAY_NAME_MAX_LENGTH = 160

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class IdentityPolicy:
    face_dim: int
    body_dim: int
    match_threshold: float

    def __post_init__(self) -> None:
        if self.face_dim < 1 or self.body_dim < 1:
            raise ValueError("embedding dimensions must be positive")
        if not 0 < self.match_threshold < 1:
            raise ValueError("match threshold must be strictly between 0 and 1")

    @classmethod
    def from_environment(cls) -> "IdentityPolicy | None":
        """Identity resolution is opt-in: a deployment with no face_recognition/
        face_enrollment cameras configured (e.g. traffic-only) has no reason to
        set an embedding dimension, and must not be forced to. Returns None
        (feature disabled -- no vector tables, resolve_identity() is a no-op)
        when none of the three variables are set. Raises if some but not all
        are set, or if any is set to an invalid value -- partial configuration
        is always a mistake, never a valid "disabled" state."""
        names = ("APEXFABRIC_FACE_EMBEDDING_DIM", "APEXFABRIC_BODY_EMBEDDING_DIM", "APEXFABRIC_FACE_MATCH_THRESHOLD")
        values = {name: os.getenv(name) for name in names}
        if not any(values.values()):
            return None
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise ValueError(f"identity resolution is partially configured; also set: {', '.join(missing)}")
        return cls(
            face_dim=int(values["APEXFABRIC_FACE_EMBEDDING_DIM"]),
            body_dim=int(values["APEXFABRIC_BODY_EMBEDDING_DIM"]),
            match_threshold=float(values["APEXFABRIC_FACE_MATCH_THRESHOLD"]),
        )


def load_extension(connection) -> None:
    try:
        import sqlite_vec
        connection.enable_load_extension(True)
        sqlite_vec.load(connection)
        connection.enable_load_extension(False)
    except Exception as error:
        raise RuntimeError(
            "sqlite-vec extension failed to load; check the installed sqlite-vec "
            "wheel matches this platform"
        ) from error


def ensure_vector_tables(connection, policy: IdentityPolicy) -> None:
    connection.executescript(
        f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS person_face_embeddings
          USING vec0(embedding float[{int(policy.face_dim)}] distance_metric=cosine);
        CREATE VIRTUAL TABLE IF NOT EXISTS person_body_embeddings
          USING vec0(embedding float[{int(policy.body_dim)}] distance_metric=cosine);
        """
    )


def _pack(vector: list[float]) -> bytes:
    return struct.pack(f"{len(vector)}f", *(float(component) for component in vector))


def _is_unit_norm(vector: list[float]) -> bool:
    norm = math.sqrt(sum(component * component for component in vector))
    return abs(norm - 1.0) <= NORM_TOLERANCE


def _valid_vector(vector: Any, expected_dim: int) -> bool:
    return (
        isinstance(vector, list)
        and len(vector) == expected_dim
        and all(isinstance(component, (int, float)) for component in vector)
        and _is_unit_norm(vector)
    )


def _best_match(connection, table: str, vector: list[float]) -> tuple[int, float] | None:
    row = connection.execute(
        f"SELECT rowid, distance FROM {table} WHERE embedding MATCH ? ORDER BY distance LIMIT 1",
        [_pack(vector)],
    ).fetchone()
    if row is None:
        return None
    return row[0], row[1]


def _insert_vector(connection, table: str, meta_table: str, person_id: str, vector: list[float], event_id: str, captured_at: float) -> None:
    cursor = connection.execute(f"INSERT INTO {table}(embedding) VALUES (?)", [_pack(vector)])
    connection.execute(
        f"INSERT INTO {meta_table}(rowid, person_id, source_event_id, captured_at) VALUES (?, ?, ?, ?)",
        (cursor.lastrowid, person_id, event_id, captured_at),
    )


def resolve_identity(
    connection,
    event_id: str,
    deployment_id: str,
    payload: dict[str, Any],
    policy: IdentityPolicy | None,
    received_at: float,
) -> str | None:
    """`payload` is the whole event envelope (same convention as alerts.evaluate);
    the analytics-event schema's own nested payload object is one level deeper,
    at payload["payload"]. Returns None (no-op) if identity resolution is
    disabled (policy is None -- see IdentityPolicy.from_environment) or the
    event isn't a face event."""
    if policy is None:
        return None
    event_type = payload.get("event_type")
    if event_type not in ("face_detection_event", "enrollment_capture_event"):
        return None
    camera_id = payload.get("camera_id") or ""
    inner = payload.get("payload") or {}
    embeddings = inner.get("embeddings") or {}
    face = embeddings.get("face")
    body = embeddings.get("body")

    if not _valid_vector(face, policy.face_dim):
        log.warning("Rejecting %s embedding: invalid or non-unit-norm face vector (camera=%s event=%s)", event_type, camera_id, event_id)
        return None
    if body is not None and not _valid_vector(body, policy.body_dim):
        log.warning("Rejecting %s embedding: invalid or non-unit-norm body vector (camera=%s event=%s)", event_type, camera_id, event_id)
        return None

    match = _best_match(connection, "person_face_embeddings", face)
    similarity = (1.0 - match[1]) if match else None

    if match is not None and similarity >= policy.match_threshold:
        person_row = connection.execute(
            "SELECT person_id FROM person_face_embedding_meta WHERE rowid = ?", (match[0],)
        ).fetchone()
        person_id = person_row[0]
        connection.execute("UPDATE persons SET last_seen = ? WHERE person_id = ?", (received_at, person_id))
    else:
        person_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO persons(person_id, display_name, status, first_seen, last_seen, "
            "enrollment_source_camera_id, enrollment_source_event_id) VALUES (?, NULL, 'auto_enrolled', ?, ?, ?, ?)",
            (person_id, received_at, received_at, camera_id, event_id),
        )

    _insert_vector(connection, "person_face_embeddings", "person_face_embedding_meta", person_id, face, event_id, received_at)
    if body is not None:
        _insert_vector(connection, "person_body_embeddings", "person_body_embedding_meta", person_id, body, event_id, received_at)

    return person_id


class PersonStore:
    def __init__(self, telemetry):
        self.telemetry = telemetry

    def list(self, status: str | None = None) -> list[dict[str, Any]]:
        with self.telemetry._connect() as connection:
            query = "SELECT * FROM persons"
            values: list[Any] = []
            if status:
                query += " WHERE status = ?"
                values.append(status)
            query += " ORDER BY last_seen DESC"
            rows = connection.execute(query, values).fetchall()
            return [dict(row) for row in rows]

    def rename(self, person_id: str, display_name: str) -> None:
        if not isinstance(display_name, str) or not 1 <= len(display_name) <= DISPLAY_NAME_MAX_LENGTH:
            raise ValueError(f"display_name is required (maximum {DISPLAY_NAME_MAX_LENGTH} characters)")
        with self.telemetry.lock, self.telemetry._connect() as connection:
            changed = connection.execute(
                "UPDATE persons SET display_name = ?, status = 'named' WHERE person_id = ?",
                (display_name, person_id),
            ).rowcount
            if not changed:
                raise ValueError("unknown person_id")
