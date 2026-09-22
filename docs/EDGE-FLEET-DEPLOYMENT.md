# Fleet deployment from a workstation

`scripts/tvt-edge-fleet.sh` automates deployment to tens of edge boxes over
SSH. It probes every edge first, groups compatible inventories, builds one
offline release bundle per group, transfers each bundle, runs the existing
prepare/install workflow, reboots when required, and writes a final failure
report. One failed edge does not stop other edges.

The fleet manifest is workstation-side YAML; see
`config/edge-fleet.example.yaml`. Use SSH configuration or an agent for
authentication. The `site_config` files are copied to the target staging area
outside the release bundle and must not contain workstation-only paths or
secrets.

## Install

Run from the repository checkout:

```bash
scripts/tvt-edge-fleet.sh install \
  --fleet config/edge-fleet.yaml \
  --version 0.1.0 \
  --output-directory /srv/tvt/releases/0.1.0 \
  --concurrency 5
```

The output directory must be outside the Git checkout because the release
builder creates and verifies large offline inputs there. Each compatibility
group gets a directory below `groups/`; fleet state and per-edge logs are
written under `fleet-state/`. The command exits zero only when all edges reach
`verified`; otherwise it exits one and records the failed edge IDs in
`fleet-report.json`.

For a development run where the checkout is intentionally dirty, add
`--allow-dirty-source` explicitly. Production builds should use a clean
checked-out commit.

## Resume and inspect

```bash
scripts/tvt-edge-fleet.sh resume \
  --fleet config/edge-fleet.yaml \
  --state-directory /srv/tvt/releases/0.1.0/fleet-state \
  --concurrency 5

scripts/tvt-edge-fleet.sh status \
  --state-directory /srv/tvt/releases/0.1.0/fleet-state
```

The controller keeps inventory, bundle-group, transfer, preparation, reboot,
installation, and verification status separately for each edge. Resuming
reuses successful probes and already-built group bundles. After final host
verification it also retrieves the non-secret
`/var/lib/tvt/install/installation-report.json` into the edge's fleet-state
directory and records that local path in `fleet-report.json`.

## Compatibility grouping

Boxes share a bundle when Ubuntu release, architecture, CPU model, running
kernel, Secure Boot state, Axelera presence, and the sorted PCI
vendor/device/class signature are identical. PCI bus addresses, RAM, free disk,
and collection timestamps do not split a group. The edge installer still
re-probes the live box and rejects a bundle whose actual profile does not match
its bundled profile.
