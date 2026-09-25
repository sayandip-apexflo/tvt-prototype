"""Face embedding identity resolution and operator-driven enrollment.

Runs inside TelemetryStore.ingest()'s single locked connection, the same way
alerts.evaluate() does (see telemetry.py) -- there is never a second writer to
race against inside this process, so no extra transaction is needed here.

TVT divergence from upstream k3s-prototype: no auto-enrollment. A
face_detection_event only ever matches an existing *named* person (read-only:
the gallery is never grown by recognition), and an enrollment_capture_event
is only staged in enrollment_captures. A person record is created solely by
PersonStore.enroll(), when the operator names a staged capture; unnamed
staged captures are discarded or expire.
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
CREATE TABLE IF NOT EXISTS enrollment_captures (
  capture_id TEXT PRIMARY KEY,
  deployment_id TEXT NOT NULL,
  camera_id TEXT NOT NULL,
  captured_at REAL NOT NULL,
  face_embedding BLOB NOT NULL,
  matched_person_id TEXT REFERENCES persons(person_id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS enrollment_captures_captured_at ON enrollment_captures(captured_at);
'''

NORM_TOLERANCE = 1e-2
DISPLAY_NAME_MAX_LENGTH = 160
# Staged enrollment faces nobody named are deleted after this long.
ENROLLMENT_CAPTURE_MAX_AGE_SECONDS = 1800
MAX_CAPTURES_PER_ENROLLMENT = 16
# Nearest-neighbour candidates inspected when looking for a *named* match.
NAMED_MATCH_CANDIDATES = 16

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


def _best_named_match(connection, packed_face: bytes) -> tuple[str, float] | None:
    """Closest face vector that belongs to a named person, as
    (person_id, cosine similarity)."""
    candidates = connection.execute(
        "SELECT rowid, distance FROM person_face_embeddings WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
        [packed_face, NAMED_MATCH_CANDIDATES],
    ).fetchall()
    for rowid, distance in candidates:
        row = connection.execute(
            "SELECT persons.person_id FROM person_face_embedding_meta "
            "JOIN persons ON persons.person_id = person_face_embedding_meta.person_id "
            "WHERE person_face_embedding_meta.rowid = ? AND persons.status = 'named'",
            (rowid,),
        ).fetchone()
        if row is not None:
            return row[0], 1.0 - distance
    return None


def _insert_vector(connection, table: str, meta_table: str, person_id: str, packed: bytes, event_id: str, captured_at: float) -> None:
    cursor = connection.execute(f"INSERT INTO {table}(embedding) VALUES (?)", [packed])
    connection.execute(
        f"INSERT INTO {meta_table}(rowid, person_id, source_event_id, captured_at) VALUES (?, ?, ?, ?)",
        (cursor.lastrowid, person_id, event_id, captured_at),
    )


def purge_expired_captures(connection, now: float) -> int:
    return connection.execute(
        "DELETE FROM enrollment_captures WHERE captured_at < ?",
        (now - ENROLLMENT_CAPTURE_MAX_AGE_SECONDS,),
    ).rowcount


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
    event isn't a face event.

    face_detection_event: returns the matched named person's ID, or None when
    no named person is at least `match_threshold` similar. Never creates a
    person and never stores the sighting's vector.

    enrollment_capture_event: stages the face in enrollment_captures and
    returns None -- see PersonStore.enroll()."""
    if policy is None:
        return None
    event_type = payload.get("event_type")
    if event_type not in ("face_detection_event", "enrollment_capture_event"):
        return None
    camera_id = payload.get("camera_id") or ""
    inner = payload.get("payload") or {}
    embeddings = inner.get("embeddings") or {}
    face = embeddings.get("face")

    if not _valid_vector(face, policy.face_dim):
        log.warning("Rejecting %s embedding: invalid or non-unit-norm face vector (camera=%s event=%s)", event_type, camera_id, event_id)
        return None
    packed = _pack(face)
    match = _best_named_match(connection, packed)
    matched_person_id = match[0] if match is not None and match[1] >= policy.match_threshold else None

    if event_type == "enrollment_capture_event":
        purge_expired_captures(connection, received_at)
        connection.execute(
            "INSERT OR IGNORE INTO enrollment_captures(capture_id, deployment_id, camera_id, captured_at, "
            "face_embedding, matched_person_id) VALUES (?, ?, ?, ?, ?, ?)",
            (event_id, deployment_id, camera_id, received_at, packed, matched_person_id),
        )
        return None

    if matched_person_id is not None:
        connection.execute("UPDATE persons SET last_seen = ? WHERE person_id = ?", (received_at, matched_person_id))
    return matched_person_id


class DuplicatePersonError(ValueError):
    """A staged enrollment face already matches an existing named person."""

    def __init__(self, person_id: str, display_name: str | None):
        super().__init__(f"This face is already enrolled as {display_name or 'an existing person'}")
        self.person_id = person_id
        self.display_name = display_name


def _capture_ids(capture_ids: Any) -> list[str]:
    if (
        not isinstance(capture_ids, list)
        or not 1 <= len(capture_ids) <= MAX_CAPTURES_PER_ENROLLMENT
        or not all(isinstance(item, str) and item for item in capture_ids)
    ):
        raise ValueError(f"capture_ids must list 1-{MAX_CAPTURES_PER_ENROLLMENT} enrollment capture IDs")
    return list(dict.fromkeys(capture_ids))


def purge_unnamed_persons(connection, dry_run: bool = False) -> dict[str, int]:
    """Delete every person that was never named, with its face/body vectors
    and attendance sessions. One-shot cleanup of pre-divergence
    auto-enrolled records; named persons are untouched."""
    person_ids = [row[0] for row in connection.execute("SELECT person_id FROM persons WHERE status != 'named'")]
    counts = {"persons": len(person_ids), "face_vectors": 0, "body_vectors": 0, "attendance_sessions": 0}
    for person_id in person_ids:
        for kind in ("face", "body"):
            rowids = [row[0] for row in connection.execute(
                f"SELECT rowid FROM person_{kind}_embedding_meta WHERE person_id = ?", (person_id,)
            )]
            counts[f"{kind}_vectors"] += len(rowids)
            if not dry_run:
                connection.executemany(f"DELETE FROM person_{kind}_embeddings WHERE rowid = ?", [(rowid,) for rowid in rowids])
                connection.execute(f"DELETE FROM person_{kind}_embedding_meta WHERE person_id = ?", (person_id,))
        counts["attendance_sessions"] += connection.execute(
            "SELECT COUNT(*) FROM attendance_sessions WHERE person_id = ?", (person_id,)
        ).fetchone()[0]
        if not dry_run:
            connection.execute("DELETE FROM attendance_sessions WHERE person_id = ?", (person_id,))
            connection.execute("DELETE FROM persons WHERE person_id = ?", (person_id,))
    return counts


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
        """Correct an enrolled (named) person's display name."""
        display_name = self._display_name(display_name)
        with self.telemetry.lock, self.telemetry._connect() as connection:
            changed = connection.execute(
                "UPDATE persons SET display_name = ? WHERE person_id = ? AND status = 'named'",
                (display_name, person_id),
            ).rowcount
            if not changed:
                raise ValueError("unknown person_id")

    @staticmethod
    def _display_name(display_name: Any) -> str:
        if not isinstance(display_name, str) or not 1 <= len(display_name.strip()) <= DISPLAY_NAME_MAX_LENGTH:
            raise ValueError(f"display_name is required (maximum {DISPLAY_NAME_MAX_LENGTH} characters)")
        return display_name.strip()

    def enrollment_captures(self, capture_ids: Any) -> list[dict[str, Any]]:
        """Staged captures by ID -- never the embedding itself."""
        ids = _capture_ids(capture_ids)
        placeholders = ",".join("?" for _ in ids)
        with self.telemetry.lock, self.telemetry._connect() as connection:
            purge_expired_captures(connection, time.time())
            rows = connection.execute(
                "SELECT c.capture_id, c.deployment_id, c.camera_id, c.captured_at, c.matched_person_id, "
                "p.display_name AS matched_display_name FROM enrollment_captures c "
                f"LEFT JOIN persons p ON p.person_id = c.matched_person_id WHERE c.capture_id IN ({placeholders}) "
                "ORDER BY c.captured_at",
                ids,
            ).fetchall()
            return [dict(row) for row in rows]

    def enroll(self, capture_ids: Any, display_name: Any) -> str:
        """Create one named person from staged captures and consume them.
        Raises DuplicatePersonError if any capture matches an existing named
        person at the current threshold and gallery."""
        policy = self.telemetry.identity_policy
        if policy is None:
            raise ValueError("identity resolution is not configured")
        ids = _capture_ids(capture_ids)
        display_name = self._display_name(display_name)
        placeholders = ",".join("?" for _ in ids)
        now = time.time()
        with self.telemetry.lock, self.telemetry._connect() as connection:
            purge_expired_captures(connection, now)
            rows = connection.execute(
                "SELECT capture_id, camera_id, captured_at, face_embedding FROM enrollment_captures "
                f"WHERE capture_id IN ({placeholders}) ORDER BY captured_at",
                ids,
            ).fetchall()
            if len(rows) != len(ids):
                raise ValueError("enrollment capture expired or unknown; start a new enrollment")
            for row in rows:
                match = _best_named_match(connection, row["face_embedding"])
                if match is not None and match[1] >= policy.match_threshold:
                    named = connection.execute(
                        "SELECT display_name FROM persons WHERE person_id = ?", (match[0],)
                    ).fetchone()
                    raise DuplicatePersonError(match[0], named[0] if named else None)
            person_id = uuid.uuid4().hex
            first = rows[0]
            connection.execute(
                "INSERT INTO persons(person_id, display_name, status, first_seen, last_seen, "
                "enrollment_source_camera_id, enrollment_source_event_id) VALUES (?, ?, 'named', ?, ?, ?, ?)",
                (person_id, display_name, first["captured_at"], now, first["camera_id"], first["capture_id"]),
            )
            for row in rows:
                _insert_vector(
                    connection, "person_face_embeddings", "person_face_embedding_meta",
                    person_id, row["face_embedding"], row["capture_id"], row["captured_at"],
                )
            connection.execute(f"DELETE FROM enrollment_captures WHERE capture_id IN ({placeholders})", ids)
            return person_id

    def discard_captures(self, capture_ids: Any) -> int:
        ids = _capture_ids(capture_ids)
        placeholders = ",".join("?" for _ in ids)
        with self.telemetry.lock, self.telemetry._connect() as connection:
            return connection.execute(
                f"DELETE FROM enrollment_captures WHERE capture_id IN ({placeholders})", ids
            ).rowcount


def main(argv: list[str] | None = None) -> int:
    """`python -m apexfabric.control_plane.identity purge-unnamed --state-dir DIR [--dry-run]`"""
    import argparse
    import json
    import sqlite3
    from pathlib import Path

    parser = argparse.ArgumentParser(description="ApexFabric identity maintenance")
    parser.add_argument("command", choices=["purge-unnamed"])
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    arguments = parser.parse_args(argv)
    database = arguments.state_dir / "telemetry" / "telemetry.sqlite3"
    if not database.is_file():
        parser.error(f"{database} does not exist")
    connection = sqlite3.connect(database, timeout=30)
    try:
        load_extension(connection)
        connection.execute("PRAGMA foreign_keys=ON")
        with connection:
            counts = purge_unnamed_persons(connection, dry_run=arguments.dry_run)
    finally:
        connection.close()
    print(json.dumps({"dry_run": arguments.dry_run, "removed" if not arguments.dry_run else "would_remove": counts}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
