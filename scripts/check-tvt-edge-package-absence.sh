#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

readonly SCRIPT_DIRECTORY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly REPOSITORY_ROOT="$(cd "${SCRIPT_DIRECTORY}/.." && pwd)"

BUNDLE=""
EDGE_HOST="admin1@192.168.1.64"
SSH_PORT=22

usage() {
  cat <<'EOF'
usage: scripts/check-tvt-edge-package-absence.sh --bundle DIR [options]

Read-only audit of the release packages against an Intel edge device.

options:
  --bundle DIR       extracted TVT release bundle to audit (required)
  --host USER@HOST   SSH destination (default: admin1@192.168.1.64)
  --port PORT        SSH port (default: 22)
  -h, --help         show this help

Exit status:
  0  every package in the release is absent
  1  at least one package is present or has residual dpkg state
  2  invalid input or an unreadable/invalid release bundle
  255 SSH connection or authentication failure
EOF
}

require_value() {
  [[ -n ${2:-} ]] || {
    printf 'missing value for %s\n' "$1" >&2
    usage >&2
    exit 2
  }
}

while (($#)); do
  case "$1" in
    --bundle)
      require_value "$1" "${2:-}"
      BUNDLE="$2"
      shift 2
      ;;
    --host)
      require_value "$1" "${2:-}"
      EDGE_HOST="$2"
      shift 2
      ;;
    --port)
      require_value "$1" "${2:-}"
      SSH_PORT="$2"
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

[[ -n ${BUNDLE} ]] || {
  printf '%s\n' '--bundle is required' >&2
  usage >&2
  exit 2
}
[[ ${SSH_PORT} =~ ^[1-9][0-9]{0,4}$ ]] && ((SSH_PORT <= 65535)) || {
  printf 'invalid SSH port: %s\n' "${SSH_PORT}" >&2
  exit 2
}
[[ ${EDGE_HOST} != -* && ${EDGE_HOST} != *[[:space:]]* ]] || {
  printf 'invalid SSH destination: %s\n' "${EDGE_HOST}" >&2
  exit 2
}

for command_name in dpkg-deb find python3 sort ssh; do
  command -v "${command_name}" >/dev/null 2>&1 || {
    printf 'required workstation command is unavailable: %s\n' "${command_name}" >&2
    exit 2
  }
done

[[ -d ${BUNDLE} && ! -L ${BUNDLE} ]] || {
  printf 'release bundle is missing, not a directory, or symlinked: %s\n' "${BUNDLE}" >&2
  exit 2
}
BUNDLE="$(cd "${BUNDLE}" && pwd -P)"

for required_path in \
  manifest.json \
  checksums.sha256 \
  hardware/driver-recipe.json \
  hardware/linux-npu-driver.tar.gz \
  packages/apt; do
  [[ -e ${BUNDLE}/${required_path} ]] || {
    printf 'release bundle is missing %s\n' "${required_path}" >&2
    exit 2
  }
done

verification_output=""
if ! verification_output="$(
  "${REPOSITORY_ROOT}/scripts/tvt-edge-operations.sh" verify-release \
    --bundle "${BUNDLE}" 2>&1
)"; then
  printf 'release verification failed:\n%s\n' "${verification_output}" >&2
  exit 2
fi

audit_directory="$(mktemp -d)"
trap 'rm -rf -- "${audit_directory}"' EXIT
raw_records="${audit_directory}/raw-records.tsv"
expected_records="${audit_directory}/expected-records.tsv"
: >"${raw_records}"

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
for package in sorted(recipe.get("apt", {})):
    print(package)
PY
)

add_debian_package() {
  local package_class="$1"
  local package_file="$2"
  local package_name package_version

  package_name="$(dpkg-deb -f "${package_file}" Package 2>/dev/null)" || {
    printf 'cannot read Debian package metadata: %s\n' "${package_file}" >&2
    exit 2
  }
  package_version="$(dpkg-deb -f "${package_file}" Version 2>/dev/null)" || {
    printf 'cannot read Debian package version: %s\n' "${package_file}" >&2
    exit 2
  }
  [[ ${package_name} != *$'\t'* && ${package_name} != *$'\n'* \
    && ${package_version} != *$'\t'* && ${package_version} != *$'\n'* ]] || {
    printf 'unsafe package metadata in %s\n' "${package_file}" >&2
    exit 2
  }
  printf 'deb\t%s\t%s\t%s\t-\n' \
    "${package_class}" "${package_name}" "${package_version}" >>"${raw_records}"
}

shopt -s nullglob
apt_packages=("${BUNDLE}"/packages/apt/*.deb)
shopt -u nullglob
(( ${#apt_packages[@]} > 0 )) || {
  printf '%s\n' 'release bundle contains no offline APT packages' >&2
  exit 2
}
for package_file in "${apt_packages[@]}"; do
  package_name="$(dpkg-deb -f "${package_file}" Package 2>/dev/null)"
  package_class=APT-DEPENDENCY
  if [[ -n ${driver_packages[${package_name}]:-} ]]; then
    package_class=INTEL-DRIVER
  elif [[ -n ${host_packages[${package_name}]:-} ]]; then
    package_class=HOST
  fi
  add_debian_package "${package_class}" "${package_file}"
done

npu_directory="${audit_directory}/npu"
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
  add_debian_package INTEL-NPU "${package_file}"
done < <(find "${npu_directory}" -type f -name '*.deb' -print0)

add_python_wheel() {
  local package_class="$1"
  local interpreter="$2"
  local wheel="$3"
  python3 - "${package_class}" "${interpreter}" "${wheel}" >>"${raw_records}" <<'PY'
import email.parser
import pathlib
import sys
import zipfile

package_class, interpreter, wheel_name = sys.argv[1:]
wheel = pathlib.Path(wheel_name)
with zipfile.ZipFile(wheel) as archive:
    metadata_names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
    if len(metadata_names) != 1:
        raise SystemExit(f"wheel has {len(metadata_names)} METADATA records: {wheel}")
    metadata = email.parser.Parser().parsestr(
        archive.read(metadata_names[0]).decode("utf-8")
    )
name = metadata.get("Name")
version = metadata.get("Version")
if not name or not version or any(character in name + version for character in "\t\n"):
    raise SystemExit(f"wheel has invalid package metadata: {wheel}")
print("\t".join(("python", package_class, name, version, interpreter)))
PY
}

shopt -s nullglob
for wheel in "${BUNDLE}"/hardware/wheels/*.whl; do
  add_python_wheel OPENVINO /opt/apexfabric/openvino-env/bin/python "${wheel}"
done
for wheel in "${BUNDLE}"/hardware/voyager-wheels/*.whl; do
  add_python_wheel VOYAGER /opt/apexfabric/voyager-1.6.1/bin/python "${wheel}"
done
shopt -u nullglob

release_version="$(python3 - "${BUNDLE}/manifest.json" <<'PY'
import json
import pathlib
import sys

manifest = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
print(manifest["release_version"])
PY
)"
application_wheel="$(python3 - "${BUNDLE}/manifest.json" <<'PY'
import json
import pathlib
import sys

manifest = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
print(manifest["artifacts"]["application_wheel"])
PY
)"
[[ -f ${BUNDLE}/${application_wheel} ]] || {
  printf 'application wheel is missing: %s\n' "${application_wheel}" >&2
  exit 2
}
shopt -s nullglob
application_wheels=("${BUNDLE}"/wheels/*.whl)
shopt -u nullglob
(( ${#application_wheels[@]} > 0 )) || {
  printf '%s\n' 'release bundle contains no application wheels' >&2
  exit 2
}
for wheel in "${application_wheels[@]}"; do
  add_python_wheel \
    TVT-RUNTIME "/opt/tvt/releases/${release_version}/venv/bin/python" "${wheel}"
done

LC_ALL=C sort -u -t $'\t' -k2,2 -k3,3 -k5,5 "${raw_records}" >"${expected_records}"

{
  cat <<'REMOTE_SCRIPT'
#!/usr/bin/env bash
set -uo pipefail

readonly HEADER_FORMAT='%-16s %-42s %-46s %-20s %s\n'
readonly ROW_FORMAT='%-16s %-42s %-46s %-20s %s\n'

printf "${HEADER_FORMAT}" CLASS PACKAGE_OR_DRIVER EXPECTED_VERSION EDGE_STATE INSTALLED_VERSION
printf "${HEADER_FORMAT}" '----------------' '------------------------------------------' \
  '----------------------------------------------' '--------------------' '-----------------'

total=0
absent=0
not_absent=0

while IFS=$'\t' read -r package_manager package_class package_name expected_version interpreter; do
  [[ -n ${package_manager} ]] || continue
  ((total += 1))
  actual_version=-
  edge_state=ABSENT

  case "${package_manager}" in
    deb)
      record="$(dpkg-query -W -f='${db:Status-Abbrev}\t${Version}' "${package_name}" 2>/dev/null || true)"
      if [[ -n ${record} ]]; then
        dpkg_state="${record%%$'\t'*}"
        actual_version="${record#*$'\t'}"
        case "${dpkg_state}" in
          ii*)
            if [[ ${actual_version} == "${expected_version}" ]]; then
              edge_state=PRESENT-EXACT
            else
              edge_state=PRESENT-OTHER
            fi
            ;;
          un*)
            edge_state=ABSENT
            actual_version=-
            ;;
          *)
            edge_state="RESIDUAL-${dpkg_state// /}"
            ;;
        esac
      fi
      ;;
    python)
      if [[ -x ${interpreter} ]]; then
        actual_version="$(
          "${interpreter}" -c \
            'import importlib.metadata as metadata, sys; print(metadata.version(sys.argv[1]))' \
            "${package_name}" 2>/dev/null || true
        )"
        if [[ -n ${actual_version} ]]; then
          if [[ ${actual_version} == "${expected_version}" ]]; then
            edge_state=PRESENT-EXACT
          else
            edge_state=PRESENT-OTHER
          fi
        else
          actual_version=-
        fi
      fi
      ;;
    *)
      printf 'unsupported package manager in audit data: %s\n' "${package_manager}" >&2
      exit 2
      ;;
  esac

  if [[ ${edge_state} == ABSENT ]]; then
    ((absent += 1))
  else
    ((not_absent += 1))
  fi
  printf "${ROW_FORMAT}" "${package_class}" "${package_name}" \
    "${expected_version}" "${edge_state}" "${actual_version}"
done <<'TVT_EXPECTED_PACKAGE_ROWS'
REMOTE_SCRIPT
  cat "${expected_records}"
  cat <<'REMOTE_SCRIPT'
TVT_EXPECTED_PACKAGE_ROWS

printf "${HEADER_FORMAT}" '----------------' '------------------------------------------' \
  '----------------------------------------------' '--------------------' '-----------------'
printf "${ROW_FORMAT}" SUMMARY "total=${total}" - "absent=${absent}" "not_absent=${not_absent}"

((not_absent == 0))
REMOTE_SCRIPT
} | ssh \
  -o StrictHostKeyChecking=yes \
  -o ConnectTimeout=10 \
  -p "${SSH_PORT}" \
  "${EDGE_HOST}" bash -s
