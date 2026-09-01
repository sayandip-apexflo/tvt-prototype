#!/usr/bin/env bash
set -Eeuo pipefail

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REGISTRY=""
SCHEME="http"
RESTART=false

usage() {
  echo "usage: bash scripts/configure-k3s-registry.sh --registry HOST[:PORT] [--scheme http|https] [--restart]" >&2
}

while (($#)); do
  case "$1" in
    --registry) REGISTRY="${2:-}"; shift 2 ;;
    --scheme) SCHEME="${2:-}"; shift 2 ;;
    --restart) RESTART=true; shift ;;
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

SUDO=()
if [[ ${EUID} -ne 0 ]]; then
  command -v sudo >/dev/null 2>&1 || { echo "run as root or install sudo" >&2; exit 1; }
  SUDO=(sudo)
fi

rendered="$(mktemp)"
trap 'rm -f "${rendered}"' EXIT
sed -e "s|__REGISTRY_ADDRESS__|${REGISTRY}|g" \
    -e "s|__REGISTRY_SCHEME__|${SCHEME}|g" \
    "${REPO_ROOT}/deploy/config/registries.yaml.in" >"${rendered}"

"${SUDO[@]}" install -d -m 0755 /etc/rancher/k3s
if "${SUDO[@]}" test -f /etc/rancher/k3s/registries.yaml \
    && ! "${SUDO[@]}" test -f /etc/rancher/k3s/registries.yaml.tvt-backup; then
  "${SUDO[@]}" cp -a /etc/rancher/k3s/registries.yaml \
    /etc/rancher/k3s/registries.yaml.tvt-backup
fi
"${SUDO[@]}" install -o root -g root -m 0600 "${rendered}" \
  /etc/rancher/k3s/registries.yaml

if ${RESTART} && "${SUDO[@]}" systemctl is-active --quiet k3s; then
  "${SUDO[@]}" systemctl restart k3s
  "${SUDO[@]}" k3s kubectl wait --for=condition=Ready node --all --timeout=180s
fi
echo "Configured K3s registry mirror ${SCHEME}://${REGISTRY}"
