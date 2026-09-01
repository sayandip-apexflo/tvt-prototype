#!/usr/bin/env bash
set -Eeuo pipefail

[[ ${EUID} -eq 0 ]] || { echo "run as root" >&2; exit 1; }
command -v k3s >/dev/null 2>&1 || { echo "K3s is not installed" >&2; exit 1; }
mapfile -t nodes < <(k3s kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')
if [[ ${#nodes[@]} -ne 1 || -z "${nodes[0]}" ]]; then
  echo "TVT Secret encryption migration requires exactly one K3s node" >&2
  exit 1
fi

status="$(k3s secrets-encrypt status 2>&1 || true)"
if grep -q 'Encryption Status: Enabled' <<<"${status}"; then
  echo "K3s Secret encryption is already enabled"
  exit 0
fi

# This command creates the initial encryption configuration for a server that
# was installed before --secrets-encryption became part of the TVT profile.
k3s secrets-encrypt enable
install -d -o root -g root -m 0700 /etc/rancher/k3s
config=/etc/rancher/k3s/config.yaml
touch "${config}"
chmod 0600 "${config}"
if ! grep -Eq '^secrets-encryption:[[:space:]]*true' "${config}"; then
  echo 'secrets-encryption: true' >>"${config}"
fi
if ! grep -Eq '^secrets-encryption-provider:[[:space:]]*secretbox' "${config}"; then
  echo 'secrets-encryption-provider: secretbox' >>"${config}"
fi
systemctl restart k3s
k3s secrets-encrypt rotate-keys
systemctl restart k3s
status="$(k3s secrets-encrypt status)"
grep -q 'Encryption Status: Enabled' <<<"${status}" || {
  echo "K3s Secret encryption did not become enabled" >&2
  exit 1
}
grep -q 'Current Rotation Stage: reencrypt_finished' <<<"${status}" || {
  echo "K3s Secret reencryption did not finish" >&2
  exit 1
}
echo "K3s Secret encryption is enabled and existing Secrets were reencrypted"
