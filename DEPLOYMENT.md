# TVT edge release deployment

## Purpose

This runbook upgrades an installed TVT edge from one immutable TVT release
bundle to another. The common workflow handles changes to:

- `tvt_edge` and `apexfabric` Python code and dependencies;
- PostgreSQL migrations;
- host systemd services and timers;
- the TVT/ApexFabric dashboard image;
- node-reporter and node-status-controller images and manifests;
- Solution Pack schemas and catalogs; and
- the vendor CV runtime image.

Use only the consolidated host operation:

```bash
sudo <release-bundle>/scripts/tvt-edge-operations.sh \
  upgrade-release ACTION OPTIONS
```

The older `upgrade-application` and `upgrade-solution-image` operations remain
component operations used by the common orchestrator and for targeted recovery.
Do not edit Kubernetes Deployments, database rows, catalog rows, image tags,
systemd units, or camera Secrets by hand.

## Two upgrade classes

`upgrade-release plan` compares the installed and target release manifests and
checksums. It reports one of two activation modes:

1. `in_place` covers application, service-unit, dashboard, node-management,
   manifest, catalog, and CV-image changes.
2. `platform_maintenance` covers registry, K3s, kernel, driver, or offline host
   package changes, plus database migrations that are not declared compatible
   with the previous application. Supply `--platform-maintenance` to make the
   disruptive intent explicit. Package/driver changes use the same operation
   ID across preparation, mandatory reboot, post-reboot hardware verification,
   and application activation. Registry or K3s changes use the same track but
   require a reboot only when host packages or drivers also changed.

A forward-only database migration is classified as platform maintenance but is
rejected until that release includes a separately reviewed recovery plan.

This distinction is deliberate. One front door identifies the impact, but a
kernel or driver transition cannot safely have the same rollback promise as a
Python service or digest-pinned Kubernetes workload.

## Availability

In-place preparation is non-disruptive: it verifies the complete bundle,
constructs the new release and virtual environment, publishes management
images, and pre-pulls/previews a changed CV solution. Platform preparation may
install host packages, restart Docker or PostgreSQL, reinstall drivers, and
then require a reboot. Treat the complete platform flow as a maintenance
window even if the existing CV Pod continues running before the reboot.

Activation has change-dependent interruption:

| Changed component | Expected impact |
|---|---|
| TVT/ApexFabric host application | Brief management API/control-service restart; the existing CV Pod continues |
| Dashboard | Brief dashboard `Recreate` rollout |
| Node-management workloads | Rolling reporter/controller restart; the CV workload normally continues |
| CV image or solution contract | Real inference interruption: the single CV replica uses `Recreate` |
| K3s, kernel, drivers, or host packages | Platform maintenance and possibly a reboot; all inference may stop |

Pre-pulling removes image-transfer time from the CV outage but does not make a
single-replica `Recreate` rollout zero-downtime. True uninterrupted inference
requires a separately qualified blue/green or multi-replica design that can
safely share camera streams, accelerators, persistent state, and event
ownership.

## Release policy

Every deployable change receives a new TVT release version. Never publish new
content under an existing version.

The release manifest declares:

- database migration strategy and whether the previous application remains
  compatible with the migrated schema; and
- bounded operator actions associated with the release.

Operator actions are displayed in the plan but are never run automatically.
This matters for data-maintenance operations such as
`purge-unnamed-persons`: it is idempotent and takes its own backup, but it
deletes legacy identity data and therefore still requires an explicit operator
decision.

## 1. Build the immutable release

Create a clean release commit and choose a new version. Probe the target edge,
then build one bundle for that exact edge profile:

```bash
./scripts/tvt-edge-operations.sh probe-edge-hardware \
  --ssh <edge-ssh-target> \
  --output /srv/tvt-release/edge-hardware-inventory.json

./scripts/make-tvt-edge-release.sh \
  --input-directory /srv/tvt-release/inputs-<release> \
  --output-directory /srv/tvt-release/output/tvt-edge-release-<release>-intel-285h \
  --edge-inventory /srv/tvt-release/edge-hardware-inventory.json \
  --version <release> \
  --source-commit <full-40-character-tvt-git-sha>
```

Before accepting a probe for a rebooting platform release, confirm that the
running kernel is also the configured next-boot kernel:

```bash
ssh <edge-ssh-target> 'uname -r; readlink -f /boot/vmlinuz'
```

If the versions differ, reboot the edge first, wait for workload recovery,
then probe and build again. The platform prepare preflight enforces the same
rule so a bundle cannot install drivers for one kernel and reboot into another.

The builder runs the source tests, builds the wheel and container artifacts,
locks external inputs, validates the vendor delivery, and covers every bundle
file with `checksums.sha256`. Transfer the completed bundle to stable local
storage on the edge. Do not activate from removable or temporary storage.

Never put SSH passwords, sudo passwords, registry credentials, camera
credentials, credential-bearing URLs, or API keys in the bundle or command
line.

## 2. Produce and review the plan

The plan operation is read-only:

```bash
<release-bundle>/scripts/tvt-edge-operations.sh \
  upgrade-release plan --bundle <release-bundle>
```

Review:

- current and target release versions;
- every changed component;
- `activation_mode`;
- availability impacts;
- database rollback compatibility; and
- required or optional operator actions.

Do not continue with the in-place workflow when `activation_mode` is
`platform_maintenance`. Use the explicit `--platform-maintenance` preparation
path below. Do not bypass the classification by invoking component installers
or altering installation state files.

If `cv_solution` is `true`, identify the stable applied deployment:

```bash
curl --fail --silent http://127.0.0.1:8089/api/v1/deployments
```

Record its deployment ID, applied revision, bundle SHA-256, catalog ID, and
image digest in the change record. The response contains no camera credentials
or RTSP URLs.

## 3. Prepare without interruption

For a release that changes the CV solution:

```bash
sudo <release-bundle>/scripts/tvt-edge-operations.sh \
  upgrade-release prepare \
  --bundle <release-bundle> \
  --deployment-id <deployment-id>
```

When the CV solution is unchanged, omit `--deployment-id`:

```bash
sudo <release-bundle>/scripts/tvt-edge-operations.sh \
  upgrade-release prepare --bundle <release-bundle>
```

For a plan reporting `platform_maintenance`, explicitly select that track:

```bash
sudo <release-bundle>/scripts/tvt-edge-operations.sh \
  upgrade-release prepare \
  --bundle <release-bundle> \
  --platform-maintenance
```

When host packages or drivers changed, preparation returns status
`platform_reboot_required`. Save its `operation_id`, then reboot:

```bash
sudo reboot
```

After SSH returns, run `activate` with the same operation ID. Activation first
verifies that the boot ID changed and that the locked GPU/NPU, Docker,
PostgreSQL, kernel, and hardware profile are healthy. It then applies any
registry/K3s changes and continues through the normal application activation.

During package preparation, enabled registry and K3s services are restored in
dependency order before the driver stage. When a platform release intentionally
changes the locked hardware recipe, the old recipe is retained as
`/var/lib/tvt/hardware-driver-recipe.json.before-<sha256>` and the target
bundle's already verified recipe/cache are published atomically.

If post-reboot verification finds a different kernel despite the preflight,
do not alter or republish the existing release version. Probe the now-running
kernel, create a newer immutable release version, and start a new
`upgrade-release prepare --platform-maintenance` operation. A newer verified
release may supersede an older `reboot_required` host-preparation state only
after the older operation's requested reboot is proven by a changed boot ID;
the superseded state is preserved under `/var/lib/tvt/install/`. Do not edit
either state file manually.

Preparation returns an `operation_id`. Save it. To resume an interrupted
preparation, pass the same ID:

```bash
sudo <release-bundle>/scripts/tvt-edge-operations.sh \
  upgrade-release prepare \
  --bundle <release-bundle> \
  --deployment-id <deployment-id> \
  --operation-id <operation-id>
```

Preparation performs these gates:

1. verifies the release manifest and full checksum coverage;
2. compares installed and target artifacts, requires explicit platform intent,
   and preserves the previous preparation state before installing a different
   release's packages or drivers;
3. creates `/opt/tvt/releases/<version>` and installs the target wheel into its
   own virtual environment;
4. publishes digest-pinned dashboard and node-management images to the
   loopback registry;
5. for a CV change, verifies the vendor archive/catalog, publishes and
   pre-pulls the exact image digest, snapshots the applied assignment, and
   performs the compare-and-swap preview; and
6. records resumable, root-only state under
   `/var/lib/tvt/install/release-upgrades/<operation-id>/`.

Inspect it at any time:

```bash
sudo <release-bundle>/scripts/tvt-edge-operations.sh \
  upgrade-release status --operation-id <operation-id>
```

For an in-place release, continue only when the outer status is `prepared`.
For package/driver maintenance, `platform_reboot_required` means reboot before
activation; it does not authorize activation on the same boot. For a CV change,
the nested solution status must also be `prepared`.

## 4. Activate during the maintenance window

```bash
sudo <release-bundle>/scripts/tvt-edge-operations.sh \
  upgrade-release activate \
  --operation-id <operation-id> \
  --timeout 900
```

Activation executes in this order:

1. save the previous release links, installation evidence, systemd units,
   dashboard/node image locks, PostgreSQL dump, and an online SQLite backup of
   ApexFabric telemetry/identity state;
2. stop the affected host writers, apply the PostgreSQL migration, switch the
   release symlinks, install current service units, and restart all affected
   long-running services;
3. apply and wait for digest-pinned node-management and dashboard rollouts;
4. activate the CV solution last, using the prepared assignment snapshot and
   compare-and-swap token; and
5. commit installation evidence only after health gates succeed.

Do not edit cameras, assignments, enrollment state, or deployment lifecycle
while activation is running. For a CV update, observe the expected
single-replica interruption separately:

```bash
sudo k3s kubectl get pods -n apexfabric -w
```

Losing the SSH session does not create a second release. Re-run `status`, then
re-run `activate` with the same operation ID when its recorded state permits
resume.

## 5. Verify

```bash
sudo <release-bundle>/scripts/tvt-edge-operations.sh \
  upgrade-release status --operation-id <operation-id>

sudo /opt/tvt/current/resources/scripts/tvt-edge-operations.sh \
  verify-k3s-plane

sudo systemctl is-active \
  apexfabric-control.service tvt-edge.service tvt-camera-sync.service

curl --fail --silent http://127.0.0.1:8088/api/customer
curl --fail --silent http://127.0.0.1:8089/api/v1/health
curl --fail --silent http://127.0.0.1:18081/dashboard/ >/dev/null
```

Verify the site-specific cameras and expected analytics in the dashboard. A
Kubernetes-ready Pod proves rollout completion, not inference correctness. Run
the Traffic qualification workflow when release acceptance requires inference
evidence.

## 6. Release-specific operator actions

The current release advertises the optional post-activation cleanup for legacy
unnamed identities. Review first:

```bash
sudo /opt/tvt/current/resources/scripts/tvt-edge-operations.sh \
  purge-unnamed-persons --dry-run
```

Run the real cleanup only when its result is approved:

```bash
sudo /opt/tvt/current/resources/scripts/tvt-edge-operations.sh \
  purge-unnamed-persons
```

It preserves named people and creates a root-only SQLite backup under
`/var/lib/tvt/install/` before deleting unnamed people, their vectors, and
attendance sessions.

## 7. Failure and rollback

If application activation fails before completion, the component upgrader
restores the previous release links, systemd units, dashboard/node image locks,
and workloads. It does not silently restore PostgreSQL from `pg_dump`, because
doing so could discard writes accepted after the backup.

The CV upgrader independently restores the previous complete bundle and image
after a failed rollout. Synchronization then enters `operator_required` to
prevent retrying a known-bad image and causing repeated outages.

Inspect first:

```bash
sudo <release-bundle>/scripts/tvt-edge-operations.sh \
  upgrade-release status --operation-id <operation-id>
```

When rollback is appropriate:

```bash
sudo <release-bundle>/scripts/tvt-edge-operations.sh \
  upgrade-release rollback \
  --operation-id <operation-id> \
  --timeout 900
```

Rollback runs in reverse order: CV solution, Kubernetes image locks/manifests,
systemd units, application links, and services. It is allowed automatically
only when the target release manifest declares the migrated database schema
compatible with the previous application. Otherwise the operation remains
`operator_required`; use a reviewed offline database recovery procedure rather
than risking silent data loss.

Application rollback does not downgrade already-installed host packages,
drivers, Registry, or K3s. A platform rollback therefore restores the previous
application and workloads only; reverting platform artifacts requires a
separate bundle-specific maintenance plan and may require another reboot.

Rollback of an active CV image uses `Recreate` and causes another bounded
inference interruption. Newly published images remain cached; cache cleanup is
a separate capacity-management action.

## 8. Evidence

Retain these non-secret records with the change ticket:

- source commit and current/target release versions;
- target `manifest.json` and `checksums.sha256`;
- plan output, operation ID, and final status;
- database migration head and backup directory;
- previous and target dashboard/node/CV image digests;
- source and target CV bundle SHA-256 values and revisions;
- installation verification and qualification reports; and
- observed management and inference interruption times.

Do not attach environment files, credential files, Secret bodies, raw database
dumps, camera URLs, faces, plates, embeddings, or command output containing
sensitive values.
