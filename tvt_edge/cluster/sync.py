"""Lease, materialize and idempotently apply committed desired revisions."""

from __future__ import annotations

import base64
import json
import subprocess
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import quote, urlencode

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from apexfabric.solution_management.renderer import Kubectl, reconcile, render
from tvt_edge.db.models import (
    Camera,
    CameraApplicationAssignment,
    CameraCredentialVersion,
    CameraDeploymentAssignment,
    CameraEndpoint,
    CameraStreamProfile,
    DeploymentAssignmentSet,
    DeploymentSyncAttempt,
    DeploymentSyncState,
    KubernetesResourceRef,
    ManagementOperation,
    Site,
    SolutionBundleRevision,
    SolutionDeployment,
    utc_now,
)
from tvt_edge.security import CredentialKeyring, redact_text
from tvt_runtime.camera_secrets import build_camera_secret_list, secret_names


@dataclass(frozen=True)
class WorkCamera:
    camera_id: uuid.UUID
    camera_key: str
    profile_id: uuid.UUID
    endpoint_scheme: str
    endpoint_host: str
    endpoint_port: int
    path: str
    fps: int
    apps: tuple[str, ...]
    credential: tuple[uuid.UUID, bytes | None, bytes | None, int, int] | None


@dataclass(frozen=True)
class SyncWorkItem:
    deployment_id: uuid.UUID
    deployment_key: str
    namespace: str
    desired_revision: int
    assignment_set_id: uuid.UUID
    bundle: dict[str, Any]
    edge_id: str
    attempt_id: uuid.UUID
    operation_id: uuid.UUID
    claim_token: uuid.UUID
    cameras: tuple[WorkCamera, ...]


def build_rtsp_url(camera: WorkCamera, credential: dict[str, Any] | None) -> str:
    host = camera.endpoint_host
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    userinfo = ""
    query: dict[str, str] = {}
    suffix = ""
    if credential:
        username = credential.get("username")
        password = credential.get("password")
        if username is not None:
            userinfo = quote(username, safe="")
            if password is not None:
                userinfo += ":" + quote(password, safe="")
            userinfo += "@"
        query = credential.get("query", {})
        suffix = credential.get("path_suffix", "")
    path = camera.path + suffix
    value = (
        f"{camera.endpoint_scheme}://{userinfo}{host}:{camera.endpoint_port}{path}"
    )
    if query:
        value += "?" + urlencode(sorted(query.items()))
    return value


class SyncWorker:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        keyring: CredentialKeyring,
        kubectl: Kubectl,
        *,
        worker_id: str,
        rollout_timeout: int = 180,
        lease_seconds: int = 600,
    ) -> None:
        self.sessions = sessions
        self.keyring = keyring
        self.kubectl = kubectl
        self.worker_id = worker_id
        self.rollout_timeout = rollout_timeout
        # A rollout is a blocking kubectl call, so the claim must remain fenced
        # for the entire rollout plus enough time to persist its result.
        self.lease_seconds = max(lease_seconds, rollout_timeout + 120)

    def claim(self) -> SyncWorkItem | None:
        now = utc_now()
        with self.sessions.begin() as session:
            sync = session.scalar(
                select(DeploymentSyncState)
                .where(
                    DeploymentSyncState.state.in_(("pending", "failed", "applying")),
                    or_(
                        DeploymentSyncState.next_attempt_at.is_(None),
                        DeploymentSyncState.next_attempt_at <= now,
                    ),
                    or_(
                        DeploymentSyncState.state != "applying",
                        DeploymentSyncState.claim_until.is_(None),
                        DeploymentSyncState.claim_until < now,
                    ),
                )
                .order_by(DeploymentSyncState.updated_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if sync is None:
                return None
            desired = session.get(
                DeploymentAssignmentSet, sync.desired_assignment_set_id
            )
            deployment = session.get(SolutionDeployment, sync.deployment_id)
            if desired is None or deployment is None:
                raise RuntimeError("pending synchronization references missing state")
            attempt_number = (
                session.scalar(
                    select(func.max(DeploymentSyncAttempt.attempt_number)).where(
                        DeploymentSyncAttempt.deployment_id == deployment.id,
                        DeploymentSyncAttempt.desired_revision
                        == desired.desired_revision,
                    )
                )
                or 0
            ) + 1
            token = uuid.uuid4()
            operation = ManagementOperation(
                operation_type="synchronize",
                target_type="deployment",
                target_id=deployment.deployment_key,
                idempotency_key=(
                    f"sync:{deployment.id}:{desired.desired_revision}:{attempt_number}"
                ),
                actor=f"worker:{self.worker_id}",
                status="running",
                started_at=now,
            )
            session.add(operation)
            session.flush()
            attempt = DeploymentSyncAttempt(
                deployment_id=deployment.id,
                assignment_set_id=desired.id,
                operation_id=operation.id,
                desired_revision=desired.desired_revision,
                attempt_number=attempt_number,
                claim_token=token,
            )
            session.add(attempt)
            session.flush()
            sync.state = "applying"
            sync.claim_owner = self.worker_id
            sync.claim_token = token
            sync.claim_until = now + timedelta(seconds=self.lease_seconds)
            sync.next_attempt_at = None
            sync.last_error_code = None
            attempt_id = attempt.id
            operation_id = operation.id
            deployment_id = deployment.id
            assignment_set_id = desired.id
        return self._load_work(
            deployment_id,
            assignment_set_id,
            attempt_id,
            operation_id,
            token,
        )

    def _load_work(
        self,
        deployment_id: uuid.UUID,
        assignment_set_id: uuid.UUID,
        attempt_id: uuid.UUID,
        operation_id: uuid.UUID,
        token: uuid.UUID,
    ) -> SyncWorkItem:
        with self.sessions() as session:
            deployment = session.get(SolutionDeployment, deployment_id)
            desired = session.get(DeploymentAssignmentSet, assignment_set_id)
            if deployment is None or desired is None:
                raise RuntimeError("claimed deployment state disappeared")
            bundle_revision = session.get(
                SolutionBundleRevision, desired.bundle_revision_id
            )
            site = session.get(Site, deployment.site_id)
            if bundle_revision is None or site is None:
                raise RuntimeError("claimed bundle or site disappeared")
            cameras: list[WorkCamera] = []
            assignments = session.scalars(
                select(CameraDeploymentAssignment)
                .where(CameraDeploymentAssignment.assignment_set_id == desired.id)
                .order_by(CameraDeploymentAssignment.ordinal)
            ).all()
            for assignment in assignments:
                camera = session.get(Camera, assignment.camera_id)
                profile = session.get(CameraStreamProfile, assignment.stream_profile_id)
                if camera is None or profile is None:
                    raise RuntimeError("assignment camera/profile disappeared")
                endpoint = session.get(CameraEndpoint, profile.endpoint_id)
                if endpoint is None:
                    raise RuntimeError("assignment endpoint disappeared")
                credential_value = None
                if assignment.credential_version_id is not None:
                    credential = session.get(
                        CameraCredentialVersion, assignment.credential_version_id
                    )
                    if credential is None:
                        raise RuntimeError("assignment credential disappeared")
                    credential_value = (
                        credential.id,
                        credential.ciphertext,
                        credential.nonce,
                        credential.key_version,
                        credential.aad_version,
                    )
                apps = session.scalars(
                    select(CameraApplicationAssignment.use_case_key)
                    .where(
                        CameraApplicationAssignment.camera_assignment_id
                        == assignment.id
                    )
                    .order_by(CameraApplicationAssignment.use_case_key)
                ).all()
                cameras.append(
                    WorkCamera(
                        camera_id=camera.id,
                        camera_key=camera.camera_key,
                        profile_id=profile.id,
                        endpoint_scheme=endpoint.scheme,
                        endpoint_host=endpoint.host,
                        endpoint_port=endpoint.port,
                        path=profile.path,
                        fps=assignment.requested_fps,
                        apps=tuple(apps),
                        credential=credential_value,
                    )
                )
            return SyncWorkItem(
                deployment_id=deployment.id,
                deployment_key=deployment.deployment_key,
                namespace=deployment.namespace,
                desired_revision=desired.desired_revision,
                assignment_set_id=desired.id,
                bundle=bundle_revision.canonical_bundle,
                edge_id=site.edge_id,
                attempt_id=attempt_id,
                operation_id=operation_id,
                claim_token=token,
                cameras=tuple(cameras),
            )

    def _secret_inputs(self, work: SyncWorkItem) -> dict[str, Any]:
        desired_cameras = []
        sources: dict[str, str] = {}
        for camera in work.cameras:
            credential = None
            if camera.credential is not None:
                credential = self.keyring.decrypt(
                    camera.camera_id,
                    camera.credential[0],
                    camera.credential[1],
                    camera.credential[2],
                    camera.credential[3],
                    camera.credential[4],
                )
            desired_cameras.append(
                {
                    "camera_id": camera.camera_key,
                    "source": (
                        f"file:/run/secrets/apexfabric/{camera.camera_key}.rtsp"
                    ),
                    "solution_pack": "traffic",
                    "fps": camera.fps,
                    "apps": list(camera.apps),
                }
            )
            sources[camera.camera_key] = build_rtsp_url(camera, credential)
        return {
            "desired_state": {
                "edge_id": work.edge_id,
                "revision": work.desired_revision,
                "cameras": desired_cameras,
            },
            "camera_sources": sources,
        }

    @staticmethod
    def _server_side_secret_manifest(secret_list: dict[str, Any]) -> dict[str, Any]:
        result = json.loads(json.dumps(secret_list))
        for item in result["items"]:
            values = item.pop("stringData", {})
            item["data"] = {
                key: base64.b64encode(value.encode()).decode()
                for key, value in values.items()
            }
        return result

    def _phase(self, work: SyncWorkItem, phase: str) -> None:
        with self.sessions.begin() as session:
            attempt = session.get(DeploymentSyncAttempt, work.attempt_id)
            sync = session.get(DeploymentSyncState, work.deployment_id)
            if (
                attempt is None
                or sync is None
                or attempt.claim_token != work.claim_token
                or sync.claim_token != work.claim_token
            ):
                raise RuntimeError("synchronization lease was lost")
            attempt.phase = phase
            sync.claim_until = utc_now() + timedelta(seconds=self.lease_seconds)

    def run_once(self) -> dict[str, Any] | None:
        work = self.claim()
        if work is None:
            return None
        try:
            inputs = self._secret_inputs(work)
            secret_list = build_camera_secret_list(
                work.bundle, inputs, work.namespace
            )
            if secret_list is None:
                raise RuntimeError("committed assignment bundle has no Secret contract")
            for item in secret_list["items"]:
                item["metadata"].setdefault("annotations", {}).update(
                    {
                        "tvt.apexfabric.com/desired-revision": str(
                            work.desired_revision
                        ),
                        "tvt.apexfabric.com/operation-id": str(work.operation_id),
                    }
                )
            self._phase(work, "applying_secrets")
            manifest = self._server_side_secret_manifest(secret_list)
            self.kubectl.run(
                "apply",
                "--server-side",
                "--field-manager=tvt-camera-sync",
                "--force-conflicts",
                "-f",
                "-",
                input_text=json.dumps(manifest, separators=(",", ":")),
            )
            # Release plaintext-bearing object graphs as soon as kubectl has
            # consumed stdin. Python cannot guarantee physical zeroization.
            del manifest
            del inputs
            configured_secrets = secret_names(secret_list)
            del secret_list
            self._phase(work, "applying_bundle")
            report = reconcile(work.bundle, work.namespace, self.kubectl)
            self._phase(work, "restarting_deployments")
            for deployment in report["observed"]:
                if deployment.get("desired_replicas", 0) <= 0:
                    continue
                name = deployment["name"]
                self._phase(work, "restarting_deployments")
                self.kubectl.run(
                    "rollout",
                    "restart",
                    f"deployment/{name}",
                    "-n",
                    work.namespace,
                )
                self.kubectl.run(
                    "rollout",
                    "status",
                    f"deployment/{name}",
                    "-n",
                    work.namespace,
                    f"--timeout={self.rollout_timeout}s",
                )
                self._phase(work, "restarting_deployments")
            self._record_success(work, report, configured_secrets)
            return {
                "deployment_id": work.deployment_key,
                "desired_revision": work.desired_revision,
                "outcome": "succeeded",
                "configured_secrets": configured_secrets,
            }
        except Exception as error:
            self._record_failure(work, error)
            raise

    def _record_success(
        self,
        work: SyncWorkItem,
        report: dict[str, Any],
        configured_secrets: list[str],
    ) -> None:
        now = utc_now()
        with self.sessions.begin() as session:
            sync = session.get(DeploymentSyncState, work.deployment_id)
            attempt = session.get(DeploymentSyncAttempt, work.attempt_id)
            operation = session.get(ManagementOperation, work.operation_id)
            if sync is None or attempt is None or operation is None:
                raise RuntimeError("synchronization record disappeared")
            if (
                attempt.claim_token != work.claim_token
                or sync.claim_token != work.claim_token
            ):
                raise RuntimeError("stale worker cannot complete synchronization")
            attempt.status = "succeeded"
            attempt.phase = "completed"
            attempt.finished_at = now
            attempt.safe_detail = {
                "bundle_sha256": report.get("revision"),
                "configured_secrets": configured_secrets,
                "resources_applied": len(report.get("applied", [])),
                "resources_removed": len(report.get("removed", [])),
            }
            operation.status = "succeeded"
            operation.finished_at = now
            operation.safe_result = attempt.safe_detail
            sync.applied_assignment_set_id = work.assignment_set_id
            if sync.desired_assignment_set_id == work.assignment_set_id:
                sync.state = "applied"
            else:
                sync.state = "pending"
            sync.claim_owner = None
            sync.claim_token = None
            sync.claim_until = None
            sync.last_error_code = None
            self._store_resource_refs(session, work, report, configured_secrets)

    @staticmethod
    def _store_resource_refs(
        session: Session,
        work: SyncWorkItem,
        report: dict[str, Any],
        configured_secrets: list[str],
    ) -> None:
        references = [
            (item["apiVersion"], item["kind"], item["metadata"]["name"])
            for item in render(work.bundle, work.namespace)
            if item["kind"] != "Namespace"
        ]
        references.extend(("v1", "Secret", name) for name in configured_secrets)
        for api_version, kind, name in sorted(set(references)):
            session.add(
                KubernetesResourceRef(
                    deployment_id=work.deployment_id,
                    sync_attempt_id=work.attempt_id,
                    desired_revision=work.desired_revision,
                    api_version=api_version,
                    kind=kind,
                    namespace=work.namespace,
                    name=name,
                    is_secret=kind == "Secret",
                )
            )

    def _record_failure(self, work: SyncWorkItem, error: Exception) -> None:
        now = utc_now()
        safe_error = redact_text(str(error))
        code = (
            "KUBERNETES_COMMAND_FAILED"
            if isinstance(error, subprocess.CalledProcessError)
            else "SYNCHRONIZATION_FAILED"
        )
        with self.sessions.begin() as session:
            sync = session.get(DeploymentSyncState, work.deployment_id)
            attempt = session.get(DeploymentSyncAttempt, work.attempt_id)
            operation = session.get(ManagementOperation, work.operation_id)
            if sync is None or attempt is None or operation is None:
                return
            if (
                attempt.claim_token != work.claim_token
                or sync.claim_token != work.claim_token
            ):
                return
            retry_delay = min(300, 2 ** min(attempt.attempt_number, 8))
            attempt.status = "failed"
            attempt.finished_at = now
            attempt.error_code = code
            attempt.safe_detail = {"error": safe_error}
            attempt.retry_at = now + timedelta(seconds=retry_delay)
            operation.status = "failed"
            operation.finished_at = now
            operation.error_code = code
            operation.safe_result = {"error": safe_error}
            if sync.desired_assignment_set_id == work.assignment_set_id:
                sync.state = "failed"
                sync.next_attempt_at = attempt.retry_at
                sync.last_error_code = code
            else:
                sync.state = "pending"
                sync.next_attempt_at = None
            sync.claim_owner = None
            sync.claim_token = None
            sync.claim_until = None
