# Traffic Pilot v2 compatibility review — 2026-09-11

Status: **not approved for the deployable solution catalog**. The original vendor
contract and examples are preserved alongside this report. They have not been
rewritten to imply compatibility.

Source: https://github.com/BhashyamDayasagar587/sporada-secure-pilot/tree/658decd2ea5bf30cf93731d50a7e7d7a439b9ccc/delivery/apexfabric-v1/intel-285h/traffic-pilot

Downloaded archive: `images/traffic-pilot-2026.09.11-v2/image.tar` (1,927,290,880 bytes).
Verified archive SHA256: `1163fdb9c990ae3cecb701181b8151f6fb554602e7fce5f1f8c78a8ca3ad9121`.
Docker tag: `localhost/traffic-pilot-runtime:intel-285h-2026.09.11-v2`.
Image config ID: `sha256:38e3ed1d38d10d0d34f4fd93ed929e6a5c203ec71aff3dfd949755ec8e9ebc77`.
The archive checksum and image config ID are not registry manifest digests.

## Verified

- Archive checksum matches the pinned published checksum; Docker load succeeds.
- Linux/amd64; UID/GID 10001; port 8080; runtime entrypoint, no bundled UI required.
- Delivered desired-state example validates against its delivered JSON Schema.
- Isolated API handler test inside the actual image: health 200, metrics JSON 200,
  readiness 503 without a worker (expected), events 200 followed by EOF, snapshot
  route 404. This test did not start inference or validate accelerator access.

## Blocking incompatibilities

1. Catalog validation rejects the contract: `ui.camera.apps must annotate every
   desired-state app ID exactly once`. It also needs a valid default app and
   supported geometry annotations. `ui.included: false` is not UI field metadata.
2. Platform `generate_bundle` rejects `traffic-pilot-runtime-limited` as an
   unsupported solution type. Existing generators/configuration updates implement
   Traffic and Surveillance shapes, not this image's `traffic-pilot` shape.
   Renaming its catalog identity to an existing pack would conceal this mismatch.
3. Camera `config` is unrestricted in the delivery schema. Its example uses
   `config.line`/`config.zone`, whereas the UI expects annotated app-specific
   geometry fields. Typed, validated settings and count semantics are needed.
4. Image handler has no `/snapshots/` route. Vendor snapshot references are
   `/state/snapshots/...` paths; the collector fetches `/snapshots/...` URLs.
   Snapshot retention and dashboard previews therefore cannot work unchanged.
5. `/events` emits current runtime state, management history and at most 200 recent
   analytics records, then closes. It is not a continuous replayable SSE feed.
   The reconnecting collector may ingest some records but cannot guarantee no
   losses between requests. Stable event IDs, timestamp/type semantics and event
   schemas must be established; none are delivered in this folder.
6. Integration must provision persistent `/state` and writable `/plans` and
   `/tmp/apexfabric`, directory-mounted configuration for hot reload, GPU/NPU
   access, and resources consistent with the declared 8 CPU / 16 GiB request.

## Required acceptance before activation

Supply a corrected delivery (or implement and version an explicitly tested
adapter) addressing the above. Test camera configuration, all four use cases,
geometry modes, event ingestion, fetchable retained snapshots, revision updates,
worker failure recovery, and restart behavior on the Intel box. Recognition
accuracy needs representative footage; endpoint tests cannot establish it.

SSH to admin1@192.168.1.64 was denied. No image was pushed to the edge box and no
edge catalog was modified. `scripts/import-traffic-pilot-review.sh` can publish
this exact archive for inspection without registering it as deployable. Existing
packs remain unchanged. The single-box delivery selection excludes this review.
