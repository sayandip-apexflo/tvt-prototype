# TVT identity & attendance CV pipeline delivery contract

Status: draft integration contract for face recognition, face enrollment,
ANPR entry/exit direction, attendance reporting, and daily vehicle
entry/exit reporting. Not yet promoted into `solution-packs/catalog/` — see
`README.md` in this directory for the staging/promotion lifecycle, why the
traffic pack's live files are not edited directly, and the 2026-09-17
revision note explaining why this contract now transmits embeddings (it
previously did not).

This document is the handoff and acceptance checklist for the CV pipeline
developer extending the `surveillance-edge-runtime` image with these use
cases. The machine-readable files are authoritative:

- `solution-packs/catalog/surveillance-edge-runtime-2026.08.24-v3/image-contract.yaml`
- `solution-packs/catalog/surveillance-edge-runtime-2026.08.24-v3/desired-state.schema.json`
- `solution-packs/catalog/surveillance-edge-runtime-2026.08.24-v3/analytics-event.schema.json`
  and `analytics-event.examples.json`
- `docs/contracts/tvt-identity-v1/traffic-analytics-event.draft-schema.json`
  and `traffic-analytics-event.example.json` — a documented delta over the
  **pinned** `solution-packs/catalog/traffic-edge-runtime-2026.08.21-v4/analytics-event.schema.json`.
  Do not edit that pinned file; `tests/test_traffic_qualification.py` checks
  its SHA256 against a vendored delivery.

The control plane stores valid runtime evidence exactly as advertised. It
does not rewrite malformed event paths, synthesize missing snapshots, infer
missing bounding boxes, or translate zone geometry into a direction. It does
assign `person_id` — see "Identity resolution and auto-enrollment" below;
that is the one exception to "the edge decides, the control plane records"
in this contract, and it is entirely on the control-plane side of the wire.

## Required HTTP service behavior

The image already declares the standard management port (`8080`) with
`/healthz`, `/readyz`, `/metrics`, `/events`, and
`/snapshots/<camera-id>/<filename>` in its `image-contract.yaml`. This work
adds **no new HTTP routes**. In particular:

- `/api/enrollment`, `/api/face_gallery`, `/api/face_group` must **not**
  exist on the pod, and there is no gallery for the edge to fetch or cache
  any more — identity resolution is entirely central-plane work over
  embeddings received on `/events`. Biometric writes never cross the
  unauthenticated pod-proxy surface.

## Events and evidence

| Application | Event type | Required event-specific data |
|---|---|---|
| `face_recognition` | `face_detection_event` | `payload.embeddings.face`, `payload.embeddings.body` (both required), `payload.subject` (face bbox) |
| `face_enrollment` | `enrollment_capture_event` | `payload.embeddings.face` (required, no `body`), `payload.subject`, `payload.quality` (`sharpness`, `pose_score`, `liveness_score`) |
| `anpr` | `plate_read_event` | unchanged (`payload.plate.text`, `payload.vehicle`), plus **optional** `payload.location` when the camera has entry/exit zone pairs configured |

There is no `face_match_event`/`face_unmatched_event` split any more. The
edge holds no gallery, so it cannot itself know whether a face matched
anything — every detection is the same event type, `face_detection_event`,
and the control plane decides match-vs-new-enrollment after the fact (see
below). Configure every one of the five `face_recognition` cameras (2 main
entrance, 2 plant entrance, 1 back exit) and the dedicated
`face_enrollment` camera identically with respect to this contract; the
only difference between them is which `application`/`event_type` pair they
emit and whether `embeddings.body` is present.

Every `face_recognition`/`face_enrollment` event must carry the same
evidence quadruple already required of `plate_read_event`:
`payload.snapshot_ref` / `snapshot_url` / `snapshot_content_type` /
`snapshot_assets.event_frame`, plus a `snapshot_assets.face_crop` entry
(mirroring the `vehicle_crop`/`plate_crop` convention `wrong_way_event`
already uses), and a `snapshot_assets.body_crop` entry whenever
`embeddings.body` is present. The event frame must be the full frame whose
pixel coordinate system the bounding box uses.

Use a globally unique, immutable `event_id`; retries preserve the ID and
exact payload. `timestamp` is UTC RFC3339.

## Embeddings

- Every embedding must be **L2-normalized (unit norm)** before emission, so
  the control plane's cosine similarity is a plain dot product. This is a
  hard requirement, not a suggestion — an un-normalized vector silently
  corrupts every similarity score computed against it, including scores for
  other people's already-correct vectors sharing the same vector-DB index.
- Embedding dimension is fixed by the deployed model version and must be
  identical across every camera feeding one deployment's vector DB. If a
  model upgrade changes the dimension, that is a breaking change requiring
  a coordinated re-enrollment, not something to paper over by padding or
  truncating vectors to match the old dimension.
- **Never** write an embedding — or any other biometric template — to a
  metric label, a log line, or an alert email. `LLD_PLAN.md` (section 14,
  section 17) and `HLD.md` already require this for the platform generally;
  it is not new here. The `/events` SSE stream carrying `payload.embeddings`
  to the control plane's vector DB is the one "separate versioned and
  access-controlled data path" `LLD_PLAN.md:708-709` sets aside for exactly
  this kind of sensitive business data. Do not duplicate an embedding into
  any other sink for debugging convenience, including local disk outside
  the declared `/state` volume's normal event-evidence flow.
- Do not persist embeddings on the edge beyond what is needed to emit the
  single event carrying them. The edge is not a store of biometric data at
  rest; the control plane's vector DB is.

## Zones, gates, and direction — no `direction` field

Entry/exit direction is represented by **two zones per gate**, not a
direction field on the event or the zone:

- ANPR: `config.zones.anpr[]` (traffic pack) — pair `<gate>-anpr_entry` /
  `<gate>-anpr_exit`.
- Face recognition: `config.zones.face_recognition[]` (surveillance pack,
  `identity_zone` def) — pair `<gate>-face_entry` / `<gate>-face_exit`. For
  this deployment: `main-entrance-1-face`/`main-entrance-2-face` and
  `plant-entrance-1-face`/`plant-entrance-2-face` each need an `_entry`/
  `_exit` pair; `back-exit-face` only ever needs `_exit` (the use case is
  exit-only at that door).

The control plane derives direction from which zone `location.id` an event
names. Neither pack's event schema needs a direction field; the traffic
pack only needed `payload.location` documented (it already validates
against the pinned v4 schema unmodified, since `payload` there has
`additionalProperties: true`).

**Two known gaps the developer must close, not just work around:**

1. The traffic pack's UI geometry (`image-contract.yaml`,
   `ui.camera.apps.anpr.geometry.idTemplate: "{camera_id}-{app}-zone"`)
   assumes exactly one auto-generated zone id per camera+app. It has no way
   to emit a paired `_entry`/`_exit` id today. Either the compiler/UI needs
   a role-aware id template (e.g. `{camera_id}-{app}-zone-{role}`), or gate
   zones must be hand-authored in desired state until that lands.
2. The traffic pack's `polygon_zone` JSON schema def only requires
   `name`+`poly` — it has no `id` property at all, despite `idTemplate`
   implying one exists. `payload.location.id` in `plate_read_event` needs
   something stable to reference. The surveillance pack's `identity_zone`
   def fixes this (`id` is required) for `zones.face_recognition[]`; the
   traffic pack's `polygon_zone` should get the same treatment when it
   promotes to a new version — it cannot be fixed in the pinned v4 file.

## Identity resolution and auto-enrollment

This is control-plane work, described here because it is the reason the
edge contract looks the way it does above. The edge's entire obligation is
to emit a normalized embedding with good evidence; everything below happens
after the event lands in the control plane.

- **Vector DB**: embedded (sqlite-vec or FAISS), colocated with the control
  plane's existing per-concern sqlite files. A `persons` metadata table
  (person_id, display_name — nullable until an operator names an
  auto-enrolled person, first_seen, last_seen) plus a vector store keyed by
  `(person_id, embedding_kind ∈ {face, body})`, append-only: every accepted
  sighting adds a new reference vector for that person rather than
  overwriting a single "canonical" one, so matching improves over time
  without needing centroid-recomputation logic. See `Aggregation.md` for
  the concrete schema and transaction design.
- **Matching**: on every incoming `payload.embeddings.face` (and
  `.body` when present), run a nearest-neighbor cosine-similarity search
  against the vector DB. A best match at or above a configured threshold
  reuses that `person_id`; below it, auto-enroll a new `person_id` — no
  human review step, for any of the five `face_recognition` cameras or the
  enrollment camera alike.
- **Race-condition guard**: two near-simultaneous unmatched sightings of
  the same not-yet-enrolled person at two different gates must not each
  independently decide "new person" and create two `person_id`s for one
  human. The nearest-neighbor read and the resulting insert of a new
  `person_id` + vector must happen inside one serialized transaction (the
  control plane's sqlite writer is already single-writer, the same pattern
  `TelemetryStore.ingest()` uses for its `if inserted:` idempotency guard —
  see `Aggregation.md`), so a second concurrent enrollment attempt blocks,
  then re-runs its own search after the first commits and finds the
  just-inserted vector instead of creating a duplicate.
- **Consequence the developer must not "fix" unilaterally**: because any
  recognition camera can auto-enroll, a stranger, visitor, or delivery
  driver walking past any entrance becomes a permanently tracked
  `person_id` with no name, indistinguishable in raw data from an employee
  until someone names them in the control-plane UI. This was an explicit
  product decision (see `README.md`), not an oversight — do not add a
  human-review gate or an allowlist-camera restriction to work around it
  without checking first.

## Persistence and shutdown

Same baseline as any TVT runtime pod: working snapshots live under the
declared `/state` volume, written with a temporary name and atomically
renamed into place; SIGTERM stops accepting new frames, completes or
discards in-progress writes, flushes event state, closes the SSE
connection, and exits within the pod termination grace period; a restart
must not expose corrupt evidence. The edge holds no gallery or other
identity state across restarts — there is nothing to reload; a fresh pod
just resumes emitting embeddings.

## Security and operational requirements

- Run as the non-root UID/GID already declared by the image contract
  (10001:10001 today).
- Read RTSP sources only through `file:/run/secrets/apexfabric/<camera-id>.rtsp`.
- Never emit credentials, host filesystem paths, base64 images, or
  authorization headers in events or logs.
- Never emit an embedding, or any other biometric template, anywhere except
  `payload.embeddings` on the two event types above — not in a metric
  label, not in a log line, not in an alert, not to local disk outside the
  event-evidence flow (see "Embeddings" above).
- No enrollment or gallery HTTP route on the pod, full stop.
- Keep the root filesystem read-only compatible; write only to declared
  state and temporary mounts.
- Bound queues and reconnect behavior for `/events`; a slow SSE consumer
  must not cause unbounded memory growth.
- Log configuration rejection and snapshot-serving failures with camera ID
  and event ID, without secrets or embeddings.

## Release acceptance gate

The image is acceptable only when all checks pass:

1. `desired-state.schema.json` and `analytics-event.schema.json` (surveillance
   pack) validate their examples via
   `scripts/validate-tvt-identity-contract.py`, mirroring
   `scripts/validate-sporada-contract.py` in ApexFabric-k3s.
2. `zones.face_recognition[]` accepts a valid ≥3-point polygon and rejects a
   self-intersecting, zero-area, or out-of-range-coordinate one, matching
   the validation already required of every other zone in this repo.
3. A live event of every event type (`face_detection_event`,
   `enrollment_capture_event`) validates against
   `surveillance-edge-runtime-2026.08.24-v3/analytics-event.schema.json`,
   with every `embeddings.*` vector at unit L2 norm (within floating-point
   tolerance) and consistent dimension across all cameras in the
   deployment.
4. A live `plate_read_event` with `payload.location` populated validates
   against both the **pinned, unmodified**
   `traffic-edge-runtime-2026.08.21-v4/analytics-event.schema.json` and this
   directory's `traffic-analytics-event.draft-schema.json`.
5. Every live event's `snapshot_url` returns `200`, an allowed image content
   type, and non-empty content.
6. The pod's actual route table matches `image-contract.yaml`'s `endpoints`
   block exactly (`healthz`, `readyz`, `metrics`, `events`, `snapshots`) —
   no enrollment or gallery HTTP route exists, and no `/api/enrollment`,
   `/api/face_gallery`, or `/api/face_group` route was reintroduced.
7. `tests/test_traffic_qualification.py`'s pinned checksum for
   `traffic-edge-runtime-2026.08.21-v4/analytics-event.schema.json` is
   unchanged — this work must not touch that vendored file.
8. (Control plane, not edge, but blocking promotion of this directory as a
   whole per `Aggregation.md`): a concurrency test that fires two unmatched
   sightings of the same embedding at two different gates within the same
   instant produces exactly one `person_id`, not two — implemented as
   `tests/test_identity.py::IdentityResolutionTests::test_concurrent_unmatched_sightings_of_the_same_person_resolve_to_one_person_id`.

Any failed item blocks promoting `docs/contracts/tvt-identity-v1/` into a
real `solution-packs/catalog/` entry.
