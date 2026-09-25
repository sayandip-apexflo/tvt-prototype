"""Transactional management operations for Slice 3."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from jsonschema import Draft202012Validator

from apexfabric.solution_management.catalog import (
    CatalogError,
    resolve_registry_digest,
)
from tvt_edge.apex_client import ApexDuplicatePersonError
from tvt_edge.delivery_metadata import load_delivery_metadata
from tvt_edge.bundles import (
    PACK_NAME_TO_SOLUTION_PACK,
    BundleCamera,
    apply_registry,
    bundle_sha256,
    catalog_traffic_bundle,
    canonical_bundle,
    instantiate_traffic_bundle,
    validate_tvt_bundle,
)
from tvt_edge.db.models import (
    AuditEvent,
    Camera,
    CameraApplicationAssignment,
    CameraCredentialVersion,
    CameraDeploymentAssignment,
    CameraEndpoint,
    CameraGeometryShape,
    CameraIdentifier,
    CameraRole,
    CameraRoleAssignment,
    CameraStreamProfile,
    CredentialKeyVersion,
    DeploymentAssignmentSet,
    EnrollmentCameraDesignation,
    EnrollmentSession,
    EnrollmentWindow,
    DeploymentSyncState,
    DeploymentSyncAttempt,
    KubernetesResourceRef,
    ManagementOperation,
    Site,
    SolutionCatalogEntry,
    SolutionBundleRevision,
    SolutionDeployment,
    utc_now,
)
from tvt_edge.enrollment import (
    ACTIVE_STATUSES as ENROLLMENT_ACTIVE_STATUSES,
    TERMINAL_STATUSES as ENROLLMENT_TERMINAL_STATUSES,
    DEFAULT_ACTIVATION_TIMEOUT_SECONDS,
    DEFAULT_CAPTURE_WINDOW_SECONDS,
    MAX_CAPTURE_WINDOW_SECONDS,
    MIN_CAPTURE_WINDOW_SECONDS,
    CAPTURE_SETTLE_SECONDS,
    MAX_ENROLLMENT_CAPTURES,
    NAMING_TIMEOUT_SECONDS,
    RESULT_CODE_TERMINAL_STATUS,
    UNNAMED_DISCARDED_MESSAGE,
    eligible_capture_candidates,
    has_rejected_capture_attempt,
)
from tvt_edge.geometry import ShapeInput, compile_camera_config, line_shape_key, slugify, validate_shape
from tvt_edge.security import CredentialKeyring, redact, redact_text


DNS_ID = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,61}[a-z0-9])?$")
SAFE_HOST = re.compile(r"^[A-Za-z0-9_.:-]+$")


def _parse_rtsp_url(url: str) -> tuple[str, str, int, str]:
    """Split an operator-supplied RTSP URL into scheme, host, port, path.

    Credentials belong to the separate credentials endpoint, never the URL.
    """

    parsed = urlsplit(url)
    if parsed.scheme not in {"rtsp", "rtsps"}:
        raise ValueError("rtsp_url must use the rtsp or rtsps scheme")
    if parsed.username or parsed.password:
        raise ValueError("rtsp_url must not contain credentials")
    if not parsed.hostname:
        raise ValueError("rtsp_url must include a host")
    if parsed.query or parsed.fragment:
        raise ValueError("rtsp_url must not contain a query or fragment")
    return parsed.scheme, parsed.hostname, parsed.port or 554, parsed.path or "/"


class ManagementService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        keyring: CredentialKeyring,
        catalog_resolver: Callable[[str, str, str], str] = resolve_registry_digest,
        enrollment_minimum_sharpness: float | None = None,
    ) -> None:
        self.sessions = sessions
        self.keyring = keyring
        self.catalog_resolver = catalog_resolver
        # Optional defense-in-depth capture-quality floor, checked only
        # against the vendor's non-sensitive payload.payload.quality block
        # (see tvt_edge/enrollment.py eligible_capture_candidate) -- never
        # the embedding itself, which apexfabric/control_plane/identity.py
        # (frozen) already validates authoritatively.
        self.enrollment_minimum_sharpness = enrollment_minimum_sharpness

    @staticmethod
    def _audit(
        session: Session,
        *,
        actor: str,
        request_id: str,
        action: str,
        target_type: str,
        target_id: str,
        result: str = "succeeded",
        details: dict[str, Any] | None = None,
    ) -> None:
        session.add(
            AuditEvent(
                actor=actor,
                request_id=request_id,
                action=action,
                target_type=target_type,
                target_id=target_id,
                result=result,
                details=redact(details or {}),
            )
        )

    @staticmethod
    def _solution_view(entry: SolutionCatalogEntry) -> dict[str, Any]:
        digest = entry.resolved_digest if entry.status == "available" else None
        return {
            "catalog_id": entry.catalog_id,
            "solution_name": entry.solution_name,
            "version": entry.version,
            "hardware_profile": entry.hardware_profile,
            "architectures": list(entry.architectures),
            "status": entry.status,
            "image": {
                "registry": entry.local_registry,
                "repository": entry.repository,
                "tag": entry.tag,
                "digest": digest,
                "reference": (
                    f"{entry.local_registry}/{entry.repository}@{digest}"
                    if digest
                    else None
                ),
            },
            "contract": copy.deepcopy(entry.contract_json),
            "desired_state_schema": copy.deepcopy(entry.desired_state_schema),
            "desired_state_example": copy.deepcopy(entry.desired_state_example),
            "provenance": copy.deepcopy(entry.provenance),
            "checksums": copy.deepcopy(entry.checksums),
            "last_error": entry.last_error,
            "last_refreshed_at": (
                entry.last_refreshed_at.isoformat()
                if entry.last_refreshed_at is not None
                else None
            ),
            "created_at": entry.created_at.isoformat(),
            "updated_at": entry.updated_at.isoformat(),
        }

    def seed_solution_catalog(
        self,
        delivery_directory: Path,
        local_registry: str,
        *,
        actor: str = "bootstrap",
        request_id: str = "bootstrap:solution-catalog",
    ) -> dict[str, Any]:
        metadata = load_delivery_metadata(delivery_directory)
        if not SAFE_HOST.fullmatch(local_registry) or ":" not in local_registry:
            raise ValueError("local registry must be a host:port value")
        repository = metadata.get("repository")
        tag = metadata.get("tag")
        if not isinstance(repository, str) or not repository or "latest" in repository:
            raise ValueError("catalog repository is invalid")
        if not isinstance(tag, str) or not tag or tag == "latest":
            raise ValueError("catalog tag must be immutable and versioned")

        with self.sessions.begin() as session:
            entry = session.get(SolutionCatalogEntry, metadata["catalog_id"])
            created = entry is None
            if entry is None:
                entry = SolutionCatalogEntry(
                    catalog_id=metadata["catalog_id"],
                    solution_name=metadata["solution_name"],
                    version=metadata["version"],
                    hardware_profile=metadata["hardware_profile"],
                    architectures=metadata["architectures"],
                    local_registry=local_registry,
                    repository=repository,
                    tag=tag,
                    status="unresolved",
                    contract_json=metadata["contract"],
                    desired_state_schema=metadata["desired_state_schema"],
                    desired_state_example=metadata["desired_state_example"],
                    provenance=metadata["provenance"],
                    checksums=metadata["checksums"],
                )
                session.add(entry)
            else:
                identity_changed = any(
                    (
                        entry.local_registry != local_registry,
                        entry.repository != repository,
                        entry.tag != tag,
                        entry.provenance != metadata["provenance"],
                    )
                )
                entry.solution_name = metadata["solution_name"]
                entry.version = metadata["version"]
                entry.hardware_profile = metadata["hardware_profile"]
                entry.architectures = metadata["architectures"]
                entry.local_registry = local_registry
                entry.repository = repository
                entry.tag = tag
                entry.contract_json = metadata["contract"]
                entry.desired_state_schema = metadata["desired_state_schema"]
                entry.desired_state_example = metadata["desired_state_example"]
                entry.provenance = metadata["provenance"]
                entry.checksums = metadata["checksums"]
                if identity_changed:
                    entry.resolved_digest = None
                    entry.status = "unresolved"
                    entry.last_error = None
                    entry.last_refreshed_at = None
            session.flush()
            self._audit(
                session,
                actor=actor,
                request_id=request_id,
                action="solution_catalog.seed",
                target_type="solution",
                target_id=entry.catalog_id,
                details={"created": created, "registry": local_registry},
            )
            return self._solution_view(entry)

    def list_solutions(self) -> list[dict[str, Any]]:
        with self.sessions() as session:
            entries = session.scalars(
                select(SolutionCatalogEntry).order_by(
                    SolutionCatalogEntry.solution_name,
                    SolutionCatalogEntry.version,
                )
            ).all()
            return [self._solution_view(entry) for entry in entries]

    def refresh_solutions(
        self,
        *,
        actor: str = "local-operator",
        request_id: str = "solution-catalog:refresh",
    ) -> list[dict[str, Any]]:
        now = utc_now()
        with self.sessions.begin() as session:
            entries = session.scalars(
                select(SolutionCatalogEntry).order_by(
                    SolutionCatalogEntry.solution_name,
                    SolutionCatalogEntry.version,
                )
            ).all()
            for entry in entries:
                try:
                    digest = self.catalog_resolver(
                        entry.local_registry, entry.repository, entry.tag
                    )
                    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                        raise CatalogError("registry resolver returned an invalid digest")
                except (CatalogError, OSError, TimeoutError) as error:
                    entry.resolved_digest = None
                    entry.status = "unavailable"
                    entry.last_error = redact_text(str(error))
                else:
                    entry.resolved_digest = digest
                    entry.status = "available"
                    entry.last_error = None
                entry.last_refreshed_at = now
                self._audit(
                    session,
                    actor=actor,
                    request_id=request_id,
                    action="solution_catalog.refresh",
                    target_type="solution",
                    target_id=entry.catalog_id,
                    result=("succeeded" if entry.status == "available" else "failed"),
                    details={"status": entry.status, "digest": entry.resolved_digest},
                )
            session.flush()
            return [self._solution_view(entry) for entry in entries]

    def create_site(
        self,
        site_key: str,
        edge_id: str,
        display_name: str,
        timezone_name: str,
        actor: str,
        request_id: str,
    ) -> Site:
        if not DNS_ID.fullmatch(site_key) or not DNS_ID.fullmatch(edge_id):
            raise ValueError("site_key and edge_id must be DNS-safe identifiers")
        with self.sessions.begin() as session:
            if session.scalar(select(Site.id).limit(1)) is not None:
                raise ValueError("TVT V1 supports exactly one site")
            site = Site(
                site_key=site_key,
                edge_id=edge_id,
                display_name=display_name.strip(),
                timezone_name=timezone_name,
            )
            session.add(site)
            session.flush()
            self._audit(
                session,
                actor=actor,
                request_id=request_id,
                action="site.create",
                target_type="site",
                target_id=site_key,
            )
            return site

    def current_site(self, session: Session | None = None) -> Site:
        if session is not None:
            site = session.scalar(select(Site).limit(1))
        else:
            with self.sessions() as owned:
                site = owned.scalar(select(Site).limit(1))
                if site is not None:
                    owned.expunge(site)
        if site is None:
            raise ValueError("site is not configured")
        return site

    def create_camera(
        self,
        *,
        camera_key: str,
        friendly_name: str,
        manufacturer: str | None,
        model: str | None,
        identifiers: list[dict[str, str]],
        rtsp_url: str | None = None,
        actor: str,
        request_id: str,
    ) -> Camera:
        if not DNS_ID.fullmatch(camera_key):
            raise ValueError("camera_key must be a DNS-safe identifier")
        with self.sessions.begin() as session:
            site = self.current_site(session)
            camera = Camera(
                site_id=site.id,
                camera_key=camera_key,
                friendly_name=friendly_name.strip(),
                manufacturer=manufacturer,
                model=model,
            )
            session.add(camera)
            session.flush()
            for value in identifiers:
                session.add(
                    CameraIdentifier(
                        camera_id=camera.id,
                        kind=value["kind"],
                        normalized_value=value["value"].strip().lower(),
                        display_value=value["value"].strip(),
                        source=value.get("source", "operator"),
                        confidence=value.get("confidence", "asserted"),
                    )
                )
            if rtsp_url:
                scheme, host, port, path = _parse_rtsp_url(rtsp_url)
                self._upsert_stream_profile(
                    session,
                    camera,
                    scheme=scheme,
                    host=host,
                    port=port,
                    path=path,
                    profile_token="primary",
                    transport="tcp",
                    codec=None,
                    width=None,
                    height=None,
                    fps=None,
                )
            self._audit(
                session,
                actor=actor,
                request_id=request_id,
                action="camera.create",
                target_type="camera",
                target_id=camera_key,
            )
            return camera

    def assign_camera_role(
        self,
        camera_key: str,
        role_key: str,
        display_name: str,
        direction: str,
        ordinal: int | None,
        actor: str,
        request_id: str,
    ) -> CameraRoleAssignment:
        if not DNS_ID.fullmatch(role_key):
            raise ValueError("role_key must be a DNS-safe identifier")
        if direction not in {"entry", "exit", "bidirectional", "unknown"}:
            raise ValueError("camera direction is invalid")
        with self.sessions.begin() as session:
            camera = self._camera(session, camera_key)
            role = session.scalar(
                select(CameraRole).where(
                    CameraRole.site_id == camera.site_id,
                    CameraRole.role_key == role_key,
                )
            )
            if role is None:
                role = CameraRole(
                    site_id=camera.site_id,
                    role_key=role_key,
                    display_name=display_name,
                )
                session.add(role)
                session.flush()
            assignment = CameraRoleAssignment(
                camera_id=camera.id,
                role_id=role.id,
                direction=direction,
                ordinal=ordinal,
            )
            session.add(assignment)
            self._audit(
                session,
                actor=actor,
                request_id=request_id,
                action="camera.role.assign",
                target_type="camera",
                target_id=camera_key,
                details={"role_key": role_key, "direction": direction},
            )
            return assignment

    @staticmethod
    def _geometry_view(shape: CameraGeometryShape) -> dict[str, Any]:
        return {
            "shape_id": str(shape.id),
            "kind": shape.kind,
            "shape_key": shape.shape_key,
            "name": shape.name,
            "points": shape.points,
            "role_key": shape.role_key,
            "direction": shape.direction,
            "inside_side": shape.inside_side,
            "enabled": shape.enabled,
            "created_at": shape.created_at.isoformat(),
            "updated_at": shape.updated_at.isoformat(),
        }

    def list_camera_geometry(self, camera_key: str) -> list[dict[str, Any]]:
        with self.sessions() as session:
            camera = self._camera(session, camera_key)
            shapes = session.scalars(
                select(CameraGeometryShape)
                .where(
                    CameraGeometryShape.camera_id == camera.id,
                    CameraGeometryShape.deleted_at.is_(None),
                )
                .order_by(CameraGeometryShape.kind, CameraGeometryShape.shape_key)
            ).all()
            return [self._geometry_view(shape) for shape in shapes]

    def camera_geometry_config(self, camera_key: str) -> dict[str, Any]:
        """Compile this camera's enabled zones/lines into the vendor pack's
        `config.zones.anpr[]`/`config.lines[]` shape (see tvt_edge/geometry.py)."""
        with self.sessions() as session:
            camera = self._camera(session, camera_key)
            shapes = session.scalars(
                select(CameraGeometryShape).where(
                    CameraGeometryShape.camera_id == camera.id,
                    CameraGeometryShape.deleted_at.is_(None),
                )
            ).all()
            inputs = [
                ShapeInput(
                    kind=shape.kind,
                    shape_key=shape.shape_key,
                    name=shape.name,
                    points=shape.points,
                    role_key=shape.role_key,
                    direction=shape.direction,
                    inside_side=shape.inside_side,
                    enabled=shape.enabled,
                )
                for shape in shapes
            ]
            return compile_camera_config(inputs)

    @staticmethod
    def _compile_camera_geometry(
        session: Session, camera_id: uuid.UUID
    ) -> dict[str, Any]:
        shapes = session.scalars(
            select(CameraGeometryShape).where(
                CameraGeometryShape.camera_id == camera_id,
                CameraGeometryShape.deleted_at.is_(None),
            )
        ).all()
        return compile_camera_config(
            [
                ShapeInput(
                    kind=shape.kind,
                    shape_key=shape.shape_key,
                    name=shape.name,
                    points=shape.points,
                    role_key=shape.role_key,
                    direction=shape.direction,
                    inside_side=shape.inside_side,
                    enabled=shape.enabled,
                )
                for shape in shapes
            ]
        )

    @staticmethod
    def _camera_for_geometry_update(session: Session, camera_key: str) -> Camera:
        camera = session.scalar(
            select(Camera)
            .where(Camera.camera_key == camera_key, Camera.deleted_at.is_(None))
            .with_for_update()
        )
        if camera is None:
            raise ValueError(f"unknown camera {camera_key!r}")
        return camera

    @staticmethod
    def _geometry_catalog_entry(
        session: Session, bundle_revision: SolutionBundleRevision
    ) -> SolutionCatalogEntry | None:
        catalog_id = bundle_revision.canonical_bundle.get("configuration", {}).get(
            "catalog_id"
        )
        if not catalog_id:
            return None
        entry = session.get(SolutionCatalogEntry, catalog_id)
        if entry is None:
            return None
        solution_pack = PACK_NAME_TO_SOLUTION_PACK.get(
            entry.contract_json.get("name"), entry.contract_json.get("name")
        )
        return entry if solution_pack == "tvt-mills-pilot" else None

    @staticmethod
    def _merge_camera_geometry(
        configuration: dict[str, Any], geometry: dict[str, Any]
    ) -> dict[str, Any]:
        """Replace geometry-owned keys without discarding unrelated config."""
        merged = copy.deepcopy(configuration)
        merged.pop("lines", None)
        if "lines" in geometry:
            merged["lines"] = copy.deepcopy(geometry["lines"])

        current_zones = merged.get("zones")
        zones = copy.deepcopy(current_zones) if isinstance(current_zones, dict) else {}
        zones.pop("anpr", None)
        geometry_zones = geometry.get("zones")
        if isinstance(geometry_zones, dict) and "anpr" in geometry_zones:
            zones["anpr"] = copy.deepcopy(geometry_zones["anpr"])
        if zones:
            merged["zones"] = zones
        else:
            merged.pop("zones", None)
        return merged

    @staticmethod
    def _assignment_desired_state(
        session: Session,
        deployment: SolutionDeployment,
        source: DeploymentAssignmentSet,
        entry: SolutionCatalogEntry,
        desired_revision: int,
        configuration_overrides: dict[uuid.UUID, dict[str, Any]],
    ) -> dict[str, Any]:
        site = session.get(Site, deployment.site_id)
        if site is None:
            raise ValueError("deployment site is missing")
        solution_pack = PACK_NAME_TO_SOLUTION_PACK.get(
            entry.contract_json.get("name"), entry.contract_json.get("name")
        )
        desired_cameras: list[dict[str, Any]] = []
        camera_assignments = session.scalars(
            select(CameraDeploymentAssignment)
            .where(CameraDeploymentAssignment.assignment_set_id == source.id)
            .order_by(CameraDeploymentAssignment.ordinal)
        ).all()
        for camera_assignment in camera_assignments:
            camera = session.get(Camera, camera_assignment.camera_id)
            if camera is None:
                raise ValueError("deployment camera is missing")
            app_assignments = session.scalars(
                select(CameraApplicationAssignment).where(
                    CameraApplicationAssignment.camera_assignment_id
                    == camera_assignment.id
                )
            ).all()
            configurations = {
                json.dumps(item.configuration, sort_keys=True, separators=(",", ":"))
                for item in app_assignments
            }
            if len(configurations) > 1:
                raise ValueError("camera applications have inconsistent geometry")
            configuration = configuration_overrides.get(camera.id)
            if configuration is None:
                configuration = (
                    json.loads(next(iter(configurations))) if configurations else {}
                )
            desired_cameras.append(
                {
                    "camera_id": camera.camera_key,
                    "source": (
                        f"file:/run/secrets/apexfabric/{camera.camera_key}.rtsp"
                    ),
                    "solution_pack": solution_pack,
                    "fps": camera_assignment.requested_fps,
                    "apps": [item.use_case_key for item in app_assignments],
                    "config": copy.deepcopy(configuration),
                }
            )
        return {
            "edge_id": site.edge_id,
            "revision": desired_revision,
            "cameras": desired_cameras,
        }

    def _queue_camera_geometry_deployments(
        self,
        session: Session,
        camera: Camera,
        compiled_geometry: dict[str, Any],
        actor: str,
    ) -> None:
        deployments = session.scalars(
            select(SolutionDeployment)
            .join(
                DeploymentSyncState,
                DeploymentSyncState.deployment_id == SolutionDeployment.id,
            )
            .join(
                CameraDeploymentAssignment,
                CameraDeploymentAssignment.assignment_set_id
                == DeploymentSyncState.desired_assignment_set_id,
            )
            .where(
                CameraDeploymentAssignment.camera_id == camera.id,
                SolutionDeployment.deleted_at.is_(None),
            )
            .order_by(SolutionDeployment.deployment_key)
            .with_for_update()
        ).unique().all()
        for deployment in deployments:
            sync = session.get(DeploymentSyncState, deployment.id)
            source = (
                session.get(DeploymentAssignmentSet, sync.desired_assignment_set_id)
                if sync is not None
                else None
            )
            if source is None:
                continue
            bundle_revision = session.get(
                SolutionBundleRevision, source.bundle_revision_id
            )
            if bundle_revision is None:
                raise ValueError("deployment bundle revision is unavailable")
            entry = self._geometry_catalog_entry(session, bundle_revision)
            if entry is None:
                continue

            target = session.scalar(
                select(CameraDeploymentAssignment).where(
                    CameraDeploymentAssignment.assignment_set_id == source.id,
                    CameraDeploymentAssignment.camera_id == camera.id,
                )
            )
            if target is None:
                continue
            target_app_assignments = session.scalars(
                select(CameraApplicationAssignment).where(
                    CameraApplicationAssignment.camera_assignment_id == target.id
                )
            ).all()
            configurations = {
                json.dumps(item.configuration, sort_keys=True, separators=(",", ":"))
                for item in target_app_assignments
            }
            if len(configurations) > 1:
                raise ValueError("camera applications have inconsistent geometry")
            target_configuration = (
                json.loads(next(iter(configurations))) if configurations else {}
            )

            enrollment_sessions = session.scalars(
                select(EnrollmentSession)
                .where(
                    EnrollmentSession.deployment_id == deployment.id,
                    EnrollmentSession.camera_id == camera.id,
                    EnrollmentSession.status.in_(ENROLLMENT_ACTIVE_STATUSES),
                )
                .with_for_update()
            ).all()
            enrollment_windows = session.scalars(
                select(EnrollmentWindow)
                .where(
                    EnrollmentWindow.deployment_id == deployment.id,
                    EnrollmentWindow.camera_id == camera.id,
                    EnrollmentWindow.status == "active",
                )
                .with_for_update()
            ).all()
            for enrollment_session in enrollment_sessions:
                enrollment_session.prior_config = self._merge_camera_geometry(
                    enrollment_session.prior_config, compiled_geometry
                )
                enrollment_session.row_version += 1
            for enrollment_window in enrollment_windows:
                enrollment_window.prior_config = self._merge_camera_geometry(
                    enrollment_window.prior_config, compiled_geometry
                )
            if (
                any(item.status != "restoring" for item in enrollment_sessions)
                or enrollment_windows
            ):
                continue

            configuration = self._merge_camera_geometry(
                target_configuration, compiled_geometry
            )

            desired_state = self._assignment_desired_state(
                session,
                deployment,
                source,
                entry,
                deployment.next_desired_revision,
                {camera.id: configuration},
            )
            errors = sorted(
                Draft202012Validator(entry.desired_state_schema).iter_errors(
                    desired_state
                ),
                key=lambda error: ".".join(
                    str(value) for value in error.absolute_path
                ),
            )
            if errors:
                error = errors[0]
                location = (
                    ".".join(str(value) for value in error.absolute_path)
                    or "desired_state"
                )
                raise ValueError(
                    f"invalid tvt-mills-pilot geometry at {location}: {error.message}"
                )

            bundle = copy.deepcopy(bundle_revision.canonical_bundle)
            bundle.setdefault("configuration", {})["desired_state_sha256"] = (
                hashlib.sha256(
                    json.dumps(
                        desired_state, sort_keys=True, separators=(",", ":")
                    ).encode()
                ).hexdigest()
            )
            validate_tvt_bundle(bundle)
            next_bundle = self._store_bundle_revision(
                session, deployment, bundle, "geometry_update", actor
            )
            clone = self._clone_assignment_set(
                session,
                deployment,
                source,
                next_bundle.id,
                actor,
                (
                    f"geometry:{camera.camera_key}:{camera.geometry_revision}:"
                    f"{deployment.deployment_key}"
                ),
                configuration_overrides={camera.id: configuration},
                geometry_revision_overrides={
                    camera.id: camera.geometry_revision
                },
            )
            for enrollment_session in enrollment_sessions:
                if enrollment_session.status == "restoring":
                    enrollment_session.restoration_assignment_set_id = clone.id
                    enrollment_session.restoration_revision = clone.desired_revision


    def create_camera_zone(
        self,
        camera_key: str,
        name: str,
        points: list[list[float]],
        actor: str,
        request_id: str,
    ) -> dict[str, Any]:
        with self.sessions.begin() as session:
            camera = self._camera_for_geometry_update(session, camera_key)
            existing_keys = set(
                session.scalars(
                    select(CameraGeometryShape.shape_key).where(
                        CameraGeometryShape.camera_id == camera.id,
                        CameraGeometryShape.deleted_at.is_(None),
                    )
                )
            )
            base = slugify(name)
            shape_key = base
            suffix = 2
            while shape_key in existing_keys:
                shape_key = f"{base}-{suffix}"
                suffix += 1
            normalized_points = [[float(x), float(y)] for x, y in points]
            validate_shape(
                ShapeInput(kind="zone", shape_key=shape_key, name=name, points=normalized_points)
            )
            row = CameraGeometryShape(
                camera_id=camera.id,
                kind="zone",
                shape_key=shape_key,
                name=name,
                points=normalized_points,
            )
            session.add(row)
            session.flush()
            camera.geometry_revision += 1
            configuration = self._compile_camera_geometry(session, camera.id)
            self._queue_camera_geometry_deployments(session, camera, configuration, actor)
            self._audit(
                session,
                actor=actor,
                request_id=request_id,
                action="camera.geometry.zone.create",
                target_type="camera",
                target_id=camera_key,
                details={"shape_key": shape_key},
            )
            return self._geometry_view(row)

    def create_camera_line(
        self,
        camera_key: str,
        name: str,
        points: list[list[float]],
        role_key: str,
        direction: str,
        inside_side: str,
        actor: str,
        request_id: str,
    ) -> dict[str, Any]:
        if not DNS_ID.fullmatch(role_key):
            raise ValueError("role_key must be a DNS-safe identifier")
        shape_key = line_shape_key(role_key, direction)
        normalized_points = [[float(x), float(y)] for x, y in points]
        validate_shape(
            ShapeInput(
                kind="line",
                shape_key=shape_key,
                name=name,
                points=normalized_points,
                role_key=role_key,
                direction=direction,
                inside_side=inside_side,
            )
        )
        with self.sessions.begin() as session:
            camera = self._camera_for_geometry_update(session, camera_key)
            existing = session.scalar(
                select(CameraGeometryShape).where(
                    CameraGeometryShape.camera_id == camera.id,
                    CameraGeometryShape.shape_key == shape_key,
                    CameraGeometryShape.deleted_at.is_(None),
                )
            )
            if existing is not None:
                raise ValueError(
                    f"camera already has a {direction} line for gate {role_key!r}"
                )
            row = CameraGeometryShape(
                camera_id=camera.id,
                kind="line",
                shape_key=shape_key,
                name=name,
                points=normalized_points,
                role_key=role_key,
                direction=direction,
                inside_side=inside_side,
            )
            session.add(row)
            session.flush()
            camera.geometry_revision += 1
            configuration = self._compile_camera_geometry(session, camera.id)
            self._queue_camera_geometry_deployments(session, camera, configuration, actor)
            self._audit(
                session,
                actor=actor,
                request_id=request_id,
                action="camera.geometry.line.create",
                target_type="camera",
                target_id=camera_key,
                details={"shape_key": shape_key, "role_key": role_key, "direction": direction},
            )
            return self._geometry_view(row)

    def update_camera_geometry(
        self,
        camera_key: str,
        shape_id: str,
        *,
        name: str,
        points: list[list[float]],
        role_key: str | None,
        direction: str | None,
        inside_side: str | None,
        actor: str,
        request_id: str,
    ) -> dict[str, Any]:
        try:
            shape_uuid = uuid.UUID(shape_id)
        except ValueError as error:
            raise ValueError(f"unknown geometry shape {shape_id!r}") from error
        normalized_points = [[float(x), float(y)] for x, y in points]
        with self.sessions.begin() as session:
            camera = self._camera_for_geometry_update(session, camera_key)
            shape = session.scalar(
                select(CameraGeometryShape)
                .where(
                    CameraGeometryShape.id == shape_uuid,
                    CameraGeometryShape.camera_id == camera.id,
                    CameraGeometryShape.deleted_at.is_(None),
                )
                .with_for_update()
            )
            if shape is None:
                raise ValueError(f"unknown geometry shape {shape_id!r}")

            if shape.kind == "zone":
                if role_key is not None or direction is not None or inside_side is not None:
                    raise ValueError(
                        "a zone carries no role_key, direction, or inside_side"
                    )
                shape_key = shape.shape_key
            else:
                if role_key is None or direction is None or inside_side is None:
                    raise ValueError(
                        "a line requires role_key, direction, and inside_side"
                    )
                if not DNS_ID.fullmatch(role_key):
                    raise ValueError("role_key must be a DNS-safe identifier")
                shape_key = line_shape_key(role_key, direction)
                duplicate = session.scalar(
                    select(CameraGeometryShape.id).where(
                        CameraGeometryShape.camera_id == camera.id,
                        CameraGeometryShape.shape_key == shape_key,
                        CameraGeometryShape.id != shape.id,
                        CameraGeometryShape.deleted_at.is_(None),
                    )
                )
                if duplicate is not None:
                    raise ValueError(
                        f"camera already has a {direction} line for gate {role_key!r}"
                    )

            validate_shape(
                ShapeInput(
                    kind=shape.kind,
                    shape_key=shape_key,
                    name=name,
                    points=normalized_points,
                    role_key=role_key,
                    direction=direction,
                    inside_side=inside_side,
                    enabled=shape.enabled,
                )
            )
            shape.shape_key = shape_key
            shape.name = name
            shape.points = normalized_points
            shape.role_key = role_key
            shape.direction = direction
            shape.inside_side = inside_side
            session.flush()
            camera.geometry_revision += 1
            configuration = self._compile_camera_geometry(session, camera.id)
            self._queue_camera_geometry_deployments(
                session, camera, configuration, actor
            )
            self._audit(
                session,
                actor=actor,
                request_id=request_id,
                action=f"camera.geometry.{shape.kind}.update",
                target_type="camera",
                target_id=camera_key,
                details={
                    "shape_key": shape.shape_key,
                    "role_key": shape.role_key,
                    "direction": shape.direction,
                },
            )
            return self._geometry_view(shape)

    def delete_camera_geometry(
        self, camera_key: str, shape_id: str, actor: str, request_id: str
    ) -> None:
        try:
            shape_uuid = uuid.UUID(shape_id)
        except ValueError as error:
            raise ValueError(f"unknown geometry shape {shape_id!r}") from error
        with self.sessions.begin() as session:
            camera = self._camera_for_geometry_update(session, camera_key)
            shape = session.get(CameraGeometryShape, shape_uuid)
            if shape is None or shape.camera_id != camera.id or shape.deleted_at is not None:
                raise ValueError(f"unknown geometry shape {shape_id!r}")
            shape.deleted_at = utc_now()
            session.flush()
            camera.geometry_revision += 1
            configuration = self._compile_camera_geometry(session, camera.id)
            self._queue_camera_geometry_deployments(session, camera, configuration, actor)
            self._audit(
                session,
                actor=actor,
                request_id=request_id,
                action="camera.geometry.delete",
                target_type="camera",
                target_id=camera_key,
                details={"shape_key": shape.shape_key, "kind": shape.kind},
            )

    def list_cameras(self) -> list[dict[str, Any]]:
        with self.sessions() as session:
            cameras = session.scalars(
                select(Camera).where(Camera.deleted_at.is_(None)).order_by(Camera.camera_key)
            ).all()
            return [self._camera_view(session, camera) for camera in cameras]

    def get_camera(self, camera_key: str) -> dict[str, Any]:
        with self.sessions() as session:
            camera = self._camera(session, camera_key)
            return self._camera_view(session, camera)

    def get_camera_deployment_status(self, camera_key: str) -> dict[str, Any]:
        with self.sessions() as session:
            camera = self._camera(session, camera_key)
            deployments = session.scalars(
                select(SolutionDeployment)
                .join(
                    DeploymentSyncState,
                    DeploymentSyncState.deployment_id == SolutionDeployment.id,
                )
                .join(
                    CameraDeploymentAssignment,
                    CameraDeploymentAssignment.assignment_set_id
                    == DeploymentSyncState.desired_assignment_set_id,
                )
                .where(
                    CameraDeploymentAssignment.camera_id == camera.id,
                    SolutionDeployment.deleted_at.is_(None),
                )
                .order_by(SolutionDeployment.deployment_key)
            ).unique().all()
            items: list[dict[str, Any]] = []
            for deployment in deployments:
                sync = session.get(DeploymentSyncState, deployment.id)
                desired = (
                    session.get(
                        DeploymentAssignmentSet, sync.desired_assignment_set_id
                    )
                    if sync is not None
                    else None
                )
                if desired is None:
                    continue
                desired_assignment = session.scalar(
                    select(CameraDeploymentAssignment).where(
                        CameraDeploymentAssignment.assignment_set_id == desired.id,
                        CameraDeploymentAssignment.camera_id == camera.id,
                    )
                )
                if desired_assignment is None:
                    continue
                applied = (
                    session.get(
                        DeploymentAssignmentSet, sync.applied_assignment_set_id
                    )
                    if sync.applied_assignment_set_id is not None
                    else None
                )
                applied_assignment = (
                    session.scalar(
                        select(CameraDeploymentAssignment).where(
                            CameraDeploymentAssignment.assignment_set_id == applied.id,
                            CameraDeploymentAssignment.camera_id == camera.id,
                        )
                    )
                    if applied is not None
                    else None
                )
                bundle_revision = session.get(
                    SolutionBundleRevision, desired.bundle_revision_id
                )
                applicable = (
                    bundle_revision is not None
                    and self._geometry_catalog_entry(session, bundle_revision)
                    is not None
                )
                enrollment_owned = (
                    session.scalar(
                        select(EnrollmentSession.id).where(
                            EnrollmentSession.deployment_id == deployment.id,
                            EnrollmentSession.camera_id == camera.id,
                            EnrollmentSession.status.in_(
                                ENROLLMENT_ACTIVE_STATUSES
                            ),
                        )
                    )
                    is not None
                    or session.scalar(
                        select(EnrollmentWindow.id).where(
                            EnrollmentWindow.deployment_id == deployment.id,
                            EnrollmentWindow.camera_id == camera.id,
                            EnrollmentWindow.status == "active",
                        )
                    )
                    is not None
                )
                if not applicable:
                    state = "not_applicable"
                elif enrollment_owned:
                    state = "waiting_for_enrollment"
                elif sync.state in {
                    "pending",
                    "applying",
                    "applied",
                    "failed",
                    "operator_required",
                }:
                    state = sync.state
                else:
                    state = "pending"
                attempt = session.scalar(
                    select(DeploymentSyncAttempt)
                    .where(
                        DeploymentSyncAttempt.deployment_id == deployment.id,
                        DeploymentSyncAttempt.desired_revision
                        == desired.desired_revision,
                    )
                    .order_by(DeploymentSyncAttempt.attempt_number.desc())
                    .limit(1)
                )
                retry_at = (
                    attempt.retry_at
                    if attempt is not None and attempt.retry_at is not None
                    else sync.next_attempt_at
                )
                items.append(
                    {
                        "deployment_id": deployment.deployment_key,
                        "state": state,
                        "phase": attempt.phase if attempt is not None else None,
                        "desired_revision": desired.desired_revision,
                        "applied_revision": (
                            applied.desired_revision if applied is not None else None
                        ),
                        "desired_geometry_revision": (
                            desired_assignment.geometry_revision
                        ),
                        "applied_geometry_revision": (
                            applied_assignment.geometry_revision
                            if applied_assignment is not None
                            else None
                        ),
                        "last_error_code": (
                            sync.last_error_code
                            or (attempt.error_code if attempt is not None else None)
                        ),
                        "retry_at": retry_at.isoformat() if retry_at else None,
                    }
                )

            if not items:
                overall_state = "not_assigned"
            else:
                priority = {
                    "failed": 6,
                    "applying": 5,
                    "pending": 4,
                    "waiting_for_enrollment": 3,
                    "applied": 2,
                    "not_applicable": 1,
                }
                overall_state = max(
                    (item["state"] for item in items),
                    key=lambda state: priority[state],
                )
            return {
                "camera_id": camera.camera_key,
                "geometry_revision": camera.geometry_revision,
                "overall_state": overall_state,
                "deployments": items,
            }

    def camera_workload_names(self, camera_key: str) -> list[str]:
        """K8s Deployment names (deployment_key-app_name) currently serving this camera."""
        with self.sessions() as session:
            camera = self._camera(session, camera_key)
            rows = session.execute(
                select(DeploymentAssignmentSet, SolutionDeployment)
                .join(
                    CameraDeploymentAssignment,
                    CameraDeploymentAssignment.assignment_set_id
                    == DeploymentAssignmentSet.id,
                )
                .join(
                    DeploymentSyncState,
                    DeploymentSyncState.desired_assignment_set_id
                    == DeploymentAssignmentSet.id,
                )
                .join(
                    SolutionDeployment,
                    SolutionDeployment.id == DeploymentAssignmentSet.deployment_id,
                )
                .where(CameraDeploymentAssignment.camera_id == camera.id)
                .distinct()
            ).all()
            names: list[str] = []
            for assignment_set, deployment in rows:
                revision = session.get(
                    SolutionBundleRevision, assignment_set.bundle_revision_id
                )
                if revision is None:
                    continue
                app_name = revision.canonical_bundle["applications"][0]["name"]
                names.append(f"{deployment.deployment_key}-{app_name}")
            return names

    @staticmethod
    def _runtime_workload_name(
        session: Session, deployment: SolutionDeployment
    ) -> str:
        """Return the rendered workload name that owns analytics events.

        Apex telemetry is partitioned by the Kubernetes workload name, not by
        the logical TVT deployment key. Keep this derivation coupled to the
        committed bundle instead of assuming every Solution Pack calls its
        event-producing application ``runtime``.
        """

        assignment_set = session.scalar(
            select(DeploymentAssignmentSet)
            .where(DeploymentAssignmentSet.deployment_id == deployment.id)
            .order_by(DeploymentAssignmentSet.desired_revision.desc())
            .limit(1)
        )
        if assignment_set is None:
            raise ValueError("deployment has no committed assignments")
        revision = session.get(SolutionBundleRevision, assignment_set.bundle_revision_id)
        if revision is None:
            raise ValueError("deployment bundle revision is unavailable")
        applications = revision.canonical_bundle.get("applications", [])
        event_apps = [
            item
            for item in applications
            if isinstance(item, dict)
            and isinstance(item.get("telemetry"), dict)
            and isinstance(item["telemetry"].get("events"), dict)
        ]
        candidates = event_apps or [
            item for item in applications if isinstance(item, dict) and item.get("name")
        ]
        if len(candidates) != 1:
            raise ValueError("deployment must have exactly one event-producing workload")
        return f"{deployment.deployment_key}-{candidates[0]['name']}"

    @staticmethod
    def _camera(session: Session, camera_key: str) -> Camera:
        camera = session.scalar(
            select(Camera).where(Camera.camera_key == camera_key, Camera.deleted_at.is_(None))
        )
        if camera is None:
            raise ValueError(f"unknown camera {camera_key!r}")
        return camera

    @staticmethod
    def _camera_view(session: Session, camera: Camera) -> dict[str, Any]:
        profile = session.scalar(
            select(CameraStreamProfile).where(
                CameraStreamProfile.camera_id == camera.id,
                CameraStreamProfile.selected.is_(True),
            )
        )
        credential = session.scalar(
            select(CameraCredentialVersion).where(
                CameraCredentialVersion.camera_id == camera.id,
                CameraCredentialVersion.state == "active",
            )
        )
        identifiers = session.scalars(
            select(CameraIdentifier).where(
                CameraIdentifier.camera_id == camera.id,
                CameraIdentifier.active.is_(True),
            )
        ).all()
        endpoint = session.get(CameraEndpoint, profile.endpoint_id) if profile else None
        role_rows = session.execute(
            select(CameraRoleAssignment, CameraRole)
            .join(CameraRole, CameraRole.id == CameraRoleAssignment.role_id)
            .where(
                CameraRoleAssignment.camera_id == camera.id,
                CameraRoleAssignment.ended_at.is_(None),
            )
            .order_by(CameraRole.role_key, CameraRoleAssignment.ordinal)
        ).all()
        assignment_rows = session.execute(
            select(
                CameraDeploymentAssignment,
                DeploymentAssignmentSet,
                SolutionDeployment,
            )
            .join(
                DeploymentAssignmentSet,
                DeploymentAssignmentSet.id
                == CameraDeploymentAssignment.assignment_set_id,
            )
            .join(
                DeploymentSyncState,
                DeploymentSyncState.desired_assignment_set_id
                == DeploymentAssignmentSet.id,
            )
            .join(
                SolutionDeployment,
                SolutionDeployment.id == DeploymentAssignmentSet.deployment_id,
            )
            .where(CameraDeploymentAssignment.camera_id == camera.id)
            .order_by(SolutionDeployment.deployment_key)
        ).all()
        assignments = []
        for camera_assignment, _assignment_set, deployment in assignment_rows:
            apps = session.scalars(
                select(CameraApplicationAssignment.use_case_key)
                .where(
                    CameraApplicationAssignment.camera_assignment_id
                    == camera_assignment.id
                )
                .order_by(CameraApplicationAssignment.use_case_key)
            ).all()
            assignments.append(
                {
                    "deployment_id": deployment.deployment_key,
                    "apps": list(apps),
                    "fps": camera_assignment.requested_fps,
                }
            )
        return {
            "camera_id": camera.camera_key,
            "friendly_name": camera.friendly_name,
            "manufacturer": camera.manufacturer,
            "model": camera.model,
            "geometry_revision": camera.geometry_revision,
            "configured": profile is not None,
            "enabled": camera.enabled,
            "credentials_configured": credential is not None,
            "selected_profile_id": str(profile.id) if profile else None,
            "selected_profile": {
                "profile_id": str(profile.id),
                "profile_token": profile.profile_token,
                "scheme": endpoint.scheme if endpoint else None,
                "host": endpoint.host if endpoint else None,
                "port": endpoint.port if endpoint else None,
                "path": profile.path,
                "transport": profile.transport,
                "codec": profile.codec,
                "width": profile.width,
                "height": profile.height,
                "fps": float(profile.fps) if profile.fps is not None else None,
            }
            if profile
            else None,
            "roles": [
                {
                    "role_key": role.role_key,
                    "display_name": role.display_name,
                    "direction": assignment.direction,
                    "ordinal": assignment.ordinal,
                }
                for assignment, role in role_rows
            ],
            "assignments": assignments,
            "identifiers": [
                {"kind": item.kind, "value": item.display_value or item.normalized_value}
                for item in identifiers
            ],
            "created_at": camera.created_at.isoformat(),
            "updated_at": camera.updated_at.isoformat(),
        }


    def list_audit_events(self, limit: int = 200) -> list[dict[str, Any]]:
        if limit <= 0 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        with self.sessions() as session:
            events = session.scalars(
                select(AuditEvent)
                .order_by(AuditEvent.created_at.desc())
                .limit(limit)
            ).all()
            return [
                {
                    "audit_id": str(event.id),
                    "actor": event.actor,
                    "request_id": event.request_id,
                    "action": event.action,
                    "target_type": event.target_type,
                    "target_id": event.target_id,
                    "result": event.result,
                    "details": redact(event.details),
                    "created_at": event.created_at.isoformat(),
                }
                for event in events
            ]

    @staticmethod
    def _upsert_stream_profile(
        session: Session,
        camera: Camera,
        *,
        scheme: str,
        host: str,
        port: int,
        path: str,
        profile_token: str,
        transport: str,
        codec: str | None,
        width: int | None,
        height: int | None,
        fps: float | None,
    ) -> CameraStreamProfile:
        if scheme not in {"rtsp", "rtsps"} or not SAFE_HOST.fullmatch(host):
            raise ValueError("stream endpoint is invalid")
        if not 1 <= port <= 65535:
            raise ValueError("stream port is invalid")
        if not path.startswith("/") or any(char in path for char in "?#@"):
            raise ValueError("stream path must be non-secret and contain no query/userinfo")
        if transport not in {"tcp", "udp"}:
            raise ValueError("transport must be tcp or udp")
        session.query(CameraStreamProfile).filter_by(camera_id=camera.id).update(
            {"selected": False}
        )
        endpoint = session.scalar(
            select(CameraEndpoint).where(
                CameraEndpoint.camera_id == camera.id,
                CameraEndpoint.kind == "rtsp",
                CameraEndpoint.host == host,
                CameraEndpoint.port == port,
            )
        )
        if endpoint is None:
            endpoint = CameraEndpoint(
                camera_id=camera.id,
                kind="rtsp",
                scheme=scheme,
                host=host,
                port=port,
                path="/",
            )
            session.add(endpoint)
            session.flush()
        elif endpoint.scheme != scheme:
            endpoint.scheme = scheme
        profile = session.scalar(
            select(CameraStreamProfile).where(
                CameraStreamProfile.camera_id == camera.id,
                CameraStreamProfile.profile_token == profile_token,
            )
        )
        values = {
            "endpoint_id": endpoint.id,
            "path": path,
            "transport": transport,
            "codec": codec,
            "width": width,
            "height": height,
            "fps": fps,
            "available": True,
            "selected": True,
            "observed_at": utc_now(),
        }
        if profile is None:
            profile = CameraStreamProfile(
                camera_id=camera.id, profile_token=profile_token, **values
            )
            session.add(profile)
        else:
            for key, value in values.items():
                setattr(profile, key, value)
        return profile

    def configure_stream(
        self,
        camera_key: str,
        *,
        scheme: str,
        host: str,
        port: int,
        path: str,
        profile_token: str,
        transport: str,
        codec: str | None,
        width: int | None,
        height: int | None,
        fps: float | None,
        actor: str,
        request_id: str,
    ) -> CameraStreamProfile:
        with self.sessions.begin() as session:
            camera = self._camera(session, camera_key)
            profile = self._upsert_stream_profile(
                session,
                camera,
                scheme=scheme,
                host=host,
                port=port,
                path=path,
                profile_token=profile_token,
                transport=transport,
                codec=codec,
                width=width,
                height=height,
                fps=fps,
            )
            camera.row_version += 1
            self._audit(
                session,
                actor=actor,
                request_id=request_id,
                action="camera.stream.configure",
                target_type="camera",
                target_id=camera_key,
                details={"profile_token": profile_token, "host": host, "port": port},
            )
            return profile

    @staticmethod
    def _validate_credential_document(document: dict[str, Any]) -> None:
        allowed = {"username", "password", "query", "path_suffix"}
        if set(document) - allowed:
            raise ValueError("credential document contains unsupported fields")
        for field in ("username", "password", "path_suffix"):
            value = document.get(field)
            if value is not None and (
                not isinstance(value, str)
                or len(value) > 1024
                or any(ord(char) < 32 for char in value)
            ):
                raise ValueError(f"credential {field} is invalid")
        query = document.get("query", {})
        if not isinstance(query, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in query.items()
        ):
            raise ValueError("credential query must be a string mapping")

    def rotate_credentials(
        self,
        camera_key: str,
        document: dict[str, Any],
        actor: str,
        request_id: str,
    ) -> CameraCredentialVersion:
        self._validate_credential_document(document)
        with self.sessions.begin() as session:
            camera = self._camera(session, camera_key)
            existing_audit = session.scalar(
                select(AuditEvent).where(
                    AuditEvent.request_id == request_id,
                    AuditEvent.action == "camera.credentials.rotate",
                    AuditEvent.target_id == camera_key,
                )
            )
            if existing_audit is not None:
                active = self._active_credential(session, camera.id)
                if active is None:
                    raise ValueError("idempotent credential result is unavailable")
                return active
            old = self._active_credential(session, camera.id)
            highest = session.scalar(
                select(func.max(CameraCredentialVersion.credential_version)).where(
                    CameraCredentialVersion.camera_id == camera.id
                )
            ) or 0
            credential_id = uuid.uuid4()
            encrypted = self.keyring.encrypt(camera.id, credential_id, document)
            key_meta = session.get(CredentialKeyVersion, encrypted.key_version)
            if key_meta is None:
                session.add(CredentialKeyVersion(version=encrypted.key_version))
                # CredentialKeyVersion <-> CameraCredentialVersion is a bare
                # column-level ForeignKey with no declared relationship(), so
                # the unit-of-work flush has no dependency edge between them
                # and isn't guaranteed to insert this row before the
                # camera_credential_versions insert below (observed in
                # production as a ForeignKeyViolation on first use of a new
                # key version). Force it in now.
                session.flush()
            now = utc_now()
            if old is not None:
                old.state = "superseded"
                old.superseded_at = now
                old.purge_after = now + timedelta(days=30)
            credential = CameraCredentialVersion(
                id=credential_id,
                camera_id=camera.id,
                credential_version=highest + 1,
                ciphertext=encrypted.ciphertext,
                nonce=encrypted.nonce,
                key_version=encrypted.key_version,
                aad_version=encrypted.aad_version,
                state="active",
                created_by=actor,
                activated_at=now,
            )
            session.add(credential)
            camera.row_version += 1
            session.flush()
            if old is not None:
                self._requeue_credential_consumers(
                    session, camera.id, credential.id, actor, request_id
                )
            self._audit(
                session,
                actor=actor,
                request_id=request_id,
                action="camera.credentials.rotate",
                target_type="camera",
                target_id=camera_key,
                details={"credential_version": highest + 1},
            )
            return credential

    @staticmethod
    def _active_credential(
        session: Session, camera_id: uuid.UUID
    ) -> CameraCredentialVersion | None:
        return session.scalar(
            select(CameraCredentialVersion).where(
                CameraCredentialVersion.camera_id == camera_id,
                CameraCredentialVersion.state == "active",
            )
        )

    def set_camera_enabled(
        self, camera_key: str, enabled: bool, actor: str, request_id: str
    ) -> Camera:
        with self.sessions.begin() as session:
            camera = self._camera(session, camera_key)
            if enabled:
                profile_id = session.scalar(
                    select(CameraStreamProfile.id).where(
                        CameraStreamProfile.camera_id == camera.id,
                        CameraStreamProfile.selected.is_(True),
                    )
                )
                if profile_id is None:
                    raise ValueError("camera requires a selected stream profile")
                camera.enabled = True
            else:
                camera.enabled = False
            camera.row_version += 1
            self._audit(
                session,
                actor=actor,
                request_id=request_id,
                action="camera.enable" if enabled else "camera.disable",
                target_type="camera",
                target_id=camera_key,
            )
            return camera

    def management_status(self) -> dict[str, Any]:
        """Aggregate durable camera-configuration and synchronization state."""

        with self.sessions() as session:
            cameras = session.scalars(
                select(Camera).where(Camera.deleted_at.is_(None))
            ).all()
            enabled = [camera for camera in cameras if camera.enabled]
            configured_ids = set(
                session.scalars(
                    select(CameraStreamProfile.camera_id).where(
                        CameraStreamProfile.selected.is_(True)
                    )
                ).all()
            )
            configured = sum(1 for camera in cameras if camera.id in configured_ids)
            if not cameras:
                camera_health = "unconfigured"
            elif any(
                camera.enabled and camera.id not in configured_ids for camera in cameras
            ):
                camera_health = "degraded"
            else:
                camera_health = "healthy"

            deployments = session.scalars(
                select(SolutionDeployment).where(SolutionDeployment.deleted_at.is_(None))
            ).all()
            deployment_ids = [deployment.id for deployment in deployments]
            sync_states = Counter(
                session.scalars(
                    select(DeploymentSyncState.state).where(
                        DeploymentSyncState.deployment_id.in_(deployment_ids)
                    )
                ).all()
                if deployment_ids
                else []
            )
            unconfigured = len(deployments) - sum(sync_states.values())
            if unconfigured:
                sync_states["unconfigured"] = unconfigured
            if not deployments or sync_states.get("unconfigured") == len(deployments):
                sync_health = "unconfigured"
            elif sync_states.get("failed"):
                sync_health = "degraded"
            elif sync_states.get("pending") or sync_states.get("applying"):
                sync_health = "progressing"
            else:
                sync_health = "healthy"

            return {
                "cameras": {
                    "status": camera_health,
                    "total": len(cameras),
                    "enabled": len(enabled),
                    "configured": configured,
                },
                "synchronization": {
                    "status": sync_health,
                    "total": len(deployments),
                    "by_state": dict(sorted(sync_states.items())),
                },
            }

    def synchronization_status(self) -> dict[str, Any]:
        """Return aggregate and bounded per-deployment synchronization state."""

        summary = self.management_status()["synchronization"]
        deployments = self.list_deployments()
        return {
            **summary,
            "items": deployments,
            "items_truncated": summary["total"] > len(deployments),
        }

    def register_deployment(
        self,
        bundle: dict[str, Any],
        namespace: str,
        registry: str,
        actor: str,
        request_id: str,
    ) -> SolutionDeployment:
        candidate = copy.deepcopy(bundle)
        apply_registry(candidate, registry)
        validate_tvt_bundle(candidate)
        candidate = canonical_bundle(candidate)
        with self.sessions.begin() as session:
            site = self.current_site(session)
            key = candidate["deployment_id"]
            deployment = session.scalar(
                select(SolutionDeployment).where(
                    SolutionDeployment.site_id == site.id,
                    SolutionDeployment.deployment_key == key,
                )
            )
            if deployment is None:
                deployment = SolutionDeployment(
                    site_id=site.id,
                    deployment_key=key,
                    solution_id=candidate["solution"]["solution_id"],
                    namespace=namespace,
                    registry=registry,
                )
                session.add(deployment)
                session.flush()
            else:
                if deployment.solution_id != candidate["solution"]["solution_id"]:
                    raise ValueError(
                        "a deployment ID cannot be reused for another solution"
                    )
                if (
                    deployment.namespace != namespace
                    and session.get(DeploymentSyncState, deployment.id) is not None
                ):
                    raise ValueError(
                        "deployment namespace cannot change after assignments are committed"
                    )
                deployment.namespace = namespace
                deployment.registry = registry
            self._store_bundle_revision(session, deployment, candidate, "register", actor)
            self._audit(
                session,
                actor=actor,
                request_id=request_id,
                action="deployment.register",
                target_type="deployment",
                target_id=key,
                details={"bundle_sha256": bundle_sha256(candidate)},
            )
            return deployment

    def _catalog_deployment_candidate(
        self,
        session: Session,
        *,
        catalog_id: str,
        deployment_key: str,
        assignments: list[dict[str, Any]],
        inference_mode: str,
        resources: dict[str, str],
        state_size: str,
        lock_deployment: bool = False,
    ) -> tuple[
        SolutionCatalogEntry,
        SolutionDeployment | None,
        list[tuple[Camera, CameraStreamProfile, CameraCredentialVersion | None, dict[str, Any]]],
        dict[str, Any],
        dict[str, Any],
    ]:
        entry = session.get(SolutionCatalogEntry, catalog_id)
        if entry is None:
            raise ValueError("unknown solution catalog entry")
        catalog = self._solution_view(entry)
        if entry.status != "available" or entry.resolved_digest is None:
            raise ValueError("only an available catalog entry can be deployed")
        solution_pack = PACK_NAME_TO_SOLUTION_PACK.get(
            catalog.get("contract", {}).get("name"), catalog.get("contract", {}).get("name")
        )
        solution_id = f"{solution_pack}-edge"
        site = self.current_site(session)
        deployment_query = select(SolutionDeployment).where(
            SolutionDeployment.site_id == site.id,
            SolutionDeployment.deployment_key == deployment_key,
            SolutionDeployment.deleted_at.is_(None),
        )
        if lock_deployment:
            deployment_query = deployment_query.with_for_update()
        deployment = session.scalar(deployment_query)
        if deployment is not None and deployment.solution_id != solution_id:
            raise ValueError("a deployment ID cannot be reused for another solution")
        desired_revision = deployment.next_desired_revision if deployment else 1
        resolved = []
        bundle_cameras: list[BundleCamera] = []
        desired_cameras: list[dict[str, Any]] = []
        for position, item in enumerate(assignments):
            camera = self._camera(session, item["camera_id"])
            if camera.site_id != site.id or not camera.enabled:
                raise ValueError(f"camera {camera.camera_key!r} is not enabled")
            profile = session.scalar(
                select(CameraStreamProfile).where(
                    CameraStreamProfile.camera_id == camera.id,
                    CameraStreamProfile.selected.is_(True),
                )
            )
            if profile is None:
                raise ValueError(f"camera {camera.camera_key!r} has no selected profile")
            credential = self._active_credential(session, camera.id)
            apps = tuple(item["apps"])
            fps = int(item.get("fps", 8))
            config = copy.deepcopy(item.get("config", {}))
            bundle_cameras.append(BundleCamera(camera.camera_key, fps, apps))
            normalized = {**item, "config": config, "ordinal": position}
            resolved.append((camera, profile, credential, normalized))
            desired_camera = {
                "camera_id": camera.camera_key,
                "source": f"file:/run/secrets/apexfabric/{camera.camera_key}.rtsp",
                "solution_pack": solution_pack,
                "fps": fps,
                "apps": list(apps),
            }
            if config or solution_pack == "tvt-mills-pilot":
                desired_camera["config"] = config
            desired_cameras.append(desired_camera)
        desired_state = {
            "edge_id": site.edge_id,
            "revision": desired_revision,
            "cameras": desired_cameras,
        }
        errors = sorted(
            Draft202012Validator(entry.desired_state_schema).iter_errors(desired_state),
            key=lambda error: ".".join(str(value) for value in error.absolute_path),
        )
        if errors:
            error = errors[0]
            location = ".".join(str(value) for value in error.absolute_path) or "desired_state"
            raise ValueError(f"invalid {solution_pack} geometry at {location}: {error.message}")
        for camera in desired_cameras:
            config = camera.get("config", {})
            apps = set(camera["apps"])
            if "wrong_way" in apps and not config.get("lines", {}).get("wrong_way"):
                raise ValueError("wrong_way requires config.lines.wrong_way geometry")
            if "illegal_parking" in apps and not config.get("zones", {}).get("illegal_parking"):
                raise ValueError("illegal_parking requires config.zones.illegal_parking geometry")
        bundle = catalog_traffic_bundle(
            catalog,
            deployment_key,
            site.edge_id,
            bundle_cameras,
            inference_mode=inference_mode,
            cpu_request=resources.get("cpu_request", "8"),
            cpu_limit=resources.get("cpu_limit", "16"),
            memory_request=resources.get("memory_request", "16Gi"),
            memory_limit=resources.get("memory_limit", "32Gi"),
            state_size=state_size,
        )
        if deployment is not None:
            bundle["applications"][0].setdefault("lifecycle", {})[
                "desired_state"
            ] = deployment.lifecycle_intent
        desired_sha = hashlib.sha256(
            json.dumps(desired_state, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        bundle["configuration"]["desired_state_sha256"] = desired_sha
        validate_tvt_bundle(bundle)
        return entry, deployment, resolved, canonical_bundle(bundle), desired_state

    def preview_catalog_deployment(
        self,
        *,
        catalog_id: str,
        deployment_key: str,
        assignments: list[dict[str, Any]],
        inference_mode: str,
        resources: dict[str, str],
        state_size: str,
        namespace: str,
    ) -> dict[str, Any]:
        if namespace != "apexfabric":
            raise ValueError("catalog deployments must use the apexfabric namespace")
        with self.sessions() as session:
            entry, _deployment, _resolved, bundle, desired_state = self._catalog_deployment_candidate(
                session,
                catalog_id=catalog_id,
                deployment_key=deployment_key,
                assignments=assignments,
                inference_mode=inference_mode,
                resources=resources,
                state_size=state_size,
            )
            return {
                "catalog_id": entry.catalog_id,
                "bundle_sha256": bundle_sha256(bundle),
                "image_reference": bundle["applications"][0]["image"]["repository"]
                + "@"
                + bundle["applications"][0]["image"]["digest"],
                "bundle": bundle,
                "desired_state": desired_state,
            }

    def commit_catalog_deployment(
        self,
        *,
        catalog_id: str,
        deployment_key: str,
        assignments: list[dict[str, Any]] | None,
        inference_mode: str | None,
        resources: dict[str, str] | None,
        state_size: str | None,
        namespace: str,
        preview_bundle_sha256: str,
        idempotency_key: str,
        actor: str,
        request_id: str,
        expected_applied_bundle_sha256: str | None = None,
        _enrollment_session_id: uuid.UUID | None = None,
    ) -> DeploymentAssignmentSet:
        if namespace != "apexfabric":
            raise ValueError("catalog deployments must use the apexfabric namespace")
        with self.sessions.begin() as session:
            site = self.current_site(session)
            idempotent_deployment = session.scalar(
                select(SolutionDeployment)
                .where(
                    SolutionDeployment.site_id == site.id,
                    SolutionDeployment.deployment_key == deployment_key,
                    SolutionDeployment.deleted_at.is_(None),
                )
                .with_for_update()
            )
            if idempotent_deployment is not None:
                existing = session.scalar(
                    select(DeploymentAssignmentSet).where(
                        DeploymentAssignmentSet.deployment_id
                        == idempotent_deployment.id,
                        DeploymentAssignmentSet.idempotency_key == idempotency_key,
                    )
                )
                if existing is not None:
                    return existing
                if expected_applied_bundle_sha256 is not None:
                    (
                        _source_catalog_id,
                        assignments,
                        inference_mode,
                        resources,
                        state_size,
                        source_bundle,
                    ) = self._applied_catalog_snapshot(
                        session, idempotent_deployment, for_update=True
                    )
                    if source_bundle.bundle_sha256 != expected_applied_bundle_sha256:
                        raise ValueError(
                            "applied deployment changed after upgrade preview; preview it again"
                        )
            if assignments is None:
                raise ValueError("catalog deployment assignments are required")
            if inference_mode is None or resources is None or state_size is None:
                raise ValueError("catalog deployment settings are required")
            if idempotent_deployment is not None:
                self._block_enrollment_conflicts(
                    session, idempotent_deployment.id, assignments, _enrollment_session_id
                )
            entry, deployment, resolved, bundle, _desired_state = self._catalog_deployment_candidate(
                session,
                catalog_id=catalog_id,
                deployment_key=deployment_key,
                assignments=assignments,
                inference_mode=inference_mode,
                resources=resources,
                state_size=state_size,
                lock_deployment=True,
            )
            actual_sha = bundle_sha256(bundle)
            if actual_sha != preview_bundle_sha256:
                raise ValueError("deployment changed after preview; preview it again")
            if deployment is None:
                deployment = SolutionDeployment(
                    site_id=site.id,
                    deployment_key=deployment_key,
                    solution_id=bundle["solution"]["solution_id"],
                    namespace=namespace,
                    registry=entry.local_registry,
                )
                session.add(deployment)
                session.flush()
            deployment.registry = entry.local_registry
            bundle_revision = self._store_bundle_revision(
                session, deployment, bundle, "catalog_commit", actor
            )
            assignment_set = DeploymentAssignmentSet(
                deployment_id=deployment.id,
                bundle_revision_id=bundle_revision.id,
                desired_revision=deployment.next_desired_revision,
                idempotency_key=idempotency_key,
                actor=actor,
            )
            deployment.next_desired_revision += 1
            session.add(assignment_set)
            session.flush()
            for camera, profile, credential, item in resolved:
                camera_assignment = CameraDeploymentAssignment(
                    assignment_set_id=assignment_set.id,
                    camera_id=camera.id,
                    stream_profile_id=profile.id,
                    credential_version_id=credential.id if credential else None,
                    ordinal=item["ordinal"],
                    requested_fps=int(item.get("fps", 8)),
                    geometry_revision=camera.geometry_revision,
                )
                session.add(camera_assignment)
                session.flush()
                for use_case in item["apps"]:
                    session.add(CameraApplicationAssignment(
                        camera_assignment_id=camera_assignment.id,
                        bundle_application="runtime",
                        use_case_key=use_case,
                        configuration=copy.deepcopy(item["config"]),
                    ))
            self._set_desired(session, deployment.id, assignment_set.id)
            self._audit(
                session,
                actor=actor,
                request_id=request_id,
                action="deployment.catalog.commit",
                target_type="deployment",
                target_id=deployment_key,
                details={
                    "catalog_id": catalog_id,
                    "bundle_sha256": actual_sha,
                    "desired_revision": assignment_set.desired_revision,
                },
            )
            return assignment_set

    def preview_catalog_upgrade(
        self, *, deployment_key: str, catalog_id: str
    ) -> dict[str, Any]:
        """Preview an image/catalog upgrade from the last applied snapshot.

        Assignments are reconstructed server-side so host automation never
        needs camera endpoints, credentials, or another secret-bearing input.
        The returned source bundle digest is a compare-and-swap token for the
        commit endpoint.
        """

        with self.sessions() as session:
            deployment = session.scalar(
                select(SolutionDeployment).where(
                    SolutionDeployment.deployment_key == deployment_key,
                    SolutionDeployment.deleted_at.is_(None),
                )
            )
            if deployment is None:
                raise ValueError(f"unknown deployment {deployment_key!r}")
            (
                source_catalog_id,
                assignments,
                inference_mode,
                resources,
                state_size,
                source_bundle,
            ) = self._applied_catalog_snapshot(session, deployment)
            entry, _deployment, _resolved, bundle, desired_state = (
                self._catalog_deployment_candidate(
                    session,
                    catalog_id=catalog_id,
                    deployment_key=deployment_key,
                    assignments=assignments,
                    inference_mode=inference_mode,
                    resources=resources,
                    state_size=state_size,
                )
            )
            source_image = source_bundle.canonical_bundle["applications"][0]["image"]
            target_image = bundle["applications"][0]["image"]
            return {
                "deployment_id": deployment_key,
                "source_catalog_id": source_catalog_id,
                "source_applied_revision": self._applied_revision(
                    session, deployment.id
                ),
                "source_bundle_sha256": source_bundle.bundle_sha256,
                "source_image_digest": source_image.get("digest"),
                "catalog_id": entry.catalog_id,
                "bundle_sha256": bundle_sha256(bundle),
                "image_reference": (
                    target_image["repository"] + "@" + target_image["digest"]
                ),
                "bundle": bundle,
                "desired_state": desired_state,
            }

    def commit_catalog_upgrade(
        self,
        *,
        deployment_key: str,
        catalog_id: str,
        source_bundle_sha256: str,
        preview_bundle_sha256: str,
        idempotency_key: str,
        actor: str,
        request_id: str,
    ) -> DeploymentAssignmentSet:
        return self.commit_catalog_deployment(
            catalog_id=catalog_id,
            deployment_key=deployment_key,
            assignments=None,
            inference_mode=None,
            resources=None,
            state_size=None,
            namespace="apexfabric",
            preview_bundle_sha256=preview_bundle_sha256,
            idempotency_key=idempotency_key,
            actor=actor,
            request_id=request_id,
            expected_applied_bundle_sha256=source_bundle_sha256,
        )

    @staticmethod
    def _applied_revision(session: Session, deployment_id: uuid.UUID) -> int:
        sync = session.get(DeploymentSyncState, deployment_id)
        if sync is None or sync.applied_assignment_set_id is None:
            raise ValueError("deployment has no applied revision")
        applied = session.get(
            DeploymentAssignmentSet, sync.applied_assignment_set_id
        )
        if applied is None:
            raise ValueError("applied assignment state is missing")
        return applied.desired_revision

    def _applied_catalog_snapshot(
        self,
        session: Session,
        deployment: SolutionDeployment,
        *,
        for_update: bool = False,
    ) -> tuple[
        str,
        list[dict[str, Any]],
        str,
        dict[str, str],
        str,
        SolutionBundleRevision,
    ]:
        query = select(DeploymentSyncState).where(
            DeploymentSyncState.deployment_id == deployment.id
        )
        if for_update:
            query = query.with_for_update()
        sync = session.scalar(query)
        if (
            sync is None
            or sync.state != "applied"
            or sync.applied_assignment_set_id is None
            or sync.desired_assignment_set_id != sync.applied_assignment_set_id
        ):
            raise ValueError(
                "deployment must have one stable applied revision before an upgrade"
            )
        if self._active_enrollment_session(session, deployment.id) is not None:
            raise ValueError("deployment upgrade is blocked by an active enrollment session")
        assignment_set = session.get(
            DeploymentAssignmentSet, sync.applied_assignment_set_id
        )
        if assignment_set is None:
            raise ValueError("applied assignment state is missing")
        bundle_revision = session.get(
            SolutionBundleRevision, assignment_set.bundle_revision_id
        )
        if bundle_revision is None:
            raise ValueError("applied bundle revision is missing")
        catalog_id, assignments, inference_mode, resources, state_size = (
            self._catalog_assignments_from_set(
                session, deployment, assignment_set
            )
        )
        return (
            catalog_id,
            assignments,
            inference_mode,
            resources,
            state_size,
            bundle_revision,
        )

    @staticmethod
    def _current_catalog_assignments(
        session: Session, deployment: SolutionDeployment
    ) -> tuple[str, list[dict[str, Any]], str, dict[str, str], str]:
        """Reconstruct (catalog_id, assignments, inference_mode, resources,
        state_size) from the latest committed assignment set, so enrollment
        start/stop can replay it through preview_catalog_deployment/
        commit_catalog_deployment with one camera's apps swapped -- the same
        call shape a manual reconfigure through the UI uses."""
        assignment_set = session.scalar(
            select(DeploymentAssignmentSet)
            .where(DeploymentAssignmentSet.deployment_id == deployment.id)
            .order_by(DeploymentAssignmentSet.desired_revision.desc())
            .limit(1)
        )
        if assignment_set is None:
            raise ValueError("deployment has no committed assignments")
        return ManagementService._catalog_assignments_from_set(
            session, deployment, assignment_set
        )

    @staticmethod
    def _catalog_assignments_from_set(
        session: Session,
        deployment: SolutionDeployment,
        assignment_set: DeploymentAssignmentSet,
    ) -> tuple[str, list[dict[str, Any]], str, dict[str, str], str]:
        bundle_revision = session.get(SolutionBundleRevision, assignment_set.bundle_revision_id)
        if bundle_revision is None:
            raise ValueError("deployment assignment bundle is missing")
        configuration = bundle_revision.canonical_bundle.get("configuration", {})
        catalog_id = configuration.get("catalog_id")
        if not catalog_id:
            raise ValueError("enrollment requires a catalog-based deployment")
        camera_assignments = session.scalars(
            select(CameraDeploymentAssignment)
            .where(CameraDeploymentAssignment.assignment_set_id == assignment_set.id)
            .order_by(CameraDeploymentAssignment.ordinal)
        ).all()
        assignments: list[dict[str, Any]] = []
        for camera_assignment in camera_assignments:
            camera = session.get(Camera, camera_assignment.camera_id)
            if camera is None:
                raise ValueError("deployment assignment camera is missing")
            apps = session.scalars(
                select(CameraApplicationAssignment).where(
                    CameraApplicationAssignment.camera_assignment_id == camera_assignment.id
                )
            ).all()
            assignments.append({
                "camera_id": camera.camera_key,
                "apps": [item.use_case_key for item in apps],
                "fps": camera_assignment.requested_fps,
                "config": copy.deepcopy(apps[0].configuration) if apps else {},
            })
        application = bundle_revision.canonical_bundle["applications"][0]
        resources = {
            "cpu_request": application["resources"]["cpu"]["request"],
            "cpu_limit": application["resources"]["cpu"]["limit"],
            "memory_request": application["resources"]["memory"]["request"],
            "memory_limit": application["resources"]["memory"]["limit"],
        }
        state_size = next(
            (item["size"] for item in application.get("persistent_volumes", []) if item["name"] == "state"),
            "20Gi",
        )
        inference_mode = configuration.get("inference_mode", "cpu-compatible")
        return catalog_id, assignments, inference_mode, resources, state_size

    def start_enrollment(
        self,
        *,
        deployment_key: str,
        camera_id: str,
        duration_seconds: int | None,
        actor: str,
        request_id: str,
    ) -> dict[str, Any]:
        """Temporarily switch a face_recognition camera to face_enrollment.

        tvt-mills-pilot has no dedicated enrollment camera -- see
        docs/contracts/tvt-mills-v1/README.md. Reverts explicitly via
        stop_enrollment, or automatically once expires_at passes (see the
        caller of EnrollmentWindow sweep in tvt_edge/cli.py's retention job).
        """
        with self.sessions.begin() as session:
            deployment = self._deployment_for_update(session, deployment_key)
            camera = self._camera(session, camera_id)
            existing = session.scalar(
                select(EnrollmentWindow).where(
                    EnrollmentWindow.deployment_id == deployment.id,
                    EnrollmentWindow.camera_id == camera.id,
                    EnrollmentWindow.status == "active",
                )
            )
            if existing is not None:
                raise ValueError(f"camera {camera_id!r} already has an active enrollment window")
            catalog_id, assignments, inference_mode, resources, state_size = self._current_catalog_assignments(
                session, deployment
            )
            target = next((item for item in assignments if item["camera_id"] == camera_id), None)
            if target is None:
                raise ValueError(f"camera {camera_id!r} is not assigned to this deployment")
            if target["apps"] == ["face_enrollment"]:
                raise ValueError(f"camera {camera_id!r} is already in enrollment mode")
            if "face_recognition" not in target["apps"]:
                raise ValueError(f"camera {camera_id!r} does not run face_recognition")
            started_at = utc_now()
            expires_at = started_at + timedelta(seconds=duration_seconds) if duration_seconds else None
            window = EnrollmentWindow(
                deployment_id=deployment.id,
                camera_id=camera.id,
                prior_apps=target["apps"],
                prior_config=target["config"],
                prior_fps=target["fps"],
                started_at=started_at,
                expires_at=expires_at,
                status="active",
            )
            session.add(window)
            session.flush()
            window_id = str(window.id)

        new_assignments = [
            {**item, "apps": ["face_enrollment"], "config": {}} if item["camera_id"] == camera_id else item
            for item in assignments
        ]
        preview = self.preview_catalog_deployment(
            catalog_id=catalog_id, deployment_key=deployment_key, assignments=new_assignments,
            inference_mode=inference_mode, resources=resources, state_size=state_size, namespace="apexfabric",
        )
        self.commit_catalog_deployment(
            catalog_id=catalog_id, deployment_key=deployment_key, assignments=new_assignments,
            inference_mode=inference_mode, resources=resources, state_size=state_size, namespace="apexfabric",
            preview_bundle_sha256=preview["bundle_sha256"],
            idempotency_key=f"enrollment-start:{window_id}", actor=actor, request_id=request_id,
        )
        return {"window_id": window_id, "camera_id": camera_id, "expires_at": expires_at.isoformat() if expires_at else None}

    def stop_enrollment(
        self, *, deployment_key: str, window_id: str, actor: str, request_id: str
    ) -> dict[str, Any]:
        with self.sessions.begin() as session:
            deployment = self._deployment_for_update(session, deployment_key)
            window = session.get(EnrollmentWindow, uuid.UUID(window_id))
            if window is None or window.deployment_id != deployment.id or window.status != "active":
                raise ValueError(f"enrollment window {window_id!r} is not active")
            camera = session.get(Camera, window.camera_id)
            camera_key = camera.camera_key
            catalog_id, assignments, inference_mode, resources, state_size = self._current_catalog_assignments(
                session, deployment
            )
            window.status = "reverted"
            window.ended_at = utc_now()

        new_assignments = [
            {**item, "apps": window.prior_apps, "config": window.prior_config, "fps": window.prior_fps}
            if item["camera_id"] == camera_key else item
            for item in assignments
        ]
        preview = self.preview_catalog_deployment(
            catalog_id=catalog_id, deployment_key=deployment_key, assignments=new_assignments,
            inference_mode=inference_mode, resources=resources, state_size=state_size, namespace="apexfabric",
        )
        self.commit_catalog_deployment(
            catalog_id=catalog_id, deployment_key=deployment_key, assignments=new_assignments,
            inference_mode=inference_mode, resources=resources, state_size=state_size, namespace="apexfabric",
            preview_bundle_sha256=preview["bundle_sha256"],
            idempotency_key=f"enrollment-stop:{window_id}", actor=actor, request_id=request_id,
        )
        return {"window_id": window_id, "camera_id": camera_key}

    def list_enrollment_windows(self, deployment_key: str | None = None) -> list[dict[str, Any]]:
        with self.sessions() as session:
            query = select(EnrollmentWindow).order_by(EnrollmentWindow.started_at.desc())
            if deployment_key:
                deployment = session.scalar(
                    select(SolutionDeployment).where(
                        SolutionDeployment.deployment_key == deployment_key,
                        SolutionDeployment.deleted_at.is_(None),
                    )
                )
                if deployment is None:
                    return []
                query = query.where(EnrollmentWindow.deployment_id == deployment.id)
            windows = session.scalars(query).all()
            deployments = {item.id: item for item in {session.get(SolutionDeployment, w.deployment_id) for w in windows}}
            cameras = {item.id: item for item in {session.get(Camera, w.camera_id) for w in windows}}
            return [{
                "window_id": str(window.id),
                "deployment_key": deployments[window.deployment_id].deployment_key,
                "camera_id": cameras[window.camera_id].camera_key,
                "started_at": window.started_at.isoformat(),
                "expires_at": window.expires_at.isoformat() if window.expires_at else None,
                "ended_at": window.ended_at.isoformat() if window.ended_at else None,
                "status": window.status,
            } for window in windows]

    def sweep_expired_enrollment_windows(self, actor: str = "scheduler", request_id: str = "enrollment:sweep") -> list[dict[str, Any]]:
        with self.sessions() as session:
            expired = session.scalars(
                select(EnrollmentWindow).where(
                    EnrollmentWindow.status == "active",
                    EnrollmentWindow.expires_at.is_not(None),
                    EnrollmentWindow.expires_at < utc_now(),
                )
            ).all()
            reverts = [
                (session.get(SolutionDeployment, window.deployment_id).deployment_key, str(window.id))
                for window in expired
            ]
        results = []
        for deployment_key, window_id in reverts:
            try:
                results.append(self.stop_enrollment(
                    deployment_key=deployment_key, window_id=window_id, actor=actor, request_id=request_id,
                ))
            except Exception as error:
                results.append({"window_id": window_id, "error": str(error)})
        return results

    # ------------------------------------------------------------------
    # Enrollment sessions: durable state machine, superseding the bookkeeping
    # above for the operator-facing workflow. See tvt_edge/enrollment.py for
    # the state machine constants and the bounded background reconciler that
    # drives every transition below except start/cancel.
    # ------------------------------------------------------------------

    @staticmethod
    def _active_enrollment_session(
        session: Session, deployment_id: uuid.UUID, *, for_update: bool = False
    ) -> EnrollmentSession | None:
        query = select(EnrollmentSession).where(
            EnrollmentSession.deployment_id == deployment_id,
            EnrollmentSession.status.in_(ENROLLMENT_ACTIVE_STATUSES),
        )
        if for_update:
            query = query.with_for_update()
        return session.scalar(query)

    @staticmethod
    def _block_enrollment_conflicts(
        session: Session,
        deployment_id: uuid.UUID,
        assignments: list[dict[str, Any]],
        exempt_session_id: uuid.UUID | None,
    ) -> None:
        """Reject a generic assignment edit that would reassign a camera an
        active enrollment session currently owns (apps/config/fps other than
        the session's own face_enrollment target). Internal calls from the
        session's own start/restore path pass their session's id in
        `exempt_session_id` so they are never blocked by themselves; unrelated
        camera changes in the same payload are left untouched."""

        active = session.scalars(
            select(EnrollmentSession).where(
                EnrollmentSession.deployment_id == deployment_id,
                EnrollmentSession.status.in_(ENROLLMENT_ACTIVE_STATUSES),
            )
        ).all()
        for row in active:
            if row.id == exempt_session_id:
                continue
            camera = session.get(Camera, row.camera_id)
            if camera is None:
                continue
            item = next(
                (entry for entry in assignments if entry["camera_id"] == camera.camera_key), None
            )
            if item is not None and list(item.get("apps", [])) != ["face_enrollment"]:
                raise ValueError(
                    f"camera {camera.camera_key!r} is under an active enrollment session"
                )

    @staticmethod
    def _as_aware(value: datetime | None, reference: datetime) -> datetime | None:
        """SQLite drops timezone metadata even for timezone-aware columns;
        PostgreSQL preserves it. Normalize against `reference`'s tzinfo so
        session-window arithmetic stays portable between the two (see
        apply_retention's identical fixup for credential.superseded_at)."""

        if value is not None and value.tzinfo is None and reference.tzinfo is not None:
            return value.replace(tzinfo=reference.tzinfo)
        return value

    @staticmethod
    def _is_revision_applied(
        session: Session, deployment_id: uuid.UUID, revision: int | None
    ) -> bool:
        if revision is None:
            return False
        sync = session.get(DeploymentSyncState, deployment_id)
        if sync is None or sync.state != "applied" or sync.applied_assignment_set_id is None:
            return False
        applied_set = session.get(DeploymentAssignmentSet, sync.applied_assignment_set_id)
        return applied_set is not None and applied_set.desired_revision == revision

    def designate_enrollment_camera(
        self, *, deployment_key: str, camera_id: str, actor: str, request_id: str
    ) -> dict[str, Any]:
        with self.sessions.begin() as session:
            deployment = self._deployment_for_update(session, deployment_key)
            if self._active_enrollment_session(session, deployment.id, for_update=True) is not None:
                raise ValueError("cannot change the enrollment camera while a session is active")
            camera = self._camera(session, camera_id)
            _catalog_id, assignments, *_ = self._current_catalog_assignments(session, deployment)
            target = next((item for item in assignments if item["camera_id"] == camera_id), None)
            if target is None:
                raise ValueError(f"camera {camera_id!r} is not assigned to this deployment")
            if "face_recognition" not in target["apps"]:
                raise ValueError(f"camera {camera_id!r} does not run face_recognition")
            designation = session.scalar(
                select(EnrollmentCameraDesignation).where(
                    EnrollmentCameraDesignation.deployment_id == deployment.id
                )
            )
            if designation is None:
                designation = EnrollmentCameraDesignation(
                    deployment_id=deployment.id, camera_id=camera.id, updated_by=actor
                )
                session.add(designation)
            else:
                designation.camera_id = camera.id
                designation.updated_by = actor
                designation.row_version += 1
            self._audit(
                session,
                actor=actor,
                request_id=request_id,
                action="enrollment.camera.designate",
                target_type="deployment",
                target_id=deployment_key,
                details={"camera_id": camera_id},
            )
            return {"deployment_key": deployment_key, "camera_id": camera_id}

    def get_enrollment_designation(self, deployment_key: str) -> dict[str, Any] | None:
        with self.sessions() as session:
            deployment = session.scalar(
                select(SolutionDeployment).where(
                    SolutionDeployment.deployment_key == deployment_key,
                    SolutionDeployment.deleted_at.is_(None),
                )
            )
            if deployment is None:
                return None
            designation = session.scalar(
                select(EnrollmentCameraDesignation).where(
                    EnrollmentCameraDesignation.deployment_id == deployment.id
                )
            )
            if designation is None:
                return None
            camera = session.get(Camera, designation.camera_id)
            return {"deployment_key": deployment_key, "camera_id": camera.camera_key}

    def _apply_enrollment_target(
        self,
        *,
        deployment_key: str,
        camera_key: str,
        apps: list[str],
        config: dict[str, Any],
        fps: int,
        idempotency_key: str,
        session_id: uuid.UUID,
        actor: str,
        request_id: str,
    ) -> DeploymentAssignmentSet:
        with self.sessions() as session:
            deployment = session.scalar(
                select(SolutionDeployment).where(
                    SolutionDeployment.deployment_key == deployment_key,
                    SolutionDeployment.deleted_at.is_(None),
                )
            )
            if deployment is None:
                raise ValueError(f"unknown deployment {deployment_key!r}")
            catalog_id, assignments, inference_mode, resources, state_size = (
                self._current_catalog_assignments(session, deployment)
            )
        new_assignments = [
            {**item, "apps": list(apps), "config": copy.deepcopy(config), "fps": fps}
            if item["camera_id"] == camera_key
            else item
            for item in assignments
        ]
        preview = self.preview_catalog_deployment(
            catalog_id=catalog_id,
            deployment_key=deployment_key,
            assignments=new_assignments,
            inference_mode=inference_mode,
            resources=resources,
            state_size=state_size,
            namespace="apexfabric",
        )
        return self.commit_catalog_deployment(
            catalog_id=catalog_id,
            deployment_key=deployment_key,
            assignments=new_assignments,
            inference_mode=inference_mode,
            resources=resources,
            state_size=state_size,
            namespace="apexfabric",
            preview_bundle_sha256=preview["bundle_sha256"],
            idempotency_key=idempotency_key,
            actor=actor,
            request_id=request_id,
            _enrollment_session_id=session_id,
        )

    def start_enrollment_session(
        self,
        *,
        deployment_key: str,
        actor: str,
        request_id: str,
        capture_window_seconds: int | None = None,
    ) -> dict[str, Any]:
        window = capture_window_seconds or DEFAULT_CAPTURE_WINDOW_SECONDS
        if not MIN_CAPTURE_WINDOW_SECONDS <= window <= MAX_CAPTURE_WINDOW_SECONDS:
            raise ValueError(
                f"capture_window_seconds must be between {MIN_CAPTURE_WINDOW_SECONDS} "
                f"and {MAX_CAPTURE_WINDOW_SECONDS}"
            )
        with self.sessions.begin() as session:
            deployment = self._deployment_for_update(session, deployment_key)
            if self._active_enrollment_session(session, deployment.id, for_update=True) is not None:
                raise ValueError("an enrollment session is already active for this deployment")
            designation = session.scalar(
                select(EnrollmentCameraDesignation).where(
                    EnrollmentCameraDesignation.deployment_id == deployment.id
                )
            )
            if designation is None:
                raise ValueError("no enrollment camera has been designated for this deployment")
            camera = session.get(Camera, designation.camera_id)
            _catalog_id, assignments, *_ = self._current_catalog_assignments(session, deployment)
            target = next(
                (item for item in assignments if item["camera_id"] == camera.camera_key), None
            )
            if target is None:
                raise ValueError(f"camera {camera.camera_key!r} is no longer assigned to this deployment")
            if target["apps"] == ["face_enrollment"]:
                raise ValueError(f"camera {camera.camera_key!r} is already in enrollment mode")
            row = EnrollmentSession(
                deployment_id=deployment.id,
                camera_id=camera.id,
                prior_apps=target["apps"],
                prior_config=target["config"],
                prior_fps=target["fps"],
                capture_window_seconds=window,
                status="activating",
                naming_status="not_applicable",
                actor=actor,
                request_id=request_id,
            )
            session.add(row)
            session.flush()
            session_id = row.id
            camera_key = camera.camera_key
            fps = target["fps"]
            self._audit(
                session,
                actor=actor,
                request_id=request_id,
                action="enrollment.session.start",
                target_type="deployment",
                target_id=deployment_key,
                details={"camera_id": camera_key},
            )

        try:
            assignment_set = self._apply_enrollment_target(
                deployment_key=deployment_key,
                camera_key=camera_key,
                apps=["face_enrollment"],
                config={},
                fps=fps,
                idempotency_key=f"enrollment-activate:{session_id}",
                session_id=session_id,
                actor=actor,
                request_id=request_id,
            )
        except Exception:
            with self.sessions.begin() as session:
                row = session.get(EnrollmentSession, session_id)
                if row is not None and row.status == "activating":
                    row.status = "failed"
                    row.result_code = "activation_failed"
                    row.error_code = "ENROLLMENT_ACTIVATION_FAILED"
                    row.completed_at = utc_now()
            raise

        with self.sessions.begin() as session:
            row = session.get(EnrollmentSession, session_id)
            if row is not None and row.status == "activating":
                row.activation_assignment_set_id = assignment_set.id
                row.activation_revision = assignment_set.desired_revision
        return self._enrollment_session_view_by_id(session_id)

    def cancel_enrollment_session(
        self,
        *,
        deployment_key: str,
        session_id: str,
        actor: str,
        request_id: str,
        apex: Any | None = None,
    ) -> dict[str, Any]:
        """Stop an enrollment before its person is named. While capturing,
        this cancels the session; once a face is captured but not yet named,
        it discards the staged captures. Either way no person record is
        created. Idempotent for already-stopped or named sessions."""

        discard_ids: list[str] = []
        capture_scope: tuple[str, str, datetime, datetime] | None = None
        with self.sessions.begin() as session:
            deployment = self._deployment_for_update(session, deployment_key)
            row = session.get(EnrollmentSession, uuid.UUID(session_id), with_for_update=True)
            if row is None or row.deployment_id != deployment.id:
                raise ValueError(f"unknown enrollment session {session_id!r}")
            if row.status in ("activating", "capturing"):
                if row.status == "capturing" and row.activated_at and row.capture_deadline_at:
                    camera = session.get(Camera, row.camera_id)
                    capture_scope = (
                        self._runtime_workload_name(session, deployment),
                        camera.camera_key,
                        self._as_aware(row.activated_at, utc_now()),
                        self._as_aware(row.capture_deadline_at, utc_now()),
                    )
                row.status = "restoring"
                row.result_code = "cancelled"
                row.restoration_started_at = utc_now()
                action = "enrollment.session.cancel"
            elif row.naming_status == "pending_name":
                discard_ids = list(row.capture_event_ids or [])
                row.naming_status = "discarded"
                row.error_code = "ENROLLMENT_UNNAMED_DISCARDED"
                action = "enrollment.session.discard"
            else:
                # Already restoring/terminal/named: cancellation is idempotent.
                return self._enrollment_session_view(session, row)
            self._audit(
                session,
                actor=actor,
                request_id=request_id,
                action=action,
                target_type="deployment",
                target_id=deployment_key,
                details={"session_id": session_id},
            )
        if apex is not None and capture_scope is not None:
            discard_ids = self._staged_capture_ids(apex, *capture_scope)
        self._discard_staged_captures(apex, discard_ids)
        # The reconciler's next tick observes restoration_assignment_set_id
        # is still unset for this 'restoring' session and queues the restore
        # (see reconcile_enrollment_sessions) -- cancellation does not itself
        # depend on the browser staying open to finish restoring the camera.
        return self._enrollment_session_view_by_id(uuid.UUID(session_id))

    def _staged_capture_ids(
        self,
        apex: Any,
        runtime_workload: str,
        camera_key: str,
        window_start: datetime,
        window_end: datetime,
    ) -> list[str]:
        """Best effort: IDs of enrollment captures this session's window may
        have staged in apexfabric-control. Staged captures also expire there
        on their own, so a failure here never blocks the caller."""

        try:
            candidates = eligible_capture_candidates(
                apex.recent_events(runtime_workload),
                deployment_key=runtime_workload,
                camera_key=camera_key,
                window_start=window_start,
                window_end=window_end,
                minimum_sharpness=self.enrollment_minimum_sharpness,
            )
        except Exception:
            return []
        return [candidate.event_id for candidate in candidates]

    @staticmethod
    def _discard_staged_captures(apex: Any | None, capture_ids: list[str]) -> None:
        if apex is None or not capture_ids:
            return
        for start in range(0, len(capture_ids), 16):
            try:
                apex.discard_enrollment_captures(capture_ids[start:start + 16])
            except Exception:
                return  # apexfabric-control expires staged captures itself

    def get_enrollment_status(self, deployment_key: str) -> dict[str, Any]:
        with self.sessions() as session:
            deployment = session.scalar(
                select(SolutionDeployment).where(
                    SolutionDeployment.deployment_key == deployment_key,
                    SolutionDeployment.deleted_at.is_(None),
                )
            )
            if deployment is None:
                raise ValueError(f"unknown deployment {deployment_key!r}")
            designation = session.scalar(
                select(EnrollmentCameraDesignation).where(
                    EnrollmentCameraDesignation.deployment_id == deployment.id
                )
            )
            designated_camera = None
            if designation is not None:
                camera = session.get(Camera, designation.camera_id)
                designated_camera = camera.camera_key if camera else None
            row = session.scalar(
                select(EnrollmentSession)
                .where(EnrollmentSession.deployment_id == deployment.id)
                .order_by(EnrollmentSession.started_at.desc())
                .limit(1)
            )
            result: dict[str, Any] = {
                "deployment_key": deployment_key,
                "designated_camera_id": designated_camera,
                "session": None,
                "degraded": False,
            }
            if row is not None:
                result["session"] = self._enrollment_session_view(session, row)
                result["observer"] = self._enrollment_observer_view(
                    session, deployment, row
                )
                if row.status in ("activating", "restoring"):
                    sync = session.get(DeploymentSyncState, deployment.id)
                    if sync is not None and sync.state == "failed":
                        result["degraded"] = True
            return result

    def _enrollment_observer_view(
        self,
        session: Session,
        deployment: SolutionDeployment,
        row: EnrollmentSession,
    ) -> dict[str, Any]:
        """Build a safe, durable enrollment progress projection.

        All inputs are management metadata already stored in PostgreSQL. The
        projection intentionally excludes raw analytics payloads, snapshots,
        embeddings, camera endpoints, credentials, and person names.
        """

        runtime_workload = self._runtime_workload_name(session, deployment)
        sync = session.get(DeploymentSyncState, deployment.id)
        target_revision = (
            row.restoration_revision
            if row.restoration_revision is not None
            and row.status in ENROLLMENT_TERMINAL_STATUSES | {"restoring"}
            else row.activation_revision
        )
        attempt = None
        if target_revision is not None:
            attempt = session.scalar(
                select(DeploymentSyncAttempt)
                .where(
                    DeploymentSyncAttempt.deployment_id == deployment.id,
                    DeploymentSyncAttempt.desired_revision == target_revision,
                )
                .order_by(DeploymentSyncAttempt.attempt_number.desc())
                .limit(1)
            )

        applied_revision = None
        if sync is not None and sync.applied_assignment_set_id is not None:
            applied = session.get(
                DeploymentAssignmentSet, sync.applied_assignment_set_id
            )
            applied_revision = applied.desired_revision if applied is not None else None

        if row.status == "activating":
            stage = attempt.phase if attempt is not None else "sync_queued"
        elif row.status == "capturing":
            stage = (
                "capture_rejected"
                if row.error_code == "ENROLLMENT_CAPTURE_REJECTED"
                else "waiting_for_face"
            )
        elif row.status == "restoring":
            stage = attempt.phase if attempt is not None else "restore_queued"
        else:
            stage = row.status

        timeline: list[dict[str, str]] = []

        def add(stage_name: str, occurred_at: datetime | None) -> None:
            if occurred_at is not None:
                timeline.append(
                    {"stage": stage_name, "occurred_at": occurred_at.isoformat()}
                )

        add("requested", row.started_at)
        if attempt is not None:
            add("sync_claimed", attempt.started_at)
            add("sync_finished", attempt.finished_at)
        add("runtime_ready", row.activated_at)
        add("capture_accepted", row.captured_at)
        add("restore_queued", row.restoration_started_at)
        add("runtime_restored", row.restored_at)
        add(row.status, row.completed_at)
        timeline.sort(key=lambda item: item["occurred_at"])

        return {
            "stage": stage,
            "runtime_workload": runtime_workload,
            "target_revision": target_revision,
            "applied_revision": applied_revision,
            "sync_state": sync.state if sync is not None else "unavailable",
            "sync_attempt_status": attempt.status if attempt is not None else None,
            "safe_reason": row.error_code or (sync.last_error_code if sync else None),
            "timeline": timeline,
        }

    def list_enrollment_sessions(self, deployment_key: str, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 200))
        with self.sessions() as session:
            deployment = session.scalar(
                select(SolutionDeployment).where(
                    SolutionDeployment.deployment_key == deployment_key,
                    SolutionDeployment.deleted_at.is_(None),
                )
            )
            if deployment is None:
                return []
            rows = session.scalars(
                select(EnrollmentSession)
                .where(EnrollmentSession.deployment_id == deployment.id)
                .order_by(EnrollmentSession.started_at.desc())
                .limit(limit)
            ).all()
            return [self._enrollment_session_view(session, row) for row in rows]

    def _enrollment_session_view(self, session: Session, row: EnrollmentSession) -> dict[str, Any]:
        deployment = session.get(SolutionDeployment, row.deployment_id)
        camera = session.get(Camera, row.camera_id)
        return {
            "session_id": str(row.id),
            "deployment_key": deployment.deployment_key if deployment else None,
            "camera_id": camera.camera_key if camera else None,
            "status": row.status,
            "naming_status": row.naming_status,
            "capture_result": row.capture_result,
            "person_id": row.person_id,
            "capture_count": len(row.capture_event_ids or []),
            "naming_deadline_at": (
                (row.captured_at + timedelta(seconds=NAMING_TIMEOUT_SECONDS)).isoformat()
                if row.captured_at and row.naming_status == "pending_name"
                else None
            ),
            "result_code": row.result_code,
            "error_code": row.error_code,
            "capture_window_seconds": row.capture_window_seconds,
            "started_at": row.started_at.isoformat(),
            "activated_at": row.activated_at.isoformat() if row.activated_at else None,
            "capture_deadline_at": row.capture_deadline_at.isoformat() if row.capture_deadline_at else None,
            "captured_at": row.captured_at.isoformat() if row.captured_at else None,
            "restoration_started_at": (
                row.restoration_started_at.isoformat() if row.restoration_started_at else None
            ),
            "restored_at": row.restored_at.isoformat() if row.restored_at else None,
            "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        }

    def _enrollment_session_view_by_id(self, session_id: uuid.UUID) -> dict[str, Any]:
        with self.sessions() as session:
            row = session.get(EnrollmentSession, session_id)
            if row is None:
                raise ValueError("enrollment session disappeared")
            return self._enrollment_session_view(session, row)

    def list_people_awaiting_names(self, deployment_key: str | None = None) -> list[dict[str, Any]]:
        with self.sessions() as session:
            rows = session.scalars(
                select(EnrollmentSession)
                .where(
                    EnrollmentSession.capture_result == "created",
                    EnrollmentSession.naming_status == "pending_name",
                )
                .order_by(EnrollmentSession.captured_at.desc())
            ).all()
            results: list[dict[str, Any]] = []
            for row in rows:
                deployment = session.get(SolutionDeployment, row.deployment_id)
                if deployment is None:
                    continue
                if deployment_key and deployment.deployment_key != deployment_key:
                    continue
                camera = session.get(Camera, row.camera_id)
                results.append(
                    {
                        "session_id": str(row.id),
                        "person_id": row.person_id,
                        "deployment_key": deployment.deployment_key,
                        "camera_id": camera.camera_key if camera else None,
                        "captured_at": row.captured_at.isoformat() if row.captured_at else None,
                    }
                )
            return results

    def name_enrollment_session(
        self,
        *,
        deployment_key: str,
        session_id: str,
        display_name: str,
        apex: Any,
        actor: str,
        request_id: str,
    ) -> dict[str, Any]:
        """Create the person for a captured face. This is the only path that
        creates a person record; the display name goes to apexfabric-control
        and is never copied into the management database or its audit log."""

        name = display_name.strip()
        if not name or len(name) > 160:
            raise ValueError("display_name is required (maximum 160 characters)")
        with self.sessions() as session:
            row = session.get(EnrollmentSession, uuid.UUID(session_id))
            deployment = session.get(SolutionDeployment, row.deployment_id) if row else None
            if row is None or deployment is None or deployment.deployment_key != deployment_key:
                raise ValueError(f"unknown enrollment session {session_id!r}")
            if row.naming_status == "named":
                return self._enrollment_session_view(session, row)
            if row.naming_status == "discarded":
                raise ValueError(UNNAMED_DISCARDED_MESSAGE)
            if row.naming_status != "pending_name" or not row.capture_event_ids:
                raise ValueError("this enrollment has no captured face to name")
            capture_ids = list(row.capture_event_ids)
        try:
            person_id = apex.enroll_person(capture_ids, name)
        except ApexDuplicatePersonError as error:
            self._discard_unnamed_session(uuid.UUID(session_id), capture_result="duplicate")
            self._discard_staged_captures(apex, capture_ids)
            raise ValueError(f"{error}. {UNNAMED_DISCARDED_MESSAGE}") from error
        except ValueError as error:
            # Staged captures expired or were already consumed.
            self._discard_unnamed_session(uuid.UUID(session_id))
            raise ValueError(UNNAMED_DISCARDED_MESSAGE) from error
        with self.sessions.begin() as session:
            row = session.get(EnrollmentSession, uuid.UUID(session_id), with_for_update=True)
            if row is not None and row.naming_status == "pending_name":
                row.person_id = person_id
                row.naming_status = "named"
                row.error_code = None
                self._audit(
                    session,
                    actor=actor,
                    request_id=request_id,
                    action="enrollment.person.name",
                    target_type="person",
                    target_id=person_id,
                    details={},
                )
        return self._enrollment_session_view_by_id(uuid.UUID(session_id))

    def _discard_unnamed_session(
        self, session_id: uuid.UUID, *, capture_result: str | None = None
    ) -> list[str]:
        with self.sessions.begin() as session:
            row = session.get(EnrollmentSession, session_id, with_for_update=True)
            if row is None or row.naming_status != "pending_name":
                return []
            row.naming_status = "discarded"
            row.error_code = "ENROLLMENT_UNNAMED_DISCARDED"
            if capture_result:
                row.capture_result = capture_result
            return list(row.capture_event_ids or [])

    def _expire_unnamed_sessions(self, apex: Any, current_time: datetime) -> list[dict[str, Any]]:
        cutoff = current_time - timedelta(seconds=NAMING_TIMEOUT_SECONDS)
        with self.sessions() as session:
            rows = session.execute(
                select(EnrollmentSession.id, EnrollmentSession.captured_at).where(
                    EnrollmentSession.naming_status == "pending_name"
                )
            ).all()
        results: list[dict[str, Any]] = []
        for session_id, captured_at in rows:
            if captured_at is None or self._as_aware(captured_at, current_time) > cutoff:
                continue
            capture_ids = self._discard_unnamed_session(session_id)
            self._discard_staged_captures(apex, capture_ids)
            results.append(
                {
                    "session_id": str(session_id),
                    "transition": "discarded",
                    "error_code": "ENROLLMENT_UNNAMED_DISCARDED",
                }
            )
        return results

    def reconcile_enrollment_sessions(
        self, apex: Any, *, now: datetime | None = None
    ) -> list[dict[str, Any]]:
        """Advance every non-terminal enrollment session by one step. Called
        from tvt_edge.enrollment.EnrollmentReconciler on a short interval so
        interactive enrollment windows never depend on the once-daily
        retention timer. Returns a list of
        {"session_id", "transition", "error_code"} for the caller's
        observability; this method itself never logs or increments metrics."""

        current_time = now or utc_now()
        with self.sessions() as session:
            ids = list(
                session.scalars(
                    select(EnrollmentSession.id).where(
                        EnrollmentSession.status.in_(ENROLLMENT_ACTIVE_STATUSES)
                    )
                ).all()
            )
        results: list[dict[str, Any]] = []
        for session_id in ids:
            try:
                outcome = self._reconcile_one_enrollment_session(session_id, apex, current_time)
            except Exception as error:
                outcome = {
                    "session_id": str(session_id),
                    "transition": None,
                    "error_code": "INTERNAL_ERROR",
                    "detail": redact_text(str(error)),
                }
            if outcome is not None:
                results.append(outcome)
        try:
            results.extend(self._expire_unnamed_sessions(apex, current_time))
        except Exception as error:
            results.append(
                {
                    "session_id": None,
                    "transition": None,
                    "error_code": "INTERNAL_ERROR",
                    "detail": redact_text(str(error)),
                }
            )
        return results

    def _reconcile_one_enrollment_session(
        self, session_id: uuid.UUID, apex: Any, current_time: datetime
    ) -> dict[str, Any] | None:
        with self.sessions.begin() as session:
            row = session.get(EnrollmentSession, session_id, with_for_update=True)
            if row is None or row.status not in ENROLLMENT_ACTIVE_STATUSES:
                return None
            deployment = session.get(SolutionDeployment, row.deployment_id)
            camera = session.get(Camera, row.camera_id)
            deployment_key = deployment.deployment_key
            runtime_workload = self._runtime_workload_name(session, deployment)
            camera_key = camera.camera_key
            status = row.status
            transition: str | None = None
            error_code: str | None = None
            pending: tuple[str, tuple[Any, ...]] | None = None

            if status == "activating":
                if row.activation_revision is not None and self._is_revision_applied(
                    session, deployment.id, row.activation_revision
                ):
                    row.status = "capturing"
                    row.activated_at = current_time
                    row.capture_deadline_at = current_time + timedelta(
                        seconds=row.capture_window_seconds
                    )
                    transition = "capturing"
                elif current_time - self._as_aware(row.started_at, current_time) > timedelta(
                    seconds=DEFAULT_ACTIVATION_TIMEOUT_SECONDS
                ):
                    row.status = "restoring"
                    row.result_code = "activation_failed"
                    row.error_code = error_code = "ENROLLMENT_ACTIVATION_FAILED"
                    row.restoration_started_at = current_time
                    transition = "restoring"
                    pending = (
                        "restore",
                        (row.prior_apps, row.prior_config, row.prior_fps, row.actor, row.request_id),
                    )
            elif status == "capturing":
                # The deadline is enforced inside _poll_enrollment_capture so
                # faces captured just before it are still collected.
                pending = (
                    "poll",
                    (
                        self._as_aware(row.activated_at, current_time),
                        self._as_aware(row.capture_deadline_at, current_time),
                        row.actor,
                        row.request_id,
                    ),
                )
            elif status == "restoring":
                if row.restoration_assignment_set_id is not None:
                    if self._is_revision_applied(session, deployment.id, row.restoration_revision):
                        if row.result_code is None:
                            row.result_code = "ok"
                        row.status = RESULT_CODE_TERMINAL_STATUS[row.result_code]
                        row.restored_at = current_time
                        row.completed_at = current_time
                        transition = row.status
                else:
                    pending = (
                        "restore",
                        (row.prior_apps, row.prior_config, row.prior_fps, row.actor, row.request_id),
                    )

        if pending is not None:
            kind, payload = pending
            if kind == "restore":
                apps, config, fps, actor, request_id = payload
                try:
                    assignment_set = self._apply_enrollment_target(
                        deployment_key=deployment_key,
                        camera_key=camera_key,
                        apps=apps,
                        config=config,
                        fps=fps,
                        idempotency_key=f"enrollment-restore:{session_id}",
                        session_id=session_id,
                        actor=actor,
                        request_id=request_id,
                    )
                except Exception:
                    # K3s/queue failure while queuing the restore commit: the
                    # session stays 'restoring' (restoration_assignment_set_id
                    # unset) and the next tick retries -- surfaced as
                    # 'degraded' by get_enrollment_status in the meantime.
                    return {
                        "session_id": str(session_id),
                        "transition": None,
                        "error_code": "ENROLLMENT_RESTORE_DEGRADED",
                    }
                with self.sessions.begin() as session:
                    row = session.get(EnrollmentSession, session_id)
                    if row is not None and row.status == "restoring":
                        row.restoration_assignment_set_id = assignment_set.id
                        row.restoration_revision = assignment_set.desired_revision
                if transition is None:
                    transition = "restoring"
            elif kind == "poll":
                activated_at, deadline, actor, request_id = payload
                capture_outcome = self._poll_enrollment_capture(
                    session_id,
                    apex,
                    deployment_key,
                    runtime_workload,
                    camera_key,
                    activated_at,
                    deadline,
                    actor,
                    request_id,
                    current_time,
                )
                if capture_outcome is not None:
                    return capture_outcome

        if transition is None:
            return None
        return {"session_id": str(session_id), "transition": transition, "error_code": error_code}

    def _poll_enrollment_capture(
        self,
        session_id: uuid.UUID,
        apex: Any,
        deployment_key: str,
        runtime_workload: str,
        camera_key: str,
        activated_at: datetime,
        deadline: datetime,
        actor: str,
        request_id: str,
        current_time: datetime,
    ) -> dict[str, Any] | None:
        """Collect staged enrollment captures for a 'capturing' session.

        Finishes capturing once MAX_ENROLLMENT_CAPTURES are staged,
        CAPTURE_SETTLE_SECONDS after the first one, or at the deadline. A
        capture that already matches a named person ends the session as
        'duplicate' and is discarded. No person is created here: a captured
        session waits in naming_status 'pending_name' for
        name_enrollment_session."""

        deadline_passed = current_time >= deadline
        try:
            events = apex.recent_events(runtime_workload)
            candidates = eligible_capture_candidates(
                events,
                deployment_key=runtime_workload,
                camera_key=camera_key,
                window_start=activated_at,
                window_end=deadline,
                minimum_sharpness=self.enrollment_minimum_sharpness,
            )
            staged = (
                apex.enrollment_captures(
                    [candidate.event_id for candidate in candidates[:MAX_ENROLLMENT_CAPTURES]]
                )
                if candidates
                else []
            )
        except Exception:
            # apex unreachable this tick: keep capturing unless the window is over.
            if deadline_passed:
                return self._time_out_enrollment_capture(
                    session_id, deployment_key, camera_key, current_time
                )
            return None
        staged_ids = {item.get("capture_id") for item in staged}
        accepted = [candidate for candidate in candidates if candidate.event_id in staged_ids]

        if not accepted:
            if deadline_passed:
                return self._time_out_enrollment_capture(
                    session_id, deployment_key, camera_key, current_time
                )
            if has_rejected_capture_attempt(
                events, deployment_key=runtime_workload, camera_key=camera_key
            ):
                with self.sessions.begin() as session:
                    row = session.get(EnrollmentSession, session_id)
                    if row is not None and row.status == "capturing":
                        row.error_code = "ENROLLMENT_CAPTURE_REJECTED"
                return {
                    "session_id": str(session_id),
                    "transition": None,
                    "error_code": "ENROLLMENT_CAPTURE_REJECTED",
                }
            return None

        settled = current_time - accepted[0].occurred_at >= timedelta(seconds=CAPTURE_SETTLE_SECONDS)
        if len(accepted) < MAX_ENROLLMENT_CAPTURES and not settled and not deadline_passed:
            return None

        capture_ids = [candidate.event_id for candidate in accepted]
        duplicate = next((item for item in staged if item.get("matched_person_id")), None)
        capture_result = "duplicate" if duplicate else "created"

        with self.sessions.begin() as session:
            row = session.get(EnrollmentSession, session_id, with_for_update=True)
            if row is None or row.status != "capturing":
                return None
            row.accepted_event_id = capture_ids[0]
            row.capture_event_ids = None if duplicate else capture_ids
            row.error_code = None
            row.capture_result = capture_result
            row.person_id = duplicate.get("matched_person_id") if duplicate else None
            row.naming_status = "not_applicable" if duplicate else "pending_name"
            row.captured_at = current_time
            row.status = "restoring"
            row.restoration_started_at = current_time
            prior_apps, prior_config, prior_fps = row.prior_apps, row.prior_config, row.prior_fps
        if duplicate:
            self._discard_staged_captures(apex, capture_ids)

        return self._queue_enrollment_restore(
            session_id, deployment_key, camera_key, prior_apps, prior_config, prior_fps,
            actor, request_id, error_code=None,
        )

    def _time_out_enrollment_capture(
        self, session_id: uuid.UUID, deployment_key: str, camera_key: str, current_time: datetime
    ) -> dict[str, Any] | None:
        with self.sessions.begin() as session:
            row = session.get(EnrollmentSession, session_id, with_for_update=True)
            if row is None or row.status != "capturing":
                return None
            row.status = "restoring"
            row.result_code = "timed_out"
            row.error_code = "ENROLLMENT_TIMEOUT"
            row.restoration_started_at = current_time
            restore = (row.prior_apps, row.prior_config, row.prior_fps, row.actor, row.request_id)
        return self._queue_enrollment_restore(
            session_id, deployment_key, camera_key, *restore, error_code="ENROLLMENT_TIMEOUT",
        )

    def _queue_enrollment_restore(
        self,
        session_id: uuid.UUID,
        deployment_key: str,
        camera_key: str,
        apps: list[str],
        config: dict[str, Any],
        fps: int,
        actor: str,
        request_id: str,
        *,
        error_code: str | None,
    ) -> dict[str, Any]:
        try:
            assignment_set = self._apply_enrollment_target(
                deployment_key=deployment_key,
                camera_key=camera_key,
                apps=apps,
                config=config,
                fps=fps,
                idempotency_key=f"enrollment-restore:{session_id}",
                session_id=session_id,
                actor=actor,
                request_id=request_id,
            )
        except Exception:
            # The session stays 'restoring' with restoration_assignment_set_id
            # unset; the next reconcile tick retries the restore.
            return {
                "session_id": str(session_id),
                "transition": "restoring",
                "error_code": "ENROLLMENT_RESTORE_DEGRADED",
            }
        with self.sessions.begin() as session:
            row = session.get(EnrollmentSession, session_id)
            if row is not None and row.status == "restoring":
                row.restoration_assignment_set_id = assignment_set.id
                row.restoration_revision = assignment_set.desired_revision
        return {"session_id": str(session_id), "transition": "restoring", "error_code": error_code}

    @staticmethod
    def _store_bundle_revision(
        session: Session,
        deployment: SolutionDeployment,
        bundle: dict[str, Any],
        action: str,
        actor: str,
    ) -> SolutionBundleRevision:
        digest = bundle_sha256(bundle)
        existing = session.scalar(
            select(SolutionBundleRevision).where(
                SolutionBundleRevision.deployment_id == deployment.id,
                SolutionBundleRevision.bundle_sha256 == digest,
            )
        )
        if existing is not None:
            return existing
        value = SolutionBundleRevision(
            deployment_id=deployment.id,
            bundle_sha256=digest,
            canonical_bundle=canonical_bundle(bundle),
            action=action,
            actor=actor,
        )
        session.add(value)
        session.flush()
        return value

    def commit_assignments(
        self,
        deployment_key: str,
        assignments: list[dict[str, Any]],
        actor: str,
        request_id: str,
        idempotency_key: str,
    ) -> DeploymentAssignmentSet:
        with self.sessions.begin() as session:
            deployment = self._deployment_for_update(session, deployment_key)
            existing = session.scalar(
                select(DeploymentAssignmentSet).where(
                    DeploymentAssignmentSet.deployment_id == deployment.id,
                    DeploymentAssignmentSet.idempotency_key == idempotency_key,
                )
            )
            if existing is not None:
                return existing
            template = session.scalar(
                select(SolutionBundleRevision)
                .where(SolutionBundleRevision.deployment_id == deployment.id)
                .order_by(SolutionBundleRevision.created_at.desc())
                .limit(1)
            )
            if template is None:
                raise ValueError("deployment has no registered bundle")
            if template.canonical_bundle.get("configuration", {}).get("catalog_id"):
                raise ValueError(
                    "catalog deployment changes require /deployments/preview"
                )
            site = session.get(Site, deployment.site_id)
            if site is None:
                raise ValueError("deployment site is missing")
            resolved: list[tuple[Camera, CameraStreamProfile, CameraCredentialVersion | None, dict[str, Any]]] = []
            bundle_cameras: list[BundleCamera] = []
            for position, item in enumerate(assignments):
                camera = self._camera(session, item["camera_id"])
                if camera.site_id != site.id or not camera.enabled:
                    raise ValueError(f"camera {camera.camera_key!r} is not enabled")
                profile = session.scalar(
                    select(CameraStreamProfile).where(
                        CameraStreamProfile.camera_id == camera.id,
                        CameraStreamProfile.selected.is_(True),
                    )
                )
                if profile is None:
                    raise ValueError(f"camera {camera.camera_key!r} has no selected profile")
                credential = self._active_credential(session, camera.id)
                apps = tuple(item["apps"])
                fps = int(item.get("fps", 8))
                bundle_cameras.append(BundleCamera(camera.camera_key, fps, apps))
                resolved.append((camera, profile, credential, {**item, "ordinal": position}))
            bundle = instantiate_traffic_bundle(
                template.canonical_bundle, site.edge_id, bundle_cameras
            )
            bundle_revision = self._store_bundle_revision(
                session, deployment, bundle, "assignment_commit", actor
            )
            assignment_set = DeploymentAssignmentSet(
                deployment_id=deployment.id,
                bundle_revision_id=bundle_revision.id,
                desired_revision=deployment.next_desired_revision,
                idempotency_key=idempotency_key,
                actor=actor,
            )
            deployment.next_desired_revision += 1
            session.add(assignment_set)
            session.flush()
            for camera, profile, credential, item in resolved:
                camera_assignment = CameraDeploymentAssignment(
                    assignment_set_id=assignment_set.id,
                    camera_id=camera.id,
                    stream_profile_id=profile.id,
                    credential_version_id=credential.id if credential else None,
                    ordinal=item["ordinal"],
                    requested_fps=int(item.get("fps", 8)),
                    geometry_revision=camera.geometry_revision,
                )
                session.add(camera_assignment)
                session.flush()
                for use_case in item["apps"]:
                    session.add(
                        CameraApplicationAssignment(
                            camera_assignment_id=camera_assignment.id,
                            bundle_application=item.get("bundle_application", "runtime"),
                            use_case_key=use_case,
                        )
                    )
            self._set_desired(session, deployment.id, assignment_set.id)
            self._audit(
                session,
                actor=actor,
                request_id=request_id,
                action="deployment.assignments.commit",
                target_type="deployment",
                target_id=deployment_key,
                details={
                    "desired_revision": assignment_set.desired_revision,
                    "camera_ids": [item.camera_key for item in bundle_cameras],
                },
            )
            return assignment_set

    @staticmethod
    def _deployment_for_update(session: Session, deployment_key: str) -> SolutionDeployment:
        deployment = session.scalar(
            select(SolutionDeployment)
            .where(
                SolutionDeployment.deployment_key == deployment_key,
                SolutionDeployment.deleted_at.is_(None),
            )
            .with_for_update()
        )
        if deployment is None:
            raise ValueError(f"unknown deployment {deployment_key!r}")
        return deployment

    @staticmethod
    def _set_desired(
        session: Session, deployment_id: uuid.UUID, assignment_set_id: uuid.UUID
    ) -> None:
        sync = session.get(DeploymentSyncState, deployment_id)
        if sync is None:
            session.add(
                DeploymentSyncState(
                    deployment_id=deployment_id,
                    desired_assignment_set_id=assignment_set_id,
                    state="pending",
                )
            )
        else:
            sync.desired_assignment_set_id = assignment_set_id
            sync.state = "pending"
            sync.next_attempt_at = None
            sync.last_error_code = None

    def _requeue_credential_consumers(
        self,
        session: Session,
        camera_id: uuid.UUID,
        credential_id: uuid.UUID,
        actor: str,
        request_id: str,
    ) -> None:
        states = session.scalars(select(DeploymentSyncState)).all()
        for sync in states:
            source = session.get(DeploymentAssignmentSet, sync.desired_assignment_set_id)
            if source is None:
                continue
            consumes = session.scalar(
                select(CameraDeploymentAssignment.id).where(
                    CameraDeploymentAssignment.assignment_set_id == source.id,
                    CameraDeploymentAssignment.camera_id == camera_id,
                )
            )
            if consumes is None:
                continue
            deployment = session.get(SolutionDeployment, sync.deployment_id)
            if deployment is None:
                continue
            self._clone_assignment_set(
                session,
                deployment,
                source,
                source.bundle_revision_id,
                actor,
                f"credential:{request_id}:{deployment.deployment_key}",
                {camera_id: credential_id},
            )

    def _clone_assignment_set(
        self,
        session: Session,
        deployment: SolutionDeployment,
        source: DeploymentAssignmentSet,
        bundle_revision_id: uuid.UUID,
        actor: str,
        idempotency_key: str,
        credential_overrides: dict[uuid.UUID, uuid.UUID | None] | None = None,
        configuration_overrides: dict[uuid.UUID, dict[str, Any]] | None = None,
        geometry_revision_overrides: dict[uuid.UUID, int] | None = None,
    ) -> DeploymentAssignmentSet:
        clone = DeploymentAssignmentSet(
            deployment_id=deployment.id,
            bundle_revision_id=bundle_revision_id,
            desired_revision=deployment.next_desired_revision,
            idempotency_key=idempotency_key,
            actor=actor,
        )
        deployment.next_desired_revision += 1
        session.add(clone)
        session.flush()
        assignments = session.scalars(
            select(CameraDeploymentAssignment)
            .where(CameraDeploymentAssignment.assignment_set_id == source.id)
            .order_by(CameraDeploymentAssignment.ordinal)
        ).all()
        for original in assignments:
            credential_id = (credential_overrides or {}).get(
                original.camera_id, original.credential_version_id
            )
            configuration = (configuration_overrides or {}).get(original.camera_id)
            geometry_revision = (geometry_revision_overrides or {}).get(
                original.camera_id, original.geometry_revision
            )
            copied = CameraDeploymentAssignment(
                assignment_set_id=clone.id,
                camera_id=original.camera_id,
                stream_profile_id=original.stream_profile_id,
                credential_version_id=credential_id,
                ordinal=original.ordinal,
                requested_fps=original.requested_fps,
                geometry_revision=geometry_revision,
            )
            session.add(copied)
            session.flush()
            apps = session.scalars(
                select(CameraApplicationAssignment).where(
                    CameraApplicationAssignment.camera_assignment_id == original.id
                )
            ).all()
            for app in apps:
                session.add(
                    CameraApplicationAssignment(
                        camera_assignment_id=copied.id,
                        bundle_application=app.bundle_application,
                        use_case_key=app.use_case_key,
                        configuration=copy.deepcopy(
                            configuration if configuration is not None else app.configuration
                        ),
                    )
                )
        self._set_desired(session, deployment.id, clone.id)
        return clone

    def set_lifecycle(
        self,
        deployment_key: str,
        desired_state: str,
        actor: str,
        request_id: str,
    ) -> DeploymentAssignmentSet:
        if desired_state not in {"Running", "Stopped"}:
            raise ValueError("desired_state must be Running or Stopped")
        with self.sessions.begin() as session:
            deployment = self._deployment_for_update(session, deployment_key)
            sync = session.get(DeploymentSyncState, deployment.id)
            if sync is None:
                raise ValueError("deployment has no committed assignments")
            source = session.get(DeploymentAssignmentSet, sync.desired_assignment_set_id)
            if source is None:
                raise ValueError("desired assignment set is missing")
            current_bundle = session.get(SolutionBundleRevision, source.bundle_revision_id)
            if current_bundle is None:
                raise ValueError("desired bundle revision is missing")
            bundle = copy.deepcopy(current_bundle.canonical_bundle)
            for application in bundle["applications"]:
                application.setdefault("lifecycle", {})["desired_state"] = desired_state
            validate_tvt_bundle(bundle)
            revision = self._store_bundle_revision(
                session, deployment, bundle, desired_state.lower(), actor
            )
            deployment.lifecycle_intent = desired_state
            clone = self._clone_assignment_set(
                session,
                deployment,
                source,
                revision.id,
                actor,
                f"lifecycle:{request_id}",
            )
            self._audit(
                session,
                actor=actor,
                request_id=request_id,
                action=f"deployment.{desired_state.lower()}",
                target_type="deployment",
                target_id=deployment_key,
                details={"desired_revision": clone.desired_revision},
            )
            return clone

    def rollback(
        self,
        deployment_key: str,
        target_bundle_sha256: str,
        actor: str,
        request_id: str,
    ) -> DeploymentAssignmentSet:
        with self.sessions.begin() as session:
            deployment = self._deployment_for_update(session, deployment_key)
            target = session.scalar(
                select(SolutionBundleRevision).where(
                    SolutionBundleRevision.deployment_id == deployment.id,
                    SolutionBundleRevision.bundle_sha256 == target_bundle_sha256,
                )
            )
            if target is None:
                raise ValueError("unknown target bundle revision")
            source = session.scalar(
                select(DeploymentAssignmentSet)
                .where(DeploymentAssignmentSet.bundle_revision_id == target.id)
                .order_by(DeploymentAssignmentSet.desired_revision.desc())
                .limit(1)
            )
            if source is None:
                raise ValueError("target bundle has no assignment snapshot")
            overrides: dict[uuid.UUID, uuid.UUID | None] = {}
            assignments = session.scalars(
                select(CameraDeploymentAssignment).where(
                    CameraDeploymentAssignment.assignment_set_id == source.id
                )
            ).all()
            for assignment in assignments:
                camera = session.get(Camera, assignment.camera_id)
                if camera is None or camera.deleted_at is not None or not camera.enabled:
                    raise ValueError("rollback camera is unavailable")
                current = self._active_credential(session, camera.id)
                if assignment.credential_version_id is not None and current is None:
                    raise ValueError(
                        f"camera {camera.camera_key!r} requires replacement credentials"
                    )
                overrides[camera.id] = current.id if current else None
            clone = self._clone_assignment_set(
                session,
                deployment,
                source,
                target.id,
                actor,
                f"rollback:{request_id}",
                overrides,
            )
            self._audit(
                session,
                actor=actor,
                request_id=request_id,
                action="deployment.rollback",
                target_type="deployment",
                target_id=deployment_key,
                details={
                    "target_bundle_sha256": target_bundle_sha256,
                    "desired_revision": clone.desired_revision,
                },
            )
            return clone

    def list_deployments(self) -> list[dict[str, Any]]:
        with self.sessions() as session:
            deployments = session.scalars(
                select(SolutionDeployment)
                .where(SolutionDeployment.deleted_at.is_(None))
                .order_by(SolutionDeployment.deployment_key)
                .limit(100)
            ).all()
            result = []
            for deployment in deployments:
                sync = session.get(DeploymentSyncState, deployment.id)
                desired = (
                    session.get(DeploymentAssignmentSet, sync.desired_assignment_set_id)
                    if sync
                    else None
                )
                applied = (
                    session.get(DeploymentAssignmentSet, sync.applied_assignment_set_id)
                    if sync and sync.applied_assignment_set_id
                    else None
                )
                desired_bundle = (
                    session.get(SolutionBundleRevision, desired.bundle_revision_id)
                    if desired else None
                )
                applied_bundle = (
                    session.get(SolutionBundleRevision, applied.bundle_revision_id)
                    if applied else None
                )
                snapshots = session.scalars(
                    select(DeploymentAssignmentSet)
                    .where(DeploymentAssignmentSet.deployment_id == deployment.id)
                    .order_by(DeploymentAssignmentSet.desired_revision.desc())
                    .limit(20)
                ).all()
                history = []
                seen_bundle_ids: set[uuid.UUID] = set()
                for snapshot in snapshots:
                    if snapshot.bundle_revision_id in seen_bundle_ids:
                        continue
                    revision = session.get(
                        SolutionBundleRevision, snapshot.bundle_revision_id
                    )
                    if revision is None:
                        continue
                    seen_bundle_ids.add(snapshot.bundle_revision_id)
                    image = revision.canonical_bundle["applications"][0]["image"]
                    history.append({
                        "bundle_sha256": revision.bundle_sha256,
                        "desired_revision": snapshot.desired_revision,
                        "image_digest": image.get("digest"),
                        "created_at": revision.created_at.isoformat(),
                    })
                result.append(
                    {
                        "deployment_id": deployment.deployment_key,
                        "solution_id": deployment.solution_id,
                        "namespace": deployment.namespace,
                        "lifecycle_intent": deployment.lifecycle_intent,
                        "sync_state": sync.state if sync else "unconfigured",
                        "desired_revision": desired.desired_revision if desired else None,
                        "applied_revision": applied.desired_revision if applied else None,
                        "last_error_code": sync.last_error_code if sync else None,
                        "catalog_id": (
                            desired_bundle.canonical_bundle.get("configuration", {}).get("catalog_id")
                            if desired_bundle else None
                        ),
                        "desired_bundle_sha256": (
                            desired_bundle.bundle_sha256 if desired_bundle else None
                        ),
                        "applied_bundle_sha256": (
                            applied_bundle.bundle_sha256 if applied_bundle else None
                        ),
                        "applied_image_digest": (
                            applied_bundle.canonical_bundle["applications"][0]["image"].get("digest")
                            if applied_bundle else None
                        ),
                        "bundle_history": history,
                    }
                )
            return result

    def delete_camera(
        self, camera_key: str, actor: str, request_id: str
    ) -> None:
        with self.sessions.begin() as session:
            camera = self._camera(session, camera_key)
            live_sets = select(
                DeploymentSyncState.desired_assignment_set_id
            ).union(
                select(DeploymentSyncState.applied_assignment_set_id).where(
                    DeploymentSyncState.applied_assignment_set_id.is_not(None)
                )
            )
            in_use = session.scalar(
                select(CameraDeploymentAssignment.id).where(
                    CameraDeploymentAssignment.camera_id == camera.id,
                    CameraDeploymentAssignment.assignment_set_id.in_(live_sets),
                )
            )
            if in_use is not None:
                raise ValueError(
                    "camera remains in a desired or applied deployment revision"
                )
            credentials = session.scalars(
                select(CameraCredentialVersion).where(
                    CameraCredentialVersion.camera_id == camera.id,
                    CameraCredentialVersion.destroyed_at.is_(None),
                )
            ).all()
            now = utc_now()
            for credential in credentials:
                credential.ciphertext = None
                credential.nonce = None
                credential.state = "revoked"
                credential.destroyed_at = now
            camera.enabled = False
            camera.deleted_at = now
            self._audit(
                session,
                actor=actor,
                request_id=request_id,
                action="camera.delete",
                target_type="camera",
                target_id=camera_key,
            )

    def clear_credentials(
        self, camera_key: str, actor: str, request_id: str
    ) -> None:
        with self.sessions.begin() as session:
            camera = self._camera(session, camera_key)
            live_sets = select(
                DeploymentSyncState.desired_assignment_set_id
            ).union(
                select(DeploymentSyncState.applied_assignment_set_id).where(
                    DeploymentSyncState.applied_assignment_set_id.is_not(None)
                )
            )
            in_use = session.scalar(
                select(CameraDeploymentAssignment.id).where(
                    CameraDeploymentAssignment.camera_id == camera.id,
                    CameraDeploymentAssignment.assignment_set_id.in_(live_sets),
                )
            )
            if in_use is not None:
                raise ValueError(
                    "credentials remain referenced by desired or applied deployment state"
                )
            credentials = session.scalars(
                select(CameraCredentialVersion).where(
                    CameraCredentialVersion.camera_id == camera.id,
                    CameraCredentialVersion.destroyed_at.is_(None),
                )
            ).all()
            now = utc_now()
            for credential in credentials:
                credential.ciphertext = None
                credential.nonce = None
                credential.state = "revoked"
                credential.destroyed_at = now
            camera.enabled = False
            self._audit(
                session,
                actor=actor,
                request_id=request_id,
                action="camera.credentials.destroy",
                target_type="camera",
                target_id=camera_key,
            )

    def apply_retention(self, now=None) -> dict[str, int]:
        """Apply bounded history retention without deleting referenced state."""

        now = now or utc_now()
        cutoffs = {
            "sync": now - timedelta(days=180),
            "audit": now - timedelta(days=365),
            "bundles": now - timedelta(days=365),
        }
        counts: dict[str, int] = {}
        with self.sessions.begin() as session:
            old_attempt_ids = select(DeploymentSyncAttempt.id).where(
                DeploymentSyncAttempt.started_at < cutoffs["sync"]
            )
            counts["kubernetes_resource_refs"] = session.query(
                KubernetesResourceRef
            ).filter(
                KubernetesResourceRef.sync_attempt_id.in_(old_attempt_ids)
            ).delete(synchronize_session=False)
            counts["sync_attempts"] = session.query(DeploymentSyncAttempt).filter(
                DeploymentSyncAttempt.started_at < cutoffs["sync"]
            ).delete(synchronize_session=False)
            counts["audit_events"] = session.query(AuditEvent).filter(
                AuditEvent.created_at < cutoffs["audit"]
            ).delete(synchronize_session=False)
            counts["operations"] = session.query(ManagementOperation).filter(
                ManagementOperation.created_at < cutoffs["audit"],
                ~ManagementOperation.id.in_(select(DeploymentSyncAttempt.operation_id)),
                ~ManagementOperation.id.in_(
                    select(AuditEvent.operation_id).where(AuditEvent.operation_id.is_not(None))
                ),
            ).delete(synchronize_session=False)
            # A session awaiting a name is never pruned (that would silently
            # drop it from list_people_awaiting_names -- the operator would
            # never learn the capture happened at all).
            counts["enrollment_sessions"] = session.query(EnrollmentSession).filter(
                EnrollmentSession.status.in_(ENROLLMENT_TERMINAL_STATUSES),
                EnrollmentSession.naming_status != "pending_name",
                EnrollmentSession.started_at < cutoffs["audit"],
            ).delete(synchronize_session=False)
            credentials = session.scalars(
                select(CameraCredentialVersion).where(
                    CameraCredentialVersion.state == "superseded",
                    CameraCredentialVersion.destroyed_at.is_(None),
                    CameraCredentialVersion.purge_after <= now,
                )
            ).all()
            live_assignment_set_ids: set[uuid.UUID] = set()
            for sync_state in session.scalars(select(DeploymentSyncState)).all():
                live_assignment_set_ids.add(sync_state.desired_assignment_set_id)
                if sync_state.applied_assignment_set_id is not None:
                    live_assignment_set_ids.add(sync_state.applied_assignment_set_id)
            destroyed_credentials = 0
            for credential in credentials:
                still_live = session.scalar(
                    select(CameraDeploymentAssignment.id).where(
                        CameraDeploymentAssignment.credential_version_id
                        == credential.id,
                        CameraDeploymentAssignment.assignment_set_id.in_(
                            live_assignment_set_ids
                        ),
                    )
                )
                superseded_at = credential.superseded_at
                # SQLite drops timezone metadata even for timezone-aware
                # columns; PostgreSQL preserves it. Keep the repository layer
                # portable for unit tests and the one-shot migration tooling.
                if (
                    superseded_at is not None
                    and superseded_at.tzinfo is None
                    and now.tzinfo is not None
                ):
                    superseded_at = superseded_at.replace(tzinfo=now.tzinfo)
                hard_expired = (
                    superseded_at is not None
                    and superseded_at <= now - timedelta(days=90)
                )
                if still_live is not None and not hard_expired:
                    continue
                credential.ciphertext = None
                credential.nonce = None
                credential.state = "revoked"
                credential.destroyed_at = now
                destroyed_credentials += 1
            counts["credential_material_destroyed"] = destroyed_credentials
            protected_bundle_ids = set(
                session.scalars(select(DeploymentAssignmentSet.bundle_revision_id)).all()
            )
            deleted_bundles = 0
            deployments = session.scalars(select(SolutionDeployment)).all()
            for deployment in deployments:
                revisions = session.scalars(
                    select(SolutionBundleRevision)
                    .where(SolutionBundleRevision.deployment_id == deployment.id)
                    .order_by(SolutionBundleRevision.created_at.desc())
                ).all()
                for value in revisions[20:]:
                    if (
                        value.created_at < cutoffs["bundles"]
                        and value.id not in protected_bundle_ids
                    ):
                        session.delete(value)
                        deleted_bundles += 1
            counts["bundle_revisions"] = deleted_bundles
        return counts
