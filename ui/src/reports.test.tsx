import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ReportsPage } from "./reports";

describe("ReportsPage", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = typeof input === "string" ? input : input.toString();
      if (path.startsWith("/api/v1/reports/attendance")) {
        return new Response(JSON.stringify({
          sessions: [{ id: "s1", person_id: "p1", display_name: "Jane Doe", gate: "main-entrance", entry_time: 1757200000, exit_time: 1757203600, duration_seconds: 3600, status: "closed" }],
          total_duration_seconds: 3600,
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      if (path.startsWith("/api/v1/reports/vehicle-traffic")) {
        return new Response(JSON.stringify({
          sessions: [{ id: "v1", plate_text: "MH12AB1234", gate: "plant-entrance", entry_time: 1757200000, exit_time: null, status: "open" }],
          entered_count: 1, exited_count: 0,
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      return new Response(JSON.stringify({}), { status: 200, headers: { "content-type": "application/json" } });
    }));
  });

  afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

  it("shows attendance sessions by default and switches to vehicle traffic", async () => {
    render(<ReportsPage />);
    await waitFor(() => expect(screen.getByText("Jane Doe")).toBeInTheDocument());
    expect(screen.getByText("main-entrance")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Vehicle traffic" }));
    await waitFor(() => expect(screen.getByText("MH12AB1234")).toBeInTheDocument());
    expect(screen.getByText("plant-entrance")).toBeInTheDocument();
  });
});
