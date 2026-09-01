#!/usr/bin/env bash
set -Eeuo pipefail

KUBECTL=(k3s kubectl)
if [[ ! -r /etc/rancher/k3s/k3s.yaml && ${EUID} -ne 0 ]]; then
  command -v sudo >/dev/null 2>&1 || { echo "K3s kubeconfig is unreadable" >&2; exit 1; }
  KUBECTL=(sudo k3s kubectl)
fi

mapfile -t nodes < <("${KUBECTL[@]}" get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')
if [[ ${#nodes[@]} -ne 1 || -z "${nodes[0]}" ]]; then
  echo "TVT verification requires exactly one node; found ${#nodes[@]}" >&2
  exit 1
fi
node="${nodes[0]}"

"${KUBECTL[@]}" wait --for=condition=Ready "node/${node}" --timeout=60s
"${KUBECTL[@]}" get namespace apexfabric >/dev/null
"${KUBECTL[@]}" get crd apexnodestatuses.apexfabric.com >/dev/null
"${KUBECTL[@]}" rollout status daemonset/node-reporter -n apexfabric --timeout=180s
"${KUBECTL[@]}" rollout status deployment/node-status-controller -n apexfabric --timeout=180s
"${KUBECTL[@]}" wait --for=jsonpath='{.status.accepted}'=true \
  "apexnodestatus/${node}" --timeout=180s

qualified="$("${KUBECTL[@]}" get "node/${node}" -o jsonpath='{.metadata.labels.apexfabric\.com/qualified}')"
[[ "${qualified}" == true ]] || { echo "node is not controller-qualified" >&2; exit 1; }
"${KUBECTL[@]}" auth can-i create deployments.apps \
  --as=system:serviceaccount:apexfabric:node-agent -n apexfabric | grep -qx yes

reporter_image="$("${KUBECTL[@]}" get daemonset/node-reporter -n apexfabric -o jsonpath='{.spec.template.spec.containers[0].image}')"
controller_image="$("${KUBECTL[@]}" get deployment/node-status-controller -n apexfabric -o jsonpath='{.spec.template.spec.containers[0].image}')"
[[ "${reporter_image}" == *@sha256:* && "${controller_image}" == *@sha256:* ]] || {
  echo "node-management images are not digest-pinned" >&2
  exit 1
}
echo "TVT K3s plane verified on ${node}"
