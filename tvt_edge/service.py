"""Transactional management operations for Slice 3."""

from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import re
import uuid
from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from jsonschema import Draft202012Validator

from apexfabric.solution_management.catalog import (
    CatalogError,
    load_delivery_metadata,
    resolve_registry_digest,
)
from tvt_edge.bundles import (
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
    CameraIdentifier,
    CameraRole,
    CameraRoleAssignment,
    CameraStatus,
    CameraStreamProfile,
    CameraValidationAttempt,
    CredentialKeyVersion,
    DeploymentAssignmentSet,
    DeploymentSyncState,
    DeploymentSyncAttempt,
    DiscoveryScope,
    DiscoveryRun,
    CameraObservation,
    KubernetesResourceRef,
    ManagementOperation,
    Site,
    SolutionCatalogEntry,
    SolutionBundleRevision,
    SolutionDeployment,
    utc_now,
)
from tvt_edge.security import CredentialKeyring, redact, redact_text


DNS_ID = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,61}[a-z0-9])?$")
SAFE_HOST = re.compile(r"^[A-Za-z0-9_.:-]+$")
SAFE_INTERFACE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


class ManagementService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        keyring: CredentialKeyring,
        catalog_resolver: Callable[[str, str, str], str] = resolve_registry_digest,
    ) -> None:
        self.sessions = sessions
        self.keyring = keyring
        self.catalog_resolver = catalog_resolver

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
            session.add(CameraStatus(camera_id=camera.id))
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

    def list_cameras(self) -> list[dict[str, Any]]:
        with self.sessions() as session:
            cameras = session.scalars(
                select(Camera).where(Camera.deleted_at.is_(None)).order_by(Camera.camera_key)
            ).all()
            return [self._camera_view(session, camera) for camera in cameras]

    def get_camera(self, camera_key: str) -> dict[str, Any]:
        with self.sessions() as session:
            camera = self._camera(session, camera_key)
            result = self._camera_view(session, camera)
            observations = session.scalars(
                select(CameraObservation)
                .where(CameraObservation.camera_id == camera.id)
                .order_by(CameraObservation.observed_at.desc())
                .limit(20)
            ).all()
            result["observations"] = [
                self._camera_observation_view(session, item) for item in observations
            ]
            return result

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
        status = session.get(CameraStatus, camera.id)
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
            "state": camera.onboarding_state,
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
            "validation_code": status.validation_code if status else None,
            "validation_failures": status.consecutive_failures if status else 0,
            "next_retry_at": status.next_retry_at.isoformat()
            if status and status.next_retry_at
            else None,
            "last_observed_at": status.last_observed_at.isoformat()
            if status and status.last_observed_at
            else None,
            "last_validated_at": status.last_validated_at.isoformat()
            if status and status.last_validated_at
            else None,
            "last_media_at": status.last_media_at.isoformat()
            if status and status.last_media_at
            else None,
            "identifiers": [
                {"kind": item.kind, "value": item.display_value or item.normalized_value}
                for item in identifiers
            ],
            "created_at": camera.created_at.isoformat(),
            "updated_at": camera.updated_at.isoformat(),
        }

    def list_discovery_scopes(self) -> list[dict[str, Any]]:
        with self.sessions() as session:
            scopes = session.scalars(
                select(DiscoveryScope)
                .order_by(DiscoveryScope.interface_name, DiscoveryScope.cidr)
                .limit(100)
            ).all()
            return [self._discovery_scope_view(scope) for scope in scopes]

    def create_discovery_scope(
        self,
        *,
        interface_name: str,
        cidr: str,
        rtsp_ports: list[int],
        enabled: bool,
        actor: str,
        request_id: str,
    ) -> dict[str, Any]:
        interface_name = interface_name.strip()
        if not SAFE_INTERFACE.fullmatch(interface_name):
            raise ValueError("interface_name contains unsupported characters")
        try:
            network = ipaddress.ip_network(cidr.strip(), strict=False)
        except ValueError as error:
            raise ValueError("cidr must be a valid network") from error
        if network.version != 4:
            raise ValueError("only IPv4 discovery scopes are supported")
        ports = sorted(set(rtsp_ports))
        if not ports or len(ports) > 16 or any(
            isinstance(port, bool) or port < 1 or port > 65535 for port in ports
        ):
            raise ValueError("rtsp_ports must contain 1 to 16 valid ports")
        canonical_cidr = str(network)
        with self.sessions.begin() as session:
            site = self.current_site(session)
            existing = session.scalar(
                select(DiscoveryScope).where(
                    DiscoveryScope.site_id == site.id,
                    DiscoveryScope.interface_name == interface_name,
                    DiscoveryScope.cidr == canonical_cidr,
                )
            )
            if existing is not None:
                raise ValueError("discovery scope already exists")
            scope = DiscoveryScope(
                site_id=site.id,
                interface_name=interface_name,
                cidr=canonical_cidr,
                rtsp_ports=ports,
                enabled=enabled,
            )
            session.add(scope)
            session.flush()
            self._audit(
                session,
                actor=actor,
                request_id=request_id,
                action="discovery_scope.create",
                target_type="discovery_scope",
                target_id=str(scope.id),
                details={
                    "interface_name": interface_name,
                    "cidr": canonical_cidr,
                    "rtsp_ports": ports,
                },
            )
            return self._discovery_scope_view(scope)

    def delete_discovery_scope(
        self, scope_id: uuid.UUID, actor: str, request_id: str
    ) -> None:
        with self.sessions.begin() as session:
            scope = session.get(DiscoveryScope, scope_id)
            if scope is None:
                raise ValueError("unknown discovery scope")
            self._audit(
                session,
                actor=actor,
                request_id=request_id,
                action="discovery_scope.delete",
                target_type="discovery_scope",
                target_id=str(scope.id),
                details={
                    "interface_name": scope.interface_name,
                    "cidr": scope.cidr,
                },
            )
            session.delete(scope)

    @staticmethod
    def _discovery_scope_view(scope: DiscoveryScope) -> dict[str, Any]:
        return {
            "scope_id": str(scope.id),
            "interface_name": scope.interface_name,
            "cidr": scope.cidr,
            "rtsp_ports": scope.rtsp_ports,
            "enabled": scope.enabled,
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
        if scheme not in {"rtsp", "rtsps"} or not SAFE_HOST.fullmatch(host):
            raise ValueError("stream endpoint is invalid")
        if not 1 <= port <= 65535:
            raise ValueError("stream port is invalid")
        if not path.startswith("/") or any(char in path for char in "?#@"):
            raise ValueError("stream path must be non-secret and contain no query/userinfo")
        if transport not in {"tcp", "udp"}:
            raise ValueError("transport must be tcp or udp")
        with self.sessions.begin() as session:
            camera = self._camera(session, camera_key)
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
            camera.onboarding_state = (
                "validating" if camera.onboarding_state != "online" else "online"
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
            camera.onboarding_state = "validating"
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
                credential = self._active_credential(session, camera.id)
                credential_filter = (
                    CameraValidationAttempt.credential_version_id
                    == credential.id
                    if credential is not None
                    else CameraValidationAttempt.credential_version_id.is_(None)
                )
                validated = session.scalar(
                    select(CameraValidationAttempt.id).where(
                        CameraValidationAttempt.camera_id == camera.id,
                        CameraValidationAttempt.profile_id == profile_id,
                        credential_filter,
                        CameraValidationAttempt.status == "succeeded",
                        CameraValidationAttempt.result_code == "OK",
                    )
                )
                if validated is None:
                    raise ValueError(
                        "camera requires successful validation of its selected "
                        "stream and current credentials"
                    )
                camera.enabled = True
                if camera.onboarding_state == "disabled":
                    camera.onboarding_state = "validating"
            else:
                camera.enabled = False
                camera.onboarding_state = "disabled"
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

    def queue_validation(
        self, camera_key: str, trigger: str, actor: str, request_id: str
    ) -> CameraValidationAttempt:
        with self.sessions.begin() as session:
            camera = self._camera(session, camera_key)
            profile = session.scalar(
                select(CameraStreamProfile).where(
                    CameraStreamProfile.camera_id == camera.id,
                    CameraStreamProfile.selected.is_(True),
                )
            )
            if profile is None:
                raise ValueError("camera requires a selected stream profile")
            credential = self._active_credential(session, camera.id)
            attempt = CameraValidationAttempt(
                camera_id=camera.id,
                profile_id=profile.id,
                credential_version_id=credential.id if credential else None,
                trigger=trigger,
            )
            session.add(attempt)
            camera.onboarding_state = "validating"
            self._audit(
                session,
                actor=actor,
                request_id=request_id,
                action="camera.validation.queue",
                target_type="camera",
                target_id=camera_key,
            )
            return attempt

    def list_validation_attempts(
        self, camera_key: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        if limit <= 0 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        with self.sessions() as session:
            camera = self._camera(session, camera_key)
            attempts = session.scalars(
                select(CameraValidationAttempt)
                .where(CameraValidationAttempt.camera_id == camera.id)
                .order_by(CameraValidationAttempt.created_at.desc())
                .limit(limit)
            ).all()
            return [
                self._validation_attempt_view(attempt, camera.camera_key)
                for attempt in attempts
            ]

    @staticmethod
    def _validation_attempt_view(
        attempt: CameraValidationAttempt, camera_key: str,
    ) -> dict[str, Any]:
        return {
            "attempt_id": str(attempt.id),
            "camera_id": camera_key,
            "profile_id": str(attempt.profile_id) if attempt.profile_id else None,
            "credential_version_id": str(attempt.credential_version_id)
            if attempt.credential_version_id
            else None,
            "trigger": attempt.trigger,
            "status": attempt.status,
            "stage": attempt.stage,
            "result_code": attempt.result_code,
            "safe_result": attempt.safe_result,
            "started_at": attempt.started_at.isoformat() if attempt.started_at else None,
            "finished_at": attempt.finished_at.isoformat() if attempt.finished_at else None,
            "created_at": attempt.created_at.isoformat(),
        }

    def record_validation_result(
        self,
        attempt_id: uuid.UUID,
        *,
        result_code: str,
        safe_result: dict[str, Any],
        actor: str,
        request_id: str,
    ) -> CameraValidationAttempt:
        with self.sessions.begin() as session:
            attempt = session.get(CameraValidationAttempt, attempt_id)
            if attempt is None:
                raise ValueError("unknown validation attempt")
            if attempt.status not in {"queued", "running"}:
                raise ValueError("validation attempt is already complete")
            success = result_code == "OK"
            attempt.status = "succeeded" if success else "failed"
            attempt.result_code = result_code
            attempt.safe_result = redact(safe_result)
            attempt.finished_at = utc_now()
            camera = session.get(Camera, attempt.camera_id)
            if camera is None:
                raise ValueError("validation camera is missing")
            camera.onboarding_state = "online" if success else "invalid"
            status = session.get(CameraStatus, camera.id)
            if status is None:
                status = CameraStatus(camera_id=camera.id)
                session.add(status)
            status.validation_code = result_code
            status.last_validated_at = attempt.finished_at
            status.consecutive_failures = 0 if success else status.consecutive_failures + 1
            self._audit(
                session,
                actor=actor,
                request_id=request_id,
                action="camera.validation.result",
                target_type="camera",
                target_id=camera.camera_key,
                result=attempt.status,
                details={"result_code": result_code},
            )
            return attempt

    def list_discovery_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        if limit <= 0 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        with self.sessions() as session:
            runs = session.scalars(
                select(DiscoveryRun)
                .order_by(DiscoveryRun.created_at.desc())
                .limit(limit)
            ).all()
            return [self._discovery_run_view(run) for run in runs]

    def get_discovery_run(
        self, run_id: uuid.UUID, observation_limit: int = 100
    ) -> dict[str, Any]:
        if observation_limit <= 0 or observation_limit > 100:
            raise ValueError("observation_limit must be between 1 and 100")
        with self.sessions() as session:
            run = session.get(DiscoveryRun, run_id)
            if run is None:
                raise ValueError("unknown discovery operation")
            observations = session.scalars(
                select(CameraObservation)
                .where(CameraObservation.run_id == run.id)
                .order_by(CameraObservation.observed_at.desc())
                .limit(observation_limit + 1)
            ).all()
            result = self._discovery_run_view(run)
            result["observations"] = [
                self._camera_observation_view(session, item)
                for item in observations[:observation_limit]
            ]
            result["observations_truncated"] = len(observations) > observation_limit
            return result

    @staticmethod
    def _discovery_run_view(run: DiscoveryRun) -> dict[str, Any]:
        return {
            "operation_id": str(run.id),
            "run_id": str(run.id),
            "trigger": run.trigger,
            "status": run.status,
            "counters": run.counters,
            "error_code": run.error_code,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "created_at": run.created_at.isoformat(),
        }

    @staticmethod
    def _camera_observation_view(
        session: Session, observation: CameraObservation
    ) -> dict[str, Any]:
        camera = (
            session.get(Camera, observation.camera_id)
            if observation.camera_id is not None
            else None
        )
        return {
            "observation_id": str(observation.id),
            "camera_id": camera.camera_key if camera is not None else None,
            "method": observation.method,
            "address": observation.address,
            "result_code": observation.result_code,
            "metadata": redact(observation.metadata_json),
            "observed_at": observation.observed_at.isoformat(),
        }

    def management_status(self) -> dict[str, Any]:
        """Aggregate durable camera-validation and synchronization state."""

        with self.sessions() as session:
            cameras = session.scalars(
                select(Camera).where(Camera.deleted_at.is_(None))
            ).all()
            camera_states = Counter(camera.onboarding_state for camera in cameras)
            enabled = [camera for camera in cameras if camera.enabled]
            enabled_ids = [camera.id for camera in enabled]
            statuses = (
                session.scalars(
                    select(CameraStatus).where(CameraStatus.camera_id.in_(enabled_ids))
                ).all()
                if enabled_ids
                else []
            )
            status_by_camera = {status.camera_id: status for status in statuses}
            validation_ok = sum(
                1
                for camera in enabled
                if camera.onboarding_state == "online"
                and status_by_camera.get(camera.id) is not None
                and status_by_camera[camera.id].validation_code == "OK"
            )
            validation_failing = len(enabled) - validation_ok
            if not cameras:
                camera_health = "unconfigured"
            elif validation_failing:
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
                    "validated_online": validation_ok,
                    "validation_failing": validation_failing,
                    "by_state": dict(sorted(camera_states.items())),
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

    def queue_discovery(
        self, trigger: str, actor: str, request_id: str
    ) -> DiscoveryRun:
        with self.sessions.begin() as session:
            site = self.current_site(session)
            run = DiscoveryRun(site_id=site.id, trigger=trigger)
            session.add(run)
            session.flush()
            self._audit(
                session,
                actor=actor,
                request_id=request_id,
                action="discovery.queue",
                target_type="discovery_run",
                target_id=str(run.id),
            )
            return run

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
        site = self.current_site(session)
        deployment_query = select(SolutionDeployment).where(
            SolutionDeployment.site_id == site.id,
            SolutionDeployment.deployment_key == deployment_key,
            SolutionDeployment.deleted_at.is_(None),
        )
        if lock_deployment:
            deployment_query = deployment_query.with_for_update()
        deployment = session.scalar(deployment_query)
        if deployment is not None and deployment.solution_id != "traffic-edge":
            raise ValueError("a deployment ID cannot be reused for another solution")
        desired_revision = deployment.next_desired_revision if deployment else 1
        resolved = []
        bundle_cameras: list[BundleCamera] = []
        desired_cameras: list[dict[str, Any]] = []
        for position, item in enumerate(assignments):
            camera = self._camera(session, item["camera_id"])
            if camera.site_id != site.id or not camera.enabled or camera.onboarding_state != "online":
                raise ValueError(f"camera {camera.camera_key!r} is not enabled and online")
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
                "solution_pack": "traffic",
                "fps": fps,
                "apps": list(apps),
            }
            if config:
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
            raise ValueError(f"invalid Traffic geometry at {location}: {error.message}")
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
        assignments: list[dict[str, Any]],
        inference_mode: str,
        resources: dict[str, str],
        state_size: str,
        namespace: str,
        preview_bundle_sha256: str,
        idempotency_key: str,
        actor: str,
        request_id: str,
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
                    solution_id="traffic-edge",
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
                if camera.site_id != site.id or not camera.enabled or camera.onboarding_state != "online":
                    raise ValueError(f"camera {camera.camera_key!r} is not enabled and online")
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
            copied = CameraDeploymentAssignment(
                assignment_set_id=clone.id,
                camera_id=original.camera_id,
                stream_profile_id=original.stream_profile_id,
                credential_version_id=credential_id,
                ordinal=original.ordinal,
                requested_fps=original.requested_fps,
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
                        configuration=copy.deepcopy(app.configuration),
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
            camera.onboarding_state = "deleted"
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
            camera.onboarding_state = "needs_credentials"
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
            "observations": now - timedelta(days=30),
            "validation": now - timedelta(days=90),
            "sync": now - timedelta(days=180),
            "audit": now - timedelta(days=365),
            "bundles": now - timedelta(days=365),
        }
        counts: dict[str, int] = {}
        with self.sessions.begin() as session:
            counts["camera_observations"] = session.query(CameraObservation).filter(
                CameraObservation.observed_at < cutoffs["observations"]
            ).delete(synchronize_session=False)
            counts["discovery_runs"] = session.query(DiscoveryRun).filter(
                DiscoveryRun.created_at < cutoffs["observations"],
                ~DiscoveryRun.id.in_(select(CameraObservation.run_id)),
            ).delete(synchronize_session=False)
            counts["validation_attempts"] = session.query(
                CameraValidationAttempt
            ).filter(
                CameraValidationAttempt.created_at < cutoffs["validation"]
            ).delete(synchronize_session=False)
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
