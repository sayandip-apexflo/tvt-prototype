# AGENTS.md — tvt-prototype

This repo is a single-box video-analytics edge system: host Python management
plane (`tvt_edge/`), reused K3s/Solution Pack runtime (`apexfabric/`,
`solution-packs/`), React console (`ui/`), K3s/host installers (`scripts/`,
`deploy/`, `config/`), and docs (`HLD.md`, `LLD_PLAN.md`, `MONITORING.md`,
`METRICS.md`, `COMMANDS.md`, `README.md`).

## 1. Repo map (where to change things)

- `tvt_edge/` — product code. Camera discovery/validation (`camera/`), host API
  (`api/app.py`), cluster sync/health (`cluster/`), DB (`db/`), alerting
  (`alerting/`), observability (`observability/`), watchdog (`watchdog.py`),
  qualification (`qualification.py`), CLI (`cli.py`), crypto/redaction
  (`security.py`), config (`settings.py`, `service.py`, `paths.py`).
- `apexfabric/` + `solution-packs/schema`, `solution-packs/traffic/` — **frozen
  reference plane** copied from `k3s-prototype` commit
  `bcb58030f89b22b14ff1dbd0a68c5806d2f6a002`. Validator, camera-locality,
  renderer, field-manager/apply/prune, reporter/controller, and their tests
  stay behavior-identical (see §6).
- `ui/` — TypeScript/Vite React console, built into `tvt_edge/static/` and
  served by the edge API. `ui/src/`.
- `tests/` — pytest suite (`test_*.py` at top level plus `unit/`,
  `integration/`, `fixtures/`). `tests/test_observability.py`,
  `test_management_plane.py`, `test_alerting.py` encode the hardest invariants.
- `scripts/tvt-edge-operations.sh` — **only** entry point for component
  host operations (subcommands). `scripts/make-tvt-edge-release.sh` — sole
  release builder. `scripts/tvt-edge-fleet.sh` — workstation-side fleet
  orchestrator. `prepare-tvt-edge-host.sh` / `install-tvt-edge-host.sh` —
  sole production host entry points. `config/*.env` — pinned digests/versions.
- `deploy/` — systemd units, K3s manifests, monitoring profile.
  `solution-packs/catalog/` — vendored Traffic v4 pack. `examples/` — docs-only
  inputs. `docs/` — runbooks (`EDGE-RELEASE-BUILD.md`,
  `TRAFFIC-EDGE-QUALIFICATION.md`, `PIPELINE-TRAFFIC-IMAGE.md`,
  `EDGE-FLEET-DEPLOYMENT.md`).

## 2. Environment, build, test

Python 3.12 is required. Always use the repo venv binaries, never bare
`python`/`pytest`:

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q
```

UI (Node, loopback dev server only):

```bash
npm --prefix ui install
npm --prefix ui test        # vitest run
npm --prefix ui run build   # tsc --noEmit && vite build
```

You **must** run `npm --prefix ui run build` before building a wheel,
installing the service, or cutting a release — the Python package ships
`static/index.html` + `static/assets/*` (see `pyproject.toml`
`tool.setuptools.package-data`).

Useful local API checks (loopback only, docs-only credentials):

```bash
.venv/bin/tvt-k3s validate solution-packs/traffic/traffic-edge-runtime-intel-285h.yaml
.venv/bin/tvt-k3s render solution-packs/traffic/traffic-edge-runtime-intel-285h.yaml \
  --registry 127.0.0.1:5000 > /tmp/tvt-traffic-rendered.yaml
.venv/bin/tvt-k3s apply solution-packs/traffic/traffic-edge-runtime-intel-285h.yaml \
  --registry 127.0.0.1:5000 \
  --secret-inputs examples/traffic.secret-inputs.example.json \
  --dry-run
npm --prefix ui run dev   # API on :8088 + Vite on loopback
```

Alembic: `alembic.ini` at root, migrations in
`tvt_edge/db/migrations/versions/`. Never edit an applied migration; add a new
revision.

## 3. Working conventions

- Read `HLD.md` + relevant section of `LLD_PLAN.md` before changing camera,
  sync, alerting, or recovery behavior. Read `MONITORING.md`/`METRICS.md`
  before touching metrics/logs/alerts.
- Keep changes small and local: new TVT behavior belongs under `tvt_edge/`,
  never inside remote-provisioning/multi-node paths (those are omitted by
  design — see `LLD_PLAN.md` §1).
- Python: type-annotate new public functions, use UTC `timestamptz` for new
  timestamps, keep `tvt_edge` imports absolute.
- UI: TypeScript strict (`tsc --noEmit` must pass); no direct K8s access, no
  secret material in state/logs; management API is `http://127.0.0.1:8088`.
- Shell: new host operations go in `scripts/tvt-edge-operations.sh` as
  subcommands plus `lib/` helpers — never add standalone component installers
  to the release bundle. Scripts must be idempotent and fail without mutating
  the host when prerequisites are missing.
- Do not commit `__pycache__/`, `.venv/`, `build/`, `node_modules/`,
  `.tvt/`, local locks under `build/pipeline/`, or any generated release
  artifact.

## 4. Security invariants (STRICT — no exceptions)

These apply to code, tests, examples, logs, metrics, emails, and any file you
touch:

- **Never** write real camera credentials, RTSP URLs, camera IPs, K8s tokens,
  Secret bodies, telemetry/API keys, SendGrid keys, enrollment credentials, or
  `site.yaml` secrets into a tracked file. `examples/` and committed fixtures
  are documentation-only values.
- Camera credentials are **write-only** from the browser/API: AES-256-GCM
  ciphertext in PostgreSQL via `tvt_edge/security.py` (`CredentialKeyring`,
  32-byte `vN.key` files, `root:tvt` `0640`, no nonce reuse). The API returns
  only `credentials_configured: true|false`. Never put credential-bearing RTSP
  URLs in bundles, ConfigMaps, Pod env, annotations, Events, API responses,
  job/audit records, logs, metrics, alerts, or email.
- Faces, embeddings, person names, number plates, and raw HTTP bodies do not
  belong in metrics, logs, alerts, or email. Business/report data has its own
  (stub) path — never reuse the camera-inventory DB or Prometheus/Loki as the
  business store.
- Redact **in the application before stdout** using
  `tvt_edge/observability/logging.py` (`redact_text`/`redact_value`,
  `SENSITIVE_KEYS`) and `tvt_edge/security.py::redact`. Alloy/collector
  redaction is defense-in-depth only.
- The K3s watchdog (`tvt_edge/watchdog.py`) accepts no user-supplied commands
  or arguments. The edge API exposes no arbitrary kubectl/shell/log-path/file/
  systemd operations. Alert payloads never supply recipients, templates,
  headers, SMTP settings, or commands.
- `site.yaml`/site files carry identifiers only (`site_key`, `edge_id`,
  `display_name`, `timezone`). Pass pipeline credentials by root-owned file
  path (`--pipeline-credentials-file`) — never as argv. Preserve existing
  `/etc/tvt/*.env`.

## 5. Observability invariants (STRICT)

- Metrics: use only `tvt_edge/observability/metrics.py` (`HttpMetrics`,
  `EdgeMetrics`, `AlertDispatcherMetrics`, `WatchdogMetricsCollector`) and its
  `LabelPolicy`. Metric objects stay private; callers go through validating
  methods. Enforced bounds: `MAX_CAMERAS = 8`, DNS-safe `camera_id`, fixed
  `use_case`/`service`/`reason`/`route` allowlists, normalized HTTP
  method/route/status-class, bounded version strings. `MetricsContractError`
  on violation is intended — do not catch-and-flatten it.
- **Never** use request/operation/event/frame IDs, IPs, URLs, usernames,
  tokens, exception text, stack traces, timestamps, faces, or plates as metric
  labels. New `reason`/`error_code`/`use_case` values must extend the
  allowlists in `metrics.py` **and** be documented in `METRICS.md`/`MONITORING.md`.
- Gauges in multiprocess Pods need explicit modes (`livesum`/`livemin`/
  `livemax`); never `liveall` without justification. The scrape registry via
  `render_metrics()` must contain only the multiprocess collector — do not
  re-register app metrics into it. `PROMETHEUS_MULTIPROC_DIR` handling and
  worker-exit cleanup follow `MONITORING.md` §7.4.
- Logging: one JSON object per stdout line via `JsonFormatter`
  (`configure_json_logging`); required fields `timestamp/level/service/event/
  message/error_code`. Correlation via `bind_log_context`
  (`request_id`/`operation_id`/`event_id`/`stream_session_id`, `contextvars`)
  and `X-Request-ID` header accept-or-regenerate policy — never as metric
  labels. No multiline tracebacks; exception goes in escaped `stack_trace`
  field. Every actionable error does **both**: increment the bounded error
  counter **and** emit one correlated JSON error log.
- Alert email content is limited to site/severity/alert-name/component/stable
  `camera_id`/safe summary/times/state/dashboard+runbook links. Dispatcher
  owns reminders/recovery eligibility/retry/expiry/audit; Alertmanager owns
  grouping/inhibition/timing. Recovery email only if the firing email for that
  occurrence was delivered. Keep Prometheus as the alert source; Loki-derived
  alerts only when a metric cannot represent the condition.

## 6. Frozen reference plane (STRICT)

- Do not modify bundle schema/semantics, camera-locality, renderer output
  (Namespace/Deployments/Services/ConfigMaps/Secrets/PVCs/policies/probes),
  deterministic revision hashing, server-side apply field manager, ownership
  labels, prune rules, PVC retention, `ApexNodeStatus` contract, or
  reporter/controller label ownership to fix a TVT problem. Add TVT behavior
  in `tvt_edge/` adapters (catalog adapter, allowlisted Apply, camera sync)
  instead.
- `solution-packs/` structure and the Traffic pack stay reference-format;
  per-deployment desired-state + camera-source Secrets keep bundle-derived
  names; camera URLs mount read-only at
  `/run/secrets/apexfabric/<camera_id>.rtsp` via `subPath` (+ controlled
  rollout restart on change). Direct per-workload RTSP sessions only — no
  gateway/restreaming layer.
- Reference tests (`test_camera_locality.py`, `test_tvt_runtime.py`,
  reporter/controller tests) must keep passing unmodified. Add TVT coverage
  without altering reference assertions.

## 7. API / DB / sync rules

- API binds loopback (`127.0.0.1:8088`); UI binds the on-site management
  interface only. All responses carry `X-Request-ID`; mutations append an
  audit event. Passwords are write-only; camera list/detail responses are
  non-secret.
- DB: host PostgreSQL on Unix socket/loopback, SQLAlchemy + Alembic, separate
  migration/app roles (`tvt-alert` role for the dispatcher). Never store
  rendered K8s Secret bodies. Claim async work with
  `SELECT ... FOR UPDATE SKIP LOCKED`. `applied_revision` means Secrets+bundle
  accepted **and** rollout completed — not inference success.
- Sync/recovery: single Uvicorn worker in V1 (no duplicate scanners); slow
  RTSP work in killable child processes with hard deadlines. K3s-down is
  degraded state (DB update succeeds, sync stays pending), not an edge-service
  startup failure. If load ever forces multiple workers, move background loops
  out or take PostgreSQL advisory locks first.

## 8. Host / release operations (host-only, brief)

Production hosts use **only** the release-bundle entry points (see
`COMMANDS.md` + `docs/EDGE-RELEASE-BUILD.md`); do not run component installers
individually on a clean host. Probe the edge over SSH first (read-only, writes
the inventory + `.sha256` sidecar), then build one bundle per edge profile:

```bash
./scripts/tvt-edge-operations.sh probe-edge-hardware \
  --ssh tvt-edge-01 --output /srv/tvt-release/edge-hardware-inventory.json
./scripts/make-tvt-edge-release.sh \
  --input-directory /srv/tvt-release/inputs-metis \
  --output-directory /srv/tvt-release/output/tvt-edge-release-0.1.0-intel-285h-metis \
  --edge-inventory /srv/tvt-release/edge-hardware-inventory.json \
  --version 0.1.0 --source-commit "$(git rev-parse HEAD)"
sudo ./prepare-tvt-edge-host.sh --bundle /media/tvt/release --mode offline
sudo reboot
sudo ./install-tvt-edge-host.sh --bundle /media/tvt/release \
  --site-config /media/tvt/site.yaml --prepare-mode offline
```

`--verify-only` is the read-only health check; `--resume` is the explicit
intent after fixing a failed stage. Evidence lands in
`/var/lib/tvt/install/` (`installation-report.json`, never a Traffic
deployment — onboarding/deploy stay explicit UI actions). Never run these
against your dev machine; the repo-local equivalents are the
`tvt-edge-operations.sh` subcommands and the qualification runner
(`tvt-traffic-qualify`, `docs/TRAFFIC-EDGE-QUALIFICATION.md`).

## 9. Before you finish

- Run: `.venv/bin/python -m pytest -q` and (if `ui/` touched)
  `npm --prefix ui test` + `npm --prefix ui run build`.
- For metrics/log/alerting changes, also run the focused suites
  (`tests/test_observability.py`, `test_alerting.py`,
  `test_management_plane.py`) and verify no secret/URL/IP/face/plate appears
  in new API responses, logs, metrics output, or email fixtures.
- For `solution-packs/`/renderer-adjacent changes, prove reference tests pass
  unmodified and diff rendered manifests to confirm no unexpected K8s output.
- Summarize what you changed, which invariants you checked, and the exact
  commands + results.
