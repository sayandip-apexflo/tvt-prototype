import { useCallback, useEffect, useState, type MouseEvent } from "react";
import { get, send } from "./api";
import { ConfirmButton, EmptyState, Field, FormActions, Icon, JsonView, StatusPill, submitForm } from "./components";
import type { CameraGeometryResponse, GeometryShape } from "./types";

type Mutate = (promise: Promise<unknown>, success: string) => Promise<boolean>;
type Mode = "zone" | "line";
type Point = [number, number];
interface Draft { kind: Mode; points: Point[] }

const clamp01 = (value: number) => Math.min(1, Math.max(0, value));
const pct = (value: number) => `${(value * 100).toFixed(2)}%`;
const fmtPoint = (point: Point) => `${(point[0] * 100).toFixed(0)}, ${(point[1] * 100).toFixed(0)}`;

export function CameraGeometryEditor({ cameraId, mutate }: { cameraId: string; mutate: Mutate }) {
  const [geometry, setGeometry] = useState<CameraGeometryResponse | null>(null);
  const [snapshotToken, setSnapshotToken] = useState(() => Date.now());
  const [snapshotError, setSnapshotError] = useState(false);
  const [mode, setMode] = useState<Mode>("zone");
  const [draft, setDraft] = useState<Draft | null>(null);
  const [insideEndpoint, setInsideEndpoint] = useState<"a" | "b">("a");
  const [showConfig, setShowConfig] = useState(false);

  const load = useCallback(async () => {
    setGeometry(await get<CameraGeometryResponse>(`/api/v1/cameras/${encodeURIComponent(cameraId)}/geometry`));
  }, [cameraId]);
  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    const timer = window.setInterval(() => { setSnapshotToken(Date.now()); setSnapshotError(false); }, 10000);
    return () => window.clearInterval(timer);
  }, [cameraId]);

  const startMode = (next: Mode) => { setMode(next); setDraft(null); setInsideEndpoint("a"); };

  const onCanvasClick = (event: MouseEvent<HTMLDivElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    const point: Point = [clamp01((event.clientX - rect.left) / rect.width), clamp01((event.clientY - rect.top) / rect.height)];
    setDraft((previous) => {
      const points = previous && previous.kind === mode ? previous.points : [];
      if (mode === "line" && points.length >= 2) return previous;
      return { kind: mode, points: [...points, point] };
    });
  };

  const resetDraft = () => { setDraft(null); setInsideEndpoint("a"); };
  const undoPoint = () => setDraft((previous) => previous && previous.points.length > 1 ? { ...previous, points: previous.points.slice(0, -1) } : null);

  const saveZone = async (name: string) => {
    if (!draft || draft.points.length < 3) return;
    if (await mutate(send(`/api/v1/cameras/${encodeURIComponent(cameraId)}/zones`, "POST", { name, points: draft.points }), "Zone saved")) {
      resetDraft(); await load();
    }
  };
  const saveLine = async (name: string, roleKey: string, direction: string) => {
    if (!draft || draft.points.length !== 2) return;
    if (await mutate(send(`/api/v1/cameras/${encodeURIComponent(cameraId)}/lines`, "POST", {
      name, points: draft.points, role_key: roleKey, direction, inside_side: insideEndpoint,
    }), "Line saved")) {
      resetDraft(); await load();
    }
  };
  const deleteShape = async (shapeId: string) => {
    if (await mutate(send(`/api/v1/cameras/${encodeURIComponent(cameraId)}/geometry/${shapeId}`, "DELETE"), "Shape removed")) await load();
  };

  const zoneReady = draft?.kind === "zone" && draft.points.length >= 3;
  const lineReady = draft?.kind === "line" && draft.points.length === 2;

  return <div className="geometry-editor">
    <div className="callout info"><Icon name="eye" /><div><strong>Draw over the live preview</strong><p>Zones filter what ANPR records to inside the drawn area. Lines mark a gate crossing — pick which endpoint faces the plant interior so entry and exit are told apart.</p></div></div>
    <div className="geometry-toolbar">
      <div className="mode-toggle">
        <button className={mode === "zone" ? "active" : ""} onClick={() => startMode("zone")}>Zone (ANPR capture area)</button>
        <button className={mode === "line" ? "active" : ""} onClick={() => startMode("line")}>Line (gate crossing)</button>
      </div>
      <div className="button-row">
        <button className="button secondary small" onClick={() => { setSnapshotToken(Date.now()); setSnapshotError(false); }}><Icon name="refresh" size={15} /> Refresh preview</button>
        {draft && draft.points.length > 0 && <button className="button secondary small" onClick={undoPoint}>Undo point</button>}
        {draft && <button className="button secondary small" onClick={resetDraft}>Clear draft</button>}
      </div>
    </div>
    <div className="geometry-canvas" onClick={onCanvasClick}>
      {snapshotError
        ? <div className="empty-state geometry-snapshot-error"><Icon name="warning" size={22} /><p>Camera preview is unavailable. Confirm the camera is enabled and streaming, then refresh.</p></div>
        : <img
            src={`/api/v1/cameras/${encodeURIComponent(cameraId)}/snapshot?t=${snapshotToken}`}
            alt="Camera preview"
            draggable={false}
            onError={() => setSnapshotError(true)}
          />}
      <svg viewBox="0 0 1 1" preserveAspectRatio="none" className="geometry-overlay">
        {geometry?.shapes.filter((shape) => shape.kind === "zone").map((shape) => (
          <polygon key={shape.shape_id} className="geometry-shape geometry-zone" points={shape.points.map((p) => `${p[0]},${p[1]}`).join(" ")} vectorEffect="non-scaling-stroke" />
        ))}
        {geometry?.shapes.filter((shape) => shape.kind === "line").map((shape) => (
          <g key={shape.shape_id} className="geometry-shape">
            <line className={`geometry-line geometry-line-${shape.direction}`} x1={shape.points[0][0]} y1={shape.points[0][1]} x2={shape.points[1][0]} y2={shape.points[1][1]} vectorEffect="non-scaling-stroke" />
          </g>
        ))}
        {draft?.kind === "zone" && draft.points.length > 0 && (
          <polyline className="geometry-draft" points={draft.points.map((p) => `${p[0]},${p[1]}`).join(" ")} vectorEffect="non-scaling-stroke" />
        )}
        {draft?.kind === "line" && draft.points.length === 2 && (
          <line className="geometry-draft" x1={draft.points[0][0]} y1={draft.points[0][1]} x2={draft.points[1][0]} y2={draft.points[1][1]} vectorEffect="non-scaling-stroke" />
        )}
        {draft?.points.map((point, index) => (
          <circle key={index} className="geometry-vertex" cx={point[0]} cy={point[1]} r={0.012} vectorEffect="non-scaling-stroke" />
        ))}
      </svg>
      {draft?.kind === "line" && draft.points.map((point, index) => (
        <span key={index} className="geometry-point-label" style={{ left: pct(point[0]), top: pct(point[1]) }}>{index === 0 ? "A" : "B"}{insideEndpoint === (index === 0 ? "a" : "b") ? " · inside" : ""}</span>
      ))}
    </div>

    {mode === "zone" && zoneReady && <form className="form-grid geometry-save-form" onSubmit={(event) => submitForm(event, async (form) => { await saveZone(String(form.get("name") || "").trim() || "ANPR zone"); })}>
      <Field label="Zone name" wide><input name="name" placeholder="ANPR capture area" required /></Field>
      <FormActions><button type="button" className="button secondary" onClick={resetDraft}>Cancel</button><button className="button" type="submit">Save zone ({draft!.points.length} points)</button></FormActions>
    </form>}

    {mode === "line" && lineReady && <form className="form-grid geometry-save-form" onSubmit={(event) => submitForm(event, async (form) => {
      await saveLine(String(form.get("name") || "").trim() || "Gate line", String(form.get("role_key") || "").trim(), String(form.get("direction") || "entry"));
    })}>
      <Field label="Line name" wide><input name="name" placeholder="Main entrance entry" required /></Field>
      <Field label="Gate name" hint="Shared by the paired camera at this gate, e.g. main-entrance"><input name="role_key" pattern="[a-z0-9][a-z0-9.-]*" placeholder="main-entrance" required /></Field>
      <Field label="This camera detects"><select name="direction">
        <option value="entry">Entry (into the plant)</option>
        <option value="exit">Exit (out of the plant)</option>
      </select></Field>
      <Field label="Which endpoint faces inside?" wide>
        <div className="mode-toggle">
          <button type="button" className={insideEndpoint === "a" ? "active" : ""} onClick={() => setInsideEndpoint("a")}>A is inside ({fmtPoint(draft!.points[0])})</button>
          <button type="button" className={insideEndpoint === "b" ? "active" : ""} onClick={() => setInsideEndpoint("b")}>B is inside ({fmtPoint(draft!.points[1])})</button>
        </div>
      </Field>
      <FormActions><button type="button" className="button secondary" onClick={resetDraft}>Cancel</button><button className="button" type="submit">Save line</button></FormActions>
    </form>}

    <h3>Saved zones &amp; lines</h3>
    {!geometry?.shapes.length && <EmptyState icon="camera" title="Nothing drawn yet" description="Draw a zone or line on the preview above, then save it." />}
    <div className="geometry-shape-list">{geometry?.shapes.map((shape: GeometryShape) => (
      <div className="line-item" key={shape.shape_id}>
        <span><strong>{shape.name}</strong><small>{shape.shape_key} · {shape.points.length} points{shape.role_key ? ` · gate ${shape.role_key}` : ""}</small></span>
        {shape.direction && <StatusPill value={shape.direction === "entry" ? "healthy" : "unconfigured"} label={shape.direction === "entry" ? "Entry" : "Exit"} />}
        <ConfirmButton className="button ghost-danger small" message={`Remove ${shape.name}? Deployments using it will need reconfiguring.`} onConfirm={() => deleteShape(shape.shape_id)}>Remove</ConfirmButton>
      </div>
    ))}</div>

    <button type="button" className="text-button" onClick={() => setShowConfig((value) => !value)}>{showConfig ? "Hide" : "Show"} compiled vendor config <Icon name="arrow" size={15} /></button>
    {showConfig && <JsonView value={geometry?.compiled_config ?? {}} />}
  </div>;
}
