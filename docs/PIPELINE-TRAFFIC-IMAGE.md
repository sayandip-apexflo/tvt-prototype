# TVT Mills solution delivery provenance

The historical filename is retained for links, but the production solution is
the combined TVT Mills pilot runtime, not the retired Traffic-only image.

The immutable source is the `TVT_images` repository release
`tvt-edge-intel-285h-2026.09.18-v1` at commit
`ab85058ab961bb7aeb4ed9c96f1a35ec5c37f934`. The production artifact is:

```text
tvt-edge-runtime-intel-285h-2026.09.18-v1.oci.tar
size:   967496192 bytes
sha256: a236a4c1c3053e6a78270d98426b72c5fcbe681273b6f5f35d5631d25a88f4a8
source: localhost/tvt-edge-runtime:intel-285h-2026.09.18-v1
target: 127.0.0.1:5000/apexfabric/tvt-mills-pilot:intel-285h-2026.09.18-v1
```

The release builder downloads this release asset directly from the pinned URL
in `config/pipeline.env`. It verifies the size, SHA-256, and embedded OCI tag
before admitting it to a release input lock. Source-build mode is deliberately
rejected: the supplied OCI archive is the only production input.

## Image and runtime contract

The image contains the combined face-recognition, face-enrollment, and ANPR
runtime and baked traffic and face models. It declares Linux `amd64`, Intel
285H, UID/GID `10001`, port `8080`, command `python -m edge`, and the
`tvt-mills-pilot-v1` contract. It exposes `/healthz`, `/readyz`, `/metrics`,
`/events`, and snapshot routes.

The importer validates the image metadata and all configured model hashes by
creating, but never starting, a temporary container. It then retags the source
image to the edge-local `apexfabric/tvt-mills-pilot` repository, pushes it to
the loopback registry, and records the registry-produced digest.

## Vendored metadata and catalog

The catalog contract is under
`solution-packs/catalog/tvt-mills-pilot-2026.09.18-v1/`. Its
`provenance.json` binds the catalog ID, immutable source commit, release tag,
archive identity, source image tag, edge-local repository/tag, and checksums
for the contract and schema files.

`scripts/validate-solution-delivery.py` is a fail-closed cross-check between
that provenance, `config/pipeline.env`, and the actual archive. The release
builder runs it before constructing a bundle. The host installer runs it in
the `solution_delivery_contract` stage before starting the registry, K3s, or
PostgreSQL provisioning. A catalog/archive/config mismatch therefore cannot
reach the database seed stage.

The PostgreSQL bootstrap idempotently seeds catalog ID
`tvt-mills-pilot:2026.09.18-v1`. Refresh resolves the edge-local tag through
the OCI Distribution API and marks it available only when the returned
manifest digest agrees with the registry response. Seeding and refreshing do
not create a deployment.

## Synchronization and lock

`tvt-pipeline-image-sync.service` receives the bundle archive and catalog paths
through `/etc/tvt/pipeline-bundle.env`. It uses a nonblocking lock, bounded
timeouts, checksum validation, stopped-container inspection, and atomic
mode-`0600` evidence replacement. A failed import preserves the previous
known-good lock.

The lock records the catalog ID, immutable source commit, archive identity,
edge-local repository and tag, registry digest and immutable reference,
metadata checksums, and verification timestamp. Existing registry images are
not deleted automatically.

Desired-state changes use a controlled rollout. Deployment remains an
explicit preview-and-commit action after camera onboarding; installation never
creates a solution workload automatically.
