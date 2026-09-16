#!/usr/bin/env bash
set -Eeuo pipefail

[[ -n "${FAKE_KUBECTL_LOG:-}" ]] && printf '%s\n' "$*" >>"${FAKE_KUBECTL_LOG}"

case "$*" in
  "get nodes -o jsonpath="*) printf 'fixture-node' ;;
  "get node fixture-node -o jsonpath={.status.nodeInfo.machineID}") tr -d '[:space:]' </etc/machine-id ;;
  "get node fixture-node -o jsonpath="*) printf 'True' ;;
  "get node fixture-node") exit 0 ;;
  *) printf 'fixture kubectl: %s\n' "$*" ;;
esac
