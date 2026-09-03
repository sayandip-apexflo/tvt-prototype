#!/usr/bin/env bash
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
