# TVT online test-kit installation

This runbook installs the experimental online test kit on a dedicated Ubuntu
24.04 `amd64` Intel Core Ultra 9 285H device. It is not the supported
checksum-complete offline production workflow. Ubuntu packages, Intel drivers,
the NPU release, and OpenVINO are resolved from the Internet on the target.

The commands below use the `0.1.0-0090aca6ffef` kit as an example. Substitute
the filename and extracted directory printed by the current build report when
using a newer kit.

## Automated, resumable Steps 1-10

`scripts/install-tvt-online-steps-1-10.sh` performs the target-side work in
this runbook as one fail-fast, resumable command. Transfer that script beside
the current archive and checksum; transfer remains a workstation action and
the SSH password must still be entered interactively. The script records a
root-only pre-install baseline in `/var/lib/tvt-online-test-install` before it
changes the host. That baseline is required by the paired reset script.

On the target, run one command with deployment-specific, non-secret site
values:

```bash
sudo bash /home/admin1/install-tvt-online-steps-1-10.sh --archive /home/admin1/tvt-edge-online-test-kit-0.1.0-0090aca6ffef.tar.gz --checksum /home/admin1/tvt-edge-online-test-kit-0.1.0-0090aca6ffef.tar.gz.sha256 --site-id plant-1 --edge-id plant-1-edge-1 --site-name "Plant 1" --timezone Asia/Kolkata --install-group admin1
```

Exit status `194` means that a required reboot boundary was reached. Reboot,
reconnect, and run the exact same one-line command. Completed stages are not
repeated. A pre-existing K3s agent is removed only when the command also has
`--approve-k3s-agent-removal`; a server or partial Kubernetes installation is
rejected before host mutation. Original agent credentials are never captured,
so the reset cannot rejoin that former cluster.

The combined installer verifies the outer checksum, every internally listed
file, the artifact manifest, driver/OpenVINO devices, local Registry, exact
bundled K3s pin, node-management plane, Traffic import, PostgreSQL, TVT units,
timers, API health, and the TVT self-check. It loads the Traffic image and
catalog but does not deploy Traffic workloads or configure cameras, external
monitoring, notification policy, or email delivery.

Run the paired reset without options to print its impact without changing the
host:

```bash
sudo bash /opt/tvt/tvt-edge-online-test-kit-0.1.0-0090aca6ffef/source/scripts/reset-tvt-online-test-host.sh
```

After review and explicit approval, its destructive form is:

```bash
sudo bash /opt/tvt/tvt-edge-online-test-kit-0.1.0-0090aca6ffef/source/scripts/reset-tvt-online-test-host.sh --execute --confirm ERASE-TVT-ONLINE-TEST
```

It drops the TVT database and roles, uninstalls the test K3s server and its
cluster/local-storage state, removes TVT services/accounts/runtime, removes
only Docker tags introduced during the run, restores changed or displaced
package versions, purges packages absent from the baseline, restores prior
manual-package and Docker/PostgreSQL service states, and preserves the
transferred archive in `/home`. It refuses to operate without its paired
baseline. Reboot after it completes before starting the next test cycle.

For a deliberately destructive lab reset that ignores the baseline and also
removes matching components that predate TVT, first review its plan:

```bash
sudo bash /opt/tvt/tvt-edge-online-test-kit-0.1.0-0090aca6ffef/source/scripts/reset-tvt-online-test-host.sh --force-full-stack
```

Run it only when loss of all K3s/Kubernetes state, every Docker/containerd
container/image/volume, every PostgreSQL cluster/database, and the installed
Intel userspace GPU/NPU/media stack is acceptable:

```bash
sudo bash /opt/tvt/tvt-edge-online-test-kit-0.1.0-0090aca6ffef/source/scripts/reset-tvt-online-test-host.sh --force-full-stack --execute --confirm ERASE-ENTIRE-TVT-STACK
```

This mode preserves files under `/home`, the Ubuntu kernel, and its built-in
`i915`, `xe`, and `intel_vpu` modules. Removing the qualified kernel or
essential Ubuntu utilities would make the device unable to run Steps 1-10.
The mode intentionally does not run a global `apt autoremove`, because that
could delete unrelated orphaned OS packages outside the named stack.

## 1. Transfer the archive

Run on the build workstation:

```bash
cd build/online-test-kit/output
scp \
  tvt-edge-online-test-kit-0.1.0-0090aca6ffef.tar.gz \
  tvt-edge-online-test-kit-0.1.0-0090aca6ffef.tar.gz.sha256 \
  admin1@192.168.1.64:/home/admin1/
```

Enter the SSH password interactively. Never include it in the command or save
it in shell history.

## 2. Finish pending host updates first

Connect to the edge device and check whether Ubuntu already requires a reboot:

```bash
ssh admin1@192.168.1.64
test ! -e /var/run/reboot-required || cat /var/run/reboot-required
```

If a reboot is required, perform it before installing the hardware stack. The
driver recipe is locked to the active kernel, so do not change kernels between
driver resolution and subsequent driver-installer retries.

```bash
sudo reboot
```

Reconnect and confirm that the required modules exist for the active kernel:

```bash
ssh admin1@192.168.1.64
uname -r
modinfo intel_vpu >/dev/null
modinfo i915 >/dev/null || modinfo xe >/dev/null
```

## 3. Verify and extract with deterministic permissions

Run these commands in the same SSH shell. Extracting beneath `/opt/tvt` keeps
the bundled Traffic archive readable by the hardened daily synchronization
service; that service cannot read files retained under `/home`.

```bash
set -Eeuo pipefail

TVT_ARCHIVE=/home/admin1/tvt-edge-online-test-kit-0.1.0-0090aca6ffef.tar.gz
TVT_CHECKSUM="${TVT_ARCHIVE}.sha256"
TVT_KIT_ROOT=/opt/tvt/tvt-edge-online-test-kit-0.1.0-0090aca6ffef
TVT_INSTALL_GROUP="$(id -gn)"

cd "$(dirname "${TVT_ARCHIVE}")"
sha256sum --check "$(basename "${TVT_CHECKSUM}")"

sudo install -d -o root -g root -m 0755 /opt/tvt
sudo tar --extract --gzip --no-same-owner \
  --file "${TVT_ARCHIVE}" --directory /opt/tvt

# This is intentionally idempotent and also repairs older test kits whose
# transport archive contained root-only modes.
sudo chown -R root:"${TVT_INSTALL_GROUP}" "${TVT_KIT_ROOT}"
sudo chmod -R u=rwX,g=rX,o= "${TVT_KIT_ROOT}"

cd "${TVT_KIT_ROOT}"
test -r checksums.sha256
test -x k3s/install.sh
test -x k3s/k3s
test -r images/registry.tar
test -r images/traffic-edge-runtime-v4.tar
sha256sum --check --quiet checksums.sha256
```

Do not use recursive mode `777` and do not make the extracted tree user-owned.
The kit contains no credentials, but installed runtime secrets created later
under `/etc/tvt` and `/var/lib/tvt` must retain their restricted ownership.

## 4. Install prerequisites and the Intel stack

```bash
cd "${TVT_KIT_ROOT}/source"

sudo apt-get update
sudo apt-get install -y --no-install-recommends \
  ca-certificates curl gnupg python3 python3-venv docker.io postgresql-16 \
  jq openssl util-linux coreutils tar git git-lfs software-properties-common

sudo systemctl enable --now docker.service postgresql.service
sudo bash scripts/install-tvt-hardware-drivers.sh --mode online
sudo python3 -m json.tool /var/lib/tvt/hardware-driver-recipe.json
sudo reboot
```

The normal command is correct for a 285H. Use `--allow-unverified-hardware`
only after separately auditing a non-285H equivalent; that override bypasses
only the CPU-model check and does not make the device formally qualified.

## 5. Verify the hardware after reboot

Reconnect and restore the shell variable:

```bash
ssh admin1@192.168.1.64
TVT_KIT_ROOT=/opt/tvt/tvt-edge-online-test-kit-0.1.0-0090aca6ffef

test -e /dev/dri/renderD128
test -e /dev/accel/accel0
lsmod | grep -E '^(i915|xe|intel_vpu)\b'
vainfo --display drm --device /dev/dri/renderD128
clinfo -l

/opt/apexfabric/openvino-env/bin/python - <<'PY'
from openvino import Core

devices = {name.split(".", 1)[0] for name in Core().available_devices}
print("OpenVINO devices:", sorted(devices))
missing = {"CPU", "GPU", "NPU"} - devices
if missing:
    raise SystemExit("missing OpenVINO devices: " + ", ".join(sorted(missing)))
PY
```

Clear the installer marker only after every check succeeds:

```bash
sudo rm -f /var/lib/tvt/hardware-driver-reboot-required
```

## 6. Install the application wheelhouse

This section is safe to run in a new SSH session; declare the paths again.
Copy underscores literally—do not paste them as `\_`.

```bash
TVT_KIT_ROOT=/opt/tvt/tvt-edge-online-test-kit-0.1.0-0090aca6ffef
cd "${TVT_KIT_ROOT}/source"

test -d "${TVT_KIT_ROOT}/source"
test -f "${TVT_KIT_ROOT}/wheels/tvt_runtime-0.1.0-py3-none-any.whl"

sudo python3 -m venv --clear /opt/tvt/venv
sudo /opt/tvt/venv/bin/python -m pip install --disable-pip-version-check --no-index --find-links="${TVT_KIT_ROOT}/wheels" "${TVT_KIT_ROOT}/wheels/tvt_runtime-0.1.0-py3-none-any.whl"
sudo chmod -R go+rX /opt/tvt/venv

TVT_EXEC_PATH=/opt/tvt/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
```

## 7. Install the Registry and K3s

TVT requires a standalone K3s server on this box. Before installation, verify
that the host is not already configured as an agent for another cluster:

```bash
TVT_KIT_ROOT=/opt/tvt/tvt-edge-online-test-kit-0.1.0-0090aca6ffef

if systemctl cat k3s-agent.service >/dev/null 2>&1; then
  echo "Existing K3s agent detected; stop and obtain approval to replace it."
  false
fi
```

On a dedicated test box where replacement has been approved, remove the old
agent using the uninstall script created by that installation, then verify that
the binary and unit are gone:

```bash
sudo /usr/local/bin/k3s-agent-uninstall.sh
test ! -e /usr/local/bin/k3s
test "$(systemctl show k3s-agent.service -p LoadState --value)" = not-found
```

Do not use this removal block on a node that must remain joined to another
cluster. After the preflight passes, install the local Registry and pinned K3s
server:

```bash
TVT_KIT_ROOT=/opt/tvt/tvt-edge-online-test-kit-0.1.0-0090aca6ffef
cd "${TVT_KIT_ROOT}/source"

sudo bash scripts/install-local-registry.sh \
  --image-archive "${TVT_KIT_ROOT}/images/registry.tar"

curl --fail --silent --show-error http://127.0.0.1:5000/v2/

sudo bash scripts/install-k3s-single-node.sh \
  --installer "${TVT_KIT_ROOT}/k3s/install.sh" \
  --k3s-binary "${TVT_KIT_ROOT}/k3s/k3s"

sudo k3s kubectl get nodes -o wide
```

## 8. Install the K3s node-management plane

Use the TVT virtual environment in `PATH` because the image-lock commands need
the packaged Python dependencies:

```bash
TVT_KIT_ROOT=/opt/tvt/tvt-edge-online-test-kit-0.1.0-0090aca6ffef
TVT_EXEC_PATH=/opt/tvt/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

sudo env PATH="${TVT_EXEC_PATH}" \
  bash scripts/publish-control-images.sh \
  --registry 127.0.0.1:5000 \
  --scheme http \
  --archive-dir "${TVT_KIT_ROOT}/images" \
  --lock-output /var/lib/tvt/node-management-images.lock.json

sudo env PATH="${TVT_EXEC_PATH}" \
  bash scripts/install-k3s-plane.sh \
  --image-lock /var/lib/tvt/node-management-images.lock.json
```

## 9. Import and schedule the Traffic image

```bash
TVT_KIT_ROOT=/opt/tvt/tvt-edge-online-test-kit-0.1.0-0090aca6ffef
TVT_EXEC_PATH=/opt/tvt/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

sudo env PATH="${TVT_EXEC_PATH}" \
  bash scripts/import-pipeline-traffic-image.sh \
  --mode archive \
  --archive-file "${TVT_KIT_ROOT}/images/traffic-edge-runtime-v4.tar" \
  --metadata-directory \
    "${TVT_KIT_ROOT}/source/solution-packs/catalog/traffic-edge-runtime-2026.08.21-v4" \
  --work-dir /var/lib/tvt/pipeline/work \
  --lock-output /var/lib/tvt/pipeline/traffic-image.lock.json \
  --concurrency-lock /var/lib/tvt/pipeline/import.lock

sudo bash scripts/install-pipeline-image-sync.sh \
  --archive-file "${TVT_KIT_ROOT}/images/traffic-edge-runtime-v4.tar" \
  --metadata-directory \
    "${TVT_KIT_ROOT}/source/solution-packs/catalog/traffic-edge-runtime-2026.08.21-v4"

sudo systemctl start tvt-pipeline-image-sync.service
sudo bash /opt/tvt/scripts/verify-pipeline-image-sync.sh
```

## 10. Configure the management plane

```bash
sudo bash scripts/bootstrap-postgresql.sh --venv /opt/tvt/venv
sudo bash scripts/install-tvt-kubeconfig.sh
```

Initialize the site with deployment-specific identifiers. Never put camera or
pipeline credentials in these arguments:

```bash
sudo -u tvt-edge env TVT_DATABASE_URL=postgresql+psycopg:///tvt \
  /opt/tvt/venv/bin/tvt-edge init-site \
  plant-1 plant-1-edge-1 "Plant 1" --timezone Asia/Kolkata
```

Start the required host services and timers:

```bash
sudo systemctl enable --now \
  tvt-edge.service \
  tvt-camera-sync.service \
  tvt-retention.timer \
  tvt-k3s-watchdog.timer \
  tvt-pipeline-image-sync.timer

sudo -u tvt-edge env \
  TVT_DATABASE_URL=postgresql+psycopg:///tvt \
  TVT_KUBECONFIG=/etc/tvt/kubeconfig \
  /opt/tvt/venv/bin/tvt-edge refresh-solutions

sudo bash scripts/install-traffic-qualification.sh
```

The alert dispatcher remains disabled until its sender, recipient policy, and
SendGrid key are configured.

## 11. Final verification

```bash
sudo systemctl is-active \
  docker.service \
  postgresql.service \
  tvt-local-registry.service \
  k3s.service \
  tvt-edge.service \
  tvt-camera-sync.service

sudo k3s kubectl get nodes
sudo k3s kubectl get pods -n apexfabric -o wide

curl --fail --silent --show-error \
  http://127.0.0.1:8088/api/v1/health | python3 -m json.tool

curl --fail --silent --show-error \
  http://127.0.0.1:8088/api/v1/solutions | python3 -m json.tool

sudo -u tvt-edge env \
  TVT_DATABASE_URL=postgresql+psycopg:///tvt \
  TVT_KUBECONFIG=/etc/tvt/kubeconfig \
  /opt/tvt/venv/bin/tvt-edge check
```

The installation loads the Traffic image and catalog entry but deliberately
does not create a Traffic deployment. Camera onboarding, validation, preview,
and deployment remain explicit operator actions.
