#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=config/platform.env
source "${REPO_ROOT}/config/platform.env"
# shellcheck source=config/pipeline.env
source "${REPO_ROOT}/config/pipeline.env"

MODE=archive
STATE_DIR="${TVT_PIPELINE_STATE_DIR:-${REPO_ROOT}/build/pipeline}"
WORK_DIR="${STATE_DIR}/work"
LOCK_OUTPUT="${STATE_DIR}/traffic-image.lock.json"
CONCURRENCY_LOCK=""

usage() {
  echo "usage: bash scripts/import-pipeline-traffic-image.sh [--mode archive|build] [--work-dir PATH] [--lock-output FILE] [--concurrency-lock FILE]" >&2
}

while (($#)); do
  case "$1" in
    --mode) MODE="${2:-}"; shift 2 ;;
    --work-dir) WORK_DIR="${2:-}"; shift 2 ;;
    --lock-output) LOCK_OUTPUT="${2:-}"; shift 2 ;;
    --concurrency-lock) CONCURRENCY_LOCK="${2:-}"; shift 2 ;;
    *) usage; exit 2 ;;
  esac
done
[[ "${MODE}" == archive || "${MODE}" == build ]] || { usage; exit 2; }
[[ -n "${CONCURRENCY_LOCK}" ]] || CONCURRENCY_LOCK="${LOCK_OUTPUT}.flock"

[[ "${PIPELINE_REVISION}" =~ ^[0-9a-f]{40}$ ]] || {
  echo "PIPELINE_REVISION must be a full 40-character commit" >&2
  exit 1
}
for digest_variable in \
  PIPELINE_TRAFFIC_ARCHIVE_SHA256 \
  PIPELINE_TRAFFIC_CONTRACT_SHA256 \
  PIPELINE_TRAFFIC_DESIRED_STATE_SCHEMA_SHA256; do
  [[ "${!digest_variable}" =~ ^[0-9a-f]{64}$ ]] || {
    echo "${digest_variable} must be a sha256 digest" >&2
    exit 1
  }
done
[[ "${PIPELINE_UBUNTU_BASE_IMAGE}" =~ ^[^[:space:]@]+:[^[:space:]@]+@sha256:[0-9a-f]{64}$ ]] || {
  echo "PIPELINE_UBUNTU_BASE_IMAGE must include a tag and sha256 digest" >&2
  exit 1
}
[[ "${PIPELINE_TRAFFIC_LOCAL_TAG}" == *"${PIPELINE_TRAFFIC_VERSION}"* ]] || {
  echo "PIPELINE_TRAFFIC_LOCAL_TAG must include ${PIPELINE_TRAFFIC_VERSION}" >&2
  exit 1
}
[[ "${LOCAL_REGISTRY_ADDRESS}" == "127.0.0.1:5000" ]] || {
  echo "the Phase 3 Traffic catalog is restricted to 127.0.0.1:5000" >&2
  exit 1
}
for command_name in curl docker flock git python3 sha256sum tar timeout; do
  command -v "${command_name}" >/dev/null 2>&1 || {
    echo "required command not found: ${command_name}" >&2
    exit 1
  }
done
if [[ "${MODE}" == archive ]] && ! timeout 10s git lfs version >/dev/null 2>&1; then
  echo "archive mode requires Git LFS; install the git-lfs package" >&2
  exit 1
fi

mkdir -p "$(dirname "${LOCK_OUTPUT}")" "$(dirname "${CONCURRENCY_LOCK}")"
exec 9>"${CONCURRENCY_LOCK}"
if ! flock --wait 0 9; then
  echo "another PIPELINE Traffic import is already running" >&2
  exit 75
fi

retry_with_timeout() {
  local description="$1"
  local duration="$2"
  shift 2
  local attempt
  for attempt in 1 2 3; do
    if timeout --signal=TERM "${duration}" "$@"; then
      return 0
    fi
    if [[ ${attempt} -lt 3 ]]; then
      echo "${description} failed (attempt ${attempt}/3); retrying in 5 seconds" >&2
      sleep 5
    fi
  done
  echo "${description} failed after 3 attempts" >&2
  return 1
}

registry_manifest_digest() {
  local repository="$1"
  local tag="$2"
  local response_dir headers manifest digest computed
  response_dir="$(mktemp -d "$(dirname "${LOCK_OUTPUT}")/manifest.XXXXXX")"
  headers="${response_dir}/headers"
  manifest="${response_dir}/manifest"
  if ! curl --fail --silent --show-error \
    --retry 2 --retry-delay 2 --retry-connrefused --max-time 15 --retry-max-time 45 \
    --dump-header "${headers}" --output "${manifest}" \
    --header 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json' \
    "http://${LOCAL_REGISTRY_ADDRESS}/v2/${repository}/manifests/${tag}"; then
    rm -rf -- "${response_dir}"
    return 1
  fi
  digest="$(awk 'tolower($1) == "docker-content-digest:" {gsub("\r", "", $2); print $2}' "${headers}")"
  computed="sha256:$(sha256sum "${manifest}" | awk '{print $1}')"
  rm -rf -- "${response_dir}"
  [[ "${digest}" =~ ^sha256:[0-9a-f]{64}$ && "${digest}" == "${computed}" ]] || return 1
  printf '%s\n' "${digest}"
}

retry_with_timeout "local registry readiness" 10s \
  curl --fail --silent --show-error --max-time 5 \
  "http://${LOCAL_REGISTRY_ADDRESS}/v2/" >/dev/null
timeout 15s docker info >/dev/null 2>&1 || {
  echo "Docker is not reachable; run with Docker access or as root" >&2
  exit 1
}

selected_local_tag="${PIPELINE_TRAFFIC_LOCAL_TAG}"
if [[ "${MODE}" == build ]]; then
  selected_local_tag="${PIPELINE_TRAFFIC_LOCAL_TAG}-source-build"
fi
local_image="${LOCAL_REGISTRY_ADDRESS}/${PIPELINE_TRAFFIC_LOCAL_REPOSITORY}:${selected_local_tag}"
locked_digest=""
if [[ -f "${LOCK_OUTPUT}" && "$(stat -c '%a' "${LOCK_OUTPUT}")" == 600 ]]; then
  locked_digest="$(python3 - "${LOCK_OUTPUT}" "${PIPELINE_TRAFFIC_CATALOG_ID}" \
    "${PIPELINE_REPOSITORY}" "${PIPELINE_REVISION}" \
    "${PIPELINE_TRAFFIC_DELIVERY_DIR}" "${PIPELINE_TRAFFIC_ARCHIVE}" \
    "${PIPELINE_TRAFFIC_ARCHIVE_SHA256}" "${PIPELINE_TRAFFIC_ARCHIVE_SIZE}" \
    "${LOCAL_REGISTRY_ADDRESS}" "${PIPELINE_TRAFFIC_LOCAL_REPOSITORY}" \
    "${selected_local_tag}" "${PIPELINE_TRAFFIC_CONTRACT_SHA256}" \
    "${PIPELINE_TRAFFIC_DESIRED_STATE_SCHEMA_SHA256}" "${MODE}" <<'PY'
import json
import re
import sys
from pathlib import Path

try:
    lock = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    raise SystemExit(0)
pipeline = lock.get("pipeline", {})
archive = lock.get("archive", {})
image = lock.get("image", {})
metadata = lock.get("metadata", {})
source = lock.get("source", {})
matches = (
    lock.get("format_version") == 2
    and lock.get("catalog_id") == sys.argv[2]
    and pipeline.get("repository") == sys.argv[3]
    and pipeline.get("commit") == sys.argv[4]
    and pipeline.get("delivery_directory") == sys.argv[5]
    and archive.get("filename") == sys.argv[6]
    and archive.get("sha256") == sys.argv[7]
    and archive.get("size") == int(sys.argv[8])
    and image.get("registry") == sys.argv[9]
    and image.get("repository") == sys.argv[10]
    and image.get("tag") == sys.argv[11]
    and metadata.get("image_contract_sha256") == sys.argv[12]
    and metadata.get("desired_state_schema_sha256") == sys.argv[13]
    and source.get("mode") == sys.argv[14]
)
digest = image.get("digest", "")
reference = f"{sys.argv[9]}/{sys.argv[10]}@{digest}"
if (
    matches
    and re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
    and image.get("reference") == reference
    and image.get("architecture") == "amd64"
    and isinstance(lock.get("verification_timestamp"), str)
):
    print(digest)
PY
)"
fi
if [[ -n "${locked_digest}" ]]; then
  registry_digest="$(registry_manifest_digest \
    "${PIPELINE_TRAFFIC_LOCAL_REPOSITORY}" "${selected_local_tag}" || true)"
  if [[ "${registry_digest}" == "${locked_digest}" ]]; then
    echo "Traffic image is already imported at ${local_image}."
    echo "Immutable digest: ${locked_digest}"
    echo "Lock: ${LOCK_OUTPUT}"
    exit 0
  fi
fi

mkdir -p "${WORK_DIR}"
echo "Synchronizing PIPELINE Traffic catalog pin ${PIPELINE_TRAFFIC_CATALOG_ID}."
echo "Fetching exact PIPELINE commit ${PIPELINE_REVISION}."
source_dir="${WORK_DIR}/source-${PIPELINE_REVISION}"
credential_helper=""
verification_dir=""
temporary_context=""
verification_container=""
cleanup() {
  if [[ -n "${verification_container}" ]]; then
    timeout 30s docker rm --force "${verification_container}" >/dev/null 2>&1 || true
  fi
  for temporary_path in "${credential_helper}" "${verification_dir}" "${temporary_context}"; do
    if [[ -n "${temporary_path}" && "${temporary_path}" == "${WORK_DIR}"/* ]]; then
      rm -rf -- "${temporary_path}"
    fi
  done
}
trap cleanup EXIT

git_environment=(env GIT_LFS_SKIP_SMUDGE=1 GIT_TERMINAL_PROMPT=0)
if [[ -n "${PIPELINE_GITHUB_TOKEN:-}" ]]; then
  credential_helper="$(mktemp "${WORK_DIR}/git-askpass.XXXXXX")"
  printf '%s\n' '#!/usr/bin/env bash' \
    'case "${1:-}" in' \
    '  *Username*) printf "%s\n" "${PIPELINE_GIT_USERNAME:-x-access-token}" ;;' \
    '  *) printf "%s\n" "${PIPELINE_GITHUB_TOKEN}" ;;' \
    'esac' >"${credential_helper}"
  chmod 0700 "${credential_helper}"
  git_environment+=(GIT_ASKPASS="${credential_helper}")
fi

created_checkout=false
if [[ ! -d "${source_dir}/.git" ]]; then
  [[ ! -e "${source_dir}" ]] || {
    echo "source path exists but is not a Git checkout: ${source_dir}" >&2
    exit 1
  }
  retry_with_timeout "PIPELINE clone" 10m "${git_environment[@]}" \
    git clone --no-checkout --filter=blob:none "${PIPELINE_REPOSITORY}" "${source_dir}"
  created_checkout=true
fi
configured_remote="$(git -C "${source_dir}" remote get-url origin)"
[[ "${configured_remote}" == "${PIPELINE_REPOSITORY}" ]] || {
  echo "PIPELINE checkout origin is ${configured_remote}, expected ${PIPELINE_REPOSITORY}" >&2
  exit 1
}
if [[ "${MODE}" == archive ]]; then
  timeout 30s git -C "${source_dir}" lfs install \
    --local --skip-smudge --skip-repo >/dev/null
fi
retry_with_timeout "PIPELINE pinned revision fetch" 5m "${git_environment[@]}" \
  git -C "${source_dir}" fetch --no-tags origin "${PIPELINE_REVISION}"
if [[ "${created_checkout}" == true ]]; then
  timeout 10m env GIT_LFS_SKIP_SMUDGE=1 \
    git -C "${source_dir}" checkout --detach "${PIPELINE_REVISION}"
fi
actual_revision="$(git -C "${source_dir}" rev-parse HEAD)"
[[ "${actual_revision}" == "${PIPELINE_REVISION}" ]] || {
  echo "existing PIPELINE checkout is ${actual_revision}, expected ${PIPELINE_REVISION}" >&2
  echo "use a different --work-dir rather than rewriting the checkout" >&2
  exit 1
}
[[ -z "$(git -C "${source_dir}" status --porcelain --untracked-files=no)" ]] || {
  echo "PIPELINE source checkout has tracked modifications: ${source_dir}" >&2
  exit 1
}

delivery_path="${source_dir}/${PIPELINE_TRAFFIC_DELIVERY_DIR}"
echo "${PIPELINE_TRAFFIC_CONTRACT_SHA256}  ${delivery_path}/image-contract.yaml" \
  | sha256sum --check --status || {
  echo "PIPELINE image contract checksum verification failed" >&2
  exit 1
}
echo "${PIPELINE_TRAFFIC_DESIRED_STATE_SCHEMA_SHA256}  ${delivery_path}/desired-state.schema.json" \
  | sha256sum --check --status || {
  echo "PIPELINE desired-state schema checksum verification failed" >&2
  exit 1
}

source_image=""
if [[ "${MODE}" == archive ]]; then
  echo "Downloading and verifying ${PIPELINE_TRAFFIC_ARCHIVE}."
  archive_relative="${PIPELINE_TRAFFIC_DELIVERY_DIR}/${PIPELINE_TRAFFIC_ARCHIVE}"
  retry_with_timeout "Traffic image archive download" 30m "${git_environment[@]}" \
    git -C "${source_dir}" lfs pull --include="${archive_relative}" \
    --exclude='' origin "${PIPELINE_REVISION}"
  archive_path="${source_dir}/${archive_relative}"
  [[ -f "${archive_path}" ]] || { echo "missing Traffic archive: ${archive_path}" >&2; exit 1; }
  actual_size="$(stat -c '%s' "${archive_path}")"
  [[ "${actual_size}" == "${PIPELINE_TRAFFIC_ARCHIVE_SIZE}" ]] || {
    echo "Traffic archive size is ${actual_size}, expected ${PIPELINE_TRAFFIC_ARCHIVE_SIZE}" >&2
    exit 1
  }
  echo "${PIPELINE_TRAFFIC_ARCHIVE_SHA256}  ${archive_path}" | sha256sum --check --status || {
    echo "Traffic archive checksum verification failed" >&2
    exit 1
  }
  timeout --signal=TERM 20m docker load --input "${archive_path}"
  source_image="${PIPELINE_TRAFFIC_ARCHIVE_IMAGE}"
else
  echo "qualification mode: building from pinned source; this is not the production synchronization path" >&2
  temporary_context="$(mktemp -d "${WORK_DIR}/context.XXXXXX")"
  git -C "${source_dir}" archive "${PIPELINE_REVISION}" \
    requirements.txt docker/Dockerfile.base docker/Dockerfile.traffic edge_runtime models/traffic \
    | tar -x -C "${temporary_context}"
  base_image="pipeline-ubuntu-python:tvt-${PIPELINE_REVISION:0:12}"
  source_image="traffic-edge-runtime:pipeline-${PIPELINE_REVISION:0:12}"
  sed "1cFROM ${PIPELINE_UBUNTU_BASE_IMAGE}" \
    "${temporary_context}/docker/Dockerfile.base" \
    >"${temporary_context}/docker/Dockerfile.base.tvt"
  sed "1cFROM ${base_image}" \
    "${temporary_context}/docker/Dockerfile.traffic" \
    >"${temporary_context}/docker/Dockerfile.traffic.tvt"
  retry_with_timeout "PIPELINE base image build" 45m docker build \
    --pull=false --platform linux/amd64 \
    --build-arg "INTEL_NPU_DRIVER_VERSION=${PIPELINE_INTEL_NPU_DRIVER_VERSION}" \
    -f "${temporary_context}/docker/Dockerfile.base.tvt" \
    -t "${base_image}" "${temporary_context}"
  retry_with_timeout "PIPELINE Traffic image build" 30m docker build \
    --pull=false --platform linux/amd64 \
    --build-arg "IMAGE_VERSION=${PIPELINE_TRAFFIC_VERSION}" \
    -f "${temporary_context}/docker/Dockerfile.traffic.tvt" \
    -t "${source_image}" "${temporary_context}"
fi

verification_dir="$(mktemp -d "${WORK_DIR}/verify.XXXXXX")"
echo "Inspecting the stopped v4 solution image and baked models."
timeout 1m docker image inspect "${source_image}" \
  >"${verification_dir}/image-inspect.json"
python3 "${REPO_ROOT}/scripts/verify-pipeline-image-inspect.py" \
  "${verification_dir}/image-inspect.json" \
  --source "${PIPELINE_TRAFFIC_OCI_SOURCE}" \
  --title "${PIPELINE_TRAFFIC_OCI_TITLE}" \
  --version "${PIPELINE_TRAFFIC_VERSION}" \
  --contract-version "${PIPELINE_TRAFFIC_CONTRACT_VERSION}" \
  --hardware-profile "${PIPELINE_TRAFFIC_HARDWARE_PROFILE}" \
  --models-delivery "${PIPELINE_TRAFFIC_MODELS_DELIVERY}" \
  --user "${PIPELINE_TRAFFIC_CONTAINER_USER}" \
  --port "${PIPELINE_TRAFFIC_CONTAINER_PORT}" \
  --command "${PIPELINE_TRAFFIC_CONTAINER_COMMAND}"

mkdir -p "${verification_dir}/models" "${verification_dir}/modules"
verification_container="$(timeout 1m docker create "${source_image}")"
timeout 10m docker cp "${verification_container}:/models/traffic/openvino/." \
  "${verification_dir}/models"
timeout 1m docker cp "${verification_container}:/opt/pipeline/edge_runtime/agent/edge_agent.py" \
  "${verification_dir}/modules/edge_agent.py"
timeout 1m docker cp "${verification_container}:/opt/pipeline/edge_runtime/runtime/solution_image_entrypoint.py" \
  "${verification_dir}/modules/solution_image_entrypoint.py"
while read -r model_file model_sha256; do
  [[ -s "${verification_dir}/models/${model_file}" ]] || {
    echo "Traffic image is missing baked model ${model_file}" >&2
    exit 1
  }
  echo "${model_sha256}  ${verification_dir}/models/${model_file}" \
    | sha256sum --check --status || {
    echo "baked model checksum verification failed for ${model_file}" >&2
    exit 1
  }
done <<EOF
vehicle.xml ${PIPELINE_TRAFFIC_MODEL_VEHICLE_XML_SHA256}
vehicle.bin ${PIPELINE_TRAFFIC_MODEL_VEHICLE_BIN_SHA256}
license_plate.xml ${PIPELINE_TRAFFIC_MODEL_LICENSE_PLATE_XML_SHA256}
license_plate.bin ${PIPELINE_TRAFFIC_MODEL_LICENSE_PLATE_BIN_SHA256}
ocr.xml ${PIPELINE_TRAFFIC_MODEL_OCR_XML_SHA256}
ocr.bin ${PIPELINE_TRAFFIC_MODEL_OCR_BIN_SHA256}
EOF
[[ -s "${verification_dir}/modules/edge_agent.py" ]] || {
  echo "Traffic image is missing the edge-agent compiler module" >&2
  exit 1
}
[[ -s "${verification_dir}/modules/solution_image_entrypoint.py" ]] || {
  echo "Traffic image is missing the solution image entrypoint" >&2
  exit 1
}
timeout 30s docker rm "${verification_container}" >/dev/null
verification_container=""

echo "Pushing verified v4 image to ${local_image}."
timeout 1m docker tag "${source_image}" "${local_image}"
retry_with_timeout "edge-local Traffic image push" 15m docker push "${local_image}"
local_digest="$(registry_manifest_digest \
  "${PIPELINE_TRAFFIC_LOCAL_REPOSITORY}" "${selected_local_tag}")"
[[ "${local_digest}" =~ ^sha256:[0-9a-f]{64}$ ]] || {
  echo "local registry did not return a verified Traffic image digest" >&2
  exit 1
}
immutable_image="${LOCAL_REGISTRY_ADDRESS}/${PIPELINE_TRAFFIC_LOCAL_REPOSITORY}@${local_digest}"

temporary_lock="$(mktemp "$(dirname "${LOCK_OUTPUT}")/.traffic-image.lock.XXXXXX")"
trap 'rm -f "${temporary_lock}"; cleanup' EXIT
python3 - "${temporary_lock}" "${PIPELINE_TRAFFIC_CATALOG_ID}" \
  "${PIPELINE_REPOSITORY}" "${PIPELINE_REVISION}" \
  "${PIPELINE_TRAFFIC_DELIVERY_DIR}" "${PIPELINE_TRAFFIC_ARCHIVE}" \
  "${PIPELINE_TRAFFIC_ARCHIVE_SHA256}" "${PIPELINE_TRAFFIC_ARCHIVE_SIZE}" \
  "${MODE}" "${LOCAL_REGISTRY_ADDRESS}" "${PIPELINE_TRAFFIC_LOCAL_REPOSITORY}" \
  "${selected_local_tag}" "${local_digest}" "${immutable_image}" \
  "${PIPELINE_TRAFFIC_CONTRACT_SHA256}" \
  "${PIPELINE_TRAFFIC_DESIRED_STATE_SCHEMA_SHA256}" <<'PY'
import datetime
import json
import sys
from pathlib import Path

output = Path(sys.argv[1])
document = {
    "format_version": 2,
    "catalog_id": sys.argv[2],
    "pipeline": {
        "repository": sys.argv[3],
        "commit": sys.argv[4],
        "delivery_directory": sys.argv[5],
    },
    "archive": {
        "filename": sys.argv[6],
        "sha256": sys.argv[7],
        "size": int(sys.argv[8]),
    },
    "source": {"mode": sys.argv[9]},
    "image": {
        "registry": sys.argv[10],
        "repository": sys.argv[11],
        "tag": sys.argv[12],
        "digest": sys.argv[13],
        "reference": sys.argv[14],
        "architecture": "amd64",
    },
    "metadata": {
        "image_contract_sha256": sys.argv[15],
        "desired_state_schema_sha256": sys.argv[16],
    },
    "verification_timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
}
output.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
chmod 0600 "${temporary_lock}"
mv -f "${temporary_lock}" "${LOCK_OUTPUT}"
trap cleanup EXIT

echo "Imported pinned PIPELINE Traffic image."
echo "Catalog ID: ${PIPELINE_TRAFFIC_CATALOG_ID}"
echo "Source revision: ${PIPELINE_REVISION}"
echo "Immutable image: ${immutable_image}"
echo "Lock: ${LOCK_OUTPUT}"
