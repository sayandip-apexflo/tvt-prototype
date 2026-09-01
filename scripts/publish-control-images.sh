#!/usr/bin/env bash
set -Eeuo pipefail

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"
# shellcheck source=config/platform.env
source "${REPO_ROOT}/config/platform.env"

REGISTRY=""
SCHEME="http"
LOCK_OUTPUT="${REPO_ROOT}/build/node-management-images.lock.json"

usage() {
  echo "usage: bash scripts/publish-control-images.sh --registry HOST[:PORT] [--scheme http|https] [--lock-output FILE]" >&2
}

while (($#)); do
  case "$1" in
    --registry) REGISTRY="${2:-}"; shift 2 ;;
    --scheme) SCHEME="${2:-}"; shift 2 ;;
    --lock-output) LOCK_OUTPUT="${2:-}"; shift 2 ;;
    *) usage; exit 2 ;;
  esac
done

if [[ -z "${REGISTRY}" || "${REGISTRY}" == *"://"* || "${REGISTRY}" == */* || "${REGISTRY}" =~ [[:space:]] ]]; then
  echo "--registry must be a HOST[:PORT] value" >&2
  exit 2
fi
if [[ "${SCHEME}" != http && "${SCHEME}" != https ]]; then
  echo "--scheme must be http or https" >&2
  exit 2
fi
command -v docker >/dev/null 2>&1 || { echo "docker is required" >&2; exit 1; }
command -v curl >/dev/null 2>&1 || { echo "curl is required" >&2; exit 1; }

DOCKER=(docker)
if ! docker info >/dev/null 2>&1; then
  command -v sudo >/dev/null 2>&1 || { echo "cannot access Docker" >&2; exit 1; }
  DOCKER=(sudo docker)
fi
curl --fail --silent --show-error "${SCHEME}://${REGISTRY}/v2/" >/dev/null

for component in node-reporter node-status-controller; do
  case "${component}" in
    node-reporter) source_dir=reporter ;;
    node-status-controller) source_dir=status_controller ;;
  esac
  image="${REGISTRY}/apexfabric/${component}:${NODE_MANAGEMENT_IMAGE_VERSION}"
  "${DOCKER[@]}" build --pull=false --provenance=false \
    -f "${REPO_ROOT}/apexfabric/node_management/${source_dir}/Dockerfile" \
    -t "${image}" "${REPO_ROOT}"
  "${DOCKER[@]}" push "${image}"
  echo "Published ${image}"
done

python3 -m tvt_runtime.image_lock create \
  --registry "${SCHEME}://${REGISTRY}" \
  --version "${NODE_MANAGEMENT_IMAGE_VERSION}" \
  --output "${LOCK_OUTPUT}"
echo "Wrote digest-pinned image lock ${LOCK_OUTPUT}"
