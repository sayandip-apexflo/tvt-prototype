#!/usr/bin/env bash
set -Eeuo pipefail
umask 027

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/tvt-installer-common.sh
source "${SCRIPT_DIR}/scripts/lib/tvt-installer-common.sh"

BUNDLE=""
MODE=""
ALLOW_UNVERIFIED_HARDWARE=false
VERIFY_ONLY=false
LOG_FILE="${TVT_PREPARE_LOG_FILE:-/var/log/tvt/prepare-edge-host.log}"
readonly PREPARE_STATE="${TVT_INSTALL_STATE_ROOT}/prepare-state.json"
readonly REBOOT_MARKER="${TVT_HARDWARE_REBOOT_MARKER:-/var/lib/tvt/hardware-driver-reboot-required}"
readonly BOOT_ID_FILE="${TVT_BOOT_ID_FILE:-/proc/sys/kernel/random/boot_id}"

usage() {
  cat >&2 <<'EOF'
usage: sudo ./prepare-tvt-edge-host.sh --bundle PATH --mode online|offline [options]

options:
  --allow-unverified-hardware  permit an audited Intel equivalent to the 285H
  --verify-only               verify an already prepared host without changing it
  --log-file PATH             write the preparation log to PATH
EOF
}

while (($#)); do
  case "$1" in
    --bundle) tvt_require_value "$1" "${2:-}"; BUNDLE="$2"; shift 2 ;;
    --mode) tvt_require_value "$1" "${2:-}"; MODE="$2"; shift 2 ;;
    --allow-unverified-hardware) ALLOW_UNVERIFIED_HARDWARE=true; shift ;;
    --verify-only) VERIFY_ONLY=true; shift ;;
    --log-file) tvt_require_value "$1" "${2:-}"; LOG_FILE="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage; tvt_fail "unknown option: $1" ;;
  esac
done
[[ -n ${BUNDLE} && -n ${MODE} ]] || { usage; exit 2; }
[[ ${MODE} == online || ${MODE} == offline ]] || tvt_fail "--mode must be online or offline"

tvt_require_root
command -v flock >/dev/null 2>&1 || tvt_fail "flock is required"
BUNDLE="$(tvt_canonical_directory "${BUNDLE}")"
[[ ! -L ${LOG_FILE} ]] || tvt_fail "refusing symlinked log file: ${LOG_FILE}"
install -d -o root -g root -m 0750 "$(dirname "${LOG_FILE}")"
touch "${LOG_FILE}"
chmod 0640 "${LOG_FILE}"
exec > >(tee -a "${LOG_FILE}") 2>&1
tvt_acquire_lock
tvt_verify_bundle "${BUNDLE}"
readonly RELEASE_VERSION="$(tvt_manifest_value "${BUNDLE}" release_version)"

preflight_host() {
  local os_release="${TVT_OS_RELEASE_FILE:-/etc/os-release}"
  [[ -r ${os_release} ]] || tvt_fail "${os_release} is missing"
  # shellcheck disable=SC1090
  source "${os_release}"
  [[ ${ID:-} == ubuntu && ${VERSION_ID:-} == 24.04 ]] || \
    tvt_fail "supported OS is Ubuntu 24.04; found ${ID:-unknown} ${VERSION_ID:-unknown}"
  [[ $(dpkg --print-architecture) == amd64 ]] || tvt_fail "supported architecture is amd64"

  local cpuinfo="${TVT_CPUINFO_FILE:-/proc/cpuinfo}"
  if ! grep -Eiq '^model name[[:space:]]*:.*Intel.*Core.*Ultra.*285H' "${cpuinfo}"; then
    ${ALLOW_UNVERIFIED_HARDWARE} || tvt_fail \
      "Intel Core Ultra 285H was not detected; use --allow-unverified-hardware only for an audited equivalent"
  fi
  local minimum_kernel current_kernel
  minimum_kernel="$(tvt_manifest_value "${BUNDLE}" minimum_kernel)"
  current_kernel="$(uname -r | sed 's/[^0-9.].*$//')"
  dpkg --compare-versions "${current_kernel}" ge "${minimum_kernel}" || \
    tvt_fail "kernel $(uname -r) is older than qualified minimum ${minimum_kernel}"
  modinfo intel_vpu >/dev/null 2>&1 || tvt_fail "kernel $(uname -r) does not provide intel_vpu"
  if ! modinfo i915 >/dev/null 2>&1 && ! modinfo xe >/dev/null 2>&1; then
    tvt_fail "kernel $(uname -r) provides neither i915 nor xe"
  fi

  local minimum_ram minimum_disk available_ram available_disk
  minimum_ram="$(tvt_manifest_value "${BUNDLE}" minimum_ram_mib)"
  minimum_disk="$(tvt_manifest_value "${BUNDLE}" minimum_disk_mib)"
  available_ram="$(awk '/^MemTotal:/ {print int($2 / 1024)}' "${TVT_MEMINFO_FILE:-/proc/meminfo}")"
  available_disk="$(df -Pm "${TVT_DISK_CHECK_PATH:-/opt}" | awk 'NR == 2 {print $4}')"
  (( available_ram >= minimum_ram )) || tvt_fail \
    "at least ${minimum_ram} MiB RAM is required; found ${available_ram} MiB"
  (( available_disk >= minimum_disk )) || tvt_fail \
    "at least ${minimum_disk} MiB free disk is required; found ${available_disk} MiB"
  tvt_log "host preflight passed"
}

install_host_packages() {
  export DEBIAN_FRONTEND=noninteractive
  local -a packages=(
    ca-certificates curl gnupg python3 python3-venv docker.io postgresql-16
    jq openssl util-linux coreutils tar
  )
  if [[ ${MODE} == online ]]; then
    packages+=(git git-lfs software-properties-common)
    apt-get update
    apt-get install -y --no-install-recommends "${packages[@]}"
  else
    local package_directory="${BUNDLE}/packages/apt"
    [[ -d ${package_directory} ]] || tvt_fail "offline APT package directory is missing"
    shopt -s nullglob
    local -a debs=("${package_directory}"/*.deb)
    shopt -u nullglob
    (( ${#debs[@]} > 0 )) || tvt_fail "offline APT package directory contains no .deb files"
    apt-get install -y --no-install-recommends "${debs[@]}"
  fi
  for command_name in curl docker jq openssl python3 psql systemctl; do
    command -v "${command_name}" >/dev/null 2>&1 || tvt_fail "host package installation did not provide ${command_name}"
  done
}

enable_host_services() {
  systemctl enable --now docker.service postgresql.service
  systemctl is-active --quiet docker.service
  systemctl is-active --quiet postgresql.service
}

install_hardware() {
  local -a arguments=(--mode "${MODE}")
  if [[ ${MODE} == offline ]]; then arguments+=(--bundle "${BUNDLE}"); fi
  if ${ALLOW_UNVERIFIED_HARDWARE}; then arguments+=(--allow-unverified-hardware); fi
  bash "${BUNDLE}/scripts/install-tvt-hardware-drivers.sh" "${arguments[@]}"
  [[ -f ${REBOOT_MARKER} ]] || tvt_fail "hardware installer did not create its reboot marker"
}

verify_post_reboot() {
  [[ -e /dev/dri/renderD128 ]] || tvt_fail "/dev/dri/renderD128 is missing"
  [[ -e /dev/accel/accel0 ]] || tvt_fail "/dev/accel/accel0 is missing"
  if ! grep -Eq '^(i915|xe) ' /proc/modules; then tvt_fail "neither i915 nor xe is loaded"; fi
  grep -Eq '^intel_vpu ' /proc/modules || tvt_fail "intel_vpu is not loaded"
  timeout 30s vainfo >/dev/null 2>&1 || tvt_fail "vainfo verification failed"
  timeout 30s clinfo -l >/dev/null 2>&1 || tvt_fail "clinfo platform listing failed"
  local openvino_python="${TVT_OPENVINO_PYTHON:-/opt/apexfabric/openvino-env/bin/python}"
  [[ -x ${openvino_python} ]] || tvt_fail "OpenVINO environment is missing"
  "${openvino_python}" - <<'PY'
import openvino
devices = {name.split(".", 1)[0] for name in openvino.Core().available_devices}
missing = {"CPU", "GPU", "NPU"} - devices
if missing:
    raise SystemExit("OpenVINO devices missing: " + ", ".join(sorted(missing)))
PY
  systemctl is-active --quiet docker.service || tvt_fail "Docker is not active"
  docker info >/dev/null 2>&1 || tvt_fail "Docker is not healthy"
  systemctl is-active --quiet postgresql.service || tvt_fail "PostgreSQL is not active"
  pg_isready --quiet || tvt_fail "PostgreSQL is not accepting connections"
  tvt_log "post-reboot hardware and service verification passed"
}

preflight_host
if ${VERIFY_ONLY}; then
  [[ "$(tvt_json_get "${PREPARE_STATE}" status 2>/dev/null || true)" == prepared ]] || \
    tvt_fail "host preparation state is not prepared"
  [[ ! -e ${REBOOT_MARKER} ]] || tvt_fail "hardware reboot-required marker still exists"
  verify_post_reboot
  tvt_log "Host preparation verification succeeded."
  exit 0
fi

current_status="$(tvt_json_get "${PREPARE_STATE}" status 2>/dev/null || true)"
if [[ ${current_status} == prepared ]]; then
  [[ ! -e ${REBOOT_MARKER} ]] || tvt_fail "prepared state conflicts with the reboot-required marker"
  verify_post_reboot
  tvt_log "Host is already prepared; no changes were made."
  exit 0
fi

if [[ ${current_status} == reboot_required ]]; then
  installed_boot="$(tvt_json_get "${PREPARE_STATE}" driver_install_boot_id 2>/dev/null || true)"
  current_boot="$(tr -d '\n' <"${BOOT_ID_FILE}")"
  [[ -n ${installed_boot} && ${installed_boot} != "${current_boot}" ]] || tvt_fail \
    "reboot has not occurred since the driver stage; reboot and rerun the same prepare command"
  verify_post_reboot
  rm -f -- "${REBOOT_MARKER}"
  tvt_write_state "${PREPARE_STATE}" prepared "${RELEASE_VERSION}" post_reboot_verified
  tvt_log "Host preparation completed and verified."
  exit 0
fi

install_host_packages
enable_host_services
install_hardware
boot_id="$(tr -d '\n' <"${BOOT_ID_FILE}")"
tvt_write_state "${PREPARE_STATE}" reboot_required "${RELEASE_VERSION}" drivers_installed "${boot_id}"
cat <<'EOF'
Host preparation stage 1 completed.
Reboot required.
After reboot, rerun the same prepare command.
EOF
