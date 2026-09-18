"""Pull-based mirror: apexfabric-control's camera inventory -> tvt-edge's DB.

apexfabric-control (an optional, separately-installed add-on) owns its own
camera store -- a ConfigMap of non-secret metadata and a Secret of RTSP
source URLs, both in the "apexfabric" namespace. tvt-edge's own deployment
pipeline (``ManagementService.commit_assignments`` /
``commit_catalog_deployment``) requires a camera to already exist in
tvt-edge's ``cameras`` table, with a selected stream profile and an active
credential, before it can be assigned to any deployment. This worker is the
only bridge between the two stores.

It is intentionally passive:

* It never assigns a synced camera to a deployment/role. apexfabric's
  inventory carries no app or fps policy to derive that from; assignment
  stays an explicit operator call to ``/api/v1/deployments/{id}/assignments``.
* It never commits a new deployment revision itself. Rotating an
  already-assigned camera's credential does automatically requeue that
  camera's existing deployments for resync (``ManagementService.
  rotate_credentials`` -> ``_requeue_credential_consumers``) -- that is
  pre-existing behavior of credential rotation itself, not something this
  worker adds, and it only replays the current assignment with the new
  secret rather than authoring a new one.
* It only ever deletes a tvt-edge camera that this worker itself would have
  created (one no longer present in apexfabric's inventory), and only when
  ``delete_camera``'s own in-use guard allows it.

Constants below intentionally duplicate the names in
``apexfabric/control_plane/server.py`` (``CAMERA_INVENTORY_CONFIG_MAP`` /
``CAMERA_INVENTORY_SECRET``) rather than importing them, so the base
tvt-edge install has no import-time dependency on the optional add-on
package. If apexfabric-control isn't installed, both lookups return
not-found and this worker is a no-op every cycle.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from sqlalchemy.orm import Session, sessionmaker

from apexfabric.solution_management.renderer import Kubectl
from tvt_edge.security import CredentialKeyring, redact_text
from tvt_edge.service import ManagementService

logger = logging.getLogger("tvt_edge.camera_inventory_sync")

NAMESPACE = "apexfabric"
CONFIG_MAP_NAME = "apexfabric-camera-inventory"
SECRET_NAME = "apexfabric-camera-sources"
PROFILE_TOKEN = "apexfabric-inventory"
ACTOR = "apexfabric-inventory-sync"


@dataclass(frozen=True)
class InventoryCamera:
    camera_id: str
    name: str
    rtsp_url: str | None


def _content_hash(document: dict[str, Any]) -> str:
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


def _split_source(url: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split an apexfabric camera source URL into a stream shape and a credential document.

    apexfabric's RTSP URLs may embed ``user:pass@`` and a query string;
    tvt-edge keeps those out of the non-secret stream profile and stores
    them only in the encrypted credential document.
    """
    parsed = urlsplit(url)
    if parsed.scheme not in {"rtsp", "rtsps"} or not parsed.hostname:
        raise ValueError("camera source is not a valid rtsp(s) URL")
    path = parsed.path or "/"
    if any(character in path for character in "?#@"):
        raise ValueError("camera source path is not representable")
    stream = {
        "scheme": parsed.scheme,
        "host": parsed.hostname,
        "port": parsed.port or 554,
        "path": path,
    }
    credential: dict[str, Any] = {}
    if parsed.username:
        credential["username"] = unquote(parsed.username)
    if parsed.password:
        credential["password"] = unquote(parsed.password)
    if parsed.query:
        credential["query"] = dict(parse_qsl(parsed.query))
    return stream, credential


class CameraInventorySyncWorker:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        keyring: CredentialKeyring,
        kubectl: Kubectl,
    ) -> None:
        self.service = ManagementService(sessions, keyring)
        self.kubectl = kubectl

    def run_once(self) -> dict[str, Any]:
        cameras = self._read_inventory()
        result = {
            "observed": len(cameras),
            "created": 0,
            "configured": 0,
            "credentials_rotated": 0,
            "enabled": 0,
            "deleted": 0,
            "skipped": 0,
        }
        seen: set[str] = set()
        for camera in cameras:
            seen.add(camera.camera_id)
            try:
                self._reconcile_camera(camera, result)
            except ValueError as error:
                result["skipped"] += 1
                logger.warning(
                    "camera inventory sync skipped a camera",
                    extra={
                        "event": "camera_inventory_sync_skipped",
                        "camera_id": camera.camera_id,
                        "reason": redact_text(str(error)),
                    },
                )
        self._reconcile_deletions(seen, result)
        return result

    def _read_inventory(self) -> list[InventoryCamera]:
        metadata = self.kubectl.run(
            "get", "configmap", CONFIG_MAP_NAME, "-n", NAMESPACE, "-o", "json", check=False
        )
        entries: list[Any] = []
        if metadata.returncode == 0:
            raw = json.loads(metadata.stdout).get("data", {}).get("cameras.json", "[]")
            parsed = json.loads(raw)
            entries = parsed if isinstance(parsed, list) else []
        secret = self.kubectl.run(
            "get", "secret", SECRET_NAME, "-n", NAMESPACE, "-o", "json", check=False
        )
        sources: dict[str, str] = {}
        if secret.returncode == 0:
            for key, value in json.loads(secret.stdout).get("data", {}).items():
                if key.endswith(".rtsp"):
                    sources[key[: -len(".rtsp")]] = base64.b64decode(value).decode("utf-8")
        cameras: list[InventoryCamera] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            camera_id, name = entry.get("camera_id"), entry.get("name")
            if not isinstance(camera_id, str) or not isinstance(name, str):
                continue
            cameras.append(
                InventoryCamera(camera_id=camera_id, name=name, rtsp_url=sources.get(camera_id))
            )
        return cameras

    def _reconcile_camera(self, camera: InventoryCamera, result: dict[str, Any]) -> None:
        try:
            view = self.service.get_camera(camera.camera_id)
        except ValueError:
            view = None

        request_prefix = f"apexfabric-inventory-sync:{camera.camera_id}"

        if view is None:
            self.service.create_camera(
                camera_key=camera.camera_id,
                friendly_name=camera.name,
                manufacturer=None,
                model=None,
                identifiers=[],
                actor=ACTOR,
                request_id=f"{request_prefix}:create",
            )
            result["created"] += 1
            view = {"enabled": False, "selected_profile": None}

        if camera.rtsp_url is None:
            return

        stream, credential = _split_source(camera.rtsp_url)
        current_profile = view.get("selected_profile")
        stream_changed = current_profile is None or any(
            current_profile.get(field) != stream[field] for field in stream
        )
        if stream_changed:
            self.service.configure_stream(
                camera.camera_id,
                scheme=stream["scheme"],
                host=stream["host"],
                port=stream["port"],
                path=stream["path"],
                profile_token=PROFILE_TOKEN,
                transport="tcp",
                codec=None,
                width=None,
                height=None,
                fps=None,
                actor=ACTOR,
                request_id=f"{request_prefix}:stream:{_content_hash(stream)}",
            )
            result["configured"] += 1

        if credential:
            # rotate_credentials is idempotent by request_id: an unchanged
            # credential document (same content hash) is a no-op here, so
            # this count includes checks, not just actual rotations.
            self.service.rotate_credentials(
                camera.camera_id,
                credential,
                ACTOR,
                f"{request_prefix}:credentials:{_content_hash(credential)}",
            )
            result["credentials_rotated"] += 1

        if not view.get("enabled"):
            self.service.set_camera_enabled(
                camera.camera_id, True, ACTOR, f"{request_prefix}:enable"
            )
            result["enabled"] += 1

    def _reconcile_deletions(self, seen: set[str], result: dict[str, Any]) -> None:
        for view in self.service.list_cameras():
            camera_id = view["camera_id"]
            if camera_id in seen:
                continue
            try:
                self.service.delete_camera(
                    camera_id, ACTOR, f"apexfabric-inventory-sync:{camera_id}:delete"
                )
                result["deleted"] += 1
            except ValueError as error:
                result["skipped"] += 1
                logger.warning(
                    "camera inventory sync could not delete a removed camera",
                    extra={
                        "event": "camera_inventory_sync_delete_skipped",
                        "camera_id": camera_id,
                        "reason": redact_text(str(error)),
                    },
                )
