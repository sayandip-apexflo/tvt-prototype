# PIPELINE Traffic v4 delivery provenance

TVT uses the ApexFabric V1 Intel Traffic delivery from the `PIPELINE`
repository. The delivery branch is `apexfabric-v1-intel-images`, but branch
HEAD is intentionally not trusted or followed: synchronization fetches exact
commit `6513562c9d27eba511322280e19e054c3948ae4d`.

The production artifact is:

```text
delivery/apexfabric-v1/intel-285h/traffic/image-2026.08.21-v4.tar
size:   1930041856 bytes
sha256: a6787bba6a27bc486f90b4c4dd41681d051c7c834568d99bc4a884d177d10e0f
image:  traffic-edge-runtime:intel-285h-2026.08.21-v4
target: 127.0.0.1:5000/apexfabric/traffic-edge-runtime:intel-285h-2026.08.21-v4
```

Archive import is the production path. A developer-only `--mode build` remains
for qualification experiments, under a distinct `-source-build` tag. It is not
used by the synchronization service because upstream APT and Python dependency
resolution is not fully immutable.

## Image and runtime contract

The image is the executable solution. PIPELINE has already placed the Traffic
runtime, edge-agent/compiler, application code, dependencies, and OpenVINO
models in it. TVT does not assemble another image, treat
`solution-pack.spec.yaml` as a Kubernetes manifest, or use the standalone
edge-agent, model-delivery, or surveillance archives for this flow.

The v4 contract declares Linux `amd64`, Intel 285H, ApexFabric V1, UID/GID
`10001`, port `8080`, compiler command
`python -m edge_runtime.agent.edge_agent`, runtime-plan output
`/plans/traffic.runtime_plan.json`, persistent state under `/state`, temporary
paths `/plans` and `/tmp/apexfabric`, and Intel devices `/dev/dri` and
`/dev/accel`. It exposes `/healthz`, `/readyz`, `/metrics`, and `/events`.

The models are baked into `/models/traffic/openvino`:

| File | SHA-256 |
|---|---|
| `vehicle.xml` | `ba402c4eabe93c1c4ad9b7ea70b95fbb0a0d374b7ec777dc26992f428778f5b9` |
| `vehicle.bin` | `e8513382b2cca7cc4fb9257a19b0f28156537a1b2e15074fe47fabc64ededf26` |
| `license_plate.xml` | `d9ff01e97241d2d0132b960d6bd6c12659a09474b3a005991aa633adba0c94aa` |
| `license_plate.bin` | `df60a0a27cd91e5cbfe0305f5bce1f8a588acd0b031c3fa2bd4a0c379cc23dda` |
| `ocr.xml` | `98cdc203e544f3908e8b47da85cb98e771d18de16e877781f7db76dd8feb63ed` |
| `ocr.bin` | `96147eba58867c42ba6040f9446a8936d4eecfed14e3e7c2ab43db6bc3f49dff` |

The importer validates the archive size and checksum before `docker load`. It
then verifies architecture, release/version and ApexFabric labels, baked-model
declaration, UID/GID, port, container command, compiler and entrypoint modules,
and all six model hashes. It creates a stopped container only to copy files for
inspection; it never starts the CV runtime.

## Vendored metadata and catalog

Metadata copied byte-for-byte from the same commit is under
`solution-packs/catalog/traffic-edge-runtime-2026.08.21-v4/`. Its
`provenance.json` binds the repository, commit, delivery path, archive, local
image identity, and SHA-256 of the image contract, desired-state files,
metrics schema, analytics-event schema, and safe event example. Catalog
loading rejects any checksum, schema, example, or contract disagreement.

The production catalog is the existing TVT PostgreSQL database, not a second
SQLite database. `scripts/bootstrap-postgresql.sh` applies Alembic and
idempotently seeds catalog ID `traffic-edge-runtime:2026.08.21-v4`, retargeted
to `127.0.0.1:5000`. `POST /api/v1/solutions/refresh` performs an OCI
Distribution manifest `GET`, hashes the returned bytes, compares that value to
`Docker-Content-Digest`, and marks the entry available only on an exact match.
`GET /api/v1/solutions` returns safe catalog metadata and no credentials.

Seeding and refreshing do not create a DeploymentBundle, register a
deployment, change a desired revision, call K3s, or alter a deployed digest.

## Synchronization and lock

`tvt-pipeline-image-sync.service` is a root oneshot ordered after Docker and the
loopback registry. Its timer rechecks the immutable pin daily. Both automated
and manual runs use a nonblocking `flock`, bounded retries/timeouts, and the
production state directory `/var/lib/tvt/pipeline`. A repository invocation
defaults to the gitignored `build/pipeline` directory for safe development.

The successful mode-`0600` JSON lock records format version, catalog ID,
repository and commit, delivery path, archive identity, local repository/tag,
registry-produced digest, immutable `repository@sha256:` reference, metadata
checksums (including the runtime metrics and event contracts), and verification
timestamp. It is written to a private temporary
file and atomically renamed only after every verification and push succeeds.
A failed run therefore retains the previous known-good lock. Existing registry
images are not deleted.

For private Git/LFS access, put `PIPELINE_GIT_USERNAME` and a read-only
`PIPELINE_GITHUB_TOKEN` in `/etc/tvt/pipeline-image-sync.env`. The installer
creates that optional environment file as root-owned mode `0600`; neither the
service nor importer prints credential values.

This is v4 and does not promise hot reload. Desired-state changes use a
controlled restart. Phase 4 generates the complete DeploymentBundle only from
an available catalog entry and its resolved digest, requires a safe preview,
preflights the immutable reference with `k3s crictl pull`, and restores the
previous applied bundle and digest if rollout fails.

Phase 5 consumes the same lock and vendored schemas during live qualification.
It therefore verifies the runtime's observed metrics and analytics events
against files from the exact commit that supplied the imported image rather
than against an independently updated contract.
