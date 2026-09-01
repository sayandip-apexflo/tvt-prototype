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

The first K3s-plane slice is implemented from the approved
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
- a small `tvt-k3s` Python CLI for validation, rendering, dry runs, and Apply.

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

Build and push the two initial control images to the site registry:

```bash
docker build -t registry.local:5000/apexfabric/node-reporter:0.1.0 \
  -f apexfabric/node_management/reporter/Dockerfile .
docker build -t registry.local:5000/apexfabric/node-status-controller:0.1.0 \
  -f apexfabric/node_management/status_controller/Dockerfile .
docker push registry.local:5000/apexfabric/node-reporter:0.1.0
docker push registry.local:5000/apexfabric/node-status-controller:0.1.0
```

On a machine with the pinned K3s release already installed, apply the
single-node foundation:

```bash
sudo bash scripts/install-k3s-plane.sh --registry registry.local:5000
```

The installer refuses a cluster with anything other than one registered Node.
It labels that Node for the Intel 285H profile and applies the reference
foundation and node-management resources. It does not install K3s, configure
the registry mirror, or build workload images yet.

Once K3s and the Traffic runtime image are available, apply the bundle and its
ephemeral direct-camera inputs:

```bash
sudo .venv/bin/tvt-k3s apply \
  solution-packs/traffic/traffic-edge-runtime-intel-285h.yaml \
  --registry registry.local:5000 \
  --secret-inputs /run/tvt/traffic-secret-inputs.json
```

Changing a camera URL or password requires running Apply again. The runtime
updates the bundle-named Secrets and performs a controlled Deployment rollout
so Kubernetes `subPath` mounts receive the new values.

### Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
```
