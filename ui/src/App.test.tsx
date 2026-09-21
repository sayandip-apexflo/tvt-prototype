import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";

const responses: Record<string, unknown> = {
  "/api/v1/health": {
    status: "healthy",
    components: {
      database: { status: "healthy" },
      k3s_api: { status: "healthy" },
      workloads: { status: "unconfigured" },
    },
  },
  "/api/v1/site": {
    site_id: "plant-01",
    edge_id: "edge-01",
    display_name: "Plant 01",
    timezone: "Asia/Kolkata",
    config_revision: 1,
  },
  "/api/v1/cameras": [],
  "/api/v1/deployments": [],
  "/api/v1/solutions": [],
  "/api/v1/cluster": {
    status: "healthy",
    api: { status: "healthy" },
    nodes: { status: "healthy", total: 1, items: [{ name: "edge-01", ready: true, qualified: true, camera_streams: {} }] },
    workloads: {
      status: "unconfigured",
      deployments: { total: 0, items: [] },
      pods: { total: 0, items: [] },
      services: { total: 0, items: [] },
      replica_sets: { total: 0, items: [] },
      persistent_volume_claims: { total: 0, items: [] },
      events: { total: 0, items: [] },
    },
  },
  "/api/v1/alerts?limit=200&include_resolved=true": [],
  "/api/v1/audit-events?limit=200": [],
};

describe("edge management UI", () => {
  beforeEach(() => {
    window.location.hash = "";
    responses["/api/v1/solutions"] = [];
    responses["/api/v1/cameras"] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = typeof input === "string" ? input : input.toString();
      return new Response(JSON.stringify(responses[path] ?? []), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }));
    vi.stubGlobal("scrollTo", vi.fn());
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("loads the edge snapshot and exposes the K3s console", async () => {
    render(<App />);
    await waitFor(() => expect(screen.getByText("Plant 01 · edge-01")).toBeInTheDocument());
    expect(screen.getByRole("heading", { name: "System overview" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /K3s cluster/i }));
    expect(screen.getByRole("heading", { name: "K3s cluster" })).toBeInTheDocument();
    expect(screen.getByText("Connected")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Events" })).toBeInTheDocument();
  });

  it("offers preview-gated deployment from an available catalog entry", async () => {
    responses["/api/v1/solutions"] = [{
      catalog_id: "traffic-edge-runtime:2026.08.21-v4", solution_name: "traffic-edge-runtime",
      version: "2026.08.21-v4", hardware_profile: "intel-285h", architectures: ["amd64"], status: "available",
      image: { registry: "127.0.0.1:5000", repository: "apexfabric/traffic-edge-runtime", tag: "intel-285h-2026.08.21-v4", digest: `sha256:${"1".repeat(64)}`, reference: `127.0.0.1:5000/apexfabric/traffic-edge-runtime@sha256:${"1".repeat(64)}` }, contract: {},
    }];
    responses["/api/v1/cameras"] = [{
      camera_id: "camera-01", friendly_name: "Main entrance", configured: true, enabled: true,
      credentials_configured: true, identifiers: [], created_at: "2026-09-03T00:00:00Z", updated_at: "2026-09-03T00:00:00Z",
    }];
    render(<App />);
    await waitFor(() => expect(screen.getByText("Plant 01 · edge-01")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /Solutions/i }));
    expect(screen.getByText("traffic-edge-runtime")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Deploy solution/i }));
    expect(screen.getByRole("button", { name: "Preview bundle" })).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox"));
    expect(screen.getByText(/Geometry config/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Preview bundle" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Commit preview" })).toBeDisabled();
  });

  it("pre-selects the catalog's declared default app when a camera is picked", async () => {
    responses["/api/v1/solutions"] = [{
      catalog_id: "traffic-edge-runtime:2026.08.21-v4", solution_name: "traffic-edge-runtime",
      version: "2026.08.21-v4", hardware_profile: "intel-285h", architectures: ["amd64"], status: "available",
      image: { registry: "127.0.0.1:5000", repository: "apexfabric/traffic-edge-runtime", tag: "intel-285h-2026.08.21-v4", digest: `sha256:${"1".repeat(64)}`, reference: `127.0.0.1:5000/apexfabric/traffic-edge-runtime@sha256:${"1".repeat(64)}` },
      contract: { ui: { camera: { defaultApp: "anpr" } } },
    }];
    responses["/api/v1/cameras"] = [{
      camera_id: "camera-01", friendly_name: "Main entrance", configured: true, enabled: true,
      credentials_configured: true, identifiers: [], created_at: "2026-09-03T00:00:00Z", updated_at: "2026-09-03T00:00:00Z",
    }];
    render(<App />);
    await waitFor(() => expect(screen.getByText("Plant 01 · edge-01")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /Solutions/i }));
    fireEvent.click(screen.getByRole("button", { name: /Deploy solution/i }));
    fireEvent.click(screen.getByRole("checkbox", { name: /Main entrance/i }));
    expect(screen.getByRole("checkbox", { name: "Anpr" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Vehicle Counting" })).not.toBeChecked();
  });

  it("shows live-feed events for a camera in the drawer's Live tab", async () => {
    const snapshotId = "a".repeat(64);
    const camera = {
      camera_id: "camera-01", friendly_name: "Main entrance", configured: true, enabled: true,
      credentials_configured: true, identifiers: [], created_at: "2026-09-03T00:00:00Z", updated_at: "2026-09-03T00:00:00Z",
    };
    responses["/api/v1/cameras"] = [camera];
    responses["/api/v1/cameras/camera-01"] = camera;
    responses["/api/v1/cameras/camera-01/live-feed"] = {
      available: true,
      events: [{
        event_id: "d:1", deployment_id: "traffic-edge-intel-285h-runtime",
        occurred_at: "2026-09-03T00:00:00Z", received_at: 1.0,
        payload: { camera_id: "camera-01", event_type: "vehicle_count_event", payload: { count: { total: 5 } } },
        snapshots: [{ snapshot_id: snapshotId, source_url: "/x", url: `/api/v1/live-feed/snapshots/${snapshotId}` }],
      }],
    };
    render(<App />);
    await waitFor(() => expect(screen.getByText("Plant 01 · edge-01")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /Cameras/i }));
    fireEvent.click(screen.getByText("Main entrance"));
    await waitFor(() => expect(screen.getByRole("button", { name: "Live" })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Live" }));
    await waitFor(() => expect(screen.getByText("Vehicle Count Event")).toBeInTheDocument());
    expect(screen.getByRole("img")).toHaveAttribute("src", `/api/v1/live-feed/snapshots/${snapshotId}`);
  });

  it("shows an unavailable callout when apexfabric-control cannot be reached", async () => {
    const camera = {
      camera_id: "camera-01", friendly_name: "Main entrance", configured: true, enabled: true,
      credentials_configured: true, identifiers: [], created_at: "2026-09-03T00:00:00Z", updated_at: "2026-09-03T00:00:00Z",
    };
    responses["/api/v1/cameras"] = [camera];
    responses["/api/v1/cameras/camera-01"] = camera;
    responses["/api/v1/cameras/camera-01/live-feed"] = {
      available: false, error: "apexfabric-control is unavailable: connection refused", events: [],
    };
    render(<App />);
    await waitFor(() => expect(screen.getByText("Plant 01 · edge-01")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /Cameras/i }));
    fireEvent.click(screen.getByText("Main entrance"));
    fireEvent.click(await screen.findByRole("button", { name: "Live" }));
    await waitFor(() => expect(screen.getByText("Live feed unavailable")).toBeInTheDocument());
  });

  describe("enrollment workflow", () => {
    const DEPLOYMENT = "tvt-mills-edge-intel-285h";
    const camera = {
      camera_id: "camera-01", friendly_name: "Main entrance", configured: true, enabled: true,
      credentials_configured: true, identifiers: [],
      assignments: [{ deployment_id: DEPLOYMENT, apps: ["face_recognition", "anpr"], fps: 8 }],
      created_at: "2026-09-03T00:00:00Z", updated_at: "2026-09-03T00:00:00Z",
    };
    const statusPath = `/api/v1/deployments/${DEPLOYMENT}/enrollment/status`;
    const cameraPath = `/api/v1/deployments/${DEPLOYMENT}/enrollment/camera`;

    const openCameraDrawer = async () => {
      render(<App />);
      await waitFor(() => expect(screen.getByText("Plant 01 · edge-01")).toBeInTheDocument());
      fireEvent.click(screen.getByRole("button", { name: /Cameras/i }));
      fireEvent.click(screen.getByText("Main entrance"));
    };

    beforeEach(() => {
      responses["/api/v1/cameras"] = [camera];
      responses["/api/v1/cameras/camera-01"] = camera;
      responses["/api/v1/enrollment/people"] = [];
      responses[statusPath] = { deployment_key: DEPLOYMENT, designated_camera_id: null, session: null, degraded: false };
    });

    it("lets an operator designate an eligible face-recognition camera", async () => {
      await openCameraDrawer();
      const designateButton = await screen.findByRole("button", { name: "Designate as enrollment camera" });
      fireEvent.click(designateButton);
      expect(screen.getByRole("heading", { name: "Designate enrollment camera" })).toBeInTheDocument();

      responses[cameraPath] = { deployment_key: DEPLOYMENT, camera_id: "camera-01" };
      responses[statusPath] = { deployment_key: DEPLOYMENT, designated_camera_id: "camera-01", session: null, degraded: false };
      fireEvent.click(screen.getByRole("button", { name: "Designate" }));
      await waitFor(() => expect(screen.getByText("Designated enrollment camera")).toBeInTheDocument());
      expect(screen.getByRole("button", { name: "Start enrollment" })).toBeInTheDocument();
    });

    it("starts enrollment and shows switching, ready-for-person, then restoring progress", async () => {
      responses[statusPath] = { deployment_key: DEPLOYMENT, designated_camera_id: "camera-01", session: null, degraded: false };
      await openCameraDrawer();
      fireEvent.click(await screen.findByRole("button", { name: "Start enrollment" }));
      expect(screen.getByRole("heading", { name: "Start enrollment" })).toBeInTheDocument();

      responses[statusPath] = {
        deployment_key: DEPLOYMENT, designated_camera_id: "camera-01", degraded: false,
        session: {
          session_id: "s1", deployment_key: DEPLOYMENT, camera_id: "camera-01", status: "activating",
          naming_status: "not_applicable", capture_result: null, person_id: null, result_code: null,
          error_code: null, capture_window_seconds: 300, started_at: "2026-09-21T09:00:00Z",
          activated_at: null, capture_deadline_at: null, captured_at: null,
          restoration_started_at: null, restored_at: null, completed_at: null,
        },
      };
      const submitButtons = screen.getAllByRole("button", { name: "Start enrollment" });
      fireEvent.click(submitButtons[submitButtons.length - 1]);
      await waitFor(() => expect(screen.getByText("Switching camera to enrollment mode…")).toBeInTheDocument());
    });

    it("shows the cancel control while capturing and restores on cancel", async () => {
      const capturingSession = {
        session_id: "s1", deployment_key: DEPLOYMENT, camera_id: "camera-01", status: "capturing",
        naming_status: "not_applicable", capture_result: null, person_id: null, result_code: null,
        error_code: null, capture_window_seconds: 300, started_at: "2026-09-21T09:00:00Z",
        activated_at: "2026-09-21T09:00:05Z", capture_deadline_at: new Date(Date.now() + 60000).toISOString(),
        captured_at: null, restoration_started_at: null, restored_at: null, completed_at: null,
      };
      responses[statusPath] = { deployment_key: DEPLOYMENT, designated_camera_id: "camera-01", degraded: false, session: capturingSession };
      vi.stubGlobal("confirm", vi.fn(() => true));
      await openCameraDrawer();
      await waitFor(() => expect(screen.getByText("Ready — capture the person's face now")).toBeInTheDocument());
      const cancelButton = screen.getByRole("button", { name: "Cancel" });

      responses[statusPath] = {
        deployment_key: DEPLOYMENT, designated_camera_id: "camera-01", degraded: false,
        session: { ...capturingSession, status: "restoring", result_code: "cancelled" },
      };
      fireEvent.click(cancelButton);
      await waitFor(() => expect(screen.getByText("Restoring normal operation…")).toBeInTheDocument());
    });

    it("shows normal operation restored once a session completes", async () => {
      responses[statusPath] = {
        deployment_key: DEPLOYMENT, designated_camera_id: "camera-01", degraded: false,
        session: {
          session_id: "s1", deployment_key: DEPLOYMENT, camera_id: "camera-01", status: "completed",
          naming_status: "not_applicable", capture_result: "duplicate", person_id: null, result_code: "ok",
          error_code: null, capture_window_seconds: 300, started_at: "2026-09-21T09:00:00Z",
          activated_at: "2026-09-21T09:00:05Z", capture_deadline_at: "2026-09-21T09:05:05Z",
          captured_at: "2026-09-21T09:00:20Z", restoration_started_at: "2026-09-21T09:00:20Z",
          restored_at: "2026-09-21T09:00:30Z", completed_at: "2026-09-21T09:00:30Z",
        },
      };
      await openCameraDrawer();
      await waitFor(() => expect(screen.getByText("Normal operation restored")).toBeInTheDocument());
      // A duplicate match never becomes a naming prompt.
      expect(screen.queryByRole("heading", { name: "Name this person" })).not.toBeInTheDocument();
    });

    it("shows a degraded state while restoring after a timeout and K3s is unreachable", async () => {
      responses[statusPath] = {
        deployment_key: DEPLOYMENT, designated_camera_id: "camera-01", degraded: true,
        session: {
          session_id: "s1", deployment_key: DEPLOYMENT, camera_id: "camera-01", status: "restoring",
          naming_status: "not_applicable", capture_result: null, person_id: null, result_code: "timed_out",
          error_code: "ENROLLMENT_TIMEOUT", capture_window_seconds: 300, started_at: "2026-09-21T09:00:00Z",
          activated_at: "2026-09-21T09:00:05Z", capture_deadline_at: "2026-09-21T09:05:05Z",
          captured_at: null, restoration_started_at: "2026-09-21T09:05:05Z", restored_at: null, completed_at: null,
        },
      };
      await openCameraDrawer();
      await waitFor(() => expect(screen.getByText("Restoring normal operation…")).toBeInTheDocument());
      expect(screen.getByText("K3s unreachable -- retrying")).toBeInTheDocument();
    });

    it("shows the timeout outcome once the camera is confirmed restored", async () => {
      responses[statusPath] = {
        deployment_key: DEPLOYMENT, designated_camera_id: "camera-01", degraded: false,
        session: {
          session_id: "s1", deployment_key: DEPLOYMENT, camera_id: "camera-01", status: "timed_out",
          naming_status: "not_applicable", capture_result: null, person_id: null, result_code: "timed_out",
          error_code: null, capture_window_seconds: 300, started_at: "2026-09-21T09:00:00Z",
          activated_at: "2026-09-21T09:00:05Z", capture_deadline_at: "2026-09-21T09:05:05Z",
          captured_at: null, restoration_started_at: "2026-09-21T09:05:05Z", restored_at: "2026-09-21T09:05:10Z",
          completed_at: "2026-09-21T09:05:10Z",
        },
      };
      await openCameraDrawer();
      await waitFor(() => expect(screen.getByText("No capture arrived in time — camera restored")).toBeInTheDocument());
    });

    it("prompts for a name after a new-person capture and offers a persistent pending-names path", async () => {
      responses[statusPath] = {
        deployment_key: DEPLOYMENT, designated_camera_id: "camera-01", degraded: false,
        session: {
          session_id: "s1", deployment_key: DEPLOYMENT, camera_id: "camera-01", status: "completed",
          naming_status: "pending_name", capture_result: "created", person_id: "person-1", result_code: "ok",
          error_code: null, capture_window_seconds: 300, started_at: "2026-09-21T09:00:00Z",
          activated_at: "2026-09-21T09:00:05Z", capture_deadline_at: "2026-09-21T09:05:05Z",
          captured_at: "2026-09-21T09:00:20Z", restoration_started_at: "2026-09-21T09:00:20Z",
          restored_at: "2026-09-21T09:00:30Z", completed_at: "2026-09-21T09:00:30Z",
        },
      };
      responses["/api/v1/enrollment/people"] = [
        { session_id: "s1", person_id: "person-1", deployment_key: DEPLOYMENT, camera_id: "camera-01", captured_at: "2026-09-21T09:00:20Z" },
      ];
      await openCameraDrawer();
      await waitFor(() => expect(screen.getByRole("heading", { name: "Name this person" })).toBeInTheDocument());
      fireEvent.click(screen.getByRole("button", { name: "Name later" }));
      expect(screen.queryByRole("heading", { name: "Name this person" })).not.toBeInTheDocument();

      // The persistent path survives closing that prompt.
      fireEvent.click(screen.getByRole("button", { name: /Cameras/i }));
      await waitFor(() => expect(screen.getByText("People awaiting a name")).toBeInTheDocument());
      fireEvent.click(screen.getByRole("button", { name: "Name" }));
      const dialog = screen.getByRole("dialog", { name: "Name this person" });
      fireEvent.change(within(dialog).getByRole("textbox"), { target: { value: "Jane Doe" } });
      responses["/api/v1/enrollment/people"] = [];
      fireEvent.click(within(dialog).getByRole("button", { name: "Save name" }));
      await waitFor(() => expect(screen.queryByText("People awaiting a name")).not.toBeInTheDocument());
    });

    it("never fetches or displays an embedding anywhere in the enrollment views", async () => {
      responses[statusPath] = {
        deployment_key: DEPLOYMENT, designated_camera_id: "camera-01", degraded: false,
        session: {
          session_id: "s1", deployment_key: DEPLOYMENT, camera_id: "camera-01", status: "completed",
          naming_status: "pending_name", capture_result: "created", person_id: "person-1", result_code: "ok",
          error_code: null, capture_window_seconds: 300, started_at: "2026-09-21T09:00:00Z",
          activated_at: "2026-09-21T09:00:05Z", capture_deadline_at: "2026-09-21T09:05:05Z",
          captured_at: "2026-09-21T09:00:20Z", restoration_started_at: "2026-09-21T09:00:20Z",
          restored_at: "2026-09-21T09:00:30Z", completed_at: "2026-09-21T09:00:30Z",
        },
      };
      responses["/api/v1/enrollment/people"] = [
        { session_id: "s1", person_id: "person-1", deployment_key: DEPLOYMENT, camera_id: "camera-01", captured_at: "2026-09-21T09:00:20Z" },
      ];
      await openCameraDrawer();
      await waitFor(() => expect(screen.getByRole("heading", { name: "Name this person" })).toBeInTheDocument());
      expect(document.body.innerHTML).not.toMatch(/embedding/i);
    });
  });
});
