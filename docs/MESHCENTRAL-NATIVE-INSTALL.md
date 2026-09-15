# MeshCentral native workstation and Intel edge agent

These scripts install MeshCentral outside K3s and Docker. The workstation is
the always-on management server; the Intel edge initiates an outbound HTTPS/WSS
connection to it.

## Assumptions

- Both hosts run Ubuntu 24.04 on `amd64`.
- The workstation has a stable DNS name or IP that the edge can reach.
- TCP 443 (or the selected HTTPS port) is allowed from the edge to the
  workstation. TCP 4433 is not needed for the software agent.
- Workstation installation is online. Edge enrollment may be online or use
  server-exported files delivered through controlled media.

The workstation must not sleep or receive a different address after agents are
enrolled. Treat it as a privileged management system: anyone controlling
MeshCentral can obtain privileged remote access to enrolled edge devices.

## 1. Install the workstation server

Choose the address that the edge will use for the lifetime of the server:

```bash
sudo ./scripts/install-meshcentral-workstation.sh \
  --server-name meshcentral.example.internal
```

The defaults pin MeshCentral 1.2.5 and Node.js 24.21.0. The installer:

- obtains Node.js from `nodejs.org` and verifies the exact release checksum;
- obtains the exact MeshCentral package from npm;
- runs MeshCentral under a non-login `meshcentral` system account;
- keeps versioned application files below `/opt/meshcentral/releases`;
- keeps persistent data and server identity below `/var/lib/meshcentral`;
- installs and enables `meshcentral.service` with systemd hardening;
- enables daily local backups with 14-day retention; and
- refuses to silently replace an existing customized `config.json`.

For an independently recorded Node archive pin, supply the checksum as well:

```bash
sudo ./scripts/install-meshcentral-workstation.sh \
  --server-name meshcentral.example.internal \
  --node-sha256 '<64-lowercase-hex-characters>'
```

The built-in MeshCentral certificate is suitable for agent certificate pinning,
but browsers and download tools will not trust it automatically. Prefer a
certificate issued by the organization's internal CA or a public CA. If using
the built-in certificate, securely copy
`/var/lib/meshcentral/meshcentral-data/webserver-cert-public.crt` to the edge
and pass it with `--ca-cert`; do not bypass TLS verification.

The installer does not rewrite host firewall policy. If UFW is active it emits
a warning; allow the selected HTTPS TCP port only from the edge and management
networks. Open the HTTP redirect port only when it is actually needed.

Open `https://meshcentral.example.internal/` immediately. The first account
created becomes the administrator. Enable MFA, create a normal software-agent
device group such as `TVT Intel Edges`, and open **Add Agent** for that group.

Back up these directories to encrypted off-host storage:

```text
/var/lib/meshcentral/meshcentral-data
/var/lib/meshcentral/meshcentral-files
```

The data directory contains the server's agent identity keys. Restoring only
the hostname without these keys does not restore the same MeshCentral identity.

Read-only verification is available after installation:

```bash
sudo ./scripts/install-meshcentral-workstation.sh --verify-only
```

To upgrade, first take and verify an off-host backup, then rerun the installer
with a reviewed exact MeshCentral version. Releases are retained so a failed
service health check can restore the previous `current` symlink.

## 2. Prepare the edge enrollment value

Copy the device-group ID from the Linux command in MeshCentral's **Add Agent**
dialog into a private single-line file. Do not put it in this repository or on
the command line; values can contain shell metacharacters such as `$`.

```bash
sudo install -o root -g root -m 0600 /dev/null /secure/meshcentral-mesh-id
sudoedit /secure/meshcentral-mesh-id
```

## 3. Install the agent online

On the Intel edge:

```bash
sudo ./scripts/install-meshagent-intel-edge.sh \
  --server-url https://meshcentral.example.internal \
  --mesh-id-file /secure/meshcentral-mesh-id
```

For a private/self-signed certificate:

```bash
sudo ./scripts/install-meshagent-intel-edge.sh \
  --server-url https://meshcentral.example.internal \
  --mesh-id-file /secure/meshcentral-mesh-id \
  --ca-cert /secure/meshcentral-ca.crt
```

The script downloads `/meshagents?id=6` and the group-specific `/meshsettings`
over verified HTTPS. It validates that the agent is a Linux x86-64 ELF binary,
validates the pinned server/group settings, installs it as a systemd service,
and verifies that the service is enabled and active. It deliberately does not
execute MeshCentral's downloaded shell installer and never falls back to HTTP.

An idempotent rerun accepts the existing agent only when its Mesh ID, Server ID,
and advertised WSS endpoint match. Replacing a different enrollment requires
the explicit `--reinstall` option.

## 4. Install the agent offline

From MeshCentral, export the Linux x86-64 agent and `meshagent.msh` settings for
the selected device group. Transfer both using controlled media and calculate
their SHA-256 values before transport. Then run on the edge:

```bash
sudo ./scripts/install-meshagent-intel-edge.sh \
  --agent-file /media/tvt/meshagent \
  --settings-file /media/tvt/meshagent.msh \
  --agent-sha256 '<agent-sha256>' \
  --settings-sha256 '<settings-sha256>'
```

Verify after installation or after an edge reboot:

```bash
sudo ./scripts/install-meshagent-intel-edge.sh --verify-only
sudo systemctl status meshagent.service
```

The edge needs no inbound firewall opening for normal Mesh Agent operation.
