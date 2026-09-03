# Traffic edge Phase 5 qualification

Phase 5 is a manual, bounded acceptance gate for the deployed PIPELINE Traffic
v4 solution on the single Intel Core Ultra 9 285H edge. It creates verifiable
local evidence; it is not a daemon, a background monitor, or an automatic
reboot/rollback mechanism.

## Scope and pass criteria

A passing report proves all of the following at one point in time:

- the vendored image, desired-state, metrics, and analytics-event contracts
  match their SHA-256 records from PIPELINE commit
  `6513562c9d27eba511322280e19e054c3948ae4d`;
- Ubuntu reports `amd64`, the 285H CPU is present, `/dev/dri` and `/dev/accel`
  exist, the reboot-required marker is absent, and VA-API, OpenCL, and OpenVINO
  expose CPU/GPU/NPU as expected;
- PostgreSQL, Docker, the loopback registry, K3s, the management service,
  camera synchronization, and image synchronization timer are active;
- the local Registry API responds, the private image lock exactly binds the
  release archive, immutable registry digest, and all runtime contracts, and
  K3s containerd can inspect that immutable reference;
- PostgreSQL desired/applied revisions agree, the catalog and deployed digest
  agree, and the single K3s node is Ready and hardware-qualified;
- the rendered Deployment uses the same digest-pinned image for the plan
  compiler and runtime, mounts desired state, camera source files, temporary
  plans, persistent state, GPU, and NPU, and does not add a host model mount;
- referenced Secrets exist, without reading or recording Secret bodies;
- the plan compiler exited successfully, the runtime container is Ready with
  the expected digest, and the retained state PVC is Bound;
- `/healthz`, `/readyz`, and `/metrics` are available through the existing
  management API telemetry path, and metrics validate against the pinned
  schema while reporting loaded plan/models and a running child process; and
- a bounded Kubernetes Pod-proxy request reaches `/events`; every observed
  JSON event validates against the pinned analytics schema.

With `--strict-events`, at least one analytics event must arrive. Without it,
an empty observation window is a documented skip because event production
depends on visible traffic; malformed events always fail qualification.

## Safety boundaries

The runner accepts only an uncredentialed loopback HTTP management URL and only
calls `/api/v1/` routes. Subprocesses use fixed argument arrays, bounded
timeouts, and bounded captured output. A deployment request is rejected before
any API call if it contains a credential-like field or an RTSP URL.

Evidence includes only safe identifiers, counts, revisions, digests, check
status, and the PVC UID. It never stores Secret values, RTSP URLs, camera-source
contents, raw metrics, raw events, or command output. Reports are atomically
created as non-symlink mode-`0600` files under `/var/lib/tvt/qualification` by
default. The standalone verifier rejects unsafe, duplicate-check, incomplete,
failed, or permission-broad reports.

Baseline, deployment-request, and image-lock inputs must also be regular
non-symlink mode-`0600` JSON files no larger than 2 MiB.

Qualification does not restart a service, reboot the host, delete an image,
delete a workload, or choose a rollback target. The only mutations available
are explicit `--commit-preview` and `--rollback-bundle-sha256` flags. Both use
the existing public management API and its normal synchronization worker.

## Normal acceptance run

First complete Phases 1–4 and hardware verification. Install the current TVT
package in `/opt/tvt/venv`, then install the runner and pinned contracts:

```bash
sudo bash scripts/install-traffic-qualification.sh
sudo /opt/tvt/scripts/qualify-traffic-edge.sh traffic-v4 \
  --strict-events \
  --output /var/lib/tvt/qualification/traffic-v4-steady.json
sudo /opt/tvt/scripts/verify-traffic-qualification.py \
  /var/lib/tvt/qualification/traffic-v4-steady.json
```

The first command exits `0` only for a passing report. The verifier is a
separate integrity and safety check and should also exit `0`. Preserve reports
according to the site's operational evidence retention policy; they are not
automatically uploaded.

## Preview and deployment exercise

Create a root-owned mode-`0600` JSON request containing only catalog ID,
deployment ID, namespace, inference settings, resources, state size, and
camera-ID/application assignments. Then run:

```bash
sudo /opt/tvt/scripts/qualify-traffic-edge.sh traffic-v4 \
  --deployment-request /var/lib/tvt/qualification/deployment-request.json \
  --commit-preview \
  --idempotency-key phase5-traffic-v4-001 \
  --output /var/lib/tvt/qualification/traffic-v4-deploy.json
```

The runner previews first, commits the exact preview digest, waits up to five
minutes by default for the database to report applied state, and then performs
the full acceptance matrix. `--wait-seconds` may be increased up to 3600 for a
known slow rollout.

## Reboot continuity

Capture a passing pre-reboot report, perform the reboot separately, and pass
the original report as the post-reboot baseline:

```bash
sudo /opt/tvt/scripts/qualify-traffic-edge.sh traffic-v4 \
  --checkpoint pre-reboot \
  --output /var/lib/tvt/qualification/traffic-v4-pre-reboot.json
sudo reboot
# Wait for the host and services to return.
sudo /opt/tvt/scripts/qualify-traffic-edge.sh traffic-v4 \
  --checkpoint post-reboot \
  --baseline /var/lib/tvt/qualification/traffic-v4-pre-reboot.json \
  --output /var/lib/tvt/qualification/traffic-v4-post-reboot.json
```

The post-check fails if deployment ID, namespace, complete bundle digest,
image digest, or PVC UID changed, in addition to rerunning every normal check.

## Rollback continuity

Before changing configuration, retain a passing report for the currently
applied bundle. That report is the target baseline if a rollback is required.
Obtain the exact complete bundle digest from its safe report or deployment
history, then run:

```bash
sudo /opt/tvt/scripts/qualify-traffic-edge.sh traffic-v4 \
  --checkpoint post-rollback \
  --baseline /var/lib/tvt/qualification/traffic-v4-target-baseline.json \
  --rollback-bundle-sha256 <target-complete-bundle-sha256> \
  --output /var/lib/tvt/qualification/traffic-v4-post-rollback.json
```

The management API rejects an unknown or incomplete target. On acceptance, the
normal worker reapplies the complete prior bundle and camera Secret snapshot.
The qualification then fails unless the target bundle digest, image digest,
and original PVC identity all match the baseline and the full runtime checks
pass.

## Failure handling

Do not edit a failed report. Run the standalone verifier, inspect only the
failed check IDs and redacted summaries, and use the existing service status,
journal, deployment audit, and monitoring paths to diagnose the component.
After correcting the problem, create a new report. A skipped analytics-event
check is acceptable only when strict event observation was intentionally
disabled; all other required checks must pass.
