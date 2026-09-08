#!/usr/bin/env bash
set -Eeuo pipefail
umask 027

readonly STATE_DIR=/var/lib/tvt-online-test-install
readonly REBOOT_EXIT=194
ARCHIVE=""
CHECKSUM=""
SITE_ID=""
EDGE_ID=""
SITE_NAME=""
TIMEZONE=UTC
INSTALL_GROUP=""
APPROVE_AGENT_REMOVAL=false

log() { printf 'tvt-online-install: %s\n' "$*"; }
fail() { printf 'tvt-online-install: ERROR: %s\n' "$*" >&2; exit 1; }
usage() {
  echo "usage: sudo bash scripts/install-tvt-online-steps-1-10.sh --archive FILE --checksum FILE --site-id ID --edge-id ID --site-name NAME [--timezone ZONE] [--install-group GROUP] [--approve-k3s-agent-removal]" >&2
}

while (($#)); do
  case "$1" in
    --archive) ARCHIVE="${2:-}"; shift 2 ;;
    --checksum) CHECKSUM="${2:-}"; shift 2 ;;
    --site-id) SITE_ID="${2:-}"; shift 2 ;;
    --edge-id) EDGE_ID="${2:-}"; shift 2 ;;
    --site-name) SITE_NAME="${2:-}"; shift 2 ;;
    --timezone) TIMEZONE="${2:-}"; shift 2 ;;
    --install-group) INSTALL_GROUP="${2:-}"; shift 2 ;;
    --approve-k3s-agent-removal) APPROVE_AGENT_REMOVAL=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage; exit 2 ;;
  esac
done

[[ ${EUID} -eq 0 ]] || fail "run with sudo"
[[ -f ${ARCHIVE} && ! -L ${ARCHIVE} ]] || fail "--archive must be a regular, non-symlinked file"
[[ -f ${CHECKSUM} && ! -L ${CHECKSUM} ]] || fail "--checksum must be a regular, non-symlinked file"
[[ ${SITE_ID} =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] || fail "--site-id must be DNS-safe"
[[ ${EDGE_ID} =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] || fail "--edge-id must be DNS-safe"
[[ -n ${SITE_NAME} && ${SITE_NAME} != *$'\n'* && ${SITE_NAME} != *'|'* ]] || fail "--site-name is invalid"
[[ -f /usr/share/zoneinfo/${TIMEZONE} && ${TIMEZONE} != *'..'* ]] || fail "--timezone is not installed"
if [[ -z ${INSTALL_GROUP} ]]; then
  [[ -n ${SUDO_USER:-} && ${SUDO_USER} != root ]] || fail "supply --install-group when not invoked through sudo"
  INSTALL_GROUP="$(id -gn "${SUDO_USER}")"
fi
getent group "${INSTALL_GROUP}" >/dev/null || fail "install group does not exist: ${INSTALL_GROUP}"
# shellcheck disable=SC1091
source /etc/os-release
[[ ${ID:-} == ubuntu && ${VERSION_ID:-} == 24.04 ]] || fail "Ubuntu 24.04 is required"
[[ $(dpkg --print-architecture) == amd64 ]] || fail "amd64 is required"
grep -Eiq '^model name[[:space:]]*:.*Intel.*Core.*Ultra.*285H' /proc/cpuinfo || fail "the qualified Intel Core Ultra 9 285H was not detected"
dpkg --compare-versions "$(uname -r | cut -d- -f1)" ge 6.8 || fail "active kernel 6.8 or newer is required"
(( $(awk '/MemTotal:/ {print int($2 / 1024)}' /proc/meminfo) >= 16384 )) || fail "at least 16384 MiB RAM is required"
(( $(df -Pm /opt | awk 'NR == 2 {print $4}') >= 20480 )) || fail "at least 20480 MiB free under /opt is required"

ARCHIVE="$(cd "$(dirname "${ARCHIVE}")" && pwd -P)/$(basename "${ARCHIVE}")"
CHECKSUM="$(cd "$(dirname "${CHECKSUM}")" && pwd -P)/$(basename "${CHECKSUM}")"
kit_name="$(basename "${ARCHIVE}" .tar.gz)"
[[ ${kit_name} == tvt-edge-online-test-kit-* ]] || fail "archive name is not a TVT online test kit"
KIT_ROOT="/opt/tvt/${kit_name}"
SOURCE_ROOT="${KIT_ROOT}/source"
TVT_EXEC_PATH=/opt/tvt/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

write_stage() {
  local temporary
  temporary="$(mktemp "${STATE_DIR}/.stage.XXXXXX")"
  printf '%s\n' "$1" >"${temporary}"
  chmod 0600 "${temporary}"
  mv -f "${temporary}" "${STATE_DIR}/stage"
}

capture_packages() {
  dpkg-query -W -f='${binary:Package}\t${Version}\n' 2>/dev/null | sort -u
}

capture_package_delta() {
  local current="${STATE_DIR}/current-packages.tsv"
  capture_packages >"${current}"
  python3 - "${STATE_DIR}/baseline-packages.tsv" "${current}" \
    "${STATE_DIR}/new-packages.txt" "${STATE_DIR}/changed-packages.tsv" \
    "${STATE_DIR}/removed-packages.tsv" <<'PY'
import pathlib, sys
before_path, after_path, new_path, changed_path, removed_path = map(pathlib.Path, sys.argv[1:])
def load(path):
    return dict(line.split("\t", 1) for line in path.read_text().splitlines() if "\t" in line)
before, after = load(before_path), load(after_path)
new_path.write_text("".join(f"{name}\n" for name in sorted(after.keys() - before.keys())))
changed_path.write_text("".join(
    f"{name}\t{before[name]}\t{after[name]}\n"
    for name in sorted(before.keys() & after.keys()) if before[name] != after[name]
))
removed_path.write_text("".join(
    f"{name}\t{before[name]}\n" for name in sorted(before.keys() - after.keys())
))
PY
  chmod 0600 "${current}" "${STATE_DIR}/new-packages.txt" \
    "${STATE_DIR}/changed-packages.tsv" "${STATE_DIR}/removed-packages.tsv"
  apt-mark showmanual | sort -u >"${STATE_DIR}/current-manual.txt"
  comm -13 "${STATE_DIR}/baseline-manual.txt" "${STATE_DIR}/current-manual.txt" >"${STATE_DIR}/newly-manual.txt"
  chmod 0600 "${STATE_DIR}/current-manual.txt" "${STATE_DIR}/newly-manual.txt"
}

capture_docker_delta() {
  if command -v docker >/dev/null 2>&1; then
    docker image ls --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | sort -u >"${STATE_DIR}/current-docker-tags.txt"
    comm -13 "${STATE_DIR}/baseline-docker-tags.txt" "${STATE_DIR}/current-docker-tags.txt" >"${STATE_DIR}/new-docker-tags.txt"
  else
    : >"${STATE_DIR}/current-docker-tags.txt"
    : >"${STATE_DIR}/new-docker-tags.txt"
  fi
  chmod 0600 "${STATE_DIR}/current-docker-tags.txt" "${STATE_DIR}/new-docker-tags.txt"
}

detect_k3s_state() {
  if systemctl cat k3s-agent.service >/dev/null 2>&1; then
    printf 'agent\n'
  elif systemctl cat k3s.service >/dev/null 2>&1; then
    printf 'server\n'
  elif systemctl cat kubelet.service >/dev/null 2>&1 || [[ -e /usr/local/bin/k3s || -e /etc/rancher/k3s || -e /var/lib/rancher/k3s || -e /etc/kubernetes || -e /var/lib/kubelet ]]; then
    printf 'partial\n'
  else
    printf 'clean\n'
  fi
}

initialize_baseline() {
  if [[ -d ${STATE_DIR} ]]; then
    [[ ! -L ${STATE_DIR} ]] || fail "state directory is a symlink"
    return
  fi
  for path in /etc/tvt /var/lib/tvt /var/lib/tvt-alert /opt/apexfabric/openvino-env "${KIT_ROOT}"; do
    [[ ! -e ${path} ]] || fail "pre-existing TVT path prevents a clean baseline: ${path}"
  done
  ! id tvt-edge >/dev/null 2>&1 || fail "pre-existing tvt-edge account prevents a clean baseline"
  ! id tvt-alert >/dev/null 2>&1 || fail "pre-existing tvt-alert account prevents a clean baseline"
  ! systemctl cat tvt-local-registry.service >/dev/null 2>&1 || fail "pre-existing TVT registry unit prevents a clean baseline"
  k3s_state="$(detect_k3s_state)"
  case "${k3s_state}" in
    clean) ;;
    agent) ${APPROVE_AGENT_REMOVAL} || fail "a pre-existing K3s agent requires --approve-k3s-agent-removal" ;;
    server|partial) fail "pre-existing K3s ${k3s_state} state must be reconciled before this installer changes the host" ;;
  esac
  install -d -o root -g root -m 0700 "${STATE_DIR}"
  capture_packages >"${STATE_DIR}/baseline-packages.tsv"
  apt-mark showmanual | sort -u >"${STATE_DIR}/baseline-manual.txt"
  if command -v docker >/dev/null 2>&1; then
    docker image ls --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | sort -u >"${STATE_DIR}/baseline-docker-tags.txt"
  else
    : >"${STATE_DIR}/baseline-docker-tags.txt"
  fi
  for unit in docker.service postgresql.service; do
    printf '%s\t%s\n' "${unit}" "$(systemctl is-enabled "${unit}" 2>/dev/null || true)"
  done >"${STATE_DIR}/baseline-unit-enabled.tsv"
  for unit in docker.service postgresql.service; do
    printf '%s\t%s\n' "${unit}" "$(systemctl is-active "${unit}" 2>/dev/null || true)"
  done >"${STATE_DIR}/baseline-unit-active.tsv"
  for path in /var/lib/docker /var/lib/containerd /etc/docker /var/lib/postgresql/16/main; do
    if [[ -e ${path} || -L ${path} ]]; then
      printf '%s\ttrue\n' "${path}"
    else
      printf '%s\tfalse\n' "${path}"
    fi
  done >"${STATE_DIR}/baseline-paths.tsv"
  if grep -RqsE 'ppa\.launchpadcontent\.net/kobuk-team/intel-graphics|kobuk-team/ubuntu.*intel-graphics' /etc/apt/sources.list /etc/apt/sources.list.d 2>/dev/null; then
    printf 'true\n' >"${STATE_DIR}/intel-ppa-preexisting"
  else
    printf 'false\n' >"${STATE_DIR}/intel-ppa-preexisting"
  fi
  printf '%s\n' "${k3s_state}" >"${STATE_DIR}/baseline-k3s-state"
  printf '%s\n' "${ARCHIVE}" >"${STATE_DIR}/archive"
  printf '%s\n' "${CHECKSUM}" >"${STATE_DIR}/checksum"
  printf '%s\n' "${KIT_ROOT}" >"${STATE_DIR}/kit-root"
  printf '%s\n' "${INSTALL_GROUP}" >"${STATE_DIR}/install-group"
  printf '%s\n' "${SITE_ID}" >"${STATE_DIR}/site-id"
  printf '%s\n' "${EDGE_ID}" >"${STATE_DIR}/edge-id"
  printf '%s\n' "${SITE_NAME}" >"${STATE_DIR}/site-name"
  printf '%s\n' "${TIMEZONE}" >"${STATE_DIR}/timezone"
  : >"${STATE_DIR}/new-packages.txt"
  : >"${STATE_DIR}/changed-packages.tsv"
  : >"${STATE_DIR}/removed-packages.tsv"
  : >"${STATE_DIR}/newly-manual.txt"
  : >"${STATE_DIR}/new-docker-tags.txt"
  chmod 0600 "${STATE_DIR}"/*
  write_stage 0
}

initialize_baseline
[[ $(<"${STATE_DIR}/archive") == "${ARCHIVE}" ]] || fail "resume archive differs from the baseline"
[[ $(<"${STATE_DIR}/checksum") == "${CHECKSUM}" ]] || fail "resume checksum differs from the baseline"
[[ $(<"${STATE_DIR}/kit-root") == "${KIT_ROOT}" ]] || fail "resume kit root differs from the baseline"
[[ $(<"${STATE_DIR}/install-group") == "${INSTALL_GROUP}" ]] || fail "resume install group differs from the baseline"
[[ $(<"${STATE_DIR}/site-id") == "${SITE_ID}" && $(<"${STATE_DIR}/edge-id") == "${EDGE_ID}" ]] || fail "resume site identity differs from the baseline"
[[ $(<"${STATE_DIR}/site-name") == "${SITE_NAME}" && $(<"${STATE_DIR}/timezone") == "${TIMEZONE}" ]] || fail "resume site metadata differs from the baseline"
stage="$(<"${STATE_DIR}/stage")"
[[ ${stage} =~ ^[0-8]$ ]] || fail "invalid installer stage state"
baseline_k3s="$(<"${STATE_DIR}/baseline-k3s-state")"
case "${baseline_k3s}" in
  clean) ;;
  agent) ${APPROVE_AGENT_REMOVAL} || fail "a pre-existing K3s agent requires --approve-k3s-agent-removal on every invocation" ;;
  server|partial) fail "pre-existing K3s ${baseline_k3s} state must be reconciled before this installer changes the host" ;;
  *) fail "invalid baseline K3s state" ;;
esac

if ((stage == 0)) && [[ -e /var/run/reboot-required ]]; then
  log "Ubuntu requires a reboot before the driver recipe can be locked. Reboot, then rerun this same command."
  exit "${REBOOT_EXIT}"
fi

if ((stage < 1)); then
  log "verifying outer checksum and extracting ${kit_name}"
  python3 - "${CHECKSUM}" "$(basename "${ARCHIVE}")" <<'PY'
import pathlib
import re
import sys

checksum_path, expected_name = pathlib.Path(sys.argv[1]), sys.argv[2]
lines = [line for line in checksum_path.read_text(encoding="utf-8").splitlines() if line.strip()]
if len(lines) != 1:
    raise SystemExit("outer checksum file must contain exactly one entry")
match = re.fullmatch(r"([0-9a-fA-F]{64}) [ *](.+)", lines[0])
if not match or pathlib.PurePath(match.group(2)).name != expected_name or match.group(2) != expected_name:
    raise SystemExit("outer checksum entry does not name the selected archive")
PY
  (cd "$(dirname "${ARCHIVE}")" && sha256sum --check "$(basename "${CHECKSUM}")")
  [[ ! -e ${KIT_ROOT} ]] || fail "kit root unexpectedly exists before extraction"
  install -d -o root -g root -m 0755 /opt/tvt
  tar --extract --gzip --no-same-owner --file "${ARCHIVE}" --directory /opt/tvt
  chown -R root:"${INSTALL_GROUP}" "${KIT_ROOT}"
  chmod -R u=rwX,g=rX,o= "${KIT_ROOT}"
  (cd "${KIT_ROOT}" && sha256sum --check --quiet checksums.sha256)
  [[ -f ${KIT_ROOT}/artifact-manifest.json ]] || fail "internal artifact manifest is missing"
  python3 - "${KIT_ROOT}/artifact-manifest.json" <<'PY'
import json
import pathlib
import re
import sys

manifest = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
expected = {
    "schema_version": 1,
    "kit_type": "online-test",
    "production_offline_release": False,
    "architecture": "amd64",
    "target_os": "ubuntu-24.04",
    "target_hardware": "intel-285h",
}
for key, value in expected.items():
    if manifest.get(key) != value:
        raise SystemExit(f"artifact manifest {key} must be {value!r}")
if not re.fullmatch(r"[0-9a-f]{40}", str(manifest.get("source_commit", ""))):
    raise SystemExit("artifact manifest source_commit is invalid")
if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+\+k3s[0-9]+", str(manifest.get("pins", {}).get("k3s_version", ""))):
    raise SystemExit("artifact manifest K3s pin is invalid")
PY
  write_stage 1
  stage=1
  log "archive verification passed; /opt/tvt is root:root 0755 and the kit is root:${INSTALL_GROUP} with no other access"
fi

[[ -d ${SOURCE_ROOT} && ! -L ${SOURCE_ROOT} ]] || fail "extracted source root is missing or unsafe"
cd "${SOURCE_ROOT}"

if ((stage < 2)); then
  log "installing host prerequisites and locking the Intel 285H driver recipe"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y --no-install-recommends ca-certificates curl gnupg python3 python3-venv docker.io postgresql-16 jq openssl util-linux coreutils tar git git-lfs software-properties-common
  capture_package_delta
  systemctl enable --now docker.service postgresql.service
  systemctl is-active --quiet docker.service
  systemctl is-active --quiet postgresql.service
  trap 'capture_package_delta || true' ERR
  bash scripts/install-tvt-hardware-drivers.sh --mode online
  trap - ERR
  cat /proc/sys/kernel/random/boot_id >"${STATE_DIR}/driver-install-boot-id"
  chmod 0600 "${STATE_DIR}/driver-install-boot-id"
  capture_package_delta
  write_stage 2
  log "driver installation completed. Reboot, then rerun this same command."
  exit "${REBOOT_EXIT}"
fi

if ((stage < 3)); then
  [[ $(<"${STATE_DIR}/driver-install-boot-id") != "$(cat /proc/sys/kernel/random/boot_id)" ]] || fail "reboot after driver installation has not occurred"
  log "verifying Intel GPU, NPU, VA-API, OpenCL, and OpenVINO"
  [[ -e /dev/dri/renderD128 && -e /dev/accel/accel0 ]] || fail "required Intel device nodes are missing"
  lsmod | grep -Eq '^(i915|xe)\b' || fail "Intel GPU module is not loaded"
  lsmod | grep -Eq '^intel_vpu\b' || fail "Intel NPU module is not loaded"
  vainfo --display drm --device /dev/dri/renderD128 >/dev/null
  clinfo -l
  /opt/apexfabric/openvino-env/bin/python - <<'PY'
from openvino import Core
devices = {name.split(".", 1)[0] for name in Core().available_devices}
missing = {"CPU", "GPU", "NPU"} - devices
print("OpenVINO devices:", sorted(devices))
if missing:
    raise SystemExit("missing OpenVINO devices: " + ", ".join(sorted(missing)))
PY
  rm -f /var/lib/tvt/hardware-driver-reboot-required
  write_stage 3
  stage=3
  log "hardware verification passed: renderD128, accel0, GPU/NPU modules, VA-API, OpenCL, and OpenVINO CPU/GPU/NPU are available"
fi

if ((stage < 4)); then
  log "installing the checksum-bundled TVT Python wheelhouse"
  mapfile -t wheels < <(find "${KIT_ROOT}/wheels" -maxdepth 1 -type f -name 'tvt_runtime-*.whl' -print)
  [[ ${#wheels[@]} -eq 1 ]] || fail "expected exactly one tvt_runtime wheel"
  python3 -m venv --clear /opt/tvt/venv
  /opt/tvt/venv/bin/python -m pip install --disable-pip-version-check --no-index --find-links="${KIT_ROOT}/wheels" "${wheels[0]}"
  chmod -R go+rX /opt/tvt/venv
  /opt/tvt/venv/bin/python -c 'import tvt_edge'
  write_stage 4
  stage=4
  log "application wheelhouse verification passed"
fi

if ((stage < 5)); then
  case "${baseline_k3s}" in
    clean) ;;
    agent)
      ${APPROVE_AGENT_REMOVAL} || fail "a pre-existing K3s agent requires --approve-k3s-agent-removal"
      if systemctl cat k3s-agent.service >/dev/null 2>&1; then
        [[ -x /usr/local/bin/k3s-agent-uninstall.sh ]] || fail "K3s agent uninstaller is missing"
        /usr/local/bin/k3s-agent-uninstall.sh
      fi
      printf 'removed; original agent credentials were not retained\n' >"${STATE_DIR}/preexisting-agent-removal"
      chmod 0600 "${STATE_DIR}/preexisting-agent-removal"
      ;;
    server|partial) fail "pre-existing K3s ${baseline_k3s} state must be reconciled outside this installer" ;;
    *) fail "invalid baseline K3s state" ;;
  esac
  log "installing the local registry and pinned K3s server"
  bash scripts/install-local-registry.sh --image-archive "${KIT_ROOT}/images/registry.tar"
  capture_docker_delta
  curl --fail --silent --show-error http://127.0.0.1:5000/v2/ >/dev/null
  bash scripts/install-k3s-single-node.sh --installer "${KIT_ROOT}/k3s/install.sh" --k3s-binary "${KIT_ROOT}/k3s/k3s"
  [[ $(/usr/local/bin/k3s --version | awk 'NR == 1 {print $3}') == "$(. config/platform.env; printf '%s' "${K3S_VERSION}")" ]] || fail "installed K3s does not match the kit pin"
  systemctl is-active --quiet tvt-local-registry.service
  systemctl is-active --quiet k3s.service
  /usr/local/bin/k3s kubectl get nodes -o wide
  write_stage 5
  stage=5
  log "registry and pinned single-node K3s verification passed"
fi

if ((stage < 6)); then
  log "publishing and installing the K3s node-management plane"
  env PATH="${TVT_EXEC_PATH}" bash scripts/publish-control-images.sh --registry 127.0.0.1:5000 --scheme http --archive-dir "${KIT_ROOT}/images" --lock-output /var/lib/tvt/node-management-images.lock.json
  capture_docker_delta
  env PATH="${TVT_EXEC_PATH}" bash scripts/install-k3s-plane.sh --image-lock /var/lib/tvt/node-management-images.lock.json
  env PATH="${TVT_EXEC_PATH}" bash scripts/verify-k3s-plane.sh
  write_stage 6
  stage=6
  log "node-management plane verification passed"
fi

if ((stage < 7)); then
  log "importing and scheduling immutable Traffic image synchronization"
  catalog_source="${SOURCE_ROOT}/solution-packs/catalog/traffic-edge-runtime-2026.08.21-v4"
  env PATH="${TVT_EXEC_PATH}" bash scripts/import-pipeline-traffic-image.sh --mode archive --archive-file "${KIT_ROOT}/images/traffic-edge-runtime-v4.tar" --metadata-directory "${catalog_source}" --work-dir /var/lib/tvt/pipeline/work --lock-output /var/lib/tvt/pipeline/traffic-image.lock.json --concurrency-lock /var/lib/tvt/pipeline/import.lock
  bash scripts/install-pipeline-image-sync.sh --archive-file "${KIT_ROOT}/images/traffic-edge-runtime-v4.tar" --metadata-directory "${catalog_source}"
  systemctl start tvt-pipeline-image-sync.service
  bash /opt/tvt/scripts/verify-pipeline-image-sync.sh
  capture_docker_delta
  write_stage 7
  stage=7
  log "Traffic image import and synchronization verification passed; no Traffic workload was deployed"
fi

if ((stage < 8)); then
  log "configuring PostgreSQL and the TVT management plane"
  bash scripts/bootstrap-postgresql.sh --venv /opt/tvt/venv
  bash scripts/install-tvt-kubeconfig.sh
  site_count="$(runuser -u postgres -- psql -d tvt -Atc 'SELECT count(*) FROM sites')"
  if [[ ${site_count} == 0 ]]; then
    runuser -u tvt-edge -- env TVT_DATABASE_URL=postgresql+psycopg:///tvt /opt/tvt/venv/bin/tvt-edge init-site "${SITE_ID}" "${EDGE_ID}" "${SITE_NAME}" --timezone "${TIMEZONE}"
  elif [[ ${site_count} != 1 ]]; then
    fail "TVT database contains ${site_count} sites"
  else
    existing_site="$(runuser -u postgres -- psql -d tvt -At -F '|' -c 'SELECT site_key,edge_id,display_name,timezone_name FROM sites LIMIT 1')"
    [[ ${existing_site} == "${SITE_ID}|${EDGE_ID}|${SITE_NAME}|${TIMEZONE}" ]] || fail "existing site does not match requested site identity"
  fi
  systemctl enable --now tvt-edge.service tvt-camera-sync.service tvt-retention.timer tvt-k3s-watchdog.timer tvt-pipeline-image-sync.timer
  runuser -u tvt-edge -- env TVT_DATABASE_URL=postgresql+psycopg:///tvt TVT_KUBECONFIG=/etc/tvt/kubeconfig /opt/tvt/venv/bin/tvt-edge refresh-solutions
  bash scripts/install-traffic-qualification.sh
  systemctl is-active --quiet postgresql.service
  systemctl is-active --quiet tvt-edge.service
  systemctl is-active --quiet tvt-camera-sync.service
  for unit in tvt-retention.timer tvt-k3s-watchdog.timer tvt-pipeline-image-sync.timer; do
    systemctl is-enabled --quiet "${unit}"
  done
  ready=false
  for _attempt in {1..30}; do
    if curl --fail --silent --show-error --max-time 5 http://127.0.0.1:8088/api/v1/health >/dev/null; then
      ready=true
      break
    fi
    sleep 2
  done
  ${ready} || fail "TVT health endpoint did not become ready within 60 seconds"
  curl --fail --silent --show-error --max-time 5 http://127.0.0.1:8088/api/v1/solutions | python3 -m json.tool >/dev/null
  runuser -u tvt-edge -- env TVT_DATABASE_URL=postgresql+psycopg:///tvt TVT_KUBECONFIG=/etc/tvt/kubeconfig /opt/tvt/venv/bin/tvt-edge check
  capture_package_delta
  write_stage 8
  log "management-plane verification passed"
fi

log "Steps 1-10 are complete. Traffic workloads, cameras, monitoring, and email notifications were not deployed or configured."
log "State and reset baseline: ${STATE_DIR}"
