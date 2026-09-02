"""Background workers for camera discovery and RTSP validation."""

from __future__ import annotations

import ipaddress
import re
import socket
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from tvt_edge.camera.identity import CameraEvidence, ensure_camera_for_evidence
from tvt_edge.camera.onvif import OnvifDiscovery, discover_onvif
from tvt_edge.camera.rtsp_probe import ProbeResult, probe_camera
from tvt_edge.camera.state_machine import CameraStateMachine
from tvt_edge.db.models import (
    Camera,
    CameraCredentialVersion,
    CameraEndpoint,
    CameraObservation,
    CameraStatus,
    CameraStreamProfile,
    DiscoveryRun,
    DiscoveryScope,
    Site,
    CameraValidationAttempt,
    utc_now,
)
from tvt_edge.security import CredentialKeyring, redact, redact_text


@dataclass(frozen=True)
class DiscoveryCandidate:
    host: str
    method: str
    rtsp_port: int
    rtsp_path: str | None = None
    onvif_endpoint_uuid: str | None = None
    onvif_device_id: str | None = None
    mac: str | None = None
    manufacturer: str | None = None
    model: str | None = None
    interface: str | None = None
    source: str = "discovery"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ValidationCandidate:
    camera_id: uuid.UUID
    stream_profile_id: uuid.UUID
    credential_version_id: uuid.UUID | None


class DiscoveryWorker:
    """Run ONVIF/neighbor/TCP discovery and queue validation attempts."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        onvif_timeout: float = 1.0,
        tcp_timeout: float = 1.0,
    ) -> None:
        self.sessions = sessions
        self.onvif_timeout = onvif_timeout
        self.tcp_timeout = tcp_timeout

    def run_once(self) -> uuid.UUID | None:
        run_id = self._claim_run()
        if run_id is None:
            return None
        try:
            self._process_run(run_id)
            return run_id
        except Exception as error:
            self._finalize_failure(run_id, str(error))
            raise

    def _claim_run(self) -> uuid.UUID | None:
        now = utc_now()
        with self.sessions.begin() as session:
            run = session.scalar(
                select(DiscoveryRun)
                .where(DiscoveryRun.status == "queued")
                .order_by(DiscoveryRun.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if run is None:
                return None
            run.status = "running"
            run.started_at = now
            run.counters = {
                "discovered_candidates": 0,
                "cameras_matched": 0,
                "cameras_created": 0,
                "validations_queued": 0,
                "endpoints_updated": 0,
            }
            return run.id

    def _process_run(self, run_id: uuid.UUID) -> None:
        with self.sessions.begin() as session:
            run = session.get(DiscoveryRun, run_id)
            if run is None:
                raise ValueError("discovery run is missing")
            if session.get(Site, run.site_id) is None:
                raise ValueError("discovery run references missing site")

            scopes = self._load_scopes(session, run.site_id)
            candidates = self._discover_candidates(scopes)

            discovered = 0
            matched = 0
            created = 0
            queued = 0
            endpoints_updated = 0

            for candidate in candidates:
                if not self._in_scopes(candidate.host, scopes):
                    continue
                discovered += 1
                camera, was_created = self._apply_candidate(session, run.site_id, candidate)
                if camera is None:
                    continue
                if was_created:
                    created += 1
                matched += 1

                if self._link_or_update_endpoint(session, camera, candidate):
                    endpoints_updated += 1

                if candidate.rtsp_path:
                    profile_token = (
                        candidate.onvif_device_id
                        or f"{candidate.method}:{candidate.host}:{candidate.rtsp_port}"
                    )
                    profile = self._upsert_profile(
                        session,
                        camera.id,
                        candidate.rtsp_path,
                        transport="tcp",
                        profile_token=profile_token,
                    )
                    if self._ensure_validation_queued(session, camera.id, profile.id):
                        queued += 1

                self._record_observation(
                    session,
                    run.id,
                    camera.id,
                    candidate.host,
                    {
                        "method": candidate.method,
                        "interface": candidate.interface,
                        "port": candidate.rtsp_port,
                        "source": candidate.source,
                    },
                )

            run.status = "succeeded"
            run.finished_at = utc_now()
            run.error_code = None
            run.counters = {
                "discovered_candidates": discovered,
                "cameras_matched": matched,
                "cameras_created": created,
                "validations_queued": queued,
                "endpoints_updated": endpoints_updated,
            }

    def _load_scopes(self, session: Session, site_id: uuid.UUID) -> list[DiscoveryScope]:
        return list(
            session.scalars(
                select(DiscoveryScope)
                .where(DiscoveryScope.site_id == site_id, DiscoveryScope.enabled.is_(True))
                .order_by(DiscoveryScope.cidr)
            ).all()
        )

    def _discover_candidates(self, scopes: list[DiscoveryScope]) -> list[DiscoveryCandidate]:
        if not scopes:
            return []
        candidates = (
            self._discover_from_onvif(scopes)
            + self._discover_from_neighbors(scopes)
            + self._discover_from_tcp(scopes)
        )
        return self._dedupe_candidates(candidates)

    def _discover_from_onvif(self, scopes: list[DiscoveryScope]) -> list[DiscoveryCandidate]:
        interfaces = [scope.interface_name for scope in scopes if scope.interface_name]
        hits = discover_onvif(
            interfaces=interfaces or None,
            timeout=self.onvif_timeout,
        )
        discovered: list[DiscoveryCandidate] = []
        for item in hits:
            host, port, path = self._choose_rtsp_from_xaddrs(item)
            if host is None:
                continue
            discovered.append(
                DiscoveryCandidate(
                    host=host,
                    method="onvif",
                    rtsp_port=port,
                    rtsp_path=path,
                    onvif_endpoint_uuid=item.endpoint_uuid,
                    onvif_device_id=self._extract_uuid(item.endpoint_uuid),
                    interface=item.interface,
                    manufacturer=item.scopes.get("manufacturer"),
                    model=item.scopes.get("model"),
                    source="onvif",
                    metadata={"scopes": item.scopes, "types": item.metadata.get("types")},
                )
            )
        return discovered

    def _discover_from_neighbors(self, scopes: list[DiscoveryScope]) -> list[DiscoveryCandidate]:
        neighbors = self._neighbor_cache()
        if not neighbors:
            return []
        primary_interface = next(
            (scope.interface_name for scope in scopes if scope.interface_name),
            None,
        )
        result: list[DiscoveryCandidate] = []
        for host, mac in neighbors:
            if not self._in_scopes(host, scopes):
                continue
            result.append(
                DiscoveryCandidate(
                    host=host,
                    method="neighbor",
                    rtsp_port=554,
                    mac=mac,
                    interface=primary_interface,
                    source="neighbor",
                )
            )
        return result

    def _discover_from_tcp(self, scopes: list[DiscoveryScope]) -> list[DiscoveryCandidate]:
        peers = self._discover_from_neighbors(scopes)
        if not peers:
            return []
        hosts = {peer.host for peer in peers}
        result: list[DiscoveryCandidate] = []
        for host in sorted(hosts):
            for scope in scopes:
                if not self._in_scopes(host, [scope]):
                    continue
                for raw_port in scope.rtsp_ports:
                    try:
                        port = int(raw_port)
                    except (TypeError, ValueError):
                        continue
                    if self._tcp_probe(host, port):
                        result.append(
                            DiscoveryCandidate(
                                host=host,
                                method="tcp",
                                rtsp_port=port,
                                rtsp_path="/",
                                interface=scope.interface_name,
                                source="tcp",
                            )
                        )
                        break
        return result

    @staticmethod
    def _in_scopes(host: str, scopes: list[DiscoveryScope]) -> bool:
        if not scopes:
            return False
        for scope in scopes:
            try:
                if ipaddress.ip_address(host) in ipaddress.ip_network(scope.cidr):
                    return True
            except ValueError:
                continue
        return False

    @staticmethod
    def _extract_uuid(value: str | None) -> str | None:
        if value is None:
            return None
        if value.startswith("uuid:"):
            return value.split(":", 1)[1]
        return value

    @staticmethod
    def _choose_rtsp_from_xaddrs(item: OnvifDiscovery) -> tuple[str | None, int, str | None]:
        for address in item.xaddrs:
            match = re.match(r"^(?:https?|rtsp|rtsps)://([^/:]+)(?::(\d+))?", address)
            if match is None:
                continue
            host = match.group(1)
            port = int(match.group(2) or 554)
            return host, port, "/"
        return None, 554, "/"

    def _tcp_probe(self, host: str, port: int) -> bool:
        try:
            with socket.create_connection((host, port), timeout=self.tcp_timeout):
                return True
        except OSError:
            return False

    @staticmethod
    def _neighbor_cache() -> set[tuple[str, str | None]]:
        try:
            command = subprocess.run(
                ["ip", "-4", "-o", "neigh"],
                check=False,
                capture_output=True,
                text=True,
                timeout=1,
            )
        except (OSError, subprocess.TimeoutExpired):
            return set()
        result: set[tuple[str, str | None]] = set()
        for line in command.stdout.splitlines():
            pieces = line.split()
            if not pieces:
                continue
            host = pieces[0]
            try:
                ipaddress.ip_address(host)
            except ValueError:
                continue
            mac = None
            if "lladdr" in pieces:
                idx = pieces.index("lladdr") + 1
                if idx < len(pieces):
                    mac = pieces[idx]
            result.add((host, mac))
        return result

    @staticmethod
    def _dedupe_candidates(candidates: list[DiscoveryCandidate]) -> list[DiscoveryCandidate]:
        deduped: list[DiscoveryCandidate] = []
        seen: set[tuple[str, int, str]] = set()
        for candidate in candidates:
            key = (candidate.host, candidate.rtsp_port, candidate.method)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(candidate)
        return deduped

    def _apply_candidate(
        self,
        session: Session,
        site_id: uuid.UUID,
        candidate: DiscoveryCandidate,
    ) -> tuple[Camera | None, bool]:
        evidence = CameraEvidence(
            host=candidate.host,
            method=candidate.method,
            rtsp_port=candidate.rtsp_port,
            rtsp_path=candidate.rtsp_path,
            onvif_endpoint_uuid=candidate.onvif_endpoint_uuid,
            onvif_device_id=candidate.onvif_device_id,
            mac=candidate.mac,
            manufacturer=candidate.manufacturer,
            model=candidate.model,
            source=candidate.source,
            metadata=candidate.metadata or {},
        )
        camera, created = ensure_camera_for_evidence(
            session,
            site_id,
            evidence,
            friendly_name=f"Discovered {candidate.host}",
        )
        camera.onboarding_state = CameraStateMachine.transition_for_discovery(
            camera.onboarding_state,
            method=candidate.method,
        )
        camera.row_version += 1
        if created or camera.manufacturer is None:
            if candidate.manufacturer:
                camera.manufacturer = candidate.manufacturer
        if created or camera.model is None:
            if candidate.model:
                camera.model = candidate.model
        return camera, created

    def _link_or_update_endpoint(
        self,
        session: Session,
        camera: Camera,
        candidate: DiscoveryCandidate,
    ) -> bool:
        endpoint = session.scalar(
            select(CameraEndpoint).where(
                CameraEndpoint.camera_id == camera.id,
                CameraEndpoint.kind == "rtsp",
                CameraEndpoint.host == candidate.host,
                CameraEndpoint.port == candidate.rtsp_port,
            )
        )
        now = utc_now()
        if endpoint is None:
            session.add(
                CameraEndpoint(
                    camera_id=camera.id,
                    kind="rtsp",
                    scheme="rtsp",
                    host=candidate.host,
                    port=candidate.rtsp_port,
                    path=candidate.rtsp_path or "/",
                    interface_name=candidate.interface,
                    is_current=True,
                    first_seen_at=now,
                    last_seen_at=now,
                )
            )
            return True

        endpoint.is_current = True
        endpoint.last_seen_at = now
        if candidate.rtsp_path is not None:
            endpoint.path = candidate.rtsp_path
        if candidate.interface is not None:
            endpoint.interface_name = candidate.interface
        return True

    def _upsert_profile(
        self,
        session: Session,
        camera_id: uuid.UUID,
        path: str,
        *,
        transport: str = "tcp",
        profile_token: str | None = None,
    ) -> CameraStreamProfile:
        endpoint = session.scalar(
            select(CameraEndpoint).where(
                CameraEndpoint.camera_id == camera_id,
                CameraEndpoint.kind == "rtsp",
            )
        )
        if endpoint is None:
            raise ValueError("camera endpoint is missing")

        existing = session.scalar(
            select(CameraStreamProfile).where(
                CameraStreamProfile.camera_id == camera_id,
                CameraStreamProfile.endpoint_id == endpoint.id,
                CameraStreamProfile.path == path,
                CameraStreamProfile.profile_token == (profile_token or "discovered"),
                CameraStreamProfile.transport == transport,
            )
        )
        if existing is not None:
            return existing

        return self._create_unselected_profile(
            session,
            camera_id,
            endpoint.id,
            path,
            transport,
            profile_token or "discovered",
        )

    @staticmethod
    def _create_unselected_profile(
        session: Session,
        camera_id: uuid.UUID,
        endpoint_id: uuid.UUID,
        path: str,
        transport: str,
        profile_token: str,
    ) -> CameraStreamProfile:
        selected = session.scalar(
            select(CameraStreamProfile).where(
                CameraStreamProfile.camera_id == camera_id,
                CameraStreamProfile.selected.is_(True),
            )
        )
        profile = CameraStreamProfile(
            camera_id=camera_id,
            endpoint_id=endpoint_id,
            profile_token=profile_token,
            path=path,
            transport=transport,
            selected=selected is None,
            available=True,
            observed_at=utc_now(),
        )
        session.add(profile)
        session.flush()
        return profile

    def _ensure_validation_queued(
        self,
        session: Session,
        camera_id: uuid.UUID,
        profile_id: uuid.UUID,
    ) -> bool:
        existing = session.scalar(
            select(CameraValidationAttempt.id).where(
                CameraValidationAttempt.camera_id == camera_id,
                CameraValidationAttempt.profile_id == profile_id,
                CameraValidationAttempt.status.in_(("queued", "running")),
            )
        )
        if existing is not None:
            return False
        session.add(
            CameraValidationAttempt(
                camera_id=camera_id,
                profile_id=profile_id,
                trigger="discovery",
                status="queued",
            )
        )
        return True

    @staticmethod
    def _record_observation(
        session: Session,
        run_id: uuid.UUID,
        camera_id: uuid.UUID | None,
        address: str,
        metadata: dict[str, Any],
    ) -> None:
        session.add(
            CameraObservation(
                run_id=run_id,
                camera_id=camera_id,
                method="discovery",
                address=address,
                result_code="OK",
                metadata_json=redact(metadata),
                observed_at=utc_now(),
            )
        )

    def _finalize_failure(self, run_id: uuid.UUID, error: str) -> None:
        now = utc_now()
        with self.sessions.begin() as session:
            run = session.get(DiscoveryRun, run_id)
            if run is None:
                return
            run.status = "failed"
            run.finished_at = now
            run.error_code = "DISCOVERY_FAILED"
            session.add(
                CameraObservation(
                    run_id=run_id,
                    method="discovery",
                    address="<worker>",
                    result_code="DISCOVERY_FAILED",
                    metadata_json={"error": redact_text(str(error))},
                    observed_at=now,
                )
            )


class ValidationWorker:
    """Claim queued validation attempts and perform RTSP validation."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        keyring: CredentialKeyring,
    ) -> None:
        self.sessions = sessions
        self.keyring = keyring

    def run_once(self) -> uuid.UUID | None:
        attempt_id = self._claim()
        if attempt_id is None:
            return None
        try:
            self._run(attempt_id)
            return attempt_id
        except Exception as error:
            self._record_failure(attempt_id, str(error))
            raise

    def _claim(self) -> uuid.UUID | None:
        now = utc_now()
        with self.sessions.begin() as session:
            attempt = session.scalar(
                select(CameraValidationAttempt)
                .outerjoin(
                    CameraStatus,
                    CameraStatus.camera_id == CameraValidationAttempt.camera_id,
                )
                .where(CameraValidationAttempt.status == "queued")
                .where(
                    or_(
                        CameraStatus.next_retry_at.is_(None),
                        CameraStatus.next_retry_at <= now,
                    )
                )
                .order_by(CameraValidationAttempt.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if attempt is None:
                return None
            attempt.status = "running"
            attempt.started_at = now
            return attempt.id

    def _run(self, attempt_id: uuid.UUID) -> None:
        with self.sessions.begin() as session:
            attempt = session.get(CameraValidationAttempt, attempt_id)
            if attempt is None or attempt.status != "running":
                return

            profile = session.get(CameraStreamProfile, attempt.profile_id)
            if profile is None:
                raise ValueError("validation attempt references missing stream profile")
            camera = session.get(Camera, attempt.camera_id)
            if camera is None:
                raise ValueError("validation attempt references missing camera")
            endpoint = session.get(CameraEndpoint, profile.endpoint_id)
            if endpoint is None:
                raise ValueError("validation attempt references missing endpoint")

            credential = None
            if attempt.credential_version_id is not None:
                credential_record = session.get(
                    CameraCredentialVersion, attempt.credential_version_id
                )
                if credential_record is None:
                    raise ValueError("validation attempt references missing credentials")
                credential = self.keyring.decrypt(
                    camera.id,
                    credential_record.id,
                    credential_record.ciphertext,
                    credential_record.nonce,
                    credential_record.key_version,
                    credential_record.aad_version,
                )

            result = probe_camera(
                endpoint.host,
                endpoint.port,
                credential,
                path=profile.path,
                scheme=endpoint.scheme,
            )
            self._write_result(session, attempt, camera, profile, result)

    def _write_result(
        self,
        session: Session,
        attempt: CameraValidationAttempt,
        camera: Camera,
        profile: CameraStreamProfile,
        result: ProbeResult,
    ) -> None:
        now = utc_now()
        attempt.finished_at = now
        attempt.result_code = result.result_code
        attempt.safe_result = redact(result.safe_result)
        attempt.stage = "complete"
        attempt.status = "succeeded" if result.result_code == "OK" else "failed"

        status = session.get(CameraStatus, camera.id)
        if status is None:
            status = CameraStatus(camera_id=camera.id)
            session.add(status)

        status.validation_code = result.result_code
        status.last_validated_at = now
        status.last_observed_at = now
        camera.onboarding_state = CameraStateMachine.after_validation(
            camera.onboarding_state,
            result.result_code,
        )
        if result.result_code != "OK":
            profile.available = False
        camera.row_version += 1

        if result.result_code == "OK":
            self._apply_success_metadata(profile, result, now, status)
            return

        status.consecutive_failures = (status.consecutive_failures or 0) + 1
        if CameraStateMachine.should_retry_validation(result.result_code):
            delay = CameraStateMachine.validation_delay_seconds(status.consecutive_failures)
            status.next_retry_at = now + timedelta(seconds=delay) if delay > 0 else None
            if delay > 0:
                self._queue_retry(session, attempt)
        else:
            status.next_retry_at = None

    @staticmethod
    def _apply_success_metadata(
        profile: CameraStreamProfile,
        result: ProbeResult,
        now,
        status: CameraStatus,
    ) -> None:
        if result.safe_result.get("width") is not None:
            profile.width = result.safe_result.get("width")
        if result.safe_result.get("height") is not None:
            profile.height = result.safe_result.get("height")
        if result.safe_result.get("fps") is not None:
            profile.fps = result.safe_result.get("fps")
        if result.safe_result.get("codec") is not None:
            profile.codec = result.safe_result.get("codec")
        profile.available = True
        profile.observed_at = now
        status.consecutive_failures = 0
        status.next_retry_at = None

    def _queue_retry(self, session: Session, attempt: CameraValidationAttempt) -> None:
        existing = session.scalar(
            select(CameraValidationAttempt.id).where(
                CameraValidationAttempt.camera_id == attempt.camera_id,
                CameraValidationAttempt.profile_id == attempt.profile_id,
                CameraValidationAttempt.credential_version_id
                == attempt.credential_version_id,
                CameraValidationAttempt.status == "queued",
            )
        )
        if existing is not None:
            return
        session.add(
            CameraValidationAttempt(
                camera_id=attempt.camera_id,
                profile_id=attempt.profile_id,
                credential_version_id=attempt.credential_version_id,
                trigger="retry",
                status="queued",
            )
        )

    def _record_failure(self, attempt_id: uuid.UUID, error: str) -> None:
        now = utc_now()
        with self.sessions.begin() as session:
            attempt = session.get(CameraValidationAttempt, attempt_id)
            if attempt is None:
                return
            camera = session.get(Camera, attempt.camera_id)
            if camera is None:
                return

            attempt.status = "failed"
            attempt.finished_at = now
            attempt.result_code = "PROBE_INTERNAL_ERROR"
            attempt.safe_result = {"error": redact_text(str(error))}
            attempt.stage = "failed"
            camera.onboarding_state = CameraStateMachine.after_validation(
                camera.onboarding_state,
                attempt.result_code,
            )
            camera.row_version += 1

            status = session.get(CameraStatus, attempt.camera_id)
            if status is None:
                status = CameraStatus(camera_id=attempt.camera_id)
                session.add(status)
            status.validation_code = attempt.result_code
            status.last_validated_at = now
            status.last_observed_at = now
            status.consecutive_failures = (status.consecutive_failures or 0) + 1

            if attempt.profile_id is not None:
                profile = session.get(CameraStreamProfile, attempt.profile_id)
                if profile is not None:
                    profile.available = False

            if CameraStateMachine.should_retry_validation(attempt.result_code):
                delay = CameraStateMachine.validation_delay_seconds(
                    status.consecutive_failures
                )
                status.next_retry_at = (
                    now + timedelta(seconds=delay) if delay > 0 else None
                )
                if delay > 0:
                    self._queue_retry(session, attempt)
            else:
                status.next_retry_at = None
