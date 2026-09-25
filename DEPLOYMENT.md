# CV solution image deployment

## Purpose

This runbook upgrades an already-deployed TVT CV workload to a new,
checksum-pinned vendor release, for example v2 to v3. It assumes the vendor has
published a complete release and the TVT engineer has integrated its immutable
archive and metadata into a TVT release bundle.

Use only the consolidated host operation:

```bash
sudo ./scripts/tvt-edge-operations.sh upgrade-solution-image ACTION OPTIONS
```

Do not edit Kubernetes Deployments, database rows, catalog rows, image tags, or
camera Secrets by hand.

## Availability statement

The preparation phase does not interrupt the running CV workload. It verifies
and stages the release, imports the image into the edge-local registry,
pre-pulls the exact digest into K3s containerd, seeds the catalog, and previews
the deployment while the current Pod continues to run.

Activation has a real interruption. The CV Deployment has one replica and the
Intel accelerator profile uses Kubernetes `Recreate`. Kubernetes terminates
the old Pod before it creates the new Pod. The outage lasts from termination of
the old runtime until the new runtime has started, loaded its models, opened its
camera streams, and passed readiness. Pre-pulling removes image-transfer time
from this interval, but it does not make the rollout zero-downtime.

A failed new-image rollout restores the previous complete bundle and image.
After that restoration, synchronization enters `operator_required` instead of
automatically retrying the bad image and causing repeated outages. The
activation command then commits an explicit rollback to the previous applied
snapshot. A failed restoration itself remains retryable because the old
workload has not been proven healthy.

True uninterrupted CV processing would require a separately designed
blue/green or multi-replica runtime that can safely share accelerators, camera
streams, event ownership, and persistent state. That is not the current
single-box contract.

## What the operation preserves

The target bundle is generated from the last successfully applied assignment
snapshot inside one database transaction. It preserves:

- deployment ID and namespace;
- lifecycle intent;
- cameras, requested FPS, applications, geometry, and application config;
- inference mode, resource requests and limits, and state PVC size;
- the existing state PVC.

The host script never retrieves camera credentials or RTSP URLs. Camera
credentials remain encrypted in PostgreSQL and are materialized only through
the existing sync worker when it applies the camera-source Secret.

The upgrade commit uses the source applied bundle SHA-256 as a
compare-and-swap token. If an operator changes assignments, enrollment, start
or stop state, or another deployment revision between preview and commit, the
commit fails and must be prepared again.

## Prerequisites

Before using this runbook, verify all of the following:

1. The vendor release contains the amd64 OCI archive and the complete solution
   delivery metadata: `image-contract.yaml`,
   `desired-state.schema.json`, `desired-state.example.json`,
   `metrics.schema.json`, `analytics-event.schema.json`,
   `analytics-event.example.json`, and `provenance.json`.
2. `config/pipeline.env` pins the vendor repository commit, catalog ID,
   version, archive name, archive size, archive SHA-256, source image tag,
   edge-local repository/tag, metadata checksums, and compatibility-module
   checksum.
3. The matching catalog directory exists at
   `solution-packs/catalog/tvt-mills-pilot-<version>/`.
4. A hardware-profile-specific TVT release bundle has been built with
   `scripts/make-tvt-edge-release.sh`. Do not use a raw vendor checkout as
   the edge bundle.
5. The edge is healthy, has enough free space for a staged copy of the OCI
   archive and its unpacked registry layers, and the local registry, Docker,
   K3s, PostgreSQL, `tvt-edge.service`, and
   `tvt-camera-sync.service` are running.
6. No enrollment session or other deployment change is in progress. The
   deployment must report one stable applied desired revision.
7. The installed TVT application includes the
   `/deployments/{deployment_id}/upgrade` endpoints. If it does not,
   upgrade the TVT application from the new release bundle first as described
   below. This restarts the management services but does not replace the
   currently running CV Pod.

Do not put SSH passwords, sudo passwords, registry credentials, camera
credentials, or credential-bearing URLs in a command line, release bundle,
state file, or this document.

## 1. Integrate and build the release

On the release workstation, update the immutable pins and catalog from the
vendor's published release. Validate that the archive and metadata agree:

```bash
.venv/bin/python scripts/validate-solution-delivery.py \
  --catalog solution-packs/catalog/tvt-mills-pilot-<v3-version> \
  --config config/pipeline.env \
  --archive /srv/tvt-release/inputs-v3/<vendor-image>.oci.tar
```

Probe the target edge and build a release for that exact inventory:

```bash
./scripts/tvt-edge-operations.sh probe-edge-hardware \
  --ssh <edge-ssh-target> \
  --output /srv/tvt-release/edge-hardware-inventory.json

./scripts/make-tvt-edge-release.sh \
  --input-directory /srv/tvt-release/inputs-v3 \
  --output-directory /srv/tvt-release/output/tvt-edge-release-<release>-intel-285h \
  --edge-inventory /srv/tvt-release/edge-hardware-inventory.json \
  --version <release> \
  --source-commit <full-40-character-tvt-git-sha>
```

The release builder and `verify-release` gate every file through
`checksums.sha256`. Transfer the completed bundle to stable local storage on
the edge; do not activate directly from a directory that will disappear during
the operation.

## 2. Upgrade the management capability when required

This step is needed once on edges running a TVT application version that
predates `upgrade-solution-image`. It upgrades the host application,
database schema, and dashboard, but it does not change the CV desired bundle:

```bash
sudo <release-bundle>/scripts/tvt-edge-operations.sh upgrade-application \
  --bundle <release-bundle>
```

Verify the loopback API and management service before proceeding:

```bash
sudo systemctl is-active tvt-edge.service tvt-camera-sync.service
curl --fail --silent http://127.0.0.1:8089/api/v1/health
```

The application upgrade has its own retained release and database backup under
`/var/lib/tvt/install/`.

## 3. Identify the deployment

List deployments locally on the edge:

```bash
curl --fail --silent http://127.0.0.1:8089/api/v1/deployments
```

Choose the `deployment_id` whose `sync_state` is `applied`. Record its
`applied_revision`, `applied_bundle_sha256`, and
`applied_image_digest` in the change record. This response contains no camera
credentials or RTSP URLs.

## 4. Prepare v3 without changing the running workload

Run prepare from the new, verified release bundle:

```bash
sudo <release-bundle>/scripts/tvt-edge-operations.sh \
  upgrade-solution-image prepare \
  --bundle <release-bundle> \
  --deployment-id <deployment-id>
```

The command returns JSON containing an `operation_id`. Keep that value. Prepare
is resumable: passing an explicit operation ID allows the same state to be
reused after an interrupted shell session:

```bash
sudo <release-bundle>/scripts/tvt-edge-operations.sh \
  upgrade-solution-image prepare \
  --bundle <release-bundle> \
  --deployment-id <deployment-id> \
  --operation-id <operation-id>
```

Prepare performs these gates before reporting `prepared`:

1. verifies the full TVT release manifest and checksum coverage;
2. copies the pinned archive and metadata into a root-only operation directory;
3. validates catalog, provenance, pipeline pins, archive hash, size, and tag;
4. imports the archive, adds the checksum-pinned TVT compatibility layer, and
   publishes the versioned image to the loopback registry;
5. records the registry-produced immutable digest in an image lock;
6. runs `k3s crictl pull` and `inspecti` for the exact
   `repository@sha256` reference;
7. seeds and refreshes the target catalog entry;
8. asks the management service to reconstruct the stable applied assignment
   snapshot and preview the target bundle;
9. verifies that the preview's catalog ID and image reference match the staged
   image lock.

All state and staged artifacts are under:

```text
/var/lib/tvt/pipeline/upgrades/<operation-id>/
```

The directory is root-only (mode `0700`), and its JSON state and staged
files are mode `0600`.

Inspect preparation at any time:

```bash
sudo <release-bundle>/scripts/tvt-edge-operations.sh \
  upgrade-solution-image status \
  --operation-id <operation-id>
```

Do not continue unless `status` is `prepared` and the source bundle/image
match the recorded pre-change values.

## 5. Activate during the maintenance window

Start activation:

```bash
sudo <release-bundle>/scripts/tvt-edge-operations.sh \
  upgrade-solution-image activate \
  --operation-id <operation-id> \
  --timeout 900
```

Activation performs one compare-and-swap commit. The sync worker applies the
target bundle, waits for the Kubernetes Deployment rollout, and marks the
revision applied only after the rollout succeeds. On success, the command also
updates the installed pipeline synchronization configuration and canonical
image lock so scheduled verification uses v3.

Because `Recreate` is used, observe the maintenance window from another
terminal:

```bash
sudo k3s kubectl get pods -n apexfabric -w
```

Do not submit camera edits, enrollment requests, start/stop actions, or another
solution upgrade while activation is running.

## 6. Verify the deployment

Check durable orchestration state:

```bash
sudo <release-bundle>/scripts/tvt-edge-operations.sh \
  upgrade-solution-image status \
  --operation-id <operation-id>
```

A successful operation reports:

- operation `status: applied`;
- deployment `sync_state: applied`;
- `applied_bundle_sha256` equal to `target_bundle_sha256`;
- `applied_image_digest` equal to `target_image_digest`.

Then run the read-only platform and application checks:

```bash
sudo <release-bundle>/install-tvt-edge-host.sh \
  --bundle <release-bundle> \
  --site-config <site-config> \
  --prepare-mode offline \
  --verify-only

sudo systemctl is-active tvt-edge.service tvt-camera-sync.service
curl --fail --silent http://127.0.0.1:8089/api/v1/health
```

Also verify the site-specific cameras and expected analytics in the TVT
dashboard. A Kubernetes-ready Pod proves rollout completion, not inference
correctness; use the Traffic qualification workflow when release acceptance
requires inference evidence.

## 7. Failure behavior and rollback

If target rollout fails after Kubernetes mutation, the sync worker first
reapplies the previous complete bundle and waits for its rollout. For an image
change that was successfully restored, it sets `operator_required` and does
not retry the failed target automatically. The activation command detects this
state and commits an explicit rollback to the previous bundle snapshot.

If activation times out, the command does not guess whether the rollout is
still progressing. Inspect status and cluster health first:

```bash
sudo <release-bundle>/scripts/tvt-edge-operations.sh \
  upgrade-solution-image status \
  --operation-id <operation-id>

sudo k3s kubectl get deployments,pods -n apexfabric
```

When rollback is appropriate, run:

```bash
sudo <release-bundle>/scripts/tvt-edge-operations.sh \
  upgrade-solution-image rollback \
  --operation-id <operation-id> \
  --timeout 900
```

Rollback creates a new desired revision from the previously applied immutable
assignment snapshot and current camera credential versions. It retains the
state PVC. If v3 had already become active, it also restores the prior pipeline
sync configuration and canonical image lock. The v3 image remains cached in
Docker, the local registry, and containerd; removing cached images is a separate
capacity-management action and is not part of rollback.

Rollback also uses `Recreate`, so it causes another bounded CV interruption.

## 8. Resume and recovery rules

- Re-run `prepare` with the same explicit operation ID to inspect an already
  created operation. It will not create a second deployment revision.
- Re-run `activate` after a lost SSH session. Its idempotency key is derived
  from the operation ID, so an already-committed upgrade is not duplicated.
- Re-run `rollback` when state is `rolling_back`; it waits for the existing
  rollback instead of posting another rollback revision.
- A `prepared` operation may be abandoned without affecting the running
  workload. It leaves staged artifacts for audit and later cleanup.
- `operator_required` deliberately requires an explicit new commit or rollback.
  Ordinary camera and lifecycle commits return synchronization to `pending`.
- Never edit the operation JSON to force a transition. Preserve it with the
  installation evidence and diagnose the underlying failed gate.

## 9. Audit and evidence

Retain these non-secret records with the change ticket:

- TVT release version and source commit;
- release `manifest.json` and `checksums.sha256`;
- operation ID and final status output;
- source and target catalog IDs, bundle SHA-256 values, image digests, and
  desired/applied revisions;
- installation/verification report;
- qualification report when required;
- start/end time of the observed CV interruption.

Do not attach environment files, camera Secret bodies, raw PostgreSQL dumps,
credential files, or command output containing sensitive values.
