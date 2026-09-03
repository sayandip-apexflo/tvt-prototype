#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CATALOG_DIRECTORY=/opt/tvt/solution-packs/catalog/traffic-edge-runtime-2026.08.21-v4
QUALIFIER=(/opt/tvt/venv/bin/tvt-traffic-qualify)

if [[ ! -x ${QUALIFIER[0]} ]]; then
  QUALIFIER=("${REPO_ROOT}/.venv/bin/python" -m tvt_edge.qualification)
  CATALOG_DIRECTORY="${REPO_ROOT}/solution-packs/catalog/traffic-edge-runtime-2026.08.21-v4"
fi

[[ ${EUID} -eq 0 ]] || {
  echo "run as root so K3s/containerd and mode-0600 evidence can be inspected" >&2
  exit 1
}
[[ -x ${QUALIFIER[0]} ]] || {
  echo "the TVT qualification command is not installed" >&2
  exit 1
}
[[ -d ${CATALOG_DIRECTORY} ]] || {
  echo "the pinned Traffic qualification contracts are not installed" >&2
  exit 1
}
for executable in dpkg k3s systemctl; do
  command -v "${executable}" >/dev/null 2>&1 || {
    echo "required qualification executable not found: ${executable}" >&2
    exit 1
  }
done

exec "${QUALIFIER[@]}" --catalog-directory "${CATALOG_DIRECTORY}" "$@"
