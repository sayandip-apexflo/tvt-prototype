#!/usr/bin/env bash
set -Eeuo pipefail

# Consolidated TVT build, installation, and verification operations. Each
# operation runs in a subshell so its legacy globals, traps, and shell options
# cannot leak into another operation.

tvt_op_bootstrap_postgresql() (
# Source: scripts/bootstrap-postgresql.sh
set -Eeuo pipefail

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly PIPELINE_VERSION="$(sed -n 's/^PIPELINE_TRAFFIC_VERSION=//p' "${REPO_ROOT}/config/pipeline.env" | head -1)"
readonly SOURCE_CATALOG="${REPO_ROOT}/solution-packs/catalog/tvt-mills-pilot-${PIPELINE_VERSION}"
readonly TARGET_CATALOG="/opt/tvt/solution-packs/catalog/tvt-mills-pilot-${PIPELINE_VERSION}"
VENV=/opt/tvt/venv

usage() {
  echo "usage: sudo scripts/tvt-edge-operations.sh bootstrap-postgresql [--venv PATH]" >&2
}

while (($#)); do
  case "$1" in
    --venv) VENV="${2:-}"; shift 2 ;;
    *) usage; exit 2 ;;
  esac
done

[[ ${EUID} -eq 0 ]] || { echo "run as root" >&2; exit 1; }
command -v psql >/dev/null 2>&1 || { echo "PostgreSQL 16 client is required" >&2; exit 1; }
command -v openssl >/dev/null 2>&1 || { echo "openssl is required" >&2; exit 1; }
[[ -x "${VENV}/bin/tvt-edge" ]] || { echo "missing installed TVT environment: ${VENV}" >&2; exit 1; }
[[ -x "${VENV}/bin/tvt-alert-dispatcher" ]] || {
  echo "missing installed TVT alert dispatcher: ${VENV}" >&2
  exit 1
}
[[ -x "${VENV}/bin/tvt-anpr-report" ]] || {
  echo "missing installed TVT ANPR report service: ${VENV}" >&2
  exit 1
}
[[ -x "${VENV}/bin/tvt-k3s-watchdog" ]] || {
  echo "missing installed TVT K3s watchdog: ${VENV}" >&2
  exit 1
}

if ! getent group tvt-edge >/dev/null; then
  groupadd --system tvt-edge
fi
if ! id tvt-edge >/dev/null 2>&1; then
  useradd --system --gid tvt-edge --home-dir /var/lib/tvt --shell /usr/sbin/nologin tvt-edge
fi
if ! getent group tvt-alert >/dev/null; then
  groupadd --system tvt-alert
fi
if ! id tvt-alert >/dev/null 2>&1; then
  useradd --system --gid tvt-alert --home-dir /var/lib/tvt-alert \
    --shell /usr/sbin/nologin tvt-alert
fi
if ! getent group tvt-report >/dev/null; then
  groupadd --system tvt-report
fi
if ! id tvt-report >/dev/null 2>&1; then
  useradd --system --gid tvt-report --home-dir /var/lib/tvt-reporting \
    --shell /usr/sbin/nologin tvt-report
fi
# Both service accounts need search permission here; sensitive descendants stay restricted.
install -d -o root -g root -m 0755 /etc/tvt
install -d -o tvt-edge -g tvt-edge -m 0750 /var/lib/tvt
install -d -o tvt-alert -g tvt-alert -m 0750 /var/lib/tvt-alert
install -d -o tvt-report -g tvt-report -m 0750 /var/lib/tvt-reporting
install -d -o root -g tvt-edge -m 0750 /etc/tvt/credential-keys
install -d -o root -g root -m 0755 "${TARGET_CATALOG}"
for filename in \
  image-contract.yaml \
  desired-state.schema.json \
  desired-state.example.json \
  metrics.schema.json \
  analytics-event.schema.json \
  analytics-event.example.json \
  provenance.json; do
  install -o root -g root -m 0644 \
    "${SOURCE_CATALOG}/${filename}" "${TARGET_CATALOG}/${filename}"
done
if [[ ! -f /etc/tvt/credential-keys/v1.key ]]; then
  temporary_key="$(mktemp /etc/tvt/credential-keys/.v1.key.XXXXXX)"
  trap 'rm -f "${temporary_key:-}"' EXIT
  openssl rand -out "${temporary_key}" 32
  chown root:tvt-edge "${temporary_key}"
  chmod 0640 "${temporary_key}"
  mv "${temporary_key}" /etc/tvt/credential-keys/v1.key
  trap - EXIT
fi
if [[ ! -f /etc/tvt/edge.env ]]; then
  install -o root -g tvt-edge -m 0640 \
    "${REPO_ROOT}/deploy/host/tvt-edge.env.example" /etc/tvt/edge.env
fi
if [[ ! -f /etc/tvt/alert-dispatcher.env ]]; then
  install -o root -g tvt-alert -m 0640 \
    "${REPO_ROOT}/deploy/host/tvt-alert-dispatcher.env.example" \
    /etc/tvt/alert-dispatcher.env
fi
if [[ ! -f /etc/tvt/anpr-report.env ]]; then
  install -o root -g tvt-report -m 0640 \
    "${REPO_ROOT}/deploy/host/tvt-anpr-report.env.example" \
    /etc/tvt/anpr-report.env
fi
if [[ ! -f /etc/tvt/alertmanager-webhook.token ]]; then
  temporary_token="$(mktemp /etc/tvt/.alertmanager-webhook.token.XXXXXX)"
  trap 'rm -f "${temporary_token:-}"' EXIT
  openssl rand -hex 32 > "${temporary_token}"
  chown root:tvt-alert "${temporary_token}"
  chmod 0640 "${temporary_token}"
  mv "${temporary_token}" /etc/tvt/alertmanager-webhook.token
  trap - EXIT
fi

postgresql_conf_dir=/etc/postgresql/16/main/conf.d
[[ -d "${postgresql_conf_dir}" ]] || {
  echo "PostgreSQL 16 Ubuntu configuration was not found" >&2
  exit 1
}
install -o root -g postgres -m 0644 \
  "${REPO_ROOT}/deploy/host/postgresql-tvt.conf" \
  "${postgresql_conf_dir}/tvt.conf"
systemctl restart postgresql

if ! runuser -u postgres -- psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='tvt-edge'" | grep -qx 1; then
  runuser -u postgres -- psql -v ON_ERROR_STOP=1 -c 'CREATE ROLE "tvt-edge" LOGIN'
fi
if ! runuser -u postgres -- psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='tvt-alert'" | grep -qx 1; then
  runuser -u postgres -- psql -v ON_ERROR_STOP=1 -c 'CREATE ROLE "tvt-alert" LOGIN'
fi
if ! runuser -u postgres -- psql -tAc "SELECT 1 FROM pg_database WHERE datname='tvt'" | grep -qx 1; then
  runuser -u postgres -- createdb tvt
fi

runuser -u postgres -- env TVT_DATABASE_URL=postgresql+psycopg:///tvt \
  "${VENV}/bin/tvt-edge" migrate
runuser -u postgres -- psql -v ON_ERROR_STOP=1 -d tvt <<'SQL'
GRANT CONNECT ON DATABASE tvt TO "tvt-edge";
GRANT USAGE ON SCHEMA public TO "tvt-edge";
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO "tvt-edge";
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO "tvt-edge";
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "tvt-edge";
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO "tvt-edge";
SQL
runuser -u tvt-edge -- env TVT_DATABASE_URL=postgresql+psycopg:///tvt \
  "${VENV}/bin/tvt-edge" seed-solutions \
  --delivery-directory "${TARGET_CATALOG}" \
  --registry 127.0.0.1:5000
runuser -u postgres -- psql -v ON_ERROR_STOP=1 -d tvt <<'SQL'
GRANT CONNECT ON DATABASE tvt TO "tvt-alert";
GRANT USAGE ON SCHEMA public TO "tvt-alert";
GRANT SELECT, INSERT, UPDATE ON
  alert_instances,
  alert_transitions,
  notification_outbox,
  notification_attempts
TO "tvt-alert";
GRANT SELECT ON notification_policies TO "tvt-alert";
SQL

install -o root -g root -m 0644 \
  "${REPO_ROOT}/deploy/systemd/tvt-edge.service" /etc/systemd/system/tvt-edge.service
install -o root -g root -m 0644 \
  "${REPO_ROOT}/deploy/systemd/tvt-camera-sync.service" \
  /etc/systemd/system/tvt-camera-sync.service
install -o root -g root -m 0644 \
  "${REPO_ROOT}/deploy/systemd/tvt-retention.service" \
  /etc/systemd/system/tvt-retention.service
install -o root -g root -m 0644 \
  "${REPO_ROOT}/deploy/systemd/tvt-retention.timer" \
  /etc/systemd/system/tvt-retention.timer
install -o root -g root -m 0644 \
  "${REPO_ROOT}/deploy/systemd/tvt-alert-dispatcher.service" \
  /etc/systemd/system/tvt-alert-dispatcher.service
install -o root -g root -m 0644 \
  "${REPO_ROOT}/deploy/systemd/tvt-anpr-report.service" \
  /etc/systemd/system/tvt-anpr-report.service
install -o root -g root -m 0644 \
  "${REPO_ROOT}/deploy/systemd/tvt-anpr-report.timer" \
  /etc/systemd/system/tvt-anpr-report.timer
install -o root -g root -m 0644 \
  "${REPO_ROOT}/deploy/systemd/tvt-anpr-report-attendance.service" \
  /etc/systemd/system/tvt-anpr-report-attendance.service
install -o root -g root -m 0644 \
  "${REPO_ROOT}/deploy/systemd/tvt-anpr-report-attendance.timer" \
  /etc/systemd/system/tvt-anpr-report-attendance.timer
install -o root -g root -m 0644 \
  "${REPO_ROOT}/deploy/systemd/tvt-k3s-watchdog.service" \
  /etc/systemd/system/tvt-k3s-watchdog.service
install -o root -g root -m 0644 \
  "${REPO_ROOT}/deploy/systemd/tvt-k3s-watchdog.timer" \
  /etc/systemd/system/tvt-k3s-watchdog.timer
systemctl daemon-reload
echo "PostgreSQL and TVT host service configuration are installed."
echo "Review /etc/tvt/edge.env, /etc/tvt/alert-dispatcher.env and /etc/tvt/anpr-report.env."
echo "Install the protected SendGrid keys, initialize site/notification policies, then enable services."

)

tvt_op_build_apt_closure() (
# Source: scripts/build-tvt-apt-closure.sh
set -Eeuo pipefail
umask 022

# This helper runs as root inside the pinned Ubuntu build container. It is not
# intended to be invoked directly on the release workstation.
OUTPUT_DIRECTORY="${1:-}"
NPU_ARCHIVE="${2:-}"
KERNEL_VERSION="${3:-}"
INCLUDE_VOYAGER="${4:-false}"
HOST_UID="${5:-0}"
HOST_GID="${6:-0}"

fail() { printf 'tvt-apt-closure: ERROR: %s\n' "$*" >&2; exit 1; }
log() { printf 'tvt-apt-closure: %s\n' "$*"; }

[[ -n ${OUTPUT_DIRECTORY} && -n ${NPU_ARCHIVE} && -n ${KERNEL_VERSION} ]] || \
  fail "usage: build-tvt-apt-closure.sh OUTPUT NPU_ARCHIVE KERNEL INCLUDE_VOYAGER UID GID"
[[ ${INCLUDE_VOYAGER} == true || ${INCLUDE_VOYAGER} == false ]] || \
  fail "INCLUDE_VOYAGER must be true or false"
[[ -f ${NPU_ARCHIVE} ]] || fail "Intel NPU archive is missing"
[[ -d ${OUTPUT_DIRECTORY} && ! -L ${OUTPUT_DIRECTORY} ]] || \
  fail "output directory is missing or symlinked"

export DEBIAN_FRONTEND=noninteractive
export LANG=C.UTF-8

log "preparing Ubuntu 24.04 package sources"
apt-get update
apt-get install -y --no-install-recommends \
  ca-certificates curl gnupg software-properties-common
add-apt-repository -y ppa:kobuk-team/intel-graphics

if [[ ${INCLUDE_VOYAGER} == true ]]; then
  key_file="$(mktemp)"
  curl --fail --location --silent --show-error \
    --proto '=https' --tlsv1.2 --connect-timeout 20 --retry 3 --retry-all-errors \
    --max-time 120 --output "${key_file}" \
    https://software.axelera.ai/artifactory/api/security/keypair/axelera/public
  fingerprint="$(gpg --show-keys --with-colons "${key_file}" | awk -F: '$1 == "fpr" {print $10; exit}')"
  [[ ${fingerprint} == 5AE357D1638F21311095816A82F63658F8BBFC11 ]] || \
    fail "unexpected Axelera signing-key fingerprint: ${fingerprint:-missing}"
  gpg --batch --yes --dearmor --output /etc/apt/keyrings/axelera.gpg "${key_file}"
  rm -f -- "${key_file}"
  printf '%s\n' \
    'deb [arch=amd64 signed-by=/etc/apt/keyrings/axelera.gpg] https://software.axelera.ai/artifactory/axelera-apt-source ubuntu24 main' \
    >/etc/apt/sources.list.d/axelera.list
  printf '%s\n' 'Package: metis-dkms' 'Pin: version 1.4.17' 'Pin-Priority: 1001' \
    >/etc/apt/preferences.d/tvt-metis
fi
apt-get update

runtime_packages=(
  ca-certificates curl gnupg python3 python3-venv docker.io postgresql-16 openssl
)
hardware_packages=(
  libze-intel-gpu1 libze1 intel-opencl-icd ocl-icd-libopencl1 clinfo
  intel-gsc intel-media-va-driver-non-free libmfx-gen1.2 libvpl2
  libvpl-tools va-driver-all vainfo libtbb12 python3-venv
)
if [[ ${INCLUDE_VOYAGER} == true ]]; then
  hardware_packages+=(dkms build-essential "linux-headers-${KERNEL_VERSION}" metis-dkms)
fi

pins_file="${OUTPUT_DIRECTORY}/.hardware-pins"
: >"${pins_file}"
for package in "${hardware_packages[@]}"; do
  if [[ ${package} == metis-dkms ]]; then
    candidate=1.4.17
    apt-cache show "${package}=${candidate}" >/dev/null 2>&1 || \
      fail "Axelera repository does not provide ${package}=${candidate}"
  else
    candidate="$(apt-cache policy "${package}" | awk '$1 == "Candidate:" {print $2; exit}')"
  fi
  [[ -n ${candidate} && ${candidate} != '(none)' ]] || \
    fail "configured repositories have no candidate for ${package}"
  printf '%s=%s\n' "${package}" "${candidate}" >>"${pins_file}"
done

# Include dependencies declared by the NPU release's Debian packages. The NPU
# packages themselves remain in their signed upstream archive and are installed
# from that archive by the target-side hardware installer.
npu_extract="$(mktemp -d)"
python3 - "${NPU_ARCHIVE}" "${npu_extract}" <<'PY'
import pathlib
import sys
import tarfile

with tarfile.open(sys.argv[1]) as archive:
    archive.extractall(sys.argv[2], filter="data")
PY
npu_dependencies=()
while IFS= read -r -d '' package_file; do
  while IFS= read -r dependency; do
    [[ -n ${dependency} ]] && npu_dependencies+=("${dependency}")
  done < <(
    # A multi-field dpkg-deb query prefixes values with labels such as
    # "Depends:", which apt would then mistake for a package name.
    for field in Depends Pre-Depends; do
      dpkg-deb -f "${package_file}" "${field}" 2>/dev/null || true
    done \
      | tr ',' '\n' \
      | sed -E 's/^ *([^ |(]+).*/\1/; /^[[:space:]]*$/d'
  )
done < <(find "${npu_extract}" -type f -name '*.deb' -print0)
rm -rf -- "${npu_extract}"

# An empty dpkg status database makes apt resolve and download the dependency
# closure instead of silently relying on packages preinstalled in the container.
closure_cache="$(mktemp -d)"
mkdir -p "${closure_cache}/partial"
empty_status="$(mktemp)"
apt-get install -y --download-only --no-install-recommends \
  -o "Dir::State::status=${empty_status}" \
  -o "Dir::Cache::archives=${closure_cache}" \
  "${runtime_packages[@]}" "${hardware_packages[@]}" "${npu_dependencies[@]}"

shopt -s nullglob
debs=("${closure_cache}"/*.deb)
shopt -u nullglob
(( ${#debs[@]} > 0 )) || fail "APT did not produce a Debian package closure"
cp -a "${debs[@]}" "${OUTPUT_DIRECTORY}/"
rm -rf -- "${closure_cache}"
rm -f -- "${empty_status}"

chown -R "${HOST_UID}:${HOST_GID}" "${OUTPUT_DIRECTORY}"
find "${OUTPUT_DIRECTORY}" -type d -exec chmod 0755 {} +
find "${OUTPUT_DIRECTORY}" -type f -exec chmod 0644 {} +
log "downloaded ${#debs[@]} Debian packages"
)

tvt_op_build_release() (
# Source: scripts/build-tvt-edge-release.sh
set -Eeuo pipefail
umask 022

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT=""
REGISTRY_IMAGE=""
NODE_REPORTER_IMAGE=""
NODE_STATUS_CONTROLLER_IMAGE=""
TRAFFIC_IMAGE=""
UI_IMAGE=""
K3S_INSTALLER=""
K3S_BINARY=""
HARDWARE_DIRECTORY=""
APT_DIRECTORY=""
RELEASE_VERSION=""
SOURCE_COMMIT=""
INPUT_LOCK=""
ALLOW_DIRTY_SOURCE=false

usage() {
  cat >&2 <<'EOF'
usage: scripts/tvt-edge-operations.sh build-release --output DIR \
  --registry-image FILE --node-reporter-image FILE \
  --node-status-controller-image FILE --traffic-image FILE \
  --ui-image FILE \
  --k3s-installer FILE --k3s-binary FILE \
  --hardware-directory DIR --apt-directory DIR \
  --input-lock FILE [--version VERSION] [--source-commit SHA]

The output directory must not exist (or must be empty). Image archives must
contain the tags pinned by config/platform.env and config/pipeline.env.
EOF
}

while (($#)); do
  case "$1" in
    --output) OUTPUT="${2:-}"; shift 2 ;;
    --registry-image) REGISTRY_IMAGE="${2:-}"; shift 2 ;;
    --node-reporter-image) NODE_REPORTER_IMAGE="${2:-}"; shift 2 ;;
    --node-status-controller-image) NODE_STATUS_CONTROLLER_IMAGE="${2:-}"; shift 2 ;;
    --traffic-image) TRAFFIC_IMAGE="${2:-}"; shift 2 ;;
    --ui-image) UI_IMAGE="${2:-}"; shift 2 ;;
    --k3s-installer) K3S_INSTALLER="${2:-}"; shift 2 ;;
    --k3s-binary) K3S_BINARY="${2:-}"; shift 2 ;;
    --hardware-directory) HARDWARE_DIRECTORY="${2:-}"; shift 2 ;;
    --apt-directory) APT_DIRECTORY="${2:-}"; shift 2 ;;
    --input-lock) INPUT_LOCK="${2:-}"; shift 2 ;;
    --version) RELEASE_VERSION="${2:-}"; shift 2 ;;
    --source-commit) SOURCE_COMMIT="${2:-}"; shift 2 ;;
    --allow-dirty-source) ALLOW_DIRTY_SOURCE=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage; exit 2 ;;
  esac
done
for value in OUTPUT REGISTRY_IMAGE NODE_REPORTER_IMAGE NODE_STATUS_CONTROLLER_IMAGE \
  TRAFFIC_IMAGE UI_IMAGE K3S_INSTALLER K3S_BINARY HARDWARE_DIRECTORY APT_DIRECTORY INPUT_LOCK; do
  [[ -n ${!value} ]] || { usage; echo "${value} is required" >&2; exit 2; }
done
for file in "${REGISTRY_IMAGE}" "${NODE_REPORTER_IMAGE}" \
  "${NODE_STATUS_CONTROLLER_IMAGE}" "${TRAFFIC_IMAGE}" "${UI_IMAGE}" \
  "${K3S_INSTALLER}" "${K3S_BINARY}" "${INPUT_LOCK}"; do
  [[ -f ${file} && ! -L ${file} ]] || { echo "artifact is missing or symlinked: ${file}" >&2; exit 1; }
done
for directory in "${HARDWARE_DIRECTORY}" "${APT_DIRECTORY}"; do
  [[ -d ${directory} && ! -L ${directory} ]] || { echo "artifact directory is missing or symlinked: ${directory}" >&2; exit 1; }
done
[[ -f ${HARDWARE_DIRECTORY}/driver-recipe.json ]] || { echo "hardware driver-recipe.json is missing" >&2; exit 1; }
[[ -f ${HARDWARE_DIRECTORY}/linux-npu-driver.tar.gz ]] || { echo "hardware NPU archive is missing" >&2; exit 1; }
[[ -d ${HARDWARE_DIRECTORY}/wheels ]] || { echo "hardware wheel directory is missing" >&2; exit 1; }
if [[ -e ${OUTPUT} ]]; then
  [[ -d ${OUTPUT} && -z $(find "${OUTPUT}" -mindepth 1 -maxdepth 1 -print -quit) ]] || {
    echo "output directory must be empty: ${OUTPUT}" >&2
    exit 1
  }
fi

cd "${REPO_ROOT}"
canonical_version="$(python3 scripts/tvt-version.py --check)"
[[ -n ${RELEASE_VERSION} ]] || RELEASE_VERSION="${canonical_version}"
[[ ${RELEASE_VERSION} == "${canonical_version}" ]] || {
  echo "requested release ${RELEASE_VERSION} does not equal canonical ${canonical_version}" >&2
  exit 1
}
[[ -n ${SOURCE_COMMIT} ]] || SOURCE_COMMIT="$(git rev-parse HEAD)"
[[ ${SOURCE_COMMIT} =~ ^[0-9a-f]{40}$ ]] || { echo "--source-commit must be a full Git SHA" >&2; exit 2; }
[[ "$(git rev-parse HEAD)" == "${SOURCE_COMMIT}" ]] || {
  echo "checked-out commit does not match --source-commit" >&2
  exit 1
}
if ! ${ALLOW_DIRTY_SOURCE} && [[ -n $(git status --porcelain) ]]; then
  echo "release builds require a clean worktree" >&2
  exit 1
fi
python3 - "${INPUT_LOCK}" "${RELEASE_VERSION}" "${SOURCE_COMMIT}" <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
try:
    lock = json.loads(path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as error:
    raise SystemExit(f"invalid release input lock: {error}")
if lock.get("release_version") != sys.argv[2]:
    raise SystemExit("release input lock version does not match the requested release")
if lock.get("source_commit") != sys.argv[3]:
    raise SystemExit("release input lock commit does not match the checked-out source")
if lock.get("schema_version") != 2:
    raise SystemExit("release input lock schema is not version 2 (edge-inventory-driven)")
inventory = lock.get("edge_inventory", {})
if not isinstance(inventory, dict) or not inventory.get("kernel_target") \
        or not isinstance(inventory.get("axelera_present"), bool):
    raise SystemExit("release input lock has no valid edge inventory record")
PY
EDGE_AXELERA="$(python3 - "${INPUT_LOCK}" <<'PY'
import json, pathlib, sys
lock = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
print("metis" if lock["edge_inventory"]["axelera_present"] else "intel-only")
PY
)"
[[ "$(basename "${OUTPUT}")" == "tvt-edge-release-${RELEASE_VERSION}-intel-285h-${EDGE_AXELERA}" ]] || {
  echo "output directory must be named tvt-edge-release-${RELEASE_VERSION}-intel-285h-${EDGE_AXELERA}" >&2
  exit 1
}

mkdir -p "${OUTPUT}"/{wheels,images,k3s,hardware,packages/apt}
mkdir -p "${OUTPUT}/tvt_edge/db"
python3 -m pip wheel --wheel-dir "${OUTPUT}/wheels" .

cp -a apexfabric config deploy docs examples scripts solution-packs "${OUTPUT}/"
find "${OUTPUT}" -name '__pycache__' -type d -prune -exec rm -rf -- {} +
cp -a alembic.ini "${OUTPUT}/alembic.ini"
cp -a tvt_edge/db/migrations "${OUTPUT}/tvt_edge/db/"
cp -a prepare-tvt-edge-host.sh install-tvt-edge-host.sh "${OUTPUT}/"
cp -a release/manifest.template.json "${OUTPUT}/manifest.json"
cp -a "${INPUT_LOCK}" "${OUTPUT}/release-inputs.lock.json"
cp -a "${REGISTRY_IMAGE}" "${OUTPUT}/images/registry.tar"
cp -a "${NODE_REPORTER_IMAGE}" "${OUTPUT}/images/node-reporter.tar"
cp -a "${NODE_STATUS_CONTROLLER_IMAGE}" "${OUTPUT}/images/node-status-controller.tar"
cp -a "${TRAFFIC_IMAGE}" "${OUTPUT}/images/$(basename "${TRAFFIC_IMAGE}")"
cp -a "${UI_IMAGE}" "${OUTPUT}/images/ui.tar"
cp -a "${K3S_INSTALLER}" "${OUTPUT}/k3s/install.sh"
cp -a "${K3S_BINARY}" "${OUTPUT}/k3s/k3s"
cp -a "${HARDWARE_DIRECTORY}/." "${OUTPUT}/hardware/"
cp -a "${APT_DIRECTORY}/." "${OUTPUT}/packages/apt/"
chmod 0755 "${OUTPUT}/prepare-tvt-edge-host.sh" "${OUTPUT}/install-tvt-edge-host.sh" \
  "${OUTPUT}/k3s/install.sh" "${OUTPUT}/k3s/k3s"

application_wheel="$(find "${OUTPUT}/wheels" -maxdepth 1 -type f -name 'tvt_runtime-*.whl' -printf '%f\n')"
[[ -n ${application_wheel} && ${application_wheel} != *$'\n'* ]] || {
  echo "could not identify exactly one TVT application wheel" >&2
  exit 1
}
python3 - "${OUTPUT}/manifest.json" "wheels/${application_wheel}" \
  "${RELEASE_VERSION}" "${SOURCE_COMMIT}" "${INPUT_LOCK}" "images/$(basename "${TRAFFIC_IMAGE}")" <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
lock = json.loads(pathlib.Path(sys.argv[5]).read_text(encoding="utf-8"))
manifest = json.loads(path.read_text(encoding="utf-8"))
manifest["artifacts"]["application_wheel"] = sys.argv[2]
manifest["release_version"] = sys.argv[3]
if lock["source_commit"] != sys.argv[4]:
    raise SystemExit("release input lock commit does not match the requested source commit")
manifest["source_commit"] = lock["source_commit"]
manifest["hardware_profile"] = "intel-285h"
manifest["axelera_variant"] = "metis" if lock["edge_inventory"]["axelera_present"] else "intel-only"
manifest["kernel_target"] = lock["edge_inventory"]["kernel_target"]
manifest["edge_inventory_sha256"] = lock["edge_inventory"]["sha256"]
manifest["artifacts"]["traffic_image"] = sys.argv[6]
path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
(cd "${OUTPUT}" && find . -type f ! -name checksums.sha256 -printf '%P\0' \
  | sort -z | xargs -0 sha256sum -- >checksums.sha256)
echo "Built checksum-complete TVT edge release: ${OUTPUT}"

)

tvt_op_build_release_inputs() (
# Source: scripts/build-tvt-release-inputs.sh
set -Eeuo pipefail
umask 027

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INPUT_DIRECTORY=""
CACHE_DIRECTORY=""
RELEASE_VERSION=""
SOURCE_COMMIT=""
EDGE_INVENTORY=""

usage() {
  cat >&2 <<'EOF'
usage: scripts/tvt-edge-operations.sh build-release-inputs --input-directory DIR --edge-inventory FILE [options]

Build every external input needed by make-tvt-edge-release.sh. The builder
requires Ubuntu 24.04 amd64, Docker, Git LFS, Python 3.12, and network access.
Hardware variants (Axelera presence, target kernel) come from the edge probe
file, never from the workstation's own PCI devices or kernel.

options:
  --edge-inventory FILE  edge probe JSON from probe-edge-hardware (required)
  --cache-directory DIR  reusable download cache (default: INPUT_PARENT/cache)
  --version VERSION      release version (default: canonical repository version)
  --source-commit SHA    source revision (default: checked-out commit)
EOF
}

fail() { printf 'tvt-input-builder: ERROR: %s\n' "$*" >&2; exit 1; }
log() { printf 'tvt-input-builder: %s\n' "$*"; }
require_value() { [[ -n ${2:-} ]] || fail "$1 requires a value"; }

while (($#)); do
  case "$1" in
    --input-directory) require_value "$1" "${2:-}"; INPUT_DIRECTORY="$2"; shift 2 ;;
    --edge-inventory) require_value "$1" "${2:-}"; EDGE_INVENTORY="$2"; shift 2 ;;
    --cache-directory) require_value "$1" "${2:-}"; CACHE_DIRECTORY="$2"; shift 2 ;;
    --version) require_value "$1" "${2:-}"; RELEASE_VERSION="$2"; shift 2 ;;
    --source-commit) require_value "$1" "${2:-}"; SOURCE_COMMIT="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage; fail "unknown option: $1" ;;
  esac
done
[[ -n ${INPUT_DIRECTORY} ]] || { usage; exit 2; }
[[ -n ${EDGE_INVENTORY} ]] || { usage; fail "--edge-inventory FILE is required"; }
[[ -f ${EDGE_INVENTORY} && ! -L ${EDGE_INVENTORY} ]] || \
  fail "edge inventory is missing or symlinked: ${EDGE_INVENTORY}"

for command_name in awk curl df docker dpkg flock git grep npm python3 sed sha256sum stat; do
  command -v "${command_name}" >/dev/null 2>&1 || fail "required command is missing: ${command_name}"
done
[[ $(dpkg --print-architecture) == amd64 ]] || fail "release inputs must be built on amd64"
[[ -r /etc/os-release ]] || fail "/etc/os-release is missing"
# shellcheck disable=SC1091
source /etc/os-release
[[ ${ID:-} == ubuntu && ${VERSION_ID:-} == 24.04 ]] || \
  fail "release input builder requires Ubuntu 24.04; found ${ID:-unknown} ${VERSION_ID:-unknown}"

input_parent="$(dirname "${INPUT_DIRECTORY}")"
mkdir -p "${input_parent}"
input_parent="$(cd "${input_parent}" && pwd -P)"
INPUT_DIRECTORY="${input_parent}/$(basename "${INPUT_DIRECTORY}")"
[[ ! -L ${INPUT_DIRECTORY} ]] || fail "refusing symlinked input directory"
if [[ -e ${INPUT_DIRECTORY} ]]; then
  [[ -d ${INPUT_DIRECTORY} ]] || fail "input path exists and is not a directory"
  [[ -z $(find "${INPUT_DIRECTORY}" -mindepth 1 -maxdepth 1 -print -quit) ]] || \
    fail "input directory is not empty; refusing to replace reviewed artifacts: ${INPUT_DIRECTORY}"
fi

[[ -n ${CACHE_DIRECTORY} ]] || CACHE_DIRECTORY="${input_parent}/cache"
[[ ! -L ${CACHE_DIRECTORY} ]] || fail "refusing symlinked cache directory"
mkdir -p "${CACHE_DIRECTORY}"
CACHE_DIRECTORY="$(cd "${CACHE_DIRECTORY}" && pwd -P)"
case "${CACHE_DIRECTORY}/" in
  "${INPUT_DIRECTORY}/"*) fail "cache directory must be outside the input directory" ;;
esac

cd "${REPO_ROOT}"
# shellcheck source=config/platform.env
source config/platform.env
# shellcheck source=config/pipeline.env
source config/pipeline.env
# shellcheck source=config/hardware-matrix.env
source config/hardware-matrix.env
[[ -n ${RELEASE_VERSION} ]] || RELEASE_VERSION="$(python3 scripts/tvt-version.py --check)"
[[ ${RELEASE_VERSION} == "$(python3 scripts/tvt-version.py --check)" ]] || \
  fail "requested release does not equal the canonical version"
[[ -n ${SOURCE_COMMIT} ]] || SOURCE_COMMIT="$(git rev-parse HEAD)"
[[ ${SOURCE_COMMIT} =~ ^[0-9a-f]{40}$ && ${SOURCE_COMMIT} == "$(git rev-parse HEAD)" ]] || \
  fail "--source-commit must equal the full checked-out Git commit"

# Hardware variants come from the edge probe file, never from the
# workstation's own PCI devices or kernel. Verify the inventory, check the
# checksum sidecar when present, and derive the target kernel + Voyager flag.
EDGE_INVENTORY="$(cd "$(dirname "${EDGE_INVENTORY}")" && pwd -P)/$(basename "${EDGE_INVENTORY}")"
python3 scripts/tvt-hardware-inventory.py verify --inventory "${EDGE_INVENTORY}" \
  || fail "edge inventory failed validation"
sidecar="${EDGE_INVENTORY}.sha256"
if [[ -L ${sidecar} ]]; then
  fail "edge inventory checksum sidecar is symlinked"
elif [[ -f ${sidecar} ]]; then
  (cd "$(dirname "${EDGE_INVENTORY}")" && sha256sum --check --status "$(basename "${sidecar}")") || \
    fail "edge inventory checksum sidecar does not match"
else
  log "warning: edge inventory has no .sha256 sidecar; continuing with the validated file"
fi
EDGE_KERNEL="$(python3 - "${EDGE_INVENTORY}" <<'PY'
import json, pathlib, sys
print(json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))["kernel_version"])
PY
)"
EDGE_AXELERA="$(python3 - "${EDGE_INVENTORY}" <<'PY'
import json, pathlib, sys
document = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
print("true" if document["axelera_present"] else "false")
PY
)"
INVENTORY_SHA256="$(sha256sum "${EDGE_INVENTORY}" | awk '{print $1}')"
dpkg --compare-versions "${EDGE_KERNEL}" ge "${MINIMUM_KERNEL}" || \
  fail "edge kernel ${EDGE_KERNEL} is below qualified minimum ${MINIMUM_KERNEL}"
INCLUDE_VOYAGER="${EDGE_AXELERA}"
log "edge profile ${HARDWARE_PROFILE}: axelera=${EDGE_AXELERA} kernel-target=${EDGE_KERNEL}"

available_mib="$(df -Pm "${input_parent}" | awk 'NR == 2 {print $4}')"
(( available_mib >= 20480 )) || \
  fail "at least 20 GiB free is required; found ${available_mib} MiB"

exec 9>"${CACHE_DIRECTORY}/.input-build.lock"
flock --wait 0 9 || fail "another release-input build is using ${CACHE_DIRECTORY}"

docker_command=(docker)
if ! docker info >/dev/null 2>&1; then
  if sudo -n docker info >/dev/null 2>&1; then
    docker_command=(sudo -n docker)
  elif [[ -t 0 && -t 1 ]]; then
    log "Docker requires sudo; requesting an interactive sudo credential"
    sudo -v
    docker_command=(sudo docker)
    "${docker_command[@]}" info >/dev/null 2>&1 || fail "Docker is not reachable"
  else
    fail "Docker is not reachable; make 'docker info' or 'sudo docker info' succeed first"
  fi
fi

temporary_root="$(mktemp -d "${input_parent}/.tvt-inputs.build.XXXXXX")"
staging="${temporary_root}/inputs"
mkdir -p "${staging}"/{images,k3s,hardware/wheels,apt}
cp -f -- "${EDGE_INVENTORY}" "${staging}/hardware/edge-inventory.json"
cleanup() {
  if [[ -n ${temporary_root:-} && -d ${temporary_root} ]]; then
    case "${temporary_root}" in
      "${input_parent}"/.tvt-inputs.build.*) rm -rf -- "${temporary_root}" ;;
      *) printf 'tvt-input-builder: refusing unsafe cleanup path: %s\n' "${temporary_root}" >&2 ;;
    esac
  fi
}
trap cleanup EXIT

download_atomic() {
  local url="$1" destination="$2" temporary
  if [[ -s ${destination} ]]; then
    log "reusing cached $(basename "${destination}")"
    return 0
  fi
  mkdir -p "$(dirname "${destination}")"
  temporary="$(mktemp "${destination}.partial.XXXXXX")"
  if ! curl --fail --location --silent --show-error \
    --proto '=https' --tlsv1.2 --connect-timeout 20 --retry 3 --retry-all-errors \
    --max-time 1800 --output "${temporary}" "${url}"; then
    rm -f -- "${temporary}"
    fail "download failed: ${url}"
  fi
  [[ -s ${temporary} ]] || { rm -f -- "${temporary}"; fail "download was empty: ${url}"; }
  mv -f -- "${temporary}" "${destination}"
}

save_registry_image() {
  local configured_tag expected_digest architecture temporary_archive
  configured_tag="${LOCAL_REGISTRY_IMAGE%%@*}"
  expected_digest="${LOCAL_REGISTRY_IMAGE##*@}"
  log "pulling pinned Registry image ${LOCAL_REGISTRY_IMAGE}"
  "${docker_command[@]}" pull --platform linux/amd64 "${LOCAL_REGISTRY_IMAGE}"
  architecture="$("${docker_command[@]}" image inspect --format '{{.Architecture}}' "${LOCAL_REGISTRY_IMAGE}")"
  [[ ${architecture} == amd64 ]] || fail "Registry image architecture is ${architecture}, not amd64"
  "${docker_command[@]}" image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' \
    "${LOCAL_REGISTRY_IMAGE}" | grep -Fq "@${expected_digest}" || \
    fail "Docker did not retain the expected Registry digest ${expected_digest}"
  "${docker_command[@]}" tag "${LOCAL_REGISTRY_IMAGE}" "${configured_tag}"
  temporary_archive="$(mktemp "${staging}/images/.registry.tar.XXXXXX")"
  "${docker_command[@]}" save --output "${temporary_archive}" "${configured_tag}"
  mv -f -- "${temporary_archive}" "${staging}/images/registry.tar"
}

# deploy/ui/Dockerfile is a frozen, exact copy (AGENTS.md §6) and must not be
# edited to fix a TVT problem. It COPYs deploy/ui/web/dist/ with no explicit
# mode, so the static files inherit the build host's umask; under
# deploy/single-box/ui.yaml's non-root securityContext (uid/gid 101) that can
# leave them unreadable by nginx (403 on every request). Apply the fix as a
# TVT-owned layer on top of the already-built frozen image instead.
fix_ui_image_static_permissions() {
  local image="$1" fixup_context
  shift
  fixup_context="$(mktemp -d)"
  printf 'FROM %s\nRUN chmod 0644 /etc/nginx/nginx.conf && chmod -R a+rX /usr/share/nginx/html\n' \
    "${image}" >"${fixup_context}/Dockerfile"
  "$@" build --pull=false -f "${fixup_context}/Dockerfile" -t "${image}" "${fixup_context}"
  rm -rf "${fixup_context}"
}

build_control_image() {
  local component="$1" source_directory="$2" image architecture temporary_archive
  image="apexfabric/${component}:${NODE_MANAGEMENT_IMAGE_VERSION}"
  log "building ${image} for linux/amd64"
  "${docker_command[@]}" build --pull=false --provenance=false --platform linux/amd64 \
    -f "${REPO_ROOT}/apexfabric/node_management/${source_directory}/Dockerfile" \
    -t "${image}" "${REPO_ROOT}"
  architecture="$("${docker_command[@]}" image inspect --format '{{.Architecture}}' "${image}")"
  [[ ${architecture} == amd64 ]] || fail "${image} architecture is ${architecture}, not amd64"
  temporary_archive="$(mktemp "${staging}/images/.${component}.tar.XXXXXX")"
  "${docker_command[@]}" save --output "${temporary_archive}" "${image}"
  mv -f -- "${temporary_archive}" "${staging}/images/${component}.tar"
}

build_ui_image() {
  local image architecture temporary_archive
  image="apexfabric/ui:${UI_IMAGE_VERSION}"
  log "building ${image} for linux/amd64"
  "${docker_command[@]}" build --pull=false --provenance=false --platform linux/amd64 \
    -f "${REPO_ROOT}/deploy/ui/Dockerfile" \
    -t "${image}" "${REPO_ROOT}"
  fix_ui_image_static_permissions "${image}" "${docker_command[@]}"
  architecture="$("${docker_command[@]}" image inspect --format '{{.Architecture}}' "${image}")"
  [[ ${architecture} == amd64 ]] || fail "${image} architecture is ${architecture}, not amd64"
  temporary_archive="$(mktemp "${staging}/images/.ui.tar.XXXXXX")"
  "${docker_command[@]}" save --output "${temporary_archive}" "${image}"
  mv -f -- "${temporary_archive}" "${staging}/images/ui.tar"
}

acquire_k3s() {
  local k3s_cache installer_cache sums_cache release_url expected actual reported
  mkdir -p "${CACHE_DIRECTORY}/k3s/${K3S_VERSION}"
  k3s_cache="${CACHE_DIRECTORY}/k3s/${K3S_VERSION}/k3s"
  installer_cache="${CACHE_DIRECTORY}/k3s/${K3S_VERSION}/install.sh"
  sums_cache="${CACHE_DIRECTORY}/k3s/${K3S_VERSION}/sha256sum-amd64.txt"
  release_url="https://github.com/k3s-io/k3s/releases/download/${K3S_VERSION}"
  log "acquiring K3s ${K3S_VERSION}"
  download_atomic "${release_url}/k3s" "${k3s_cache}"
  download_atomic "${release_url}/sha256sum-amd64.txt" "${sums_cache}"
  download_atomic "https://get.k3s.io" "${installer_cache}"
  expected="$(awk '$1 ~ /^[0-9a-f]{64}$/ && (NF == 1 || $2 == "k3s" || $2 == "*k3s") {print $1; exit}' "${sums_cache}")"
  [[ ${expected} =~ ^[0-9a-f]{64}$ ]] || fail "could not parse the official K3s amd64 checksum"
  actual="$(sha256sum "${k3s_cache}" | awk '{print $1}')"
  [[ ${actual} == "${expected}" ]] || fail "K3s checksum mismatch"
  chmod 0755 "${k3s_cache}" "${installer_cache}"
  reported="$("${k3s_cache}" --version | awk 'NR == 1 {print $3}')"
  [[ ${reported} == "${K3S_VERSION}" ]] || \
    fail "K3s binary reports ${reported}, expected ${K3S_VERSION}"
  cp -a "${k3s_cache}" "${staging}/k3s/k3s"
  cp -a "${installer_cache}" "${staging}/k3s/install.sh"
}

acquire_traffic() {
  local cache_directory source_archive actual_size actual_digest
  cache_directory="${CACHE_DIRECTORY}/tvt-mills/${PIPELINE_TRAFFIC_VERSION}"
  source_archive="${cache_directory}/${PIPELINE_TRAFFIC_ARCHIVE}"
  mkdir -p "${cache_directory}"
  log "acquiring pinned TVT Mills OCI archive ${PIPELINE_TRAFFIC_VERSION}"
  download_atomic "${PIPELINE_TRAFFIC_ARCHIVE_URL}" "${source_archive}"
  [[ -f ${source_archive} && ! -L ${source_archive} ]] || \
    fail "TVT Mills OCI archive was not downloaded"
  actual_size="$(stat -c '%s' "${source_archive}")"
  [[ ${actual_size} == "${PIPELINE_TRAFFIC_ARCHIVE_SIZE}" ]] || \
    fail "TVT Mills archive size is ${actual_size}, expected ${PIPELINE_TRAFFIC_ARCHIVE_SIZE}"
  actual_digest="$(sha256sum "${source_archive}" | awk '{print $1}')"
  [[ ${actual_digest} == "${PIPELINE_TRAFFIC_ARCHIVE_SHA256}" ]] || \
    fail "TVT Mills archive checksum mismatch"
  "${REPO_ROOT}/scripts/tvt-edge-operations.sh" verify-docker-archive-tag \
    --archive "${source_archive}" --expected "${PIPELINE_TRAFFIC_ARCHIVE_IMAGE}"
  cp --reflink=auto --sparse=always "${source_archive}" \
    "${staging}/images/${PIPELINE_TRAFFIC_ARCHIVE}"
}

log_edge_profile() {
  # INCLUDE_VOYAGER and EDGE_KERNEL come from the edge inventory loaded above;
  # the workstation's own PCI devices and kernel must never influence the bundle.
  log "Axelera Voyager closure enabled: ${INCLUDE_VOYAGER} (edge kernel ${EDGE_KERNEL})"
}

acquire_npu_archive() {
  local metadata_json metadata_file npu_cache api_digest actual_digest
  metadata_json="${temporary_root}/intel-npu-release.json"
  metadata_file="${temporary_root}/intel-npu-metadata.txt"
  curl --fail --location --silent --show-error \
    --proto '=https' --tlsv1.2 --connect-timeout 20 --retry 3 --retry-all-errors \
    -H 'Accept: application/vnd.github+json' \
    -H 'X-GitHub-Api-Version: 2022-11-28' \
    -H 'User-Agent: tvt-release-input-builder/1' \
    --output "${metadata_json}" \
    'https://api.github.com/repos/intel/linux-npu-driver/releases?per_page=100'
  python3 - "${metadata_json}" "${PIPELINE_INTEL_NPU_DRIVER_VERSION}" "${metadata_file}" <<'PY'
import json
import pathlib
import sys

releases = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
version = sys.argv[2]
expected_name = f"linux-npu-driver-v{version}-ubuntu2404.tar.gz"
matches = [
    (release, asset)
    for release in releases
    for asset in release.get("assets", [])
    if asset.get("name") == expected_name
]
if len(matches) != 1:
    raise SystemExit(f"could not identify the unique Intel NPU asset {expected_name}")
release, asset = matches[0]
url = str(asset.get("browser_download_url", ""))
if not url.startswith("https://github.com/intel/linux-npu-driver/releases/download/"):
    raise SystemExit("GitHub returned an untrusted Intel NPU asset URL")
digest = str(asset.get("digest") or "").removeprefix("sha256:")
pathlib.Path(sys.argv[3]).write_text(
    "\n".join((str(release["tag_name"]), str(asset["name"]), url, digest)) + "\n",
    encoding="utf-8",
)
PY
  mapfile -t NPU_METADATA <"${metadata_file}"
  npu_cache="${CACHE_DIRECTORY}/hardware/${PIPELINE_INTEL_NPU_DRIVER_VERSION}/linux-npu-driver.tar.gz"
  download_atomic "${NPU_METADATA[2]}" "${npu_cache}"
  actual_digest="$(sha256sum "${npu_cache}" | awk '{print $1}')"
  api_digest="${NPU_METADATA[3]:-}"
  if [[ -n ${api_digest} ]]; then
    [[ ${api_digest} =~ ^[0-9a-f]{64}$ && ${actual_digest} == "${api_digest}" ]] || \
      fail "Intel NPU archive checksum does not match GitHub release metadata"
  fi
  NPU_SHA256="${actual_digest}"
  cp -a "${npu_cache}" "${staging}/hardware/linux-npu-driver.tar.gz"
}

resolve_python_wheels() {
  log "resolving OpenVINO Python 3.12 amd64 wheel closure"
  python3 -m pip download --disable-pip-version-check --no-cache-dir \
    --only-binary=:all: --platform manylinux2014_x86_64 \
    --implementation cp --python-version 3.12 --abi cp312 \
    --dest "${staging}/hardware/wheels" openvino openvino-genai
  if [[ ${INCLUDE_VOYAGER} == true ]]; then
    mkdir -p "${staging}/hardware/voyager-wheels"
    log "resolving Axelera Voyager 1.6.1 wheel closure"
    python3 -m pip download --disable-pip-version-check --no-cache-dir \
      --only-binary=:all: --platform manylinux2014_x86_64 \
      --implementation cp --python-version 3.12 --abi cp312 \
      --extra-index-url https://software.axelera.ai/artifactory/api/pypi/axelera-pypi/simple \
      --dest "${staging}/hardware/voyager-wheels" 'axelera-rt==1.6.1'
  fi
}

build_apt_closure() {
  log "resolving the Ubuntu 24.04 amd64 offline APT closure"
  "${docker_command[@]}" pull --platform linux/amd64 "${PIPELINE_UBUNTU_BASE_IMAGE}"
  "${docker_command[@]}" run --rm --platform linux/amd64 \
    -v "${REPO_ROOT}/scripts/tvt-edge-operations.sh:/usr/local/bin/tvt-edge-operations:ro" \
    -v "${staging}/hardware/linux-npu-driver.tar.gz:/input/linux-npu-driver.tar.gz:ro" \
    -v "${staging}/apt:/output" \
    "${PIPELINE_UBUNTU_BASE_IMAGE}" \
    /usr/local/bin/tvt-edge-operations build-apt-closure \
      /output /input/linux-npu-driver.tar.gz "${EDGE_KERNEL}" "${INCLUDE_VOYAGER}" \
      "$(id -u)" "$(id -g)"
  [[ -f ${staging}/apt/.hardware-pins ]] || fail "APT resolver did not emit hardware pins"
  mv "${staging}/apt/.hardware-pins" "${temporary_root}/hardware-pins.txt"
}

write_hardware_recipe() {
  python3 - "${staging}/hardware" "${temporary_root}/hardware-pins.txt" \
    "${NPU_METADATA[0]}" "${NPU_METADATA[1]}" "${NPU_METADATA[2]}" "${NPU_SHA256}" \
    "${EDGE_KERNEL}" "${INCLUDE_VOYAGER}" "${INVENTORY_SHA256}" <<'PY'
import email
import hashlib
import json
import pathlib
import re
import sys
import zipfile

root = pathlib.Path(sys.argv[1])
apt = dict(line.split("=", 1) for line in pathlib.Path(sys.argv[2]).read_text().splitlines())
tag, asset, url, npu_sha, kernel, include_voyager, inventory_sha = sys.argv[3:]

def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()

def wheel_records(directory):
    hashes = {}
    versions = {}
    if not directory.is_dir():
        return hashes, versions
    for wheel in sorted(directory.glob("*.whl")):
        hashes[wheel.name] = sha256(wheel)
        with zipfile.ZipFile(wheel) as archive:
            names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
            if len(names) != 1:
                raise SystemExit(f"cannot identify wheel metadata: {wheel.name}")
            metadata = email.message_from_bytes(archive.read(names[0]))
        name = re.sub(r"[-_.]+", "-", metadata.get("Name", "")).lower()
        versions[name] = metadata.get("Version", "")
    return hashes, versions

wheels, versions = wheel_records(root / "wheels")
for required in ("openvino", "openvino-genai"):
    if not versions.get(required):
        raise SystemExit(f"wheel closure does not contain {required}")
voyager_wheels, voyager_versions = wheel_records(root / "voyager-wheels")
voyager_enabled = include_voyager == "true"
if voyager_enabled and voyager_versions.get("axelera-rt") != "1.6.1":
    raise SystemExit("Voyager wheel closure does not contain axelera-rt 1.6.1")

recipe = {
    "schema_version": 3,
    "policy": "edge-inventory-driven",
    "hardware_profile": "intel-285h",
    "os_id": "ubuntu",
    "os_version_id": "24.04",
    "architecture": "amd64",
    "kernel_target": kernel,
    "inventory_sha256": inventory_sha,
    "apt": apt,
    "python": {name: versions[name] for name in ("openvino", "openvino-genai")},
    "wheels": wheels,
    "npu": {"release": tag, "asset": asset, "url": url, "sha256": npu_sha},
    "voyager": {
        "enabled": voyager_enabled,
        "runtime_version": "1.6.1" if voyager_enabled else None,
        "driver_package": "metis-dkms" if voyager_enabled else None,
        "driver_version": "1.4.17" if voyager_enabled else None,
        "firmware_recommended": "1.6.0" if voyager_enabled else None,
        "board_controller_recommended": "7.4" if voyager_enabled else None,
        "wheels": voyager_wheels,
    },
}
(root / "driver-recipe.json").write_text(
    json.dumps(recipe, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
PY
}

save_registry_image
build_control_image node-reporter reporter
build_control_image node-status-controller status_controller
npm --prefix "${REPO_ROOT}/deploy/ui/web" ci
npm --prefix "${REPO_ROOT}/deploy/ui/web" run build
build_ui_image
# npm ci leaves symlinks (e.g. node_modules/.bin/*) in the source tree; the
# release bundle copies deploy/ verbatim and rejects any symlink it contains.
rm -rf "${REPO_ROOT}/deploy/ui/web/node_modules"
acquire_k3s
acquire_traffic
log_edge_profile
acquire_npu_archive
resolve_python_wheels
build_apt_closure
write_hardware_recipe

chmod 0755 "${staging}/k3s/install.sh" "${staging}/k3s/k3s"
python3 scripts/tvt-release-inputs.py create \
  --input-directory "${staging}" --output "${staging}/release-inputs.lock.json" \
  --release-version "${RELEASE_VERSION}" --source-commit "${SOURCE_COMMIT}" \
  --platform-config config/platform.env --pipeline-config config/pipeline.env \
  --hardware-matrix config/hardware-matrix.env --edge-inventory "${EDGE_INVENTORY}"
python3 scripts/tvt-release-inputs.py verify \
  --input-directory "${staging}" --lock "${staging}/release-inputs.lock.json" \
  --release-version "${RELEASE_VERSION}" --source-commit "${SOURCE_COMMIT}" \
  --platform-config config/platform.env --pipeline-config config/pipeline.env \
  --hardware-matrix config/hardware-matrix.env --edge-inventory "${EDGE_INVENTORY}"

if [[ -d ${INPUT_DIRECTORY} ]]; then rmdir "${INPUT_DIRECTORY}"; fi
mv "${staging}" "${INPUT_DIRECTORY}"
log "built and locked release inputs at ${INPUT_DIRECTORY}"
)

tvt_op_configure_k3s_registry() (
# Source: scripts/configure-k3s-registry.sh
set -Eeuo pipefail

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=config/platform.env
source "${REPO_ROOT}/config/platform.env"
REGISTRY="${LOCAL_REGISTRY_ADDRESS}"
SCHEME="http"
RESTART=false

usage() {
  echo "usage: scripts/tvt-edge-operations.sh configure-k3s-registry [--registry HOST[:PORT]] [--scheme http|https] [--restart]" >&2
}

while (($#)); do
  case "$1" in
    --registry) REGISTRY="${2:-}"; shift 2 ;;
    --scheme) SCHEME="${2:-}"; shift 2 ;;
    --restart) RESTART=true; shift ;;
    *) usage; exit 2 ;;
  esac
done

if [[ ! "${REGISTRY}" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?(:[0-9]{1,5})?$ ]]; then
  echo "--registry must be a HOST[:PORT] value" >&2
  exit 2
fi
if [[ "${REGISTRY}" == *:* ]]; then
  registry_port="${REGISTRY##*:}"
  if ((10#${registry_port} < 1 || 10#${registry_port} > 65535)); then
    echo "--registry port must be between 1 and 65535" >&2
    exit 2
  fi
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

)

tvt_op_enable_k3s_secrets_encryption() (
# Source: scripts/enable-k3s-secrets-encryption.sh
set -Eeuo pipefail

[[ ${EUID} -eq 0 ]] || { echo "run as root" >&2; exit 1; }
command -v k3s >/dev/null 2>&1 || { echo "K3s is not installed" >&2; exit 1; }
mapfile -t nodes < <(k3s kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')
if [[ ${#nodes[@]} -ne 1 || -z "${nodes[0]}" ]]; then
  echo "TVT Secret encryption migration requires exactly one K3s node" >&2
  exit 1
fi

status="$(k3s secrets-encrypt status 2>&1 || true)"
if grep -q 'Encryption Status: Enabled' <<<"${status}"; then
  echo "K3s Secret encryption is already enabled"
  exit 0
fi

# This command creates the initial encryption configuration for a server that
# was installed before --secrets-encryption became part of the TVT profile.
k3s secrets-encrypt enable
install -d -o root -g root -m 0700 /etc/rancher/k3s
config=/etc/rancher/k3s/config.yaml
touch "${config}"
chmod 0600 "${config}"
if ! grep -Eq '^secrets-encryption:[[:space:]]*true' "${config}"; then
  echo 'secrets-encryption: true' >>"${config}"
fi
if ! grep -Eq '^secrets-encryption-provider:[[:space:]]*secretbox' "${config}"; then
  echo 'secrets-encryption-provider: secretbox' >>"${config}"
fi
systemctl restart k3s
k3s secrets-encrypt rotate-keys
systemctl restart k3s
status="$(k3s secrets-encrypt status)"
grep -q 'Encryption Status: Enabled' <<<"${status}" || {
  echo "K3s Secret encryption did not become enabled" >&2
  exit 1
}
grep -q 'Current Rotation Stage: reencrypt_finished' <<<"${status}" || {
  echo "K3s Secret reencryption did not finish" >&2
  exit 1
}
echo "K3s Secret encryption is enabled and existing Secrets were reencrypted"

)

tvt_op_import_pipeline_traffic_image() (
# Source: scripts/import-pipeline-traffic-image.sh
set -Eeuo pipefail
umask 077

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=config/platform.env
source "${REPO_ROOT}/config/platform.env"
# shellcheck source=config/pipeline.env
source "${REPO_ROOT}/config/pipeline.env"

MODE=archive
STATE_DIR="${TVT_PIPELINE_STATE_DIR:-${REPO_ROOT}/build/pipeline}"
WORK_DIR="${STATE_DIR}/work"
LOCK_OUTPUT="${STATE_DIR}/traffic-image.lock.json"
CONCURRENCY_LOCK=""
ARCHIVE_FILE="${TVT_PIPELINE_ARCHIVE_FILE:-}"
METADATA_DIRECTORY="${TVT_PIPELINE_METADATA_DIRECTORY:-}"

usage() {
  echo "usage: scripts/tvt-edge-operations.sh import-pipeline-traffic-image --mode archive --archive-file FILE --metadata-directory DIR [--work-dir PATH] [--lock-output FILE] [--concurrency-lock FILE]" >&2
}

while (($#)); do
  case "$1" in
    --mode) MODE="${2:-}"; shift 2 ;;
    --work-dir) WORK_DIR="${2:-}"; shift 2 ;;
    --lock-output) LOCK_OUTPUT="${2:-}"; shift 2 ;;
    --concurrency-lock) CONCURRENCY_LOCK="${2:-}"; shift 2 ;;
    --archive-file) ARCHIVE_FILE="${2:-}"; shift 2 ;;
    --metadata-directory) METADATA_DIRECTORY="${2:-}"; shift 2 ;;
    *) usage; exit 2 ;;
  esac
done
[[ "${MODE}" == archive ]] || {
  echo "source-build mode is not supported for the vendor release; use the checksum-pinned OCI archive" >&2
  exit 2
}
[[ -n ${ARCHIVE_FILE} && -n ${METADATA_DIRECTORY} \
  && -f ${ARCHIVE_FILE} && ! -L ${ARCHIVE_FILE} \
  && -d ${METADATA_DIRECTORY} && ! -L ${METADATA_DIRECTORY} ]] || {
  echo "--archive-file and --metadata-directory are required together in archive mode" >&2
  exit 2
}
ARCHIVE_FILE="$(cd "$(dirname "${ARCHIVE_FILE}")" && pwd -P)/$(basename "${ARCHIVE_FILE}")"
METADATA_DIRECTORY="$(cd "${METADATA_DIRECTORY}" && pwd -P)"
[[ -n "${CONCURRENCY_LOCK}" ]] || CONCURRENCY_LOCK="${LOCK_OUTPUT}.flock"
SOURCE_MODE=bundled

[[ "${PIPELINE_REVISION}" =~ ^[0-9a-f]{40}$ ]] || {
  echo "PIPELINE_REVISION must be a full 40-character commit" >&2
  exit 1
}
for digest_variable in \
  PIPELINE_TRAFFIC_ARCHIVE_SHA256 \
  PIPELINE_TRAFFIC_CONTRACT_SHA256 \
  PIPELINE_TRAFFIC_DESIRED_STATE_SCHEMA_SHA256 \
  PIPELINE_TRAFFIC_METRICS_SCHEMA_SHA256 \
  PIPELINE_TRAFFIC_ANALYTICS_EVENT_SCHEMA_SHA256 \
  PIPELINE_TRAFFIC_ANALYTICS_EVENT_EXAMPLE_SHA256 \
  PIPELINE_TRAFFIC_PLAN_COMPILER_SHA256 \
  PIPELINE_FACE_MODEL_ADAFACE_XML_SHA256 \
  PIPELINE_FACE_MODEL_ADAFACE_BIN_SHA256 \
  PIPELINE_FACE_MODEL_DETECTOR_SHA256 \
  PIPELINE_FACE_MODEL_RECOGNIZER_SHA256; do
  [[ "${!digest_variable}" =~ ^[0-9a-f]{64}$ ]] || {
    echo "${digest_variable} must be a sha256 digest" >&2
    exit 1
  }
done
[[ "${PIPELINE_UBUNTU_BASE_IMAGE}" =~ ^[^[:space:]@]+:[^[:space:]@]+@sha256:[0-9a-f]{64}$ ]] || {
  echo "PIPELINE_UBUNTU_BASE_IMAGE must include a tag and sha256 digest" >&2
  exit 1
}
[[ "${PIPELINE_TRAFFIC_LOCAL_TAG}" == *"${PIPELINE_TRAFFIC_VERSION}"* ]] || {
  echo "PIPELINE_TRAFFIC_LOCAL_TAG must include ${PIPELINE_TRAFFIC_VERSION}" >&2
  exit 1
}
[[ "${PIPELINE_TRAFFIC_PLAN_COMPILER_COMPATIBILITY_ID}" == "tvt-direct-desired-state-v1" ]] || {
  echo "unsupported TVT Mills plan-compiler compatibility ID" >&2
  exit 1
}
[[ "${LOCAL_REGISTRY_ADDRESS}" == "127.0.0.1:5000" ]] || {
  echo "the Phase 3 Traffic catalog is restricted to 127.0.0.1:5000" >&2
  exit 1
}
required_commands=(curl docker flock python3 sha256sum tar timeout)
for command_name in "${required_commands[@]}"; do
  command -v "${command_name}" >/dev/null 2>&1 || {
    echo "required command not found: ${command_name}" >&2
    exit 1
  }
done

mkdir -p "$(dirname "${LOCK_OUTPUT}")" "$(dirname "${CONCURRENCY_LOCK}")"
exec 9>"${CONCURRENCY_LOCK}"
if ! flock --wait 0 9; then
  echo "another PIPELINE Traffic import is already running" >&2
  exit 75
fi

retry_with_timeout() {
  local description="$1"
  local duration="$2"
  shift 2
  local attempt
  for attempt in 1 2 3; do
    if timeout --signal=TERM "${duration}" "$@"; then
      return 0
    fi
    if [[ ${attempt} -lt 3 ]]; then
      echo "${description} failed (attempt ${attempt}/3); retrying in 5 seconds" >&2
      sleep 5
    fi
  done
  echo "${description} failed after 3 attempts" >&2
  return 1
}

oci_load_directory=""
docker_load_container_archive() {
  local archive="$1"
  local load_archive="${archive}"
  if tar -tf "${archive}" | grep -qx 'index.json'; then
    oci_load_directory="$(mktemp -d "${WORK_DIR}/oci-docker-load.XXXXXX")"
    load_archive="${oci_load_directory}/image.tar"
    cp --reflink=auto --sparse=always "${archive}" "${load_archive}"
    python3 - "${archive}" "${oci_load_directory}/manifest.json" <<'PY'
import json
import pathlib
import re
import sys
import tarfile

source = pathlib.Path(sys.argv[1])
output = pathlib.Path(sys.argv[2])
digest_pattern = re.compile(r"sha256:([0-9a-f]{64})")
with tarfile.open(source, mode="r:") as archive:
    index_stream = archive.extractfile("index.json")
    if index_stream is None:
        raise SystemExit("OCI archive index.json cannot be read")
    index = json.load(index_stream)
    manifests = index.get("manifests") if isinstance(index, dict) else None
    if not isinstance(manifests, list) or len(manifests) != 1:
        raise SystemExit("OCI archive must contain exactly one image manifest")
    descriptor = manifests[0]
    match = digest_pattern.fullmatch(str(descriptor.get("digest", "")))
    annotations = descriptor.get("annotations") if isinstance(descriptor, dict) else None
    tag = annotations.get("org.opencontainers.image.ref.name") if isinstance(annotations, dict) else None
    if match is None or not isinstance(tag, str) or not tag:
        raise SystemExit("OCI archive manifest descriptor is invalid")
    manifest_name = f"blobs/sha256/{match.group(1)}"
    manifest_stream = archive.extractfile(manifest_name)
    if manifest_stream is None:
        raise SystemExit("OCI image manifest blob cannot be read")
    manifest = json.load(manifest_stream)
    config_match = digest_pattern.fullmatch(str((manifest.get("config") or {}).get("digest", "")))
    layers = manifest.get("layers")
    if config_match is None or not isinstance(layers, list) or not layers:
        raise SystemExit("OCI image manifest config or layers are invalid")
    layer_names = []
    for layer in layers:
        layer_match = digest_pattern.fullmatch(str((layer or {}).get("digest", "")))
        if layer_match is None:
            raise SystemExit("OCI image manifest contains an invalid layer digest")
        layer_names.append(f"blobs/sha256/{layer_match.group(1)}")
document = [{
    "Config": f"blobs/sha256/{config_match.group(1)}",
    "RepoTags": [tag],
    "Layers": layer_names,
}]
output.write_text(json.dumps(document, separators=(",", ":")) + "\n", encoding="utf-8")
PY
    tar --append --file "${load_archive}" -C "${oci_load_directory}" manifest.json
  fi
  timeout --signal=TERM 20m docker load --input "${load_archive}"
}

registry_manifest_digest() {
  local repository="$1"
  local tag="$2"
  local response_dir headers manifest digest computed
  response_dir="$(mktemp -d "$(dirname "${LOCK_OUTPUT}")/manifest.XXXXXX")"
  headers="${response_dir}/headers"
  manifest="${response_dir}/manifest"
  if ! curl --fail --silent --show-error \
    --retry 2 --retry-delay 2 --retry-connrefused --max-time 15 --retry-max-time 45 \
    --dump-header "${headers}" --output "${manifest}" \
    --header 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json' \
    "http://${LOCAL_REGISTRY_ADDRESS}/v2/${repository}/manifests/${tag}"; then
    rm -rf -- "${response_dir}"
    return 1
  fi
  digest="$(awk 'tolower($1) == "docker-content-digest:" {gsub("\r", "", $2); print $2}' "${headers}")"
  computed="sha256:$(sha256sum "${manifest}" | awk '{print $1}')"
  rm -rf -- "${response_dir}"
  [[ "${digest}" =~ ^sha256:[0-9a-f]{64}$ && "${digest}" == "${computed}" ]] || return 1
  printf '%s\n' "${digest}"
}

retry_with_timeout "local registry readiness" 10s \
  curl --fail --silent --show-error --max-time 5 \
  "http://${LOCAL_REGISTRY_ADDRESS}/v2/" >/dev/null
timeout 15s docker info >/dev/null 2>&1 || {
  echo "Docker is not reachable; run with Docker access or as root" >&2
  exit 1
}

selected_local_tag="${PIPELINE_TRAFFIC_LOCAL_TAG}"
if [[ "${MODE}" == build ]]; then
  selected_local_tag="${PIPELINE_TRAFFIC_LOCAL_TAG}-source-build"
fi
local_image="${LOCAL_REGISTRY_ADDRESS}/${PIPELINE_TRAFFIC_LOCAL_REPOSITORY}:${selected_local_tag}"
locked_digest=""
if [[ -f "${LOCK_OUTPUT}" && "$(stat -c '%a' "${LOCK_OUTPUT}")" == 600 ]]; then
  locked_digest="$(python3 - "${LOCK_OUTPUT}" "${PIPELINE_TRAFFIC_CATALOG_ID}" \
    "${PIPELINE_REPOSITORY}" "${PIPELINE_REVISION}" \
    "${PIPELINE_TRAFFIC_DELIVERY_DIR}" "${PIPELINE_TRAFFIC_ARCHIVE}" \
    "${PIPELINE_TRAFFIC_ARCHIVE_SHA256}" "${PIPELINE_TRAFFIC_ARCHIVE_SIZE}" \
    "${LOCAL_REGISTRY_ADDRESS}" "${PIPELINE_TRAFFIC_LOCAL_REPOSITORY}" \
    "${selected_local_tag}" "${PIPELINE_TRAFFIC_CONTRACT_SHA256}" \
    "${PIPELINE_TRAFFIC_DESIRED_STATE_SCHEMA_SHA256}" \
    "${PIPELINE_TRAFFIC_METRICS_SCHEMA_SHA256}" \
    "${PIPELINE_TRAFFIC_ANALYTICS_EVENT_SCHEMA_SHA256}" \
    "${PIPELINE_TRAFFIC_ANALYTICS_EVENT_EXAMPLE_SHA256}" \
    "${PIPELINE_TRAFFIC_PLAN_COMPILER_COMPATIBILITY_ID}" \
    "${PIPELINE_TRAFFIC_PLAN_COMPILER_SHA256}" "${SOURCE_MODE}" <<'PY'
import json
import re
import sys
from pathlib import Path

try:
    lock = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    raise SystemExit(0)
pipeline = lock.get("pipeline", {})
archive = lock.get("archive", {})
image = lock.get("image", {})
metadata = lock.get("metadata", {})
source = lock.get("source", {})
compatibility = lock.get("compatibility", {})
matches = (
    lock.get("format_version") == 2
    and lock.get("catalog_id") == sys.argv[2]
    and pipeline.get("repository") == sys.argv[3]
    and pipeline.get("commit") == sys.argv[4]
    and pipeline.get("delivery_directory") == sys.argv[5]
    and archive.get("filename") == sys.argv[6]
    and archive.get("sha256") == sys.argv[7]
    and archive.get("size") == int(sys.argv[8])
    and image.get("registry") == sys.argv[9]
    and image.get("repository") == sys.argv[10]
    and image.get("tag") == sys.argv[11]
    and metadata.get("image_contract_sha256") == sys.argv[12]
    and metadata.get("desired_state_schema_sha256") == sys.argv[13]
    and metadata.get("metrics_schema_sha256") == sys.argv[14]
    and metadata.get("analytics_event_schema_sha256") == sys.argv[15]
    and metadata.get("analytics_event_example_sha256") == sys.argv[16]
    and compatibility.get("plan_compiler") == sys.argv[17]
    and compatibility.get("sha256") == sys.argv[18]
    and source.get("mode") == sys.argv[19]
)
digest = image.get("digest", "")
reference = f"{sys.argv[9]}/{sys.argv[10]}@{digest}"
if (
    matches
    and re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
    and image.get("reference") == reference
    and image.get("architecture") == "amd64"
    and isinstance(lock.get("verification_timestamp"), str)
):
    print(digest)
PY
)"
fi
if [[ -n "${locked_digest}" ]]; then
  registry_digest="$(registry_manifest_digest \
    "${PIPELINE_TRAFFIC_LOCAL_REPOSITORY}" "${selected_local_tag}" || true)"
  if [[ "${registry_digest}" == "${locked_digest}" ]]; then
    echo "Traffic image is already imported at ${local_image}."
    echo "Immutable digest: ${locked_digest}"
    echo "Lock: ${LOCK_OUTPUT}"
    exit 0
  fi
fi

mkdir -p "${WORK_DIR}"
echo "Synchronizing PIPELINE Traffic catalog pin ${PIPELINE_TRAFFIC_CATALOG_ID}."
if [[ -z ${ARCHIVE_FILE} ]]; then
  echo "Fetching exact PIPELINE commit ${PIPELINE_REVISION}."
else
  echo "Using checksum-verified bundled archive ${ARCHIVE_FILE}."
fi
source_dir="${WORK_DIR}/source-${PIPELINE_REVISION}"
credential_helper=""
verification_dir=""
temporary_context=""
compatibility_context=""
verification_container=""
cleanup() {
  if [[ -n "${verification_container}" ]]; then
    timeout 30s docker rm --force "${verification_container}" >/dev/null 2>&1 || true
  fi
  for temporary_path in "${credential_helper}" "${verification_dir}" "${temporary_context}" \
    "${compatibility_context}" "${oci_load_directory}"; do
    if [[ -n "${temporary_path}" && "${temporary_path}" == "${WORK_DIR}"/* ]]; then
      rm -rf -- "${temporary_path}"
    fi
  done
}
trap cleanup EXIT

if [[ -n ${ARCHIVE_FILE} ]]; then
  delivery_path="${METADATA_DIRECTORY}"
  for metadata_check in \
    "${PIPELINE_TRAFFIC_CONTRACT_SHA256} image-contract.yaml" \
    "${PIPELINE_TRAFFIC_DESIRED_STATE_SCHEMA_SHA256} desired-state.schema.json" \
    "${PIPELINE_TRAFFIC_METRICS_SCHEMA_SHA256} metrics.schema.json" \
    "${PIPELINE_TRAFFIC_ANALYTICS_EVENT_SCHEMA_SHA256} analytics-event.schema.json" \
    "${PIPELINE_TRAFFIC_ANALYTICS_EVENT_EXAMPLE_SHA256} analytics-event.example.json"; do
    read -r expected_digest metadata_name <<<"${metadata_check}"
    [[ -f ${delivery_path}/${metadata_name} && ! -L ${delivery_path}/${metadata_name} ]] || {
      echo "bundled Traffic metadata is missing ${metadata_name}" >&2
      exit 1
    }
    echo "${expected_digest}  ${delivery_path}/${metadata_name}" | sha256sum --check --status || {
      echo "bundled Traffic metadata checksum failed for ${metadata_name}" >&2
      exit 1
    }
  done
  actual_size="$(stat -c '%s' "${ARCHIVE_FILE}")"
  [[ ${actual_size} == "${PIPELINE_TRAFFIC_ARCHIVE_SIZE}" ]] || {
    echo "Traffic archive size is ${actual_size}, expected ${PIPELINE_TRAFFIC_ARCHIVE_SIZE}" >&2
    exit 1
  }
  echo "${PIPELINE_TRAFFIC_ARCHIVE_SHA256}  ${ARCHIVE_FILE}" | sha256sum --check --status || {
    echo "Traffic archive checksum verification failed" >&2
    exit 1
  }
  "${REPO_ROOT}/scripts/tvt-edge-operations.sh" verify-docker-archive-tag \
    --archive "${ARCHIVE_FILE}" --expected "${PIPELINE_TRAFFIC_ARCHIVE_IMAGE}"
  docker_load_container_archive "${ARCHIVE_FILE}"
  source_image="${PIPELINE_TRAFFIC_ARCHIVE_IMAGE}"
else
git_environment=(env GIT_LFS_SKIP_SMUDGE=1 GIT_TERMINAL_PROMPT=0)
if [[ -n "${PIPELINE_GITHUB_TOKEN:-}" ]]; then
  credential_helper="$(mktemp "${WORK_DIR}/git-askpass.XXXXXX")"
  printf '%s\n' '#!/usr/bin/env bash' \
    'case "${1:-}" in' \
    '  *Username*) printf "%s\n" "${PIPELINE_GIT_USERNAME:-x-access-token}" ;;' \
    '  *) printf "%s\n" "${PIPELINE_GITHUB_TOKEN}" ;;' \
    'esac' >"${credential_helper}"
  chmod 0700 "${credential_helper}"
  git_environment+=(GIT_ASKPASS="${credential_helper}")
fi

created_checkout=false
if [[ ! -d "${source_dir}/.git" ]]; then
  [[ ! -e "${source_dir}" ]] || {
    echo "source path exists but is not a Git checkout: ${source_dir}" >&2
    exit 1
  }
  retry_with_timeout "PIPELINE clone" 10m "${git_environment[@]}" \
    git clone --no-checkout --filter=blob:none "${PIPELINE_REPOSITORY}" "${source_dir}"
  created_checkout=true
fi
configured_remote="$(git -C "${source_dir}" remote get-url origin)"
[[ "${configured_remote}" == "${PIPELINE_REPOSITORY}" ]] || {
  echo "PIPELINE checkout origin is ${configured_remote}, expected ${PIPELINE_REPOSITORY}" >&2
  exit 1
}
if [[ "${MODE}" == archive ]]; then
  timeout 30s git -C "${source_dir}" lfs install \
    --local --skip-smudge --skip-repo >/dev/null
fi
retry_with_timeout "PIPELINE pinned revision fetch" 5m "${git_environment[@]}" \
  git -C "${source_dir}" fetch --no-tags origin "${PIPELINE_REVISION}"
if [[ "${created_checkout}" == true ]]; then
  timeout 10m env GIT_LFS_SKIP_SMUDGE=1 \
    git -C "${source_dir}" checkout --detach "${PIPELINE_REVISION}"
fi
actual_revision="$(git -C "${source_dir}" rev-parse HEAD)"
[[ "${actual_revision}" == "${PIPELINE_REVISION}" ]] || {
  echo "existing PIPELINE checkout is ${actual_revision}, expected ${PIPELINE_REVISION}" >&2
  echo "use a different --work-dir rather than rewriting the checkout" >&2
  exit 1
}
[[ -z "$(git -C "${source_dir}" status --porcelain --untracked-files=no)" ]] || {
  echo "PIPELINE source checkout has tracked modifications: ${source_dir}" >&2
  exit 1
}

delivery_path="${source_dir}/${PIPELINE_TRAFFIC_DELIVERY_DIR}"
echo "${PIPELINE_TRAFFIC_CONTRACT_SHA256}  ${delivery_path}/image-contract.yaml" \
  | sha256sum --check --status || {
  echo "PIPELINE image contract checksum verification failed" >&2
  exit 1
}
echo "${PIPELINE_TRAFFIC_DESIRED_STATE_SCHEMA_SHA256}  ${delivery_path}/desired-state.schema.json" \
  | sha256sum --check --status || {
  echo "PIPELINE desired-state schema checksum verification failed" >&2
  exit 1
}

source_image=""
if [[ "${MODE}" == archive ]]; then
  echo "Downloading and verifying ${PIPELINE_TRAFFIC_ARCHIVE}."
  archive_relative="${PIPELINE_TRAFFIC_DELIVERY_DIR}/${PIPELINE_TRAFFIC_ARCHIVE}"
  retry_with_timeout "Traffic image archive download" 30m "${git_environment[@]}" \
    git -C "${source_dir}" lfs pull --include="${archive_relative}" \
    --exclude='' origin "${PIPELINE_REVISION}"
  archive_path="${source_dir}/${archive_relative}"
  [[ -f "${archive_path}" ]] || { echo "missing Traffic archive: ${archive_path}" >&2; exit 1; }
  actual_size="$(stat -c '%s' "${archive_path}")"
  [[ "${actual_size}" == "${PIPELINE_TRAFFIC_ARCHIVE_SIZE}" ]] || {
    echo "Traffic archive size is ${actual_size}, expected ${PIPELINE_TRAFFIC_ARCHIVE_SIZE}" >&2
    exit 1
  }
  echo "${PIPELINE_TRAFFIC_ARCHIVE_SHA256}  ${archive_path}" | sha256sum --check --status || {
    echo "Traffic archive checksum verification failed" >&2
    exit 1
  }
  "${REPO_ROOT}/scripts/tvt-edge-operations.sh" verify-docker-archive-tag \
    --archive "${archive_path}" --expected "${PIPELINE_TRAFFIC_ARCHIVE_IMAGE}"
  docker_load_container_archive "${archive_path}"
  source_image="${PIPELINE_TRAFFIC_ARCHIVE_IMAGE}"
else
  echo "qualification mode: building from pinned source; this is not the production synchronization path" >&2
  temporary_context="$(mktemp -d "${WORK_DIR}/context.XXXXXX")"
  git -C "${source_dir}" archive "${PIPELINE_REVISION}" \
    requirements.txt docker/Dockerfile.base docker/Dockerfile.traffic edge_runtime models/traffic \
    | tar -x -C "${temporary_context}"
  base_image="pipeline-ubuntu-python:tvt-${PIPELINE_REVISION:0:12}"
  source_image="traffic-edge-runtime:pipeline-${PIPELINE_REVISION:0:12}"
  sed "1cFROM ${PIPELINE_UBUNTU_BASE_IMAGE}" \
    "${temporary_context}/docker/Dockerfile.base" \
    >"${temporary_context}/docker/Dockerfile.base.tvt"
  sed "1cFROM ${base_image}" \
    "${temporary_context}/docker/Dockerfile.traffic" \
    >"${temporary_context}/docker/Dockerfile.traffic.tvt"
  retry_with_timeout "PIPELINE base image build" 45m docker build \
    --pull=false --platform linux/amd64 \
    --build-arg "INTEL_NPU_DRIVER_VERSION=${PIPELINE_INTEL_NPU_DRIVER_VERSION}" \
    -f "${temporary_context}/docker/Dockerfile.base.tvt" \
    -t "${base_image}" "${temporary_context}"
  retry_with_timeout "PIPELINE Traffic image build" 30m docker build \
    --pull=false --platform linux/amd64 \
    --build-arg "IMAGE_VERSION=${PIPELINE_TRAFFIC_VERSION}" \
    -f "${temporary_context}/docker/Dockerfile.traffic.tvt" \
    -t "${source_image}" "${temporary_context}"
fi
fi

# The vendor TVT Mills image consumes /configs/desired_state.json directly,
# while the frozen ApexFabric V1 renderer first invokes
# edge_runtime.agent.edge_agent in an init container. Add a TVT-owned,
# checksum-pinned compatibility module as a derived layer. This keeps the
# reference renderer untouched and does not copy camera Secret contents into
# the image or the generated plan receipt.
compatibility_source=""
for candidate in \
  "${REPO_ROOT}/tvt_runtime/image_compat/edge_runtime/agent/edge_agent.py" \
  "/opt/tvt/venv/lib/python3.12/site-packages/tvt_runtime/image_compat/edge_runtime/agent/edge_agent.py"; do
  if [[ -f ${candidate} && ! -L ${candidate} ]]; then
    compatibility_source="${candidate}"
    break
  fi
done
[[ -n ${compatibility_source} ]] || {
  echo "TVT Mills plan-compiler compatibility module is unavailable" >&2
  exit 1
}
echo "${PIPELINE_TRAFFIC_PLAN_COMPILER_SHA256}  ${compatibility_source}" \
  | sha256sum --check --status || {
  echo "TVT Mills plan-compiler compatibility checksum failed" >&2
  exit 1
}
compatibility_context="$(mktemp -d "${WORK_DIR}/plan-compiler.XXXXXX")"
install -d -m 0755 "${compatibility_context}/edge_runtime/agent"
install -m 0644 /dev/null "${compatibility_context}/edge_runtime/__init__.py"
install -m 0644 /dev/null "${compatibility_context}/edge_runtime/agent/__init__.py"
install -m 0644 "${compatibility_source}" \
  "${compatibility_context}/edge_runtime/agent/edge_agent.py"
printf '%s\n' \
  'ARG BASE_IMAGE' \
  'FROM ${BASE_IMAGE}' \
  'COPY --chown=10001:10001 edge_runtime /opt/tvt/edge_runtime' \
  "LABEL io.tvt.plan-compiler.compatibility=${PIPELINE_TRAFFIC_PLAN_COMPILER_COMPATIBILITY_ID}" \
  >"${compatibility_context}/Dockerfile"
compatible_image="tvt-mills-pilot-compat:${PIPELINE_TRAFFIC_VERSION}-${PIPELINE_TRAFFIC_PLAN_COMPILER_SHA256:0:12}"
retry_with_timeout "TVT Mills compatibility image build" 10m docker build \
  --pull=false --network=none --platform linux/amd64 \
  --build-arg "BASE_IMAGE=${source_image}" \
  -t "${compatible_image}" "${compatibility_context}"
source_image="${compatible_image}"

verification_dir="$(mktemp -d "${WORK_DIR}/verify.XXXXXX")"
echo "Inspecting the stopped TVT Mills solution image, compatibility module, and baked models."
timeout 1m docker image inspect "${source_image}" \
  >"${verification_dir}/image-inspect.json"
"${REPO_ROOT}/scripts/tvt-edge-operations.sh" verify-pipeline-image-inspect \
  "${verification_dir}/image-inspect.json" \
  --source "${PIPELINE_TRAFFIC_OCI_SOURCE}" \
  --title "${PIPELINE_TRAFFIC_OCI_TITLE}" \
  --version "${PIPELINE_TRAFFIC_VERSION}" \
  --contract-version "${PIPELINE_TRAFFIC_CONTRACT_VERSION}" \
  --hardware-profile "${PIPELINE_TRAFFIC_HARDWARE_PROFILE}" \
  --models-delivery "${PIPELINE_TRAFFIC_MODELS_DELIVERY}" \
  --user "${PIPELINE_TRAFFIC_CONTAINER_USER}" \
  --port "${PIPELINE_TRAFFIC_CONTAINER_PORT}" \
  --command "${PIPELINE_TRAFFIC_CONTAINER_COMMAND}"

mkdir -p "${verification_dir}/models/traffic" "${verification_dir}/models/face" \
  "${verification_dir}/modules"
verification_container="$(timeout 1m docker create "${source_image}")"
timeout 10m docker cp "${verification_container}:/models/traffic/openvino/." \
  "${verification_dir}/models/traffic"
timeout 10m docker cp "${verification_container}:/models/face/." \
  "${verification_dir}/models/face"
timeout 1m docker cp "${verification_container}:/opt/tvt/edge/__main__.py" \
  "${verification_dir}/modules/edge-main.py"
timeout 1m docker cp \
  "${verification_container}:/opt/tvt/edge_runtime/agent/edge_agent.py" \
  "${verification_dir}/modules/edge-agent.py"
while read -r model_file model_sha256; do
  [[ -s "${verification_dir}/models/traffic/${model_file}" ]] || {
    echo "Traffic image is missing baked model ${model_file}" >&2
    exit 1
  }
  echo "${model_sha256}  ${verification_dir}/models/traffic/${model_file}" \
    | sha256sum --check --status || {
    echo "baked model checksum verification failed for ${model_file}" >&2
    exit 1
  }
done <<EOF
vehicle.xml ${PIPELINE_TRAFFIC_MODEL_VEHICLE_XML_SHA256}
vehicle.bin ${PIPELINE_TRAFFIC_MODEL_VEHICLE_BIN_SHA256}
license_plate.xml ${PIPELINE_TRAFFIC_MODEL_LICENSE_PLATE_XML_SHA256}
license_plate.bin ${PIPELINE_TRAFFIC_MODEL_LICENSE_PLATE_BIN_SHA256}
ocr.xml ${PIPELINE_TRAFFIC_MODEL_OCR_XML_SHA256}
ocr.bin ${PIPELINE_TRAFFIC_MODEL_OCR_BIN_SHA256}
EOF
while read -r model_file model_sha256; do
  [[ -s "${verification_dir}/models/face/${model_file}" ]] || {
    echo "TVT Mills image is missing baked face model ${model_file}" >&2
    exit 1
  }
  echo "${model_sha256}  ${verification_dir}/models/face/${model_file}" \
    | sha256sum --check --status || {
    echo "baked face model checksum verification failed for ${model_file}" >&2
    exit 1
  }
done <<EOF
adaface_ir101_int8.xml ${PIPELINE_FACE_MODEL_ADAFACE_XML_SHA256}
adaface_ir101_int8.bin ${PIPELINE_FACE_MODEL_ADAFACE_BIN_SHA256}
face_reid/models/buffalo_s/det_500m.onnx ${PIPELINE_FACE_MODEL_DETECTOR_SHA256}
face_reid/models/buffalo_s/1k3d68.onnx ${PIPELINE_FACE_MODEL_RECOGNIZER_SHA256}
EOF
[[ -s "${verification_dir}/modules/edge-main.py" ]] || {
  echo "TVT Mills image is missing its Python runtime entrypoint" >&2
  exit 1
}
echo "${PIPELINE_TRAFFIC_PLAN_COMPILER_SHA256}  ${verification_dir}/modules/edge-agent.py" \
  | sha256sum --check --status || {
  echo "TVT Mills image has the wrong plan-compiler compatibility module" >&2
  exit 1
}
timeout 30s docker rm "${verification_container}" >/dev/null
verification_container=""

# Start only the bounded compiler help path. This proves that Python can import
# the module from the final derived image without starting camera or inference
# processing.
verification_container="$(timeout 1m docker create --network none \
  --entrypoint python "${source_image}" \
  -m edge_runtime.agent.edge_agent --help)"
timeout 1m docker start --attach "${verification_container}" >/dev/null
timeout 30s docker rm "${verification_container}" >/dev/null
verification_container=""

echo "Pushing verified v4 image to ${local_image}."
timeout 1m docker tag "${source_image}" "${local_image}"
retry_with_timeout "edge-local Traffic image push" 15m docker push "${local_image}"
local_digest="$(registry_manifest_digest \
  "${PIPELINE_TRAFFIC_LOCAL_REPOSITORY}" "${selected_local_tag}")"
[[ "${local_digest}" =~ ^sha256:[0-9a-f]{64}$ ]] || {
  echo "local registry did not return a verified Traffic image digest" >&2
  exit 1
}
immutable_image="${LOCAL_REGISTRY_ADDRESS}/${PIPELINE_TRAFFIC_LOCAL_REPOSITORY}@${local_digest}"

temporary_lock="$(mktemp "$(dirname "${LOCK_OUTPUT}")/.traffic-image.lock.XXXXXX")"
trap 'rm -f "${temporary_lock}"; cleanup' EXIT
python3 - "${temporary_lock}" "${PIPELINE_TRAFFIC_CATALOG_ID}" \
  "${PIPELINE_REPOSITORY}" "${PIPELINE_REVISION}" \
  "${PIPELINE_TRAFFIC_DELIVERY_DIR}" "${PIPELINE_TRAFFIC_ARCHIVE}" \
  "${PIPELINE_TRAFFIC_ARCHIVE_SHA256}" "${PIPELINE_TRAFFIC_ARCHIVE_SIZE}" \
  "${SOURCE_MODE}" "${LOCAL_REGISTRY_ADDRESS}" "${PIPELINE_TRAFFIC_LOCAL_REPOSITORY}" \
  "${selected_local_tag}" "${local_digest}" "${immutable_image}" \
  "${PIPELINE_TRAFFIC_CONTRACT_SHA256}" \
  "${PIPELINE_TRAFFIC_DESIRED_STATE_SCHEMA_SHA256}" \
  "${PIPELINE_TRAFFIC_METRICS_SCHEMA_SHA256}" \
  "${PIPELINE_TRAFFIC_ANALYTICS_EVENT_SCHEMA_SHA256}" \
  "${PIPELINE_TRAFFIC_ANALYTICS_EVENT_EXAMPLE_SHA256}" \
  "${PIPELINE_TRAFFIC_PLAN_COMPILER_COMPATIBILITY_ID}" \
  "${PIPELINE_TRAFFIC_PLAN_COMPILER_SHA256}" <<'PY'
import datetime
import json
import sys
from pathlib import Path

output = Path(sys.argv[1])
document = {
    "format_version": 2,
    "catalog_id": sys.argv[2],
    "pipeline": {
        "repository": sys.argv[3],
        "commit": sys.argv[4],
        "delivery_directory": sys.argv[5],
    },
    "archive": {
        "filename": sys.argv[6],
        "sha256": sys.argv[7],
        "size": int(sys.argv[8]),
    },
    "source": {"mode": sys.argv[9]},
    "image": {
        "registry": sys.argv[10],
        "repository": sys.argv[11],
        "tag": sys.argv[12],
        "digest": sys.argv[13],
        "reference": sys.argv[14],
        "architecture": "amd64",
    },
    "metadata": {
        "image_contract_sha256": sys.argv[15],
        "desired_state_schema_sha256": sys.argv[16],
        "metrics_schema_sha256": sys.argv[17],
        "analytics_event_schema_sha256": sys.argv[18],
        "analytics_event_example_sha256": sys.argv[19],
    },
    "compatibility": {
        "plan_compiler": sys.argv[20],
        "sha256": sys.argv[21],
    },
    "verification_timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
}
output.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
chmod 0600 "${temporary_lock}"
mv -f "${temporary_lock}" "${LOCK_OUTPUT}"
trap cleanup EXIT

echo "Imported pinned PIPELINE Traffic image."
echo "Catalog ID: ${PIPELINE_TRAFFIC_CATALOG_ID}"
echo "Source revision: ${PIPELINE_REVISION}"
echo "Immutable image: ${immutable_image}"
echo "Lock: ${LOCK_OUTPUT}"

)

tvt_op_install_k3s_plane() (
# Source: scripts/install-k3s-plane.sh
set -Eeuo pipefail

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"
IMAGE_LOCK=""

usage() {
  echo "usage: scripts/tvt-edge-operations.sh install-k3s-plane --image-lock FILE" >&2
}

while (($#)); do
  case "$1" in
    --image-lock) IMAGE_LOCK="${2:-}"; shift 2 ;;
    *) usage; exit 2 ;;
  esac
done
[[ -f "${IMAGE_LOCK}" ]] || { usage; exit 2; }

SUDO=()
if [[ ${EUID} -ne 0 ]]; then
  command -v sudo >/dev/null 2>&1 || { echo "run as root or install sudo" >&2; exit 1; }
  SUDO=(sudo)
fi
command -v k3s >/dev/null 2>&1 || { echo "K3s is not installed" >&2; exit 1; }

mapfile -t nodes < <("${SUDO[@]}" k3s kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')
if [[ ${#nodes[@]} -ne 1 || -z "${nodes[0]}" ]]; then
  echo "TVT requires exactly one registered K3s node; found ${#nodes[@]}" >&2
  exit 1
fi

rendered="$(mktemp)"
trap 'rm -f "${rendered}"' EXIT
python3 -m tvt_runtime.image_lock render \
  --lock "${IMAGE_LOCK}" \
  --template "${REPO_ROOT}/deploy/k8s/apexfabric-node-management.yaml" \
  --output "${rendered}"

"${SUDO[@]}" k3s kubectl label node "${nodes[0]}" --overwrite \
  apexfabric.com/control-plane=true \
  apexfabric.com/node-reporter-enabled=true \
  apexfabric.com/hardware-profile=intel-285h
"${SUDO[@]}" k3s kubectl apply -f "${REPO_ROOT}/deploy/k8s/apexfabric-foundation.yaml"
"${SUDO[@]}" k3s kubectl apply -f "${rendered}"
"${REPO_ROOT}/scripts/tvt-edge-operations.sh" verify-k3s-plane

)

tvt_op_install_apexfabric_ui() (
# Source: scripts/install-apexfabric-ui.sh
#
# Opt-in add-on: the ApexFabric dashboard and its k3s-native control plane
# (SQLite-backed catalog/telemetry/alert-rules) run alongside TVT's own
# Postgres-backed management plane, not in place of it. Alert rules and
# telemetry are tracked independently in each; this is a known V1 limitation,
# not an oversight. Run this after install-k3s-plane and install-tvt-kubeconfig.
set -Eeuo pipefail

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"
UI_IMAGE_LOCK=""

usage() {
  echo "usage: scripts/tvt-edge-operations.sh install-apexfabric-ui --image-lock FILE" >&2
}

while (($#)); do
  case "$1" in
    --image-lock) UI_IMAGE_LOCK="${2:-}"; shift 2 ;;
    *) usage; exit 2 ;;
  esac
done
[[ -f "${UI_IMAGE_LOCK}" ]] || { usage; exit 2; }
[[ ${EUID} -eq 0 ]] || { echo "run as root" >&2; exit 1; }
command -v k3s >/dev/null 2>&1 || { echo "K3s is not installed" >&2; exit 1; }
command -v openssl >/dev/null 2>&1 || { echo "openssl is required" >&2; exit 1; }
getent group tvt-edge >/dev/null || { echo "the tvt-edge group is not installed; run bootstrap-postgresql first" >&2; exit 1; }
[[ -f /etc/tvt/kubeconfig ]] || { echo "/etc/tvt/kubeconfig is missing; run install-tvt-kubeconfig first" >&2; exit 1; }
k3s kubectl get namespace apexfabric >/dev/null 2>&1 || {
  echo "the apexfabric namespace is missing; run install-k3s-plane first" >&2
  exit 1
}

# Camera snapshots (POST /api/cameras/snapshot) shell out to ffmpeg on the
# control-plane host; only this opt-in dashboard needs it, so install it here
# rather than in every base install.
command -v ffmpeg >/dev/null 2>&1 || apt-get install -y ffmpeg

# deploy/k8s/apexfabric-foundation.yaml is a frozen, exact copy (AGENTS.md §6)
# and must not be edited to fix a TVT problem. It never grants the node-agent
# ServiceAccount get on pods/proxy, so the telemetry supervisor's SSE proxy to
# a workload's /events endpoint is always rejected with 403 Forbidden (the
# collector swallows this and retries forever, silently). Grant it via a
# separate, additive Role/RoleBinding instead of touching the frozen manifest.
k3s kubectl apply -f - <<'YAML'
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata: {name: tvt-node-agent-proxy-reader, namespace: apexfabric}
rules:
  - apiGroups: [""]
    resources: ["pods/proxy"]
    verbs: ["get"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata: {name: tvt-node-agent-proxy-reader, namespace: apexfabric}
subjects:
  - {kind: ServiceAccount, name: node-agent, namespace: apexfabric}
roleRef: {kind: Role, name: tvt-node-agent-proxy-reader, apiGroup: rbac.authorization.k8s.io}
YAML

# The /apexfabricdashboard Cluster page reads apexfabric-control's
# /api/status, which lists ReplicaSets, Events and ApexNodeStatuses as the
# node-agent ServiceAccount. The frozen foundation manifest never grants those
# reads, so the tabs render empty. Grant read-only access additively, same as
# above.
k3s kubectl apply -f - <<'YAML'
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata: {name: tvt-node-agent-cluster-view, namespace: apexfabric}
rules:
  - apiGroups: ["apps"]
    resources: ["replicasets"]
    verbs: ["get", "list"]
  - apiGroups: [""]
    resources: ["events"]
    verbs: ["get", "list"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata: {name: tvt-node-agent-cluster-view, namespace: apexfabric}
subjects:
  - {kind: ServiceAccount, name: node-agent, namespace: apexfabric}
roleRef: {kind: Role, name: tvt-node-agent-cluster-view, apiGroup: rbac.authorization.k8s.io}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata: {name: tvt-node-agent-node-status-reader}
rules:
  - apiGroups: ["apexfabric.com"]
    resources: ["apexnodestatuses"]
    verbs: ["get", "list"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata: {name: tvt-node-agent-node-status-reader}
subjects:
  - {kind: ServiceAccount, name: node-agent, namespace: apexfabric}
roleRef: {kind: ClusterRole, name: tvt-node-agent-node-status-reader, apiGroup: rbac.authorization.k8s.io}
YAML

install -o root -g root -m 0644 \
  "${REPO_ROOT}/deploy/systemd/apexfabric-control.service" \
  /etc/systemd/system/apexfabric-control.service
systemctl daemon-reload
systemctl enable --now apexfabric-control.service

ready=false
for _attempt in {1..30}; do
  if curl --fail --silent --show-error --max-time 2 http://127.0.0.1:8088/api/status >/dev/null 2>&1; then
    ready=true
    break
  fi
  sleep 1
done
"${ready}" || { echo "apexfabric-control.service did not become ready on 127.0.0.1:8088" >&2; exit 1; }

if ! k3s kubectl get secret apexfabric-ui-admin-auth -n apexfabric >/dev/null 2>&1; then
  # APEXFABRIC_UI_ADMIN_PASSWORD lets an operator pin a known password
  # instead of the generated-per-install random one (e.g. to match a site's
  # existing device-admin password). Never hardcode a real password value
  # here; pass it as an environment variable on the host at install time.
  password="${APEXFABRIC_UI_ADMIN_PASSWORD:-$(openssl rand -base64 24)}"
  temporary_htpasswd="$(mktemp)"
  trap 'rm -f "${temporary_htpasswd}"' EXIT
  printf 'admin:%s\n' "$(openssl passwd -apr1 "${password}")" >"${temporary_htpasswd}"
  k3s kubectl create secret generic apexfabric-ui-admin-auth \
    --from-file=admin.htpasswd="${temporary_htpasswd}" -n apexfabric
  rm -f "${temporary_htpasswd}"
  trap - EXIT
  # /etc/tvt is shared with tvt-camera-sync.service and apexfabric-control.service,
  # both of which run as a non-root user and need to traverse into it to reach their
  # own tighter-permissioned files (e.g. kubeconfig, this password file). install -d
  # re-chmods an already-existing directory, so 0700 here would silently break their
  # traversal; match install-tvt-kubeconfig's 0755 instead.
  install -d -o root -g root -m 0755 /etc/tvt
  temporary_password="$(mktemp /etc/tvt/.apexfabric-ui-admin-password.XXXXXX)"
  printf '%s\n' "${password}" >"${temporary_password}"
  chmod 0600 "${temporary_password}"
  mv -f "${temporary_password}" /etc/tvt/apexfabric-ui-admin-password
  if [[ -n "${APEXFABRIC_UI_ADMIN_PASSWORD:-}" ]]; then
    echo "Set the apexfabricdashboard admin password from APEXFABRIC_UI_ADMIN_PASSWORD (saved to /etc/tvt/apexfabric-ui-admin-password)."
  else
    echo "Generated the apexfabricdashboard admin password (saved to /etc/tvt/apexfabric-ui-admin-password):"
    echo "${password}"
  fi
  unset password
fi

rendered="$(mktemp)"
trap 'rm -f "${rendered}"' EXIT
python3 -m tvt_runtime.ui_image render \
  --lock "${UI_IMAGE_LOCK}" \
  --template "${REPO_ROOT}/deploy/single-box/ui.yaml" \
  --output "${rendered}"
k3s kubectl apply -f "${rendered}"
k3s kubectl rollout status deployment/apexfabric-ui -n apexfabric --timeout=180s

)

tvt_op_install_k3s_single_node() (
# Source: scripts/install-k3s-single-node.sh
set -Eeuo pipefail

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=config/platform.env
source "${REPO_ROOT}/config/platform.env"

INSTALLER=""
K3S_BINARY=""
DOWNLOAD_INSTALLER=false

usage() {
  echo "usage: scripts/tvt-edge-operations.sh install-k3s-single-node (--installer FILE | --download-installer) [--k3s-binary FILE]" >&2
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

if systemctl cat k3s-agent.service >/dev/null 2>&1; then
  echo "an existing K3s agent installation was detected" >&2
  echo "TVT requires this device to be a standalone single-node K3s server" >&2
  echo "remove or migrate the agent through its owner-approved uninstall procedure before continuing" >&2
  exit 1
fi
if command -v k3s >/dev/null 2>&1 && ! systemctl cat k3s.service >/dev/null 2>&1; then
  echo "a K3s binary exists without a K3s server service" >&2
  echo "remove the incomplete or non-server installation before continuing" >&2
  exit 1
fi

if ! systemctl is-active --quiet tvt-local-registry.service; then
  echo "the local registry is not active; run the install-local-registry operation first" >&2
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
"${REPO_ROOT}/scripts/tvt-edge-operations.sh" configure-k3s-registry \
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
    INSTALL_K3S_EXEC="server --secrets-encryption --secrets-encryption-provider=secretbox")
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
nodes_seen=false
for _attempt in {1..60}; do
  if "${SUDO[@]}" k3s kubectl get nodes -o name 2>/dev/null | grep -q '^node/'; then
    nodes_seen=true
    break
  fi
  sleep 2
done
${nodes_seen} || { echo "K3s did not register its node within 120 seconds" >&2; exit 1; }
"${SUDO[@]}" k3s kubectl wait --for=condition=Ready node --all --timeout=180s
mapfile -t nodes < <("${SUDO[@]}" k3s kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')
if [[ ${#nodes[@]} -ne 1 || -z "${nodes[0]}" ]]; then
  echo "TVT requires exactly one registered node; found ${#nodes[@]}" >&2
  exit 1
fi
"${SUDO[@]}" k3s kubectl apply -f "${REPO_ROOT}/deploy/k8s/apexfabric-foundation.yaml"
echo "Pinned single-node K3s foundation is ready on ${nodes[0]}"

)

tvt_op_install_local_registry() (
# Source: scripts/install-local-registry.sh
set -Eeuo pipefail

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=config/platform.env
source "${REPO_ROOT}/config/platform.env"

IMAGE_ARCHIVE=""
usage() {
  echo "usage: sudo scripts/tvt-edge-operations.sh install-local-registry [--image-archive FILE]" >&2
}
while (($#)); do
  case "$1" in
    --image-archive) IMAGE_ARCHIVE="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage; exit 2 ;;
  esac
done
if [[ -n ${IMAGE_ARCHIVE} && (! -f ${IMAGE_ARCHIVE} || -L ${IMAGE_ARCHIVE}) ]]; then
  echo "registry image archive is missing or is a symlink" >&2
  exit 2
fi

retry_with_timeout() {
  local description="$1"
  local duration="$2"
  shift 2
  local attempt
  for attempt in 1 2 3; do
    if timeout --signal=TERM "${duration}" "$@"; then
      return 0
    fi
    if [[ ${attempt} -lt 3 ]]; then
      echo "${description} failed (attempt ${attempt}/3); retrying in 3 seconds" >&2
      sleep 3
    fi
  done
  echo "${description} failed after 3 attempts" >&2
  return 1
}

[[ ${EUID} -eq 0 ]] || { echo "run as root" >&2; exit 1; }
[[ "$(dpkg --print-architecture)" == amd64 ]] || {
  echo "the TVT local registry image is pinned for amd64" >&2
  exit 1
}
# shellcheck disable=SC1091
source /etc/os-release
[[ "${ID:-}" == ubuntu && "${VERSION_ID:-}" == 24.04 ]] || {
  echo "the TVT edge registry installer requires Ubuntu 24.04" >&2
  exit 1
}
for executable in /usr/bin/curl /usr/bin/docker /usr/bin/install /usr/bin/sed /usr/bin/systemctl /usr/bin/timeout; do
  [[ -x "${executable}" ]] || { echo "required executable not found: ${executable}" >&2; exit 1; }
done
[[ "${LOCAL_REGISTRY_IMAGE}" =~ ^[^[:space:]@]+:[^[:space:]@]+@sha256:[0-9a-f]{64}$ ]] || {
  echo "LOCAL_REGISTRY_IMAGE must include a tag and sha256 digest" >&2
  exit 1
}

systemctl enable --now docker.service
retry_with_timeout "Docker daemon readiness" 15s docker info >/dev/null
RUNTIME_REGISTRY_IMAGE="${LOCAL_REGISTRY_IMAGE}"
if [[ -n ${IMAGE_ARCHIVE} ]]; then
  timeout --signal=TERM 10m docker load --input "${IMAGE_ARCHIVE}"
  loaded_registry_tag="${LOCAL_REGISTRY_IMAGE%%@*}"
  RUNTIME_REGISTRY_IMAGE="$(docker image inspect --format '{{.Id}}' "${loaded_registry_tag}")"
  [[ ${RUNTIME_REGISTRY_IMAGE} =~ ^sha256:[0-9a-f]{64}$ ]] || {
    echo "loaded registry archive did not produce an immutable image ID" >&2
    exit 1
  }
else
  retry_with_timeout "pinned registry image pull" 5m \
    docker pull --platform linux/amd64 "${LOCAL_REGISTRY_IMAGE}"
fi

registry_architecture="$(docker image inspect --format '{{.Architecture}}' "${RUNTIME_REGISTRY_IMAGE}")"
[[ "${registry_architecture}" == amd64 ]] || {
  echo "pinned registry image architecture is ${registry_architecture}, not amd64" >&2
  exit 1
}
if [[ -z ${IMAGE_ARCHIVE} ]]; then
  expected_digest="${LOCAL_REGISTRY_IMAGE##*@}"
  docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' \
    "${LOCAL_REGISTRY_IMAGE}" | grep -Fq "@${expected_digest}" || {
    echo "Docker did not retain the expected registry image digest ${expected_digest}" >&2
    exit 1
  }
fi

install -d -o root -g root -m 0750 "${LOCAL_REGISTRY_DATA_DIR}"
rendered_unit="$(mktemp)"
trap 'rm -f "${rendered_unit}"' EXIT
sed "s|__TVT_LOCAL_REGISTRY_IMAGE__|${RUNTIME_REGISTRY_IMAGE}|g" \
  "${REPO_ROOT}/deploy/systemd/tvt-local-registry.service.in" >"${rendered_unit}"
install -o root -g root -m 0644 "${rendered_unit}" \
  /etc/systemd/system/tvt-local-registry.service
install -d -o root -g root -m 0755 /etc/systemd/system/k3s.service.d
install -o root -g root -m 0644 \
  "${REPO_ROOT}/deploy/systemd/k3s-tvt-local-registry.conf" \
  /etc/systemd/system/k3s.service.d/20-tvt-local-registry.conf

systemctl daemon-reload
systemctl enable tvt-local-registry.service
systemctl restart tvt-local-registry.service
systemctl is-active --quiet tvt-local-registry.service

restart_k3s=()
if systemctl is-active --quiet k3s.service; then
  restart_k3s=(--restart)
fi
"${REPO_ROOT}/scripts/tvt-edge-operations.sh" configure-k3s-registry \
  --registry "${LOCAL_REGISTRY_ADDRESS}" --scheme http "${restart_k3s[@]}"

curl --fail --silent --show-error --max-time 5 \
  "http://${LOCAL_REGISTRY_ADDRESS}/v2/" >/dev/null
echo "TVT local registry is ready at http://${LOCAL_REGISTRY_ADDRESS}."
echo "Persistent image data is stored under ${LOCAL_REGISTRY_DATA_DIR}."
if [[ ${#restart_k3s[@]} -eq 0 ]]; then
  echo "K3s is not active; run the install-k3s-single-node operation next."
fi

)

tvt_op_install_pipeline_image_sync() (
# Source: scripts/install-pipeline-image-sync.sh
set -Eeuo pipefail
umask 077

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=config/platform.env
source "${REPO_ROOT}/config/platform.env"

ARCHIVE_FILE=""
METADATA_DIRECTORY=""
usage() {
  echo "usage: sudo scripts/tvt-edge-operations.sh install-pipeline-image-sync [--archive-file FILE --metadata-directory DIR]" >&2
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
install -d -o root -g root -m 0700 /var/lib/tvt/pipeline
# Shared traversal only; credential and environment files remain private below it.
install -d -o root -g root -m 0755 /etc/tvt
install -o root -g root -m 0755 \
  "${REPO_ROOT}/scripts/tvt-edge-operations.sh" \
  /opt/tvt/scripts/tvt-edge-operations.sh
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

)

tvt_op_install_traffic_qualification() (
# Source: scripts/install-traffic-qualification.sh
set -Eeuo pipefail
umask 077

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly PIPELINE_VERSION="$(sed -n 's/^PIPELINE_TRAFFIC_VERSION=//p' "${REPO_ROOT}/config/pipeline.env" | head -1)"
readonly SOURCE_CONTRACTS="${REPO_ROOT}/solution-packs/catalog/tvt-mills-pilot-${PIPELINE_VERSION}"
readonly TARGET_CONTRACTS="/opt/tvt/solution-packs/catalog/tvt-mills-pilot-${PIPELINE_VERSION}"

[[ ${EUID} -eq 0 ]] || { echo "run as root" >&2; exit 1; }
[[ -x /opt/tvt/venv/bin/tvt-traffic-qualify ]] || {
  echo "install the current TVT Python package in /opt/tvt/venv first" >&2
  exit 1
}
[[ -d ${SOURCE_CONTRACTS} ]] || {
  echo "pinned Traffic qualification contracts are missing" >&2
  exit 1
}

install -d -o root -g root -m 0755 /opt/tvt/scripts "${TARGET_CONTRACTS}"
install -d -o root -g root -m 0750 /var/lib/tvt/qualification
install -o root -g root -m 0755 \
  "${REPO_ROOT}/scripts/tvt-edge-operations.sh" \
  /opt/tvt/scripts/tvt-edge-operations.sh
for filename in \
  image-contract.yaml \
  desired-state.schema.json \
  desired-state.example.json \
  metrics.schema.json \
  analytics-event.schema.json \
  analytics-event.example.json \
  provenance.json; do
  install -o root -g root -m 0644 \
    "${SOURCE_CONTRACTS}/${filename}" "${TARGET_CONTRACTS}/${filename}"
done

echo "Installed the manual Traffic edge qualification tools and pinned contracts."
echo "No qualification, deployment, rollback, restart, or reboot was performed."

)

tvt_op_install_tvt_hardware_drivers() (
# Source: scripts/install-tvt-hardware-drivers.sh
set -Eeuo pipefail
umask 022

readonly EXPECTED_OS_ID="ubuntu"
readonly EXPECTED_OS_VERSION="24.04"
readonly EXPECTED_ARCH="amd64"
readonly DRIVER_PPA="ppa:kobuk-team/intel-graphics"
readonly AXELERA_APT_KEY_URL="https://software.axelera.ai/artifactory/api/security/keypair/axelera/public"
readonly AXELERA_APT_KEY_FINGERPRINT="5AE357D1638F21311095816A82F63658F8BBFC11"
readonly AXELERA_APT_KEYRING="/etc/apt/keyrings/axelera.gpg"
readonly AXELERA_APT_SOURCE="/etc/apt/sources.list.d/axelera.list"
readonly METIS_APT_PREFERENCE="/etc/apt/preferences.d/tvt-metis"
readonly AXELERA_APT_REPOSITORY="https://software.axelera.ai/artifactory/axelera-apt-source"
readonly METIS_DKMS_VERSION="1.4.17"
readonly VOYAGER_RUNTIME_VERSION="1.6.1"
readonly METIS_FIRMWARE_RECOMMENDED="1.6.0"
readonly METIS_BOARD_CONTROLLER_RECOMMENDED="7.4"
readonly VOYAGER_PYPI_INDEX="https://software.axelera.ai/artifactory/api/pypi/axelera-pypi/simple"
readonly PCI_SYSFS_ROOT="${TVT_PCI_SYSFS_ROOT:-/sys/bus/pci/devices}"
readonly STATE_DIRECTORY="${TVT_HARDWARE_STATE_DIRECTORY:-/var/lib/tvt}"
readonly CACHE_DIRECTORY="${TVT_HARDWARE_CACHE_DIRECTORY:-/var/cache/tvt/hardware-drivers}"
readonly LOCK_FILE="${STATE_DIRECTORY}/hardware-driver-recipe.json"
readonly VENV_DIRECTORY="${TVT_OPENVINO_VENV_DIRECTORY:-/opt/apexfabric/openvino-env}"
readonly VOYAGER_VENV_DIRECTORY="${TVT_VOYAGER_VENV_DIRECTORY:-/opt/apexfabric/voyager-1.6.1}"
readonly REBOOT_MARKER="${TVT_HARDWARE_REBOOT_MARKER:-${STATE_DIRECTORY}/hardware-driver-reboot-required}"

MODE=online
BUNDLE=""
ALLOW_UNVERIFIED_HARDWARE=false

log() { printf 'tvt-driver-install: %s\n' "$*"; }
fail() { printf 'tvt-driver-install: ERROR: %s\n' "$*" >&2; exit 1; }

usage() {
  echo "usage: sudo scripts/tvt-edge-operations.sh install-tvt-hardware-drivers [--mode online|offline] [--bundle PATH] [--allow-unverified-hardware]" >&2
}

while (($#)); do
  case "$1" in
    --mode) MODE="${2:-}"; shift 2 ;;
    --bundle) BUNDLE="${2:-}"; shift 2 ;;
    --allow-unverified-hardware) ALLOW_UNVERIFIED_HARDWARE=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage; exit 2 ;;
  esac
done
[[ ${MODE} == online || ${MODE} == offline ]] || { usage; exit 2; }
if [[ ${MODE} == offline ]]; then
  [[ -d ${BUNDLE} && ! -L ${BUNDLE} ]] || fail "offline mode requires --bundle PATH"
  BUNDLE="$(cd "${BUNDLE}" && pwd -P)"
fi

APT_PACKAGES=(
  libze-intel-gpu1
  libze1
  intel-opencl-icd
  ocl-icd-libopencl1
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
readonly -a VOYAGER_PYTHON_PACKAGES=("axelera-rt==${VOYAGER_RUNTIME_VERSION}")

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
  [[ ${TVT_ALLOW_UNVERIFIED_HARDWARE:-false} == true || ${ALLOW_UNVERIFIED_HARDWARE} == true ]] || \
    fail "Intel 285H hardware was not detected (set TVT_ALLOW_UNVERIFIED_HARDWARE=true only for an audited equivalent host)"
fi

for module in intel_vpu; do
  modinfo "${module}" >/dev/null 2>&1 || \
    fail "kernel $(uname -r) does not provide ${module}; install the qualified Ubuntu kernel first"
done
if ! modinfo i915 >/dev/null 2>&1 && ! modinfo xe >/dev/null 2>&1; then
  fail "kernel $(uname -r) provides neither the i915 nor xe Intel graphics module"
fi

AXELERA_HARDWARE_PRESENT=false
axelera_pci_address=""
shopt -s nullglob
vendor_files=("${PCI_SYSFS_ROOT}"/*/vendor)
shopt -u nullglob
for vendor_file in "${vendor_files[@]}"; do
  read -r pci_vendor <"${vendor_file}" || continue
  if [[ ${pci_vendor,,} == 0x1f9d ]]; then
    AXELERA_HARDWARE_PRESENT=true
    axelera_pci_address="$(basename "$(dirname "${vendor_file}")")"
    break
  fi
done
if ${AXELERA_HARDWARE_PRESENT}; then
  [[ ! -L ${VOYAGER_VENV_DIRECTORY} ]] || fail "refusing symlinked Voyager environment: ${VOYAGER_VENV_DIRECTORY}"
  [[ ! -L ${METIS_APT_PREFERENCE} ]] || fail "refusing symlinked Metis APT preference: ${METIS_APT_PREFERENCE}"
  log "detected Axelera PCI hardware at ${axelera_pci_address}; enabling Metis/Voyager installation"
  APT_PACKAGES+=(dkms build-essential "linux-headers-$(uname -r)" metis-dkms)
else
  log "no Axelera PCI hardware (vendor 0x1f9d) detected; installing only the Intel accelerator/media stack"
fi
readonly -a APT_PACKAGES

export DEBIAN_FRONTEND=noninteractive
if ${AXELERA_HARDWARE_PRESENT}; then
  install -d -o root -g root -m 0755 /etc/apt/preferences.d
  printf 'Package: metis-dkms\nPin: version %s\nPin-Priority: 1001\n' \
    "${METIS_DKMS_VERSION}" >"${METIS_APT_PREFERENCE}.new"
  chmod 0644 "${METIS_APT_PREFERENCE}.new"
  mv -f -- "${METIS_APT_PREFERENCE}.new" "${METIS_APT_PREFERENCE}"
fi
if [[ ${MODE} == online ]]; then
  apt-get update
  apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg python3 python3-venv software-properties-common

  if ! grep -RqsE '(^|/)kobuk-team/ubuntu.*intel-graphics|ppa\.launchpadcontent\.net/kobuk-team/intel-graphics' \
    /etc/apt/sources.list /etc/apt/sources.list.d 2>/dev/null; then
    log "adding the Intel graphics package source used by k3s-prototype"
    add-apt-repository -y "${DRIVER_PPA}"
  fi

  if ${AXELERA_HARDWARE_PRESENT}; then
    [[ ! -L ${AXELERA_APT_KEYRING} ]] || fail "refusing symlinked Axelera keyring: ${AXELERA_APT_KEYRING}"
    [[ ! -L ${AXELERA_APT_SOURCE} ]] || fail "refusing symlinked Axelera APT source: ${AXELERA_APT_SOURCE}"
    install -d -o root -g root -m 0755 /etc/apt/keyrings
    if [[ -e ${AXELERA_APT_KEYRING} || -e ${AXELERA_APT_SOURCE} ]]; then
      [[ -f ${AXELERA_APT_KEYRING} && -f ${AXELERA_APT_SOURCE} ]] || \
        fail "the existing Axelera APT configuration is incomplete"
      key_fingerprint="$(gpg --show-keys --with-colons "${AXELERA_APT_KEYRING}" | awk -F: '$1 == "fpr" {print $10; exit}')"
      [[ ${key_fingerprint} == "${AXELERA_APT_KEY_FINGERPRINT}" ]] || \
        fail "the existing Axelera key fingerprint is ${key_fingerprint:-missing}; expected ${AXELERA_APT_KEY_FINGERPRINT}"
      grep -Fqs "${AXELERA_APT_REPOSITORY} ubuntu24 main" "${AXELERA_APT_SOURCE}" || \
        fail "the existing Axelera APT source is not the supported Ubuntu 24.04 repository"
    else
      key_download="$(mktemp)"
      curl --fail --location --silent --show-error \
        --proto '=https' --tlsv1.2 --connect-timeout 15 --retry 3 --retry-all-errors \
        --max-time 120 --output "${key_download}" "${AXELERA_APT_KEY_URL}"
      key_fingerprint="$(gpg --show-keys --with-colons "${key_download}" | awk -F: '$1 == "fpr" {print $10; exit}')"
      [[ ${key_fingerprint} == "${AXELERA_APT_KEY_FINGERPRINT}" ]] || {
        rm -f -- "${key_download}"
        fail "Axelera APT signing-key fingerprint is ${key_fingerprint:-missing}; expected ${AXELERA_APT_KEY_FINGERPRINT}"
      }
      gpg --batch --yes --dearmor --output "${AXELERA_APT_KEYRING}.new" "${key_download}"
      rm -f -- "${key_download}"
      chmod 0644 "${AXELERA_APT_KEYRING}.new"
      mv -f -- "${AXELERA_APT_KEYRING}.new" "${AXELERA_APT_KEYRING}"
      printf 'deb [arch=amd64 signed-by=%s] %s ubuntu24 main\n' \
        "${AXELERA_APT_KEYRING}" "${AXELERA_APT_REPOSITORY}" >"${AXELERA_APT_SOURCE}.new"
      chmod 0644 "${AXELERA_APT_SOURCE}.new"
      mv -f -- "${AXELERA_APT_SOURCE}.new" "${AXELERA_APT_SOURCE}"
    fi
  fi
  apt-get update
fi

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
  local voyager_wheels="${work_directory}/voyager-wheels"
  local voyager_sums="${work_directory}/voyager-wheel-sha256.txt"
  : >"${apt_pins}"

  log "resolving exact APT candidates"
  for package in "${APT_PACKAGES[@]}"; do
    if [[ ${package} == metis-dkms ]]; then
      candidate="${METIS_DKMS_VERSION}"
      apt-cache show "${package}=${candidate}" >/dev/null 2>&1 || \
        fail "the Axelera repository does not provide ${package}=${candidate}"
    else
      candidate="$(apt-cache policy "${package}" | awk '$1 == "Candidate:" {print $2; exit}')"
    fi
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

  : >"${voyager_sums}"
  if ${AXELERA_HARDWARE_PRESENT}; then
    log "resolving and caching the Voyager ${VOYAGER_RUNTIME_VERSION} runtime wheel closure"
    install -d -m 0755 "${voyager_wheels}"
    python3 -m venv "${work_directory}/voyager-resolver-venv"
    "${work_directory}/voyager-resolver-venv/bin/python" -m pip download \
      --disable-pip-version-check --no-cache-dir --only-binary=:all: \
      --extra-index-url "${VOYAGER_PYPI_INDEX}" --dest "${voyager_wheels}" \
      "${VOYAGER_PYTHON_PACKAGES[@]}"
    python3 - "${voyager_wheels}" "${VOYAGER_RUNTIME_VERSION}" <<'PY'
import email
import pathlib
import re
import sys
import zipfile

wheels, expected = pathlib.Path(sys.argv[1]), sys.argv[2]
versions = {}
for wheel in wheels.glob("*.whl"):
    with zipfile.ZipFile(wheel) as archive:
        names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(names) != 1:
            raise SystemExit(f"cannot identify metadata in {wheel.name}")
        metadata = email.message_from_bytes(archive.read(names[0]))
    name = re.sub(r"[-_.]+", "-", metadata.get("Name", "")).lower()
    versions[name] = metadata.get("Version", "")
if versions.get("axelera-rt") != expected:
    raise SystemExit(f"pip resolved axelera-rt {versions.get('axelera-rt')!r}; expected {expected!r}")
PY
    (cd "${voyager_wheels}" && sha256sum -- *.whl | sort -k2) >"${voyager_sums}"
  fi

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
  rm -rf -- "${CACHE_DIRECTORY}/voyager-wheels"
  if ${AXELERA_HARDWARE_PRESENT}; then
    install -d -m 0755 "${CACHE_DIRECTORY}/voyager-wheels"
    cp -a "${voyager_wheels}/." "${CACHE_DIRECTORY}/voyager-wheels/"
  fi
  install -m 0644 "${npu_archive}" "${CACHE_DIRECTORY}/linux-npu-driver.tar.gz"

  python3 - "${LOCK_FILE}.new" "${apt_pins}" "${requirements}" "${wheel_sums}" "${voyager_sums}" \
    "${npu_fields[0]:-}" "${npu_fields[1]:-}" "${npu_url}" "${npu_sha256}" \
    "$(uname -r)" "${VOYAGER_RUNTIME_VERSION}" "${METIS_DKMS_VERSION}" \
    "${METIS_FIRMWARE_RECOMMENDED}" "${METIS_BOARD_CONTROLLER_RECOMMENDED}" \
    "${AXELERA_HARDWARE_PRESENT}" <<'PY'
import json
import pathlib
import sys

output, apt_file, requirements_file, sums_file, voyager_sums_file, tag, asset, url, digest, kernel, voyager_version, metis_version, firmware, board_controller, axelera_present = sys.argv[1:]
axelera_enabled = axelera_present == "true"
apt = dict(line.split("=", 1) for line in pathlib.Path(apt_file).read_text().splitlines())
python = dict(line.split("==", 1) for line in pathlib.Path(requirements_file).read_text().splitlines())
wheels = {}
for line in pathlib.Path(sums_file).read_text().splitlines():
    sha256, filename = line.split(maxsplit=1)
    wheels[filename.lstrip("*")] = sha256
voyager_wheels = {}
for line in pathlib.Path(voyager_sums_file).read_text().splitlines():
    sha256, filename = line.split(maxsplit=1)
    voyager_wheels[filename.lstrip("*")] = sha256
recipe = {
    "schema_version": 2,
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
    "voyager": {
        "enabled": axelera_enabled,
        "runtime_version": voyager_version if axelera_enabled else None,
        "driver_package": "metis-dkms" if axelera_enabled else None,
        "driver_version": metis_version if axelera_enabled else None,
        "firmware_recommended": firmware if axelera_enabled else None,
        "board_controller_recommended": board_controller if axelera_enabled else None,
        "wheels": voyager_wheels,
    },
}
pathlib.Path(output).write_text(json.dumps(recipe, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
  chmod 0644 "${LOCK_FILE}.new"
  mv -f -- "${LOCK_FILE}.new" "${LOCK_FILE}"
}

validate_lock_and_cache() {
  python3 - "${LOCK_FILE}" "${CACHE_DIRECTORY}" "$(uname -r)" \
    "${AXELERA_HARDWARE_PRESENT}" <<'PY'
import hashlib
import json
import pathlib
import sys

lock_path, cache_path, kernel = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3]
axelera_detected = sys.argv[4] == "true"
recipe = json.loads(lock_path.read_text(encoding="utf-8"))
expected = {
    "hardware_profile": "intel-285h",
    "os_id": "ubuntu",
    "os_version_id": "24.04",
    "architecture": "amd64",
    "kernel_target": kernel,
}
if recipe.get("schema_version") not in (2, 3):
    raise SystemExit(f"locked schema_version is {recipe.get('schema_version')!r}; expected 2 or 3")
if recipe.get("schema_version") == 2 and "kernel_version" in recipe:
    # Legacy edge-resolved lock: kernel_version carries the target kernel.
    recipe = {**recipe, "kernel_target": recipe["kernel_version"]}
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
voyager = recipe.get("voyager", {})
if not isinstance(voyager.get("enabled"), bool):
    raise SystemExit("locked recipe has no valid Voyager enabled flag")
if voyager["enabled"] != axelera_detected:
    raise SystemExit("locked recipe does not match current Axelera PCI hardware presence; remove the lock and regenerate it")
if axelera_detected:
    if voyager.get("runtime_version") != "1.6.1" or voyager.get("driver_version") != "1.4.17":
        raise SystemExit("locked Voyager/Metis versions do not match Voyager 1.6.1 and metis-dkms 1.4.17")
    if voyager.get("firmware_recommended") != "1.6.0" or voyager.get("board_controller_recommended") != "7.4":
        raise SystemExit("locked Metis firmware compatibility recommendations are invalid")
    if recipe.get("apt", {}).get("metis-dkms") != voyager.get("driver_version"):
        raise SystemExit("locked metis-dkms APT version does not match the Voyager compatibility pin")
    if not voyager.get("wheels"):
        raise SystemExit("locked Voyager runtime has no wheel closure")
    for filename, expected_digest in voyager["wheels"].items():
        wheel = cache_path / "voyager-wheels" / filename
        if not wheel.is_file() or sha256(wheel) != expected_digest:
            raise SystemExit(f"cached Voyager wheel is missing or does not match the lock: {filename}")
else:
    if "metis-dkms" in recipe.get("apt", {}):
        raise SystemExit("Intel-only recipe unexpectedly pins metis-dkms")
    if voyager.get("wheels") != {} or any(voyager.get(key) is not None for key in (
        "runtime_version", "driver_package", "driver_version",
        "firmware_recommended", "board_controller_recommended",
    )):
        raise SystemExit("Intel-only recipe unexpectedly contains a Voyager/Metis closure")
PY
}

if [[ ! -f ${LOCK_FILE} ]]; then
  if [[ ${MODE} == offline ]]; then
    offline_hardware="${BUNDLE}/hardware"
    [[ -f ${offline_hardware}/driver-recipe.json ]] || \
      fail "offline driver recipe is missing"
    [[ -f ${offline_hardware}/linux-npu-driver.tar.gz ]] || \
      fail "offline Intel NPU archive is missing"
    [[ -d ${offline_hardware}/wheels ]] || fail "offline OpenVINO wheels are missing"
    install -m 0644 "${offline_hardware}/driver-recipe.json" "${LOCK_FILE}"
    install -m 0644 "${offline_hardware}/linux-npu-driver.tar.gz" \
      "${CACHE_DIRECTORY}/linux-npu-driver.tar.gz"
    install -d -m 0755 "${CACHE_DIRECTORY}/wheels"
    cp -a "${offline_hardware}/wheels/." "${CACHE_DIRECTORY}/wheels/"
    rm -rf -- "${CACHE_DIRECTORY}/voyager-wheels"
    if ${AXELERA_HARDWARE_PRESENT}; then
      [[ -d ${offline_hardware}/voyager-wheels ]] || fail "offline Voyager runtime wheels are missing"
      install -d -m 0755 "${CACHE_DIRECTORY}/voyager-wheels"
      cp -a "${offline_hardware}/voyager-wheels/." "${CACHE_DIRECTORY}/voyager-wheels/"
    fi
  else
    resolve_recipe
  fi
else
  log "reusing locked recipe ${LOCK_FILE}"
  if [[ ${MODE} == offline ]]; then
    cmp -s "${BUNDLE}/hardware/driver-recipe.json" "${LOCK_FILE}" || \
      fail "installed driver recipe does not match the offline release bundle"
  fi
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
if [[ ${MODE} == offline ]]; then
  for pin in "${apt_pins[@]}"; do
    package="${pin%%=*}"
    expected="${pin#*=}"
    actual="$(dpkg-query -W -f='${Version}' "${package}" 2>/dev/null || true)"
    [[ ${actual} == "${expected}" ]] || \
      fail "offline package ${package} is ${actual:-missing}; expected ${expected}"
  done
else
  apt-get install -y --no-install-recommends --allow-downgrades "${apt_pins[@]}"
fi

if ${AXELERA_HARDWARE_PRESENT}; then
  actual_metis_version="$(dpkg-query -W -f='${Version}' metis-dkms 2>/dev/null || true)"
  [[ ${actual_metis_version} == "${METIS_DKMS_VERSION}" ]] || \
    fail "metis-dkms is ${actual_metis_version:-missing}; expected ${METIS_DKMS_VERSION} for Voyager ${VOYAGER_RUNTIME_VERSION}"
  if ! dkms status -m metis -v "${METIS_DKMS_VERSION}" -k "$(uname -r)" 2>/dev/null | grep -Eq ': installed$'; then
    log "completing the Metis DKMS build for kernel $(uname -r)"
    dkms autoinstall -k "$(uname -r)"
  fi
  dkms status -m metis -v "${METIS_DKMS_VERSION}" -k "$(uname -r)" 2>/dev/null | grep -Eq ': installed$' || \
    fail "Metis DKMS ${METIS_DKMS_VERSION} is not installed for kernel $(uname -r)"
  modinfo -k "$(uname -r)" metis >/dev/null 2>&1 || \
    fail "the Metis module is unavailable for kernel $(uname -r)"
fi

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

if ${AXELERA_HARDWARE_PRESENT}; then
  log "installing the locked Voyager ${VOYAGER_RUNTIME_VERSION} runtime"
  voyager_ready=false
  if [[ -x ${VOYAGER_VENV_DIRECTORY}/bin/python ]] && \
    "${VOYAGER_VENV_DIRECTORY}/bin/python" - "${VOYAGER_RUNTIME_VERSION}" <<'PY'
import importlib.metadata
import sys
raise SystemExit(importlib.metadata.version("axelera-rt") != sys.argv[1])
PY
  then
    if "${VOYAGER_VENV_DIRECTORY}/bin/python" -m pip check >/dev/null 2>&1; then
      voyager_ready=true
    fi
  fi
  if ! ${voyager_ready}; then
    install -d -m 0755 "$(dirname "${VOYAGER_VENV_DIRECTORY}")"
    python3 -m venv --clear "${VOYAGER_VENV_DIRECTORY}"
    "${VOYAGER_VENV_DIRECTORY}/bin/python" -m pip install --disable-pip-version-check \
      --no-index --find-links="${CACHE_DIRECTORY}/voyager-wheels" \
      "${VOYAGER_PYTHON_PACKAGES[@]}"
  fi
  "${VOYAGER_VENV_DIRECTORY}/bin/python" -m pip check
  chmod -R go+rX "${VOYAGER_VENV_DIRECTORY}"
fi

modprobe i915 2>/dev/null || modprobe xe 2>/dev/null || true
modprobe intel_vpu 2>/dev/null || true
if ${AXELERA_HARDWARE_PRESENT}; then
  if ! modprobe metis 2>/dev/null; then
    log "Metis is installed but could not be loaded before reboot; check Secure Boot/MOK enrollment if post-reboot verification fails"
  fi
fi
install -m 0644 /dev/null "${REBOOT_MARKER}"
printf 'installed_at=%s\nkernel=%s\nrecipe=%s\n' \
  "$(date --utc --iso-8601=seconds)" "$(uname -r)" "${LOCK_FILE}" >"${REBOOT_MARKER}"

log "installation complete; exact versions are recorded in ${LOCK_FILE}"
log "reboot this device before performing the verification commands below"

)

tvt_op_install_tvt_kubeconfig() (
# Source: scripts/install-tvt-kubeconfig.sh
set -Eeuo pipefail

TARGET=/etc/tvt/kubeconfig

usage() {
  echo "usage: sudo scripts/tvt-edge-operations.sh install-tvt-kubeconfig [--target PATH]" >&2
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
if [[ ${target_dir} == /etc/tvt ]]; then
  install -d -o root -g root -m 0755 "${target_dir}"
else
  install -d -o root -g tvt-edge -m 0750 "${target_dir}"
fi
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

)

tvt_op_publish_control_images() (
# Source: scripts/publish-control-images.sh
set -Eeuo pipefail

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"
# shellcheck source=config/platform.env
source "${REPO_ROOT}/config/platform.env"

REGISTRY=""
SCHEME="http"
LOCK_OUTPUT="${REPO_ROOT}/build/node-management-images.lock.json"
ARCHIVE_DIR=""

usage() {
  echo "usage: scripts/tvt-edge-operations.sh publish-control-images --registry HOST[:PORT] [--archive-dir DIR] [--scheme http|https] [--lock-output FILE]" >&2
}

while (($#)); do
  case "$1" in
    --registry) REGISTRY="${2:-}"; shift 2 ;;
    --scheme) SCHEME="${2:-}"; shift 2 ;;
    --lock-output) LOCK_OUTPUT="${2:-}"; shift 2 ;;
    --archive-dir) ARCHIVE_DIR="${2:-}"; shift 2 ;;
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
command -v docker >/dev/null 2>&1 || { echo "docker is required" >&2; exit 1; }
command -v curl >/dev/null 2>&1 || { echo "curl is required" >&2; exit 1; }
if [[ -n ${ARCHIVE_DIR} && (! -d ${ARCHIVE_DIR} || -L ${ARCHIVE_DIR}) ]]; then
  echo "--archive-dir must be a non-symlinked directory" >&2
  exit 2
fi

DOCKER=(docker)
if ! docker info >/dev/null 2>&1; then
  command -v sudo >/dev/null 2>&1 || { echo "cannot access Docker" >&2; exit 1; }
  DOCKER=(sudo docker)
fi
curl --fail --silent --show-error "${SCHEME}://${REGISTRY}/v2/" >/dev/null

for component in node-reporter node-status-controller; do
  case "${component}" in
    node-reporter) source_dir=reporter ;;
    node-status-controller) source_dir=status_controller ;;
  esac
  image="${REGISTRY}/apexfabric/${component}:${NODE_MANAGEMENT_IMAGE_VERSION}"
  if [[ -n ${ARCHIVE_DIR} ]]; then
    archive="${ARCHIVE_DIR}/${component}.tar"
    [[ -f ${archive} && ! -L ${archive} ]] || {
      echo "prebuilt image archive is missing: ${archive}" >&2
      exit 1
    }
    "${DOCKER[@]}" load --input "${archive}"
    source_image="apexfabric/${component}:${NODE_MANAGEMENT_IMAGE_VERSION}"
    architecture="$("${DOCKER[@]}" image inspect --format '{{.Architecture}}' "${source_image}")"
    [[ ${architecture} == amd64 ]] || {
      echo "${component} image architecture is ${architecture}, not amd64" >&2
      exit 1
    }
    "${DOCKER[@]}" tag "${source_image}" "${image}"
  else
    "${DOCKER[@]}" build --pull=false --provenance=false \
      -f "${REPO_ROOT}/apexfabric/node_management/${source_dir}/Dockerfile" \
      -t "${image}" "${REPO_ROOT}"
  fi
  "${DOCKER[@]}" push "${image}"
  echo "Published ${image}"
done

python3 -m tvt_runtime.image_lock create \
  --registry "${SCHEME}://${REGISTRY}" \
  --version "${NODE_MANAGEMENT_IMAGE_VERSION}" \
  --output "${LOCK_OUTPUT}"
echo "Wrote digest-pinned image lock ${LOCK_OUTPUT}"

)

tvt_op_publish_ui_image() (
# Source: scripts/publish-ui-image.sh
set -Eeuo pipefail

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"
# shellcheck source=config/platform.env
source "${REPO_ROOT}/config/platform.env"

# deploy/ui/Dockerfile is a frozen, exact copy (AGENTS.md §6) and must not be
# edited to fix a TVT problem. It COPYs deploy/ui/web/dist/ with no explicit
# mode, so the static files inherit the build host's umask; under
# deploy/single-box/ui.yaml's non-root securityContext (uid/gid 101) that can
# leave them unreadable by nginx (403 on every request). Apply the fix as a
# TVT-owned layer on top of the already-built frozen image instead.
fix_ui_image_static_permissions() {
  local image="$1" fixup_context
  shift
  fixup_context="$(mktemp -d)"
  printf 'FROM %s\nRUN chmod 0644 /etc/nginx/nginx.conf && chmod -R a+rX /usr/share/nginx/html\n' \
    "${image}" >"${fixup_context}/Dockerfile"
  "$@" build --pull=false -f "${fixup_context}/Dockerfile" -t "${image}" "${fixup_context}"
  rm -rf "${fixup_context}"
}

REGISTRY=""
SCHEME="http"
LOCK_OUTPUT="${REPO_ROOT}/build/ui-image.lock.json"
ARCHIVE_DIR=""

usage() {
  echo "usage: scripts/tvt-edge-operations.sh publish-ui-image --registry HOST[:PORT] [--archive-dir DIR] [--scheme http|https] [--lock-output FILE]" >&2
}

while (($#)); do
  case "$1" in
    --registry) REGISTRY="${2:-}"; shift 2 ;;
    --scheme) SCHEME="${2:-}"; shift 2 ;;
    --lock-output) LOCK_OUTPUT="${2:-}"; shift 2 ;;
    --archive-dir) ARCHIVE_DIR="${2:-}"; shift 2 ;;
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
command -v docker >/dev/null 2>&1 || { echo "docker is required" >&2; exit 1; }
command -v curl >/dev/null 2>&1 || { echo "curl is required" >&2; exit 1; }
if [[ -n ${ARCHIVE_DIR} && (! -d ${ARCHIVE_DIR} || -L ${ARCHIVE_DIR}) ]]; then
  echo "--archive-dir must be a non-symlinked directory" >&2
  exit 2
fi

DOCKER=(docker)
if ! docker info >/dev/null 2>&1; then
  command -v sudo >/dev/null 2>&1 || { echo "cannot access Docker" >&2; exit 1; }
  DOCKER=(sudo docker)
fi
curl --fail --silent --show-error "${SCHEME}://${REGISTRY}/v2/" >/dev/null

image="${REGISTRY}/apexfabric/ui:${UI_IMAGE_VERSION}"
if [[ -n ${ARCHIVE_DIR} ]]; then
  archive="${ARCHIVE_DIR}/ui.tar"
  [[ -f ${archive} && ! -L ${archive} ]] || {
    echo "prebuilt image archive is missing: ${archive}" >&2
    exit 1
  }
  "${DOCKER[@]}" load --input "${archive}"
  source_image="apexfabric/ui:${UI_IMAGE_VERSION}"
  architecture="$("${DOCKER[@]}" image inspect --format '{{.Architecture}}' "${source_image}")"
  [[ ${architecture} == amd64 ]] || {
    echo "ui image architecture is ${architecture}, not amd64" >&2
    exit 1
  }
  "${DOCKER[@]}" tag "${source_image}" "${image}"
else
  npm --prefix "${REPO_ROOT}/deploy/ui/web" ci
  npm --prefix "${REPO_ROOT}/deploy/ui/web" run build
  "${DOCKER[@]}" build --pull=false --provenance=false \
    -f "${REPO_ROOT}/deploy/ui/Dockerfile" \
    -t "${image}" "${REPO_ROOT}"
fi
fix_ui_image_static_permissions "${image}" "${DOCKER[@]}"
"${DOCKER[@]}" push "${image}"
echo "Published ${image}"

python3 -m tvt_runtime.ui_image create \
  --registry "${SCHEME}://${REGISTRY}" \
  --version "${UI_IMAGE_VERSION}" \
  --output "${LOCK_OUTPUT}"
echo "Wrote digest-pinned image lock ${LOCK_OUTPUT}"

)

tvt_op_qualify_traffic_edge() (
# Source: scripts/qualify-traffic-edge.sh
set -Eeuo pipefail
umask 077

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALLED_PIPELINE_VERSION="$(sed -n 's/^PIPELINE_TRAFFIC_VERSION=//p' /opt/tvt/config/pipeline.env 2>/dev/null | head -1)"
CATALOG_DIRECTORY="/opt/tvt/solution-packs/catalog/tvt-mills-pilot-${INSTALLED_PIPELINE_VERSION}"
QUALIFIER=(/opt/tvt/venv/bin/tvt-traffic-qualify)

if [[ ! -x ${QUALIFIER[0]} ]]; then
  QUALIFIER=("${REPO_ROOT}/.venv/bin/python" -m tvt_edge.qualification)
  REPO_PIPELINE_VERSION="$(sed -n 's/^PIPELINE_TRAFFIC_VERSION=//p' "${REPO_ROOT}/config/pipeline.env" | head -1)"
  CATALOG_DIRECTORY="${REPO_ROOT}/solution-packs/catalog/tvt-mills-pilot-${REPO_PIPELINE_VERSION}"
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

)

tvt_op_verify_k3s_plane() (
# Source: scripts/verify-k3s-plane.sh
set -Eeuo pipefail

KUBECTL=(k3s kubectl)
if [[ ! -r /etc/rancher/k3s/k3s.yaml && ${EUID} -ne 0 ]]; then
  command -v sudo >/dev/null 2>&1 || { echo "K3s kubeconfig is unreadable" >&2; exit 1; }
  KUBECTL=(sudo k3s kubectl)
fi

mapfile -t nodes < <("${KUBECTL[@]}" get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')
if [[ ${#nodes[@]} -ne 1 || -z "${nodes[0]}" ]]; then
  echo "TVT verification requires exactly one node; found ${#nodes[@]}" >&2
  exit 1
fi
node="${nodes[0]}"

"${KUBECTL[@]}" wait --for=condition=Ready "node/${node}" --timeout=60s
"${KUBECTL[@]}" get namespace apexfabric >/dev/null
"${KUBECTL[@]}" get crd apexnodestatuses.apexfabric.com >/dev/null
"${KUBECTL[@]}" rollout status daemonset/node-reporter -n apexfabric --timeout=180s
"${KUBECTL[@]}" rollout status deployment/node-status-controller -n apexfabric --timeout=180s
"${KUBECTL[@]}" wait --for=jsonpath='{.status.accepted}'=true \
  "apexnodestatus/${node}" --timeout=180s

qualified="$("${KUBECTL[@]}" get "node/${node}" -o jsonpath='{.metadata.labels.apexfabric\.com/qualified}')"
[[ "${qualified}" == true ]] || { echo "node is not controller-qualified" >&2; exit 1; }
"${KUBECTL[@]}" auth can-i create deployments.apps \
  --as=system:serviceaccount:apexfabric:node-agent -n apexfabric | grep -qx yes

reporter_image="$("${KUBECTL[@]}" get daemonset/node-reporter -n apexfabric -o jsonpath='{.spec.template.spec.containers[0].image}')"
controller_image="$("${KUBECTL[@]}" get deployment/node-status-controller -n apexfabric -o jsonpath='{.spec.template.spec.containers[0].image}')"
[[ "${reporter_image}" == *@sha256:* && "${controller_image}" == *@sha256:* ]] || {
  echo "node-management images are not digest-pinned" >&2
  exit 1
}
echo "TVT K3s plane verified on ${node}"

)

tvt_op_verify_local_registry() (
# Source: scripts/verify-local-registry.sh
set -Eeuo pipefail

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=config/platform.env
source "${REPO_ROOT}/config/platform.env"

retry_with_timeout() {
  local description="$1"
  local duration="$2"
  shift 2
  local attempt
  for attempt in 1 2 3; do
    if timeout --signal=TERM "${duration}" "$@"; then
      return 0
    fi
    if [[ ${attempt} -lt 3 ]]; then
      echo "${description} failed (attempt ${attempt}/3); retrying in 3 seconds" >&2
      sleep 3
    fi
  done
  echo "${description} failed after 3 attempts" >&2
  return 1
}

[[ ${EUID} -eq 0 ]] || { echo "run as root" >&2; exit 1; }
for command_name in curl docker k3s systemctl timeout; do
  command -v "${command_name}" >/dev/null 2>&1 || {
    echo "required command not found: ${command_name}" >&2
    exit 1
  }
done
systemctl is-active --quiet docker.service || { echo "docker.service is not active" >&2; exit 1; }
systemctl is-active --quiet tvt-local-registry.service || {
  echo "tvt-local-registry.service is not active" >&2
  exit 1
}
systemctl is-active --quiet k3s.service || { echo "k3s.service is not active" >&2; exit 1; }

curl --fail --silent --show-error --max-time 5 \
  "http://${LOCAL_REGISTRY_ADDRESS}/v2/" >/dev/null
container_image="$(docker inspect --format '{{.Config.Image}}' "${LOCAL_REGISTRY_CONTAINER_NAME}")"
[[ "${container_image}" == "${LOCAL_REGISTRY_IMAGE}" ]] || {
  echo "registry container uses ${container_image}, expected ${LOCAL_REGISTRY_IMAGE}" >&2
  exit 1
}
published_port="$(docker port "${LOCAL_REGISTRY_CONTAINER_NAME}" 5000/tcp)"
[[ "${published_port}" == "${LOCAL_REGISTRY_ADDRESS}" ]] || {
  echo "registry publishes ${published_port}, expected loopback-only ${LOCAL_REGISTRY_ADDRESS}" >&2
  exit 1
}
registry_mount="$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/var/lib/registry"}}{{.Source}}{{end}}{{end}}' "${LOCAL_REGISTRY_CONTAINER_NAME}")"
[[ "${registry_mount}" == "${LOCAL_REGISTRY_DATA_DIR}" ]] || {
  echo "registry data mount is ${registry_mount}, expected ${LOCAL_REGISTRY_DATA_DIR}" >&2
  exit 1
}

local_image="${LOCAL_REGISTRY_ADDRESS}/${LOCAL_REGISTRY_SMOKE_REPOSITORY}:${LOCAL_REGISTRY_SMOKE_TAG}"
retry_with_timeout "pinned smoke image pull" 5m \
  docker pull --platform linux/amd64 "${LOCAL_REGISTRY_SMOKE_IMAGE}"
docker tag "${LOCAL_REGISTRY_SMOKE_IMAGE}" "${local_image}"
retry_with_timeout "smoke image push" 5m docker push "${local_image}"

manifest_headers="$(curl --fail --silent --show-error --head --max-time 10 \
  --header 'Accept: application/vnd.docker.distribution.manifest.v2+json' \
  "http://${LOCAL_REGISTRY_ADDRESS}/v2/${LOCAL_REGISTRY_SMOKE_REPOSITORY}/manifests/${LOCAL_REGISTRY_SMOKE_TAG}")"
local_digest="$(awk 'tolower($1) == "docker-content-digest:" {gsub("\\r", "", $2); print $2}' <<<"${manifest_headers}")"
[[ "${local_digest}" =~ ^sha256:[0-9a-f]{64}$ ]] || {
  echo "local registry did not return an immutable digest for ${local_image}" >&2
  exit 1
}
immutable_image="${LOCAL_REGISTRY_ADDRESS}/${LOCAL_REGISTRY_SMOKE_REPOSITORY}@${local_digest}"

retry_with_timeout "K3s/containerd smoke image pull" 3m \
  k3s crictl pull "${immutable_image}"
crictl_images="$(k3s crictl images --digests --no-trunc)"
grep -Fq "${LOCAL_REGISTRY_ADDRESS}/${LOCAL_REGISTRY_SMOKE_REPOSITORY}" \
  <<<"${crictl_images}" || {
  echo "k3s crictl does not list the local smoke repository" >&2
  exit 1
}
grep -Fq "${local_digest}" <<<"${crictl_images}" || {
  echo "k3s crictl does not list the pulled digest ${local_digest}" >&2
  exit 1
}

echo "Verified Docker push and K3s/containerd pull through ${LOCAL_REGISTRY_ADDRESS}."
echo "Smoke image: ${immutable_image}"

)

tvt_op_verify_pipeline_image_sync() (
# Source: scripts/verify-pipeline-image-sync.sh
set -Eeuo pipefail
umask 077

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=config/platform.env
source "${REPO_ROOT}/config/platform.env"
# shellcheck source=config/pipeline.env
source "${REPO_ROOT}/config/pipeline.env"

LOCK_OUTPUT=/var/lib/tvt/pipeline/traffic-image.lock.json
if (($#)); then
  [[ "$1" == --lock-output && -n "${2:-}" && $# -eq 2 ]] || {
    echo "usage: scripts/tvt-edge-operations.sh verify-pipeline-image-sync [--lock-output FILE]" >&2
    exit 2
  }
  LOCK_OUTPUT="$2"
fi

for command_name in curl python3 sha256sum stat systemctl; do
  command -v "${command_name}" >/dev/null 2>&1 || {
    echo "required command not found: ${command_name}" >&2
    exit 1
  }
done
[[ -f "${LOCK_OUTPUT}" ]] || { echo "image lock not found: ${LOCK_OUTPUT}" >&2; exit 1; }
[[ "$(stat -c '%a' "${LOCK_OUTPUT}")" == 600 ]] || {
  echo "image lock must have mode 0600" >&2
  exit 1
}

lock_values="$(python3 - "${LOCK_OUTPUT}" "${PIPELINE_TRAFFIC_CATALOG_ID}" \
  "${PIPELINE_REPOSITORY}" "${PIPELINE_REVISION}" \
  "${PIPELINE_TRAFFIC_DELIVERY_DIR}" "${PIPELINE_TRAFFIC_ARCHIVE}" \
  "${PIPELINE_TRAFFIC_ARCHIVE_SHA256}" "${PIPELINE_TRAFFIC_ARCHIVE_SIZE}" \
  "${LOCAL_REGISTRY_ADDRESS}" "${PIPELINE_TRAFFIC_LOCAL_REPOSITORY}" \
  "${PIPELINE_TRAFFIC_LOCAL_TAG}" "${PIPELINE_TRAFFIC_CONTRACT_SHA256}" \
  "${PIPELINE_TRAFFIC_DESIRED_STATE_SCHEMA_SHA256}" \
  "${PIPELINE_TRAFFIC_METRICS_SCHEMA_SHA256}" \
  "${PIPELINE_TRAFFIC_ANALYTICS_EVENT_SCHEMA_SHA256}" \
  "${PIPELINE_TRAFFIC_ANALYTICS_EVENT_EXAMPLE_SHA256}" \
  "${PIPELINE_TRAFFIC_PLAN_COMPILER_COMPATIBILITY_ID}" \
  "${PIPELINE_TRAFFIC_PLAN_COMPILER_SHA256}" <<'PY'
import json
import re
import sys
from datetime import datetime
from pathlib import Path

lock = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
pipeline = lock.get("pipeline", {})
archive = lock.get("archive", {})
image = lock.get("image", {})
metadata = lock.get("metadata", {})
source = lock.get("source", {})
compatibility = lock.get("compatibility", {})
expected = {
    "catalog": (lock.get("catalog_id"), sys.argv[2]),
    "repository": (pipeline.get("repository"), sys.argv[3]),
    "commit": (pipeline.get("commit"), sys.argv[4]),
    "delivery": (pipeline.get("delivery_directory"), sys.argv[5]),
    "archive": (archive.get("filename"), sys.argv[6]),
    "archive sha256": (archive.get("sha256"), sys.argv[7]),
    "archive size": (archive.get("size"), int(sys.argv[8])),
    "registry": (image.get("registry"), sys.argv[9]),
    "image repository": (image.get("repository"), sys.argv[10]),
    "tag": (image.get("tag"), sys.argv[11]),
    "contract checksum": (metadata.get("image_contract_sha256"), sys.argv[12]),
    "schema checksum": (metadata.get("desired_state_schema_sha256"), sys.argv[13]),
    "metrics schema checksum": (metadata.get("metrics_schema_sha256"), sys.argv[14]),
    "event schema checksum": (metadata.get("analytics_event_schema_sha256"), sys.argv[15]),
    "event example checksum": (metadata.get("analytics_event_example_sha256"), sys.argv[16]),
    "plan compiler compatibility": (compatibility.get("plan_compiler"), sys.argv[17]),
    "plan compiler checksum": (compatibility.get("sha256"), sys.argv[18]),
    "architecture": (image.get("architecture"), "amd64"),
}
if lock.get("format_version") != 2:
    raise SystemExit("unsupported image lock format")
for name, (actual, wanted) in expected.items():
    if actual != wanted:
        raise SystemExit(f"image lock {name} does not match the configured v4 pin")
if source.get("mode") not in {"archive", "bundled"}:
    raise SystemExit("image lock source mode is not an approved archive path")
digest = image.get("digest", "")
reference = image.get("reference", "")
expected_reference = f"{sys.argv[9]}/{sys.argv[10]}@{digest}"
if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest) or reference != expected_reference:
    raise SystemExit("image lock does not contain a valid immutable reference")
try:
    verified_at = datetime.fromisoformat(lock["verification_timestamp"])
except (KeyError, TypeError, ValueError) as error:
    raise SystemExit("image lock has an invalid verification timestamp") from error
if verified_at.tzinfo is None:
    raise SystemExit("image lock verification timestamp must include a timezone")
print(digest)
print(reference)
PY
)"
locked_digest="$(sed -n '1p' <<<"${lock_values}")"
immutable_reference="$(sed -n '2p' <<<"${lock_values}")"

response_dir="$(mktemp -d)"
trap 'rm -rf -- "${response_dir}"' EXIT
curl --fail --silent --show-error --retry 2 --retry-delay 2 \
  --retry-connrefused --max-time 15 --retry-max-time 45 \
  --dump-header "${response_dir}/headers" --output "${response_dir}/manifest" \
  --header 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json' \
  "http://${LOCAL_REGISTRY_ADDRESS}/v2/${PIPELINE_TRAFFIC_LOCAL_REPOSITORY}/manifests/${PIPELINE_TRAFFIC_LOCAL_TAG}"
registry_digest="$(awk 'tolower($1) == "docker-content-digest:" {gsub("\r", "", $2); print $2}' "${response_dir}/headers")"
computed_digest="sha256:$(sha256sum "${response_dir}/manifest" | awk '{print $1}')"
[[ "${registry_digest}" == "${computed_digest}" && "${registry_digest}" == "${locked_digest}" ]] || {
  echo "registry manifest digest does not match the known-good image lock" >&2
  exit 1
}

if [[ "${LOCK_OUTPUT}" == /var/lib/tvt/* ]]; then
  systemctl is-active --quiet tvt-local-registry.service
  systemctl is-enabled --quiet tvt-pipeline-image-sync.timer
  [[ "$(systemctl show tvt-pipeline-image-sync.service --property=Result --value)" == success ]] || {
    echo "the most recent PIPELINE image synchronization did not succeed" >&2
    exit 1
  }
fi

echo "Verified pinned PIPELINE Traffic image synchronization."
echo "Immutable image: ${immutable_reference}"
echo "Lock: ${LOCK_OUTPUT}"

)

tvt_op_verify_release() (
# Source: scripts/verify-tvt-edge-release.sh
set -Eeuo pipefail
umask 027

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUNDLE=""
usage() { echo "usage: scripts/tvt-edge-operations.sh verify-release --bundle DIR" >&2; }
while (($#)); do
  case "$1" in
    --bundle) BUNDLE="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage; exit 2 ;;
  esac
done
[[ -d ${BUNDLE} && ! -L ${BUNDLE} ]] || { usage; exit 2; }
BUNDLE="$(cd "${BUNDLE}" && pwd -P)"

# shellcheck source=scripts/lib/tvt-installer-common.sh
source "${REPO_ROOT}/scripts/lib/tvt-installer-common.sh"
tvt_verify_bundle "${BUNDLE}"

python3 - "${BUNDLE}" <<'PY'
import hashlib
import json
import pathlib
import re
import sys
import zipfile

root = pathlib.Path(sys.argv[1])
manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
lock = json.loads((root / manifest["artifacts"]["input_lock"]).read_text(encoding="utf-8"))
version = manifest["release_version"]
commit = manifest["source_commit"]
if lock.get("release_version") != version or lock.get("source_commit") != commit:
    raise SystemExit("release identity does not match the external-input lock")
wheel = root / manifest["artifacts"]["application_wheel"]
with zipfile.ZipFile(wheel) as archive:
    metadata_names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
    if len(metadata_names) != 1:
        raise SystemExit("application wheel does not contain exactly one metadata record")
    metadata = archive.read(metadata_names[0]).decode("utf-8", errors="strict")
    match = re.search(r"^Version: (.+)$", metadata, flags=re.MULTILINE)
    if not match or match.group(1).strip() != version:
        raise SystemExit("application wheel version does not match the manifest")
    # No tvt_edge/static/ check here: the legacy ui/ console (whose build this
    # once verified) was retired -- tvt_edge/api/app.py is headless, and the
    # TVT dashboard ships as its own Docker image (manifest artifacts.ui_image
    # / images/ui.tar), not embedded in the application wheel. See the TVT
    # frontend policy in AGENTS.md.

def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()

locked_files = lock.get("files")
if not isinstance(locked_files, dict) or not locked_files:
    raise SystemExit("external-input lock has no file records")
for relative, record in locked_files.items():
    if not isinstance(relative, str) or not isinstance(record, dict):
        raise SystemExit("external-input lock contains an invalid file record")
    installed = pathlib.PurePosixPath(relative)
    if installed.is_absolute() or not installed.parts or any(
        part in {"", ".", ".."} for part in installed.parts
    ):
        raise SystemExit(f"external-input lock contains an unsafe path: {relative}")
    if installed.parts[0] == "apt":
        installed = pathlib.PurePosixPath("packages", *installed.parts)
    path = root.joinpath(*installed.parts)
    if not path.is_file():
        raise SystemExit(f"locked release input is missing from bundle: {installed}")
    if path.stat().st_size != record.get("size") or sha256(path) != record.get("sha256"):
        raise SystemExit(f"bundled input differs from its lock: {installed}")
configuration = lock.get("configuration", {})
if not isinstance(configuration, dict):
    raise SystemExit("external-input lock contains an invalid configuration record")
if sha256(root / "config/platform.env") != configuration.get("platform_sha256"):
    raise SystemExit("bundled platform config differs from the input lock")
if sha256(root / "config/pipeline.env") != configuration.get("pipeline_sha256"):
    raise SystemExit("bundled pipeline config differs from the input lock")
if sha256(root / "config/hardware-matrix.env") != configuration.get("hardware_matrix_sha256"):
    raise SystemExit("bundled hardware matrix differs from the input lock")
if lock.get("schema_version") != 2:
    raise SystemExit("external-input lock schema is not version 2 (edge-inventory-driven)")
inventory = lock.get("edge_inventory", {})
if not isinstance(inventory, dict) or not inventory.get("kernel_target") \
        or not isinstance(inventory.get("axelera_present"), bool) \
        or not inventory.get("sha256"):
    raise SystemExit("external-input lock has no valid edge inventory record")
if manifest.get("kernel_target") != inventory.get("kernel_target"):
    raise SystemExit("manifest kernel target does not match the input lock")
expected_variant = "metis" if inventory.get("axelera_present") else "intel-only"
if manifest.get("axelera_variant") != expected_variant:
    raise SystemExit("manifest Axelera variant does not match the input lock")
if manifest.get("edge_inventory_sha256") != inventory.get("sha256"):
    raise SystemExit("manifest inventory digest does not match the input lock")
print(f"Verified TVT edge release {version} from {commit}")
PY

)

tvt_op_verify_docker_archive_tag() (
# Source: scripts/verify-docker-archive-tag.py
  exec python3 - "$@" <<'TVT_VERIFY_DOCKER_ARCHIVE_TAG_PY'
"""Verify that a Docker-save or OCI-layout archive has one expected tag."""

from __future__ import annotations

import argparse
import json
import sys
import tarfile
from pathlib import Path


MAX_MANIFEST_SIZE = 4 * 1024 * 1024


class ArchiveTagError(ValueError):
    pass


def _read_json(archive: tarfile.TarFile, name: str) -> object:
    members = [member for member in archive.getmembers() if member.name == name]
    if len(members) != 1:
        raise ArchiveTagError(f"archive must contain exactly one {name}")
    member = members[0]
    if not member.isfile() or member.size > MAX_MANIFEST_SIZE:
        raise ArchiveTagError(f"archive {name} is not a bounded regular file")
    stream = archive.extractfile(member)
    if stream is None:
        raise ArchiveTagError(f"archive {name} cannot be read")
    return json.load(stream)


def archive_tags(path: Path) -> list[str]:
    try:
        with tarfile.open(path, mode="r:*") as archive:
            names = {member.name for member in archive.getmembers()}
            if "manifest.json" in names:
                document = _read_json(archive, "manifest.json")
                if not isinstance(document, list) or not document:
                    raise ArchiveTagError("archive manifest.json must contain a non-empty list")
                tags: list[str] = []
                for entry in document:
                    if not isinstance(entry, dict) or not isinstance(entry.get("RepoTags"), list):
                        raise ArchiveTagError("archive manifest entry has no RepoTags list")
                    if not all(isinstance(tag, str) and tag for tag in entry["RepoTags"]):
                        raise ArchiveTagError("archive manifest contains an invalid image tag")
                    tags.extend(entry["RepoTags"])
                return tags
            if "index.json" in names and "oci-layout" in names:
                document = _read_json(archive, "index.json")
                if not isinstance(document, dict) or document.get("schemaVersion") != 2:
                    raise ArchiveTagError("OCI index.json has an invalid schema")
                manifests = document.get("manifests")
                if not isinstance(manifests, list) or len(manifests) != 1:
                    raise ArchiveTagError("OCI archive must contain exactly one image manifest")
                annotations = manifests[0].get("annotations") if isinstance(manifests[0], dict) else None
                tag = annotations.get("org.opencontainers.image.ref.name") if isinstance(annotations, dict) else None
                if not isinstance(tag, str) or not tag:
                    raise ArchiveTagError("OCI image manifest has no reference-name annotation")
                return [tag]
            raise ArchiveTagError("archive is neither Docker-save nor OCI-layout format")
    except (OSError, tarfile.TarError, json.JSONDecodeError) as error:
        raise ArchiveTagError(f"invalid container archive: {error}") from error


def verify(path: Path, expected: str) -> None:
    tags = archive_tags(path)
    if tags != [expected]:
        rendered = ", ".join(tags) if tags else "none"
        raise ArchiveTagError(
            f"archive image tag mismatch: found [{rendered}], expected [{expected}]"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--expected", required=True)
    args = parser.parse_args()
    try:
        verify(args.archive, args.expected)
    except ArchiveTagError as error:
        print(f"container-archive-tag: ERROR: {error}", file=sys.stderr)
        return 1
    print(f"Verified container archive image tag: {args.expected}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

TVT_VERIFY_DOCKER_ARCHIVE_TAG_PY
)

tvt_op_verify_pipeline_image_inspect() (
# Source: scripts/verify-pipeline-image-inspect.py
  exec python3 - "$@" <<'TVT_VERIFY_PIPELINE_IMAGE_INSPECT_PY'
"""Validate Docker image-inspect JSON against the pinned Traffic v4 contract."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def verify(document: object, expected: argparse.Namespace) -> list[str]:
    if not isinstance(document, list) or len(document) != 1:
        return ["Docker returned invalid image inspection data"]
    image = document[0]
    if not isinstance(image, dict):
        return ["Docker returned invalid image inspection data"]
    config = image.get("Config") or {}
    labels = config.get("Labels") or {}
    expected_labels = {
        "org.opencontainers.image.title": expected.title,
        "org.opencontainers.image.version": expected.version,
        "io.apexfabric.contract.version": expected.contract_version,
        "io.apexfabric.hardware.profile": expected.hardware_profile,
        "io.apexfabric.models.delivery": expected.models_delivery,
    }
    if expected.source:
        expected_labels["org.opencontainers.image.source"] = expected.source
    errors = []
    if image.get("Architecture") != "amd64":
        errors.append(f"architecture {image.get('Architecture')!r}, expected 'amd64'")
    for name, wanted in expected_labels.items():
        if labels.get(name) != wanted:
            errors.append(f"label {name} is {labels.get(name)!r}, expected {wanted!r}")
    if config.get("User") != expected.user:
        errors.append(f"user {config.get('User')!r}, expected {expected.user!r}")
    if expected.port not in (config.get("ExposedPorts") or {}):
        errors.append(f"port {expected.port!r} is not exposed")
    command = " ".join((config.get("Entrypoint") or []) + (config.get("Cmd") or []))
    if command != expected.command:
        errors.append(f"container command is {command!r}, expected {expected.command!r}")
    return errors


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser()
    value.add_argument("inspection", type=Path)
    value.add_argument("--source", required=True)
    value.add_argument("--title", required=True)
    value.add_argument("--version", required=True)
    value.add_argument("--contract-version", required=True)
    value.add_argument("--hardware-profile", required=True)
    value.add_argument("--models-delivery", required=True)
    value.add_argument("--user", required=True)
    value.add_argument("--port", required=True)
    value.add_argument("--command", required=True)
    return value


def main() -> int:
    args = parser().parse_args()
    try:
        document = json.loads(args.inspection.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"cannot read Docker image inspection: {error}") from error
    errors = verify(document, args)
    if errors:
        raise SystemExit("solution image contract verification failed: " + "; ".join(errors))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

TVT_VERIFY_PIPELINE_IMAGE_INSPECT_PY
)

tvt_op_verify_traffic_qualification() (
# Source: scripts/verify-traffic-qualification.py
  exec python3 - "$@" <<'TVT_VERIFY_TRAFFIC_QUALIFICATION_PY'
"""Verify a completed qualification report without exposing its contents."""

from __future__ import annotations

import argparse
import json
import re
import stat
from pathlib import Path
from typing import Any


DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
HEX_DIGEST = re.compile(r"[0-9a-f]{64}")
RTSP = re.compile(r"rtsps?://", re.IGNORECASE)
SENSITIVE_KEYS = {
    "authorization",
    "camera_sources",
    "ciphertext",
    "credential",
    "credentials",
    "password",
    "secret",
    "stringdata",
    "token",
    "username",
}
REQUIRED_CHECK_IDS = {
    "contracts.provenance",
    "host.platform",
    "host.devices",
    "host.accelerators",
    "service.postgresql",
    "service.docker",
    "service.tvt-local-registry",
    "service.k3s",
    "service.tvt-edge",
    "service.tvt-camera-sync",
    "service.tvt-pipeline-image-sync",
    "registry.api",
    "deployment.applied",
    "catalog.available",
    "image_lock.immutable",
    "containerd.image",
    "kubernetes.node",
    "kubernetes.workload_contract",
    "kubernetes.secrets",
    "kubernetes.pod",
    "kubernetes.persistent_state",
    "runtime.health",
    "runtime.metrics_schema",
    "runtime.events_sse",
    "runtime.analytics_events",
}


def contains_sensitive_key(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            key.lower() in SENSITIVE_KEYS or contains_sensitive_key(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(contains_sensitive_key(item) for item in value)
    return False


def main(argv: list[str] | None = None) -> int:
    arguments = argparse.ArgumentParser(
        description="Verify a redacted Traffic qualification evidence report"
    )
    arguments.add_argument("report", type=Path)
    args = arguments.parse_args(argv)
    path = args.report
    if path.is_symlink() or not path.is_file():
        raise SystemExit("qualification report must be a regular non-symlink file")
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise SystemExit("qualification report must have mode 0600")
    body = path.read_text(encoding="utf-8")
    if RTSP.search(body):
        raise SystemExit("qualification report contains an RTSP URL")
    try:
        report = json.loads(body)
    except json.JSONDecodeError as error:
        raise SystemExit("qualification report is not valid JSON") from error
    if not isinstance(report, dict) or report.get("format_version") != 1:
        raise SystemExit("qualification report format is unsupported")
    if report.get("qualification") != "traffic-edge-runtime-v4":
        raise SystemExit("qualification report has the wrong qualification ID")
    if contains_sensitive_key(report):
        raise SystemExit("qualification report contains a sensitive field")
    checks = report.get("checks")
    if not isinstance(checks, list) or not checks:
        raise SystemExit("qualification report has no checks")
    identifiers = [item.get("id") for item in checks if isinstance(item, dict)]
    if len(identifiers) != len(checks) or len(set(identifiers)) != len(identifiers):
        raise SystemExit("qualification check identifiers must be present and unique")
    identifier_set = set(identifiers)
    missing = REQUIRED_CHECK_IDS - identifier_set
    if missing:
        raise SystemExit("qualification report is missing required checks")
    if any(item.get("status") not in {"passed", "skipped"} for item in checks):
        raise SystemExit("qualification contains an invalid or unsuccessful check status")
    failed = [item for item in checks if item.get("status") == "failed"]
    if report.get("outcome") != "passed" or failed:
        raise SystemExit("qualification did not pass")
    expected_summary = {
        "passed": sum(item.get("status") == "passed" for item in checks),
        "failed": 0,
        "skipped": sum(item.get("status") == "skipped" for item in checks),
    }
    if report.get("summary") != expected_summary:
        raise SystemExit("qualification summary does not match its checks")
    checkpoint = report.get("checkpoint")
    if checkpoint not in {"steady", "pre-reboot", "post-reboot", "post-rollback"}:
        raise SystemExit("qualification checkpoint is invalid")
    if checkpoint != "steady" and f"checkpoint.{checkpoint}" not in identifier_set:
        raise SystemExit("qualification report is missing its checkpoint comparison")
    actions = report.get("actions")
    if (
        not isinstance(actions, dict)
        or set(actions)
        != {"preview_requested", "commit_requested", "rollback_requested"}
        or any(not isinstance(value, bool) for value in actions.values())
        or (actions["commit_requested"] and not actions["preview_requested"])
    ):
        raise SystemExit("qualification action record is invalid")
    if actions.get("preview_requested") and "deployment.preview" not in identifier_set:
        raise SystemExit("qualification report is missing its deployment preview")
    if actions.get("commit_requested") and "deployment.commit" not in identifier_set:
        raise SystemExit("qualification report is missing its deployment commit")
    if actions.get("rollback_requested") and "rollback.request" not in identifier_set:
        raise SystemExit("qualification report is missing its rollback request")
    invariants = report.get("invariants", {})
    if (
        invariants.get("catalog_id") != "tvt-mills-pilot:2026.09.18-v2-ungated-enroll"
        or not invariants.get("deployment_id")
        or invariants.get("namespace") != "apexfabric"
        or not isinstance(invariants.get("applied_revision"), int)
    ):
        raise SystemExit("qualification deployment invariants are invalid")
    if not HEX_DIGEST.fullmatch(str(invariants.get("bundle_sha256", ""))):
        raise SystemExit("qualification bundle digest is invalid")
    if not DIGEST.fullmatch(str(invariants.get("image_digest", ""))):
        raise SystemExit("qualification image digest is invalid")
    if not invariants.get("pvc_uid"):
        raise SystemExit("qualification report has no persistent-state PVC identity")
    print(
        json.dumps(
            {
                "outcome": "verified",
                "report": str(path),
                "checks": len(checks),
                "checkpoint": report.get("checkpoint"),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

TVT_VERIFY_TRAFFIC_QUALIFICATION_PY
)

tvt_op_upgrade_application() (
# Upgrade only the TVT application, database schema, and dashboard image on an
# already-installed edge. The previous release directory is retained and the
# application symlinks are restored automatically if a later step fails.
set -Eeuo pipefail
umask 027

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUNDLE="${REPO_ROOT}"

usage() {
  echo "usage: scripts/tvt-edge-operations.sh upgrade-application [--bundle DIR]" >&2
}

while (($#)); do
  case "$1" in
    --bundle) BUNDLE="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage; exit 2 ;;
  esac
done

# shellcheck source=scripts/lib/tvt-installer-common.sh
source "${REPO_ROOT}/scripts/lib/tvt-installer-common.sh"
tvt_require_root
for command_name in curl docker flock k3s pg_dump python3 readlink runuser systemctl; do
  command -v "${command_name}" >/dev/null 2>&1 || tvt_fail "required command is unavailable: ${command_name}"
done
BUNDLE="$(tvt_canonical_directory "${BUNDLE}")"
tvt_verify_bundle "${BUNDLE}"
tvt_acquire_lock

readonly RELEASE_VERSION="$(tvt_manifest_value "${BUNDLE}" release_version)"
readonly OPT_TVT="${TVT_OPT_DIRECTORY:-/opt/tvt}"
readonly RELEASE_DIRECTORY="${OPT_TVT}/releases/${RELEASE_VERSION}"
readonly RESOURCE_DIRECTORY="${RELEASE_DIRECTORY}/resources"
readonly VENV_DIRECTORY="${RELEASE_DIRECTORY}/venv"
readonly INSTALL_STATE="${TVT_INSTALL_STATE_ROOT}/install-state.json"
readonly UPGRADE_REPORT="${TVT_INSTALL_STATE_ROOT}/application-upgrade-report.json"

[[ -L ${OPT_TVT}/current && -L ${OPT_TVT}/venv ]] || \
  tvt_fail "the installed application links are missing or unsafe"
previous_release="$(readlink -f "${OPT_TVT}/current")"
previous_venv="$(readlink -f "${OPT_TVT}/venv")"
[[ -d ${previous_release} && -x ${previous_venv}/bin/tvt-edge ]] || \
  tvt_fail "the current TVT release is incomplete"
[[ ${previous_release} != "${RELEASE_DIRECTORY}" ]] || \
  tvt_fail "release ${RELEASE_VERSION} is already active"

backup_stamp="$(date --utc +'%Y%m%dT%H%M%SZ')"
backup_directory="${TVT_INSTALL_STATE_ROOT}/upgrade-backups/${backup_stamp}-${RELEASE_VERSION}"
install -d -o root -g root -m 0700 "${backup_directory}"
printf '%s\n' "${previous_release}" >"${backup_directory}/previous-release"
printf '%s\n' "${previous_venv}" >"${backup_directory}/previous-venv"
chmod 0600 "${backup_directory}/previous-release" "${backup_directory}/previous-venv"
for evidence in install-state.json installation-report.json ui-image.lock.json; do
  if [[ -f ${TVT_INSTALL_STATE_ROOT}/${evidence} ]]; then
    cp -a "${TVT_INSTALL_STATE_ROOT}/${evidence}" "${backup_directory}/${evidence}"
  fi
done
deployment_count_before="$(runuser -u postgres -- psql -d tvt -Atc 'SELECT count(*) FROM solution_deployments')"
runuser -u postgres -- pg_dump --format=custom tvt >"${backup_directory}/tvt.pg_dump"
chmod 0600 "${backup_directory}/tvt.pg_dump"

rollback_required=true
rollback_application() {
  local status=$?
  if ${rollback_required}; then
    tvt_log "application upgrade failed; restoring ${previous_release}"
    local old_venv_link old_current_link
    old_venv_link="${OPT_TVT}/.venv.rollback.$$"
    old_current_link="${OPT_TVT}/.current.rollback.$$"
    ln -s "${previous_venv}" "${old_venv_link}"
    ln -s "${previous_release}" "${old_current_link}"
    mv -Tf "${old_venv_link}" "${OPT_TVT}/venv"
    mv -Tf "${old_current_link}" "${OPT_TVT}/current"
    systemctl restart tvt-edge.service tvt-camera-sync.service || true
  fi
  return "${status}"
}
trap rollback_application ERR

install -d -o root -g root -m 0755 "${OPT_TVT}/releases" "${RELEASE_DIRECTORY}"
[[ ! -L ${RELEASE_DIRECTORY} ]] || tvt_fail "refusing symlinked release directory"
if [[ -f ${RESOURCE_DIRECTORY}/manifest.json ]]; then
  cmp -s "${BUNDLE}/manifest.json" "${RESOURCE_DIRECTORY}/manifest.json" || \
    tvt_fail "release ${RELEASE_VERSION} is already present with a different manifest"
fi
install -d -o root -g root -m 0755 "${RESOURCE_DIRECTORY}"
for item in manifest.json checksums.sha256 alembic.ini apexfabric config deploy examples \
  scripts solution-packs images k3s tvt_edge; do
  cp -a "${BUNDLE}/${item}" "${RESOURCE_DIRECTORY}/"
done
python3 -m venv --clear "${VENV_DIRECTORY}"
wheel_relative="$(tvt_manifest_value "${BUNDLE}" artifacts.application_wheel)"
"${VENV_DIRECTORY}/bin/python" -m pip install --disable-pip-version-check \
  --no-index --find-links "${BUNDLE}/wheels" "${BUNDLE}/${wheel_relative}"
chown -R root:root "${RELEASE_DIRECTORY}"
chmod -R go+rX "${VENV_DIRECTORY}" "${RESOURCE_DIRECTORY}"
export PATH="${VENV_DIRECTORY}/bin:${PATH}"

systemctl stop tvt-camera-sync.service tvt-edge.service
runuser -u postgres -- env \
  TVT_RESOURCE_ROOT="${RESOURCE_DIRECTORY}" \
  TVT_DATABASE_URL=postgresql+psycopg:///tvt \
  "${VENV_DIRECTORY}/bin/tvt-edge" migrate

new_venv_link="${OPT_TVT}/.venv.${RELEASE_VERSION}.$$"
new_current_link="${OPT_TVT}/.current.${RELEASE_VERSION}.$$"
ln -s "${VENV_DIRECTORY}" "${new_venv_link}"
ln -s "${RELEASE_DIRECTORY}" "${new_current_link}"
mv -Tf "${new_venv_link}" "${OPT_TVT}/venv"
mv -Tf "${new_current_link}" "${OPT_TVT}/current"
systemctl daemon-reload
systemctl restart tvt-edge.service tvt-camera-sync.service

ui_lock="${TVT_INSTALL_STATE_ROOT}/ui-image.lock.json"
"${RESOURCE_DIRECTORY}/scripts/tvt-edge-operations.sh" publish-ui-image \
  --registry 127.0.0.1:5000 --scheme http \
  --archive-dir "${RESOURCE_DIRECTORY}/images" --lock-output "${ui_lock}"
"${RESOURCE_DIRECTORY}/scripts/tvt-edge-operations.sh" install-apexfabric-ui \
  --image-lock "${ui_lock}"

expected_migration="$(TVT_RESOURCE_ROOT="${RESOURCE_DIRECTORY}" "${VENV_DIRECTORY}/bin/python" - <<'PY'
from alembic.config import Config
from alembic.script import ScriptDirectory
from tvt_edge.paths import RESOURCE_ROOT

heads = ScriptDirectory.from_config(Config(str(RESOURCE_ROOT / "alembic.ini"))).get_heads()
if len(heads) != 1:
    raise SystemExit("release must contain exactly one Alembic head")
print(heads[0])
PY
)"
actual_migration="$(runuser -u postgres -- psql -d tvt -Atc 'SELECT version_num FROM alembic_version')"
[[ ${actual_migration} == "${expected_migration}" ]] || \
  tvt_fail "database migration ${actual_migration:-missing} does not match release head ${expected_migration}"
deployment_count_after="$(runuser -u postgres -- psql -d tvt -Atc 'SELECT count(*) FROM solution_deployments')"
[[ ${deployment_count_after} == "${deployment_count_before}" ]] || \
  tvt_fail "application upgrade changed the deployment count"
api_health="$(curl --fail --silent --show-error --max-time 15 http://127.0.0.1:8089/api/v1/health)"
python3 - "${api_health}" <<'PY'
import json
import sys

health = json.loads(sys.argv[1])
if health.get("status") != "healthy":
    raise SystemExit("TVT API is not healthy after upgrade")
PY
curl --fail --silent --show-error --max-time 15 http://127.0.0.1:18081/dashboard/ >/dev/null

python3 - "${INSTALL_STATE}" "${UPGRADE_REPORT}" "${RELEASE_VERSION}" \
  "$(basename "${previous_release}")" "${backup_directory}" "${expected_migration}" \
  "${deployment_count_after}" <<'PY'
import datetime
import json
import pathlib
import sys

state_path, report_path = map(pathlib.Path, sys.argv[1:3])
release, previous, backup, migration, deployment_count = sys.argv[3:]
now = datetime.datetime.now(datetime.timezone.utc).isoformat()
try:
    state = json.loads(state_path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    state = {"schema_version": 1}
state.update({
    "release_version": release,
    "previous_release_version": previous,
    "status": "installed",
    "updated_at": now,
    "completed_at": now,
})
state["stages"] = {
    "application_upgrade": {"status": "completed", "completed_at": now, "updated_at": now}
}
state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
report = {
    "schema_version": 1,
    "status": "installed",
    "release_version": release,
    "previous_release_version": previous,
    "database_migration": migration,
    "deployment_count": int(deployment_count),
    "backup_directory": backup,
    "completed_at": now,
}
report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
chmod 0640 "${INSTALL_STATE}" "${UPGRADE_REPORT}"
rollback_required=false
trap - ERR
tvt_log "application release ${RELEASE_VERSION} upgraded successfully"
tvt_log "rollback backup: ${backup_directory}"

)

tvt_op_probe_edge_hardware() (
# Source: scripts/tvt-hardware-inventory.py (probe/verify)
set -Eeuo pipefail
umask 022

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT=""
SSH_TARGET=""
SUDO_STDIN=false

usage() {
  echo "usage: scripts/tvt-edge-operations.sh probe-edge-hardware --output FILE [--ssh TARGET] [--sudo-stdin]" >&2
  echo "  Read-only edge probe. With --ssh, prompts for SSH and sudo credentials when needed." >&2
  echo "  --sudo-stdin reads a sudo password from stdin without allocating a remote TTY." >&2
}

while (($#)); do
  case "$1" in
    --output) OUTPUT="${2:-}"; shift 2 ;;
    --ssh) SSH_TARGET="${2:-}"; shift 2 ;;
    --sudo-stdin) SUDO_STDIN=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage; exit 2 ;;
  esac
done
[[ -n ${OUTPUT} ]] || { usage; exit 2; }
[[ ! -L ${OUTPUT} && ! -L ${OUTPUT}.sha256 ]] || {
  echo "refusing symlinked probe output" >&2
  exit 1
}
mkdir -p "$(dirname "${OUTPUT}")"
if [[ -n ${SSH_TARGET} ]]; then
  command -v ssh >/dev/null 2>&1 || { echo "ssh is required for --ssh" >&2; exit 1; }
  command -v base64 >/dev/null 2>&1 || { echo "base64 is required for --ssh" >&2; exit 1; }
  command -v tr >/dev/null 2>&1 || { echo "tr is required for --ssh" >&2; exit 1; }
  temporary="$(mktemp)"
  cleanup() { rm -f -- "${temporary}"; }
  trap cleanup EXIT
  encoded_probe="$(base64 -w 0 <"${REPO_ROOT}/scripts/tvt-hardware-inventory.py")"
  if ${SUDO_STDIN}; then
    ssh -o BatchMode=yes -o ConnectTimeout=15 \
      -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
      "${SSH_TARGET}" \
      "sudo -S -p '' python3 -c 'import base64; exec(compile(base64.b64decode(\"${encoded_probe}\"), \"tvt-hardware-inventory.py\", \"exec\"))' probe --output -" \
      >"${temporary}"
  else
    ssh -tt -o ConnectTimeout=15 -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
      "${SSH_TARGET}" \
      "sudo python3 -c 'import base64; exec(compile(base64.b64decode(\"${encoded_probe}\"), \"tvt-hardware-inventory.py\", \"exec\"))' probe --output -" \
      | tee /dev/tty | tr -d '\r' | sed -n '/^{/,$p' >"${temporary}"
  fi
  python3 "${REPO_ROOT}/scripts/tvt-hardware-inventory.py" \
    verify --inventory "${temporary}" >/dev/null
  install -m 0644 "${temporary}" "${OUTPUT}"
  digest="$(sha256sum "${OUTPUT}" | awk '{print $1}')"
  printf '%s  %s\n' "${digest}" "$(basename "${OUTPUT}")" >"${OUTPUT}.sha256"
  chmod 0644 "${OUTPUT}.sha256"
else
  python3 "${REPO_ROOT}/scripts/tvt-hardware-inventory.py" probe --output "${OUTPUT}"
fi
)

tvt_operations_usage() {
  cat >&2 <<'EOF'
usage: scripts/tvt-edge-operations.sh OPERATION [arguments]

operations:
  bootstrap-postgresql
  build-apt-closure
  build-release
  build-release-inputs
  configure-k3s-registry
  enable-k3s-secrets-encryption
  import-pipeline-traffic-image
  install-apexfabric-ui
  install-k3s-plane
  install-k3s-single-node
  install-local-registry
  install-pipeline-image-sync
  install-traffic-qualification
  install-tvt-hardware-drivers
  install-tvt-kubeconfig
  probe-edge-hardware
  publish-control-images
  publish-ui-image
  qualify-traffic-edge
  upgrade-application
  verify-k3s-plane
  verify-local-registry
  verify-pipeline-image-sync
  verify-release
  verify-docker-archive-tag
  verify-pipeline-image-inspect
  verify-traffic-qualification
EOF
}

operation="${1:-}"
[[ -n ${operation} ]] || { tvt_operations_usage; exit 2; }
shift
case "${operation}" in
  bootstrap-postgresql) tvt_op_bootstrap_postgresql "$@" ;;
  build-apt-closure) tvt_op_build_apt_closure "$@" ;;
  build-release) tvt_op_build_release "$@" ;;
  build-release-inputs) tvt_op_build_release_inputs "$@" ;;
  configure-k3s-registry) tvt_op_configure_k3s_registry "$@" ;;
  enable-k3s-secrets-encryption) tvt_op_enable_k3s_secrets_encryption "$@" ;;
  import-pipeline-traffic-image) tvt_op_import_pipeline_traffic_image "$@" ;;
  install-apexfabric-ui) tvt_op_install_apexfabric_ui "$@" ;;
  install-k3s-plane) tvt_op_install_k3s_plane "$@" ;;
  install-k3s-single-node) tvt_op_install_k3s_single_node "$@" ;;
  install-local-registry) tvt_op_install_local_registry "$@" ;;
  install-pipeline-image-sync) tvt_op_install_pipeline_image_sync "$@" ;;
  install-traffic-qualification) tvt_op_install_traffic_qualification "$@" ;;
  install-tvt-hardware-drivers) tvt_op_install_tvt_hardware_drivers "$@" ;;
  install-tvt-kubeconfig) tvt_op_install_tvt_kubeconfig "$@" ;;
  probe-edge-hardware) tvt_op_probe_edge_hardware "$@" ;;
  publish-control-images) tvt_op_publish_control_images "$@" ;;
  publish-ui-image) tvt_op_publish_ui_image "$@" ;;
  qualify-traffic-edge) tvt_op_qualify_traffic_edge "$@" ;;
  upgrade-application) tvt_op_upgrade_application "$@" ;;
  verify-k3s-plane) tvt_op_verify_k3s_plane "$@" ;;
  verify-local-registry) tvt_op_verify_local_registry "$@" ;;
  verify-pipeline-image-sync) tvt_op_verify_pipeline_image_sync "$@" ;;
  verify-release) tvt_op_verify_release "$@" ;;
  verify-docker-archive-tag) tvt_op_verify_docker_archive_tag "$@" ;;
  verify-pipeline-image-inspect) tvt_op_verify_pipeline_image_inspect "$@" ;;
  verify-traffic-qualification) tvt_op_verify_traffic_qualification "$@" ;;
  -h|--help|help) tvt_operations_usage ;;
  *) tvt_operations_usage; printf 'unknown operation: %s\n' "${operation}" >&2; exit 2 ;;
esac
