#!/usr/bin/env bash
set -Eeuo pipefail

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=config/platform.env
source "${REPO_ROOT}/config/platform.env"

INSTALLER=""
K3S_BINARY=""
DOWNLOAD_INSTALLER=false

usage() {
  echo "usage: bash scripts/install-k3s-single-node.sh (--installer FILE | --download-installer) [--k3s-binary FILE]" >&2
}

while (($#)); do
  case "$1" in
    --installer) INSTALLER="${2:-}"; shift 2 ;;
    --download-installer) DOWNLOAD_INSTALLER=true; shift ;;
    --k3s-binary) K3S_BINARY="${2:-}"; shift 2 ;;
    *) usage; exit 2 ;;
  esac
done

if [[ -n "${INSTALLER}" && ${DOWNLOAD_INSTALLER} == true ]]; then
  echo "choose either --installer or --download-installer" >&2
  exit 2
fi

if ! systemctl is-active --quiet tvt-local-registry.service; then
  echo "the local registry is not active; run scripts/install-local-registry.sh first" >&2
  exit 1
fi
curl --fail --silent --show-error --max-time 5 \
  "http://${LOCAL_REGISTRY_ADDRESS}/v2/" >/dev/null || {
  echo "the local registry is not ready at http://${LOCAL_REGISTRY_ADDRESS}" >&2
  exit 1
}
if [[ -z "${INSTALLER}" && ${DOWNLOAD_INSTALLER} == false && ! -x /usr/local/bin/k3s ]]; then
  echo "supply a reviewed --installer file or explicitly use --download-installer" >&2
  exit 2
fi

SUDO=()
if [[ ${EUID} -ne 0 ]]; then
  command -v sudo >/dev/null 2>&1 || { echo "run as root or install sudo" >&2; exit 1; }
  SUDO=(sudo)
fi

if command -v k3s >/dev/null 2>&1; then
  installed_version="$(k3s --version | awk 'NR == 1 {print $3}')"
  if [[ "${installed_version}" != "${K3S_VERSION}" ]]; then
    echo "installed K3s ${installed_version} does not match pinned ${K3S_VERSION}" >&2
    exit 1
  fi
  mapfile -t existing_nodes < <("${SUDO[@]}" k3s kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')
  if [[ ${#existing_nodes[@]} -ne 1 || -z "${existing_nodes[0]}" ]]; then
    echo "refusing to reconfigure an existing non-single-node cluster" >&2
    exit 1
  fi
fi

registry_restart=()
if command -v k3s >/dev/null 2>&1; then registry_restart=(--restart); fi
bash "${REPO_ROOT}/scripts/configure-k3s-registry.sh" \
  --registry "${LOCAL_REGISTRY_ADDRESS}" --scheme http \
  "${registry_restart[@]}"

if ! command -v k3s >/dev/null 2>&1; then
  temporary_installer=""
  if ${DOWNLOAD_INSTALLER}; then
    command -v curl >/dev/null 2>&1 || { echo "curl is required" >&2; exit 1; }
    temporary_installer="$(mktemp)"
    trap 'rm -f "${temporary_installer}"' EXIT
    curl --fail --silent --show-error --location --proto '=https' \
      https://get.k3s.io --output "${temporary_installer}"
    INSTALLER="${temporary_installer}"
  fi
  [[ -f "${INSTALLER}" ]] || { echo "installer file not found: ${INSTALLER}" >&2; exit 1; }

  skip_download=false
  if [[ -n "${K3S_BINARY}" ]]; then
    [[ -x "${K3S_BINARY}" ]] || { echo "K3s binary is not executable: ${K3S_BINARY}" >&2; exit 1; }
    "${SUDO[@]}" install -o root -g root -m 0755 "${K3S_BINARY}" /usr/local/bin/k3s
    skip_download=true
  fi
  echo "Installing pinned K3s ${K3S_VERSION}"
  install_environment=(env \
    INSTALL_K3S_VERSION="${K3S_VERSION}" \
    K3S_KUBECONFIG_MODE="600" \
    INSTALL_K3S_EXEC="server --disable=traefik --disable=servicelb --secrets-encryption --secrets-encryption-provider=secretbox")
  if ${skip_download}; then
    install_environment+=(INSTALL_K3S_SKIP_DOWNLOAD=true)
  fi
  "${SUDO[@]}" "${install_environment[@]}" sh "${INSTALLER}"
fi

"${SUDO[@]}" systemctl enable --now k3s
installed_version="$(k3s --version | awk 'NR == 1 {print $3}')"
if [[ "${installed_version}" != "${K3S_VERSION}" ]]; then
  echo "installed K3s ${installed_version} does not match pinned ${K3S_VERSION}" >&2
  exit 1
fi
"${SUDO[@]}" k3s kubectl wait --for=condition=Ready node --all --timeout=180s
mapfile -t nodes < <("${SUDO[@]}" k3s kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')
if [[ ${#nodes[@]} -ne 1 || -z "${nodes[0]}" ]]; then
  echo "TVT requires exactly one registered node; found ${#nodes[@]}" >&2
  exit 1
fi
"${SUDO[@]}" k3s kubectl apply -f "${REPO_ROOT}/deploy/k8s/apexfabric-foundation.yaml"
echo "Pinned single-node K3s foundation is ready on ${nodes[0]}"
