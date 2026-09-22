#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
readonly FLEET_CONTROLLER="${SCRIPT_DIR}/tvt-edge-fleet.sh"

usage() {
  cat >&2 <<'EOF'
usage:
  scripts/tvt-edge-single.sh install \
    --ssh-target USER@HOST --site-key ID --edge-id ID --display-name NAME [options]
  scripts/tvt-edge-single.sh resume --edge-id ID [--version VERSION] [options]
  scripts/tvt-edge-single.sh status --edge-id ID [--version VERSION] [options]

install options:
  --timezone ZONE              IANA timezone (default: Asia/Kolkata)
  --version VERSION            defaults to the canonical repository version
  --source-commit SHA          defaults to the checked-out commit
  --output-directory DIR       release/state output outside the Git checkout
  --operator-directory DIR     private generated manifest/site-config directory
  --skip-tests                 development only; skip release source gates
  --allow-dirty-source         development only; permit a dirty checkout

resume/status options:
  --version VERSION            selects the saved run (default: repository version)
  --operator-directory DIR     selects a non-default saved run directory

The install command probes, builds, transfers, prepares, reboots, installs,
and verifies one edge. Resume continues the same saved run after a corrected
failure. Site configuration contains identifiers only; credentials are never
accepted by this command.
EOF
}

fail() {
  printf 'tvt-edge-single: ERROR: %s\n' "$*" >&2
  exit 1
}

require_value() {
  local option="$1" value="${2:-}"
  [[ -n ${value} ]] || fail "${option} requires a value"
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "required command is unavailable: $1"
}

create_owned_directory() {
  local directory="$1"
  if mkdir -p -- "${directory}" 2>/dev/null; then
    chmod 0750 "${directory}"
    return
  fi
  require_command sudo
  printf 'Creating operator output directory %s with sudo.\n' "${directory}" >&2
  sudo install -d -o "$(id -u)" -g "$(id -g)" -m 0750 "${directory}"
}

dns_safe() {
  [[ $1 =~ ^[a-z0-9]([a-z0-9.-]{0,61}[a-z0-9])?$ ]]
}

canonical_version() {
  python3 "${REPO_ROOT}/scripts/tvt-version.py" --check
}

default_operator_directory() {
  local edge_id="$1" version="$2"
  printf '%s/.tvt/single-edge/%s/%s\n' "${REPO_ROOT}" "${edge_id}" "${version}"
}

COMMAND="${1:-}"
case "${COMMAND}" in
  install|resume|status) shift ;;
  -h|--help) usage; exit 0 ;;
  *) usage; exit 2 ;;
esac

SSH_TARGET=""
SITE_KEY=""
EDGE_ID=""
DISPLAY_NAME=""
TIMEZONE="Asia/Kolkata"
VERSION=""
SOURCE_COMMIT=""
OUTPUT_DIRECTORY=""
OPERATOR_DIRECTORY=""
SKIP_TESTS=false
ALLOW_DIRTY_SOURCE=false

while (($#)); do
  case "$1" in
    --ssh-target) require_value "$1" "${2:-}"; SSH_TARGET="$2"; shift 2 ;;
    --site-key) require_value "$1" "${2:-}"; SITE_KEY="$2"; shift 2 ;;
    --edge-id) require_value "$1" "${2:-}"; EDGE_ID="$2"; shift 2 ;;
    --display-name) require_value "$1" "${2:-}"; DISPLAY_NAME="$2"; shift 2 ;;
    --timezone) require_value "$1" "${2:-}"; TIMEZONE="$2"; shift 2 ;;
    --version) require_value "$1" "${2:-}"; VERSION="$2"; shift 2 ;;
    --source-commit) require_value "$1" "${2:-}"; SOURCE_COMMIT="$2"; shift 2 ;;
    --output-directory) require_value "$1" "${2:-}"; OUTPUT_DIRECTORY="$2"; shift 2 ;;
    --operator-directory) require_value "$1" "${2:-}"; OPERATOR_DIRECTORY="$2"; shift 2 ;;
    --skip-tests) SKIP_TESTS=true; shift ;;
    --allow-dirty-source) ALLOW_DIRTY_SOURCE=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage; fail "unknown option: $1" ;;
  esac
done

require_command python3
require_command jq
[[ -x ${FLEET_CONTROLLER} ]] || fail "fleet controller is not executable: ${FLEET_CONTROLLER}"

REPOSITORY_VERSION="$(canonical_version)"
VERSION="${VERSION:-${REPOSITORY_VERSION}}"
[[ ${VERSION} == "${REPOSITORY_VERSION}" ]] || \
  fail "requested version ${VERSION} does not match repository version ${REPOSITORY_VERSION}"
[[ -n ${EDGE_ID} ]] || fail "--edge-id is required"
dns_safe "${EDGE_ID}" || fail "--edge-id must be a lowercase DNS-safe identifier of at most 63 characters"
OPERATOR_DIRECTORY="${OPERATOR_DIRECTORY:-$(default_operator_directory "${EDGE_ID}" "${VERSION}")}"
OPERATOR_DIRECTORY="$(python3 - "${OPERATOR_DIRECTORY}" <<'PY'
import pathlib, sys
print(pathlib.Path(sys.argv[1]).resolve())
PY
)"
RUN_FILE="${OPERATOR_DIRECTORY}/run.json"

if [[ ${COMMAND} == status ]]; then
  [[ -f ${RUN_FILE} && ! -L ${RUN_FILE} ]] || fail "saved run does not exist: ${RUN_FILE}"
  STATE_DIRECTORY="$(jq -er '.state_directory' "${RUN_FILE}")" || fail "saved run metadata is invalid"
  exec "${FLEET_CONTROLLER}" status --state-directory "${STATE_DIRECTORY}"
fi

for dependency in bash curl docker git git-lfs gzip npm rsync sha256sum ssh tar; do
  require_command "${dependency}"
done

if [[ ${COMMAND} == resume ]]; then
  [[ -f ${RUN_FILE} && ! -L ${RUN_FILE} ]] || fail "saved run does not exist: ${RUN_FILE}"
  FLEET_MANIFEST="$(jq -er '.fleet_manifest' "${RUN_FILE}")" || fail "saved run metadata is invalid"
  STATE_DIRECTORY="$(jq -er '.state_directory' "${RUN_FILE}")" || fail "saved run metadata is invalid"
  STORED_EDGE_ID="$(jq -er '.edge_id' "${RUN_FILE}")" || fail "saved run metadata is invalid"
  STORED_VERSION="$(jq -er '.version' "${RUN_FILE}")" || fail "saved run metadata is invalid"
  STORED_COMMIT="$(jq -er '.source_commit' "${RUN_FILE}")" || fail "saved run metadata is invalid"
  [[ ${STORED_EDGE_ID} == "${EDGE_ID}" ]] || fail "saved run belongs to edge ${STORED_EDGE_ID}"
  [[ ${STORED_VERSION} == "${VERSION}" ]] || fail "saved run belongs to version ${STORED_VERSION}"
  [[ ${STORED_COMMIT} == "$(git -C "${REPO_ROOT}" rev-parse HEAD)" ]] || \
    fail "resume requires the saved source commit ${STORED_COMMIT}"
  [[ -f ${FLEET_MANIFEST} && ! -L ${FLEET_MANIFEST} ]] || fail "saved fleet manifest is missing"
  exec "${FLEET_CONTROLLER}" resume \
    --fleet "${FLEET_MANIFEST}" \
    --state-directory "${STATE_DIRECTORY}" \
    --concurrency 1
fi

[[ "$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')" == 3.12 ]] || \
  fail "Python 3.12 is required on the release workstation"
docker info >/dev/null 2>&1 || \
  fail "the release workstation Docker daemon is unavailable to the current user"
if [[ ! -x ${REPO_ROOT}/.venv/bin/python ]]; then
  printf 'Creating the repository Python 3.12 build environment...\n'
  python3 -m venv "${REPO_ROOT}/.venv"
  "${REPO_ROOT}/.venv/bin/pip" install -e "${REPO_ROOT}[dev]"
elif [[ "$("${REPO_ROOT}/.venv/bin/python" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')" != 3.12 ]]; then
  fail "the repository .venv must use Python 3.12"
fi

[[ -n ${SSH_TARGET} ]] || fail "--ssh-target is required"
[[ ${SSH_TARGET} != -* && ${SSH_TARGET} != *[[:space:]]* ]] || \
  fail "--ssh-target must be one SSH target token, not an option or command"
[[ -n ${SITE_KEY} ]] || fail "--site-key is required"
dns_safe "${SITE_KEY}" || fail "--site-key must be a lowercase DNS-safe identifier of at most 63 characters"
[[ -n ${DISPLAY_NAME} && ${#DISPLAY_NAME} -le 160 ]] || \
  fail "--display-name must contain between 1 and 160 characters"
[[ ${DISPLAY_NAME} != *$'\n'* && ${DISPLAY_NAME} != *$'\t'* ]] || \
  fail "--display-name must be a single-line value"
python3 - "${TIMEZONE}" <<'PY' || fail "--timezone must be a valid IANA timezone"
import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
try:
    ZoneInfo(sys.argv[1])
except (ValueError, ZoneInfoNotFoundError):
    raise SystemExit(1)
PY

SOURCE_COMMIT="${SOURCE_COMMIT:-$(git -C "${REPO_ROOT}" rev-parse HEAD)}"
[[ ${SOURCE_COMMIT} =~ ^[0-9a-f]{40}$ ]] || fail "--source-commit must be a full lowercase Git SHA"
[[ ${SOURCE_COMMIT} == "$(git -C "${REPO_ROOT}" rev-parse HEAD)" ]] || \
  fail "--source-commit must equal the checked-out commit"
if ! ${ALLOW_DIRTY_SOURCE} && [[ -n $(git -C "${REPO_ROOT}" status --porcelain) ]]; then
  fail "production installation requires a clean source checkout"
fi

OUTPUT_DIRECTORY="${OUTPUT_DIRECTORY:-/srv/tvt-release/output/single-edge-${VERSION}-${EDGE_ID}}"
OUTPUT_DIRECTORY="$(python3 - "${OUTPUT_DIRECTORY}" <<'PY'
import pathlib, sys
print(pathlib.Path(sys.argv[1]).resolve())
PY
)"
case "${OUTPUT_DIRECTORY%/}/" in
  "${REPO_ROOT%/}/"*) fail "--output-directory must be outside the Git checkout" ;;
esac
[[ ! -e ${RUN_FILE} ]] || fail "saved run already exists; use the resume command: ${RUN_FILE}"
create_owned_directory "${OUTPUT_DIRECTORY}"
install -d -m 0700 "${OPERATOR_DIRECTORY}"

SITE_CONFIG="${OPERATOR_DIRECTORY}/site-config.json"
FLEET_MANIFEST="${OPERATOR_DIRECTORY}/fleet.json"
STATE_DIRECTORY="${OUTPUT_DIRECTORY}/fleet-state"

printf 'Checking SSH key access and non-interactive sudo on %s...\n' "${SSH_TARGET}"
ssh \
  -o BatchMode=yes \
  -o ConnectTimeout=15 \
  -o ServerAliveInterval=15 \
  -o ServerAliveCountMax=3 \
  "${SSH_TARGET}" 'sudo -n true' \
  || fail "SSH key access and non-interactive sudo are required"

jq -n \
  --arg site_key "${SITE_KEY}" \
  --arg edge_id "${EDGE_ID}" \
  --arg display_name "${DISPLAY_NAME}" \
  --arg timezone "${TIMEZONE}" \
  '{site_key: $site_key, edge_id: $edge_id, display_name: $display_name, timezone: $timezone}' \
  >"${SITE_CONFIG}"

jq -n \
  --arg id "${EDGE_ID}" \
  --arg ssh_target "${SSH_TARGET}" \
  --arg site_config "${SITE_CONFIG}" \
  '{edges: [{id: $id, ssh_target: $ssh_target, site_config: $site_config}]}' \
  >"${FLEET_MANIFEST}"

jq -n \
  --arg edge_id "${EDGE_ID}" \
  --arg version "${VERSION}" \
  --arg source_commit "${SOURCE_COMMIT}" \
  --arg output_directory "${OUTPUT_DIRECTORY}" \
  --arg state_directory "${STATE_DIRECTORY}" \
  --arg fleet_manifest "${FLEET_MANIFEST}" \
  '{
    schema_version: 1,
    edge_id: $edge_id,
    version: $version,
    source_commit: $source_commit,
    output_directory: $output_directory,
    state_directory: $state_directory,
    fleet_manifest: $fleet_manifest
  }' >"${RUN_FILE}"
chmod 0600 "${SITE_CONFIG}" "${FLEET_MANIFEST}" "${RUN_FILE}"

arguments=(
  install
  --fleet "${FLEET_MANIFEST}"
  --version "${VERSION}"
  --source-commit "${SOURCE_COMMIT}"
  --output-directory "${OUTPUT_DIRECTORY}"
  --concurrency 1
)
${SKIP_TESTS} && arguments+=(--skip-tests)
${ALLOW_DIRTY_SOURCE} && arguments+=(--allow-dirty-source)

printf 'Installing TVT edge %s from commit %s.\n' "${EDGE_ID}" "${SOURCE_COMMIT}"
printf 'Operator state: %s\n' "${OPERATOR_DIRECTORY}"
printf 'Release output: %s\n' "${OUTPUT_DIRECTORY}"
exec "${FLEET_CONTROLLER}" "${arguments[@]}"
