# TVT prototype

Single box deployment, reusing apexfabric.

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

The first four slices are implemented from the approved
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
  single-node monitoring deployment profile under `deploy/monitoring/`.

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
  --registry registry.local:5000 > /tmp/tvt-traffic-rendered.yaml
```

Test the direct-camera Secret and render contracts without contacting K3s:

```bash
.venv/bin/tvt-k3s apply solution-packs/traffic/traffic-edge-runtime-intel-285h.yaml \
  --registry registry.local:5000 \
  --secret-inputs examples/traffic.secret-inputs.example.json \
  --dry-run
```

The example contains documentation-only camera URLs. Never put real camera
credentials in a tracked file. The management service builds the same input
ephemerally from its encrypted inventory. Non-dry-run `tvt-k3s apply` and the
old local SQLite lifecycle commands are retired from the production CLI.

### Single-node K3s plane

Install the frozen K3s version and configure its registry mirror. Online
installation must be requested explicitly:

```bash
sudo bash scripts/install-k3s-single-node.sh \
  --registry registry.local:5000 \
  --download-installer
```

For an offline installation, provide the reviewed installer and pinned K3s
binary instead:

```bash
sudo bash scripts/install-k3s-single-node.sh \
  --registry registry.local:5000 \
  --installer /media/tvt/k3s/install.sh \
  --k3s-binary /media/tvt/k3s/k3s
```

Build and publish the two initial control images. This also resolves the
registry manifests and writes an immutable digest lock:

```bash
bash scripts/publish-control-images.sh --registry registry.local:5000
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

Use `--registry-scheme https` and `--scheme https` for a TLS registry. The
initial HTTP mode is only for the isolated on-site registry network.

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
runs Alembic, and installs—but deliberately does not enable—the units, including
the fixed K3s API recovery watchdog. Edit the
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

Register the bundle with `POST http://127.0.0.1:8088/api/v1/deployments` as a
JSON object containing `bundle`, `namespace`, and `registry`.

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
```
