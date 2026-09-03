#!/usr/bin/env bash
set -Eeuo pipefail

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=config/platform.env
source "${REPO_ROOT}/config/platform.env"

IMAGE_ARCHIVE=""
usage() {
  echo "usage: sudo bash scripts/install-local-registry.sh [--image-archive FILE]" >&2
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
bash "${REPO_ROOT}/scripts/configure-k3s-registry.sh" \
  --registry "${LOCAL_REGISTRY_ADDRESS}" --scheme http "${restart_k3s[@]}"

curl --fail --silent --show-error --max-time 5 \
  "http://${LOCAL_REGISTRY_ADDRESS}/v2/" >/dev/null
echo "TVT local registry is ready at http://${LOCAL_REGISTRY_ADDRESS}."
echo "Persistent image data is stored under ${LOCAL_REGISTRY_DATA_DIR}."
if [[ ${#restart_k3s[@]} -eq 0 ]]; then
  echo "K3s is not active; install it next with scripts/install-k3s-single-node.sh."
fi
