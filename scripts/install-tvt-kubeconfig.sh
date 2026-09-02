#!/usr/bin/env bash
set -Eeuo pipefail

TARGET=/etc/tvt/kubeconfig

usage() {
  echo "usage: sudo bash scripts/install-tvt-kubeconfig.sh [--target PATH]" >&2
}

while (($#)); do
  case "$1" in
    --target) TARGET="${2:-}"; shift 2 ;;
    *) usage; exit 2 ;;
  esac
done

[[ ${EUID} -eq 0 ]] || { echo "run as root" >&2; exit 1; }
command -v k3s >/dev/null 2>&1 || { echo "K3s is not installed" >&2; exit 1; }
getent group tvt-edge >/dev/null || { echo "the tvt-edge group is not installed" >&2; exit 1; }

mapfile -t nodes < <(k3s kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')
if [[ ${#nodes[@]} -ne 1 || -z "${nodes[0]}" ]]; then
  echo "TVT kubeconfig installation requires exactly one K3s node" >&2
  exit 1
fi

secret=node-agent-host-token
namespace=apexfabric
for _attempt in {1..30}; do
  token_data="$(k3s kubectl get secret "${secret}" -n "${namespace}" -o jsonpath='{.data.token}' 2>/dev/null || true)"
  ca_data="$(k3s kubectl get secret "${secret}" -n "${namespace}" -o jsonpath='{.data.ca\.crt}' 2>/dev/null || true)"
  if [[ -n "${token_data}" && -n "${ca_data}" ]]; then
    break
  fi
  sleep 1
done
[[ -n "${token_data:-}" && -n "${ca_data:-}" ]] || {
  echo "the apexfabric node-agent token has not been populated" >&2
  exit 1
}
token="$(printf '%s' "${token_data}" | base64 --decode)"

target_dir="$(dirname "${TARGET}")"
install -d -o root -g tvt-edge -m 0750 "${target_dir}"
temporary="$(mktemp "${target_dir}/.kubeconfig.XXXXXX")"
trap 'rm -f "${temporary:-}"' EXIT
chmod 0600 "${temporary}"
{
  printf '%s\n' 'apiVersion: v1' 'kind: Config' 'clusters:'
  printf '%s\n' '- name: tvt-k3s' '  cluster:'
  printf '    certificate-authority-data: %s\n' "${ca_data}"
  printf '%s\n' '    server: https://127.0.0.1:6443' 'contexts:'
  printf '%s\n' '- name: tvt-edge' '  context:' '    cluster: tvt-k3s' '    namespace: apexfabric' '    user: tvt-edge'
  printf '%s\n' 'current-context: tvt-edge' 'users:' '- name: tvt-edge' '  user:'
  printf '    token: %s\n' "${token}"
} >"${temporary}"
chown root:tvt-edge "${temporary}"
chmod 0640 "${temporary}"
k3s kubectl --kubeconfig "${temporary}" auth can-i patch deployments -n apexfabric | grep -qx yes
k3s kubectl --kubeconfig "${temporary}" auth can-i patch namespace/apexfabric | grep -qx yes
k3s kubectl --kubeconfig "${temporary}" auth can-i list nodes | grep -qx yes
if k3s kubectl --kubeconfig "${temporary}" auth can-i patch nodes | grep -qx yes; then
  echo "refusing TVT worker credentials with Node mutation access" >&2
  exit 1
fi
mv "${temporary}" "${TARGET}"
trap - EXIT
unset token token_data ca_data
echo "Installed the namespace-scoped TVT worker kubeconfig at ${TARGET}"
