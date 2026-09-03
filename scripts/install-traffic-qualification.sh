#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly SOURCE_CONTRACTS="${REPO_ROOT}/solution-packs/catalog/traffic-edge-runtime-2026.08.21-v4"
readonly TARGET_CONTRACTS="/opt/tvt/solution-packs/catalog/traffic-edge-runtime-2026.08.21-v4"

[[ ${EUID} -eq 0 ]] || { echo "run as root" >&2; exit 1; }
[[ -x /opt/tvt/venv/bin/tvt-traffic-qualify ]] || {
  echo "install the current TVT Python package in /opt/tvt/venv first" >&2
  exit 1
}
[[ -d ${SOURCE_CONTRACTS} ]] || {
  echo "pinned Traffic qualification contracts are missing" >&2
  exit 1
}

install -d -o root -g root -m 0755 /opt/tvt/scripts "${TARGET_CONTRACTS}"
install -d -o root -g root -m 0750 /var/lib/tvt/qualification
install -o root -g root -m 0755 \
  "${REPO_ROOT}/scripts/qualify-traffic-edge.sh" \
  /opt/tvt/scripts/qualify-traffic-edge.sh
install -o root -g root -m 0755 \
  "${REPO_ROOT}/scripts/verify-traffic-qualification.py" \
  /opt/tvt/scripts/verify-traffic-qualification.py
for filename in \
  image-contract.yaml \
  desired-state.schema.json \
  desired-state.example.json \
  metrics.schema.json \
  analytics-event.schema.json \
  analytics-event.example.json \
  provenance.json; do
  install -o root -g root -m 0644 \
    "${SOURCE_CONTRACTS}/${filename}" "${TARGET_CONTRACTS}/${filename}"
done

echo "Installed the manual Traffic edge qualification tools and pinned contracts."
echo "No qualification, deployment, rollback, restart, or reboot was performed."
