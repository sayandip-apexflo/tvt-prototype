# TVT edge operational commands

## Supported production host workflow

Use only the release-bundle entry points for a production host:

```bash
sudo ./prepare-tvt-edge-host.sh --bundle /media/tvt/release --mode offline
sudo reboot
sudo ./prepare-tvt-edge-host.sh --bundle /media/tvt/release --mode offline
sudo ./install-tvt-edge-host.sh \
  --bundle /media/tvt/release \
  --site-config /media/tvt/site.yaml
```

Online host package/driver preparation is available with `--mode online`.
K3s still defaults to the reviewed bundled installer and binary; authorize its
network installer explicitly with `--k3s-mode download`.

Useful non-mutating checks:

```bash
sudo ./prepare-tvt-edge-host.sh \
  --bundle /media/tvt/release --mode offline --verify-only
sudo ./install-tvt-edge-host.sh \
  --bundle /media/tvt/release --verify-only
```

Resume after correcting a failed installation stage:

```bash
sudo ./install-tvt-edge-host.sh \
  --bundle /media/tvt/release \
  --site-config /media/tvt/site.yaml \
  --resume
```

Pipeline credentials, when an online fallback is intentionally used, must be
passed only by root-owned file path with `--pipeline-credentials-file`; secret
values are never accepted as command arguments. Existing `/etc/tvt/*.env`
files are preserved. Installation evidence is under `/var/lib/tvt/install/`.

The remaining commands in this document are internal worker/developer
procedures and are not the supported clean-host workflow.

## Phase 1: edge-local OCI registry and K3s

Run these commands from the `tvt-prototype` repository root on the Ubuntu 24.04
`amd64` edge device. The registry is intentionally reachable only from that
device at `127.0.0.1:5000`.

### 1. Install host prerequisites

```bash
sudo apt-get update
sudo apt-get install -y docker.io curl
```

This refreshes Ubuntu package metadata and installs the Docker daemon and curl.
The repository scripts fail without changing the host if either dependency is
missing.

### 2. Review the immutable image pins and service definition

```bash
sed -n '1,160p' config/platform.env
sed -n '1,220p' deploy/systemd/tvt-local-registry.service.in
```

These commands show the digest-pinned registry and smoke-test images, the fixed
loopback endpoint, persistent storage path, and systemd container arguments.

### 3. Install or reconcile the local registry

```bash
sudo bash scripts/install-local-registry.sh
```

This pulls the pinned Linux `amd64` registry image, creates
`/var/lib/tvt/registry`, installs and starts `tvt-local-registry.service`,
installs the K3s ordering drop-in, and writes the HTTP loopback mirror to
`/etc/rancher/k3s/registries.yaml`. Re-running it preserves registry data. If
K3s is already active, the command restarts K3s and waits for its node to become
Ready.

### 4. Install the pinned single-node K3s release

For an edge device with Internet access:

```bash
sudo bash scripts/install-k3s-single-node.sh --download-installer
```

This downloads the official installer only after the explicit flag, requests
the frozen K3s version from `config/platform.env`, enables datastore Secret
encryption, and verifies that exactly one node is registered. It refuses to run
until the local registry is healthy.

For a reviewed offline installer and K3s binary:

```bash
sudo bash scripts/install-k3s-single-node.sh \
  --installer /media/tvt/k3s/install.sh \
  --k3s-binary /media/tvt/k3s/k3s
```

This performs the same installation without downloading either executable.

### 5. Verify the registry push and K3s pull path

```bash
sudo bash scripts/verify-local-registry.sh
```

This checks the service's image, bind address, and persistent mount; pushes a
pinned BusyBox smoke image with Docker; resolves the local immutable digest;
pulls it with `k3s crictl`; and checks containerd's image list. The stable smoke
tag makes the command idempotent.

```bash
sudo k3s crictl images --digests --no-trunc
```

This independently lists the images and immutable digests known to K3s's
containerd.

### 6. Inspect health, configuration, and logs

```bash
curl --fail --silent --show-error http://127.0.0.1:5000/v2/
sudo systemctl status tvt-local-registry.service k3s.service
sudo systemctl cat k3s.service
sudo cat /etc/rancher/k3s/registries.yaml
sudo journalctl -u tvt-local-registry.service -u k3s.service --since today
```

These commands check the Registry API, show both service states and their
Docker-to-registry-to-K3s ordering, display the installed mirror configuration,
and print current-day logs for troubleshooting.

## Phase 3: synchronize PIPELINE Traffic v4 and refresh the catalog

### 1. Review the PIPELINE pins and inspection

```bash
sed -n '1,200p' config/pipeline.env
less docs/PIPELINE-TRAFFIC-IMAGE.md
```

These commands show the exact v4 commit and delivery path, archive size and
checksum, baked-model contract, local image identity, and developer-only build
inputs. The delivery branch is informational; synchronization does not follow
its current HEAD.

### 2. Install source-download prerequisites

```bash
sudo apt-get update
sudo apt-get install -y git git-lfs
```

This installs Git and Git LFS. The default import downloads only the pinned
Traffic image archive from the pinned PIPELINE checkout. Allow at least 6 GB of
free disk for the 1.93 GB archive, Docker's loaded layers, and registry copy.

### 3. Install automated synchronization

```bash
sudo bash scripts/install-pipeline-image-sync.sh
```

This installs the importer and immutable configuration under `/opt/tvt`,
creates `/var/lib/tvt/pipeline`, installs the root oneshot plus persistent daily
timer, and creates `/etc/tvt/pipeline-image-sync.env` as root-owned mode `0600`.
Leave that file commented for public Git access. If private read access is
needed, place only a read-only Git username/token there; do not put credentials
in the repository.

### 4. Run the first synchronization

```bash
sudo systemctl start tvt-pipeline-image-sync.service
sudo systemctl status tvt-pipeline-image-sync.service \
  tvt-pipeline-image-sync.timer
```

The oneshot waits for the loopback registry, takes a nonblocking import lock,
fetches only the exact commit and v4 Git LFS archive, verifies the archive and
image contract, and pushes the versioned image. It does not delete older
images, generate a DeploymentBundle, call K3s, or change a deployed digest.
Re-running it is a no-op when the private lock and verified registry manifest
digest agree.

### 5. Verify the immutable result

```bash
sudo bash scripts/verify-pipeline-image-sync.sh
sudo python3 -m json.tool /var/lib/tvt/pipeline/traffic-image.lock.json
sudo docker image inspect \
  127.0.0.1:5000/apexfabric/traffic-edge-runtime:intel-285h-2026.08.21-v4
sudo journalctl -u tvt-pipeline-image-sync.service --since today
```

The verifier checks the private lock, v4 provenance fields, timer state, last
oneshot result, and registry manifest bytes. The JSON command displays the
registry-produced digest and immutable `repository@sha256:` reference.

### 6. Seed and refresh the PostgreSQL catalog

The PostgreSQL bootstrap applies the catalog migration and idempotently seeds
the vendored v4 entry:

```bash
sudo bash scripts/bootstrap-postgresql.sh
curl --fail --silent --show-error \
  -X POST http://127.0.0.1:8088/api/v1/solutions/refresh
curl --fail --silent --show-error \
  http://127.0.0.1:8088/api/v1/solutions | python3 -m json.tool
```

Run refresh only after image synchronization succeeds. The entry becomes
`available` only when `Docker-Content-Digest` equals the SHA-256 of the returned
manifest bytes. These catalog calls do not register or alter deployments.

For a bootstrap that has already run, repeat only the idempotent seed command:

```bash
sudo -u tvt-edge env TVT_DATABASE_URL=postgresql+psycopg:///tvt \
  /opt/tvt/venv/bin/tvt-edge seed-solutions \
  --delivery-directory \
  "$PWD/solution-packs/catalog/traffic-edge-runtime-2026.08.21-v4" \
  --registry 127.0.0.1:5000
```

### 7. Repository-local manual import or qualification build

```bash
sudo bash scripts/import-pipeline-traffic-image.sh
sudo python3 -m json.tool build/pipeline/traffic-image.lock.json
```

These commands use the gitignored repository-local development state path.
Production services use `/var/lib/tvt/pipeline` instead.

```bash
sudo bash scripts/import-pipeline-traffic-image.sh --mode build \
  --lock-output build/pipeline/traffic-source-build.lock.json
```

This builds from the same pinned commit using the pinned Ubuntu base manifest
and NPU-driver argument, then stores it under a distinct `-source-build` tag.
Use it only for qualification: the upstream Dockerfiles still resolve mutable
APT packages and ranged Python requirements. The release archive remains the
production import source.

## Phase 4: preview and deploy Traffic from the catalog

The normal operator path is the Solutions page at
`http://127.0.0.1:8088/#solutions`. Select an `available` catalog entry,
enabled/online cameras, applications, inference mode, resources, and normalized
geometry. Review the generated bundle and desired state, then commit that exact
preview.

The equivalent API sequence is:

```bash
curl --fail --silent --show-error \
  -H 'Content-Type: application/json' \
  --data @deployment-request.json \
  http://127.0.0.1:8088/api/v1/deployments/preview \
  > /tmp/tvt-deployment-preview.json

jq '.bundle, .desired_state, .image_reference, .bundle_sha256' \
  /tmp/tvt-deployment-preview.json

jq --slurpfile preview /tmp/tvt-deployment-preview.json \
  '. + {preview_bundle_sha256: $preview[0].bundle_sha256, idempotency_key: "operator-traffic-v4-001"}' \
  deployment-request.json > /tmp/tvt-deployment-commit.json

curl --fail --silent --show-error \
  -H 'Content-Type: application/json' \
  --data @/tmp/tvt-deployment-commit.json \
  http://127.0.0.1:8088/api/v1/deployments
```

`deployment-request.json` contains `catalog_id`, `deployment_id`, `namespace`,
`inference_mode`, `resources`, `state_size`, and `assignments`. Each assignment
contains `camera_id`, `apps`, `fps`, and optional desired-state `config`
geometry. It never contains an RTSP URL or credential.

The existing synchronization worker applies committed state explicitly. It
first runs the equivalent of:

```bash
sudo k3s crictl pull \
  127.0.0.1:5000/apexfabric/traffic-edge-runtime@sha256:<resolved-digest>
```

A pull failure occurs before any Secret or workload mutation. An import or
rollout failure leaves the database's applied revision unchanged; after a
rollout mutation the worker reapplies the previous complete bundle and camera
Secret snapshot. To request an explicit rollback, choose the previous revision
in the Solutions UI or call:

```bash
curl --fail --silent --show-error \
  -H 'Content-Type: application/json' \
  --data '{"bundle_sha256":"<previous-complete-bundle-sha256>"}' \
  http://127.0.0.1:8088/api/v1/deployments/traffic-v4/rollback
```

## Phase 5: qualify the complete Traffic edge path

Install the current package in `/opt/tvt/venv` using the normal application
deployment procedure, then install the manual runner and exact v4 contracts:

```bash
sudo bash scripts/install-traffic-qualification.sh
```

This command installs files only. It does not deploy, roll back, restart, or
reboot anything. Run steady-state qualification against an already applied
deployment:

```bash
sudo /opt/tvt/scripts/qualify-traffic-edge.sh traffic-v4 \
  --strict-events \
  --output /var/lib/tvt/qualification/traffic-v4-steady.json
sudo /opt/tvt/scripts/verify-traffic-qualification.py \
  /var/lib/tvt/qualification/traffic-v4-steady.json
```

Use `--strict-events` only while known traffic crosses a configured camera. If
no event is expected, omit it: endpoint reachability and schema validation
still run, while an empty bounded observation window is recorded as skipped.

To exercise preview and exact commit through the public API, use a non-secret
request containing camera IDs—not RTSP URLs or credentials:

```bash
sudo /opt/tvt/scripts/qualify-traffic-edge.sh traffic-v4 \
  --deployment-request /var/lib/tvt/qualification/deployment-request.json \
  --commit-preview \
  --idempotency-key phase5-traffic-v4-001 \
  --output /var/lib/tvt/qualification/traffic-v4-deploy.json
```

Capture a passing baseline before reboot, reboot as a separate deliberate
operator action, then compare the complete applied bundle/image/PVC identity:

```bash
sudo /opt/tvt/scripts/qualify-traffic-edge.sh traffic-v4 \
  --checkpoint pre-reboot \
  --output /var/lib/tvt/qualification/traffic-v4-pre-reboot.json
sudo reboot
# After the host returns:
sudo /opt/tvt/scripts/qualify-traffic-edge.sh traffic-v4 \
  --checkpoint post-reboot \
  --baseline /var/lib/tvt/qualification/traffic-v4-pre-reboot.json \
  --output /var/lib/tvt/qualification/traffic-v4-post-reboot.json
```

For rollback, use a passing report captured while the intended target bundle
was applied. The following explicitly requests that bundle, waits for applied
state, and compares bundle, image, and PVC invariants to the baseline:

```bash
sudo /opt/tvt/scripts/qualify-traffic-edge.sh traffic-v4 \
  --checkpoint post-rollback \
  --baseline /var/lib/tvt/qualification/traffic-v4-target-baseline.json \
  --rollback-bundle-sha256 <target-complete-bundle-sha256> \
  --output /var/lib/tvt/qualification/traffic-v4-post-rollback.json
```

Every report is atomically written with mode `0600`, excludes Secret bodies,
camera URLs, credentials, and raw event payloads, and exits non-zero if any
required check fails. See `docs/TRAFFIC-EDGE-QUALIFICATION.md` for the complete
acceptance matrix and failure handling.

## TVT edge hardware-driver commands

These commands install and verify the Intel 285H hardware-driver stack used by
TVT. The installer follows the `k3s-prototype` resolve-once policy: its first
run selects the current versions of the same driver packages, saves their exact
versions and hashes, and reuses that lock on later runs.

The installer supports Ubuntu 24.04 on an `amd64` Intel Core Ultra 9 285H-class
host. It is not a Jetson installer; JetPack/L4T must be installed as a matched
NVIDIA BSP image.

### 1. Review the installer

```bash
less scripts/install-tvt-hardware-drivers.sh
```

This displays the script before granting it root access. Confirm the target OS,
hardware checks, package list, download sources, state paths, and cache paths.

### 2. Install the hardware drivers

Run this command from the repository root on the TVT edge device:

```bash
sudo ./scripts/install-tvt-hardware-drivers.sh
```

The command:

- verifies Ubuntu 24.04, `amd64`, Intel 285H hardware, and the required kernel
  modules;
- enables the Intel graphics PPA used by `k3s-prototype`;
- resolves and installs the matching Intel GPU, media, Level Zero, OpenCL,
  oneVPL, VA-API, and NPU packages from the Internet;
- installs `openvino` and `openvino-genai` in
  `/opt/apexfabric/openvino-env`;
- writes the exact resolved recipe to
  `/var/lib/tvt/hardware-driver-recipe.json`; and
- caches the locked NPU archive and Python wheel closure under
  `/var/cache/tvt/hardware-drivers` for repeatable retries.

If an audited Intel host is compatible but its CPU model string is not exactly
285H, use the explicit override:

```bash
sudo env TVT_ALLOW_UNVERIFIED_HARDWARE=true \
  ./scripts/install-tvt-hardware-drivers.sh
```

This bypasses only the CPU-model-name check. OS, architecture, kernel-module,
version-lock, and artifact-integrity checks still run.

Re-running the normal installation command reuses the existing recipe and
cached artifacts. Do not delete the recipe merely to retry a failed install;
deleting it authorizes the selection of newer versions.

### 3. Inspect the locked versions

```bash
sudo python3 -m json.tool /var/lib/tvt/hardware-driver-recipe.json
```

This prints the exact APT versions, OpenVINO versions, wheel hashes, Intel NPU
release URL and digest, OS, architecture, and kernel tuple selected during the
first resolution.

### 4. Reboot the edge device

```bash
sudo reboot
```

The reboot loads the installed GPU/NPU firmware and kernel/userspace stack as a
matched runtime. Wait for the device to return before continuing.

### 5. Verify kernel devices and modules

```bash
test -e /dev/dri/renderD128
test -e /dev/accel/accel0
lsmod | grep -E '^(i915|xe|intel_vpu)\b'
```

These commands confirm that the GPU render node, NPU accelerator node, Intel
graphics module, and Intel NPU module are present after reboot. A non-zero exit
status means hardware qualification has not succeeded.

### 6. Verify the media and compute runtimes

```bash
vainfo --display drm --device /dev/dri/renderD128
clinfo -l
```

`vainfo` checks the Intel VA-API media driver against the render device.
`clinfo -l` lists the OpenCL platforms and devices exposed by the installed
Intel runtime.

### 7. Verify OpenVINO device discovery

```bash
/opt/apexfabric/openvino-env/bin/python - <<'PY'
from openvino import Core

devices = set(Core().available_devices)
print("OpenVINO devices:", sorted(devices))
missing = {"CPU", "GPU", "NPU"} - devices
if missing:
    raise SystemExit("missing OpenVINO devices: " + ", ".join(sorted(missing)))
PY
```

This uses the isolated OpenVINO environment created by the installer and fails
unless OpenVINO detects the CPU, GPU, and NPU.

### 8. Clear the reboot-required marker

Run this only after all verification commands succeed:

```bash
sudo rm -f /var/lib/tvt/hardware-driver-reboot-required
```

This records operationally that post-reboot qualification is complete. It does
not remove the version recipe or cached driver artifacts.
