#!/usr/bin/env bash
# Shared, side-effect-free helpers for the two TVT edge installers.

TVT_INSTALL_STATE_ROOT="${TVT_INSTALL_STATE_ROOT:-/var/lib/tvt/install}"
TVT_INSTALL_LOCK="${TVT_INSTALL_LOCK:-/run/lock/tvt-edge-install.lock}"

tvt_log() {
  printf '%s tvt-installer: %s\n' "$(date --utc +'%Y-%m-%dT%H:%M:%SZ')" "$*"
}

tvt_fail() {
  tvt_log "ERROR: $*" >&2
  exit 1
}

tvt_require_root() {
  [[ ${EUID} -eq 0 ]] || tvt_fail "run this command as root (for example, with sudo)"
}

tvt_require_value() {
  local option="$1" value="${2:-}"
  [[ -n ${value} ]] || tvt_fail "${option} requires a value"
}

tvt_canonical_directory() {
  local path="$1"
  [[ -d ${path} && ! -L ${path} ]] || tvt_fail "directory does not exist or is a symlink: ${path}"
  (cd "${path}" && pwd -P)
}

tvt_acquire_lock() {
  local lock_directory
  lock_directory="$(dirname "${TVT_INSTALL_LOCK}")"
  install -d -m 0755 "${lock_directory}"
  exec 8>"${TVT_INSTALL_LOCK}"
  flock --wait 0 8 || tvt_fail "another TVT host installation command is running"
}

tvt_manifest_value() {
  local bundle="$1" key="$2"
  python3 - "${bundle}/manifest.json" "${key}" <<'PY'
import json
import pathlib
import sys

value = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
for part in sys.argv[2].split("."):
    if not isinstance(value, dict) or part not in value:
        raise SystemExit(f"manifest key is missing: {sys.argv[2]}")
    value = value[part]
if isinstance(value, bool):
    print(str(value).lower())
elif isinstance(value, (str, int)):
    print(value)
else:
    raise SystemExit(f"manifest key is not a scalar: {sys.argv[2]}")
PY
}

tvt_verify_bundle() {
  local bundle="$1"
  [[ -f ${bundle}/manifest.json ]] || tvt_fail "release manifest is missing: ${bundle}/manifest.json"
  [[ -f ${bundle}/checksums.sha256 ]] || tvt_fail "release checksums are missing: ${bundle}/checksums.sha256"
  python3 - "${bundle}" <<'PY' || tvt_fail "release bundle verification failed"
import hashlib
import json
import os
import pathlib
import re
import sys

root = pathlib.Path(sys.argv[1]).resolve()
manifest_path = root / "manifest.json"
checksums_path = root / "checksums.sha256"
try:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as error:
    raise SystemExit(f"invalid release manifest: {error}")
expected = {
    "schema_version": 1,
    "bundle_contract": 1,
    "product": "tvt-edge",
    "architecture": "amd64",
    "os_id": "ubuntu",
    "os_version": "24.04",
}
for key, value in expected.items():
    if manifest.get(key) != value:
        raise SystemExit(f"manifest {key} must be {value!r}")
version = manifest.get("release_version")
if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", version):
    raise SystemExit("manifest release_version is invalid")
artifacts = manifest.get("artifacts")
required = {
    "application_wheel", "registry_image", "node_reporter_image",
    "node_status_controller_image", "traffic_image", "k3s_installer", "k3s_binary",
}
if not isinstance(artifacts, dict) or required - artifacts.keys():
    raise SystemExit("manifest artifacts section is incomplete")

def safe_relative(raw: str) -> pathlib.PurePosixPath:
    if not isinstance(raw, str) or not raw:
        raise SystemExit("manifest and checksum paths must be non-empty strings")
    path = pathlib.PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts or str(path) in {".", "checksums.sha256"}:
        raise SystemExit(f"unsafe release path: {raw!r}")
    return path

for raw in artifacts.values():
    path = root.joinpath(*safe_relative(raw).parts)
    if not path.is_file() or path.is_symlink():
        raise SystemExit(f"manifest artifact is missing or unsafe: {raw}")
required_resources = {
    "prepare-tvt-edge-host.sh", "install-tvt-edge-host.sh", "alembic.ini",
    "config/platform.env", "config/pipeline.env",
    "scripts/lib/tvt-installer-common.sh",
    "scripts/install-tvt-hardware-drivers.sh", "scripts/install-local-registry.sh",
    "scripts/install-k3s-single-node.sh", "scripts/publish-control-images.sh",
    "scripts/install-k3s-plane.sh", "scripts/verify-k3s-plane.sh",
    "scripts/import-pipeline-traffic-image.sh", "scripts/verify-pipeline-image-inspect.py",
    "scripts/verify-pipeline-image-sync.sh", "scripts/install-pipeline-image-sync.sh",
    "scripts/bootstrap-postgresql.sh", "scripts/install-tvt-kubeconfig.sh",
    "scripts/install-traffic-qualification.sh",
    "deploy/k8s/apexfabric-foundation.yaml", "deploy/k8s/apexfabric-node-management.yaml",
    "deploy/host/tvt-edge.env.example", "deploy/host/postgresql-tvt.conf",
    "deploy/systemd/tvt-edge.service", "deploy/systemd/tvt-camera-sync.service",
    "solution-packs/schema/deployment-bundle.schema.json",
    "solution-packs/catalog/traffic-edge-runtime-2026.08.21-v4/provenance.json",
    "solution-packs/catalog/traffic-edge-runtime-2026.08.21-v4/image-contract.yaml",
    "tvt_edge/db/migrations/env.py",
}
for relative in sorted(required_resources):
    path = root / relative
    if not path.is_file() or path.is_symlink():
        raise SystemExit(f"required release resource is missing or unsafe: {relative}")
if not any((root / "packages/apt").glob("*.deb")):
    raise SystemExit("release contains no offline APT packages")
for relative in ("hardware/driver-recipe.json", "hardware/linux-npu-driver.tar.gz"):
    if not (root / relative).is_file(): raise SystemExit(f"required release resource is missing: {relative}")
if not any((root / "hardware/wheels").glob("*.whl")):
    raise SystemExit("release contains no OpenVINO wheels")

listed: dict[str, str] = {}
for number, line in enumerate(checksums_path.read_text(encoding="utf-8").splitlines(), 1):
    match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
    if not match:
        raise SystemExit(f"invalid checksum line {number}")
    digest, raw = match.groups()
    relative = safe_relative(raw).as_posix()
    if relative in listed:
        raise SystemExit(f"duplicate checksum entry: {relative}")
    listed[relative] = digest

actual_files = set()
for path in root.rglob("*"):
    if path.is_symlink():
        raise SystemExit(f"release bundle contains a symlink: {path.relative_to(root)}")
    if path.is_file() and path != checksums_path:
        actual_files.add(path.relative_to(root).as_posix())
if set(listed) != actual_files:
    missing = sorted(actual_files - set(listed))
    extra = sorted(set(listed) - actual_files)
    raise SystemExit(f"checksum coverage mismatch; unlisted={missing}, missing={extra}")
for relative, expected_digest in listed.items():
    digest = hashlib.sha256()
    with (root / relative).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != expected_digest:
        raise SystemExit(f"checksum mismatch: {relative}")
PY
  tvt_log "verified release manifest and checksums"
}

tvt_json_get() {
  local file="$1" key="$2"
  [[ -f ${file} ]] || return 1
  python3 - "${file}" "${key}" <<'PY'
import json, pathlib, sys
try:
    value = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
    for part in sys.argv[2].split("."):
        value = value[part]
except (OSError, json.JSONDecodeError, KeyError, TypeError):
    raise SystemExit(1)
if isinstance(value, bool): print(str(value).lower())
elif value is not None: print(value)
PY
}

tvt_write_state() {
  local file="$1" status="$2" release_version="$3" stage="${4:-}" boot_id="${5:-}"
  install -d -m 0750 "$(dirname "${file}")"
  local temporary
  temporary="$(mktemp "$(dirname "${file}")/.state.XXXXXX")"
  python3 - "${temporary}" "${status}" "${release_version}" "${stage}" "${boot_id}" <<'PY'
import datetime, json, pathlib, sys
output, status, release, stage, boot_id = sys.argv[1:]
document = {
    "schema_version": 1,
    "status": status,
    "release_version": release,
    "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
}
if stage: document["stage"] = stage
if boot_id: document["driver_install_boot_id"] = boot_id
if status == "prepared": document["reboot_verified"] = True
pathlib.Path(output).write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
  chmod 0640 "${temporary}"
  mv -f -- "${temporary}" "${file}"
}

tvt_stage_completed() {
  local state_file="$1" stage="$2"
  [[ "$(tvt_json_get "${state_file}" "stages.${stage}.status" 2>/dev/null || true)" == completed ]]
}

tvt_record_stage() {
  local state_file="$1" release="$2" stage="$3" result="$4"
  install -d -m 0750 "$(dirname "${state_file}")"
  local temporary
  temporary="$(mktemp "$(dirname "${state_file}")/.install-state.XXXXXX")"
  python3 - "${state_file}" "${temporary}" "${release}" "${stage}" "${result}" <<'PY'
import datetime, json, pathlib, sys
source, output, release, stage, result = sys.argv[1:]
try: document = json.loads(pathlib.Path(source).read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError): document = {}
now = datetime.datetime.now(datetime.timezone.utc).isoformat()
document.update({"schema_version": 1, "release_version": release, "status": "installing", "updated_at": now})
entry = document.setdefault("stages", {}).setdefault(stage, {})
entry.update({"status": result, "updated_at": now})
if result == "completed" and "completed_at" not in entry: entry["completed_at"] = now
pathlib.Path(output).write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
  chmod 0640 "${temporary}"
  mv -f -- "${temporary}" "${state_file}"
}

tvt_run_stage() {
  local state_file="$1" release="$2" stage="$3"
  shift 3
  if tvt_stage_completed "${state_file}" "${stage}"; then
    tvt_log "stage ${stage}: already completed"
    return 0
  fi
  tvt_log "stage ${stage}: starting"
  tvt_record_stage "${state_file}" "${release}" "${stage}" running
  # Run outside an `if` condition so Bash does not suppress errexit inside a
  # worker function. The parent temporarily owns the exit status so it can
  # persist failure evidence before returning it to the operator entry point.
  set +e
  ( set -Eeuo pipefail; "$@" )
  local result=$?
  set -e
  if (( result == 0 )); then
    tvt_record_stage "${state_file}" "${release}" "${stage}" completed
    tvt_log "stage ${stage}: completed"
  else
    tvt_record_stage "${state_file}" "${release}" "${stage}" failed
    tvt_log "stage ${stage}: failed" >&2
    return "${result}"
  fi
}
