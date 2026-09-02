"""Identity normalization and camera re-use helpers for discovery results."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from tvt_edge.db.models import Camera, CameraEndpoint, CameraIdentifier


def _normalize_mac(value: str) -> str:
    cleaned = re.sub(r"[^0-9a-fA-F]", "", value).lower()
    if len(cleaned) not in {12, 14} and len(cleaned) != 12:
        # Accept forms where separators were already removed.
        if len(cleaned) == 12:
            pass
        else:
            raise ValueError("invalid MAC address format")
    if len(cleaned) == 12:
        return ":".join(cleaned[i : i + 2] for i in range(0, 12, 2))
    if len(cleaned) == 14:
        # Handle accidental separators being preserved in rare captures.
        return ":".join(cleaned[:12][i : i + 2] for i in range(0, 12, 2))
    raise ValueError("invalid MAC address format")


def _normalize_onvif_uuid(value: str) -> str:
    value = value.strip().lower()
    if value.startswith("uuid:"):
        value = value[5:]
    value = value.strip("{}")
    return value


def normalized_camera_key() -> str:
    """Generate a DNS-safe camera identifier."""

    return f"camera-{uuid.uuid4().hex[:16]}"


@dataclass(frozen=True)
class CameraEvidence:
    host: str
    method: str
    rtsp_port: int = 554
    rtsp_path: str | None = None
    onvif_endpoint_uuid: str | None = None
    onvif_device_id: str | None = None
    mac: str | None = None
    manufacturer: str | None = None
    model: str | None = None
    source: str = "discovery"
    metadata: dict[str, Any] | None = None


def deduce_camera_identity(evidence: CameraEvidence) -> list[tuple[str, str]]:
    """Return ordered identity candidates from strongest to weakest evidence."""

    values: list[tuple[str, str]] = []
    if evidence.onvif_endpoint_uuid:
        values.append(("onvif_endpoint_uuid", _normalize_onvif_uuid(evidence.onvif_endpoint_uuid)))
    if evidence.onvif_device_id:
        values.append(("onvif_device_id", evidence.onvif_device_id.strip().lower()))
    if evidence.mac:
        values.append(("mac", _normalize_mac(evidence.mac)))
    return values


def _camera_by_identifier(
    session: Session, site_id: uuid.UUID, kind: str, value: str
) -> Camera | None:
    identifier = session.scalar(
        select(CameraIdentifier)
        .join(Camera, Camera.id == CameraIdentifier.camera_id)
        .where(
            Camera.site_id == site_id,
            CameraIdentifier.kind == kind,
            CameraIdentifier.normalized_value == value,
            CameraIdentifier.active.is_(True),
        )
    )
    return session.get(Camera, identifier.camera_id) if identifier is not None else None


def _camera_by_host_fingerprint(
    session: Session, site_id: uuid.UUID, host: str, manufacturer: str | None, model: str | None
) -> Camera | None:
    query = (
        select(Camera)
        .join(CameraEndpoint)
        .where(Camera.site_id == site_id, CameraEndpoint.host == host)
    )
    if manufacturer is not None:
        query = query.where(Camera.manufacturer == manufacturer)
    if model is not None:
        query = query.where(Camera.model == model)
    cameras = list(session.scalars(query).all())
    if len(cameras) != 1:
        return None
    return cameras[0]


def deduce_camera_identity_for_session(
    session: Session, site_id: uuid.UUID, evidence: CameraEvidence
) -> Camera | None:
    """Resolve an existing camera by ordered evidence priorities."""

    for kind, value in deduce_camera_identity(evidence):
        camera = _camera_by_identifier(session, site_id, kind, value)
        if camera is not None:
            return camera
    return _camera_by_host_fingerprint(
        session,
        site_id,
        evidence.host,
        evidence.manufacturer,
        evidence.model,
    )


def ensure_camera_for_evidence(
    session: Session,
    site_id: uuid.UUID,
    evidence: CameraEvidence,
    *,
    friendly_name: str,
) -> tuple[Camera, bool]:
    """Return an existing camera for this evidence, or create a new one."""

    camera = deduce_camera_identity_for_session(session, site_id, evidence)
    created = False
    if camera is None:
        camera = Camera(
            site_id=site_id,
            camera_key=normalized_camera_key(),
            friendly_name=friendly_name,
            manufacturer=evidence.manufacturer,
            model=evidence.model,
        )
        session.add(camera)
        session.flush()
        created = True

    if evidence.onvif_endpoint_uuid:
        _upsert_identifier(
            session,
            camera.id,
            "onvif_endpoint_uuid",
            _normalize_onvif_uuid(evidence.onvif_endpoint_uuid),
            evidence.source,
        )
    if evidence.onvif_device_id:
        _upsert_identifier(
            session,
            camera.id,
            "onvif_device_id",
            evidence.onvif_device_id.strip().lower(),
            evidence.source,
        )
    if evidence.mac:
        _upsert_identifier(
            session,
            camera.id,
            "mac",
            _normalize_mac(evidence.mac),
            evidence.source,
        )
    return camera, created


def _upsert_identifier(
    session: Session,
    camera_id: uuid.UUID,
    kind: str,
    normalized_value: str,
    source: str,
) -> None:
    existing = session.scalar(
        select(CameraIdentifier).where(
            CameraIdentifier.camera_id == camera_id,
            CameraIdentifier.kind == kind,
            CameraIdentifier.normalized_value == normalized_value,
        )
    )
    if existing is not None:
        if not existing.active:
            existing.active = True
            existing.display_value = normalized_value
            existing.source = source
        return
    session.add(
        CameraIdentifier(
            camera_id=camera_id,
            kind=kind,
            normalized_value=normalized_value,
            display_value=normalized_value,
            source=source,
            confidence="observed",
        )
    )


__all__ = [
    "CameraEvidence",
    "deduce_camera_identity",
    "deduce_camera_identity_for_session",
    "ensure_camera_for_evidence",
    "normalized_camera_key",
]

