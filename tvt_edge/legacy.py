"""One-shot read-only import of the temporary Slice 2 lifecycle database."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tvt_edge.bundles import bundle_sha256, validate_tvt_bundle
from tvt_edge.db.models import (
    AuditEvent,
    LegacyImport,
    Site,
    SolutionBundleRevision,
    SolutionDeployment,
)
from tvt_edge.security import RTSP_URL, redact


def import_sqlite_lifecycle(
    sessions: sessionmaker[Session], source: Path, *, archive_read_only: bool = False
) -> dict[str, int]:
    raw = source.read_bytes()
    source_hash = hashlib.sha256(raw).hexdigest()
    connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        deployments = connection.execute("SELECT * FROM deployments").fetchall()
        revisions = connection.execute(
            "SELECT * FROM revisions ORDER BY sequence"
        ).fetchall()
        events = connection.execute(
            "SELECT * FROM lifecycle_events ORDER BY sequence"
        ).fetchall()
    finally:
        connection.close()
    counts = {
        "deployments": len(deployments),
        "revisions": len(revisions),
        "lifecycle_events": len(events),
    }
    with sessions.begin() as session:
        previous = session.scalar(
            select(LegacyImport).where(LegacyImport.source_sha256 == source_hash)
        )
        if previous is not None:
            return previous.row_counts
        site = session.scalar(select(Site).limit(1))
        if site is None:
            raise ValueError("configure the site before importing SQLite")
        mapped: dict[str, SolutionDeployment] = {}
        for row in deployments:
            deployment = session.scalar(
                select(SolutionDeployment).where(
                    SolutionDeployment.site_id == site.id,
                    SolutionDeployment.deployment_key == row["deployment_id"],
                )
            )
            if deployment is None:
                deployment = SolutionDeployment(
                    site_id=site.id,
                    deployment_key=row["deployment_id"],
                    solution_id="legacy-import",
                    namespace=row["namespace"],
                    registry=row["registry"],
                    lifecycle_intent=row["desired_state"],
                )
                session.add(deployment)
                session.flush()
            mapped[row["deployment_id"]] = deployment
        for row in revisions:
            bundle = json.loads(row["bundle_json"])
            serialized = json.dumps(bundle, sort_keys=True, separators=(",", ":"))
            if RTSP_URL.search(serialized):
                raise ValueError("legacy bundle contains a credential-bearing RTSP URL")
            validate_tvt_bundle(bundle)
            deployment = mapped[row["deployment_id"]]
            digest = bundle_sha256(bundle)
            exists = session.scalar(
                select(SolutionBundleRevision.id).where(
                    SolutionBundleRevision.deployment_id == deployment.id,
                    SolutionBundleRevision.bundle_sha256 == digest,
                )
            )
            if exists is None:
                session.add(
                    SolutionBundleRevision(
                        deployment_id=deployment.id,
                        bundle_sha256=digest,
                        canonical_bundle=bundle,
                        action=f"legacy:{row['action']}",
                        actor="legacy-import",
                    )
                )
        for row in events:
            details = json.loads(row["detail_json"])
            session.add(
                AuditEvent(
                    actor="legacy-import",
                    request_id=f"legacy:{source_hash[:12]}:{row['sequence']}",
                    action=f"legacy.{row['action']}",
                    target_type="deployment",
                    target_id=row["deployment_id"],
                    result=row["outcome"],
                    details=redact(details),
                )
            )
        session.add(
            LegacyImport(
                source_sha256=source_hash,
                source_name=source.name,
                row_counts=counts,
                result="succeeded",
            )
        )
    if archive_read_only:
        os.chmod(source, 0o400)
    return counts
