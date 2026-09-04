#!/usr/bin/env bash
set -Eeuo pipefail
umask 027

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

OUTPUT_DIRECTORY="${REPO_ROOT}/build/online-test-kit/output"
CACHE_DIRECTORY="${REPO_ROOT}/build/online-test-kit/cache"
SOURCE_COMMIT=""
ALLOW_DIRTY_SOURCE=false
SKIP_SOURCE_TESTS=false

usage() {
  cat >&2 <<'EOF'
usage: scripts/make-tvt-online-test-kit.sh [options]

Build a checksum-complete transfer archive for an online, experimental TVT
installation. The kit contains the source, Python wheelhouse, OCI image
archives, and reviewed K3s inputs. Intel drivers and Ubuntu packages are
resolved on the target and are intentionally not included.

options:
  --output-directory DIR  final archive directory
  --cache-directory DIR   reusable download and Git LFS cache
  --source-commit SHA     defaults to the checked-out commit
  --allow-dirty-source    permit running the builder from a dirty checkout;
                          the kit still contains only --source-commit
  --skip-source-tests     skip source tests (recorded in the manifest)
  -h, --help              show this help
EOF
}

fail() {
  printf 'tvt-online-test-kit: ERROR: %s\n' "$*" >&2
  exit 1
}

log() {
  printf 'tvt-online-test-kit: %s\n' "$*"
}

require_value() {
  [[ -n ${2:-} ]] || fail "$1 requires a value"
}

while (($#)); do
  case "$1" in
    --output-directory)
      require_value "$1" "${2:-}"
      OUTPUT_DIRECTORY="$2"
      shift 2
      ;;
    --cache-directory)
      require_value "$1" "${2:-}"
      CACHE_DIRECTORY="$2"
      shift 2
      ;;
    --source-commit)
      require_value "$1" "${2:-}"
      SOURCE_COMMIT="$2"
      shift 2
      ;;
    --allow-dirty-source)
      ALLOW_DIRTY_SOURCE=true
      shift
      ;;
    --skip-source-tests)
      SKIP_SOURCE_TESTS=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage
      fail "unknown option: $1"
      ;;
  esac
done

for command_name in awk curl df flock git gzip npm python3 sha256sum stat tar timeout; do
  command -v "${command_name}" >/dev/null 2>&1 || fail "required command is missing: ${command_name}"
done
timeout 10s git lfs version >/dev/null 2>&1 || fail "Git LFS is required"

[[ ! -L ${OUTPUT_DIRECTORY} ]] || fail "refusing symlinked output directory"
[[ ! -L ${CACHE_DIRECTORY} ]] || fail "refusing symlinked cache directory"
mkdir -p "${OUTPUT_DIRECTORY}" "${CACHE_DIRECTORY}"
OUTPUT_DIRECTORY="$(cd "${OUTPUT_DIRECTORY}" && pwd -P)"
CACHE_DIRECTORY="$(cd "${CACHE_DIRECTORY}" && pwd -P)"

exec 9>"${CACHE_DIRECTORY}/.build.lock"
flock --wait 0 9 || fail "another online test-kit build is using ${CACHE_DIRECTORY}"

cd "${REPO_ROOT}"
# shellcheck source=config/platform.env
source config/platform.env
# shellcheck source=config/pipeline.env
source config/pipeline.env

release_version="$(python3 scripts/tvt-version.py --check)"
[[ -n ${SOURCE_COMMIT} ]] || SOURCE_COMMIT="$(git rev-parse HEAD)"
[[ ${SOURCE_COMMIT} =~ ^[0-9a-f]{40}$ ]] || fail "--source-commit must be a full lowercase Git SHA"
[[ ${SOURCE_COMMIT} == "$(git rev-parse HEAD)" ]] || fail "--source-commit must equal the checked-out commit"
if ! ${ALLOW_DIRTY_SOURCE} && [[ -n $(git status --porcelain) ]]; then
  fail "source checkout is dirty; commit the changes or use --allow-dirty-source for a development build"
fi

[[ $(dpkg --print-architecture) == amd64 ]] || fail "the test kit must be built on amd64"
# shellcheck disable=SC1091
source /etc/os-release
[[ ${ID:-} == ubuntu && ${VERSION_ID:-} == 24.04 ]] || fail "the test kit builder requires Ubuntu 24.04"

available_mib="$(df -Pm "${OUTPUT_DIRECTORY}" | awk 'NR == 2 {print $4}')"
(( available_mib >= 20480 )) || fail "at least 20 GiB free is required; found ${available_mib} MiB"

docker_command=(docker)
if ! docker info >/dev/null 2>&1; then
  if sudo -n docker info >/dev/null 2>&1; then
    docker_command=(sudo -n docker)
  elif [[ -t 0 && -t 1 ]]; then
    log "Docker requires sudo; requesting an interactive sudo credential"
    sudo -v
    docker_command=(sudo docker)
    "${docker_command[@]}" info >/dev/null 2>&1 || fail "Docker is not reachable"
  else
    fail "Docker is not reachable; run 'docker info' or 'sudo docker info' successfully first"
  fi
fi

short_commit="${SOURCE_COMMIT:0:12}"
kit_name="tvt-edge-online-test-kit-${release_version}-${short_commit}"
archive="${OUTPUT_DIRECTORY}/${kit_name}.tar.gz"
archive_checksum="${archive}.sha256"
archive_report="${OUTPUT_DIRECTORY}/${kit_name}.build-report.json"

if [[ -f ${archive} && -f ${archive_checksum} && -f ${archive_report} ]]; then
  if (cd "${OUTPUT_DIRECTORY}" && sha256sum --check "$(basename "${archive_checksum}")" >/dev/null 2>&1); then
    log "verified existing archive ${archive}"
    exit 0
  fi
  fail "existing output is incomplete or corrupt; move it aside before rebuilding: ${archive}"
fi
for path in "${archive}" "${archive_checksum}" "${archive_report}"; do
  [[ ! -e ${path} ]] || fail "refusing to overwrite partial output: ${path}"
done

temporary_root="$(mktemp -d "${OUTPUT_DIRECTORY}/.${kit_name}.build.XXXXXX")"
cleanup() {
  if [[ -n ${temporary_root:-} && -d ${temporary_root} ]]; then
    case "${temporary_root}" in
      "${OUTPUT_DIRECTORY}"/.*.build.*) rm -rf -- "${temporary_root}" ;;
      *) printf 'tvt-online-test-kit: refusing unsafe cleanup path: %s\n' "${temporary_root}" >&2 ;;
    esac
  fi
}
trap cleanup EXIT

kit="${temporary_root}/${kit_name}"
mkdir -p "${kit}"/{source,wheels,images,k3s}

if ${SKIP_SOURCE_TESTS}; then
  tests_status=skipped
else
  test_python=python3
  [[ ! -x .venv/bin/python ]] || test_python=.venv/bin/python
  log "running source verification"
  "${test_python}" -m pytest -q
  npm --prefix ui test -- --run
  npm --prefix ui run build
  bash -n prepare-tvt-edge-host.sh install-tvt-edge-host.sh scripts/*.sh scripts/lib/*.sh
  git diff --check
  tests_status=passed
fi

log "exporting source commit ${SOURCE_COMMIT}"
git archive --format=tar "${SOURCE_COMMIT}" | tar -xf - -C "${kit}/source"

log "building the offline Python wheelhouse"
python3 -m pip wheel --disable-pip-version-check \
  --wheel-dir "${kit}/wheels" "${kit}/source"

save_registry_image() {
  local configured_tag expected_digest architecture temporary_archive
  configured_tag="${LOCAL_REGISTRY_IMAGE%%@*}"
  expected_digest="${LOCAL_REGISTRY_IMAGE##*@}"
  log "pulling pinned Registry image ${LOCAL_REGISTRY_IMAGE}"
  "${docker_command[@]}" pull --platform linux/amd64 "${LOCAL_REGISTRY_IMAGE}"
  architecture="$("${docker_command[@]}" image inspect --format '{{.Architecture}}' "${LOCAL_REGISTRY_IMAGE}")"
  [[ ${architecture} == amd64 ]] || fail "Registry image architecture is ${architecture}, not amd64"
  "${docker_command[@]}" image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' \
    "${LOCAL_REGISTRY_IMAGE}" | grep -Fq "@${expected_digest}" || \
    fail "Docker did not retain the expected Registry digest ${expected_digest}"
  "${docker_command[@]}" tag "${LOCAL_REGISTRY_IMAGE}" "${configured_tag}"
  temporary_archive="$(mktemp "${kit}/images/.registry.tar.XXXXXX")"
  "${docker_command[@]}" save --output "${temporary_archive}" "${configured_tag}"
  mv -f -- "${temporary_archive}" "${kit}/images/registry.tar"
}

build_control_image() {
  local component source_directory image architecture temporary_archive
  component="$1"
  source_directory="$2"
  image="apexfabric/${component}:${NODE_MANAGEMENT_IMAGE_VERSION}"
  log "building ${image} for linux/amd64"
  "${docker_command[@]}" build --pull=false --provenance=false --platform linux/amd64 \
    -f "${kit}/source/apexfabric/node_management/${source_directory}/Dockerfile" \
    -t "${image}" "${kit}/source"
  architecture="$("${docker_command[@]}" image inspect --format '{{.Architecture}}' "${image}")"
  [[ ${architecture} == amd64 ]] || fail "${image} architecture is ${architecture}, not amd64"
  temporary_archive="$(mktemp "${kit}/images/.${component}.tar.XXXXXX")"
  "${docker_command[@]}" save --output "${temporary_archive}" "${image}"
  mv -f -- "${temporary_archive}" "${kit}/images/${component}.tar"
}

download_atomic() {
  local url destination temporary
  url="$1"
  destination="$2"
  if [[ -s ${destination} ]]; then
    log "reusing cached $(basename "${destination}")"
    return 0
  fi
  temporary="$(mktemp "${destination}.partial.XXXXXX")"
  if ! curl --fail --location --show-error --silent \
    --proto '=https' --tlsv1.2 --connect-timeout 20 --retry 3 --retry-all-errors \
    --max-time 1800 --output "${temporary}" "${url}"; then
    rm -f -- "${temporary}"
    fail "download failed: ${url}"
  fi
  [[ -s ${temporary} ]] || { rm -f -- "${temporary}"; fail "download was empty: ${url}"; }
  mv -f -- "${temporary}" "${destination}"
}

acquire_k3s() {
  local k3s_cache installer_cache sums_cache release_url expected actual reported
  mkdir -p "${CACHE_DIRECTORY}/k3s/${K3S_VERSION}"
  k3s_cache="${CACHE_DIRECTORY}/k3s/${K3S_VERSION}/k3s"
  installer_cache="${CACHE_DIRECTORY}/k3s/${K3S_VERSION}/install.sh"
  sums_cache="${CACHE_DIRECTORY}/k3s/${K3S_VERSION}/sha256sum-amd64.txt"
  release_url="https://github.com/k3s-io/k3s/releases/download/${K3S_VERSION}"
  log "acquiring K3s ${K3S_VERSION}"
  download_atomic "${release_url}/k3s" "${k3s_cache}"
  download_atomic "${release_url}/sha256sum-amd64.txt" "${sums_cache}"
  download_atomic "https://get.k3s.io" "${installer_cache}"
  expected="$(awk '$1 ~ /^[0-9a-f]{64}$/ && (NF == 1 || $2 == "k3s" || $2 == "*k3s") {print $1; exit}' "${sums_cache}")"
  [[ ${expected} =~ ^[0-9a-f]{64}$ ]] || fail "could not parse the official K3s amd64 checksum"
  actual="$(sha256sum "${k3s_cache}" | awk '{print $1}')"
  [[ ${actual} == "${expected}" ]] || fail "K3s checksum mismatch"
  chmod 0755 "${k3s_cache}" "${installer_cache}"
  reported="$("${k3s_cache}" --version | awk 'NR == 1 {print $3}')"
  [[ ${reported} == "${K3S_VERSION}" ]] || fail "K3s binary reports ${reported}, expected ${K3S_VERSION}"
  cp -a "${k3s_cache}" "${kit}/k3s/k3s"
  cp -a "${installer_cache}" "${kit}/k3s/install.sh"
  cp -a "${sums_cache}" "${kit}/k3s/sha256sum-amd64.txt"
}

acquire_traffic() {
  local checkout source_archive destination actual_size actual_digest
  checkout="${CACHE_DIRECTORY}/pipeline"
  if [[ ! -d ${checkout}/.git ]]; then
    [[ ! -e ${checkout} ]] || fail "PIPELINE cache exists but is not a Git checkout: ${checkout}"
    log "cloning PIPELINE without LFS payloads"
    GIT_LFS_SKIP_SMUDGE=1 git clone --no-checkout "${PIPELINE_REPOSITORY}" "${checkout}"
  fi
  git -C "${checkout}" remote set-url origin "${PIPELINE_REPOSITORY}"
  log "fetching exact PIPELINE commit ${PIPELINE_REVISION}"
  GIT_LFS_SKIP_SMUDGE=1 git -C "${checkout}" fetch --no-tags origin "${PIPELINE_REVISION}"
  GIT_LFS_SKIP_SMUDGE=1 git -C "${checkout}" checkout --detach --force "${PIPELINE_REVISION}"
  git -C "${checkout}" lfs install --local >/dev/null
  git -C "${checkout}" lfs pull origin \
    --include="${PIPELINE_TRAFFIC_DELIVERY_DIR}/${PIPELINE_TRAFFIC_ARCHIVE}" --exclude=""
  source_archive="${checkout}/${PIPELINE_TRAFFIC_DELIVERY_DIR}/${PIPELINE_TRAFFIC_ARCHIVE}"
  [[ -f ${source_archive} && ! -L ${source_archive} ]] || fail "Traffic archive was not materialized by Git LFS"
  actual_size="$(stat -c '%s' "${source_archive}")"
  [[ ${actual_size} == "${PIPELINE_TRAFFIC_ARCHIVE_SIZE}" ]] || \
    fail "Traffic archive size is ${actual_size}, expected ${PIPELINE_TRAFFIC_ARCHIVE_SIZE}"
  actual_digest="$(sha256sum "${source_archive}" | awk '{print $1}')"
  [[ ${actual_digest} == "${PIPELINE_TRAFFIC_ARCHIVE_SHA256}" ]] || fail "Traffic archive checksum mismatch"
  destination="${kit}/images/traffic-edge-runtime-v4.tar"
  cp --reflink=auto --sparse=always "${source_archive}" "${destination}"
}

save_registry_image
build_control_image node-reporter reporter
build_control_image node-status-controller status_controller
acquire_k3s
acquire_traffic

cat >"${kit}/README-target-install.md" <<EOF
# TVT online test kit

This is an experimental online-install kit for source commit \`${SOURCE_COMMIT}\`.
It is not the checksum-complete offline production release. Ubuntu packages,
Intel drivers, and OpenVINO must be resolved on the target. The Intel 255H is
not the formally qualified 285H, so use of the hardware override is explicit
and the current Phase-5 host qualification will report a failure.

## Verify and unpack

Verify the outer archive checksum before extraction. After extraction, verify
the internal files:

\`\`\`bash
cd ${kit_name}
sha256sum --check checksums.sha256
\`\`\`

## Target order

Run from the extracted \`source/\` directory on Ubuntu 24.04 amd64:

1. Stop competing GPU/NPU workloads and ensure adequate free disk.
2. Install the Intel stack online with
   \`sudo bash scripts/install-tvt-hardware-drivers.sh --mode online --allow-unverified-hardware\`.
3. Reboot and verify \`/dev/dri/renderD128\`, \`/dev/accel/accel0\`, VA-API,
   OpenCL, and OpenVINO CPU/GPU/NPU discovery.
4. Install the local Registry from \`../images/registry.tar\`.
5. Install K3s using \`../k3s/install.sh\` and \`../k3s/k3s\`.
6. Publish the two control archives with \`--archive-dir ../images\`, then
   install and verify the K3s plane.
7. Import \`../images/traffic-edge-runtime-v4.tar\` with the vendored catalog
   metadata under \`solution-packs/catalog/traffic-edge-runtime-2026.08.21-v4\`.
8. Create \`/opt/tvt/venv\` and install \`tvt-runtime==${release_version}\`
   using only \`../wheels\`.
9. Bootstrap PostgreSQL, install the scoped kubeconfig and host services,
   initialize the site, onboard and validate a known RTSP camera, and deploy
   Traffic first in \`cpu-compatible\` mode.

The detailed component commands and verification steps are in \`source/COMMANDS.md\`.
Do not store camera credentials in this directory or in shell history.
EOF

log "writing artifact manifest and internal checksums"
python3 - "${kit}" "${release_version}" "${SOURCE_COMMIT}" "${tests_status}" \
  "${K3S_VERSION}" "${LOCAL_REGISTRY_IMAGE}" "${PIPELINE_REVISION}" \
  "${PIPELINE_TRAFFIC_ARCHIVE_SHA256}" <<'PY'
import datetime
import hashlib
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
version, commit, tests, k3s, registry, pipeline_commit, traffic_sha = sys.argv[2:]

def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()

artifacts = {}
for relative in (
    "images/registry.tar",
    "images/node-reporter.tar",
    "images/node-status-controller.tar",
    "images/traffic-edge-runtime-v4.tar",
    "k3s/install.sh",
    "k3s/k3s",
):
    path = root / relative
    artifacts[relative] = {"sha256": sha256(path), "size": path.stat().st_size}

document = {
    "schema_version": 1,
    "kit_type": "online-test",
    "production_offline_release": False,
    "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "release_version": version,
    "source_commit": commit,
    "source_tests": tests,
    "architecture": "amd64",
    "target_os": "ubuntu-24.04",
    "target_hardware": "intel-255h-unverified-equivalent",
    "target_requires_network": True,
    "target_requires_driver_reboot": True,
    "contains_credentials": False,
    "pins": {
        "k3s_version": k3s,
        "registry_image": registry,
        "pipeline_commit": pipeline_commit,
        "traffic_archive_sha256": traffic_sha,
    },
    "artifacts": artifacts,
    "omitted_target_resolved_inputs": [
        "ubuntu-apt-closure",
        "intel-driver-recipe",
        "intel-npu-driver-archive",
        "openvino-wheel-closure",
    ],
    "warnings": [
        "Intel 255H is not the formally qualified 285H profile",
        "Phase-5 host.platform qualification will fail on the 255H",
        "node-management Dockerfiles currently use a mutable python:3.12-slim build base",
        "the get.k3s.io installer is snapshotted and hashed here but has no repository-configured upstream pin",
    ],
}
(root / "artifact-manifest.json").write_text(
    json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
PY

(
  cd "${kit}"
  find . -type f ! -name checksums.sha256 -printf '%P\0' \
    | sort -z | xargs -0 sha256sum -- >checksums.sha256
  sha256sum --check checksums.sha256
)

source_epoch="$(git show -s --format=%ct "${SOURCE_COMMIT}")"
temporary_archive="$(mktemp "${OUTPUT_DIRECTORY}/.${kit_name}.tar.gz.XXXXXX")"
log "creating transport archive"
tar --sort=name --mtime="@${source_epoch}" --owner=0 --group=0 --numeric-owner \
  -C "${temporary_root}" -cf - "${kit_name}" | gzip -n >"${temporary_archive}"
tar -tzf "${temporary_archive}" >/dev/null
mv -f -- "${temporary_archive}" "${archive}"

archive_digest="$(sha256sum "${archive}" | awk '{print $1}')"
printf '%s  %s\n' "${archive_digest}" "$(basename "${archive}")" >"${archive_checksum}"
python3 - "${archive_report}.new" "${kit_name}" "${archive_digest}" \
  "$(stat -c '%s' "${archive}")" "${SOURCE_COMMIT}" "${tests_status}" <<'PY'
import datetime
import json
import pathlib
import sys

output, kit, digest, size, commit, tests = sys.argv[1:]
report = {
    "schema_version": 1,
    "kit": kit,
    "source_commit": commit,
    "source_tests": tests,
    "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "archive": {"filename": f"{kit}.tar.gz", "sha256": digest, "size": int(size)},
    "ready_for_transfer": True,
    "production_offline_release": False,
}
pathlib.Path(output).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
mv -f -- "${archive_report}.new" "${archive_report}"
chmod 0644 "${archive}" "${archive_checksum}"
chmod 0600 "${archive_report}"
(cd "${OUTPUT_DIRECTORY}" && sha256sum --check "$(basename "${archive_checksum}")")

log "online test kit is ready"
printf 'Archive: %s\nChecksum: %s\nReport: %s\n' \
  "${archive}" "${archive_checksum}" "${archive_report}"
