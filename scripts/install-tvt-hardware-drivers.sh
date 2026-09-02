#!/usr/bin/env bash
set -Eeuo pipefail
umask 022

readonly EXPECTED_OS_ID="ubuntu"
readonly EXPECTED_OS_VERSION="24.04"
readonly EXPECTED_ARCH="amd64"
readonly DRIVER_PPA="ppa:kobuk-team/intel-graphics"
readonly STATE_DIRECTORY="/var/lib/tvt"
readonly CACHE_DIRECTORY="/var/cache/tvt/hardware-drivers"
readonly LOCK_FILE="${STATE_DIRECTORY}/hardware-driver-recipe.json"
readonly VENV_DIRECTORY="/opt/apexfabric/openvino-env"
readonly REBOOT_MARKER="${STATE_DIRECTORY}/hardware-driver-reboot-required"

readonly -a APT_PACKAGES=(
  libze-intel-gpu1
  libze1
  intel-opencl-icd
  clinfo
  intel-gsc
  intel-media-va-driver-non-free
  libmfx-gen1.2
  libvpl2
  libvpl-tools
  va-driver-all
  vainfo
  libtbb12
  python3-venv
)
readonly -a PYTHON_PACKAGES=(openvino openvino-genai)

log() { printf 'tvt-driver-install: %s\n' "$*"; }
fail() { printf 'tvt-driver-install: ERROR: %s\n' "$*" >&2; exit 1; }

[[ ${EUID} -eq 0 ]] || fail "run this script as root (for example, with sudo)"
[[ ! -L ${STATE_DIRECTORY} ]] || fail "refusing symlinked state directory: ${STATE_DIRECTORY}"
[[ ! -L ${CACHE_DIRECTORY} ]] || fail "refusing symlinked cache directory: ${CACHE_DIRECTORY}"
[[ ! -L ${VENV_DIRECTORY} ]] || fail "refusing symlinked OpenVINO environment: ${VENV_DIRECTORY}"
[[ -r /etc/os-release ]] || fail "/etc/os-release is missing"

# shellcheck disable=SC1091
source /etc/os-release
[[ ${ID:-} == "${EXPECTED_OS_ID}" && ${VERSION_ID:-} == "${EXPECTED_OS_VERSION}" ]] || \
  fail "supported OS is Ubuntu 24.04; found ${ID:-unknown} ${VERSION_ID:-unknown}"
[[ $(dpkg --print-architecture) == "${EXPECTED_ARCH}" ]] || \
  fail "supported architecture is amd64; found $(dpkg --print-architecture)"

if ! grep -Eiq '^model name[[:space:]]*:.*Intel.*Core.*Ultra.*285H' /proc/cpuinfo; then
  [[ ${TVT_ALLOW_UNVERIFIED_HARDWARE:-false} == true ]] || \
    fail "Intel 285H hardware was not detected (set TVT_ALLOW_UNVERIFIED_HARDWARE=true only for an audited equivalent host)"
fi

for module in intel_vpu; do
  modinfo "${module}" >/dev/null 2>&1 || \
    fail "kernel $(uname -r) does not provide ${module}; install the qualified Ubuntu kernel first"
done
if ! modinfo i915 >/dev/null 2>&1 && ! modinfo xe >/dev/null 2>&1; then
  fail "kernel $(uname -r) provides neither the i915 nor xe Intel graphics module"
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  ca-certificates curl gnupg python3 python3-venv software-properties-common

if ! grep -RqsE '(^|/)kobuk-team/ubuntu.*intel-graphics|ppa\.launchpadcontent\.net/kobuk-team/intel-graphics' \
  /etc/apt/sources.list /etc/apt/sources.list.d 2>/dev/null; then
  log "adding the Intel graphics package source used by k3s-prototype"
  add-apt-repository -y "${DRIVER_PPA}"
fi
apt-get update

install -d -o root -g root -m 0755 "${STATE_DIRECTORY}" "${CACHE_DIRECTORY}"
work_directory="$(mktemp -d "${CACHE_DIRECTORY}/resolve.XXXXXX")"
cleanup() { rm -rf -- "${work_directory}"; }
trap cleanup EXIT

resolve_recipe() {
  local package candidate npu_url npu_sha256
  local -a npu_fields=()
  local apt_pins="${work_directory}/apt-pins.txt"
  local requirements="${work_directory}/requirements.txt"
  local wheel_sums="${work_directory}/wheel-sha256.txt"
  local github_json="${work_directory}/github-release.json"
  local npu_metadata="${work_directory}/npu-metadata.txt"
  local npu_archive="${work_directory}/linux-npu-driver.tar.gz"
  local wheels="${work_directory}/wheels"
  : >"${apt_pins}"

  log "resolving exact APT candidates"
  for package in "${APT_PACKAGES[@]}"; do
    candidate="$(apt-cache policy "${package}" | awk '$1 == "Candidate:" {print $2; exit}')"
    [[ -n ${candidate} && ${candidate} != '(none)' ]] || \
      fail "configured repositories have no candidate for ${package}"
    printf '%s=%s\n' "${package}" "${candidate}" >>"${apt_pins}"
  done

  log "resolving and caching the OpenVINO wheel closure"
  install -d -m 0755 "${wheels}"
  python3 -m venv "${work_directory}/resolver-venv"
  "${work_directory}/resolver-venv/bin/python" -m pip download \
    --disable-pip-version-check --no-cache-dir \
    --only-binary=:all: --dest "${wheels}" "${PYTHON_PACKAGES[@]}"
  python3 - "${wheels}" "${requirements}" <<'PY'
import email
import pathlib
import re
import sys
import zipfile

wheels = pathlib.Path(sys.argv[1])
required = {"openvino", "openvino-genai"}
resolved = {}
for wheel in wheels.glob("*.whl"):
    with zipfile.ZipFile(wheel) as archive:
        metadata_names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(metadata_names) != 1:
            raise SystemExit(f"cannot identify metadata in {wheel.name}")
        metadata = email.message_from_bytes(archive.read(metadata_names[0]))
    name = re.sub(r"[-_.]+", "-", metadata.get("Name", "")).lower()
    if name in required:
        resolved[name] = metadata.get("Version", "")
missing = sorted(required - resolved.keys())
if missing:
    raise SystemExit("pip did not resolve required wheels: " + ", ".join(missing))
pathlib.Path(sys.argv[2]).write_text(
    "".join(f"{name}=={resolved[name]}\n" for name in sorted(required)),
    encoding="utf-8",
)
PY
  (cd "${wheels}" && sha256sum -- *.whl | sort -k2) >"${wheel_sums}"

  log "resolving and caching the latest Intel NPU Ubuntu 24.04 release"
  curl --fail --location --silent --show-error \
    --proto '=https' --tlsv1.2 --connect-timeout 15 --retry 3 --retry-all-errors \
    -H 'Accept: application/vnd.github+json' \
    -H 'X-GitHub-Api-Version: 2022-11-28' \
    -H 'User-Agent: tvt-prototype-driver-installer/1' \
    -o "${github_json}" \
    https://api.github.com/repos/intel/linux-npu-driver/releases/latest
  python3 - "${github_json}" "${npu_metadata}" <<'PY'
import json
import pathlib
import sys

release = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
assets = [
    asset for asset in release.get("assets", [])
    if str(asset.get("name", "")).endswith("-ubuntu2404.tar.gz")
]
if len(assets) != 1:
    raise SystemExit("latest Intel NPU release does not contain exactly one Ubuntu 24.04 archive")
url = str(assets[0].get("browser_download_url", ""))
if not url.startswith("https://github.com/intel/linux-npu-driver/releases/download/"):
    raise SystemExit("GitHub returned an untrusted Intel NPU asset URL")
pathlib.Path(sys.argv[2]).write_text(
    f"{release.get('tag_name', '')}\n{assets[0]['name']}\n{url}\n",
    encoding="utf-8",
)
PY
  mapfile -t npu_fields <"${npu_metadata}"
  npu_url="${npu_fields[2]:-}"
  [[ -n ${npu_url} ]] || fail "could not resolve the Intel NPU archive URL"
  curl --fail --location --silent --show-error \
    --proto '=https' --tlsv1.2 --connect-timeout 15 --retry 3 --retry-all-errors \
    --max-time 1800 --output "${npu_archive}" "${npu_url}"
  npu_sha256="$(sha256sum "${npu_archive}" | awk '{print $1}')"

  # Publish artifacts first and the lock last. Once the lock exists, later runs
  # never resolve a different recipe implicitly.
  rm -rf -- "${CACHE_DIRECTORY}/wheels"
  install -d -m 0755 "${CACHE_DIRECTORY}/wheels"
  cp -a "${wheels}/." "${CACHE_DIRECTORY}/wheels/"
  install -m 0644 "${npu_archive}" "${CACHE_DIRECTORY}/linux-npu-driver.tar.gz"

  python3 - "${LOCK_FILE}.new" "${apt_pins}" "${requirements}" "${wheel_sums}" \
    "${npu_fields[0]:-}" "${npu_fields[1]:-}" "${npu_url}" "${npu_sha256}" \
    "$(uname -r)" <<'PY'
import json
import pathlib
import sys

output, apt_file, requirements_file, sums_file, tag, asset, url, digest, kernel = sys.argv[1:]
apt = dict(line.split("=", 1) for line in pathlib.Path(apt_file).read_text().splitlines())
python = dict(line.split("==", 1) for line in pathlib.Path(requirements_file).read_text().splitlines())
wheels = {}
for line in pathlib.Path(sums_file).read_text().splitlines():
    sha256, filename = line.split(maxsplit=1)
    wheels[filename.lstrip("*")] = sha256
recipe = {
    "schema_version": 1,
    "policy": "k3s-prototype-latest-resolved-once",
    "hardware_profile": "intel-285h",
    "os_id": "ubuntu",
    "os_version_id": "24.04",
    "architecture": "amd64",
    "kernel_version": kernel,
    "apt": apt,
    "python": python,
    "wheels": wheels,
    "npu": {"release": tag, "asset": asset, "url": url, "sha256": digest},
}
pathlib.Path(output).write_text(json.dumps(recipe, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
  chmod 0644 "${LOCK_FILE}.new"
  mv -f -- "${LOCK_FILE}.new" "${LOCK_FILE}"
}

validate_lock_and_cache() {
  python3 - "${LOCK_FILE}" "${CACHE_DIRECTORY}" "$(uname -r)" <<'PY'
import hashlib
import json
import pathlib
import sys

lock_path, cache_path, kernel = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3]
recipe = json.loads(lock_path.read_text(encoding="utf-8"))
expected = {
    "schema_version": 1,
    "hardware_profile": "intel-285h",
    "os_id": "ubuntu",
    "os_version_id": "24.04",
    "architecture": "amd64",
    "kernel_version": kernel,
}
for name, value in expected.items():
    if recipe.get(name) != value:
        raise SystemExit(f"locked {name} is {recipe.get(name)!r}; expected {value!r}")

def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()

npu = cache_path / "linux-npu-driver.tar.gz"
if not npu.is_file() or sha256(npu) != recipe["npu"]["sha256"]:
    raise SystemExit("cached Intel NPU archive is missing or does not match the lock")
for filename, expected_digest in recipe["wheels"].items():
    wheel = cache_path / "wheels" / filename
    if not wheel.is_file() or sha256(wheel) != expected_digest:
        raise SystemExit(f"cached wheel is missing or does not match the lock: {filename}")
PY
}

if [[ ! -f ${LOCK_FILE} ]]; then
  resolve_recipe
else
  log "reusing locked recipe ${LOCK_FILE}"
fi
validate_lock_and_cache

mapfile -t apt_pins < <(python3 - "${LOCK_FILE}" <<'PY'
import json, pathlib, sys
recipe = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
for name, version in sorted(recipe["apt"].items()):
    print(f"{name}={version}")
PY
)
mapfile -t python_pins < <(python3 - "${LOCK_FILE}" <<'PY'
import json, pathlib, sys
recipe = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
for name, version in sorted(recipe["python"].items()):
    print(f"{name}=={version}")
PY
)

log "installing the locked Intel GPU/media package set"
apt-get install -y --no-install-recommends --allow-downgrades "${apt_pins[@]}"

log "installing the locked Intel NPU release"
npu_extract="${work_directory}/npu"
install -d -m 0755 "${npu_extract}"
python3 - "${CACHE_DIRECTORY}/linux-npu-driver.tar.gz" "${npu_extract}" <<'PY'
import pathlib
import tarfile
import sys

archive, destination = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
with tarfile.open(archive) as source:
    source.extractall(destination, filter="data")
PY
mapfile -d '' -t npu_packages < <(find "${npu_extract}" -type f -name '*.deb' -print0 | sort -z)
(( ${#npu_packages[@]} > 0 )) || fail "Intel NPU release contains no Debian packages"
apt-get install -y --no-install-recommends --allow-downgrades "${npu_packages[@]}"

log "installing the locked OpenVINO runtime"
install -d -m 0755 "$(dirname "${VENV_DIRECTORY}")"
python3 -m venv --clear "${VENV_DIRECTORY}"
"${VENV_DIRECTORY}/bin/python" -m pip install --disable-pip-version-check \
  --no-index --find-links="${CACHE_DIRECTORY}/wheels" "${python_pins[@]}"
chmod -R go+rX "${VENV_DIRECTORY}"

modprobe i915 2>/dev/null || modprobe xe 2>/dev/null || true
modprobe intel_vpu 2>/dev/null || true
install -m 0644 /dev/null "${REBOOT_MARKER}"
printf 'installed_at=%s\nkernel=%s\nrecipe=%s\n' \
  "$(date --utc --iso-8601=seconds)" "$(uname -r)" "${LOCK_FILE}" >"${REBOOT_MARKER}"

log "installation complete; exact versions are recorded in ${LOCK_FILE}"
log "reboot this device before performing the verification commands below"
