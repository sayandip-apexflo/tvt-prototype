"""Persistent Solution Pack catalog backed by immutable OCI image digests."""

from __future__ import annotations

import json
import hashlib
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import yaml

DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
MANIFEST_ACCEPT = ", ".join((
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
))


class CatalogError(RuntimeError):
    pass


def resolve_registry_digest(registry: str, repository: str, reference: str, timeout: int = 10) -> str:
    """Resolve a registry tag to the digest returned by the V2 manifest API."""
    registry_url = registry.rstrip("/")
    if "://" not in registry_url:
        registry_url = f"http://{registry_url}"
    request = Request(
        f"{registry_url}/v2/{repository}/manifests/{reference}",
        headers={"Accept": MANIFEST_ACCEPT},
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            manifest = response.read(4 * 1024 * 1024 + 1)
            digest = response.headers.get("Docker-Content-Digest", "")
    except (HTTPError, URLError, TimeoutError) as error:
        raise CatalogError(f"cannot resolve {repository}:{reference}: {error}") from error
    if not DIGEST_RE.fullmatch(digest):
        raise CatalogError(f"registry returned an invalid or missing digest for {repository}:{reference}")
    if len(manifest) > 4 * 1024 * 1024:
        raise CatalogError(f"registry manifest is too large for {repository}:{reference}")
    computed = "sha256:" + hashlib.sha256(manifest).hexdigest()
    if digest != computed:
        raise CatalogError(f"registry digest mismatch for {repository}:{reference}")
    return digest


class SolutionCatalog:
    def __init__(self, database: Path):
        self.database = database
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS solutions (
                    catalog_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    version TEXT NOT NULL,
                    registry TEXT NOT NULL,
                    repository TEXT NOT NULL,
                    tag TEXT NOT NULL,
                    digest TEXT,
                    status TEXT NOT NULL,
                    contract_json TEXT NOT NULL,
                    desired_state_schema_json TEXT,
                    desired_state_example_json TEXT,
                    last_error TEXT,
                    updated_at TEXT NOT NULL
                )
            """)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        return connection

    def seed_delivery(self, directory: Path, registry: str, repository: str) -> None:
        contract = yaml.safe_load((directory / "image-contract.yaml").read_text(encoding="utf-8"))
        schema_path = directory / "desired-state.schema.json"
        example_path = directory / "desired-state.example.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8")) if schema_path.exists() else None
        example = json.loads(example_path.read_text(encoding="utf-8")) if example_path.exists() else None
        name = contract["name"]
        version = str(contract["version"])
        tag = f"intel-285h-{version}" if name == "traffic-edge-runtime" else version
        catalog_id = f"{name}:{version}"
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute("""
                INSERT INTO solutions (
                    catalog_id, name, version, registry, repository, tag, digest, status,
                    contract_json, desired_state_schema_json, desired_state_example_json,
                    last_error, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, 'unresolved', ?, ?, ?, NULL, ?)
                ON CONFLICT(catalog_id) DO UPDATE SET
                    name=excluded.name, version=excluded.version, registry=excluded.registry,
                    repository=excluded.repository, tag=excluded.tag,
                    contract_json=excluded.contract_json,
                    desired_state_schema_json=excluded.desired_state_schema_json,
                    desired_state_example_json=excluded.desired_state_example_json,
                    updated_at=excluded.updated_at
            """, (
                catalog_id, name, version, registry, repository, tag,
                json.dumps(contract, sort_keys=True),
                json.dumps(schema, sort_keys=True) if schema is not None else None,
                json.dumps(example, sort_keys=True) if example is not None else None,
                now,
            ))

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["contract"] = json.loads(result.pop("contract_json"))
        schema = result.pop("desired_state_schema_json")
        example = result.pop("desired_state_example_json")
        result["desired_state_schema"] = json.loads(schema) if schema else None
        result["desired_state_example"] = json.loads(example) if example else None
        result["image"] = {
            "repository": f"{result['registry']}/{result['repository']}",
            "tag": result["tag"],
            "digest": result["digest"],
        }
        return result

    def list(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM solutions ORDER BY name, version").fetchall()
        return [self._row(row) for row in rows]

    def get(self, catalog_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM solutions WHERE catalog_id = ?", (catalog_id,)).fetchone()
        return self._row(row) if row else None

    def refresh(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT catalog_id, registry, repository, tag FROM solutions").fetchall()
            for row in rows:
                try:
                    digest = resolve_registry_digest(row["registry"], row["repository"], row["tag"])
                    status, error = "available", None
                except CatalogError as caught:
                    digest, status, error = None, "unavailable", str(caught)
                connection.execute(
                    "UPDATE solutions SET digest=?, status=?, last_error=?, updated_at=? WHERE catalog_id=?",
                    (digest, status, error, datetime.now(timezone.utc).isoformat(), row["catalog_id"]),
                )
        return self.list()
