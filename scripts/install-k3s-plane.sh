#!/usr/bin/env bash
set -Eeuo pipefail

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REGISTRY=""

usage() {
  echo "usage: bash scripts/install-k3s-plane.sh --registry HOST[:PORT]" >&2
}

while (($#)); do
  case "$1" in
    --registry)
      REGISTRY="${2:-}"
      shift 2
      ;;
    *)
      usage
      exit 2
      ;;
  esac
done

if [[ -z "${REGISTRY}" || "${REGISTRY}" == *"://"* || "${REGISTRY}" =~ [[:space:]] ]]; then
  echo "--registry must be a non-empty HOST[:PORT] value" >&2
  exit 2
fi
if [[ ${EUID} -ne 0 ]]; then
  echo "run this installer as root" >&2
  exit 2
fi
if ! command -v k3s >/dev/null 2>&1; then
  echo "k3s is not installed; install the pinned K3s release before this plane" >&2
  exit 1
fi

mapfile -t nodes < <(k3s kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')
if [[ ${#nodes[@]} -ne 1 || -z "${nodes[0]}" ]]; then
  echo "TVT requires exactly one registered K3s node" >&2
  exit 1
fi

k3s kubectl label node "${nodes[0]}" --overwrite \
  apexfabric.com/control-plane=true \
  apexfabric.com/node-reporter-enabled=true \
  apexfabric.com/hardware-profile=intel-285h
k3s kubectl apply -f "${REPO_ROOT}/deploy/k8s/apexfabric-foundation.yaml"
sed "s|__APEXFABRIC_REGISTRY__|${REGISTRY%/}|g" \
  "${REPO_ROOT}/deploy/k8s/apexfabric-node-management.yaml" \
  | k3s kubectl apply -f -

echo "TVT K3s foundation and node-management plane applied to ${nodes[0]}"
