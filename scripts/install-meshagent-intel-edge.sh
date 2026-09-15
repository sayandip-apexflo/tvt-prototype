#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

SERVER_URL=""
MESH_ID_FILE=""
CA_CERT=""
AGENT_FILE=""
SETTINGS_FILE=""
AGENT_SHA256=""
SETTINGS_SHA256=""
REINSTALL=false
VERIFY_ONLY=false
TEMPORARY_DIRECTORY=""
readonly INSTALL_LOCK="${MESHAGENT_INSTALL_LOCK:-/run/lock/meshagent-install.lock}"

log() {
  printf '%s meshagent-edge: %s\n' "$(date --utc +'%Y-%m-%dT%H:%M:%SZ')" "$*"
}

fail() {
  log "ERROR: $*" >&2
  exit 1
}

cleanup() {
  if [[ -n ${TEMPORARY_DIRECTORY:-} ]]; then
    rm -rf -- "${TEMPORARY_DIRECTORY}"
    TEMPORARY_DIRECTORY=""
  fi
}

usage() {
  cat >&2 <<'EOF'
usage: sudo ./scripts/install-meshagent-intel-edge.sh [online options]
       sudo ./scripts/install-meshagent-intel-edge.sh [offline options]
       sudo ./scripts/install-meshagent-intel-edge.sh --verify-only

Installs the native Linux amd64 Mesh Agent on the Ubuntu 24.04 Intel edge box.

online options:
  --server-url HTTPS_URL       external MeshCentral URL seen by the edge
  --mesh-id-file FILE          root-readable file containing the device-group ID
  --ca-cert FILE               CA/certificate used to verify private MeshCentral TLS

offline options:
  --agent-file FILE            server-exported Linux x86-64 Mesh Agent binary
  --settings-file FILE         server-exported meshagent.msh file
  --agent-sha256 SHA256        required checksum for --agent-file
  --settings-sha256 SHA256     required checksum for --settings-file

common options:
  --reinstall                  replace an existing, differently enrolled agent
  --verify-only                verify the existing agent without changes
  -h, --help                   show this help

Use the exact device-group ID shown by MeshCentral's Add Agent dialog. Keeping
it in a file avoids shell expansion of characters such as '$'.
EOF
}

require_value() {
  [[ -n ${2:-} ]] || fail "$1 requires a value"
}

require_root() {
  [[ ${EUID} -eq 0 ]] || fail "run this command as root (for example, with sudo)"
}

validate_platform() {
  [[ $(uname -m) == x86_64 ]] || fail "the Intel edge agent requires Linux amd64"
  [[ -r /etc/os-release ]] || fail "/etc/os-release is missing"
  # shellcheck disable=SC1091
  source /etc/os-release
  [[ ${ID:-} == ubuntu && ${VERSION_ID:-} == 24.04 ]] || \
    fail "only Ubuntu 24.04 is supported (found ${ID:-unknown} ${VERSION_ID:-unknown})"
  [[ -d /run/systemd/system ]] || fail "systemd is required"
}

canonical_file() {
  local path="$1"
  [[ -f ${path} && ! -L ${path} ]] || fail "file is missing or symlinked: ${path}"
  (cd "$(dirname "${path}")" && printf '%s/%s\n' "$PWD" "$(basename "${path}")")
}

require_private_file() {
  local path="$1" label="$2" owner mode
  owner="$(stat -c '%u' "${path}")"
  mode="$(stat -c '%a' "${path}")"
  [[ ${owner} -eq 0 ]] || fail "${label} must be owned by root"
  (( (8#${mode} & 8#077) == 0 )) || fail "${label} must not be accessible by group or other users"
}

normalize_server_url() {
  python3 - "$1" <<'PY'
import sys
import urllib.parse

raw = sys.argv[1]
url = urllib.parse.urlsplit(raw)
if url.scheme != "https" or not url.hostname or url.username or url.password:
    raise SystemExit("server URL must be HTTPS without embedded credentials")
if url.query or url.fragment or url.path not in ("", "/"):
    raise SystemExit("server URL must not include a path, query, or fragment")
try:
    port = url.port
except ValueError as error:
    raise SystemExit(str(error))
host = url.hostname.lower()
if ":" in host:
    host = f"[{host}]"
print(f"https://{host}" + (f":{port}" if port is not None else ""))
PY
}

read_mesh_id() {
  python3 - "$1" <<'PY'
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
if path.stat().st_size > 4096:
    raise SystemExit("device-group ID file is too large")
raw = path.read_bytes()
if b"\x00" in raw:
    raise SystemExit("device-group ID contains a NUL byte")
value = raw.decode("utf-8").strip()
if len(value) <= 63 or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in value):
    raise SystemExit("device-group ID is invalid")
print(value)
PY
}

verify_sha256() {
  local file="$1" expected="$2" label="$3"
  [[ ${expected} =~ ^[0-9a-f]{64}$ ]] || fail "${label} checksum must be 64 lowercase hex characters"
  local actual
  actual="$(sha256sum "${file}" | awk '{print $1}')"
  [[ ${actual} == "${expected}" ]] || fail "${label} checksum mismatch"
}

validate_agent_binary() {
  python3 - "$1" <<'PY' || fail "agent is not a valid Linux x86-64 ELF binary"
import pathlib
import struct
import sys

path = pathlib.Path(sys.argv[1])
size = path.stat().st_size
if not 100_000 <= size <= 30_000_000:
    raise SystemExit(1)
header = path.read_bytes()[:20]
if len(header) < 20 or header[:4] != b"\x7fELF" or header[4] != 2 or header[5] != 1:
    raise SystemExit(1)
if struct.unpack("<H", header[18:20])[0] != 62:
    raise SystemExit(1)
PY
}

validate_settings() {
  python3 - "$1" "${SERVER_URL}" <<'PY' || fail "meshagent settings are invalid or advertise a different server"
import pathlib
import sys
import urllib.parse

path = pathlib.Path(sys.argv[1])
raw = path.read_bytes()
if not 100 <= len(raw) <= 65536 or b"\x00" in raw:
    raise SystemExit(1)
values = {}
for line in raw.decode("utf-8").splitlines():
    if "=" not in line:
        continue
    key, value = line.split("=", 1)
    if key in values:
        raise SystemExit(f"duplicate setting: {key}")
    values[key] = value
for required in ("MeshID", "ServerID", "MeshServer"):
    if not values.get(required):
        raise SystemExit(f"missing setting: {required}")
mesh_server = urllib.parse.urlsplit(values["MeshServer"])
if (
    mesh_server.scheme != "wss"
    or not mesh_server.hostname
    or mesh_server.username
    or mesh_server.password
    or mesh_server.path != "/agent.ashx"
    or mesh_server.query
    or mesh_server.fragment
):
    raise SystemExit("MeshServer is not wss")
mesh_id = values["MeshID"]
server_id = values["ServerID"]
if not mesh_id.startswith("0x") or len(mesh_id) < 66:
    raise SystemExit("mesh identity is invalid")
if any(ch not in "0123456789abcdefABCDEF" for ch in mesh_id[2:]):
    raise SystemExit("mesh identity is invalid")
if len(server_id) < 64 or any(ch not in "0123456789abcdefABCDEF" for ch in server_id):
    raise SystemExit("server or mesh identity is too short")
if sys.argv[2]:
    requested = urllib.parse.urlsplit(sys.argv[2])
    requested_port = requested.port or 443
    advertised_port = mesh_server.port or 443
    if requested.hostname.lower() != mesh_server.hostname.lower() or requested_port != advertised_port:
        raise SystemExit("advertised MeshServer does not match --server-url")
PY
}

normalize_settings() {
  local source="$1" destination="$2"
  sed '/^StartupType=/d' "${source}" >"${destination}"
  printf 'StartupType=1\n' >>"${destination}"
  chmod 0600 "${destination}"
}

settings_identity() {
  grep -E '^(MeshID|ServerID|MeshServer)=' "$1" | LC_ALL=C sort
}

find_installed_settings() {
  local -a roots=()
  [[ -d /usr/local/mesh_services/meshagent ]] && roots+=(/usr/local/mesh_services/meshagent)
  [[ -d /usr/local/mesh ]] && roots+=(/usr/local/mesh)
  ((${#roots[@]})) || return 0
  find "${roots[@]}" -maxdepth 4 -type f -name '*.msh' -print 2>/dev/null
}

find_agent_units() {
  systemctl list-unit-files --type=service --no-legend 'meshagent*.service' 2>/dev/null | awk '{print $1}'
}

verify_installed_agent() {
  local -a units settings
  mapfile -t units < <(find_agent_units)
  ((${#units[@]} > 0)) || fail "no installed Mesh Agent systemd unit was found"
  local active=false unit
  for unit in "${units[@]}"; do
    if systemctl is-active --quiet "${unit}"; then active=true; fi
    systemctl is-enabled --quiet "${unit}" || fail "${unit} is not enabled"
  done
  ${active} || fail "no Mesh Agent systemd unit is active"
  mapfile -t settings < <(find_installed_settings)
  ((${#settings[@]} == 1)) || fail "expected one installed meshagent settings file; found ${#settings[@]}"
  validate_settings "${settings[0]}"
  log "verification passed: Mesh Agent is enabled, active, and enrolled over WSS"
}

download_agent_inputs() {
  local directory="$1" mesh_id
  mesh_id="$(read_mesh_id "${MESH_ID_FILE}")" || fail "could not read a valid device-group ID"
  local -a curl_args=(--fail --show-error --silent --location --proto '=https' --tlsv1.2
    --connect-timeout 10 --max-time 180)
  if [[ -n ${CA_CERT} ]]; then curl_args+=(--cacert "${CA_CERT}"); fi
  log "downloading the Linux amd64 agent and enrollment settings over verified HTTPS"
  curl "${curl_args[@]}" --get --data-urlencode 'id=6' \
    --output "${directory}/meshagent" "${SERVER_URL}/meshagents"
  curl "${curl_args[@]}" --get --data-urlencode "id=${mesh_id}" \
    --output "${directory}/meshagent.msh.download" "${SERVER_URL}/meshsettings"
}

copy_offline_inputs() {
  local directory="$1"
  verify_sha256 "${AGENT_FILE}" "${AGENT_SHA256}" "agent"
  verify_sha256 "${SETTINGS_FILE}" "${SETTINGS_SHA256}" "settings"
  cp -- "${AGENT_FILE}" "${directory}/meshagent"
  cp -- "${SETTINGS_FILE}" "${directory}/meshagent.msh.download"
}

remove_existing_agent() {
  local settings="$1" binary
  binary="$(dirname "${settings}")/meshagent"
  [[ -x ${binary} ]] || fail "cannot locate installed Mesh Agent binary beside ${settings}"
  log "uninstalling the existing Mesh Agent before explicit re-enrollment"
  "${binary}" -fulluninstall
}

main() {
  while (($#)); do
    case "$1" in
      --server-url) require_value "$1" "${2:-}"; SERVER_URL="$2"; shift 2 ;;
      --mesh-id-file) require_value "$1" "${2:-}"; MESH_ID_FILE="$2"; shift 2 ;;
      --ca-cert) require_value "$1" "${2:-}"; CA_CERT="$2"; shift 2 ;;
      --agent-file) require_value "$1" "${2:-}"; AGENT_FILE="$2"; shift 2 ;;
      --settings-file) require_value "$1" "${2:-}"; SETTINGS_FILE="$2"; shift 2 ;;
      --agent-sha256) require_value "$1" "${2:-}"; AGENT_SHA256="$2"; shift 2 ;;
      --settings-sha256) require_value "$1" "${2:-}"; SETTINGS_SHA256="$2"; shift 2 ;;
      --reinstall) REINSTALL=true; shift ;;
      --verify-only) VERIFY_ONLY=true; shift ;;
      -h|--help) usage; exit 0 ;;
      *) usage; fail "unknown option: $1" ;;
    esac
  done
  require_root
  validate_platform
  command -v python3 >/dev/null || fail "python3 is required"
  command -v systemctl >/dev/null || fail "systemctl is required"
  if [[ -n ${SERVER_URL} ]]; then
    SERVER_URL="$(normalize_server_url "${SERVER_URL}")" || fail "invalid --server-url"
  fi
  if ${VERIFY_ONLY}; then
    verify_installed_agent
    exit 0
  fi

  local online=false offline=false
  [[ -n ${SERVER_URL} || -n ${MESH_ID_FILE} || -n ${CA_CERT} ]] && online=true
  [[ -n ${AGENT_FILE} || -n ${SETTINGS_FILE} || -n ${AGENT_SHA256} || -n ${SETTINGS_SHA256} ]] && offline=true
  ! (${online} && ${offline}) || fail "online and offline installation options cannot be combined"
  (${online} || ${offline}) || { usage; fail "select online or offline installation inputs"; }
  if ${online}; then
    [[ -n ${SERVER_URL} && -n ${MESH_ID_FILE} ]] || fail "online mode requires --server-url and --mesh-id-file"
    command -v curl >/dev/null || fail "curl is required for online installation"
    MESH_ID_FILE="$(canonical_file "${MESH_ID_FILE}")"
    require_private_file "${MESH_ID_FILE}" "device-group ID file"
    if [[ -n ${CA_CERT} ]]; then CA_CERT="$(canonical_file "${CA_CERT}")"; fi
  else
    [[ -n ${AGENT_FILE} && -n ${SETTINGS_FILE} && -n ${AGENT_SHA256} && -n ${SETTINGS_SHA256} ]] || \
      fail "offline mode requires both files and both SHA-256 values"
    AGENT_FILE="$(canonical_file "${AGENT_FILE}")"
    SETTINGS_FILE="$(canonical_file "${SETTINGS_FILE}")"
  fi

  install -d -m 0755 "$(dirname "${INSTALL_LOCK}")"
  exec 8>"${INSTALL_LOCK}"
  flock --wait 0 8 || fail "another Mesh Agent installation is running"
  TEMPORARY_DIRECTORY="$(mktemp -d /var/tmp/meshagent-install.XXXXXX)"
  trap cleanup EXIT
  if ${online}; then download_agent_inputs "${TEMPORARY_DIRECTORY}"; else copy_offline_inputs "${TEMPORARY_DIRECTORY}"; fi
  validate_agent_binary "${TEMPORARY_DIRECTORY}/meshagent"
  validate_settings "${TEMPORARY_DIRECTORY}/meshagent.msh.download"
  normalize_settings "${TEMPORARY_DIRECTORY}/meshagent.msh.download" "${TEMPORARY_DIRECTORY}/meshagent.msh"
  chmod 0700 "${TEMPORARY_DIRECTORY}/meshagent"

  local -a installed_settings
  mapfile -t installed_settings < <(find_installed_settings)
  if ((${#installed_settings[@]} > 1)); then
    fail "multiple installed Mesh Agent settings files were found; reconcile them manually"
  elif ((${#installed_settings[@]} == 1)); then
    if diff -q <(settings_identity "${installed_settings[0]}") \
      <(settings_identity "${TEMPORARY_DIRECTORY}/meshagent.msh") >/dev/null; then
      local -a existing_units
      mapfile -t existing_units < <(find_agent_units)
      if ((${#existing_units[@]} > 0)); then
        log "the existing Mesh Agent enrollment already matches"
        local existing_unit
        for existing_unit in "${existing_units[@]}"; do
          systemctl enable --now "${existing_unit}"
        done
        verify_installed_agent
        exit 0
      fi
      log "the enrollment matches but its systemd service is missing; repairing it"
    fi
    if [[ $(settings_identity "${installed_settings[0]}") != \
      "$(settings_identity "${TEMPORARY_DIRECTORY}/meshagent.msh")" ]]; then
      ${REINSTALL} || fail "an agent is enrolled with a different server/group; use --reinstall to replace it"
      remove_existing_agent "${installed_settings[0]}"
    fi
  fi

  log "installing the Mesh Agent as a system service"
  (cd "${TEMPORARY_DIRECTORY}" && ./meshagent -fullinstall --copy-msh=1)
  systemctl daemon-reload
  local unit
  while IFS= read -r unit; do systemctl enable --now "${unit}"; done < <(find_agent_units)
  verify_installed_agent
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
  main "$@"
fi
