#!/usr/bin/env bash
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
    echo "usage: bash scripts/verify-pipeline-image-sync.sh [--lock-output FILE]" >&2
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
  "${PIPELINE_TRAFFIC_ANALYTICS_EVENT_EXAMPLE_SHA256}" <<'PY'
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
