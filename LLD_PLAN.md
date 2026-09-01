# TVT prototype: sample low-level design plan

**Status:** Proposed for review  
**Scope:** Single physical server, single-node K3s, five initially installed cameras with a design ceiling of eight  
**Reference implementation:** `../k3s-prototype`  
**Related TVT documents:** `README.md`, `HLD.md`, `MONITORING.md`, `APEXFABRIC_ARCHITECTURE.md`

## 1. Review summary

This plan keeps the working K3s-plane boundaries from `k3s-prototype` and adds
a TVT-specific host camera-management plane.

The K3s plane retains:

- the `ApexNodeStatus` capability-reporting contract;
- controller-owned node qualification and scheduling labels;
- schema and semantic validation of `DeploymentBundle`;
- deterministic bundle revision hashing and Kubernetes object rendering;
- server-side apply, ownership labels, pruning, and retained PVC behavior;
- Kubernetes scheduling through resource requests and required node affinity;
- startup, readiness, and liveness probes;
- declarative stop/start, rollout, and explicit rollback behavior; and
- digest-pinned images delivered through a local registry.

The TVT-specific addition is a host-managed Python service which discovers
camera candidates, validates RTSP media, persists camera inventory in
PostgreSQL, serves the management API/UI, and synchronizes enabled camera
configuration into K3s.

This revision also proposes a separate host alert-dispatcher service for
durable alert email. That is an intentional scope extension: `HLD.md` and
`MONITORING.md` currently list external email alert delivery as out of scope.
Those documents must be updated if this LLD decision is approved.

This plan deliberately excludes:

- discovery, connection establishment, enrollment, approval, rejection, or
  revocation of remote node agents;
- controller-to-node K3s installation or token delivery;
- controller-side driver bundle construction;
- node-side offline driver download, installation, reboot, and progress RPCs;
- multi-server K3s control-plane high availability; and
- automatic network, VLAN, switch, or camera configuration.

The phrase "discover cameras by reading RTSP streams" is interpreted as two
separate operations:

1. discover candidate devices using ONVIF WS-Discovery and bounded LAN probes;
2. identify a device as a usable camera only after the Python runtime opens and
   reads its RTSP media.

RTSP alone cannot reliably discover an unknown device when its address, stream
path, or credentials are not known.

## 2. Proposed decisions

| Area | Proposed V1 decision | Reason |
|---|---|---|
| Topology | One Ubuntu server runs the K3s server and worker | Matches the TVT deployment and removes remote-node enrollment |
| Product namespace | Retain `apexfabric` for the first implementation; use `monitoring` for observability | Maximizes reuse of manifests, labels, tests, and renderer defaults |
| Host runtime | Python 3.12, one `systemd`-managed process with an asyncio scheduler and one API worker | Simple ownership and no duplicate background jobs |
| Host API | FastAPI served by Uvicorn; compiled React assets served by the same process | Provides typed APIs while keeping the UI alive when K3s is down |
| Database | Host PostgreSQL with SQLAlchemy and Alembic | Camera inventory must remain usable during a K3s outage |
| Camera discovery | ONVIF WS-Discovery first, neighbor-table candidates second, rate-limited TCP probes last | Avoids blind full-network scans where possible |
| RTSP validation | PyAV/FFmpeg in killable child processes with hard deadlines | Verifies packets/keyframes while containing native-library hangs |
| Credential storage | AES-256-GCM ciphertext in PostgreSQL; versioned key stored as a protected host file | Keeps browser/API/database views free of plaintext credentials |
| Camera-to-K3s contract | Installer-owned `ConfigMap/tvt-camera-sources` and `Secret/tvt-camera-credentials` | Smaller V1 than a new CRD and compatible with current renderer/mount patterns |
| Gateway topology | One gateway Pod containing independent per-camera pipelines | Five to eight streams do not justify one Pod per camera initially |
| Gateway implementation | MediaMTX plus a small TVT configuration/health adapter, subject to a short load PoC | Supplies one upstream RTSP pull with compressed cluster-local fan-out |
| CV camera access | CV Pods use `rtsp://tvt-stream-gateway.apexfabric.svc:8554/<camera_id>` | Hides mutable camera addresses and credentials from CV applications |
| Network-camera scheduling | `runtime-connectivity` and `requires_camera_labels: false` | LAN RTSP cameras are not node-local Kubernetes devices |
| Reconciliation | Reuse bundle schema, semantic validator, renderer, field manager, ownership labels, and prune rules | Preserves the proven K3s-plane behavior |
| Monitoring | Follow `MONITORING.md`: Prometheus metrics, JSON logs, Alloy, Loki, Alertmanager | Keeps monitoring contracts separate from business data |
| Alert email | Host `tvt-alert-dispatcher.service` with authenticated webhooks, PostgreSQL outbox, and TLS SMTP | Preserves delivery/audit state and can report K3s outages without depending on an in-cluster sender |

The gateway product is a proposed default, not a locked dependency. The PoC
must prove supported codecs, reconnect behavior, one-upstream-session fan-out,
latency, and eight-camera load before it is accepted.

## 3. Reference implementation mapping

| `k3s-prototype` area | TVT treatment |
|---|---|
| `apexfabric/common/kube.py` | Reuse the minimal in-cluster API client for reporter/controller Pods |
| `apexfabric/node_management/discovery/` | Reuse host hardware discovery; do not confuse it with new TVT camera discovery |
| `apexfabric/node_management/reporter/` | Reuse as the local-node DaemonSet reporter |
| `apexfabric/node_management/status_controller/` | Reuse freshness, identity, readiness, architecture, and device validation |
| `apexfabric/solution_management/validation.py` | Reuse JSON Schema plus semantic validation and add TVT application-profile rules |
| `apexfabric/solution_management/renderer.py` | Reuse deterministic rendering, affinity, probes, configuration mounts, Services, policies, PVCs, apply, and prune; stop rendering the Namespace because the installer owns it |
| `apexfabric/solution_management/catalog.py` | Retain digest resolution; move catalog records into the TVT PostgreSQL database |
| `solution-packs/schema/deployment-bundle.schema.json` | Copy as the initial TVT bundle contract and version changes explicitly |
| `deploy/k8s/apexfabric-foundation.yaml` | Reuse namespace and namespace-scoped reconciler RBAC, then narrow where possible |
| `deploy/k8s/apexfabric-node-management.yaml` | Reuse CRD, reporter, status controller, RBAC, and probes |
| failure, rollout, and rollback tests | Port to TVT and add camera/gateway failure scenarios |
| `apexfabric_controller` enrollment/provisioning RPCs | Omit |
| `edge_seed`, `edge_bundle`, `edge_installer`, `node_bootstrap` | Omit |
| `driver_build_service`, `driver_build_worker`, driver bundle tooling | Omit |
| multi-node connection and approval UI | Omit |

The code should be copied or extracted with its existing tests before TVT
changes are added. A large rewrite of the K3s plane is not part of this plan.

## 4. Runtime topology

```mermaid
flowchart TB
    Operator[Operator browser]
    Cameras[Camera LAN<br/>5 initially, up to 8]

    subgraph Host[Single Linux server]
        Edge[TVT edge-management service<br/>Python + React assets]
        Dispatcher[TVT alert-dispatcher<br/>systemd service]
        DB[(Host PostgreSQL)]
        Key[Host credential key]
        K3sSvc[k3s.service]
        Watchdog[tvt-k3s-watchdog.timer]
        Registry[Local OCI registry]

        subgraph K3s[Single-node K3s]
            API[Kubernetes API and scheduler]
            Reporter[Node reporter DaemonSet]
            Status[Node-status controller]
            CameraObjects[Camera ConfigMap + Secret]
            Gateway[Stream gateway + config adapter]
            Face[Face-recognition workload]
            ANPR[ANPR workload]
            Presence[Inside/outside workload]
            Reporting[Reporting workloads]
            Observability[Prometheus, Alertmanager,<br/>Grafana, Loki, Alloy]
        end
    end

    SMTP[Organization SMTP relay]

    Operator --> Edge
    Edge <--> DB
    Edge --> Key
    Edge -->|discover and validate| Cameras
    Edge -->|scoped reconcile| CameraObjects
    Edge -->|bundle reconcile and health reads| API
    Registry -->|image pull| K3s
    Reporter --> API
    API --> Status
    CameraObjects --> Gateway
    Cameras -->|one upstream pull per enabled camera| Gateway
    Gateway -->|stable internal RTSP paths| Face
    Gateway -->|stable internal RTSP paths| ANPR
    Gateway -->|stable internal RTSP paths| Presence
    Face --> Reporting
    ANPR --> Reporting
    Presence --> Reporting
    Observability -->|firing and resolved webhook| Dispatcher
    Edge -->|independent host alerts| Dispatcher
    Dispatcher <--> DB
    Dispatcher -->|TLS email| SMTP
    Watchdog --> K3sSvc
```

The host service and K3s are peers. Existing inference continues from the last
applied camera configuration if the host service stops. The UI and camera
inventory remain available if K3s stops.

## 5. Proposed repository layout

```text
tvt-prototype/
  pyproject.toml
  alembic.ini
  tvt_edge/
    api/
      app.py
      cameras.py
      alerts.py
      cluster.py
      workloads.py
      middleware.py
    camera/
      discovery.py
      identity.py
      onvif.py
      rtsp_probe.py
      state_machine.py
    cluster/
      camera_sync.py
      health.py
      kube.py
    db/
      models.py
      repositories.py
      migrations/
    observability/
      logging.py
      metrics.py
    alerting/
      receiver.py
      policy.py
      outbox.py
      emergency_spool.py
      email_sender.py
      templates/
    runtime.py
    settings.py
  apexfabric/
    common/
    node_management/
    solution_management/
  ui/
    src/
  deploy/
    host/
    systemd/
    k8s/
    monitoring/
  solution-packs/
    schema/
    platform/
    face/
    anpr/
    attendance/
    reporting/
  tests/
    unit/
    integration/
    acceptance/
    fixtures/
  scripts/
```

The `apexfabric` package initially stays close to the reference tree so that
upstream differences remain reviewable. TVT-specific behavior belongs under
`tvt_edge`, not inside remote provisioning modules.

## 6. Host edge-management service

### 6.1 Process model

One process owns these async loops:

- HTTP API and static React assets;
- scheduled camera discovery;
- explicit and scheduled RTSP validation;
- camera-to-K3s synchronization;
- host and K3s health aggregation; and
- alert-state reads for the UI.

Alert webhook ingestion and email delivery run in the separate host
`tvt-alert-dispatcher.service` described below. This lets alert delivery remain
available when K3s is unhealthy without coupling SMTP retries to the UI/API
process.

Only one Uvicorn worker is used in V1. This avoids duplicate scanners and
reconcilers. Slow or potentially stuck RTSP operations run in short-lived child
processes, not on the API event loop. [PyAV exposes separate open/read timeout
inputs](https://pyav.org/docs/develop/api/_globals.html), but the parent-process
deadline remains the final containment boundary.
`systemd` uses `Restart=on-failure`, a bounded restart delay, and a watchdog
heartbeat.

If load later requires multiple API workers, background loops must first move
to a separate `tvt-camera-manager.service` or use PostgreSQL advisory locks.

### 6.2 Configuration

Root-owned `/etc/tvt/edge.yaml` contains only non-secret settings:

```yaml
site_id: tvt-plant-01
listen: 127.0.0.1:8088
camera_interfaces: [enp2s0]
camera_subnets: [192.168.20.0/24]
rtsp_ports: [554, 8554]
discovery_interval_seconds: 900
validation_interval_seconds: 60
max_discovery_concurrency: 4
max_validation_concurrency: 2
kubeconfig: /etc/tvt/kubeconfig
camera_sync_namespace: apexfabric
```

Database credentials use a protected environment file or PostgreSQL peer
authentication. The camera encryption key is not placed in this YAML.

### 6.3 Startup sequence

1. Parse and strictly validate configuration.
2. Verify the credential-key owner, permissions, and supported version.
3. Connect to PostgreSQL and verify the Alembic schema revision.
4. Start the UI/API even if K3s is unavailable.
5. Resume incomplete discovery, validation, and sync records idempotently.
6. Run an immediate bounded discovery cycle.
7. Query K3s health and retry pending camera synchronization.

A missing database or credential key is a management-plane startup failure. A
missing K3s API is a degraded state, not an edge-service startup failure.

## 7. Camera discovery and RTSP validation

### 7.1 Discovery algorithm

Each discovery run has an `operation_id` and follows this order:

1. Send ONVIF WS-Discovery probes only on configured interfaces.
2. Normalize returned endpoint UUIDs and service addresses.
3. Read the local neighbor table for candidates inside configured subnets.
4. Probe configured RTSP and management ports with bounded TCP connects.
5. Upsert observations using the identity rules below.
6. For anonymous cameras, query ONVIF media profiles immediately.
7. For protected cameras, set `needs_credentials` without guessing passwords.
8. Queue RTSP validation only when a candidate has a usable path/profile.

The initial defaults are a 15-minute scheduled scan, concurrency four, a
one-second TCP timeout, and per-host backoff. An operator-triggered run uses the
same limits. The service never scans outside its subnet allowlist.

### 7.2 Camera identity

The immutable internal ID is a generated DNS-safe value such as `camera-01`.
Evidence is matched in this order:

1. normalized ONVIF endpoint UUID;
2. normalized MAC address observed on the local LAN;
3. existing address plus vendor/model evidence;
4. otherwise create a new provisional camera record.

An IP address is not a primary identity. A merge caused by weaker evidence is
recorded in the audit log and must never overwrite a conflicting strong
identity automatically.

### 7.3 Validation algorithm

Validation runs in a child process with a hard overall deadline:

1. Resolve the current camera address.
2. Open the configured RTSP port.
3. Perform RTSP `OPTIONS` and `DESCRIBE`.
4. Select the configured video track and transport (`tcp` by default).
5. Receive packets for a bounded interval.
6. Decode at least one keyframe when the codec is supported.
7. Return codec, dimensions, observed FPS, transport, and categorized result.

Suggested deadlines are 3 seconds for TCP connect, 5 seconds for RTSP
negotiation, 10 seconds for media, and 20 seconds overall. They are deployment
settings and must be tuned with real cameras.

Stable failure codes are:

```text
NETWORK_TIMEOUT
CONNECTION_REFUSED
DNS_FAILED
RTSP_AUTH_FAILED
RTSP_PATH_NOT_FOUND
RTSP_NEGOTIATION_FAILED
UNSUPPORTED_CODEC
MEDIA_TIMEOUT
DECODE_FAILED
PROBE_INTERNAL_ERROR
```

For enabled cameras, continuous media health comes from the gateway. The host
does not maintain a second continuous RTSP session. It performs a direct probe
only during onboarding, explicit revalidation, or when the gateway is
unavailable and diagnosis is required.

### 7.4 Camera state machine

```mermaid
stateDiagram-v2
    [*] --> discovered
    discovered --> needs_credentials: authentication required
    discovered --> validating: anonymous/profile known
    needs_credentials --> validating: credentials and profile saved
    validating --> online: packets and keyframe valid
    validating --> invalid: categorized validation failure
    invalid --> validating: retry or configuration change
    online --> offline: gateway media stale
    offline --> online: gateway media fresh
    online --> disabled: operator disables
    offline --> disabled: operator disables
    invalid --> disabled: operator disables
    disabled --> validating: operator enables
```

`enabled` is an operator intent, while `online` is an observation. An enabled
camera may be offline; disabling it removes it from the next K3s camera-set
revision.

## 8. PostgreSQL model

All timestamps are UTC `timestamptz`. IDs exposed to APIs are UUIDs or stable
DNS-safe domain IDs. Secret material is never included in generic ORM string
representations.

| Table | Important fields and constraints |
|---|---|
| `cameras` | `id UUID PK`, `camera_id UNIQUE`, `friendly_name`, `site_role`, `direction`, `onvif_uuid UNIQUE NULL`, `mac_address UNIQUE NULL`, `vendor`, `model`, `state`, `enabled`, `created_at`, `updated_at` |
| `camera_addresses` | `id`, `camera_id FK`, `ip inet`, `interface`, `first_seen_at`, `last_seen_at`, `is_current`; unique active address per camera |
| `camera_streams` | `id`, `camera_id FK`, `profile_token`, `rtsp_port`, `path`, `transport`, `codec`, `width`, `height`, `fps`, `selected`, `updated_at`; one selected stream per camera |
| `camera_credentials` | `camera_id PK/FK`, one encrypted credential-document `ciphertext`, unique `nonce`, `key_version`, `updated_at`; no plaintext columns and no nonce reuse across encryptions |
| `camera_observations` | `id`, `camera_id FK NULL`, `operation_id`, `method`, `address`, `result_code`, bounded JSON metadata, `observed_at`; time-based retention |
| `camera_health` | `camera_id PK/FK`, `validation_code`, `last_validated_at`, `last_packet_at`, `last_keyframe_at`, `gateway_up`, `consecutive_failures`, `next_retry_at` |
| `camera_sync_state` | singleton/site row containing `desired_revision`, `applied_revision`, `status`, `last_attempt_at`, `last_error` |
| `alert_instances` | stable fingerprint, source, severity, state, first/last seen, occurrence count, acknowledgement fields, resolved time, and last Alertmanager group key |
| `alert_transitions` | alert FK, transition type, source timestamp, received timestamp, redacted payload, and unique idempotency key |
| `notification_policies` | severity/alert matchers, recipients or recipient-group reference, initial/repeat interval, resolved-email policy, enabled state |
| `notification_outbox` | alert transition FK, notification type, deterministic message ID, state, attempt count, next attempt, sent/expired timestamps |
| `notification_attempts` | outbox FK, attempt number, start/end time, redacted SMTP result code, and bounded error category |
| `audit_events` | actor, operation ID, action, target type/ID, result, redacted details, timestamp; append-only through the application role |
| `solution_revisions` | deployment ID, canonical bundle, SHA-256 revision, lifecycle status, actor, timestamps; no secret values |

Recommended indexes cover camera state, enabled cameras, current addresses,
pending validation, pending synchronization, active alerts, due outbox rows,
and recent audit events. Observation, alert-transition, delivery-attempt, and
audit retention are bounded by scheduled database jobs.

The transaction which changes camera configuration also increments the desired
camera-set revision and marks synchronization pending. A background loop claims
pending work with `SELECT ... FOR UPDATE SKIP LOCKED` so a future process split
does not require a data-model rewrite.

## 9. Host API contract

All responses include `X-Request-ID`. Mutation endpoints append an audit event.
Passwords are write-only and are represented in reads only by
`credentials_configured: true|false`.

| Method and route | Purpose |
|---|---|
| `GET /api/v1/health` | Independent host, database, K3s API, Node, gateway, and workload health |
| `GET /api/v1/cameras` | List cameras, configuration state, and last-known health |
| `GET /api/v1/cameras/{camera_id}` | Detailed non-secret camera record and observations |
| `POST /api/v1/discovery-runs` | Start a bounded discovery run; returns `operation_id` |
| `GET /api/v1/discovery-runs/{operation_id}` | Read progress and categorized results |
| `PATCH /api/v1/cameras/{camera_id}` | Set name, physical role, direction, selected profile, transport, or enabled intent |
| `PUT /api/v1/cameras/{camera_id}/credentials` | Replace write-only camera credentials |
| `POST /api/v1/cameras/{camera_id}/validate` | Queue immediate authenticated RTSP validation |
| `GET /api/v1/alerts` | List active/recent alerts |
| `POST /api/v1/alerts/{alert_id}/acknowledge` | Record local acknowledgement |
| `GET /api/v1/alerts/{alert_id}/notifications` | Show redacted email delivery state and attempts |
| `GET /api/v1/cluster` | Return bounded K3s Node, Deployment, Pod, and synchronization summaries |
| `GET /api/v1/solutions` | List catalog entries and deployed revisions |
| `POST /api/v1/solutions/{solution_id}/apply` | Validate and reconcile a selected bundle |
| `POST /api/v1/solutions/{deployment_id}/stop` | Persist desired state and reconcile replicas to zero |
| `POST /api/v1/solutions/{deployment_id}/start` | Restore declared replicas and reconcile |

The API does not expose arbitrary `kubectl`, shell, log-path, file-read, or
systemd operations. Workload diagnostics are allowlisted by ownership label,
namespace, maximum output size, and timeout.

The non-public alert receiver is
`POST /internal/v1/alerts/alertmanager`. It binds only to the host address
reachable from the Pod network, requires a dedicated bearer credential, limits
payload size and alert count, and accepts no operator-supplied command or
template.

## 10. Camera synchronization into K3s

The installer creates these fixed objects before dropping privileges:

- `ConfigMap/tvt-camera-sources`;
- `Secret/tvt-camera-credentials`;
- a service account and kubeconfig limited to `get`, `patch`, and `update` for
  those named objects; and
- read-only permissions for the required Nodes, Deployments, Pods, Services,
  Events, and `ApexNodeStatus` objects.

The ConfigMap contains a canonical JSON document with non-secret fields:

```json
{
  "schema_version": "1.0",
  "revision": 12,
  "cameras": [
    {
      "camera_id": "camera-01",
      "role": "main-entrance",
      "direction": "entry",
      "secret_file": "/run/secrets/tvt/camera-01.rtsp",
      "internal_path": "camera-01",
      "transport": "tcp"
    }
  ]
}
```

The Secret contains the same revision and one complete RTSP URL per camera.
Keys are `<camera_id>.rtsp`. Credential-bearing URLs are never placed in the
ConfigMap, bundle, Pod environment, annotation, Event, API response, or log.

The two-object update is not atomic. Both objects therefore carry the desired
revision. The gateway adapter retains its last-good in-memory configuration
until both projected files have the same revision and the new complete set
validates. A mismatch produces a metric and alert; it never partially applies
a new camera set. Both objects are mounted as directories, not with Kubernetes
`subPath`, so projected updates can become visible without Pod replacement.

Synchronization is idempotent:

```mermaid
sequenceDiagram
    participant DB as PostgreSQL
    participant Edge as Edge service
    participant API as Kubernetes API
    participant Adapter as Gateway adapter
    participant Gateway as Stream gateway

    Edge->>DB: Claim pending desired revision N
    Edge->>API: Patch Secret with revision N
    Edge->>API: Patch ConfigMap with revision N
    Edge->>DB: Record applied revision N
    Adapter->>Adapter: Wait for matching projected revisions
    Adapter->>Adapter: Validate complete camera set
    Adapter->>Gateway: Atomically activate revision N
    Gateway-->>Adapter: Per-camera media status
```

`applied_revision` means accepted by the Kubernetes API, not active in the
gateway. Gateway metrics expose `tvt_gateway_config_revision`, and health
aggregation reports the difference.

## 11. Stream gateway contract

The gateway Deployment contains:

- one MediaMTX container;
- one unprivileged TVT adapter container which reads projected camera files,
  configures MediaMTX through loopback, and exposes product metrics/health;
- read-only camera ConfigMap and Secret volumes;
- a writable `emptyDir` for generated runtime configuration only;
- a ClusterIP Service named `tvt-stream-gateway`; and
- startup, liveness, and readiness probes.

For each enabled camera, there is exactly one logical upstream source and a
stable internal path equal to `camera_id`. Multiple CV consumers attach to the
internal path. The acceptance test must verify from camera/gateway connection
metrics that adding consumers does not add upstream sessions.

This matches [MediaMTX's documented path
model](https://mediamtx.org/docs/features/architecture): one path is fed by one
publisher or external source and broadcast to readers. Its documented
RTSP-source, Control API, metrics, and hot-reload capabilities make it a
reasonable PoC candidate, but the TVT load test remains authoritative for
acceptance.

Probe meanings are:

- startup: adapter has loaded a syntactically valid matching revision and the
  gateway API is responsive;
- liveness: both adapter and gateway processes respond; camera outages do not
  fail liveness;
- readiness: the gateway can serve consumers and at least the configured
  readiness policy is met;
- per-camera availability: separate metric/status, never collapsed into Pod
  liveness.

Recommended internal endpoints are:

```text
RTSP  rtsp://tvt-stream-gateway.apexfabric.svc:8554/<camera_id>
HTTP  /healthz
HTTP  /readyz
HTTP  /metrics
HTTP  /api/v1/streams
```

The NetworkPolicy allows gateway egress only to configured camera subnets and
DNS where needed. CV namespaces/labels may reach the internal RTSP port; they
cannot reach physical camera subnets directly. The Pod receives no Kubernetes
API token.

If the MediaMTX PoC fails a required codec, latency, or resource criterion, the
same adapter contract can manage GStreamer restream pipelines instead. The
camera database and CV endpoint contract do not change.

## 12. K3s platform behavior

### 12.1 Installation

Port the reference server installer while removing all remote-worker paths. It
must:

1. validate the supported Ubuntu/kernel/hardware profile;
2. install a pinned K3s version and configure the local registry mirror;
3. create the `apexfabric` namespace and least-privilege RBAC;
4. publish or import pinned multi-container platform images;
5. apply the `ApexNodeStatus` CRD, reporter, and status controller;
6. label only the local node as reporter-enabled and with its configured
   hardware profile;
7. apply the stream gateway and camera sync objects;
8. install the monitoring stack with bounded resources; and
9. install and enable host PostgreSQL, edge service, and watchdog units.

No agent join token, approval state, enrollment certificate, driver bundle, or
remote install transaction exists in this sequence.

### 12.2 Node reporting and qualification

Every 30 seconds, the reporter reads mounted host information and patches its
`ApexNodeStatus`. The status controller accepts a report only when:

- `spec.nodeName` matches the Kubernetes Node;
- the observation is fresh;
- the Node is Ready;
- discovered architecture matches the Node label; and
- the configured hardware profile's required decoder/accelerator devices are
  usable.

The controller alone writes `apexfabric.com/qualified` and related capability
labels. The local node is not allowed to self-assert qualification merely
because it is the only node.

Camera IDs are not Node labels. The node reporter may advertise a statically
qualified `apexfabric.com/camera-streams` capacity from the hardware profile.
Each CV application requests capacity for every stream it independently
decodes, so this resource represents decode/load budget, not physical-camera
count.

### 12.3 Solution lifecycle

The host service loads a bundle from the catalog and runs the same stages as
the reference implementation:

1. JSON Schema validation;
2. semantic validation;
3. resolution of mutable image tags to registry digests;
4. canonical SHA-256 desired-state revision;
5. preview rendering;
6. server-side apply with a stable field manager;
7. pruning of obsolete managed Deployments, ConfigMaps, Secrets, Services, and
   NetworkPolicies;
8. deliberate retention of PVCs; and
9. rollout observation and audit.

The renderer emits Deployments, Services, ConfigMaps, Secret references, PVCs,
NetworkPolicies, resource requests, affinity, probes, telemetry annotations,
security contexts, and termination grace periods. It does not assign
`nodeName`; the default scheduler selects the qualified local Node. Unlike the
reference renderer, the TVT renderer does not emit a Namespace object. Namespace
creation is an installer responsibility and remains outside the namespace-
scoped reconciliation credential.

Network-camera applications use:

```yaml
camera_contract:
  configuration_owner: platform
  transport: rtsp
  required_for_readiness: true
  scheduling_mode: runtime-connectivity
placement:
  requires_qualified_node: true
  requires_camera_labels: false
```

Camera credentials are references to gateway endpoints, not direct physical
camera URLs. A CV application receives only the camera IDs assigned to it.

### 12.4 TVT Solution Packs

The platform contract should support independent bundles for:

- stream gateway;
- face recognition;
- face enrollment API/UI integration;
- ANPR;
- inside/outside event correlation and attendance;
- vehicle entry/exit aggregation; and
- scheduled report generation and email delivery.

Not all functions need one Pod each. The bundle boundary should follow
independent release, scaling, hardware, and persistence needs. All CV images
must expose health, readiness, Prometheus metrics, and a versioned event
contract.

Face enrollment, attendance, vehicle history, and daily reports require a
durable application data design. The host camera-inventory PostgreSQL database
must not silently become the business/evidence database. Until retention,
privacy, backup/restore, schema migration, and deletion requirements are
approved, those workloads are integration stubs rather than restart-safe
product features.

## 13. Camera assignment

The initial logical inventory is:

| Camera | Site role | Direction | Expected consumers |
|---|---|---|---|
| `camera-01` | Main entrance | Entry | Face recognition, presence/attendance |
| `camera-02` | Main entrance | Exit | Face recognition, presence/attendance |
| `camera-03` | Plant entrance | Entry | Face recognition, presence/attendance |
| `camera-04` | Plant entrance | Exit | Face recognition, presence/attendance |
| `camera-05` | Back exit | Exit | Face recognition, presence/attendance |

ANPR camera assignment remains unconfirmed. The current README lists ANPR but
does not identify a vehicle-lane camera or say whether one of these five views
has suitable plate angle, resolution, illumination, and shutter behavior.

Camera-to-use-case assignment is desired state stored outside application
images. A camera may feed several CV workloads through the gateway, but each
physical camera still has only one gateway upstream session.

## 14. Observability and error handling

`MONITORING.md` is the detailed contract. Implementation must include:

- `prometheus_client` metrics with bounded camera/use-case/error labels;
- one JSON object per stdout line;
- request, operation, event, and stream-session correlation IDs in logs, not
  metric labels;
- Alloy collection of Pod logs and the edge-service journal into Loki;
- individual Pod endpoint scraping through `ServiceMonitor`/`PodMonitor`;
- Alertmanager firing and resolved notifications to an authenticated host
  webhook;
- active/acknowledged/cleared alert state in PostgreSQL; and
- dashboards for server, camera, gateway, CV workload, and errors.

Neither metrics nor logs may contain camera credentials, direct RTSP URLs,
faces, embeddings, person names, or number plates. Business events require a
separate versioned and access-controlled data path.

## 15. Alert-dispatcher service

### 15.1 Purpose and boundary

The alert dispatcher is a host `systemd` service, separate from K3s and the
edge-management API. It converts actionable alert state into durable,
auditable email delivery. It does not evaluate PromQL, query arbitrary logs,
restart services, execute runbooks, or decide whether a raw exception is
important.

Alertmanager remains responsible for grouping, inhibition, deduplication, and
initial webhook timing. The dispatcher receives Alertmanager's firing and
resolved webhooks, stores alert state, applies site email/reminder policy, and
delivers mail through the organization SMTP relay. Alertmanager documents its
routing and timing controls, including `group_wait`, `group_interval`,
`repeat_interval`, and `send_resolved`, in its [configuration
reference](https://prometheus.io/docs/alerting/latest/configuration/).

The dispatcher, rather than Alertmanager, owns email reminder intervals because
it also owns acknowledgement. Alertmanager uses a long repeat interval as a
state refresh/safety net; repeated firing webhooks update last-seen state but do
not themselves create another email. This prevents two independent repeat
schedulers from producing duplicate reminders.

The dispatcher is used instead of Alertmanager's direct SMTP receiver because
TVT requires durable delivery history, UI acknowledgement, retry visibility,
host-originated K3s-down alerts, and recipient policy in one place. A simpler
deployment may use direct Alertmanager email only if those requirements are
explicitly dropped.

### 15.2 Input paths

There are two trusted alert sources:

```text
In-cluster condition
  -> application metric
  -> PrometheusRule with a `for` duration
  -> Alertmanager group/inhibition
  -> authenticated dispatcher webhook

Host/control-plane condition
  -> edge-management host check
  -> permission-restricted dispatcher Unix socket
```

The second path allows an email when K3s, Prometheus, or Alertmanager is down.
Both sources use the same normalized alert schema and persistence path.

Raw `warning` and `error` log lines do not directly produce email. The code
path for an actionable error increments a bounded metric and writes a
correlated JSON log; a Prometheus rule evaluates the symptom. A Loki-derived
alert is permitted only when the condition cannot reasonably be represented by
a metric, and it must enter Alertmanager through the same labels and routing
policy. This follows the Prometheus recommendation to alert on actionable
symptoms and keep alert counts small rather than notifying on every possible
cause. See [Prometheus alerting
practices](https://prometheus.io/docs/practices/alerting/).

### 15.3 Normalized alert contract

Every accepted alert contains:

```json
{
  "schema_version": "1.0",
  "source": "alertmanager",
  "status": "firing",
  "starts_at": "2026-09-01T08:30:00Z",
  "ends_at": null,
  "labels": {
    "site_id": "tvt-plant-01",
    "alertname": "CameraMediaMissing",
    "severity": "critical",
    "service": "stream-gateway",
    "camera_id": "camera-03"
  },
  "annotations": {
    "summary": "Camera media has been missing for two minutes",
    "runbook_url": "https://operations.example/runbooks/camera-media-missing"
  }
}
```

Allowed label names and bounded values are validated. Annotations are length-
limited and treated as untrusted display text. Unknown fields, invalid
timestamps, excessive alerts, and credential-bearing values are rejected or
redacted before persistence.

The stable fingerprint is calculated from:

```text
site_id + alertname + service + camera_id + use_case
```

Empty optional values have a canonical representation. Pod name, container ID,
request ID, exception text, IP address, and timestamp are excluded so restarts
and repeated evaluations do not create new logical incidents.

### 15.4 State and idempotency

The dispatcher applies this state model:

```mermaid
stateDiagram-v2
    [*] --> active: first firing event
    active --> active: repeated/updated firing event
    active --> acknowledged: operator acknowledges
    acknowledged --> acknowledged: repeated firing event
    active --> resolved: resolved event
    acknowledged --> resolved: resolved event
    resolved --> active: later firing with a new start time
```

Receiving a webhook starts one database transaction which:

1. upserts the logical alert instance by fingerprint;
2. inserts an idempotent transition record;
3. evaluates the matching notification policy;
4. creates an outbox row when email is required; and
5. commits before returning HTTP success.

The transition idempotency key includes source, fingerprint, occurrence
`starts_at`, transition status, and `ends_at` for resolution. Duplicate or
retried webhooks update last-seen state but do not enqueue another transition
email.

If PostgreSQL is unavailable, the Alertmanager receiver returns a retryable
failure. An allowlisted host-generated `PostgreSQLUnavailable` alert instead
uses a small, bounded emergency filesystem spool so the database can report its
own outage. The spool accepts only fixed internal alert types over the Unix
socket, uses atomic write/rename, and is imported into PostgreSQL after
recovery. If SMTP is unavailable, the receiver still returns success after the
alert and normal outbox row are committed; the email worker retries
independently. Acknowledgement suppresses future reminder emails but does not
clear the alert and does not suppress its recovery email.

### 15.5 Routing and notification policy

Initial values are tuning defaults, not final SLOs:

| Severity | Rule/route behavior | Email behavior |
|---|---|---|
| `critical` | Usually require a 2-minute sustained condition; Alertmanager `group_wait` 30 seconds | Send immediately after grouping; repeat every 30 minutes while active and unacknowledged |
| `warning` | Usually require 5-10 minutes; group for 5 minutes | Send once; repeat every 4 hours while active and unacknowledged |
| `info` | Record and display | No immediate email; optional daily digest |

Only alerts with a configured policy produce email. Recipient groups are
site-level configuration, not alert labels supplied by workloads.

Alertmanager's `repeat_interval` is initially 24 hours and acts only as a state
refresh. Dispatcher-generated reminder jobs use the email intervals in the
table. Receiving the 24-hour firing refresh does not reset acknowledgement or
enqueue an extra transition email.

Required inhibition rules include:

- Node or K3s down inhibits gateway, Pod, and CV workload alerts;
- stream gateway down inhibits per-camera inference-stalled alerts;
- a camera-offline alert inhibits source/inference alerts for that camera; and
- PostgreSQL down inhibits secondary edge-API database symptoms.

A resolved email is sent only if a firing email for that alert occurrence was
successfully delivered. Alertmanager email defaults do not imply this policy;
the dispatcher implements it explicitly from its delivery records.

### 15.6 Email outbox and SMTP delivery

The worker claims due outbox rows using `SELECT ... FOR UPDATE SKIP LOCKED`,
renders a versioned local template, and submits mail over certificate-verified
TLS to the configured SMTP relay. It never accepts templates, recipients,
headers, or SMTP settings from the alert payload.

The emergency filesystem spool is used only for fixed host-infrastructure
alerts when PostgreSQL cannot accept the normal transaction. It has strict file
count/byte limits, contains the same redacted normalized fields, and is not a
general replacement queue for Alertmanager webhooks.

Suggested retry intervals are:

```text
1 minute -> 2 minutes -> 5 minutes -> 10 minutes -> 30 minutes -> hourly
```

Each attempt has connection, command, and overall deadlines. Permanent SMTP
responses mark the notification failed; transient responses schedule the next
attempt. A configured maximum delivery age expires undeliverable mail while
retaining redacted evidence for the UI.

Delivery is at-least-once. An ambiguous network failure after SMTP accepts a
message can cause a duplicate. Each transition therefore uses a deterministic
`Message-ID` and alert thread key so mail clients can group repeats and recovery
messages.

Email content is limited to:

- site ID, severity, alert name, component, and stable camera ID;
- safe summary, start time, duration, and current state;
- dashboard and runbook links; and
- acknowledgement link only when the management UI has an approved reachable
  address and authentication boundary.

Email must not include RTSP URLs, credentials, faces, embeddings, person names,
number plates, Kubernetes Secrets, arbitrary log bodies, or raw stack traces.

### 15.7 Availability and self-monitoring

The dispatcher exposes `/healthz`, `/readyz`, and `/metrics` on a host-local
endpoint. Metrics include accepted/rejected events, active alerts, outbox
depth/age, delivery attempts by bounded result, last successful delivery time,
and webhook/SMTP duration.

The edge-management service checks the dispatcher and displays degraded email
delivery in the UI. Prometheus scrapes it when K3s is healthy. A scheduled
synthetic alert exercises Prometheus, Alertmanager, webhook persistence, and
SMTP submission without claiming that a human mailbox read the message.

The local dispatcher cannot report complete server, power, disk, host-network,
or site-WAN failure after the host is unreachable. Detecting that failure
requires an external heartbeat or central monitoring receiver. SMTP delivery
also requires a working outbound network path; queued mail resumes when that
path returns.

## 16. Recovery behavior

| Failure | Detection | V1 response |
|---|---|---|
| Discovery/validation child hangs | Parent deadline | Kill child, categorize timeout, back off, keep API responsive |
| One camera disappears | Gateway media age and host observations | Mark camera offline, alert, reconnect with jitter; other pipelines remain active |
| Gateway process exits | Kubelet liveness/Deployment | Restart container or replace Pod; rebuild all paths from mounted desired state |
| CV process wedges | Liveness probe | Kubelet restarts container |
| CV is alive but cannot serve | Readiness probe | Remove Pod from Service endpoints without destroying diagnostics |
| Edge service exits | `systemd` | Restart; K3s continues with last active camera revision |
| Alert dispatcher exits | `systemd` | Restart; Alertmanager retries failed webhook deliveries and committed outbox work resumes |
| PostgreSQL exits | `systemd` and host check | Restart; UI is degraded; an allowlisted host alert uses the bounded emergency spool; K3s continues |
| SMTP relay/WAN is unavailable | Dispatcher delivery result | Retain committed outbox rows and retry with bounded backoff; show oldest pending age in UI |
| K3s process exits | `systemd` | Restart and reconcile Deployments |
| K3s API stays unhealthy | Root-owned fixed watchdog | One controlled restart after sustained failure, then cooldown and alert |
| Host reboots | `systemd` ordering and declarative state | Restore database, edge API, K3s, gateway, and CV workloads |
| Complete host/power/disk failure | Future off-box heartbeat | All local functions stop; not HA |

The watchdog accepts no API-supplied command or argument. Initial timing is the
HLD policy: check every 30 seconds, tolerate at least two minutes, restart once,
then wait ten minutes before another action.

## 17. Security boundaries

- Bind the prototype UI to loopback until authentication and CSRF protection
  are implemented. Exposure to a management LAN is a separate review gate.
- Run the edge service as an unprivileged `tvt` user.
- Store the AES-GCM master key at `/etc/tvt/credential-keys/v1.key`, owned by
  `root:tvt`, mode `0640`; support explicit key versions and rotation.
- Give the edge kubeconfig named-object camera update rights and bounded
  read-only status rights only.
- Precreate fixed camera objects because Kubernetes RBAC cannot restrict a
  general `create` verb to future object names using `resourceNames`.
- Mount only gateway-internal RTSP credentials into the gateway. CV Pods receive
  no physical-camera password.
- Disable service-account token automount for gateway and CV Pods.
- Use non-root UIDs, read-only roots, dropped Linux capabilities, RuntimeDefault
  seccomp, and explicit writable mounts unless an approved accelerator profile
  temporarily requires more privilege.
- Pin runtime images by digest. Add signature/SBOM/admission enforcement before
  production release.
- Keep PostgreSQL on a Unix socket or loopback with separate migration and
  application roles.
- Bind the dispatcher webhook only to the host address needed by the Pod
  network, authenticate it with a dedicated rotated credential, and enforce
  request-size, alert-count, field, and timeout limits.
- Store SMTP credentials in a protected host credential file or approved secret
  store. Require certificate-verified TLS, restrict sender/recipient domains,
  and prohibit plaintext fallback.
- Own the host-alert Unix socket and emergency spool as `tvt-alert:tvt`, deny
  access to other users, set hard file/byte limits, and reject non-allowlisted
  emergency alert types.
- Redact secrets before logging; collector-side redaction is only a secondary
  safeguard.
- Treat faces, embeddings, attendance, and plates as sensitive business data
  governed outside the camera-inventory schema.

## 18. Verification plan

### 18.1 Unit tests

- camera identity normalization, matching, conflict, and IP-change behavior;
- state-machine transitions and invalid-transition rejection;
- subnet/interface allowlist enforcement;
- ONVIF and RTSP response parsing;
- validation timeout and error categorization;
- AES-GCM encrypt/decrypt, tamper rejection, key versioning, and redaction;
- API schema and authorization behavior;
- camera revision generation and idempotent synchronization;
- DeploymentBundle schema and semantic validation;
- deterministic rendering, placement, probe, Secret-mount, prune, and PVC
  retention behavior;
- reporter freshness and status-controller label ownership; and
- bounded metric labels and structured log fields;
- alert fingerprint normalization and collision cases;
- notification policy matching, inhibition inputs, acknowledgement, and
  resolved-email rules;
- webhook and host-alert idempotency; and
- outbox retry classification, expiry, and deterministic message IDs.

### 18.2 Integration tests

Use containerized fake ONVIF/RTSP devices and recorded non-sensitive video:

- discover an anonymous camera;
- discover a protected camera and enter credentials;
- reject incorrect credentials and invalid paths;
- validate H.264/H.265 support as required;
- preserve identity after an address change;
- converge PostgreSQL revision to matching K3s objects;
- retain last-good gateway configuration during a revision mismatch;
- add two internal consumers while preserving one upstream session;
- disconnect one source without affecting the others;
- restart the edge service, PostgreSQL, gateway Pod, and K3s; and
- process duplicate/out-of-order firing and resolved webhooks;
- queue email while a fake SMTP relay is down and deliver it after recovery;
- stop PostgreSQL, queue only the allowlisted emergency database alert on the
  filesystem, and import its delivery record after database recovery;
- verify secrets never appear in APIs, logs, metrics, Events, or rendered
  bundles.

### 18.3 K3s acceptance tests

Port the reference lifecycle suite and retain its concrete assertions:

- liveness failure increases container restart count without changing Pod UID;
- Pod deletion produces a replacement UID;
- readiness failure removes the endpoint while the Pod remains running;
- impossible resources leave a Pod Pending with a scheduler reason;
- a healthy revision rolls out and preserves external configuration;
- an unhealthy revision never replaces the healthy endpoint set; and
- an explicit rollback restores the recorded healthy revision.

Add TVT assertions for camera ConfigMap/Secret revision matching, gateway
session count, per-camera reconnect, alert delivery, and host-UI availability
while K3s is stopped.

### 18.4 Capacity and field acceptance

With all intended cameras and CV consumers active for a sustained run, record:

- input codec, resolution, configured/observed FPS, and bitrate;
- upstream and downstream session counts;
- decode sessions and frame-drop ratio;
- CPU, memory, GPU/NPU, disk, network, and temperature;
- discovery and reconnect load on cameras and the LAN;
- inference latency and throughput by use case;
- recovery times for camera, Pod, K3s, database, service, and host restart; and
- monitoring disk growth and retention behavior.

Five-camera success does not establish the eight-camera design ceiling unless
the eight-camera workload is actually measured.

### 18.5 Alert delivery acceptance

The alert-dispatcher path is accepted when:

1. a sustained camera outage produces one firing email;
2. a transient outage shorter than the rule/route delay produces no email;
3. recovery produces one resolved email only after a firing email was sent;
4. K3s or Alertmanager failure produces a host-originated email;
5. root-cause inhibition prevents downstream notification storms;
6. SMTP failure preserves the notification and retries after restart;
7. duplicate webhooks do not create duplicate logical transitions;
8. acknowledgement stops reminders without clearing the alert or suppressing
   recovery;
9. no credential, RTSP URL, face, person, or plate data appears in email; and
10. a PostgreSQL outage uses only the bounded emergency path and is reported;
    and
11. a synthetic end-to-end alert reaches SMTP and records auditable evidence.

## 19. Implementation plan and review gates

### Phase 0: Freeze contracts

- Confirm camera/LAN/hardware inputs and the open questions below.
- Copy the reference bundle schema and add contract-version tests.
- Record the exact `k3s-prototype` commit used as the baseline.
- Produce a module-by-module reuse/omit diff.

**Exit:** Approved scope, camera source contract, gateway PoC criteria, and
business-data boundary.

### Phase 1: Establish the single-node K3s plane

- Port pinned K3s and local-registry installation.
- Port node reporter, `ApexNodeStatus`, status controller, and RBAC.
- Port bundle validation, rendering, apply/prune, lifecycle, and tests.
- Remove remote enrollment, approval, installer, and driver paths.

**Exit:** A synthetic bundle schedules only on the qualified local Node and
passes reference failure/rollout acceptance tests.

### Phase 2: Build host management foundations

- Create Python service packaging and `systemd` unit.
- Add PostgreSQL models/migrations and repository layer, including alert and
  notification-outbox tables.
- Add configuration, credential encryption, request IDs, JSON logging, and
  base metrics.
- Serve the React shell and independent host/K3s health API.

**Exit:** UI and camera database remain available while K3s is stopped; secret
round-trip and redaction tests pass.

### Phase 3: Implement camera discovery and onboarding

- Add ONVIF, neighbor-table, and TCP discovery.
- Add identity/deduplication and operator camera metadata.
- Add killable RTSP/keyframe validation and the camera state machine.
- Build camera list/detail/edit/validate UI flows.

**Exit:** New cameras are discovered, protected streams can be configured, and
IP changes do not duplicate a strong identity.

### Phase 4: Synchronize cameras and fan out streams

- Install fixed ConfigMap/Secret and scoped kubeconfig.
- Implement revisioned, idempotent sync.
- Complete MediaMTX/GStreamer gateway PoC and select the gateway.
- Implement adapter, probes, metrics, internal paths, and NetworkPolicies.

**Exit:** One physical session feeds two CV consumers, revision mismatch is
safe, and one camera disconnect does not interrupt the others.

### Phase 5: Integrate TVT Solution Packs

- Define application image/event/config contracts.
- Add face, ANPR, presence/attendance, and reporting bundle skeletons.
- Map cameras to use cases without direct physical URLs.
- Validate start, stop, update, failed rollout, and rollback.

**Exit:** Each available application is independently deployable and reports
accurate readiness and telemetry. Durable features remain gated until their
data design is approved.

### Phase 6: Monitoring, recovery, and security acceptance

- Deploy the pinned monitoring stack from `MONITORING.md`.
- Add alert rules, grouping/inhibition routes, and bounded retention.
- Implement `tvt-alert-dispatcher.service`, authenticated receivers, policy
  evaluation, durable outbox, bounded emergency spool, SMTP delivery,
  templates, and UI delivery state.
- Add firing/resolved email, retry, acknowledgement, synthetic-alert, and
  notification-storm acceptance tests.
- Add K3s watchdog and boot/recovery acceptance.
- Complete secret-leak, RBAC, network-policy, image, and host-service review.

**Exit:** The documented acceptance suites and sustained-load test pass with a
stored evidence bundle and recovery measurements.

## 20. Questions for review

These answers are not required to review the overall structure, but they are
required before the associated phase is closed.

1. What are the exact camera makes/models, ONVIF support, codecs, resolutions,
   FPS, authentication modes, and DHCP/static-address behavior?
2. Which configured subnet and host interface may discovery scan? Are cameras
   on a dedicated VLAN?
3. Is compressed cluster-local RTSP fan-out acceptable, even though every CV
   Pod decodes its own copy, or is shared decoded-frame transport a V1
   requirement?
4. Does one of the five listed cameras provide a qualified ANPR view, or is a
   separate vehicle-lane camera expected?
5. What exact server CPU, memory, storage, GPU/NPU/accelerator, and Ubuntu
   version form the supported hardware profile?
6. Are face enrollment, attendance history, vehicle history, and generated
   reports required to survive restart in this prototype? If yes, what are the
   retention, backup, deletion, privacy, and audit requirements?
7. Will the management UI remain loopback-only, or must it be reachable on a
   customer management LAN? If reachable, what identity provider or local-user
   authentication is required?
8. Is an organization SMTP relay reachable from the site, which recipient
   groups receive critical/warning alerts, and what sender domain and
   credential-storage mechanism are approved?
9. What recovery target qualifies as "within a few minutes" for camera
   reconnect, Pod replacement, K3s restart, and full host reboot?
10. Should the local OCI registry and monitoring data survive OS
    reinstallation, and what backup/restore medium is acceptable?

## 21. Definition of the first usable increment

The first usable increment is the platform and camera path, not the completion
of every CV business feature. It is accepted when:

1. the local node is reported and controller-qualified;
2. a camera is discovered without K3s;
3. an operator can configure and validate its authenticated RTSP stream;
4. an enabled camera revision converges into K3s without exposing credentials;
5. the gateway provides a stable internal path and one upstream pull;
6. two synthetic CV Pods consume the same internal stream;
7. camera, Pod, edge-service, PostgreSQL, and K3s recovery behavior is tested;
8. the UI remains available during a K3s outage; and
9. metrics, JSON logs, and alerts identify failures without requiring shell
   access; and
10. one synthetic firing/recovery sequence is durably queued and delivered
    through the approved SMTP relay.

Production face/ANPR accuracy, durable attendance/report data, scheduled
business-report email, and eight-camera capacity are separate acceptance gates
with their own input data and evidence. Scheduled business-report email is not
the alert-dispatcher path.
