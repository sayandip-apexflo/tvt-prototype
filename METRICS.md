# TVT edge-to-central monitoring contract

## 1. Purpose

This document defines the telemetry that a TVT edge deployment sends to the
central monitoring plane. It complements `MONITORING.md` and groups the
contract by transport:

1. Prometheus Remote Write for numeric time-series metrics.
2. A host heartbeat every 30 seconds for independent edge liveness and coarse
   platform state.
3. Loki ingestion for structured operational logs.
4. An edge-event webhook for low-frequency host state transitions that may not
   reach central monitoring through the normal metric path.

The heartbeat, Loki, and edge-event channels do not replace Prometheus Remote
Write. Routine host, camera, workload, inference, API, and resource conditions
are represented as metrics and evaluated by central alerting rules.

## 2. Common identity and data rules

All four channels use outbound HTTPS with mutual TLS and independently scoped
credentials. The ingestion gateway derives the authoritative `customer_id`,
`site_id`, `edge_id`, tenant, and permitted route from the authenticated client
identity. Payload fields and edge-supplied labels must not be allowed to select
another customer, site, edge, or tenant.

Metrics, logs, heartbeats, and events must not contain:

- camera usernames, passwords, credential-bearing RTSP URLs, or camera IP
  addresses;
- Kubernetes tokens, Secret values, telemetry credentials, or email
  credentials;
- face images, embeddings, person names, or identifying face metadata;
- number plates unless separately authorized by an approved data policy;
- arbitrary HTTP request or response bodies; or
- unbounded exception text, raw log bodies, or stack traces outside the
  structured log channel.

Prometheus and Loki are operational telemetry systems, not the authoritative
store for attendance, recognition, ANPR, vehicle, or business-report events.

## 3. Prometheus Remote Write

Edge Prometheus scrapes local application, exporter, Kubernetes, and host
targets. It adds stable external labels and sends samples to the central
metrics receiver using Prometheus Remote Write over mTLS. Native exporter
metric names should be retained where available.

### 3.1 Host and hardware

Standard `node-exporter` metrics:

```text
up
node_boot_time_seconds
node_time_seconds
node_cpu_seconds_total
node_load1
node_load5
node_load15
node_memory_MemTotal_bytes
node_memory_MemAvailable_bytes
node_memory_SwapTotal_bytes
node_memory_SwapFree_bytes
node_filesystem_size_bytes
node_filesystem_avail_bytes
node_filesystem_files_free
node_disk_read_bytes_total
node_disk_written_bytes_total
node_disk_io_time_seconds_total
node_network_up
node_network_receive_bytes_total
node_network_transmit_bytes_total
node_network_receive_errs_total
node_network_transmit_errs_total
node_network_receive_drop_total
node_network_transmit_drop_total
node_hwmon_temp_celsius
node_timex_offset_seconds
node_timex_sync_status
```

Vendor-exporter or TVT adapter metrics, where supported:

```text
tvt_gpu_utilization_ratio
tvt_gpu_memory_used_bytes
tvt_gpu_temperature_celsius
tvt_npu_utilization_ratio
tvt_npu_temperature_celsius
tvt_decoder_utilization_ratio
tvt_decoder_active_sessions
tvt_decoder_capacity_sessions
tvt_thermal_throttled
```

### 3.2 Host services

```text
tvt_host_service_up{service}
tvt_k3s_api_ready
tvt_k3s_api_request_duration_seconds
tvt_postgresql_up
tvt_postgresql_check_duration_seconds
tvt_host_watchdog_checks_total{result}
tvt_host_watchdog_actions_total{action,result}
tvt_host_last_success_timestamp_seconds{check}
```

`service`, `check`, `action`, and `result` must come from documented bounded
sets. Initial `service` values are `k3s`, `postgresql`, `edge-management`,
`alert-dispatcher`, `prometheus`, and `alloy`.

### 3.3 Kubernetes node and workloads

Use `kube-state-metrics`, kubelet, and cAdvisor metrics:

```text
kube_node_status_condition
kube_deployment_spec_replicas
kube_deployment_status_replicas_available
kube_pod_status_phase
kube_pod_container_status_ready
kube_pod_container_status_restarts_total
kube_pod_container_status_waiting_reason
kube_pod_container_status_terminated_reason
kube_pod_container_resource_requests
kube_pod_container_resource_limits
container_cpu_usage_seconds_total
container_memory_working_set_bytes
container_network_receive_bytes_total
container_network_transmit_bytes_total
container_fs_usage_bytes
```

Node reporter metrics already implemented by the prototype:

```text
apexfabric_node_reporter_up
apexfabric_node_report_cycles_total
apexfabric_node_reporter_capacity_updates_total
```

Additional node reporter metrics required for central diagnosis:

```text
apexfabric_node_report_last_success_timestamp_seconds
apexfabric_node_report_failures_total{reason}
apexfabric_node_report_age_seconds
apexfabric_node_qualified
apexfabric_camera_stream_capacity
apexfabric_camera_stream_allocated
```

### 3.4 Camera discovery and validation

```text
edge_camera_discovered{camera_id}
edge_camera_enabled{camera_id}
edge_camera_rtsp_valid{camera_id}
edge_camera_last_seen_timestamp_seconds{camera_id}
edge_camera_validation_last_success_timestamp_seconds{camera_id}
edge_camera_validation_failures_total{camera_id,reason}
edge_camera_validation_duration_seconds{camera_id}
edge_camera_assigned_consumers{camera_id}
edge_camera_sync_pending{camera_id}
edge_camera_config_sync_attempts_total{camera_id,result}
edge_camera_config_sync_duration_seconds
edge_camera_config_sync_oldest_pending_age_seconds
```

### 3.5 Direct camera-source sessions

```text
cv_source_up{camera_id,use_case}
cv_source_last_media_timestamp_seconds{camera_id,use_case}
cv_source_bitrate_bytes_per_second{camera_id,use_case}
cv_source_reconnects_total{camera_id,use_case,reason}
cv_source_packets_received_total{camera_id,use_case}
cv_source_packets_dropped_total{camera_id,use_case,reason}
cv_source_active_sessions{camera_id}
cv_source_observed_fps{camera_id,use_case}
cv_source_decode_errors_total{camera_id,use_case,reason}
cv_source_reconnect_duration_seconds{use_case}
```

### 3.6 CV inference

```text
cv_frames_received_total{camera_id,use_case}
cv_frames_processed_total{camera_id,use_case}
cv_frames_dropped_total{camera_id,use_case,reason}
cv_inference_attempts_total{camera_id,use_case}
cv_inference_errors_total{camera_id,use_case,reason}
cv_inference_duration_seconds{use_case}
cv_queue_depth{use_case}
cv_last_success_timestamp_seconds{camera_id,use_case}
cv_model_ready{use_case,model_version}
```

### 3.7 HTTP APIs and applications

```text
http_requests_total{service,method,route,status_class}
http_request_duration_seconds{service,method,route}
http_requests_in_progress{service}
application_errors_total{service,error_code}
application_build_info{service,version}
```

HTTP routes must be normalized templates such as `/cameras/{camera_id}`, not
raw paths containing IDs.

### 3.8 Alert dispatcher

```text
tvt_alert_events_total{source,status,result}
tvt_alerts_active{severity}
tvt_notification_outbox_depth{state}
tvt_notification_oldest_pending_age_seconds
tvt_notification_delivery_attempts_total{result}
tvt_notification_delivery_duration_seconds{result}
tvt_notification_last_success_timestamp_seconds
tvt_alert_emergency_spool_items
```

### 3.9 Telemetry delivery

Use native Prometheus and Alloy internal metrics where the pinned versions
provide them. TVT adapters supply any normalized metrics not available from
those components.

```text
prometheus_remote_storage_samples_pending
prometheus_remote_storage_samples_retried_total
prometheus_remote_storage_samples_failed_total
prometheus_remote_storage_samples_dropped_total
prometheus_remote_storage_highest_timestamp_in_seconds
tvt_remote_write_oldest_pending_age_seconds
tvt_remote_write_last_success_timestamp_seconds

tvt_alloy_up
tvt_alloy_parse_failures_total{source}
tvt_alloy_loki_write_attempts_total{result}
tvt_alloy_log_entries_dropped_total{reason}
tvt_alloy_oldest_pending_age_seconds
tvt_alloy_last_success_timestamp_seconds

tvt_edge_event_outbox_depth{state}
tvt_edge_event_oldest_pending_age_seconds
tvt_edge_event_delivery_attempts_total{result}
```

Remote Write queue metrics may be unavailable centrally during the outage they
describe. The heartbeat therefore includes a coarse copy of current telemetry
pipeline health. Retained metric samples are backfilled with their original
timestamps when connectivity returns.

### 3.10 Metric label policy

Allowed product dimensions are:

- stable internal `camera_id` for the small configured camera set;
- `use_case` from the fixed use-case catalog;
- `reason` and `error_code` from documented taxonomies;
- normalized service, HTTP method, route, and status class; and
- software or model version when it changes infrequently.

Do not use request IDs, operation IDs, stream-session IDs, event IDs, frame
IDs, person data, number plates, IP addresses, URLs, exception messages, or
stack traces as metric labels.

## 4. Host heartbeat

A host `systemd` service outside K3s sends one small authenticated heartbeat
every 30 seconds. The central receipt time is authoritative for liveness;
device time is retained only as diagnostic context.

### 4.1 Payload

```json
{
  "schema_version": "1.0",
  "sequence": 4812,
  "boot_id": "bounded-opaque-id",
  "sent_at": "2026-09-02T10:20:30Z",
  "uptime_seconds": 72631,
  "agent_version": "1.2.0",
  "edge_software_version": "2026.09.1",
  "k3s": {
    "service_active": true,
    "api_ready": true,
    "node_ready": true
  },
  "host": {
    "disk_pressure": false,
    "memory_pressure": false,
    "thermal_pressure": false
  },
  "telemetry": {
    "prometheus_up": true,
    "remote_write_state": "healthy",
    "remote_write_oldest_pending_seconds": 0,
    "alloy_up": true,
    "log_queue_state": "healthy",
    "event_outbox_state": "healthy"
  },
  "credentials": {
    "renewal_state": "healthy",
    "minimum_expiry_seconds": 172800
  }
}
```

Required envelope fields:

```text
schema_version
sequence
boot_id
sent_at
uptime_seconds
agent_version
edge_software_version
```

Required bounded status fields:

```text
k3s.service_active
k3s.api_ready
k3s.node_ready
host.disk_pressure
host.memory_pressure
host.thermal_pressure
telemetry.prometheus_up
telemetry.remote_write_state
telemetry.remote_write_oldest_pending_seconds
telemetry.alloy_up
telemetry.log_queue_state
telemetry.event_outbox_state
credentials.renewal_state
credentials.minimum_expiry_seconds
```

State fields use a bounded enumeration where a Boolean is insufficient:

```text
healthy
degraded
blocked
unknown
```

### 4.2 Central derived metrics

The heartbeat receiver produces these metrics using identity injected by the
trusted gateway:

```text
tvt_edge_heartbeat_age_seconds{edge_id}
tvt_edge_online{edge_id}
tvt_edge_heartbeat_received_total{result}
tvt_edge_heartbeat_rejected_total{reason}
tvt_edge_clock_skew_seconds{edge_id}
tvt_edge_restarts_total{edge_id}
tvt_edge_k3s_ready{edge_id}
tvt_edge_telemetry_pipeline_healthy{edge_id,channel}
tvt_edge_certificate_expiry_seconds{edge_id,role}
```

### 4.3 Registry thresholds

- `online`: a valid heartbeat arrived within 90 seconds.
- `suspected_offline`: no valid heartbeat for 90 seconds.
- `offline`: no valid heartbeat for five minutes outside an approved
  maintenance window.

The sender applies jitter so fleet heartbeats do not synchronize. It retries
transient failures with bounded exponential backoff rather than accumulating
old heartbeats.

## 5. Loki structured logs

Loki receives structured operational logs, not Prometheus metrics. A condition
that can be represented by a bounded counter or gauge should normally increment
that metric and emit a correlated log from the same code path.

### 5.1 Sources

Grafana Alloy collects:

- product Pod stdout;
- monitoring-component Pod stdout;
- selected Kubernetes Events;
- K3s and containerd host journals needed for operations;
- edge-management and alert-dispatcher service journals;
- heartbeat-agent and edge-event sender journals; and
- identity and certificate-renewal service journals.

### 5.2 JSON log record

Every service writes one complete JSON object per stdout line. Required fields:

```text
timestamp
level
service
event
message
error_code
```

Contextual fields are added when applicable:

```text
camera_id
use_case
request_id
operation_id
event_id
stream_session_id
deployment
namespace
pod
container
model_version
software_version
retry_in_seconds
duration_seconds
result
reason
exception_type
stack_trace
```

Example:

```json
{
  "timestamp": "2026-09-02T10:20:30.123Z",
  "level": "error",
  "service": "face-recognition",
  "event": "rtsp_connection_failed",
  "error_code": "RTSP_AUTH_FAILED",
  "message": "RTSP authentication failed",
  "camera_id": "camera-03",
  "use_case": "face-recognition",
  "request_id": null,
  "stream_session_id": "d2f7c3d5-8c41-4201-904b-9ed525e527c2",
  "retry_in_seconds": 30
}
```

An exception type or stack trace may be an escaped JSON string field. Services
must not emit a separate multiline, non-JSON traceback.

### 5.3 Loki labels

Only these low-cardinality fields normally become Loki labels:

```text
cluster
namespace
service
container
level
```

Keep the following as parsed JSON fields rather than indexed labels:

```text
camera_id
request_id
operation_id
event_id
stream_session_id
message
stack_trace
timestamp
```

## 6. Edge-event webhook

The edge-event webhook carries low-frequency host state transitions that may
not reach central monitoring through Remote Write. It must not carry routine
metrics, logs, per-frame events, per-request events, alert recipients, email
templates, or commands.

### 6.1 Event envelope

```json
{
  "schema_version": "1.0",
  "event_id": "8c0e382e-7c88-4536-a791-4a14b678dc47",
  "event_code": "PROMETHEUS_UNAVAILABLE",
  "state": "firing",
  "occurred_at": "2026-09-02T10:20:30Z",
  "sequence": 42,
  "boot_id": "bounded-opaque-id",
  "software_version": "2026.09.1",
  "attributes": {
    "duration_seconds": 180,
    "reason": "health_check_failed"
  }
}
```

Required fields:

```text
schema_version
event_id
event_code
state
occurred_at
sequence
boot_id
software_version
attributes
```

`event_id` is a UUID used for at-least-once delivery deduplication. `state`
uses one of:

```text
firing
resolved
occurred
```

`occurred` represents a one-time action such as a watchdog restart.

### 6.2 Allowlisted event codes

```text
PROMETHEUS_UNAVAILABLE
ALLOY_UNAVAILABLE
K3S_API_UNAVAILABLE
K3S_WATCHDOG_RESTARTED
TELEMETRY_CERT_RENEWAL_FAILED
REMOTE_WRITE_QUEUE_EXHAUSTED
REMOTE_WRITE_BUFFER_LOSS
LOG_BUFFER_EXHAUSTED
LOG_BUFFER_LOSS
EDGE_EVENT_OUTBOX_EXHAUSTED
POSTGRESQL_UNAVAILABLE
HOST_DISK_CRITICAL
HOST_THERMAL_CRITICAL
HOST_TIME_UNSYNCHRONIZED
```

Stateful conditions use the same event code with `state="firing"` and
`state="resolved"`. Separate `*_RECOVERED` event codes are not used.

### 6.3 Permitted attributes

Only safe, bounded attributes are accepted:

```text
duration_seconds
queue_depth
oldest_pending_seconds
discarded_items
certificate_role
expiry_seconds
watchdog_action
reason
```

`reason`, `certificate_role`, and `watchdog_action` must come from documented
allowlists. Attributes must not contain raw logs, exception messages, stack
traces, URLs, credentials, people, plates, recipients, templates, or commands.

### 6.4 Delivery behavior

The edge sender:

1. persists an event before its first delivery attempt;
2. retries transient failures with bounded exponential backoff and jitter;
3. marks the event delivered only after the central receiver durably commits
   it;
4. retains permanent schema or authorization failures as bounded local
   diagnostics instead of retrying forever; and
5. never asks the central plane to alert, notify a recipient, render a
   template, or execute an operation.

The central receiver authenticates the edge, validates schema, size, time and
allowlisted values, deduplicates by `event_id`, and converts accepted events
into the canonical central alert pipeline.
