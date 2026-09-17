# Attendance and vehicle-traffic aggregation: low-level implementation plan

Status: draft design note, staged alongside `CV-PIPELINE-HANDOFF.md` in this
directory. Not implemented. It has the same promotion status as the rest of
`docs/contracts/tvt-identity-v1/` — see `README.md` in this directory for the
staging/promotion lifecycle.

## Scope and placement

Attendance (inside/outside duration) and daily vehicle entry/exit reporting
are **not new CV event types**. Per `CV-PIPELINE-HANDOFF.md` and this
directory's `README.md`, the CV pipeline's only obligation for these two use
cases is to populate `payload.location` on `face_match_event` and
`plate_read_event` when a camera has an entry/exit zone pair configured. The
correlation and rollup logic described below is **control-plane work**, not
CV pipeline work.

Two architectural framings for where this logic runs currently disagree in
this repo and have not been reconciled:

- `docs/contracts/tvt-identity-v1/README.md` states the report endpoints
  (`/api/reports/attendance`, `/api/reports/vehicle-traffic`) are host
  control-plane API surface, flat routes matching every other route in
  `apexfabric/control_plane/server.py` — i.e. this runs in the existing host
  process, not in K3s.
- `HLD.md`'s system diagram and `LLD_PLAN.md` section 12.4 instead describe
  "inside/outside event correlation and attendance" and "vehicle entry/exit
  aggregation" as candidate independent Solution Pack bundles — i.e. their
  own K3s-deployed pods, downstream of an in-cluster event path that does not
  currently exist.

This plan follows the README's framing (host control-plane process) because
it is the more specific, more recent document and because the raw event
evidence this logic depends on already lands in the control plane's
`TelemetryStore`, not anywhere in the cluster. Building a separate correlator
pod would first require new event-distribution plumbing and a durable-data
design that `LLD_PLAN.md` (12.4) says has not been approved.

## Shared mechanics (both use cases)

- New module `apexfabric/control_plane/reporting.py`, structured like the
  existing `apexfabric/control_plane/alerts.py`: a `SCHEMA` string executed
  once in `TelemetryStore.__init__` alongside `ALERT_SCHEMA`, plus one
  `evaluate_*()` function per use case called from `TelemetryStore.ingest()`
  immediately after `evaluate_alerts(...)` (`telemetry.py:154`) — same
  transaction, same `if inserted:` guard, so a retried SSE event (identical
  `event_id`) never double-correlates.
- Each `evaluate_*()` function early-returns on the wrong `event_type` — a
  cheap no-op for every other event passing through ingest.
- Correlation is a stateful open/close machine per key, mirroring
  `alert_rule_state`'s `(rule_id, deployment_id, camera_id) → active/last_trigger`
  shape — here it is `(join_key, gate) → open session row`.
- `gate` is derived by stripping the `_entry`/`_exit` suffix from
  `payload.location.id` (e.g. `gate-2-face_entry` → `gate-2-face`). Pairing
  is scoped to the same gate, not globally per person/plate, so a crossing at
  one gate is never paired with an unrelated crossing at another.
- This derived data needs its **own retention policy**, separate from
  `TelemetryStore`'s size/age-bounded raw-event eviction (`RetentionPolicy`).
  A computed attendance or vehicle session must be able to outlive the raw
  `face_match_event`/`plate_read_event` it was built from, or ordinary
  eviction of old events silently corrupts historical reports. No such
  business-data retention policy exists in this repo today
  (`LLD_PLAN.md:664-669`).

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

**`evaluate_attendance(connection, event_id, deployment_id, payload, received_at)`:**

1. Only act on `event_type == "face_match_event"` — never
   `face_unmatched_event`, which carries no `identity` block and therefore no
   `person_id` to key on.
2. Read `person_id = payload.identity.person_id` and `payload.location`. If
   `location` is absent (whole-frame matching, no zone configured), skip —
   there is no direction to derive.
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
   restart mid-dwell), insert a `closed` row with null `entry_*` fields,
   flagged as an orphan for audit rather than silently dropped.
6. **Nightly sweep** (new small scheduled job — a `systemd` timer, or riding
   the existing `_telemetry_supervisor` loop's cadence): any session still
   `open` past a cutoff (end of business day, or a configured N hours) is
   marked `status = 'forced_closed'`, `duration_seconds = null`. Without
   this, a missed exit leaves a session open indefinitely and corrupts every
   later query for that person.

**API:** `GET /api/reports/attendance?person_id=&date=` sums
`duration_seconds` over `closed`/`forced_closed` sessions whose `entry_time`
falls in the requested day. A second endpoint (or a query flag) lists raw
sessions for audit.

## Vehicles — daily entry/exit reporting

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

The CV pods are untouched by this plan. They keep emitting the same atomic
`face_match_event` / `plate_read_event`, with `payload.location` populated
per the existing contract; everything above is new control-plane code
reading the existing `/events` ingest path after the fact. This preserves
the boundary already established in `CV-PIPELINE-HANDOFF.md`: the edge never
assigns identity and never translates zone geometry into a direction.
