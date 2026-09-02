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
  "/api/v1/discovery-runs?limit=50": [],
  "/api/v1/discovery-scopes": [],
  "/api/v1/deployments": [],
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
});
