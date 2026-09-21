export type Status = "healthy" | "degraded" | "unavailable" | "unconfigured" | "progressing" | string;

export interface ComponentHealth { status: Status; [key: string]: unknown }
export interface HealthResponse {
  status: Status;
  components: Record<string, ComponentHealth>;
}

export interface Site {
  site_id: string;
  edge_id: string;
  display_name: string;
  timezone: string;
  config_revision: number;
}

export interface CameraIdentifier { kind: string; value: string }
export interface CameraRole { role_key: string; display_name: string; direction: string; ordinal?: number | null }
export interface CameraProfile {
  profile_id?: string;
  profile_token?: string;
  scheme?: string;
  host?: string;
  port?: number;
  path?: string;
  transport?: string;
  codec?: string | null;
  width?: number | null;
  height?: number | null;
  fps?: number | null;
}
export interface Camera {
  camera_id: string;
  friendly_name: string;
  manufacturer?: string | null;
  model?: string | null;
  configured: boolean;
  enabled: boolean;
  credentials_configured: boolean;
  selected_profile_id?: string | null;
  selected_profile?: CameraProfile | null;
  roles?: CameraRole[];
  assignments?: Array<{ deployment_id: string; apps: string[]; fps: number }>;
  identifiers: CameraIdentifier[];
  created_at: string;
  updated_at: string;
}

export interface GeometryShape {
  shape_id: string;
  kind: "zone" | "line";
  shape_key: string;
  name: string;
  points: number[][];
  role_key?: string | null;
  direction?: "entry" | "exit" | null;
  inside_side?: "a" | "b" | null;
  enabled: boolean;
  created_at: string;
  updated_at: string;
}
export interface CameraGeometryResponse {
  shapes: GeometryShape[];
  compiled_config: Record<string, unknown>;
}

export interface AttendanceSession {
  id: string; person_id: string; gate: string; display_name?: string;
  entry_time?: number | null; exit_time?: number | null; duration_seconds?: number | null; status: string;
}
export interface AttendanceReport { sessions: AttendanceSession[]; total_duration_seconds: number }
export interface VehicleSession {
  id: string; plate_text: string; gate: string;
  entry_time?: number | null; exit_time?: number | null; status: string;
}
export interface VehicleTrafficReport { sessions: VehicleSession[]; entered_count: number; exited_count: number }

export interface LiveFeedSnapshot { snapshot_id: string; source_url: string; url: string }
export interface LiveFeedEvent {
  event_id: string;
  deployment_id: string;
  occurred_at?: string | null;
  received_at: number;
  payload: { camera_id?: string; event_type?: string; [key: string]: unknown };
  snapshots: LiveFeedSnapshot[];
}
export interface LiveFeedResponse { available: boolean; error?: string; events: LiveFeedEvent[] }

export interface Deployment {
  deployment_id: string;
  solution_id: string;
  namespace: string;
  lifecycle_intent: string;
  sync_state: string;
  desired_revision?: number | null;
  applied_revision?: number | null;
  last_error_code?: string | null;
  catalog_id?: string | null;
  desired_bundle_sha256?: string | null;
  applied_bundle_sha256?: string | null;
  applied_image_digest?: string | null;
  bundle_history?: Array<{ bundle_sha256: string; desired_revision: number; image_digest?: string | null; created_at: string }>;
}

export interface SolutionCatalog {
  catalog_id: string;
  solution_name: string;
  version: string;
  hardware_profile: string;
  architectures: string[];
  status: string;
  image: { registry: string; repository: string; tag: string; digest?: string | null; reference?: string | null };
  contract: Record<string, unknown>;
  last_error?: string | null;
}

export interface DeploymentPreview {
  catalog_id: string;
  bundle_sha256: string;
  image_reference: string;
  bundle: Record<string, unknown>;
  desired_state: Record<string, unknown>;
}

export interface NodeView {
  name: string;
  ready: boolean;
  qualified: boolean;
  architecture?: string | null;
  hardware_profile?: string | null;
  roles?: string[];
  qualification_reason?: string | null;
  reporter_observed_at?: string | null;
  capabilities?: Record<string, unknown>;
  camera_streams: { capacity?: string | null; allocatable?: string | null };
}
export interface DeploymentView {
  name: string;
  deployment_id?: string | null;
  application?: string | null;
  namespace?: string | null;
  image?: string | null;
  desired_replicas: number;
  ready_replicas: number;
  available_replicas: number;
  ready: boolean;
}
export interface PodView {
  name: string;
  deployment_id?: string | null;
  application?: string | null;
  node?: string | null;
  phase: string;
  ready: boolean;
  restart_count: number;
  created_at?: string | null;
  containers?: Array<{ name: string; ready: boolean; restarts: number; state?: string }>;
}
export interface ServiceView { name: string; type: string; cluster_ip?: string | null; ports: string[] }
export interface ReplicaSetView { name: string; desired: number; ready: number; available: number; deployment_id?: string | null }
export interface PvcView { name: string; phase: string; capacity?: string | null; storage_class?: string | null; retention?: string | null }
export interface EventView { type: string; object: string; reason?: string | null; message?: string | null; count: number; last_seen?: string | null }
export interface ClusterResponse {
  status: Status;
  api: ComponentHealth;
  nodes: { status: Status; total: number; items: NodeView[] };
  workloads: {
    status: Status;
    deployments: { total: number; items: DeploymentView[] };
    pods: { total: number; items: PodView[] };
    services?: { total: number; items: ServiceView[] };
    replica_sets?: { total: number; items: ReplicaSetView[] };
    persistent_volume_claims?: { total: number; items: PvcView[] };
    events?: { total: number; items: EventView[] };
  };
  synchronization?: { status: Status; total: number; by_state: Record<string, number>; items: Deployment[] };
}

export interface AlertItem {
  alert_id: string;
  alertname: string;
  severity: string;
  service?: string | null;
  camera_id?: string | null;
  use_case?: string | null;
  state: string;
  starts_at: string;
  first_seen_at: string;
  last_seen_at: string;
  occurrence_count: number;
  acknowledged_at?: string | null;
  acknowledged_by?: string | null;
  resolved_at?: string | null;
  annotations: Record<string, string>;
}
export interface NotificationItem {
  notification_id: string;
  type: string;
  state: string;
  attempt_count: number;
  next_attempt_at?: string | null;
  sent_at?: string | null;
  recipient_count: number;
  attempts: Array<{ attempt_number: number; result: string; smtp_code?: number | null; error_category?: string | null; started_at: string }>;
}
export interface AuditItem {
  audit_id: string;
  actor: string;
  request_id: string;
  action: string;
  target_type: string;
  target_id: string;
  result: string;
  details: Record<string, unknown>;
  created_at: string;
}

export interface TelemetryResponse {
  deployment: string;
  available: boolean;
  contract?: Record<string, Record<string, string | null>>;
  health?: Record<string, unknown> | null;
  readiness?: Record<string, unknown> | null;
  metrics?: string;
  kubernetes?: Record<string, unknown>;
  error?: string;
}

export type EnrollmentSessionStatus =
  | "activating" | "capturing" | "restoring" | "completed" | "timed_out" | "cancelled" | "failed";
export type EnrollmentNamingStatus = "not_applicable" | "pending_name" | "named";
export type EnrollmentCaptureResult = "created" | "duplicate" | null;

// Never carries an embedding, a display name, a snapshot URL, or a raw
// event body -- see AGENTS.md security invariants and
// ManagementService._enrollment_session_view.
export interface EnrollmentSessionView {
  session_id: string;
  deployment_key: string;
  camera_id: string;
  status: EnrollmentSessionStatus;
  naming_status: EnrollmentNamingStatus;
  capture_result: EnrollmentCaptureResult;
  person_id: string | null;
  result_code: string | null;
  error_code: string | null;
  capture_window_seconds: number;
  started_at: string;
  activated_at: string | null;
  capture_deadline_at: string | null;
  captured_at: string | null;
  restoration_started_at: string | null;
  restored_at: string | null;
  completed_at: string | null;
}

export interface EnrollmentStatusResponse {
  deployment_key: string;
  designated_camera_id: string | null;
  session: EnrollmentSessionView | null;
  degraded: boolean;
}

export interface EnrollmentCameraResponse {
  deployment_key: string;
  camera_id: string | null;
}

export interface PendingPerson {
  session_id: string;
  person_id: string;
  deployment_key: string;
  camera_id: string | null;
  captured_at: string | null;
}
