# TVT identity/attendance use cases — draft contract delta

This directory stages the schema changes needed for face recognition, face
enrollment, ANPR-based entry/exit direction, attendance reporting, and daily
vehicle entry/exit reporting. It follows the same shape as ApexFabric-k3s's
`docs/contracts/sporada-secure-v1/` staging area: changes land here first,
then get promoted into a real `solution-packs/catalog/<pack>-<version>/`
directory once accepted — the same lifecycle `sporada-secure-v1` went through
before `solution-packs/catalog/sporada-secure-2026.09.16-v6/` existed.

## Revision note (2026-09-17)

An earlier draft of this directory specified a stricter design: the edge
never emitted embeddings at all, and enrollment was a human-operator kiosk
flow (control plane builds a gallery bundle from an operator-picked frame,
pushes it down through `config.face_gallery`, edge matches locally). That
design has been **replaced** by the one below, per an explicit customer/
product decision: the edge now extracts and returns embeddings, the central
plane owns a vector DB, and unmatched faces are auto-enrolled without a
human review step. This is not a reversal of `LLD_PLAN.md`/`HLD.md`'s
security posture — those documents ban faces/embeddings from **metrics,
logs, and alert email** (`LLD_PLAN.md:708-709`, `:912`, `:990`) and require
"a separate versioned and access-controlled data path" for business events.
The existing `/events` SSE stream into the control plane already is that
path. Embeddings travel there and nowhere else; they still must never reach
Prometheus, Loki, or the alert dispatcher. See `CV-PIPELINE-HANDOFF.md` and
`Aggregation.md` for the resulting contract and control-plane design.

## What changed and where

**Surveillance pack — edited in place**
(`solution-packs/catalog/surveillance-edge-runtime-2026.08.24-v3/`)

This pack has no pinned checksums and no qualification pipeline referencing
it (unlike the traffic pack, see below), so it was safe to extend directly:

- `analytics-event.schema.json` — `face_match_event`/`face_unmatched_event`
  are gone; both collapse into one `face_detection_event` (application
  `face_recognition`), because the edge no longer holds a gallery and
  therefore cannot itself say whether a face matched anything. It carries
  `payload.embeddings.face` and `payload.embeddings.body` (both required).
  `enrollment_capture_event` (application `face_enrollment`) carries
  `payload.embeddings.face` only — no `body`, since the dedicated enrollment
  camera is a single-purpose face capture, not a re-identification source.
  Neither event carries a `person_id` or `match_score` any more: identity is
  a central-plane fact now, not something the edge can assert.
- `analytics-event.examples.json` — one example per event type, with
  illustrative (short, non-production-dimension) embedding vectors.
- `desired-state.schema.json` — `config.zones.face_recognition[]` (the
  `identity_zone` def, optional, whole-frame if omitted) is unchanged.
  `config.face_gallery` is **removed** — the edge no longer downloads or
  holds any gallery bundle, so there is nothing to push down through desired
  state for identity any more.
- `image-contract.yaml` — geometry for `face_recognition` and the
  `face_enrollment` app are unchanged. The `faceGallery`/`faceEnrollment`/
  `faceGroup` HTTP routes remain absent (they were declared but never
  implemented before this work even started, and implementing them would
  mean accepting biometric writes through the unauthenticated pod-proxy
  surface). Embeddings now flow entirely through the existing `/events`
  stream; there is no gallery download path to remove a route for any more.

**Traffic pack — staged here, not edited in place**
(`solution-packs/catalog/traffic-edge-runtime-2026.08.21-v4/`)

`tests/test_traffic_qualification.py` pins this pack's
`analytics-event.schema.json` to an exact SHA256 and validates a vendored
delivery against it end to end (see `tvt_edge/delivery_metadata.py` — this
checksum pinning was a deliberate choice to track the vendor's exact
shipped contract). Editing it in place would silently break that pin and
misrepresent what the pinned v4 image actually emits. The ANPR delta is
staged here instead:

- `traffic-analytics-event.draft-schema.json` — the v4 schema plus a
  documented (but non-breaking) `location` payload field for `plate_read_event`.
  No schema change is strictly required to unlock this: `payload` already
  has `additionalProperties: true` in the pinned v4 schema, so a runtime can
  start emitting `payload.location` today and still validate against v4
  unmodified. This draft exists purely to document the field and its
  validation shape ahead of promotion into a new pack version (e.g.
  `traffic-edge-runtime-2026.XX.XX-v5`).
- `traffic-analytics-event.example.json` — a `plate_read_event` with
  `location` populated for an entry zone.

ANPR (vehicles) is unaffected by the embeddings/vector-DB change: plates are
matched by OCR text, not by embedding, so there is no vector-DB involvement
on that side. See `Aggregation.md` for why the two use cases still share the
same gate/direction and dedup framing.

## Direction convention (ANPR and face_recognition)

Per the "two zones per gate" decision: there is **no `direction` field**.
Configure two zones per gate — one for each direction — and give each a
distinguishing `id`/`name` ending in `_entry` or `_exit`, e.g.
`gate-2-anpr_entry` / `gate-2-anpr_exit` for ANPR
(`config.zones.anpr[]`, traffic pack — reuses the existing `polygon_zone`
shape unchanged, so nothing there needed a schema edit either), or
`gate-2-face_entry` / `gate-2-face_exit` for face recognition
(`config.zones.face_recognition[]`, surveillance pack). The control plane
derives direction by looking at which zone `location.id` an event names —
not from a payload field. This keeps both packs' event schemas untouched
for the direction concept itself; only `location` needed adding to the
traffic pack's documented payload shape (it already validates today; see
above).

Note: the traffic pack's existing `polygon_zone` definition has no `id`
field (`solution-packs/catalog/traffic-edge-runtime-2026.08.21-v4/desired-state.schema.json`),
only `name` — so `name` is what should carry the `_entry`/`_exit` suffix for
ANPR gate pairs today. This wasn't changed here (see above, no in-place
edits to the pinned traffic pack) and should be reconciled when this pack
promotes into a new version — ideally by requiring `id` the way the new
`identity_zone` def does for the surveillance pack, so `location.id` in
`plate_read_event` has something stable to reference.

## Enrollment and identity resolution decision

The edge is a sensor, not an identity store. Every `face_recognition` camera
(all five: 2 main entrance, 2 plant entrance, 1 back exit) and the dedicated
`face_enrollment` camera all just extract normalized embeddings and emit
them as events; none of them decide who anyone is. The control plane:

1. Runs a cosine-similarity nearest-neighbor search of every incoming face
   embedding against its vector DB.
2. Reuses the matched `person_id` when the best match clears a configured
   similarity threshold; otherwise auto-enrolls a brand-new `person_id`
   with no human review step.
3. Does both of the above for embeddings arriving from **any** of the five
   `face_recognition` cameras, not just the dedicated enrollment camera —
   an unrecognized face at any entrance becomes a permanent tracked
   identity, by explicit choice. This trades a human review gate for zero
   friction, and it means visitors/contractors/deliveries who are never
   given a name get auto-enrolled and reported on indistinguishably from
   employees unless someone later names them in the control-plane UI.
4. Guards the enroll-vs-match decision with a single-writer transaction so
   two near-simultaneous unmatched sightings of the same physical person at
   two different gates cannot both decide "new person" and mint two
   `person_id`s for one human. See `Aggregation.md` for the mechanism.

Vector DB choice: embedded (sqlite-vec or FAISS), colocated with the
control plane's existing per-concern sqlite files (`telemetry.sqlite3`,
`catalog.sqlite3`, `device-registry.sqlite3`) rather than a new standalone
service — the expected scale (one plant, five cameras, low hundreds to low
thousands of enrolled people) does not need a dedicated ANN service, and
adding one would mean a new stateful workload with its own backup/failure
story that the rest of this platform does not have. See `Aggregation.md`
for the schema.

## Reporting

Attendance and vehicle-traffic reporting are not new event types — they're
control-plane aggregation over resolved-identity `face_detection_event`s
(after step 1-2 above assigns a `person_id`) and `plate_read_event`s, both
already carrying `location`. No event schema change is needed for them; the
report endpoints themselves (`/api/reports/attendance`,
`/api/reports/vehicle-traffic` — flat `/api/...`, matching every existing
route in `apexfabric/control_plane/server.py`, not `/api/v1/...`) are
control-plane API surface, out of scope for this event-contract deliverable.
