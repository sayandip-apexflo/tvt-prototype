#!/usr/bin/env bash
set -Eeuo pipefail

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV=/opt/tvt/venv

usage() {
  echo "usage: sudo bash scripts/bootstrap-postgresql.sh [--venv PATH]" >&2
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
install -d -o tvt-edge -g tvt-edge -m 0750 /var/lib/tvt
install -d -o tvt-alert -g tvt-alert -m 0750 /var/lib/tvt-alert
install -d -o root -g tvt-edge -m 0750 /etc/tvt/credential-keys
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
  "${REPO_ROOT}/deploy/systemd/tvt-k3s-watchdog.service" \
  /etc/systemd/system/tvt-k3s-watchdog.service
install -o root -g root -m 0644 \
  "${REPO_ROOT}/deploy/systemd/tvt-k3s-watchdog.timer" \
  /etc/systemd/system/tvt-k3s-watchdog.timer
systemctl daemon-reload
echo "PostgreSQL and TVT host service configuration are installed."
echo "Review /etc/tvt/edge.env and /etc/tvt/alert-dispatcher.env, install the"
echo "SendGrid key, initialize the site and notification policies, then enable services."
