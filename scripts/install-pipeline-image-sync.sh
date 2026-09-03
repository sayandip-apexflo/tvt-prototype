#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=config/platform.env
source "${REPO_ROOT}/config/platform.env"

ARCHIVE_FILE=""
METADATA_DIRECTORY=""
usage() {
  echo "usage: sudo bash scripts/install-pipeline-image-sync.sh [--archive-file FILE --metadata-directory DIR]" >&2
}
while (($#)); do
  case "$1" in
    --archive-file) ARCHIVE_FILE="${2:-}"; shift 2 ;;
    --metadata-directory) METADATA_DIRECTORY="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage; exit 2 ;;
  esac
done
if [[ -n ${ARCHIVE_FILE} || -n ${METADATA_DIRECTORY} ]]; then
  [[ -f ${ARCHIVE_FILE} && ! -L ${ARCHIVE_FILE} && -d ${METADATA_DIRECTORY} && ! -L ${METADATA_DIRECTORY} ]] || {
    echo "--archive-file and --metadata-directory must be supplied together" >&2
    exit 2
  }
fi

[[ ${EUID} -eq 0 ]] || { echo "run as root" >&2; exit 1; }
[[ "$(dpkg --print-architecture)" == amd64 ]] || {
  echo "the PIPELINE Traffic delivery is pinned for amd64" >&2
  exit 1
}
executables=(curl docker flock install python3 sha256sum systemctl timeout)
if [[ -z ${ARCHIVE_FILE} ]]; then executables+=(git git-lfs); fi
for executable in "${executables[@]}"; do
  command -v "${executable}" >/dev/null 2>&1 || {
    echo "required executable not found: ${executable}" >&2
    exit 1
  }
done
systemctl is-active --quiet docker.service || {
  echo "docker.service must be active before installing image synchronization" >&2
  exit 1
}
systemctl is-active --quiet tvt-local-registry.service || {
  echo "tvt-local-registry.service must be active before installing image synchronization" >&2
  exit 1
}
curl --fail --silent --show-error --retry 2 --retry-delay 2 \
  --retry-connrefused --max-time 5 --retry-max-time 20 \
  "http://${LOCAL_REGISTRY_ADDRESS}/v2/" >/dev/null

install -d -o root -g root -m 0755 /opt/tvt/scripts /opt/tvt/config
install -d -o root -g root -m 0700 /var/lib/tvt/pipeline /etc/tvt
install -o root -g root -m 0755 \
  "${REPO_ROOT}/scripts/import-pipeline-traffic-image.sh" \
  /opt/tvt/scripts/import-pipeline-traffic-image.sh
install -o root -g root -m 0755 \
  "${REPO_ROOT}/scripts/verify-pipeline-image-sync.sh" \
  /opt/tvt/scripts/verify-pipeline-image-sync.sh
install -o root -g root -m 0755 \
  "${REPO_ROOT}/scripts/verify-pipeline-image-inspect.py" \
  /opt/tvt/scripts/verify-pipeline-image-inspect.py
install -o root -g root -m 0644 \
  "${REPO_ROOT}/config/platform.env" /opt/tvt/config/platform.env
install -o root -g root -m 0644 \
  "${REPO_ROOT}/config/pipeline.env" /opt/tvt/config/pipeline.env
if [[ ! -e /etc/tvt/pipeline-image-sync.env ]]; then
  install -o root -g root -m 0600 \
    "${REPO_ROOT}/deploy/host/tvt-pipeline-image-sync.env.example" \
    /etc/tvt/pipeline-image-sync.env
fi
if [[ -n ${ARCHIVE_FILE} ]]; then
  temporary_bundle_env="$(mktemp /etc/tvt/.pipeline-bundle.env.XXXXXX)"
  trap 'rm -f "${temporary_bundle_env:-}"' EXIT
  printf 'TVT_PIPELINE_ARCHIVE_FILE=%s\nTVT_PIPELINE_METADATA_DIRECTORY=%s\n' \
    "${ARCHIVE_FILE}" "${METADATA_DIRECTORY}" >"${temporary_bundle_env}"
  chmod 0600 "${temporary_bundle_env}"
  chown root:root "${temporary_bundle_env}"
  mv -f -- "${temporary_bundle_env}" /etc/tvt/pipeline-bundle.env
  trap - EXIT
fi
[[ "$(stat -c '%a' /etc/tvt/pipeline-image-sync.env)" == 600 ]] || {
  echo "/etc/tvt/pipeline-image-sync.env must have mode 0600" >&2
  exit 1
}
[[ "$(stat -c '%U' /etc/tvt/pipeline-image-sync.env)" == root ]] || {
  echo "/etc/tvt/pipeline-image-sync.env must be owned by root" >&2
  exit 1
}

install -o root -g root -m 0644 \
  "${REPO_ROOT}/deploy/systemd/tvt-pipeline-image-sync.service" \
  /etc/systemd/system/tvt-pipeline-image-sync.service
install -o root -g root -m 0644 \
  "${REPO_ROOT}/deploy/systemd/tvt-pipeline-image-sync.timer" \
  /etc/systemd/system/tvt-pipeline-image-sync.timer
systemctl daemon-reload
systemctl enable --now tvt-pipeline-image-sync.timer

echo "Installed immutable PIPELINE Traffic image synchronization."
echo "Run the first import now with: systemctl start tvt-pipeline-image-sync.service"
echo "Inspect non-secret status with: journalctl -u tvt-pipeline-image-sync.service"
