#!/usr/bin/env bash
set -Eeuo pipefail

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"
IMAGE_LOCK=""

usage() {
  echo "usage: bash scripts/install-k3s-plane.sh --image-lock FILE" >&2
}

while (($#)); do
  case "$1" in
    --image-lock) IMAGE_LOCK="${2:-}"; shift 2 ;;
    *) usage; exit 2 ;;
  esac
done
[[ -f "${IMAGE_LOCK}" ]] || { usage; exit 2; }

SUDO=()
if [[ ${EUID} -ne 0 ]]; then
  command -v sudo >/dev/null 2>&1 || { echo "run as root or install sudo" >&2; exit 1; }
  SUDO=(sudo)
fi
command -v k3s >/dev/null 2>&1 || { echo "K3s is not installed" >&2; exit 1; }

mapfile -t nodes < <("${SUDO[@]}" k3s kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')
if [[ ${#nodes[@]} -ne 1 || -z "${nodes[0]}" ]]; then
  echo "TVT requires exactly one registered K3s node; found ${#nodes[@]}" >&2
  exit 1
fi

rendered="$(mktemp)"
trap 'rm -f "${rendered}"' EXIT
python3 -m tvt_runtime.image_lock render \
  --lock "${IMAGE_LOCK}" \
  --template "${REPO_ROOT}/deploy/k8s/apexfabric-node-management.yaml" \
  --output "${rendered}"

"${SUDO[@]}" k3s kubectl label node "${nodes[0]}" --overwrite \
  apexfabric.com/control-plane=true \
  apexfabric.com/node-reporter-enabled=true \
  apexfabric.com/hardware-profile=intel-285h
"${SUDO[@]}" k3s kubectl apply -f "${REPO_ROOT}/deploy/k8s/apexfabric-foundation.yaml"
"${SUDO[@]}" k3s kubectl apply -f "${rendered}"
bash "${REPO_ROOT}/scripts/verify-k3s-plane.sh"
