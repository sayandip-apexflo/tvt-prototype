# TVT Mills pilot — canonical CV contract (v1)

Status: **active**. This supersedes `docs/contracts/tvt-identity-v1/` (kept
in git history only) and the two-pack catalog it staged changes against
(`surveillance-edge-runtime`, `traffic-edge-runtime`, both removed from
`solution-packs/catalog/`).

## Why this replaced the old contract

The CV vendor delivered `tvt-edge-runtime-intel-285h-2026.09.18-v1`
(`https://github.com/BhashyamDayasagar587/TVT_images`, release
`tvt-edge-intel-285h-2026.09.18-v1`): a single image running face
recognition, face enrollment, and ANPR together in one pod, with one
`desired_state.json` and one `/events` stream. This does not fit the old
two-pack architecture (separately versioned/pinned surveillance and traffic
images) or the old identity contract's two-zone `_entry`/`_exit` direction
convention (the vendor's image uses line-crossing with an `accepted`
direction instead). Rather than asking the vendor to rebuild against the
old contract, the decision was made to retire the two-pack architecture and
adopt this vendor's actual delivered shape as the one canonical CV contract
for the whole TVT project.

The canonical pack lives at
`solution-packs/catalog/tvt-mills-pilot-2026.09.18-v1/`. Its
`provenance.json` records exactly what came from the vendor unmodified
(`analytics-event.schema.json` — this is what the shipped binary emits, so
it isn't rewritten) versus what this repo authored to make the delivery
ingestible by `apexfabric/solution_management/catalog.py` (`image-contract
.yaml`, and `desired-state.schema.json` with `cameras.items` inlined and
line `id`s required to end in `_entry`/`_exit`).

## Accepted trade-offs (reviewed, not oversights)

- **No body/ReID embedding in v1.** The vendor's image only ever emits
  `payload.embeddings.face`; `body` is never sent. `apexfabric/control_plane
  /identity.py:resolve_identity` already treats `body` as optional and
  requires no code change — set `APEXFABRIC_BODY_EMBEDDING_DIM` to any
  placeholder positive integer (it will simply never receive a vector to
  validate against that dimension). The practical effect: no ReID fallback
  when a face is briefly occluded or angled away — matching is face-only.
- **Direction via line-crossing, not paired zones.** Every configured line's
  `id` must end in `_entry`/`_exit` (enforced by this pack's
  `desired-state.schema.json` and `scripts/validate-tvt-mills-contract.py`);
  `apexfabric/control_plane/reporting.py:_gate_and_role` derives gate +
  role from that suffix on `payload.line.id` (face events) or
  `payload.location.id`/`payload.line.id` (ANPR, which supports both zone
  and line framing per the vendor's schema).
- **Enrollment is time-shared, not a dedicated camera.** There is no 6th
  enrollment-only camera. Any of the five `face_recognition` cameras can be
  temporarily switched to `apps: ["face_enrollment"]` (the only combination
  the schema allows for that app) and back — see
  `apexfabric/control_plane/enrollment_windows.py`. While a camera is in an
  enrollment window it stops contributing attendance/vehicle events at its
  gate; any open session there is force-closed by the existing
  `reporting.sweep_stale_sessions` cutoff like any other missed exit. This
  is accepted, not silently dropped.

## Security posture (unchanged from before)

Embeddings must never reach a metric label, a log line, or an alert email
(`LLD_PLAN.md` sections 14 and 17). The `/events` SSE stream into the
control plane's vector DB is the one access-controlled path for this data.
There is no gallery or enrollment HTTP route on the pod — never was, and
this delivery doesn't add one either.
