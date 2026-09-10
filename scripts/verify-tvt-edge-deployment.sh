#!/usr/bin/env bash
set -uo pipefail
umask 077

BUNDLE=""

usage() {
  cat <<'EOF'
usage: sudo ./verify-tvt-edge-deployment.sh --bundle DIR

Read-only post-install audit for a TVT Intel edge deployment.

The report verifies exact Debian/Python package versions, Intel hardware and
runtime availability, systemd services, Docker, PostgreSQL, the local registry,
K3s node health, TVT health, image synchronization, and Kubernetes pods.

Exit status:
  0  every required check passed
  1  one or more required checks failed
  2  invalid invocation or bundle
EOF
}

while (($#)); do
  case "$1" in
    --bundle)
      [[ -n ${2:-} ]] || { usage >&2; exit 2; }
      BUNDLE="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'unknown option: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

[[ ${EUID} -eq 0 ]] || {
  printf '%s\n' 'run this script with sudo' >&2
  exit 2
}
[[ -n ${BUNDLE} && -d ${BUNDLE} && ! -L ${BUNDLE} ]] || {
  printf 'bundle is missing, not a directory, or symlinked: %s\n' "${BUNDLE:-<unset>}" >&2
  exit 2
}
BUNDLE="$(cd "${BUNDLE}" && pwd -P)"

for command_name in curl docker dpkg-deb dpkg-query find k3s python3 sort systemctl timeout; do
  command -v "${command_name}" >/dev/null 2>&1 || {
    printf 'required command is unavailable: %s\n' "${command_name}" >&2
    exit 2
  }
done
for required_path in \
  manifest.json checksums.sha256 config/platform.env \
  hardware/driver-recipe.json hardware/linux-npu-driver.tar.gz packages/apt \
  scripts/tvt-edge-operations.sh; do
  [[ -e ${BUNDLE}/${required_path} ]] || {
    printf 'bundle is missing required path: %s\n' "${required_path}" >&2
    exit 2
  }
done

work_directory="$(mktemp -d)"
trap 'rm -rf -- "${work_directory}"' EXIT
expected_records="${work_directory}/expected-packages.tsv"
raw_records="${work_directory}/raw-packages.tsv"
: >"${raw_records}"

failures=0
warnings=0
passes=0

status_count() {
  case "$1" in
    PASS) ((passes += 1)) ;;
    WARN) ((warnings += 1)) ;;
    FAIL) ((failures += 1)) ;;
  esac
}

clean_value() {
  tr '\t\r\n' '   ' | sed -E 's/[[:space:]]+/ /g; s/^ //; s/ $//'
}

section() {
  printf '\n%s\n' "$1"
}

package_header() {
  printf '%-16s %-40s %-38s %-38s %-6s\n' \
    CLASS PACKAGE EXPECTED_VERSION INSTALLED_VERSION RESULT
  printf '%-16s %-40s %-38s %-38s %-6s\n' \
    '----------------' '----------------------------------------' \
    '--------------------------------------' '--------------------------------------' '------'
}

check_header() {
  printf '%-30s %-30s %-48s %-6s\n' CHECK EXPECTED ACTUAL RESULT
  printf '%-30s %-30s %-48s %-6s\n' \
    '------------------------------' '------------------------------' \
    '------------------------------------------------' '------'
}

check_row() {
  local name="$1" expected="$2" actual="$3" result="$4"
  actual="$(printf '%s' "${actual}" | clean_value)"
  [[ -n ${actual} ]] || actual=-
  printf '%-30s %-30s %-48s %-6s\n' \
    "${name}" "${expected}" "${actual}" "${result}"
  status_count "${result}"
}

add_debian_package() {
  local package_class="$1" package_file="$2"
  local package_name package_version
  package_name="$(dpkg-deb -f "${package_file}" Package 2>/dev/null)" || return 1
  package_version="$(dpkg-deb -f "${package_file}" Version 2>/dev/null)" || return 1
  [[ ${package_name} != *$'\t'* && ${package_version} != *$'\t'* ]] || return 1
  printf 'deb\t%s\t%s\t%s\t-\n' \
    "${package_class}" "${package_name}" "${package_version}" >>"${raw_records}"
}

add_python_wheel() {
  local package_class="$1" interpreter="$2" wheel="$3"
  python3 - "${package_class}" "${interpreter}" "${wheel}" >>"${raw_records}" <<'PY'
import email.parser
import pathlib
import sys
import zipfile

package_class, interpreter, wheel_name = sys.argv[1:]
wheel = pathlib.Path(wheel_name)
with zipfile.ZipFile(wheel) as archive:
    records = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
    if len(records) != 1:
        raise SystemExit(f"invalid wheel metadata count in {wheel}")
    metadata = email.parser.Parser().parsestr(
        archive.read(records[0]).decode("utf-8")
    )
name = metadata.get("Name")
version = metadata.get("Version")
if not name or not version or any(character in name + version for character in "\t\n"):
    raise SystemExit(f"invalid wheel metadata in {wheel}")
print("\t".join(("python", package_class, name, version, interpreter)))
PY
}

declare -A host_packages=()
for package_name in \
  ca-certificates curl docker.io gnupg openssl postgresql-16 python3 python3-venv; do
  host_packages["${package_name}"]=1
done

declare -A driver_packages=()
while IFS= read -r package_name; do
  [[ -n ${package_name} ]] && driver_packages["${package_name}"]=1
done < <(
  python3 - "${BUNDLE}/hardware/driver-recipe.json" <<'PY'
import json
import pathlib
import sys

recipe = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
for name in sorted(recipe.get("apt", {})):
    print(name)
PY
)

shopt -s nullglob
apt_packages=("${BUNDLE}"/packages/apt/*.deb)
shopt -u nullglob
(( ${#apt_packages[@]} > 0 )) || {
  printf '%s\n' 'bundle contains no offline Debian packages' >&2
  exit 2
}
for package_file in "${apt_packages[@]}"; do
  package_name="$(dpkg-deb -f "${package_file}" Package 2>/dev/null)" || {
    printf 'cannot read package metadata: %s\n' "${package_file}" >&2
    exit 2
  }
  package_class=APT-DEPENDENCY
  if [[ -n ${driver_packages[${package_name}]:-} ]]; then
    package_class=INTEL-DRIVER
  elif [[ -n ${host_packages[${package_name}]:-} ]]; then
    package_class=HOST
  fi
  add_debian_package "${package_class}" "${package_file}" || {
    printf 'cannot read package metadata: %s\n' "${package_file}" >&2
    exit 2
  }
done

npu_directory="${work_directory}/npu"
python3 - "${BUNDLE}/hardware/linux-npu-driver.tar.gz" "${npu_directory}" <<'PY'
import pathlib
import sys
import tarfile

archive = pathlib.Path(sys.argv[1])
destination = pathlib.Path(sys.argv[2])
destination.mkdir(mode=0o700)
with tarfile.open(archive) as source:
    source.extractall(destination, filter="data")
PY
while IFS= read -r -d '' package_file; do
  add_debian_package INTEL-NPU "${package_file}" || {
    printf 'cannot read NPU package metadata: %s\n' "${package_file}" >&2
    exit 2
  }
done < <(find "${npu_directory}" -type f -name '*.deb' -print0)

readarray -t release_metadata < <(
  python3 - "${BUNDLE}/manifest.json" <<'PY'
import json
import pathlib
import sys

manifest = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
print(manifest["release_version"])
print(manifest["k3s_version"])
print(manifest["os_id"])
print(manifest["os_version"])
print(manifest["architecture"])
PY
)
(( ${#release_metadata[@]} == 5 )) || {
  printf '%s\n' 'manifest metadata is incomplete' >&2
  exit 2
}
release_version="${release_metadata[0]}"
expected_k3s="${release_metadata[1]}"
expected_os="${release_metadata[2]} ${release_metadata[3]}"
expected_architecture="${release_metadata[4]}"

shopt -s nullglob
for wheel in "${BUNDLE}"/hardware/wheels/*.whl; do
  add_python_wheel OPENVINO /opt/apexfabric/openvino-env/bin/python "${wheel}" || exit 2
done
for wheel in "${BUNDLE}"/hardware/voyager-wheels/*.whl; do
  add_python_wheel VOYAGER /opt/apexfabric/voyager-1.6.1/bin/python "${wheel}" || exit 2
done
for wheel in "${BUNDLE}"/wheels/*.whl; do
  add_python_wheel TVT-RUNTIME "/opt/tvt/releases/${release_version}/venv/bin/python" "${wheel}" || exit 2
done
shopt -u nullglob

LC_ALL=C sort -u -t $'\t' -k2,2 -k3,3 -k5,5 "${raw_records}" >"${expected_records}"

section 'PACKAGE AND DRIVER VERSIONS'
package_header
package_total=0
package_pass=0
package_fail=0
while IFS=$'\t' read -r manager package_class package_name expected_version interpreter; do
  [[ -n ${manager} ]] || continue
  ((package_total += 1))
  actual_version=-
  result=FAIL
  if [[ ${manager} == deb ]]; then
    record="$(dpkg-query -W -f='${Status}\t${Version}' "${package_name}" 2>/dev/null || true)"
    if [[ ${record%%$'\t'*} == 'install ok installed' ]]; then
      actual_version="${record#*$'\t'}"
      [[ ${actual_version} == "${expected_version}" ]] && result=PASS
    fi
  else
    if [[ -x ${interpreter} ]]; then
      actual_version="$(
        "${interpreter}" -c \
          'import importlib.metadata as m, sys; print(m.version(sys.argv[1]))' \
          "${package_name}" 2>/dev/null || true
      )"
      [[ -n ${actual_version} ]] || actual_version=-
      [[ ${actual_version} == "${expected_version}" ]] && result=PASS
    fi
  fi
  printf '%-16s %-40s %-38s %-38s %-6s\n' \
    "${package_class}" "${package_name}" "${expected_version}" "${actual_version}" "${result}"
  if [[ ${result} == PASS ]]; then
    ((package_pass += 1))
    ((passes += 1))
  else
    ((package_fail += 1))
    ((failures += 1))
  fi
done <"${expected_records}"
printf '%-16s %-40s %-38s %-38s %-6s\n' \
  SUMMARY "total=${package_total}" "passed=${package_pass}" "failed=${package_fail}" \
  "$([[ ${package_fail} -eq 0 ]] && printf PASS || printf FAIL)"

readarray -t hardware_metadata < <(
  python3 - "${BUNDLE}/hardware/driver-recipe.json" <<'PY'
import json
import pathlib
import sys

recipe = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
print(recipe["kernel_version"])
print(str(recipe.get("voyager", {}).get("enabled", False)).lower())
print(recipe.get("python", {}).get("openvino", "unknown"))
PY
)
expected_kernel="${hardware_metadata[0]:-unknown}"
voyager_enabled="${hardware_metadata[1]:-false}"
expected_openvino="${hardware_metadata[2]:-unknown}"

section 'HARDWARE AND ACCELERATOR CHECKS'
check_header
actual_os="$(. /etc/os-release; printf '%s %s' "${ID:-unknown}" "${VERSION_ID:-unknown}")"
[[ ${actual_os} == "${expected_os}" ]] && result=PASS || result=FAIL
check_row 'Operating system' "${expected_os}" "${actual_os}" "${result}"

actual_architecture="$(dpkg --print-architecture 2>/dev/null || true)"
[[ ${actual_architecture} == "${expected_architecture}" ]] && result=PASS || result=FAIL
check_row 'Architecture' "${expected_architecture}" "${actual_architecture}" "${result}"

cpu_model="$(awk -F: '/^model name/ {sub(/^[[:space:]]+/, "", $2); print $2; exit}' /proc/cpuinfo)"
[[ ${cpu_model} =~ Intel.*Core.*Ultra.*285H ]] && result=PASS || result=FAIL
check_row 'CPU profile' 'Intel Core Ultra 285H' "${cpu_model}" "${result}"

actual_kernel="$(uname -r)"
[[ ${actual_kernel} == "${expected_kernel}" ]] && result=PASS || result=FAIL
check_row 'Qualified kernel' "${expected_kernel}" "${actual_kernel}" "${result}"

gpu_module=-
grep -q '^xe ' /proc/modules && gpu_module=xe
[[ ${gpu_module} == - ]] && grep -q '^i915 ' /proc/modules && gpu_module=i915
[[ ${gpu_module} != - ]] && result=PASS || result=FAIL
check_row 'Intel GPU kernel module' 'xe or i915 loaded' "${gpu_module} (${actual_kernel})" "${result}"

grep -q '^intel_vpu ' /proc/modules && result=PASS || result=FAIL
check_row 'Intel NPU kernel module' 'intel_vpu loaded' \
  "$([[ ${result} == PASS ]] && printf 'intel_vpu (%s)' "${actual_kernel}" || printf 'not loaded')" "${result}"

[[ -e /dev/dri/renderD128 ]] && result=PASS || result=FAIL
check_row 'GPU render device' '/dev/dri/renderD128' \
  "$([[ -e /dev/dri/renderD128 ]] && stat -c '%n %U:%G %a' /dev/dri/renderD128 || printf missing)" "${result}"

[[ -e /dev/accel/accel0 ]] && result=PASS || result=FAIL
check_row 'NPU accelerator device' '/dev/accel/accel0' \
  "$([[ -e /dev/accel/accel0 ]] && stat -c '%n %U:%G %a' /dev/accel/accel0 || printf missing)" "${result}"

openvino_python=/opt/apexfabric/openvino-env/bin/python
actual_openvino=-
openvino_devices=-
if [[ -x ${openvino_python} ]]; then
  actual_openvino="$("${openvino_python}" -c 'import importlib.metadata as m; print(m.version("openvino"))' 2>/dev/null || true)"
  openvino_devices="$("${openvino_python}" - <<'PY' 2>/dev/null || true
import openvino
print(",".join(sorted({name.split(".", 1)[0] for name in openvino.Core().available_devices})))
PY
)"
fi
[[ ${actual_openvino} == "${expected_openvino}" ]] && result=PASS || result=FAIL
check_row 'OpenVINO runtime' "${expected_openvino}" "${actual_openvino}" "${result}"
[[ ,${openvino_devices}, == *,CPU,* && ,${openvino_devices}, == *,GPU,* && ,${openvino_devices}, == *,NPU,* ]] \
  && result=PASS || result=FAIL
check_row 'OpenVINO devices' 'CPU,GPU,NPU' "${openvino_devices}" "${result}"

if timeout 30s vainfo >/dev/null 2>&1; then result=PASS; else result=FAIL; fi
check_row 'VA-API acceleration' 'vainfo succeeds' "$([[ ${result} == PASS ]] && printf available || printf unavailable)" "${result}"
if timeout 30s clinfo -l >/dev/null 2>&1; then result=PASS; else result=FAIL; fi
check_row 'OpenCL acceleration' 'clinfo succeeds' "$([[ ${result} == PASS ]] && printf available || printf unavailable)" "${result}"

if [[ ${voyager_enabled} == true ]]; then
  if grep -q '^metis ' /proc/modules && [[ -d /sys/class/metis ]] \
    && compgen -G '/dev/metis-*' >/dev/null; then
    result=PASS
    actual='module and devices available'
  else
    result=FAIL
    actual='module or devices missing'
  fi
  check_row 'Axelera Metis/Voyager' 'enabled' "${actual}" "${result}"
else
  check_row 'Axelera Metis/Voyager' 'not included by recipe' 'not required' PASS
fi

section 'SERVICES AND PLATFORM HEALTH'
check_header
required_units=(
  containerd.service docker.service postgresql.service tvt-local-registry.service
  k3s.service tvt-edge.service tvt-camera-sync.service tvt-retention.timer
  tvt-k3s-watchdog.timer tvt-pipeline-image-sync.timer
)
for unit in "${required_units[@]}"; do
  active="$(systemctl is-active "${unit}" 2>/dev/null || true)"
  enabled="$(systemctl is-enabled "${unit}" 2>/dev/null || true)"
  [[ ${active} == active && ${enabled} == enabled ]] && result=PASS || result=FAIL
  check_row "${unit}" 'active and enabled' "${active}/${enabled}" "${result}"
done

alert_active="$(systemctl is-active tvt-alert-dispatcher.service 2>/dev/null || true)"
if [[ ${alert_active} == active ]]; then
  check_row 'tvt-alert-dispatcher.service' 'optional' active PASS
else
  check_row 'tvt-alert-dispatcher.service' 'optional' "${alert_active:-disabled}" WARN
fi

if docker info >/dev/null 2>&1; then result=PASS; else result=FAIL; fi
check_row 'Docker daemon API' 'reachable' \
  "$(docker --version 2>/dev/null || printf unavailable)" "${result}"

if pg_isready --quiet >/dev/null 2>&1; then result=PASS; else result=FAIL; fi
check_row 'PostgreSQL readiness' 'accepting connections' \
  "$(pg_isready 2>&1 || true)" "${result}"

platform_value() {
  local key="$1"
  sed -n "s/^${key}=//p" "${BUNDLE}/config/platform.env" | tail -n 1
}
registry_address="$(platform_value LOCAL_REGISTRY_ADDRESS)"
registry_container="$(platform_value LOCAL_REGISTRY_CONTAINER_NAME)"
registry_image="$(platform_value LOCAL_REGISTRY_IMAGE)"

if curl --fail --silent --show-error --max-time 5 \
  "http://${registry_address}/v2/" >/dev/null 2>&1; then result=PASS; else result=FAIL; fi
check_row 'Local registry API' "http://${registry_address}/v2/" \
  "$([[ ${result} == PASS ]] && printf reachable || printf unreachable)" "${result}"

container_state="$(docker inspect --format '{{.State.Status}}' "${registry_container}" 2>/dev/null || true)"
container_image="$(docker inspect --format '{{.Config.Image}}' "${registry_container}" 2>/dev/null || true)"
registry_digest="${registry_image##*@}"
# The offline installer starts the registry from its immutable image ID, so
# Docker may retain either the complete repository@digest reference or only
# the equivalent sha256 digest in Config.Image.
[[ ${container_state} == running \
  && ( ${container_image} == "${registry_image}" || ${container_image} == "${registry_digest}" ) ]] \
  && result=PASS || result=FAIL
check_row 'Registry container' "running ${registry_image}" \
  "${container_state:-missing} ${container_image:-missing}" "${result}"

registry_port="$(docker port "${registry_container}" 5000/tcp 2>/dev/null || true)"
[[ ${registry_port} == "${registry_address}" ]] && result=PASS || result=FAIL
check_row 'Registry network binding' "${registry_address}" "${registry_port}" "${result}"

actual_k3s="$(k3s --version 2>/dev/null | awk 'NR == 1 {print $3}')"
[[ ${actual_k3s} == "${expected_k3s}" ]] && result=PASS || result=FAIL
check_row 'K3s version' "${expected_k3s}" "${actual_k3s}" "${result}"

mapfile -t nodes < <(k3s kubectl get nodes -o name 2>/dev/null || true)
[[ ${#nodes[@]} -eq 1 && -n ${nodes[0]:-} ]] && result=PASS || result=FAIL
check_row 'K3s registered nodes' 'exactly one' \
  "count=${#nodes[@]} ${nodes[*]:-}" "${result}"

node_ready="$(k3s kubectl get nodes --no-headers 2>/dev/null | awk '{print $2}' | paste -sd, -)"
[[ -n ${node_ready} && ${node_ready} != *NotReady* ]] && result=PASS || result=FAIL
check_row 'K3s node readiness' 'Ready' "${node_ready}" "${result}"

if "${BUNDLE}/scripts/tvt-edge-operations.sh" verify-k3s-plane >/dev/null 2>&1; then
  result=PASS
else
  result=FAIL
fi
check_row 'TVT K3s management plane' 'verified' \
  "$([[ ${result} == PASS ]] && printf verified || printf 'verification failed')" "${result}"

api_health="$(curl --fail --silent --show-error --max-time 10 \
  http://127.0.0.1:8088/api/v1/health 2>/dev/null || true)"
if python3 - "${api_health}" <<'PY' >/dev/null 2>&1
import json
import sys
document = json.loads(sys.argv[1])
raise SystemExit(0 if document.get("status") == "healthy" else 1)
PY
then result=PASS; else result=FAIL; fi
check_row 'TVT API health' 'healthy' "${api_health}" "${result}"

if runuser -u tvt-edge -- env \
  TVT_RESOURCE_ROOT="/opt/tvt/releases/${release_version}/resources" \
  TVT_DATABASE_URL=postgresql+psycopg:///tvt \
  "/opt/tvt/releases/${release_version}/venv/bin/tvt-edge" check >/dev/null 2>&1; then
  result=PASS
else
  result=FAIL
fi
check_row 'TVT application self-check' 'passes' \
  "$([[ ${result} == PASS ]] && printf passed || printf failed)" "${result}"

if "${BUNDLE}/scripts/tvt-edge-operations.sh" verify-pipeline-image-sync >/dev/null 2>&1; then
  result=PASS
else
  result=FAIL
fi
check_row 'Traffic image synchronization' 'verified' \
  "$([[ ${result} == PASS ]] && printf verified || printf 'verification failed')" "${result}"

install_state="$(python3 - <<'PY' 2>/dev/null || true
import json
from pathlib import Path
path = Path("/var/lib/tvt/install/install-state.json")
print(json.loads(path.read_text(encoding="utf-8")).get("status", "missing"))
PY
)"
[[ ${install_state} == installed ]] && result=PASS || result=FAIL
check_row 'TVT installation state' 'installed' "${install_state}" "${result}"

swap_state="$(swapon --noheadings --show 2>/dev/null || true)"
[[ -z ${swap_state} ]] && result=PASS || result=FAIL
check_row 'Swap' 'disabled' "$([[ -z ${swap_state} ]] && printf disabled || printf '%s' "${swap_state}")" "${result}"

failed_units="$(systemctl --failed --no-legend --plain 2>/dev/null | awk 'NF {print $1}' | paste -sd, -)"
[[ -z ${failed_units} ]] && result=PASS || result=FAIL
check_row 'Failed systemd units' 'none' "${failed_units:-none}" "${result}"

section 'KUBERNETES PODS'
pods_file="${work_directory}/pods.json"
if k3s kubectl get pods -A -o json >"${pods_file}" 2>/dev/null; then
  if ! python3 - "${pods_file}" <<'PY'
import json
import pathlib
import sys

document = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
print(f"{'NAMESPACE':<22} {'POD':<52} {'READY':<11} {'PHASE':<14} {'RESTARTS':<10} RESULT")
print(f"{'-' * 22} {'-' * 52} {'-' * 11} {'-' * 14} {'-' * 10} {'-' * 6}")
failed = False
items = document.get("items", [])
for pod in sorted(items, key=lambda item: (item["metadata"].get("namespace", ""), item["metadata"].get("name", ""))):
    metadata = pod.get("metadata", {})
    status = pod.get("status", {})
    containers = status.get("containerStatuses") or []
    init_containers = status.get("initContainerStatuses") or []
    phase = status.get("phase", "Unknown")
    ready_count = sum(bool(item.get("ready")) for item in containers)
    ready = f"{ready_count}/{len(containers)}"
    restarts = sum(int(item.get("restartCount", 0)) for item in containers + init_containers)
    healthy = phase == "Succeeded" or (
        phase == "Running"
        and bool(containers)
        and ready_count == len(containers)
        and all(item.get("state", {}).get("terminated", {}).get("exitCode") == 0 for item in init_containers)
    )
    result = "PASS" if healthy else "FAIL"
    failed = failed or not healthy
    print(
        f"{metadata.get('namespace', '-'):<22} {metadata.get('name', '-'):<52} "
        f"{ready:<11} {phase:<14} {restarts:<10} {result}"
    )
if not items:
    print(f"{'-':<22} {'no pods found':<52} {'-':<11} {'-':<14} {'-':<10} FAIL")
    failed = True
raise SystemExit(1 if failed else 0)
PY
  then
    ((failures += 1))
  else
    ((passes += 1))
  fi
else
  printf '%s\n' 'FAIL: could not query Kubernetes pods'
  ((failures += 1))
fi

section 'SUMMARY'
printf '%-12s %d\n' PASS "${passes}"
printf '%-12s %d\n' WARN "${warnings}"
printf '%-12s %d\n' FAIL "${failures}"
if ((failures == 0)); then
  printf '%s\n' 'OVERALL: PASS - all required deployment checks succeeded.'
  exit 0
fi
printf '%s\n' 'OVERALL: FAIL - review every row marked FAIL.'
exit 1
