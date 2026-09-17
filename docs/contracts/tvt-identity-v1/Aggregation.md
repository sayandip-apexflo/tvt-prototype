# Identity resolution, attendance, and vehicle-traffic aggregation: low-level implementation plan

Status: draft design note, staged alongside `CV-PIPELINE-HANDOFF.md` in this
directory. Not implemented. It has the same promotion status as the rest of
`docs/contracts/tvt-identity-v1/` — see `README.md` in this directory for the
staging/promotion lifecycle, and its 2026-09-17 revision note for why this
plan now includes a vector DB and auto-enrollment step that an earlier draft
did not.

## Scope and placement

Per `CV-PIPELINE-HANDOFF.md` and this directory's `README.md`, the CV
pipeline's obligation is to emit a normalized embedding (`payload.embeddings`)
on every `face_detection_event`/`enrollment_capture_event`, and to populate
`payload.location` on `face_detection_event`/`plate_read_event` when a camera
has an entry/exit zone pair configured. Everything in this document —
matching an embedding against known people, auto-enrolling new ones, and
rolling both up into attendance/vehicle-traffic reports — is **control-plane
work**, not CV pipeline work.

This plan follows `docs/contracts/tvt-identity-v1/README.md`'s framing (host
control-plane process, not a new K3s-deployed pod) because the raw event
evidence this logic depends on already lands in the control plane's
`TelemetryStore`, not anywhere in the cluster, and because `LLD_PLAN.md`
section 12.4 says a separate correlator pod's event-distribution plumbing and
durable-data design has not been approved. `HLD.md`'s system diagram
describing these as candidate independent Solution Pack bundles remains
unreconciled with the README the same way it did before this revision; that
disagreement is unchanged by adding a vector DB.

## Shared mechanics (identity resolution, attendance, vehicle-traffic)

- New module `apexfabric/control_plane/reporting.py`, structured like the
  existing `apexfabric/control_plane/alerts.py`: a `SCHEMA` string executed
  once in `TelemetryStore.__init__` alongside `ALERT_SCHEMA`, plus one
  `evaluate_*()`/`resolve_*()` function per stage called from
  `TelemetryStore.ingest()` immediately after `evaluate_alerts(...)`
  (`telemetry.py:154`) — same transaction, same `if inserted:` guard, so a
  retried SSE event (identical `event_id`) never re-resolves an identity or
  double-correlates a session.
- Each function early-returns on the wrong `event_type` — a cheap no-op for
  every other event passing through ingest.
- Attendance/vehicle correlation is a stateful open/close machine per key,
  mirroring `alert_rule_state`'s
  `(rule_id, deployment_id, camera_id) → active/last_trigger` shape — here it
  is `(join_key, gate) → open session row`.
- `gate` is derived by stripping the `_entry`/`_exit` suffix from
  `payload.location.id` (e.g. `gate-2-face_entry` → `gate-2-face`). Pairing
  is scoped to the same gate, not globally per person/plate, so a crossing at
  one gate is never paired with an unrelated crossing at another.
- This derived data needs its **own retention policy**, separate from
  `TelemetryStore`'s size/age-bounded raw-event eviction (`RetentionPolicy`).
  A person's identity record, its reference embeddings, and a computed
  attendance or vehicle session must be able to outlive the raw
  `face_detection_event`/`plate_read_event` they were built from, or ordinary
  eviction of old events silently corrupts identity matching and historical
  reports. No such business-data retention policy exists in this repo today
  (`LLD_PLAN.md:664-669`).

## Identity resolution and auto-enrollment (new)

This stage runs first, before attendance aggregation, and produces the
`person_id` that attendance keys on. It is the only place in this platform
that assigns a `person_id`; the edge never does (`CV-PIPELINE-HANDOFF.md`).

### Storage

Embedded vector DB (sqlite-vec) colocated with the control plane's existing
per-concern sqlite files (`telemetry.sqlite3`, `catalog.sqlite3`,
`device-registry.sqlite3`), as a new `identity.sqlite3`:

```sql
CREATE TABLE persons (
  person_id TEXT PRIMARY KEY,
  display_name TEXT,
  status TEXT NOT NULL CHECK(status IN ('auto_enrolled', 'named')) DEFAULT 'auto_enrolled',
  first_seen REAL NOT NULL,
  last_seen REAL NOT NULL,
  enrollment_source_camera_id TEXT NOT NULL,
  enrollment_source_event_id TEXT NOT NULL
);

-- sqlite-vec virtual table; rows are addressed by rowid, so a companion
-- table carries the metadata a plain vec0 table cannot.
CREATE VIRTUAL TABLE person_embeddings USING vec0(embedding float[EMBED_DIM]);

CREATE TABLE person_embedding_meta (
  rowid INTEGER PRIMARY KEY,  -- same rowid as the matching person_embeddings row
  person_id TEXT NOT NULL REFERENCES persons(person_id),
  embedding_kind TEXT NOT NULL CHECK(embedding_kind IN ('face', 'body')),
  source_event_id TEXT NOT NULL,
  captured_at REAL NOT NULL
);
CREATE INDEX person_embedding_meta_person ON person_embedding_meta(person_id, embedding_kind);
```

`EMBED_DIM` is fixed per deployment by the model actually baked into the
running `surveillance-edge-runtime` image (see `CV-PIPELINE-HANDOFF.md`'s
"Embeddings" section) and set once when `identity.sqlite3` is created. A
vector arriving with a different length is a hard rejection at ingest, not a
silent pad/truncate — logged with camera ID and event ID, never with the
embedding itself.

Storage is **append-only**: every accepted sighting adds a new reference
vector for its resolved person rather than overwriting a single "canonical"
vector. Matching is nearest-neighbor across all of a person's stored vectors
(effectively taking the best similarity across every past observation), which
tolerates lighting/pose/angle drift far better than maintaining one running
centroid, at the cost of the table growing without bound — the business-data
retention policy above must eventually cap or downsample per-person vectors
too, not just raw events; that policy is out of scope for this v1 plan.

### Matching decision

Only the `face` embedding drives the match/enroll decision in v1. The `body`
(ReID) embedding is stored (linked to whatever `person_id` the face decision
resolves to) so it is available for future work, but it must **not** be used
to decide identity by itself — clothing/appearance similarity across
different people is far less discriminative than face similarity, and wiring
it into the match step without addressing that would silently merge distinct
people who happen to be dressed alike.

`resolve_identity(connection, event_id, camera_id, embeddings, received_at)`:

1. Only act on `event_type in ("face_detection_event", "enrollment_capture_event")`.
2. Reject outright (log camera ID + event ID, never the vector) if
   `len(embeddings.face) != EMBED_DIM` or the vector's L2 norm is not ~1
   within tolerance — a non-normalized vector corrupts every similarity score
   computed against it, including other people's already-correct entries.
3. Run a cosine-similarity nearest-neighbor query (`person_embeddings MATCH
   embeddings.face` filtered to `embedding_kind = 'face'` via the meta table,
   ordered by distance) and take the best match.
4. If the best match's similarity is at or above the configured threshold
   (a control-plane setting, tuned per deployed model — not hardcoded in this
   contract): reuse that `person_id`. Insert this sighting's `face` vector
   (and `body` vector, if present) into `person_embeddings`/
   `person_embedding_meta` under that `person_id`; update `persons.last_seen`.
5. Otherwise, this is the race-prone path — **auto-enrollment must happen
   inside one `BEGIN IMMEDIATE` transaction**, not just the final insert:
   1. Re-run the exact same nearest-neighbor query from step 3 now that the
      write lock is held. Another writer may have committed a matching
      vector for this same physical person (seen at a different gate a
      moment earlier) between step 3's read and this transaction acquiring
      the lock.
   2. If that re-check now clears the threshold, treat it as a match (step
      4's handling) — do **not** create a new person.
   3. Only if it still doesn't clear the threshold: `INSERT INTO persons`
      with a freshly generated `person_id`, `status = 'auto_enrolled'`,
      `display_name = NULL`; insert the `face` (and `body`, if present)
      vector under it.
   4. Commit. A second concurrent auto-enrollment attempt for the same
      not-yet-enrolled person blocks on the write lock during this whole
      sequence (sqlite is single-writer), then re-runs its own step-3-style
      search after this commits and finds the just-inserted vector — landing
      in step 4 (match), not step 5.3 (new person). This is the mechanism
      that prevents one human from getting two `person_id`s from two
      near-simultaneous sightings at two different gates.
6. Store the resolved `person_id` alongside the raw event row (a new
   `resolved_person_id` column on the events table, populated for these two
   event types only) so attendance aggregation below never needs to re-run a
   vector search.
7. Idempotency: this function must not run twice for the same `event_id`.
   It sits inside the same `if inserted:` guard as every other
   `evaluate_*()` (see "Shared mechanics"), so a retried SSE event with an
   identical `event_id` never gets resolved a second time — a retry reuses
   whatever `resolved_person_id` the first successful ingest already wrote.

Auto-enrollment applies uniformly to sightings from any of the five
`face_recognition` cameras and the dedicated `face_enrollment` camera — see
`CV-PIPELINE-HANDOFF.md`'s "consequence the developer must not fix
unilaterally" note. `enrollment_capture_event`'s better-controlled framing
(closer camera, `payload.quality` present) makes it the more reliable source
of a person's *first* reference vector, but it does not get special
treatment in the matching algorithm itself — a `face_detection_event` from
any entrance can just as well be the sighting that first enrolls someone.

## Persons — attendance / inside-outside duration

**Table:**

```sql
CREATE TABLE attendance_sessions (
  id TEXT PRIMARY KEY, person_id TEXT NOT NULL, gate TEXT NOT NULL,
  entry_event_id TEXT, entry_time REAL, entry_zone_id TEXT,
  exit_event_id TEXT, exit_time REAL, exit_zone_id TEXT,
  status TEXT NOT NULL CHECK(status IN ('open','closed','forced_closed')),
  duration_seconds REAL
);
CREATE INDEX attendance_open ON attendance_sessions(person_id, gate, status);
```

**`evaluate_attendance(connection, event_id, deployment_id, payload, resolved_person_id, received_at)`:**

1. Only act on `event_type == "face_detection_event"`, and only after
   identity resolution (above) has produced a `resolved_person_id` for this
   `event_id` — a resolution rejection (dimension mismatch, malformed
   vector) must not silently fall through to attendance with a null person;
   skip the event and let the rejection be visible in logs instead.
2. Read `person_id = resolved_person_id` and `payload.location`. There is no
   `payload.identity.person_id` on the wire any more — the edge never
   assigns one (see `CV-PIPELINE-HANDOFF.md`). If `location` is absent
   (whole-frame capture, no zone configured), skip — there is no direction
   to derive.
3. Determine role from the suffix of `location.id`. Ignore zone ids with
   neither `_entry` nor `_exit` (e.g. a plain restricted-area zone like
   `restricted-3-face_zone` — that is watchlist geometry, not a gate).
4. **Entry:** if an open session already exists for `(person_id, gate)`,
   treat this as a re-read while lingering in the zone — ignore it and keep
   the original `entry_time` rather than opening a second session. Otherwise
   insert a new `open` row.
5. **Exit:** find the most recent `open` session for `(person_id, gate)`. If
   found, close it — set the exit fields and
   `duration_seconds = exit_time - entry_time`, `status = 'closed'`. If none
   is found (an exit with no prior entry — tailgating, a missed frame, a
   restart mid-dwell, or the person's very first sighting happening to be an
   exit), insert a `closed` row with null `entry_*` fields, flagged as an
   orphan for audit rather than silently dropped.
6. **Nightly sweep** (new small scheduled job — a `systemd` timer, or riding
   the existing `_telemetry_supervisor` loop's cadence): any session still
   `open` past a cutoff (end of business day, or a configured N hours) is
   marked `status = 'forced_closed'`, `duration_seconds = null`. Without
   this, a missed exit leaves a session open indefinitely and corrupts every
   later query for that person.

**API:** `GET /api/reports/attendance?person_id=&date=` sums
`duration_seconds` over `closed`/`forced_closed` sessions whose `entry_time`
falls in the requested day. A second endpoint (or a query flag) lists raw
sessions for audit. Because auto-enrollment has no name attached at capture
time, reports should surface `persons.display_name` where set and fall back
to `person_id` otherwise, and should make `persons.status`
(`auto_enrolled` vs `named`) visible so a viewer can tell a report row is an
unidentified visitor rather than a named employee.

## Vehicles — daily entry/exit reporting

Unaffected by the identity-resolution work above: vehicles are matched by
OCR plate text, not by embedding, and ANPR does not use the vector DB.

**Table:**

```sql
CREATE TABLE vehicle_sessions (
  id TEXT PRIMARY KEY, plate_text TEXT NOT NULL, gate TEXT NOT NULL,
  entry_event_id TEXT, entry_time REAL, entry_zone_id TEXT,
  exit_event_id TEXT, exit_time REAL, exit_zone_id TEXT,
  status TEXT NOT NULL CHECK(status IN ('open','closed','forced_closed'))
);
CREATE INDEX vehicle_open ON vehicle_sessions(plate_text, gate, status);
```

**`evaluate_vehicle_traffic(connection, event_id, deployment_id, payload, received_at)`:**

1. Only act on `event_type == "plate_read_event"`.
2. The join key is `payload.plate.text`, **not** `vehicle_ref` /
   `vehicle_track_id` — those are scoped to a single camera's tracking run
   and reset on pod restart, so they cannot correlate an entry-gate read with
   an exit-gate read produced by a different pod. If `payload.location` is
   absent (no zone pair configured for this camera), skip.
3. Drop reads below a configured `payload.plate.confidence` floor from
   correlation — still store the raw event, just do not let a low-confidence
   OCR guess open or close a session.
4. Same open/close logic as attendance, keyed on `(plate_text, gate)`: an
   entry opens a session (or is absorbed into an already-open one for the
   same plate+gate, so a vehicle idling in the ANPR zone does not spawn
   duplicates); an exit closes the most recent open session for that
   plate+gate.
5. Same nightly sweep for stuck-open sessions.
6. **Known limitation specific to vehicles:** OCR misreads mean the same
   physical vehicle can read as two different plate strings at entry versus
   exit (e.g. `KA05MN7788` vs `KA05MNZ788`). Exact-match join treats these as
   two different vehicles. Ship v1 with exact match only. Fuzzy/edit-distance
   matching is a real follow-up, not a v1 requirement, and must not be
   silently assumed to work.

**API:** `GET /api/reports/vehicle-traffic?date=&gate=` returns the count of
distinct plates entered/exited that day, with paired durations where
available.

## What this does not change

The CV pods are untouched by this plan beyond what `CV-PIPELINE-HANDOFF.md`
already specifies. They emit `face_detection_event` /
`enrollment_capture_event` with normalized embeddings and
`plate_read_event`, with `payload.location` populated per the existing
contract; everything above is new control-plane code reading the existing
`/events` ingest path after the fact. The edge still never assigns identity
and never translates zone geometry into a direction — it now also never
holds a gallery or matches an embedding against anything itself. Those
boundaries are unchanged from before this revision; what changed is that the
control plane now does the matching over embeddings the edge sends it,
instead of the edge matching against a gallery the control plane used to
push down.
