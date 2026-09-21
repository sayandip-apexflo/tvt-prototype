import { useEffect, useState } from "react";
import { get } from "./api";
import { DataTable, EmptyState, Icon, PageHeader, Panel, StatusPill } from "./components";
import type { AttendanceReport, VehicleTrafficReport } from "./types";

type Tab = "attendance" | "vehicles";

const todayLocal = () => new Date().toLocaleDateString("en-CA"); // YYYY-MM-DD
const fmtClock = (value?: number | null) => value ? new Date(value * 1000).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", dateStyle: "short", timeStyle: "medium" }) : "—";
const fmtDuration = (seconds: number) => {
  if (!seconds) return "0m";
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.round((seconds % 3600) / 60);
  return hours ? `${hours}h ${minutes}m` : `${minutes}m`;
};

async function safeGet<T>(path: string, fallback: T): Promise<T> {
  try { return await get<T>(path); } catch { return fallback; }
}

function AttendanceTab({ date }: { date: string }) {
  const [report, setReport] = useState<AttendanceReport | null>(null);
  useEffect(() => {
    let active = true;
    void safeGet<AttendanceReport>(`/api/v1/reports/attendance?date=${encodeURIComponent(date)}`, { sessions: [], total_duration_seconds: 0 })
      .then((result) => { if (active) setReport(result); });
    return () => { active = false; };
  }, [date]);
  if (!report) return <div className="drawer-loading">Loading attendance…</div>;
  return <Panel title="Attendance" subtitle={`Time spent inside the plant per person, ${date}`} className="flush">
    <div className="metric-grid three" style={{ padding: "0 16px 16px" }}>
      <div className="metric-card"><div className="metric-top"><span>Closed sessions</span></div><strong>{report.sessions.length}</strong></div>
      <div className="metric-card"><div className="metric-top"><span>Total time inside</span></div><strong>{fmtDuration(report.total_duration_seconds)}</strong></div>
    </div>
    {report.sessions.length ? <DataTable headers={["Person", "Gate", "Entry", "Exit", "Duration", "Status"]}>{report.sessions.map((session) => <tr key={session.id}>
      <td><strong>{session.display_name || session.person_id}</strong></td>
      <td>{session.gate}</td>
      <td>{fmtClock(session.entry_time)}</td>
      <td>{fmtClock(session.exit_time)}</td>
      <td>{fmtDuration(session.duration_seconds || 0)}</td>
      <td><StatusPill value={session.status} /></td>
    </tr>)}</DataTable> : <EmptyState icon="history" title="No attendance sessions" description="Entry/exit crossings on a face-recognition gate will appear here once recorded." />}
  </Panel>;
}

function VehicleTrafficTab({ date }: { date: string }) {
  const [report, setReport] = useState<VehicleTrafficReport | null>(null);
  useEffect(() => {
    let active = true;
    void safeGet<VehicleTrafficReport>(`/api/v1/reports/vehicle-traffic?date=${encodeURIComponent(date)}`, { sessions: [], entered_count: 0, exited_count: 0 })
      .then((result) => { if (active) setReport(result); });
    return () => { active = false; };
  }, [date]);
  if (!report) return <div className="drawer-loading">Loading vehicle traffic…</div>;
  return <Panel title="Vehicle traffic" subtitle={`ANPR gate entries and exits, ${date}`} className="flush">
    <div className="metric-grid three" style={{ padding: "0 16px 16px" }}>
      <div className="metric-card"><div className="metric-top"><span>Vehicles entered</span></div><strong>{report.entered_count}</strong></div>
      <div className="metric-card"><div className="metric-top"><span>Vehicles exited</span></div><strong>{report.exited_count}</strong></div>
    </div>
    {report.sessions.length ? <DataTable headers={["Plate", "Gate", "Entry", "Exit", "Status"]}>{report.sessions.map((session) => <tr key={session.id}>
      <td className="mono"><strong>{session.plate_text}</strong></td>
      <td>{session.gate}</td>
      <td>{fmtClock(session.entry_time)}</td>
      <td>{fmtClock(session.exit_time)}</td>
      <td><StatusPill value={session.status} /></td>
    </tr>)}</DataTable> : <EmptyState icon="boxes" title="No vehicle sessions" description="ANPR reads on a gate line will appear here once recorded." />}
  </Panel>;
}

export function ReportsPage() {
  const [tab, setTab] = useState<Tab>("attendance");
  const [date, setDate] = useState(todayLocal());
  return <>
    <PageHeader eyebrow="Site reporting" title="Reports" description="Attendance (time spent inside vs outside the plant) and daily vehicle entry/exit, derived from configured gate lines." />
    <div className="toolbar">
      <nav className="tabs"><button className={tab === "attendance" ? "active" : ""} onClick={() => setTab("attendance")}>Attendance</button><button className={tab === "vehicles" ? "active" : ""} onClick={() => setTab("vehicles")}>Vehicle traffic</button></nav>
      <label className="field" style={{ maxWidth: 200 }}><span>Date</span><input type="date" value={date} max={todayLocal()} onChange={(event) => setDate(event.target.value)} /></label>
    </div>
    {tab === "attendance" ? <AttendanceTab date={date} /> : <VehicleTrafficTab date={date} />}
    <div className="callout info"><Icon name="eye" /><div><strong>Source</strong><p>Sourced live from apexfabric-control's attendance/vehicle-traffic aggregation. Gates only appear here once a camera has a saved Entry and Exit line (see a camera's Zones &amp; Lines tab).</p></div></div>
  </>;
}
