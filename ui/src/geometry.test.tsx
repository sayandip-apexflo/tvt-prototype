import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CameraGeometryEditor } from "./geometry";

const mutate = async (promise: Promise<unknown>) => {
  try { await promise; return true; } catch { return false; }
};

describe("CameraGeometryEditor", () => {
  let calls: Array<{ path: string; method: string; body?: unknown }>;
  let geometryResponse: { shapes: unknown[]; compiled_config: Record<string, unknown> };

  beforeEach(() => {
    calls = [];
    geometryResponse = {
      shapes: [{
        shape_id: "11111111-1111-1111-1111-111111111111", kind: "zone", shape_key: "anpr-capture-area",
        name: "ANPR capture area", points: [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9]],
        role_key: null, direction: null, inside_side: null, enabled: true,
        created_at: "2026-09-03T00:00:00Z", updated_at: "2026-09-03T00:00:00Z",
      }],
      compiled_config: { zones: { anpr: [{ id: "anpr-capture-area", name: "ANPR capture area", poly: [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9]] }] } },
    };
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = typeof input === "string" ? input : input.toString();
      const method = init?.method || "GET";
      calls.push({ path, method, body: init?.body ? JSON.parse(init.body as string) : undefined });
      if (path.includes("/geometry") && method === "GET") {
        return new Response(JSON.stringify(geometryResponse), { status: 200, headers: { "content-type": "application/json" } });
      }
      if (path.includes("/zones") && method === "POST") {
        return new Response(JSON.stringify({ shape_id: "2", kind: "zone", shape_key: "test-zone", name: "Test zone", points: [], enabled: true, created_at: "", updated_at: "" }), { status: 201, headers: { "content-type": "application/json" } });
      }
      if (method === "DELETE") {
        return new Response(null, { status: 204 });
      }
      return new Response(JSON.stringify({}), { status: 200, headers: { "content-type": "application/json" } });
    }));
    vi.stubGlobal("confirm", vi.fn(() => true));
    vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue({
      x: 0, y: 0, top: 0, left: 0, right: 300, bottom: 200, width: 300, height: 200, toJSON: () => {},
    });
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("loads and lists saved zones, and reveals the compiled vendor config", async () => {
    render(<CameraGeometryEditor cameraId="camera-01" mutate={mutate} />);
    await waitFor(() => expect(screen.getByText("ANPR capture area")).toBeInTheDocument());
    expect(screen.getByText(/anpr-capture-area/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Show compiled vendor config/i }));
    expect(screen.getByText(/"zones"/)).toBeInTheDocument();
  });

  it("draws a zone by clicking three points on the canvas and saves it", async () => {
    render(<CameraGeometryEditor cameraId="camera-01" mutate={mutate} />);
    await waitFor(() => expect(screen.getByText("ANPR capture area")).toBeInTheDocument());

    const canvas = document.querySelector(".geometry-canvas") as HTMLElement;
    fireEvent.click(canvas, { clientX: 30, clientY: 20 });
    fireEvent.click(canvas, { clientX: 270, clientY: 20 });
    fireEvent.click(canvas, { clientX: 150, clientY: 180 });

    const nameInput = await screen.findByPlaceholderText("ANPR capture area");
    fireEvent.change(nameInput, { target: { value: "New dock zone" } });
    fireEvent.click(screen.getByRole("button", { name: /Save zone \(3 points\)/i }));

    await waitFor(() => expect(calls.some((call) => call.path.endsWith("/zones") && call.method === "POST")).toBe(true));
    const zoneCall = calls.find((call) => call.path.endsWith("/zones") && call.method === "POST");
    expect(zoneCall?.body).toMatchObject({ name: "New dock zone" });
    expect((zoneCall?.body as { points: number[][] }).points).toHaveLength(3);
  });

  it("deletes a shape after confirmation", async () => {
    render(<CameraGeometryEditor cameraId="camera-01" mutate={mutate} />);
    await waitFor(() => expect(screen.getByText("ANPR capture area")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Remove" }));
    await waitFor(() => expect(calls.some((call) => call.method === "DELETE")).toBe(true));
  });
});
