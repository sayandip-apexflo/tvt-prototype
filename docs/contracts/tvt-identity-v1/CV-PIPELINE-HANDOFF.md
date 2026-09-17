# TVT identity & attendance CV pipeline delivery contract

Status: draft integration contract for face recognition, face enrollment,
ANPR entry/exit direction, attendance reporting, and daily vehicle
entry/exit reporting. Not yet promoted into `solution-packs/catalog/` — see
`README.md` in this directory for the staging/promotion lifecycle and why
the traffic pack's live files are not edited directly.

This document is the handoff and acceptance checklist for the CV pipeline
developer extending the `surveillance-edge-runtime` and `traffic-edge-runtime`
images with these use cases. The machine-readable files are authoritative:

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
missing bounding boxes, translate zone geometry into a direction, or assign
a `person_id` on the edge's behalf.

## Required HTTP service behavior

Both images already declare the standard management port (`8080`) with
`/healthz`, `/readyz`, `/metrics`, `/events`, and (surveillance only today)
`/snapshots/<camera-id>/<filename>` in their `image-contract.yaml`. This
work adds **no new HTTP routes**. In particular:

- `/api/enrollment`, `/api/face_gallery`, `/api/face_group` must **not**
  exist on the pod. They were declared in the surveillance pack's
  `image-contract.yaml` before this change and have been removed — they
  were never implemented, and implementing them would mean accepting
  biometric writes through the same unauthenticated pod-proxy surface used
  for health/metrics.
- The gallery arrives only through desired state (`config.face_gallery`,
  below). Enrollment capture arrives only as events on the existing
  `/events` stream.

Pre-existing gap, not introduced by this work but worth the developer's
attention: the traffic pack's `image-contract.yaml` does not declare a
`snapshots` endpoint at all, even though `plate_read_event` has always
required `snapshot_url`. Confirm the traffic runtime actually serves
`/snapshots/<camera-id>/<filename>` before relying on it for
`gate-*-anpr` evidence.

## Events and evidence

| Application | Event type | Required event-specific data |
|---|---|---|
| `face_recognition` | `face_match_event` | `payload.identity.person_id`, `payload.identity.match_score`, `payload.subject` (face bbox) |
| `face_recognition` | `face_unmatched_event` | `payload.subject` (face bbox); no `identity` block — this is what drives restricted-zone/watchlist-miss alerting |
| `face_enrollment` | `enrollment_capture_event` | `payload.subject`, `payload.quality` (`sharpness`, `pose_score`, `liveness_score`); no `identity` block — the edge never assigns a `person_id` |
| `anpr` | `plate_read_event` | unchanged (`payload.plate.text`, `payload.vehicle`), plus **optional** `payload.location` when the camera has entry/exit zone pairs configured |

Every `face_recognition`/`face_enrollment` event must carry the same
evidence quadruple already required of `plate_read_event`:
`payload.snapshot_ref` / `snapshot_url` / `snapshot_content_type` /
`snapshot_assets.event_frame`, plus a `snapshot_assets.face_crop` entry
(mirroring the `vehicle_crop`/`plate_crop` convention `wrong_way_event`
already uses). The event frame must be the full frame whose pixel
coordinate system the bounding box uses.

Never emit embeddings, raw base64 face crops, or any other biometric
template in an event or a log line. `subject.bbox` plus a cropped
`snapshot_assets` entry is the only evidence a client should ever need.

Use a globally unique, immutable `event_id`; retries preserve the ID and
exact payload. `timestamp` is UTC RFC3339.

## Zones, gates, and direction — no `direction` field

Entry/exit direction is represented by **two zones per gate**, not a
direction field on the event or the zone:

- ANPR: `config.zones.anpr[]` (traffic pack) — pair `<gate>-anpr_entry` /
  `<gate>-anpr_exit`.
- Face recognition: `config.zones.face_recognition[]` (surveillance pack,
  new `identity_zone` def) — pair `<gate>-face_entry` / `<gate>-face_exit`.

The control plane derives direction from which zone `location.id` an event
names. Neither pack's event schema needed a direction field; the traffic
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
   something stable to reference. The surveillance pack's new
   `identity_zone` def fixes this (`id` is required) for
   `zones.face_recognition[]`; the traffic pack's `polygon_zone` should get
   the same treatment when it promotes to a new version — it cannot be
   fixed in the pinned v4 file.

## Face gallery and enrollment

- No pod HTTP route (see above). The gallery is delivered as
  `config.face_gallery: {source, revision}` through desired state and
  applied with the same transactional rules as any other revision: read and
  validate the whole document, reject a stale or invalid revision without
  altering the last good state, apply atomically, report `/readyz` only
  once the new revision is loaded and active.
- `source` must match
  `file:/run/secrets/apexfabric/face-gallery/<edge_id>.bundle` — mirrors
  the existing `file:/run/secrets/apexfabric/<camera-id>.rtsp` convention
  already used by both packs. The edge reads this file; it never writes to
  it.
- Enrollment kiosk flow: a camera running `application: face_enrollment`
  emits `enrollment_capture_event` candidates with quality/pose/liveness
  scores. A human operator picks a frame and commits the `person_id` in the
  control-plane UI, which produces the next `face_gallery` revision. The
  edge never extracts an embedding for enrollment and never assigns
  identity itself.

## Persistence and shutdown

Same baseline as any TVT runtime pod: working snapshots live under the
declared `/state` volume, written with a temporary name and atomically
renamed into place; SIGTERM stops accepting new frames, completes or
discards in-progress writes, flushes event state, closes the SSE
connection, and exits within the pod termination grace period; a restart
must not expose corrupt evidence.

Additionally for this work: the loaded `face_gallery` bundle is a local
cache of a control-plane-owned artifact, delivered via secret mount, not a
network fetch. Losing the in-memory index on restart must trigger a reload
of the already-resident bundle file at the last-applied revision — it must
not require re-fetching anything and must not silently fall back to
matching against a stale or partial gallery.

## Security and operational requirements

- Run as the non-root UID/GID already declared by each image contract
  (10001:10001 today).
- Read RTSP sources only through `file:/run/secrets/apexfabric/<camera-id>.rtsp`.
- Read the face gallery only through
  `file:/run/secrets/apexfabric/face-gallery/<edge_id>.bundle`; treat it as
  read-only.
- Never emit credentials, host filesystem paths, base64 images, embeddings,
  raw biometric templates, or authorization headers in events or logs.
- No enrollment or gallery HTTP route on the pod, full stop — this was
  previously declared (incorrectly) in this same pack's `image-contract.yaml`
  and has been removed; do not reintroduce it.
- Keep the root filesystem read-only compatible; write only to declared
  state and temporary mounts.
- Bound queues and reconnect behavior for `/events`; a slow SSE consumer
  must not cause unbounded memory growth.
- Log configuration rejection and snapshot-serving failures with camera ID
  and event ID, without secrets.

## Release acceptance gate

The image is acceptable only when all checks pass:

1. `desired-state.schema.json` and `analytics-event.schema.json` (surveillance
   pack) validate their examples. No validation script exists yet for this
   pack — the developer must add one (e.g.
   `scripts/validate-tvt-identity-contract.py`), mirroring
   `scripts/validate-sporada-contract.py` in ApexFabric-k3s, before this can
   be promoted.
2. `zones.face_recognition[]` accepts a valid ≥3-point polygon and rejects a
   self-intersecting, zero-area, or out-of-range-coordinate one, matching
   the validation already required of every other zone in this repo.
3. A live event of every new type (`face_match_event`, `face_unmatched_event`,
   `enrollment_capture_event`) validates against
   `surveillance-edge-runtime-2026.08.24-v3/analytics-event.schema.json`.
4. A live `plate_read_event` with `payload.location` populated validates
   against both the **pinned, unmodified**
   `traffic-edge-runtime-2026.08.21-v4/analytics-event.schema.json` and this
   directory's `traffic-analytics-event.draft-schema.json`.
5. Every live event's `snapshot_url` returns `200`, an allowed image content
   type, and non-empty content.
6. `face_gallery` revision handling: a stale or lower revision is rejected
   without altering the last-good gallery; `/readyz` flips to `200` only
   after the new revision is active.
7. A pod restart with an already-applied `face_gallery` bundle reloads it
   from the resident secret-mounted file without a network fetch and
   without exposing a corrupt or partial gallery.
8. The pod's actual route table matches `image-contract.yaml`'s `endpoints`
   block exactly (`healthz`, `readyz`, `metrics`, `events`, `snapshots`) —
   no enrollment or gallery HTTP route exists.
9. `tests/test_traffic_qualification.py`'s pinned checksum for
   `traffic-edge-runtime-2026.08.21-v4/analytics-event.schema.json` is
   unchanged — this work must not touch that vendored file.

Any failed item blocks promoting `docs/contracts/tvt-identity-v1/` into a
real `solution-packs/catalog/` entry.
