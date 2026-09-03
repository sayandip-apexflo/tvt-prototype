#!/usr/bin/env bash
set -Eeuo pipefail
umask 022

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT=""
REGISTRY_IMAGE=""
NODE_REPORTER_IMAGE=""
NODE_STATUS_CONTROLLER_IMAGE=""
TRAFFIC_IMAGE=""
K3S_INSTALLER=""
K3S_BINARY=""
HARDWARE_DIRECTORY=""
APT_DIRECTORY=""

usage() {
  cat >&2 <<'EOF'
usage: scripts/build-tvt-edge-release.sh --output DIR \
  --registry-image FILE --node-reporter-image FILE \
  --node-status-controller-image FILE --traffic-image FILE \
  --k3s-installer FILE --k3s-binary FILE \
  --hardware-directory DIR --apt-directory DIR

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
    --k3s-installer) K3S_INSTALLER="${2:-}"; shift 2 ;;
    --k3s-binary) K3S_BINARY="${2:-}"; shift 2 ;;
    --hardware-directory) HARDWARE_DIRECTORY="${2:-}"; shift 2 ;;
    --apt-directory) APT_DIRECTORY="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage; exit 2 ;;
  esac
done
for value in OUTPUT REGISTRY_IMAGE NODE_REPORTER_IMAGE NODE_STATUS_CONTROLLER_IMAGE \
  TRAFFIC_IMAGE K3S_INSTALLER K3S_BINARY HARDWARE_DIRECTORY APT_DIRECTORY; do
  [[ -n ${!value} ]] || { usage; echo "${value} is required" >&2; exit 2; }
done
for file in "${REGISTRY_IMAGE}" "${NODE_REPORTER_IMAGE}" \
  "${NODE_STATUS_CONTROLLER_IMAGE}" "${TRAFFIC_IMAGE}" "${K3S_INSTALLER}" "${K3S_BINARY}"; do
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
npm --prefix ui ci
npm --prefix ui run build
mkdir -p "${OUTPUT}"/{wheels,images,k3s,hardware,packages/apt}
mkdir -p "${OUTPUT}/tvt_edge/db"
python3 -m pip wheel --wheel-dir "${OUTPUT}/wheels" .

cp -a config deploy docs examples scripts solution-packs "${OUTPUT}/"
cp -a alembic.ini "${OUTPUT}/alembic.ini"
cp -a tvt_edge/db/migrations "${OUTPUT}/tvt_edge/db/"
cp -a prepare-tvt-edge-host.sh install-tvt-edge-host.sh "${OUTPUT}/"
cp -a release/manifest.template.json "${OUTPUT}/manifest.json"
cp -a "${REGISTRY_IMAGE}" "${OUTPUT}/images/registry.tar"
cp -a "${NODE_REPORTER_IMAGE}" "${OUTPUT}/images/node-reporter.tar"
cp -a "${NODE_STATUS_CONTROLLER_IMAGE}" "${OUTPUT}/images/node-status-controller.tar"
cp -a "${TRAFFIC_IMAGE}" "${OUTPUT}/images/traffic-edge-runtime-v4.tar"
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
python3 - "${OUTPUT}/manifest.json" "wheels/${application_wheel}" <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
manifest = json.loads(path.read_text(encoding="utf-8"))
manifest["artifacts"]["application_wheel"] = sys.argv[2]
path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
(cd "${OUTPUT}" && find . -type f ! -name checksums.sha256 -printf '%P\0' \
  | sort -z | xargs -0 sha256sum -- >checksums.sha256)
echo "Built checksum-complete TVT edge release: ${OUTPUT}"
