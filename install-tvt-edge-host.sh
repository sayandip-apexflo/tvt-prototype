#!/usr/bin/env bash
set -Eeuo pipefail
umask 027

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/tvt-installer-common.sh
source "${SCRIPT_DIR}/scripts/lib/tvt-installer-common.sh"

BUNDLE=""
SITE_CONFIG=""
K3S_MODE=bundled
PIPELINE_CREDENTIALS_FILE=""
SKIP_QUALIFICATION_TOOLS=false
VERIFY_ONLY=false
RESUME=false
readonly PREPARE_STATE="${TVT_INSTALL_STATE_ROOT}/prepare-state.json"
readonly INSTALL_STATE="${TVT_INSTALL_STATE_ROOT}/install-state.json"
readonly INSTALL_REPORT="${TVT_INSTALL_STATE_ROOT}/installation-report.json"
readonly REBOOT_MARKER="${TVT_HARDWARE_REBOOT_MARKER:-/var/lib/tvt/hardware-driver-reboot-required}"
readonly OPT_TVT="${TVT_OPT_DIRECTORY:-/opt/tvt}"

usage() {
  cat >&2 <<'EOF'
usage: sudo ./install-tvt-edge-host.sh --bundle PATH --site-config PATH [options]

options:
  --k3s-mode bundled|download
  --pipeline-credentials-file PATH  root-readable environment file; never pass secrets as values
  --skip-qualification-tools
  --verify-only                    run final verification without changing the host
  --resume                         explicitly resume (normal reruns are also idempotent)
EOF
}

while (($#)); do
  case "$1" in
    --bundle) tvt_require_value "$1" "${2:-}"; BUNDLE="$2"; shift 2 ;;
    --site-config) tvt_require_value "$1" "${2:-}"; SITE_CONFIG="$2"; shift 2 ;;
    --k3s-mode) tvt_require_value "$1" "${2:-}"; K3S_MODE="$2"; shift 2 ;;
    --pipeline-credentials-file) tvt_require_value "$1" "${2:-}"; PIPELINE_CREDENTIALS_FILE="$2"; shift 2 ;;
    --skip-qualification-tools) SKIP_QUALIFICATION_TOOLS=true; shift ;;
    --verify-only) VERIFY_ONLY=true; shift ;;
    --resume) RESUME=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage; tvt_fail "unknown option: $1" ;;
  esac
done
[[ -n ${BUNDLE} ]] || { usage; exit 2; }
if ! ${VERIFY_ONLY}; then [[ -n ${SITE_CONFIG} ]] || { usage; exit 2; }; fi
[[ ${K3S_MODE} == bundled || ${K3S_MODE} == download ]] || tvt_fail "--k3s-mode must be bundled or download"

tvt_require_root
command -v flock >/dev/null 2>&1 || tvt_fail "flock is required"
BUNDLE="$(tvt_canonical_directory "${BUNDLE}")"
if [[ -n ${SITE_CONFIG} ]]; then
  [[ -f ${SITE_CONFIG} && ! -L ${SITE_CONFIG} ]] || tvt_fail "site config is missing or symlinked"
  [[ $(stat -c '%s' "${SITE_CONFIG}") -le 65536 ]] || tvt_fail "site config is larger than 64 KiB"
  SITE_CONFIG="$(cd "$(dirname "${SITE_CONFIG}")" && pwd -P)/$(basename "${SITE_CONFIG}")"
fi
if [[ -n ${PIPELINE_CREDENTIALS_FILE} ]]; then
  [[ -f ${PIPELINE_CREDENTIALS_FILE} && ! -L ${PIPELINE_CREDENTIALS_FILE} ]] || \
    tvt_fail "pipeline credentials file is missing or symlinked"
  [[ $(stat -c '%s' "${PIPELINE_CREDENTIALS_FILE}") -le 65536 ]] || \
    tvt_fail "pipeline credentials file is larger than 64 KiB"
  PIPELINE_CREDENTIALS_FILE="$(cd "$(dirname "${PIPELINE_CREDENTIALS_FILE}")" && pwd -P)/$(basename "${PIPELINE_CREDENTIALS_FILE}")"
fi
tvt_acquire_lock
tvt_verify_bundle "${BUNDLE}"
readonly RELEASE_VERSION="$(tvt_manifest_value "${BUNDLE}" release_version)"
readonly RELEASE_DIRECTORY="${OPT_TVT}/releases/${RELEASE_VERSION}"
readonly RESOURCE_DIRECTORY="${RELEASE_DIRECTORY}/resources"
readonly VENV_DIRECTORY="${RELEASE_DIRECTORY}/venv"
readonly TRAFFIC_CATALOG_DIRECTORY="${RESOURCE_DIRECTORY}/solution-packs/catalog/traffic-edge-runtime-2026.08.21-v4"

preflight_install() {
  [[ "$(tvt_json_get "${PREPARE_STATE}" status 2>/dev/null || true)" == prepared ]] || \
    tvt_fail "host preparation has not completed successfully"
  [[ ! -e ${REBOOT_MARKER} ]] || tvt_fail "reboot-required marker exists; rerun host preparation after reboot"
  [[ $(dpkg --print-architecture) == amd64 ]] || tvt_fail "release architecture is amd64"
  if command -v k3s >/dev/null 2>&1; then
    installed_version="$(k3s --version | awk 'NR == 1 {print $3}')"
    [[ ${installed_version} == "$(tvt_manifest_value "${BUNDLE}" k3s_version)" ]] || \
      tvt_fail "installed K3s ${installed_version} does not match the release"
    mapfile -t nodes < <(k3s kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')
    [[ ${#nodes[@]} -eq 1 && -n ${nodes[0]} ]] || \
      tvt_fail "refusing an existing cluster that is not exactly one node"
  fi
  tvt_log "installation preflight passed"
}

install_application() {
  install -d -o root -g root -m 0755 "${OPT_TVT}/releases" "${RELEASE_DIRECTORY}"
  [[ ! -L ${RELEASE_DIRECTORY} ]] || tvt_fail "refusing symlinked release directory"
  if [[ -f ${RESOURCE_DIRECTORY}/manifest.json ]]; then
    cmp -s "${BUNDLE}/manifest.json" "${RESOURCE_DIRECTORY}/manifest.json" || \
      tvt_fail "release ${RELEASE_VERSION} is already present with a different manifest"
  fi
  install -d -o root -g root -m 0755 "${RESOURCE_DIRECTORY}"
  local item
  for item in manifest.json checksums.sha256 alembic.ini config deploy scripts solution-packs images k3s; do
    cp -a "${BUNDLE}/${item}" "${RESOURCE_DIRECTORY}/"
  done
  python3 -m venv --clear "${VENV_DIRECTORY}"
  local wheel_relative
  wheel_relative="$(tvt_manifest_value "${BUNDLE}" artifacts.application_wheel)"
  "${VENV_DIRECTORY}/bin/python" -m pip install --disable-pip-version-check \
    --no-index --find-links "${BUNDLE}/wheels" "${BUNDLE}/${wheel_relative}"
  chown -R root:root "${RELEASE_DIRECTORY}"
  chmod -R go+rX "${VENV_DIRECTORY}" "${RESOURCE_DIRECTORY}"
  install -d -o root -g root -m 0755 "${OPT_TVT}"
  local venv_link current_link
  venv_link="${OPT_TVT}/.venv.${RELEASE_VERSION}.$$"
  current_link="${OPT_TVT}/.current.${RELEASE_VERSION}.$$"
  ln -s "${VENV_DIRECTORY}" "${venv_link}"
  ln -s "${RELEASE_DIRECTORY}" "${current_link}"
  if [[ (-e ${OPT_TVT}/venv || -L ${OPT_TVT}/venv) && ! -L ${OPT_TVT}/venv ]]; then tvt_fail "${OPT_TVT}/venv is not a symlink"; fi
  if [[ (-e ${OPT_TVT}/current || -L ${OPT_TVT}/current) && ! -L ${OPT_TVT}/current ]]; then tvt_fail "${OPT_TVT}/current is not a symlink"; fi
  mv -Tf "${venv_link}" "${OPT_TVT}/venv"
  mv -Tf "${current_link}" "${OPT_TVT}/current"
}

install_registry() {
  bash "${RESOURCE_DIRECTORY}/scripts/install-local-registry.sh" \
    --image-archive "${RESOURCE_DIRECTORY}/images/registry.tar"
  ss -H -ltn 'sport = :5000' | awk '{print $4}' | grep -qx '127.0.0.1:5000' || \
    tvt_fail "local registry is not bound exclusively to 127.0.0.1:5000"
}

install_k3s() {
  local -a arguments
  if [[ ${K3S_MODE} == bundled ]]; then
    arguments=(--installer "${RESOURCE_DIRECTORY}/k3s/install.sh" --k3s-binary "${RESOURCE_DIRECTORY}/k3s/k3s")
  else
    arguments=(--download-installer)
  fi
  bash "${RESOURCE_DIRECTORY}/scripts/install-k3s-single-node.sh" "${arguments[@]}"
}

install_node_management() {
  export PATH="${VENV_DIRECTORY}/bin:${PATH}"
  local lock="${TVT_INSTALL_STATE_ROOT}/node-management-images.lock.json"
  bash "${RESOURCE_DIRECTORY}/scripts/publish-control-images.sh" \
    --registry 127.0.0.1:5000 --scheme http \
    --archive-dir "${RESOURCE_DIRECTORY}/images" --lock-output "${lock}"
  bash "${RESOURCE_DIRECTORY}/scripts/install-k3s-plane.sh" --image-lock "${lock}"
}

install_pipeline_image() {
  export PATH="${VENV_DIRECTORY}/bin:${PATH}"
  install -d -o root -g root -m 0700 /var/lib/tvt/pipeline /etc/tvt
  if [[ -n ${PIPELINE_CREDENTIALS_FILE} ]]; then
    if [[ -e /etc/tvt/pipeline-image-sync.env ]]; then
      cmp -s "${PIPELINE_CREDENTIALS_FILE}" /etc/tvt/pipeline-image-sync.env || \
        tvt_fail "preserving existing /etc/tvt/pipeline-image-sync.env; reconcile it explicitly"
    else
      install -o root -g root -m 0600 "${PIPELINE_CREDENTIALS_FILE}" /etc/tvt/pipeline-image-sync.env
    fi
  fi
  bash "${RESOURCE_DIRECTORY}/scripts/import-pipeline-traffic-image.sh" \
    --mode archive \
    --archive-file "${RESOURCE_DIRECTORY}/images/traffic-edge-runtime-v4.tar" \
    --metadata-directory "${TRAFFIC_CATALOG_DIRECTORY}" \
    --work-dir /var/lib/tvt/pipeline/work \
    --lock-output /var/lib/tvt/pipeline/traffic-image.lock.json \
    --concurrency-lock /var/lib/tvt/pipeline/import.lock
  bash "${RESOURCE_DIRECTORY}/scripts/install-pipeline-image-sync.sh" \
    --archive-file "${RESOURCE_DIRECTORY}/images/traffic-edge-runtime-v4.tar" \
    --metadata-directory "${TRAFFIC_CATALOG_DIRECTORY}"
  systemctl start tvt-pipeline-image-sync.service
}

provision_services() {
  export PATH="${VENV_DIRECTORY}/bin:${PATH}"
  export TVT_RESOURCE_ROOT="${RESOURCE_DIRECTORY}"
  bash "${RESOURCE_DIRECTORY}/scripts/bootstrap-postgresql.sh" --venv "${VENV_DIRECTORY}"
  bash "${RESOURCE_DIRECTORY}/scripts/install-tvt-kubeconfig.sh"
}

initialize_site() {
  export TVT_RESOURCE_ROOT="${RESOURCE_DIRECTORY}"
  mapfile -t site_fields < <("${VENV_DIRECTORY}/bin/python" - "${SITE_CONFIG}" <<'PY'
import pathlib, re, sys, yaml
path = pathlib.Path(sys.argv[1])
document = yaml.safe_load(path.read_text(encoding="utf-8"))
if not isinstance(document, dict): raise SystemExit("site config must be a mapping")
secret = re.compile(r"(password|passwd|secret|token|credential|api.?key)", re.I)
def keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from keys(item)
    elif isinstance(value, list):
        for item in value: yield from keys(item)
if any(secret.search(key) for key in keys(document)):
    raise SystemExit("site config must not contain credentials or secrets")
if "site" in document:
    if set(document) != {"site"} or not isinstance(document["site"], dict):
        raise SystemExit("a wrapped site config may contain only the site mapping")
    document = document["site"]
allowed = {"site_key", "edge_id", "display_name", "timezone"}
unknown = set(document) - allowed
if unknown: raise SystemExit("unknown site config keys: " + ", ".join(sorted(unknown)))
values = [document.get("site_key"), document.get("edge_id"), document.get("display_name"), document.get("timezone", "UTC")]
if not all(isinstance(value, str) and value and "\n" not in value and "\t" not in value for value in values):
    raise SystemExit("site_key, edge_id, display_name, and timezone must be non-empty single-line strings")
print("\n".join(values))
PY
  )
  [[ ${#site_fields[@]} -eq 4 ]] || tvt_fail "site config did not produce four fields"
  runuser -u tvt-edge -- env TVT_RESOURCE_ROOT="${RESOURCE_DIRECTORY}" \
    TVT_DATABASE_URL=postgresql+psycopg:///tvt \
    "${VENV_DIRECTORY}/bin/tvt-edge" init-site \
    "${site_fields[0]}" "${site_fields[1]}" "${site_fields[2]}" --timezone "${site_fields[3]}"
}

start_core_services() {
  systemctl daemon-reload
  systemctl enable --now tvt-edge.service tvt-camera-sync.service \
    tvt-retention.timer tvt-k3s-watchdog.timer tvt-pipeline-image-sync.timer
  if [[ -s /etc/tvt/sendgrid-api-key ]] && \
    grep -Eq '^TVT_ALERT_EMAIL_FROM=.+@.+' /etc/tvt/alert-dispatcher.env && \
    ! grep -Eq '^TVT_ALERT_EMAIL_FROM=.*@tvt\.example$' /etc/tvt/alert-dispatcher.env; then
    systemctl enable --now tvt-alert-dispatcher.service
  else
    systemctl disable --now tvt-alert-dispatcher.service >/dev/null 2>&1 || true
    tvt_log "alert dispatcher left disabled because notification configuration is incomplete"
  fi
}

refresh_catalog() {
  export TVT_RESOURCE_ROOT="${RESOURCE_DIRECTORY}"
  runuser -u tvt-edge -- env TVT_RESOURCE_ROOT="${RESOURCE_DIRECTORY}" \
    TVT_DATABASE_URL=postgresql+psycopg:///tvt TVT_KUBECONFIG=/etc/tvt/kubeconfig \
    "${VENV_DIRECTORY}/bin/tvt-edge" refresh-solutions
  deployment_count="$(runuser -u postgres -- psql -d tvt -Atc 'SELECT count(*) FROM solution_deployments')"
  [[ ${deployment_count} == 0 ]] || tvt_fail "installation must not create a Traffic deployment"
}

install_qualification() {
  bash "${RESOURCE_DIRECTORY}/scripts/install-traffic-qualification.sh"
}

final_verification() {
  export TVT_RESOURCE_ROOT="${RESOURCE_DIRECTORY}"
  local unit
  for unit in docker.service postgresql.service tvt-local-registry.service k3s.service \
    tvt-edge.service tvt-camera-sync.service tvt-retention.timer \
    tvt-k3s-watchdog.timer tvt-pipeline-image-sync.timer; do
    systemctl is-active --quiet "${unit}" || tvt_fail "required unit is not active: ${unit}"
  done
  docker info >/dev/null
  curl --fail --silent --show-error --max-time 5 http://127.0.0.1:5000/v2/ >/dev/null
  mapfile -t ready_nodes < <(k3s kubectl get nodes \
    -o jsonpath='{range .items[?(@.status.conditions[?(@.type=="Ready")].status=="True")]}{.metadata.name}{"\n"}{end}')
  [[ ${#ready_nodes[@]} -eq 1 && -n ${ready_nodes[0]} ]] || tvt_fail "K3s does not have exactly one Ready node"
  bash "${RESOURCE_DIRECTORY}/scripts/verify-k3s-plane.sh"
  pg_isready --quiet
  runuser -u tvt-edge -- env TVT_RESOURCE_ROOT="${RESOURCE_DIRECTORY}" \
    TVT_DATABASE_URL=postgresql+psycopg:///tvt \
    "${VENV_DIRECTORY}/bin/tvt-edge" check >/dev/null
  expected_migration="$(TVT_RESOURCE_ROOT="${RESOURCE_DIRECTORY}" "${VENV_DIRECTORY}/bin/python" - <<'PY'
from alembic.config import Config
from alembic.script import ScriptDirectory
from tvt_edge.paths import RESOURCE_ROOT
heads = ScriptDirectory.from_config(Config(str(RESOURCE_ROOT / "alembic.ini"))).get_heads()
if len(heads) != 1: raise SystemExit("release must contain exactly one Alembic head")
print(heads[0])
PY
  )"
  actual_migration="$(runuser -u postgres -- psql -d tvt -Atc 'SELECT version_num FROM alembic_version')"
  [[ ${actual_migration} == "${expected_migration}" ]] || tvt_fail \
    "database migration ${actual_migration:-missing} does not match release head ${expected_migration}"
  api_health="$(curl --fail --silent --show-error --max-time 10 http://127.0.0.1:8088/api/v1/health)"
  python3 - "${api_health}" <<'PY'
import json, sys
health = json.loads(sys.argv[1])
if health.get("status") != "healthy": raise SystemExit("TVT API is not healthy")
PY
  bash "${RESOURCE_DIRECTORY}/scripts/verify-pipeline-image-sync.sh"
  available="$(runuser -u postgres -- psql -d tvt -Atc "SELECT count(*) FROM solution_catalog_entries WHERE status='available'")"
  [[ ${available} -ge 1 ]] || tvt_fail "Traffic catalog entry is not available"
  deployment_count="$(runuser -u postgres -- psql -d tvt -Atc 'SELECT count(*) FROM solution_deployments')"
  [[ ${deployment_count} == 0 ]] || tvt_fail "a Traffic deployment was created automatically"
  [[ $(stat -c '%a' /etc/tvt/credential-keys) == 750 ]] || tvt_fail "credential key directory permissions are unsafe"
  ! grep -Eiq '(^|_)(password|secret|token|credential|api_key)=' /etc/tvt/edge.env || \
    tvt_fail "inline credential-like values are forbidden in /etc/tvt/edge.env"
}

write_install_evidence() {
  local temporary_state temporary_report
  temporary_state="$(mktemp "${TVT_INSTALL_STATE_ROOT}/.install-state-final.XXXXXX")"
  temporary_report="$(mktemp "${TVT_INSTALL_STATE_ROOT}/.installation-report.XXXXXX")"
  python3 - "${INSTALL_STATE}" "${temporary_state}" "${temporary_report}" \
    "${BUNDLE}/manifest.json" "${TVT_INSTALL_STATE_ROOT}/node-management-images.lock.json" \
    /var/lib/tvt/pipeline/traffic-image.lock.json <<'PY'
import datetime, json, pathlib, subprocess, sys
state_path, state_output, report_output, manifest_path, node_lock_path, traffic_lock_path = map(pathlib.Path, sys.argv[1:])
now = datetime.datetime.now(datetime.timezone.utc).isoformat()
state = json.loads(state_path.read_text(encoding="utf-8"))
state.update({"status": "installed", "completed_at": now, "updated_at": now})
state_output.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
node_lock = json.loads(node_lock_path.read_text(encoding="utf-8"))
traffic_lock = json.loads(traffic_lock_path.read_text(encoding="utf-8"))
units = [
    "docker.service", "postgresql.service", "tvt-local-registry.service", "k3s.service",
    "tvt-edge.service", "tvt-camera-sync.service", "tvt-retention.timer",
    "tvt-k3s-watchdog.timer", "tvt-pipeline-image-sync.timer",
]
health = {}
for unit in units:
    result = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True, check=False)
    health[unit] = result.stdout.strip() or "unknown"
report = {
    "schema_version": 1,
    "generated_at": now,
    "release_version": manifest["release_version"],
    "hardware_profile": manifest["hardware_profile"],
    "architecture": manifest["architecture"],
    "k3s_version": manifest["k3s_version"],
    "versions": {
        "kernel": subprocess.run(["uname", "-r"], capture_output=True, text=True, check=True).stdout.strip(),
        "docker": subprocess.run(["docker", "--version"], capture_output=True, text=True, check=True).stdout.strip(),
        "postgresql": subprocess.run(["psql", "--version"], capture_output=True, text=True, check=True).stdout.strip(),
    },
    "completed_stages": sorted(name for name, item in state.get("stages", {}).items() if item.get("status") == "completed"),
    "node_management_images": node_lock,
    "traffic_image": {
        "catalog_id": traffic_lock.get("catalog_id"),
        "reference": traffic_lock.get("image", {}).get("reference"),
        "digest": traffic_lock.get("image", {}).get("digest"),
        "source_mode": traffic_lock.get("source", {}).get("mode"),
    },
    "service_health": health,
    "credentials_included": False,
    "automatic_traffic_deployment": False,
}
report_output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
  chmod 0640 "${temporary_state}"
  chmod 0600 "${temporary_report}"
  mv -f -- "${temporary_state}" "${INSTALL_STATE}"
  mv -f -- "${temporary_report}" "${INSTALL_REPORT}"
}

preflight_install
if ${VERIFY_ONLY}; then
  [[ "$(tvt_json_get "${INSTALL_STATE}" status 2>/dev/null || true)" == installed ]] || \
    tvt_fail "installation state is not installed"
  final_verification
  tvt_log "TVT edge host verification succeeded."
  exit 0
fi

existing_release="$(tvt_json_get "${INSTALL_STATE}" release_version 2>/dev/null || true)"
if [[ -n ${existing_release} && ${existing_release} != "${RELEASE_VERSION}" ]]; then
  tvt_fail "install state belongs to release ${existing_release}; explicit upgrade support is required"
fi
${RESUME} && tvt_log "explicit resume requested"
tvt_run_stage "${INSTALL_STATE}" "${RELEASE_VERSION}" application install_application
tvt_run_stage "${INSTALL_STATE}" "${RELEASE_VERSION}" registry install_registry
tvt_run_stage "${INSTALL_STATE}" "${RELEASE_VERSION}" k3s install_k3s
tvt_run_stage "${INSTALL_STATE}" "${RELEASE_VERSION}" node_management install_node_management
tvt_run_stage "${INSTALL_STATE}" "${RELEASE_VERSION}" traffic_image install_pipeline_image
tvt_run_stage "${INSTALL_STATE}" "${RELEASE_VERSION}" postgresql_and_services provision_services
tvt_run_stage "${INSTALL_STATE}" "${RELEASE_VERSION}" site initialize_site
tvt_run_stage "${INSTALL_STATE}" "${RELEASE_VERSION}" core_services start_core_services
tvt_run_stage "${INSTALL_STATE}" "${RELEASE_VERSION}" catalog refresh_catalog
if ! ${SKIP_QUALIFICATION_TOOLS}; then
  tvt_run_stage "${INSTALL_STATE}" "${RELEASE_VERSION}" qualification_tools install_qualification
fi
tvt_run_stage "${INSTALL_STATE}" "${RELEASE_VERSION}" verification final_verification
write_install_evidence
tvt_log "TVT edge host release ${RELEASE_VERSION} installed successfully."
tvt_log "Evidence: ${INSTALL_REPORT}"
