# TVT identity/attendance use cases — draft contract delta

This directory stages the schema changes needed for face recognition, face
enrollment, ANPR-based entry/exit direction, attendance reporting, and daily
vehicle entry/exit reporting. It follows the same shape as ApexFabric-k3s's
`docs/contracts/sporada-secure-v1/` staging area: changes land here first,
then get promoted into a real `solution-packs/catalog/<pack>-<version>/`
directory once accepted — the same lifecycle `sporada-secure-v1` went through
before `solution-packs/catalog/sporada-secure-2026.09.16-v6/` existed.

## What changed and where

**Surveillance pack — edited in place**
(`solution-packs/catalog/surveillance-edge-runtime-2026.08.24-v3/`)

This pack has no pinned checksums and no qualification pipeline referencing
it (unlike the traffic pack, see below), so it was safe to extend directly:

- `analytics-event.schema.json` — new file. Defines `face_match_event`,
  `face_unmatched_event` (application `face_recognition`), and
  `enrollment_capture_event` (application `face_enrollment`).
- `analytics-event.examples.json` — new file. One example per event type.
- `desired-state.schema.json` — added `face_enrollment` to the apps enum,
  `config.zones.face_recognition[]` (optional, whole-frame if omitted),
  and `config.face_gallery` (control-plane-owned gallery bundle delivered
  through desired state, per the enrollment decision below).
- `image-contract.yaml` — added geometry for `face_recognition`, added the
  `face_enrollment` app, and **removed** the `faceGallery`/`faceEnrollment`/
  `faceGroup` HTTP routes that were declared but never implemented anywhere
  in `apexfabric/control_plane/server.py`. Biometric writes never cross the
  unauthenticated pod-proxy surface; enrollment now flows entirely through
  `config.face_gallery` (control plane push) plus `enrollment_capture_event`
  (kiosk-assist candidate frames) on the existing `/events` stream.

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

## Enrollment decision

Control-plane-owned gallery push (`config.face_gallery`, versioned bundle,
applied with the same transactional validate → reject-stale → apply →
ready-only-when-active rules as any other desired-state revision) plus a
kiosk-assist `enrollment_capture_event`. The edge never extracts an
embedding for enrollment or assigns a `person_id` — it only scores candidate
frames (`payload.quality`) for a human operator to pick from in the
control-plane UI, and only ever loads a gallery bundle the control plane
already built.

## Reporting

Attendance and vehicle-traffic reporting are not new event types — they're
control-plane aggregation over `face_match_event`/`plate_read_event` pairs
already carrying `location`. No event schema change is needed for them; the
report endpoints themselves (`/api/reports/attendance`,
`/api/reports/vehicle-traffic` — flat `/api/...`, matching every existing
route in `apexfabric/control_plane/server.py`, not `/api/v1/...`) are
control-plane API surface, out of scope for this event-contract deliverable.
