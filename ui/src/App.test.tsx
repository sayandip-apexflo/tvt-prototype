import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
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

  it("offers to start enrollment for a face_recognition camera and reflects an active window", async () => {
    const camera = {
      camera_id: "camera-01", friendly_name: "Main entrance", configured: true, enabled: true,
      credentials_configured: true, identifiers: [],
      assignments: [{ deployment_id: "tvt-mills-edge-intel-285h", apps: ["face_recognition", "anpr"], fps: 8 }],
      created_at: "2026-09-03T00:00:00Z", updated_at: "2026-09-03T00:00:00Z",
    };
    responses["/api/v1/cameras"] = [camera];
    responses["/api/v1/cameras/camera-01"] = camera;
    responses["/api/v1/deployments/tvt-mills-edge-intel-285h/enrollment-windows"] = [];
    render(<App />);
    await waitFor(() => expect(screen.getByText("Plant 01 · edge-01")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /Cameras/i }));
    fireEvent.click(screen.getByText("Main entrance"));
    const startButton = await screen.findByRole("button", { name: "Use as enrollment camera" });
    fireEvent.click(startButton);
    expect(screen.getByRole("heading", { name: "Use as enrollment camera" })).toBeInTheDocument();

    responses["/api/v1/deployments/tvt-mills-edge-intel-285h/enrollment-windows"] = [{
      window_id: "w1", deployment_key: "tvt-mills-edge-intel-285h", camera_id: "camera-01",
      started_at: "2026-09-03T00:00:00Z", expires_at: null, ended_at: null, status: "active",
    }];
    fireEvent.click(screen.getByRole("button", { name: "Start enrollment" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Revert now" })).toBeInTheDocument());
  });
});
