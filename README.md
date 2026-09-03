# TVT prototype

Single box deployment, reusing apexfabric.

## Production edge installation

The supported production entry points are the two release-bundle scripts. Do
not run the component installers individually on a new edge host.

On a clean Ubuntu 24.04 Intel Core Ultra 285H host, mount the checksum-complete
release bundle and run:

```bash
sudo ./prepare-tvt-edge-host.sh --bundle /media/tvt/release --mode offline
sudo reboot
sudo ./prepare-tvt-edge-host.sh --bundle /media/tvt/release --mode offline
sudo ./install-tvt-edge-host.sh \
  --bundle /media/tvt/release \
  --site-config /media/tvt/site.yaml
```

The first preparation pass installs host packages and the pinned Intel driver
closure, records `/var/lib/tvt/install/prepare-state.json`, and stops without
rebooting. The second pass proves that the reboot occurred and verifies GPU,
NPU, VA-API, OpenCL, OpenVINO, Docker, and PostgreSQL before clearing the driver
reboot marker. The application installer will not run before that state is
`prepared`.

A site file contains identifiers only; never put credentials in it:

```yaml
site_key: plant-1
edge_id: plant-1-edge-1
display_name: Plant 1
timezone: Asia/Kolkata
```

Installation is staged and idempotent. Safe reruns skip completed stages, and
`--resume` makes operator intent explicit after a corrected failure. Use
`--verify-only` on either entry point for a read-only health check. The final
installer writes non-secret evidence to
`/var/lib/tvt/install/installation-report.json` and never creates a Traffic
deployment; camera onboarding and deployment remain explicit UI actions.

Create a release with `scripts/make-tvt-edge-release.sh`. Its required input
library contains reviewed K3s files, the full offline APT/driver closure, and
prebuilt amd64 archives for Distribution, both node-management images, and
Traffic v4. The script locks those inputs, runs the source gates, compiles the
React UI, builds the application and dependency wheels, constructs and
independently verifies the immutable release directory, and writes a
reproducible transport archive, checksum, and release report. The complete
input, build, verification, publication, and new-commit rebuild procedure is in
[TVT edge release build runbook](docs/EDGE-RELEASE-BUILD.md).

The project includes five cameras. We have instructed the customer to install them at the appropriate locations:

- 2 cameras at the main entrance to cover people entering and exiting
- 2 cameras at the plant entrance to cover people entering and exiting
- 1 camera at the back exit to cover people exiting

The required features are:

- Face recognition
- Face enrollment
- Automatic Number Plate Recognition (ANPR)*
- Reporting on the time people spend inside versus outside the plant, attendance.
- Daily vehicle entry and exit reporting
- Automated daily reports via email

## Error reporting and monitoring

The secure tunnel should be for last-resort shell access. The bulk of debugging should happen off metrics/logs/traces/snapshots that are already flowing to a central dashboard, so 90% of issues are diagnosable without ever opening a session to the box.

## Current implementation

The first five slices are implemented from the approved
`k3s-prototype` commit `bcb58030f89b22b14ff1dbd0a68c5806d2f6a002`.
It includes:

- the unchanged `solution-packs/` schema and Traffic pack files;
- the unchanged bundle validator, camera-locality rules, Kubernetes renderer,
  server-side Apply/prune behavior, and their focused reference tests;
- K3s Namespace, RBAC, `ApexNodeStatus`, node reporter, and status-controller
  manifests;
- a GStreamer-free node reporter which checks Intel devices and VA-API;
- strict materialization of per-deployment desired-state and camera-source
  Secrets; and
- pinned single-node K3s installation and registry-mirror tooling;
- digest-pinned publication and installation of the node-management images;
  and
- a host-local PostgreSQL management plane with an encrypted camera inventory,
  immutable desired revisions, lifecycle operations, audit, and retention;
- a loopback-only `tvt-edge` API and CLI; and
- a leased reconciliation worker which materializes camera Secrets only in
  memory, applies them server-side, invokes the unchanged renderer, waits for
  rollouts, and advances applied state only after success; and
- an authenticated Alertmanager receiver with durable alert state,
  acknowledgement-aware notification policy, a persistent SMTP retry outbox,
  redacted delivery history, and a separate host dispatcher service; and
- bounded `prometheus_client` metrics, redacting single-line JSON logs, and a
  single-node monitoring deployment profile under `deploy/monitoring/`; and
- a manual, fail-closed Traffic v4 qualification runner that validates pinned
  provenance, the image lock, Intel acceleration, services, K3s rollout and
  persistent state, runtime metrics/events, reboot invariants, and rollback
  invariants into private verifiable evidence reports.

The web UI, active ONVIF/RTSP probing, host emergency alert spool, fleet
heartbeat/event senders, and production dashboard tuning are intentionally
deferred.

### Local runtime

Python 3.12 is required. Create an environment and install the runtime:

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

Validate and render the unchanged Traffic runtime bundle:

```bash
.venv/bin/tvt-k3s validate solution-packs/traffic/traffic-edge-runtime-intel-285h.yaml
.venv/bin/tvt-k3s render solution-packs/traffic/traffic-edge-runtime-intel-285h.yaml \
  --registry 127.0.0.1:5000 > /tmp/tvt-traffic-rendered.yaml
```

Test the direct-camera Secret and render contracts without contacting K3s:

```bash
.venv/bin/tvt-k3s apply solution-packs/traffic/traffic-edge-runtime-intel-285h.yaml \
  --registry 127.0.0.1:5000 \
  --secret-inputs examples/traffic.secret-inputs.example.json \
  --dry-run
```

The example contains documentation-only camera URLs. Never put real camera
credentials in a tracked file. The management service builds the same input
ephemerally from its encrypted inventory. Non-dry-run `tvt-k3s apply` and the
old local SQLite lifecycle commands are retired from the production CLI.

### Edge-local OCI registry

Workload images are stored on the edge device in a Docker Distribution 2.8.3
registry. The registry container image is pinned to its Linux `amd64` digest,
the service publishes only on `127.0.0.1:5000`, and image data persists in the
host directory `/var/lib/tvt/registry`.

```text
Docker build/pull -> Docker push -> 127.0.0.1:5000 -> K3s/containerd pull
                         systemd: Docker -> registry -> K3s
```

Install Docker and curl from Ubuntu, then install the registry. The installer
pulls the immutable registry image, installs its systemd unit and the K3s
ordering drop-in, writes `/etc/rancher/k3s/registries.yaml`, and restarts K3s
only when K3s is already active:

```bash
sudo apt-get update
sudo apt-get install -y docker.io curl
sudo bash scripts/install-local-registry.sh
```

The registry intentionally has no TLS or authentication because it is reachable
only through the host loopback interface. Do not change the publish address
without adding TLS, authentication, firewall policy, and a corresponding threat
review. Re-running the installer retains `/var/lib/tvt/registry` and reconciles
the service to the pinned image.

### PIPELINE Traffic image import

The Traffic workload comes from the `PIPELINE` ApexFabric V1 delivery branch,
but synchronization fetches exact commit
`6513562c9d27eba511322280e19e054c3948ae4d` rather than following mutable branch
HEAD. The production artifact is the baked-model v4 archive at
`delivery/apexfabric-v1/intel-285h/traffic/image-2026.08.21-v4.tar`; its size,
SHA-256, image contract, expected model hashes, and local image identity are
pinned in `config/pipeline.env`.

Import it after the local registry is running:

```bash
sudo apt-get install -y git git-lfs
sudo bash scripts/import-pipeline-traffic-image.sh
```

The archive path verifies the 1.93 GB Git LFS artifact before loading it. The
script validates the Linux `amd64` OCI/runtime contract and the six baked
OpenVINO model files without starting the CV runtime, pushes the versioned tag
to `127.0.0.1:5000`, verifies the registry manifest digest against its bytes,
and atomically writes a private immutable lock under `build/pipeline/` for a
repository run. When that lock still matches the registry, a repeat invocation
is a no-op. `--mode build` is developer qualification only and is never used by
automated synchronization.

See [PIPELINE Traffic image provenance](docs/PIPELINE-TRAFFIC-IMAGE.md) for the
complete v4 provenance, contract, model checksums, and lock format.
See [Traffic edge qualification](docs/TRAFFIC-EDGE-QUALIFICATION.md) for the
Phase 5 live-device acceptance, reboot, and rollback procedure.

For an installed edge, install the root oneshot and persistent timer, run the
first import, and verify the known-good lock and registry bytes:

```bash
sudo bash scripts/install-pipeline-image-sync.sh
sudo systemctl start tvt-pipeline-image-sync.service
sudo bash scripts/verify-pipeline-image-sync.sh
```

Installed synchronization stores operational state under
`/var/lib/tvt/pipeline`. It never deletes older images, generates a bundle, or
changes a Kubernetes workload.

### Single-node K3s plane

Install the frozen K3s version and configure its registry mirror. Online
installation must be requested explicitly:

```bash
sudo bash scripts/install-k3s-single-node.sh \
  --download-installer
```

For an offline installation, provide the reviewed installer and pinned K3s
binary instead:

```bash
sudo bash scripts/install-k3s-single-node.sh \
  --installer /media/tvt/k3s/install.sh \
  --k3s-binary /media/tvt/k3s/k3s
```

Build and publish the two initial control images. This also resolves the
registry manifests and writes an immutable digest lock:

```bash
bash scripts/publish-control-images.sh --registry 127.0.0.1:5000
```

Apply and verify the node-management plane using that lock:

```bash
sudo bash scripts/install-k3s-plane.sh \
  --image-lock build/node-management-images.lock.json
```

The installers refuse a cluster with anything other than one registered Node.
Verification requires a Ready node, healthy reporter/controller rollouts, an
accepted `ApexNodeStatus`, the controller-owned qualification label, expected
RBAC, and digest-pinned workload images.

Verify the complete Docker-to-registry-to-containerd path after K3s is ready:

```bash
sudo bash scripts/verify-local-registry.sh
```

The verifier pushes a digest-pinned BusyBox smoke image, resolves its digest
from the local registry, pulls that immutable reference with `k3s crictl`, and
confirms it in containerd's image list. It is safe to run repeatedly.

### Host management plane and Solution Pack lifecycle

PostgreSQL stays on the Ubuntu edge host. It is bound only to Unix sockets;
neither PostgreSQL nor its data directory is placed in K3s. Bootstrap it and
install the service units after reviewing the example environment file:

```bash
sudo bash scripts/bootstrap-postgresql.sh
sudo bash scripts/install-tvt-kubeconfig.sh
sudo systemctl enable --now tvt-edge.service tvt-camera-sync.service \
  tvt-retention.timer tvt-k3s-watchdog.timer
```

The bootstrap creates the local role/database, generates the credential key,
runs Alembic, idempotently seeds the vendored v4 Traffic entry into the
PostgreSQL solution catalog, and installs—but deliberately does not enable—the
units, including the fixed K3s API recovery watchdog. Edit the
environment file before enabling them. Initialize the site and register the
unchanged Traffic bundle with the loopback service:

The root-owned watchdog accepts no arguments. It checks the fixed local K3s
readiness endpoint every 30 seconds, restarts `k3s.service` only after 120
seconds of continuous API failure, and then enforces a 10-minute cooldown.
Its bounded state and cumulative action counters are exposed through management
health and metrics; `journalctl -u tvt-k3s-watchdog.service` provides the local
action audit.

```bash
sudo -u tvt-edge /opt/tvt/venv/bin/tvt-edge init-site \
  plant-01 edge-01 'Plant 01' --timezone Asia/Kolkata
```

Catalog state is read with `GET /api/v1/solutions` and explicitly refreshed
from the edge-local OCI registry with `POST /api/v1/solutions/refresh`.
Refreshing only resolves the v4 tag and availability; it does not register or
modify deployments. Create or reconfigure a deployment through the Solutions
page, or call `POST /api/v1/deployments/preview` followed by
`POST /api/v1/deployments` with the returned `bundle_sha256`. Raw bundle upload
is restricted to the trusted internal installer endpoint.

Catalog deployment commits accept only an `available` entry with a resolved
`sha256:` digest. The generated Traffic bundle carries that digest, uses the
same image for the plan compiler and runtime, mounts camera Secrets and desired
state, supplies `/plans`, `/tmp/apexfabric`, retained `/state`, `/dev/dri`, and
`/dev/accel`, and never creates a model mount or model PVC. The synchronization
worker runs `k3s crictl pull repository@sha256:...` before changing Kubernetes.
If rollout fails after mutation, it restores the previous complete bundle,
camera Secret snapshot, and image digest while leaving the failed revision
pending for operator action or explicit rollback.

The same edge service hosts the React management console at
`http://127.0.0.1:8088/`. It includes site health, camera onboarding and
validation, bounded network discovery, Solution Pack assignments and lifecycle,
alerts and notification history, audit activity, and the K3s node/workload view
that previously lived in the prototype port-8088 console. Workload telemetry is
read-only and restricted to ApexFabric-managed Deployments.

The console is a TypeScript/Vite project in `ui/`. For local development, run
the API on port 8088 and start Vite's loopback development server:

```bash
npm --prefix ui install
npm --prefix ui run dev
```

Create the self-contained assets packaged with `tvt_edge` before building a
wheel or installing the service:

```bash
npm --prefix ui run build
```

Camera onboarding, credential rotation, assignment commit, start, stop, and
rollback are API operations. Assignment and lifecycle changes create immutable
desired revisions; rotating an assigned camera's credentials requeues each
affected deployment as a new revision. The sync worker updates the bundle-named
Secrets and performs a controlled Deployment rollout so Kubernetes `subPath`
mounts receive changed values. See [Slices 3 and 4](docs/SLICE-3-4.md) for the
API and operational contract.

The bootstrap also installs `tvt-alert-dispatcher.service`, creates its
separate `tvt-alert` OS/database role, and generates
`/etc/tvt/alertmanager-webhook.token`. Before enabling the dispatcher, install
a restricted SendGrid API key at `/etc/tvt/sendgrid-api-key` owned by
`root:tvt-alert` with mode `0640`, replace the example sender/address settings,
configure at least one `notification_policies` row, and point Alertmanager at
`POST /internal/v1/alerts/alertmanager` with that bearer token. Then enable it:

```bash
sudo systemctl enable --now tvt-alert-dispatcher.service
```

The management API exposes `GET /api/v1/alerts`, acknowledgement at
`POST /api/v1/alerts/{alert_id}/acknowledge`, and redacted outbox history at
`GET /api/v1/alerts/{alert_id}/notifications`.

Existing installations should enable K3s datastore Secret encryption during a
reviewed maintenance window:

```bash
sudo bash scripts/enable-k3s-secrets-encryption.sh
```

New K3s installs enable `secretbox` datastore encryption automatically. The
management database stores camera credentials only as AES-256-GCM ciphertext;
the API never returns credential material, logs and error details are redacted,
and the database never stores rendered Kubernetes Secret bodies.

### Tests

```bash
.venv/bin/python -m pytest -q
npm --prefix ui test
npm --prefix ui run build
```
