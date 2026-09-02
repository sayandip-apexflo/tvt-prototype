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
  state: string;
  enabled: boolean;
  credentials_configured: boolean;
  selected_profile_id?: string | null;
  selected_profile?: CameraProfile | null;
  roles?: CameraRole[];
  assignments?: Array<{ deployment_id: string; apps: string[]; fps: number }>;
  validation_code?: string | null;
  validation_failures: number;
  next_retry_at?: string | null;
  last_observed_at?: string | null;
  last_validated_at?: string | null;
  last_media_at?: string | null;
  identifiers: CameraIdentifier[];
  observations?: Observation[];
  created_at: string;
  updated_at: string;
}

export interface Observation {
  observation_id: string;
  camera_id?: string | null;
  method: string;
  address: string;
  result_code: string;
  metadata: Record<string, unknown>;
  observed_at: string;
}

export interface ValidationAttempt {
  attempt_id: string;
  trigger: string;
  status: string;
  stage?: string | null;
  result_code?: string | null;
  safe_result: Record<string, unknown>;
  started_at?: string | null;
  finished_at?: string | null;
  created_at: string;
}

export interface DiscoveryRun {
  operation_id: string;
  trigger: string;
  status: string;
  counters: Record<string, number>;
  error_code?: string | null;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  observations?: Observation[];
}

export interface DiscoveryScope {
  scope_id: string;
  interface_name: string;
  cidr: string;
  rtsp_ports: number[];
  enabled: boolean;
}

export interface Deployment {
  deployment_id: string;
  solution_id: string;
  namespace: string;
  lifecycle_intent: string;
  sync_state: string;
  desired_revision?: number | null;
  applied_revision?: number | null;
  last_error_code?: string | null;
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
