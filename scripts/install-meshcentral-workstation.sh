#!/usr/bin/env bash
set -Eeuo pipefail
umask 027

readonly DEFAULT_MESHCENTRAL_VERSION="1.2.5"
readonly DEFAULT_NODE_VERSION="24.21.0"
readonly SERVICE_NAME="meshcentral.service"
readonly SERVICE_USER="meshcentral"
readonly SERVICE_GROUP="meshcentral"
readonly APPLICATION_ROOT="${MESHCENTRAL_APPLICATION_ROOT:-/opt/meshcentral}"
readonly NODE_ROOT="${MESHCENTRAL_NODE_ROOT:-/opt/nodejs}"
readonly STATE_ROOT="${MESHCENTRAL_STATE_ROOT:-/var/lib/meshcentral}"
readonly UNIT_PATH="${MESHCENTRAL_UNIT_PATH:-/etc/systemd/system/meshcentral.service}"
readonly INSTALL_LOCK="${MESHCENTRAL_INSTALL_LOCK:-/run/lock/meshcentral-install.lock}"

SERVER_NAME=""
MESHCENTRAL_VERSION="${MESHCENTRAL_VERSION:-${DEFAULT_MESHCENTRAL_VERSION}}"
NODE_VERSION="${MESHCENTRAL_NODE_VERSION:-${DEFAULT_NODE_VERSION}}"
NODE_SHA256="${MESHCENTRAL_NODE_SHA256:-}"
HTTPS_PORT=443
HTTP_PORT=80
HTTPS_PORT_SET=false
VERIFY_ONLY=false

log() {
  printf '%s meshcentral-workstation: %s\n' "$(date --utc +'%Y-%m-%dT%H:%M:%SZ')" "$*"
}

fail() {
  log "ERROR: $*" >&2
  exit 1
}

usage() {
  cat >&2 <<'EOF'
usage: sudo ./scripts/install-meshcentral-workstation.sh --server-name NAME [options]

Installs a pinned native MeshCentral server on Ubuntu 24.04 amd64.

options:
  --server-name NAME              stable DNS name or IP advertised to agents
  --meshcentral-version VERSION   exact npm version (default: 1.2.5)
  --node-version VERSION          exact Node.js LTS version (default: 24.21.0)
  --node-sha256 SHA256            optional expected Node archive checksum
  --https-port PORT               HTTPS/UI/agent port (default: 443)
  --http-port PORT                HTTP redirect port; 0 disables it (default: 80)
  --verify-only                   verify the installed service without changes
  -h, --help                      show this help

The first MeshCentral user must be created promptly in the web UI; it becomes
the administrator. The installer deliberately does not accept passwords.
EOF
}

require_root() {
  [[ ${EUID} -eq 0 ]] || fail "run this command as root (for example, with sudo)"
}

require_value() {
  [[ -n ${2:-} ]] || fail "$1 requires a value"
}

validate_version() {
  local label="$1" value="$2"
  [[ ${value} =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "${label} must be an exact numeric version"
}

validate_port() {
  local label="$1" value="$2" allow_zero="$3"
  [[ ${value} =~ ^[0-9]+$ ]] || fail "${label} must be an integer"
  if [[ ${allow_zero} == true && ${value} -eq 0 ]]; then return; fi
  (( value >= 1 && value <= 65535 )) || fail "${label} must be between 1 and 65535"
}

validate_server_name() {
  python3 - "${SERVER_NAME}" <<'PY' || fail "invalid --server-name"
import ipaddress
import re
import sys

value = sys.argv[1]
if not value or len(value) > 253 or any(ch.isspace() for ch in value):
    raise SystemExit(1)
try:
    ipaddress.ip_address(value)
except ValueError:
    labels = value.rstrip(".").split(".")
    if not labels or any(
        not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
        for label in labels
    ):
        raise SystemExit(1)
PY
}

validate_platform() {
  [[ $(uname -m) == x86_64 ]] || fail "only Linux amd64 workstations are supported"
  [[ -r /etc/os-release ]] || fail "/etc/os-release is missing"
  # shellcheck disable=SC1091
  source /etc/os-release
  [[ ${ID:-} == ubuntu && ${VERSION_ID:-} == 24.04 ]] || \
    fail "only Ubuntu 24.04 is supported (found ${ID:-unknown} ${VERSION_ID:-unknown})"
  [[ -d /run/systemd/system ]] || fail "systemd is required"
}

install_prerequisites() {
  log "installing workstation prerequisites"
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates curl iproute2 python3 xz-utils
}

install_node() {
  local destination="${NODE_ROOT}/node-v${NODE_VERSION}"
  if [[ -x ${destination}/bin/node ]] && \
     [[ $(${destination}/bin/node --version) == "v${NODE_VERSION}" ]]; then
    log "Node.js ${NODE_VERSION} is already installed"
  else
    [[ ! -e ${destination} ]] || fail "existing Node.js directory is incomplete: ${destination}"
    local temporary archive_name base_url archive checksum_file expected actual
    temporary="$(mktemp -d /tmp/meshcentral-node.XXXXXX)"
    archive_name="node-v${NODE_VERSION}-linux-x64.tar.xz"
    base_url="https://nodejs.org/download/release/v${NODE_VERSION}"
    archive="${temporary}/${archive_name}"
    checksum_file="${temporary}/SHASUMS256.txt"
    trap 'rm -rf -- "${temporary}"' RETURN
    log "downloading Node.js ${NODE_VERSION} from nodejs.org"
    curl --fail --show-error --silent --location --proto '=https' --tlsv1.2 \
      --output "${checksum_file}" "${base_url}/SHASUMS256.txt"
    curl --fail --show-error --silent --location --proto '=https' --tlsv1.2 \
      --output "${archive}" "${base_url}/${archive_name}"
    mapfile -t matches < <(awk -v filename="${archive_name}" '$2 == filename { print $1 }' "${checksum_file}")
    [[ ${#matches[@]} -eq 1 && ${matches[0]} =~ ^[0-9a-f]{64}$ ]] || \
      fail "Node.js checksum manifest has no unique entry for ${archive_name}"
    expected="${matches[0]}"
    if [[ -n ${NODE_SHA256} ]]; then
      [[ ${NODE_SHA256} =~ ^[0-9a-f]{64}$ ]] || fail "--node-sha256 must be 64 lowercase hex characters"
      [[ ${NODE_SHA256} == "${expected}" ]] || fail "supplied Node.js checksum disagrees with the release manifest"
    fi
    actual="$(sha256sum "${archive}" | awk '{print $1}')"
    [[ ${actual} == "${expected}" ]] || fail "Node.js archive checksum mismatch"
    install -d -o root -g root -m 0755 "${NODE_ROOT}"
    local staging="${NODE_ROOT}/.node-v${NODE_VERSION}.$$"
    install -d -o root -g root -m 0755 "${staging}"
    tar -xJf "${archive}" --strip-components=1 -C "${staging}"
    chown -R root:root "${staging}"
    chmod -R go-w "${staging}"
    [[ $(${staging}/bin/node --version) == "v${NODE_VERSION}" ]] || fail "installed Node.js version is incorrect"
    mv -- "${staging}" "${destination}"
    rm -rf -- "${temporary}"
    trap - RETURN
  fi
  local link="${NODE_ROOT}/.current.$$"
  ln -s "${destination}" "${link}"
  mv -Tf -- "${link}" "${NODE_ROOT}/current"
}

ensure_service_account() {
  if getent passwd "${SERVICE_USER}" >/dev/null; then
    [[ $(id -g -n "${SERVICE_USER}") == "${SERVICE_GROUP}" ]] || \
      fail "existing ${SERVICE_USER} account does not use group ${SERVICE_GROUP}"
  else
    getent group "${SERVICE_GROUP}" >/dev/null || groupadd --system "${SERVICE_GROUP}"
    useradd --system --gid "${SERVICE_GROUP}" --home-dir "${STATE_ROOT}" \
      --shell /usr/sbin/nologin "${SERVICE_USER}"
  fi
  install -d -o root -g root -m 0755 "${APPLICATION_ROOT}" "${APPLICATION_ROOT}/releases"
  install -d -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" -m 0750 "${STATE_ROOT}"
  install -d -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" -m 0700 \
    "${STATE_ROOT}/meshcentral-data"
  install -d -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" -m 0750 \
    "${STATE_ROOT}/meshcentral-files" "${STATE_ROOT}/meshcentral-backups" \
    "${STATE_ROOT}/npm-cache"
}

install_meshcentral_release() {
  local release="${APPLICATION_ROOT}/releases/${MESHCENTRAL_VERSION}"
  local installed=""
  if [[ -f ${release}/node_modules/meshcentral/package.json ]]; then
    installed="$(${NODE_ROOT}/current/bin/node -p \
      "require('${release}/node_modules/meshcentral/package.json').version" 2>/dev/null || true)"
  fi
  if [[ ${installed} == "${MESHCENTRAL_VERSION}" ]]; then
    log "MeshCentral ${MESHCENTRAL_VERSION} is already installed"
  else
    [[ ! -e ${release} ]] || fail "existing MeshCentral release is incomplete: ${release}"
    local staging="${APPLICATION_ROOT}/releases/.${MESHCENTRAL_VERSION}.$$"
    install -d -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" -m 0750 "${staging}"
    log "installing MeshCentral ${MESHCENTRAL_VERSION} from npm"
    runuser --user "${SERVICE_USER}" -- env \
      HOME="${STATE_ROOT}" \
      npm_config_cache="${STATE_ROOT}/npm-cache" \
      PATH="${NODE_ROOT}/current/bin:/usr/bin:/bin" \
      "${NODE_ROOT}/current/bin/npm" install --prefix "${staging}" \
      --omit=dev --save-exact --no-audit --no-fund "meshcentral@${MESHCENTRAL_VERSION}"
    installed="$(${NODE_ROOT}/current/bin/node -p \
      "require('${staging}/node_modules/meshcentral/package.json').version")"
    [[ ${installed} == "${MESHCENTRAL_VERSION}" ]] || fail "npm installed MeshCentral ${installed}, not ${MESHCENTRAL_VERSION}"
    mv -- "${staging}" "${release}"
  fi
  # Application code is immutable to the service account, but every parent
  # directory and module must remain readable/traversable by its service group.
  # This is also deliberately outside the install branch so a normal rerun
  # repairs permissions created by an older version of this installer.
  chown -R "root:${SERVICE_GROUP}" "${release}"
  chmod -R g+rX,o-rwx,go-w "${release}"
  local name
  for name in meshcentral-data meshcentral-files meshcentral-backups; do
    if [[ -e ${release}/${name} || -L ${release}/${name} ]]; then
      [[ -L ${release}/${name} && $(readlink -f "${release}/${name}") == "${STATE_ROOT}/${name}" ]] || \
        fail "refusing unexpected release path: ${release}/${name}"
    else
      ln -s "${STATE_ROOT}/${name}" "${release}/${name}"
    fi
  done
}

write_or_validate_config() {
  local config="${STATE_ROOT}/meshcentral-data/config.json"
  if [[ -e ${config} ]]; then
    [[ -f ${config} && ! -L ${config} ]] || fail "MeshCentral config must be a regular, non-symlink file"
    python3 - "${config}" "${SERVER_NAME}" "${HTTPS_PORT}" "${HTTP_PORT}" <<'PY' || \
      fail "existing config.json conflicts with requested server identity or ports"
import json
import pathlib
import sys

path, name, https_port, http_port = sys.argv[1:]
document = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
settings = document.get("settings", {})
if settings.get("cert") != name:
    raise SystemExit("settings.cert mismatch")
if settings.get("port") != int(https_port):
    raise SystemExit("settings.port mismatch")
if settings.get("redirPort") != int(http_port):
    raise SystemExit("settings.redirPort mismatch")
PY
    log "preserving existing MeshCentral config"
    return
  fi
  local temporary
  temporary="$(mktemp "${STATE_ROOT}/meshcentral-data/.config.XXXXXX")"
  python3 - "${temporary}" "${SERVER_NAME}" "${HTTPS_PORT}" "${HTTP_PORT}" \
    "${STATE_ROOT}/meshcentral-backups" <<'PY'
import json
import pathlib
import sys

output, name, https_port, http_port, backup_path = sys.argv[1:]
document = {
    "$schema": "https://raw.githubusercontent.com/Ylianst/MeshCentral/master/meshcentral-config-schema.json",
    "settings": {
        "cert": name,
        "port": int(https_port),
        "redirPort": int(http_port),
        "exactPorts": True,
        "WANonly": True,
        "selfUpdate": False,
        "autoBackup": {
            "backupIntervalHours": 24,
            "keepLastDaysBackup": 14,
            "backupPath": backup_path,
            "backupOtherFolders": True,
        },
    },
    "domains": {
        "": {
            "title": "TVT MeshCentral",
            "newAccounts": False,
            "minify": True,
        }
    },
}
pathlib.Path(output).write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
PY
  chown "${SERVICE_USER}:${SERVICE_GROUP}" "${temporary}"
  chmod 0640 "${temporary}"
  mv -f -- "${temporary}" "${config}"
  log "created MeshCentral configuration"
}

install_systemd_unit() {
  local temporary
  temporary="$(mktemp /tmp/meshcentral.service.XXXXXX)"
  cat >"${temporary}" <<EOF
[Unit]
Description=MeshCentral remote management server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_GROUP}
WorkingDirectory=${APPLICATION_ROOT}/current
Environment=NODE_ENV=production
ExecStart=${NODE_ROOT}/current/bin/node ${APPLICATION_ROOT}/current/node_modules/meshcentral
Restart=on-failure
RestartSec=5s
TimeoutStopSec=30s
UMask=0077
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ProtectControlGroups=true
ProtectKernelModules=true
ProtectKernelTunables=true
ProtectHostname=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
RestrictNamespaces=true
RestrictRealtime=true
SystemCallArchitectures=native
ReadWritePaths=${STATE_ROOT}

[Install]
WantedBy=multi-user.target
EOF
  chmod 0644 "${temporary}"
  chown root:root "${temporary}"
  if [[ ! -f ${UNIT_PATH} ]] || ! cmp -s "${temporary}" "${UNIT_PATH}"; then
    install -o root -g root -m 0644 "${temporary}" "${UNIT_PATH}"
  fi
  rm -f -- "${temporary}"
  systemctl daemon-reload
}

port_is_listening() {
  local port="$1"
  ss -H -ltn | awk '{print $4}' | grep -Eq "(^|[.:])${port}$"
}

assert_fresh_ports_available() {
  if systemctl is-active --quiet "${SERVICE_NAME}"; then return; fi
  port_is_listening "${HTTPS_PORT}" && fail "HTTPS port ${HTTPS_PORT} is already in use"
  if (( HTTP_PORT != 0 )); then
    port_is_listening "${HTTP_PORT}" && fail "HTTP port ${HTTP_PORT} is already in use"
  fi
}

activate_release() {
  local release="${APPLICATION_ROOT}/releases/${MESHCENTRAL_VERSION}"
  local link="${APPLICATION_ROOT}/.current.${MESHCENTRAL_VERSION}.$$"
  ln -s "${release}" "${link}"
  mv -Tf -- "${link}" "${APPLICATION_ROOT}/current"
}

wait_for_service() {
  local attempt response
  for attempt in $(seq 1 30); do
    if systemctl is-active --quiet "${SERVICE_NAME}" && port_is_listening "${HTTPS_PORT}"; then
      # Require MeshCentral's application-level response, not merely an HTTPS
      # response: a proxy or another listener can also return a valid 4xx.
      # Agent downloads never disable certificate validation; --insecure is
      # limited to this local first-run readiness probe.
      response="$(curl --insecure --fail --silent --show-error --max-time 5 \
        --noproxy '*' --header "Host: ${SERVER_NAME}:${HTTPS_PORT}" \
        "https://127.0.0.1:${HTTPS_PORT}/health.ashx" 2>/dev/null || true)"
      [[ ${response} == "ok" ]] && return 0
    fi
    sleep 1
  done
  return 1
}

load_installed_https_port() {
  local config="${STATE_ROOT}/meshcentral-data/config.json"
  [[ -f ${config} ]] || return
  HTTPS_PORT="$(python3 - "${config}" <<'PY'
import json
import pathlib
import sys

document = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
print(document.get("settings", {}).get("port", 443))
PY
)"
}

verify_installation() {
  [[ -x ${NODE_ROOT}/current/bin/node ]] || fail "managed Node.js runtime is missing"
  [[ -f ${APPLICATION_ROOT}/current/node_modules/meshcentral/package.json ]] || \
    fail "MeshCentral installation is missing"
  [[ -f ${STATE_ROOT}/meshcentral-data/config.json ]] || fail "MeshCentral config is missing"
  systemctl is-enabled --quiet "${SERVICE_NAME}" || fail "${SERVICE_NAME} is not enabled"
  systemctl is-active --quiet "${SERVICE_NAME}" || fail "${SERVICE_NAME} is not active"
  port_is_listening "${HTTPS_PORT}" || fail "MeshCentral is not listening on HTTPS port ${HTTPS_PORT}"
  local installed
  installed="$(${NODE_ROOT}/current/bin/node -p \
    "require('${APPLICATION_ROOT}/current/node_modules/meshcentral/package.json').version")"
  log "verification passed: MeshCentral ${installed} is active on HTTPS port ${HTTPS_PORT}"
}

main() {
  while (($#)); do
    case "$1" in
      --server-name) require_value "$1" "${2:-}"; SERVER_NAME="$2"; shift 2 ;;
      --meshcentral-version) require_value "$1" "${2:-}"; MESHCENTRAL_VERSION="$2"; shift 2 ;;
      --node-version) require_value "$1" "${2:-}"; NODE_VERSION="$2"; shift 2 ;;
      --node-sha256) require_value "$1" "${2:-}"; NODE_SHA256="$2"; shift 2 ;;
      --https-port) require_value "$1" "${2:-}"; HTTPS_PORT="$2"; HTTPS_PORT_SET=true; shift 2 ;;
      --http-port) require_value "$1" "${2:-}"; HTTP_PORT="$2"; shift 2 ;;
      --verify-only) VERIFY_ONLY=true; shift ;;
      -h|--help) usage; exit 0 ;;
      *) usage; fail "unknown option: $1" ;;
    esac
  done
  require_root
  validate_platform
  validate_version "--meshcentral-version" "${MESHCENTRAL_VERSION}"
  validate_version "--node-version" "${NODE_VERSION}"
  if ${VERIFY_ONLY} && ! ${HTTPS_PORT_SET}; then load_installed_https_port; fi
  validate_port "--https-port" "${HTTPS_PORT}" false
  validate_port "--http-port" "${HTTP_PORT}" true
  [[ ${HTTPS_PORT} -ne ${HTTP_PORT} ]] || fail "HTTPS and HTTP ports must differ"
  if ${VERIFY_ONLY}; then
    verify_installation
    exit 0
  fi
  [[ -n ${SERVER_NAME} ]] || { usage; fail "--server-name is required"; }
  validate_server_name
  install -d -m 0755 "$(dirname "${INSTALL_LOCK}")"
  exec 8>"${INSTALL_LOCK}"
  flock --wait 0 8 || fail "another MeshCentral installation is running"

  install_prerequisites
  install_node
  ensure_service_account
  install_meshcentral_release
  write_or_validate_config
  install_systemd_unit
  assert_fresh_ports_available

  local previous="" was_active=false
  if [[ -L ${APPLICATION_ROOT}/current ]]; then previous="$(readlink -f "${APPLICATION_ROOT}/current")"; fi
  if systemctl is-active --quiet "${SERVICE_NAME}"; then was_active=true; systemctl stop "${SERVICE_NAME}"; fi
  activate_release
  systemctl enable "${SERVICE_NAME}"
  systemctl restart "${SERVICE_NAME}"
  if ! wait_for_service; then
    journalctl -u "${SERVICE_NAME}" -n 50 --no-pager >&2 || true
    if [[ -n ${previous} && ${previous} != "${APPLICATION_ROOT}/releases/${MESHCENTRAL_VERSION}" ]]; then
      log "new release failed verification; restoring previous release"
      local rollback_link="${APPLICATION_ROOT}/.rollback.$$"
      ln -s "${previous}" "${rollback_link}"
      mv -Tf -- "${rollback_link}" "${APPLICATION_ROOT}/current"
      if ${was_active}; then systemctl restart "${SERVICE_NAME}" || true; fi
    fi
    fail "MeshCentral did not become healthy"
  fi
  verify_installation
  log "create the first administrator now at https://${SERVER_NAME}:${HTTPS_PORT}/"
  log "back up ${STATE_ROOT}/meshcentral-data and ${STATE_ROOT}/meshcentral-files off-host"
  if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q '^Status: active'; then
    log "WARNING: UFW is active; allow TCP ${HTTPS_PORT} only from the edge/management networks"
  fi
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
  main "$@"
fi
