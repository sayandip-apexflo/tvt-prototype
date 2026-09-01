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

if ! getent group tvt-edge >/dev/null; then
  groupadd --system tvt-edge
fi
if ! id tvt-edge >/dev/null 2>&1; then
  useradd --system --gid tvt-edge --home-dir /var/lib/tvt --shell /usr/sbin/nologin tvt-edge
fi
install -d -o tvt-edge -g tvt-edge -m 0750 /var/lib/tvt
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
systemctl daemon-reload
echo "PostgreSQL and TVT host service configuration are installed."
echo "Review /etc/tvt/edge.env, initialize the site, then explicitly enable services."
