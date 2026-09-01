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

The first two K3s-plane slices are implemented from the approved
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
- a `tvt-k3s` Python CLI for validation, rendering, Apply/prune, status,
  declarative start/stop, revision history, and rollback.

The alert dispatcher and full observability stack are intentionally not part
of this implementation slice.

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
credentials in a tracked file. The production management service will build
the same JSON input ephemerally from its encrypted inventory.

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

### Solution Pack lifecycle

Once K3s and the Traffic runtime image are available, apply the bundle and its
ephemeral direct-camera inputs:

```bash
sudo .venv/bin/tvt-k3s apply \
  solution-packs/traffic/traffic-edge-runtime-intel-285h.yaml \
  --registry registry.local:5000 \
  --secret-inputs /run/tvt/traffic-secret-inputs.json \
  --state-dir /var/lib/tvt/runtime
```

Changing a camera URL or password requires running Apply again. The runtime
updates the bundle-named Secrets and performs a controlled Deployment rollout
so Kubernetes `subPath` mounts receive the new values.

Inspect and control the deployment declaratively:

```bash
sudo .venv/bin/tvt-k3s list --state-dir /var/lib/tvt/runtime
sudo .venv/bin/tvt-k3s status traffic-edge-intel-285h \
  --state-dir /var/lib/tvt/runtime
sudo .venv/bin/tvt-k3s stop traffic-edge-intel-285h \
  --state-dir /var/lib/tvt/runtime
sudo .venv/bin/tvt-k3s start traffic-edge-intel-285h \
  --state-dir /var/lib/tvt/runtime
sudo .venv/bin/tvt-k3s history traffic-edge-intel-285h \
  --state-dir /var/lib/tvt/runtime
sudo .venv/bin/tvt-k3s rollback traffic-edge-intel-285h \
  --state-dir /var/lib/tvt/runtime
```

The runtime database is created with mode `0600`. It stores validated bundles,
safe rollout summaries, and active revision metadata, but never stores
ephemeral camera URLs. TVT rejects `applications[].secrets` at the runtime
boundary. A rollback which changes camera assignments, or follows a failed
camera-Secret update, requires a matching new `--secret-inputs` file because
prior camera credentials are intentionally not retained.

### Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
```
