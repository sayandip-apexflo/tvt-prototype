"""Lease, materialize and idempotently apply committed desired revisions."""

from __future__ import annotations

import base64
import copy
import json
import re
import subprocess
import time
import uuid
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any
from urllib.parse import quote, urlencode

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from apexfabric.solution_management.renderer import Kubectl, reconcile, render, revision
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
from tvt_runtime.camera_secrets import RUNTIME_CONTRACTS, build_camera_secret_list, secret_names


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
    configuration: dict[str, Any]
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
    previous: AppliedState | None


@dataclass(frozen=True)
class AppliedState:
    desired_revision: int
    assignment_set_id: uuid.UUID
    bundle: dict[str, Any]
    cameras: tuple[WorkCamera, ...]


def _bundle_image_references(bundle: dict[str, Any]) -> tuple[str, ...]:
    references = []
    for application in bundle.get("applications", []):
        image = application.get("image", {})
        repository = image.get("repository")
        digest = image.get("digest")
        tag = image.get("tag")
        if repository and digest:
            references.append(f"{repository}@{digest}")
        elif repository and tag:
            references.append(f"{repository}:{tag}")
    return tuple(sorted(references))


class NodeImagePreflight:
    """Verify an immutable catalog image digest is already cached on a node.

    ``tvt-camera-sync`` runs as the unprivileged ``tvt-edge`` service
    account, which has no path to the k3s containerd socket or crictl config
    (both are root-only, and granting that access — via sudo or a Linux
    capability — would widen this hardened service's privileges just for a
    preflight check). Every Node object's ``status.images`` already lists
    what containerd has cached, populated by kubelet directly from the
    runtime, and it's readable through the same RBAC-scoped kubeconfig this
    worker already uses for every other kubectl call (``list nodes`` is
    already granted; see ``install-tvt-kubeconfig``).

    Catalog images are pulled into containerd by the root-run
    ``tvt-pipeline-image-sync`` service ahead of cataloging, so by the time a
    deployment can be committed the digest is normally already present. If
    it somehow isn't, this fails fast here — matching the previous
    preflight's fail-fast intent — rather than leaving a Pod stuck in
    ImagePullBackOff with no clear signal. Kubelet's own
    ``imagePullPolicy: IfNotPresent`` remains the actual pull mechanism once
    the bundle is applied; this class only ever reads, never pulls.
    """

    def __init__(self, kubectl: Kubectl):
        self.kubectl = kubectl

    def __call__(self, image_reference: str) -> None:
        result = self.kubectl.run("get", "nodes", "-o", "json")
        nodes = json.loads(result.stdout)
        for node in nodes.get("items", []):
            names = {
                name
                for image in node.get("status", {}).get("images", [])
                for name in image.get("names", [])
            }
            if image_reference in names:
                return
        raise RuntimeError(f"image {image_reference} is not yet cached on any node")


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
        live_reload_timeout: int = 0,
        lease_seconds: int = 600,
        image_puller: Any | None = None,
    ) -> None:
        self.sessions = sessions
        self.keyring = keyring
        self.kubectl = kubectl
        self.worker_id = worker_id
        self.rollout_timeout = rollout_timeout
        self.live_reload_timeout = max(0, min(live_reload_timeout, 60))
        self.image_puller = image_puller
        # A rollout is a blocking kubectl call, so the claim must remain fenced
        # for the entire rollout plus enough time to persist its result.
        self.lease_seconds = max(lease_seconds, rollout_timeout + 120)

    @staticmethod
    def _camera_source_signature(cameras: tuple[WorkCamera, ...]) -> tuple[Any, ...]:
        """Non-secret identity of every mounted camera source.

        A changed profile or credential version requires a Pod restart because
        camera URLs use Kubernetes Secret ``subPath`` mounts. App selection,
        geometry and FPS deliberately do not participate: the runtime can
        reload those fields from its projected desired-state ConfigMap.
        """

        return tuple(
            (
                camera.camera_key,
                camera.profile_id,
                camera.endpoint_scheme,
                camera.endpoint_host,
                camera.endpoint_port,
                camera.path,
                camera.credential[0] if camera.credential is not None else None,
            )
            for camera in cameras
        )

    def _is_live_reload_candidate(self, work: SyncWorkItem) -> bool:
        def live_signature(bundle: dict[str, Any]) -> str:
            normalized = copy.deepcopy(bundle)
            normalized.get("configuration", {}).pop("desired_state_sha256", None)
            return revision(normalized)

        return bool(
            self.live_reload_timeout
            and work.previous is not None
            and len(work.bundle.get("applications", [])) == 1
            and live_signature(work.bundle) == live_signature(work.previous.bundle)
            and self._camera_source_signature(work.cameras)
            == self._camera_source_signature(work.previous.cameras)
        )

    @staticmethod
    def _live_reload_report(work: SyncWorkItem) -> dict[str, Any]:
        """Report the resource-stable subset used by ConfigMap live reload.

        The only bundle difference allowed by `_is_live_reload_candidate` is
        `configuration.desired_state_sha256`, whose historical purpose was to
        force the renderer's Pod-template digest to change. Applying that
        template would race the live reload with a rollout, so this path
        deliberately leaves bundle-owned Kubernetes objects unchanged after
        the separately validated desired-state ConfigMap has been applied.
        """

        observed = []
        for application in work.bundle.get("applications", []):
            desired_state = application.get("lifecycle", {}).get(
                "desired_state", "Running"
            )
            observed.append(
                {
                    "name": f"{work.deployment_key}-{application['name']}",
                    "desired_replicas": (
                        application.get("replicas", 1)
                        if desired_state == "Running"
                        else 0
                    ),
                }
            )
        return {
            "revision": revision(work.bundle),
            "applied": [],
            "removed": [],
            "observed": observed,
        }

    @staticmethod
    def _runtime_readiness_contract(
        work: SyncWorkItem, deployment_name: str
    ) -> tuple[str, int, str, str] | None:
        prefix = f"{work.deployment_key}-"
        if not deployment_name.startswith(prefix):
            return None
        app_name = deployment_name[len(prefix) :]
        app = next(
            (
                item
                for item in work.bundle.get("applications", [])
                if item.get("name") == app_name
            ),
            None,
        )
        if app is None:
            return None
        readiness = app.get("health", {}).get("readiness", {})
        port_name = readiness.get("port")
        port = next(
            (
                item.get("container_port")
                for item in app.get("ports", [])
                if item.get("name") == port_name
            ),
            None,
        )
        path = readiness.get("path")
        if not isinstance(port, int) or not isinstance(path, str) or not path.startswith("/"):
            return None
        selector = (
            f"apexfabric.com/deployment-id={work.deployment_key},"
            f"apexfabric.com/application={app_name}"
        )
        return app_name, port, path, selector

    def _wait_for_runtime_revision(
        self, work: SyncWorkItem, deployment_name: str
    ) -> bool:
        """Wait for the running Pod to acknowledge the desired-state revision.

        The readiness response is bounded metadata (status + revision). Raw
        analytics events and camera sources never cross this path. Any proxy,
        schema or timeout failure returns False so the caller falls back to
        the established controlled rollout.
        """

        contract = self._runtime_readiness_contract(work, deployment_name)
        if contract is None:
            return False
        _app_name, port, path, selector = contract
        deadline = time.monotonic() + self.live_reload_timeout
        nudged_pods: set[str] = set()
        while time.monotonic() < deadline:
            try:
                pods_result = self.kubectl.run(
                    "get",
                    "pods",
                    "-n",
                    work.namespace,
                    "-l",
                    selector,
                    "-o",
                    "json",
                )
                pods = json.loads(pods_result.stdout).get("items", [])
                pods.sort(
                    key=lambda item: item.get("metadata", {}).get(
                        "creationTimestamp", ""
                    ),
                    reverse=True,
                )
                pod = next(
                    (
                        item
                        for item in pods
                        if item.get("status", {}).get("phase") == "Running"
                    ),
                    None,
                )
                if pod is not None:
                    pod_name = pod["metadata"]["name"]
                    if pod_name not in nudged_pods:
                        # Updating Pod metadata schedules an immediate kubelet
                        # sync, avoiding the normal projected-ConfigMap cache
                        # delay without changing the Pod template or UID.
                        self.kubectl.run(
                            "patch",
                            "pod",
                            pod_name,
                            "-n",
                            work.namespace,
                            "--type=merge",
                            "-p",
                            json.dumps(
                                {
                                    "metadata": {
                                        "annotations": {
                                            "tvt.apexfabric.com/desired-revision": str(
                                                work.desired_revision
                                            )
                                        }
                                    }
                                },
                                separators=(",", ":"),
                            ),
                        )
                        nudged_pods.add(pod_name)
                    proxy_path = (
                        f"/api/v1/namespaces/{work.namespace}/pods/"
                        f"http:{pod_name}:{port}/proxy{path}"
                    )
                    response = self.kubectl.run("get", "--raw", proxy_path)
                    payload = json.loads(response.stdout)
                    if (
                        payload.get("status") == "ready"
                        and payload.get("revision") == work.desired_revision
                    ):
                        return True
            except (KeyError, TypeError, ValueError, json.JSONDecodeError, subprocess.CalledProcessError):
                pass
            time.sleep(0.5)
        return False

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
            sync = session.get(DeploymentSyncState, deployment.id)

            def load_cameras(snapshot: DeploymentAssignmentSet) -> tuple[WorkCamera, ...]:
                cameras: list[WorkCamera] = []
                assignments = session.scalars(
                select(CameraDeploymentAssignment)
                .where(CameraDeploymentAssignment.assignment_set_id == snapshot.id)
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
                    app_assignments = session.scalars(
                        select(CameraApplicationAssignment)
                        .where(
                            CameraApplicationAssignment.camera_assignment_id
                            == assignment.id
                        )
                        .order_by(CameraApplicationAssignment.use_case_key)
                    ).all()
                    apps = tuple(item.use_case_key for item in app_assignments)
                    configurations = {
                        json.dumps(item.configuration, sort_keys=True, separators=(",", ":"))
                        for item in app_assignments
                    }
                    if len(configurations) > 1:
                        raise RuntimeError("camera applications have inconsistent geometry")
                    configuration = (
                        json.loads(next(iter(configurations))) if configurations else {}
                    )
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
                            apps=apps,
                            configuration=configuration,
                            credential=credential_value,
                        )
                    )
                return tuple(cameras)

            cameras = load_cameras(desired)
            previous = None
            if sync is not None and sync.applied_assignment_set_id is not None:
                applied = session.get(
                    DeploymentAssignmentSet, sync.applied_assignment_set_id
                )
                if applied is None:
                    raise RuntimeError("applied assignment state disappeared")
                applied_bundle = session.get(
                    SolutionBundleRevision, applied.bundle_revision_id
                )
                if applied_bundle is None:
                    raise RuntimeError("applied bundle state disappeared")
                previous = AppliedState(
                    desired_revision=applied.desired_revision,
                    assignment_set_id=applied.id,
                    bundle=applied_bundle.canonical_bundle,
                    cameras=load_cameras(applied),
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
                cameras=cameras,
                previous=previous,
            )

    def _secret_inputs(self, work: SyncWorkItem) -> dict[str, Any]:
        input_contract = work.bundle.get("configuration", {}).get("secret_input_contract")
        solution_pack = RUNTIME_CONTRACTS.get(input_contract, (None, None))[0] or "traffic"
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
            desired_camera = {
                    "camera_id": camera.camera_key,
                    "source": (
                        f"file:/run/secrets/apexfabric/{camera.camera_key}.rtsp"
                    ),
                    "solution_pack": solution_pack,
                    "fps": camera.fps,
                    "apps": list(camera.apps),
                }
            if camera.configuration:
                desired_camera["config"] = camera.configuration
            desired_cameras.append(desired_camera)
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
            # Secrets carry write-only stringData -> base64 data for server-side
            # apply. ConfigMaps carry plain data and must not be base64-encoded.
            if item.get("kind") == "Secret":
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

    def _preflight_images(self, work: SyncWorkItem) -> None:
        if not work.bundle.get("configuration", {}).get("catalog_id"):
            return
        if self.image_puller is None:
            raise RuntimeError("catalog image pull preflight is not configured")
        references = []
        for application in work.bundle["applications"]:
            image = application["image"]
            digest = image.get("digest")
            if not isinstance(digest, str) or not re.fullmatch(
                r"sha256:[0-9a-f]{64}", digest
            ):
                raise RuntimeError("catalog bundle image is not immutable")
            references.append(f"{image['repository']}@{digest}")
        self._phase(work, "pulling_images")
        for reference in sorted(set(references)):
            self.image_puller(reference)

    def _restore_previous(self, work: SyncWorkItem) -> None:
        if work.previous is None:
            return
        previous = replace(
            work,
            desired_revision=work.previous.desired_revision,
            assignment_set_id=work.previous.assignment_set_id,
            bundle=work.previous.bundle,
            cameras=work.previous.cameras,
        )
        self._phase(work, "restoring_previous")
        inputs = self._secret_inputs(previous)
        secret_list = build_camera_secret_list(
            previous.bundle, inputs, previous.namespace
        )
        if secret_list is None:
            raise RuntimeError("previous bundle has no Secret contract")
        for item in secret_list["items"]:
            item["metadata"].setdefault("annotations", {}).update(
                {
                    "tvt.apexfabric.com/desired-revision": str(
                        previous.desired_revision
                    ),
                    "tvt.apexfabric.com/operation-id": str(work.operation_id),
                    "tvt.apexfabric.com/recovery": "previous-applied",
                }
            )
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
        report = reconcile(previous.bundle, previous.namespace, self.kubectl)
        for deployment in report["observed"]:
            if deployment.get("desired_replicas", 0) <= 0:
                continue
            name = deployment["name"]
            self.kubectl.run(
                "rollout", "restart", f"deployment/{name}", "-n", previous.namespace
            )
            self.kubectl.run(
                "rollout",
                "status",
                f"deployment/{name}",
                "-n",
                previous.namespace,
                f"--timeout={self.rollout_timeout}s",
            )

    def run_once(self) -> dict[str, Any] | None:
        work = self.claim()
        if work is None:
            return None
        mutation_started = False
        recovery_error: Exception | None = None
        recovered = False
        image_changed = bool(
            work.previous is not None
            and _bundle_image_references(work.bundle)
            != _bundle_image_references(work.previous.bundle)
        )
        try:
            self._preflight_images(work)
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
            mutation_started = True
            # Release plaintext-bearing object graphs as soon as kubectl has
            # consumed stdin. Python cannot guarantee physical zeroization.
            del manifest
            del inputs
            configured_secrets = secret_names(secret_list)
            del secret_list
            live_reload = self._is_live_reload_candidate(work)
            self._phase(work, "applying_bundle")
            report = (
                self._live_reload_report(work)
                if live_reload
                else reconcile(work.bundle, work.namespace, self.kubectl)
            )
            bundle_changed = bool(
                work.previous is None
                or revision(work.bundle) != revision(work.previous.bundle)
            )
            for deployment in report["observed"]:
                if deployment.get("desired_replicas", 0) <= 0:
                    continue
                name = deployment["name"]
                if live_reload:
                    self._phase(work, "waiting_runtime_configuration")
                    if self._wait_for_runtime_revision(work, name):
                        continue
                    # The runtime did not acknowledge the ConfigMap update.
                    # Apply the full bundle now so its Pod-template digest
                    # drives the established rollout fallback.
                    report = reconcile(work.bundle, work.namespace, self.kubectl)
                if bundle_changed:
                    # Server-side apply already changed the Pod template (or
                    # created it for the first time). Wait for that rollout;
                    # issuing `rollout restart` here would create a second
                    # ReplicaSet for the same desired bundle.
                    self._phase(work, "waiting_deployment_rollout")
                    self.kubectl.run(
                        "rollout",
                        "status",
                        f"deployment/{name}",
                        "-n",
                        work.namespace,
                        f"--timeout={self.rollout_timeout}s",
                    )
                    continue
                # Camera-source Secrets use subPath mounts and cannot refresh
                # in-place. This is also the safe fallback when a runtime does
                # not acknowledge live desired-state reload in time.
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
            self._record_success(work, report, configured_secrets)
            return {
                "deployment_id": work.deployment_key,
                "desired_revision": work.desired_revision,
                "outcome": "succeeded",
                "configured_secrets": configured_secrets,
            }
        except Exception as error:
            if mutation_started and work.previous is not None:
                try:
                    self._restore_previous(work)
                    recovered = True
                except Exception as restore_error:
                    recovery_error = restore_error
            self._record_failure(
                work,
                error,
                recovered=recovered,
                recovery_error=recovery_error,
                operator_required=recovered and image_changed,
            )
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
        for name in configured_secrets:
            # Input List at 5ada504 is ConfigMap <deployment>-desired-state
            # (non-secret desired state) + Secret <deployment>-camera-sources
            # (write-only RTSP URLs). Record kinds exactly; never store bodies.
            kind = "ConfigMap" if name.endswith("-desired-state") else "Secret"
            references.append(("v1", kind, name))
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

    def _record_failure(
        self,
        work: SyncWorkItem,
        error: Exception,
        *,
        recovered: bool = False,
        recovery_error: Exception | None = None,
        operator_required: bool = False,
    ) -> None:
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
            attempt.safe_detail = {
                "error": safe_error,
                "previous_applied_bundle_restored": recovered,
            }
            if recovery_error is not None:
                attempt.safe_detail["recovery_error"] = redact_text(
                    str(recovery_error)
                )
            attempt.retry_at = (
                None if operator_required else now + timedelta(seconds=retry_delay)
            )
            operation.status = "failed"
            operation.finished_at = now
            operation.error_code = code
            operation.safe_result = copy.deepcopy(attempt.safe_detail)
            if sync.desired_assignment_set_id == work.assignment_set_id:
                sync.state = "operator_required" if operator_required else "failed"
                sync.next_attempt_at = attempt.retry_at
                sync.last_error_code = code
            else:
                sync.state = "pending"
                sync.next_attempt_at = None
            sync.claim_owner = None
            sync.claim_token = None
            sync.claim_until = None
