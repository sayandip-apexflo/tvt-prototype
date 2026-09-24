import React,{useEffect,useState} from 'react';
import {edgeApi} from './api';
import {Pill} from './shared';

const todayLocal=()=>new Date().toLocaleDateString('en-CA');
const fmtClock=value=>value?new Date(value*1000).toLocaleString('en-IN',{timeZone:'Asia/Kolkata',dateStyle:'short',timeStyle:'medium'}):'—';
function fmtDuration(seconds){
  if(!seconds)return '0m';
  const hours=Math.floor(seconds/3600),minutes=Math.round((seconds%3600)/60);
  return hours?`${hours}h ${minutes}m`:`${minutes}m`;
}
async function safeGet(path,fallback){try{return await edgeApi(path)}catch{return fallback}}
const employeeName=person=>person.display_name?.trim()||'Unnamed employee';
const cameraLabel=(cameraId,cameras)=>{const camera=cameras.find(item=>item.camera_id===cameraId);return `${camera?.name||camera?.friendly_name||cameraId||'Unknown camera'} (${cameraId||'—'})`};

function ActivityLog({cameras}){
  const [log,setLog]=useState(null);
  useEffect(()=>{let live=true;safeGet('reports/attendance-log?limit=10',{events:[]}).then(r=>{if(live)setLog(r)});return()=>{live=false}},[]);
  if(!log)return <p>Loading recent activity…</p>;
  return <section className="cu-settings-panel">
    <h2>Recent activity</h2>
    <p>Last {log.events.length} entry/exit crossings, including sessions still open.</p>
    {log.events.length?<ul className="cu-plate-list cu-attendance-list">{log.events.map((e,i)=>{const rowKey=`${e.session_id}-${e.action}-${i}`;return <li key={rowKey} style={{padding:'8px'}}>
      <div className="cu-attendance-copy"><strong>{employeeName(e)}</strong>&nbsp;{e.action==='entry'?'entered':'exited'} at {fmtClock(e.time)}</div>
      <time style={{padding:'4px'}}>{cameraLabel(e.camera_id,cameras)}</time>
    </li>})}</ul>:<p className="cu-empty">No entry/exit crossings recorded yet.</p>}
  </section>;
}

function AttendanceTab({date,cameras}){
  const [report,setReport]=useState(null);
  useEffect(()=>{let live=true;safeGet(`reports/attendance?date=${encodeURIComponent(date)}`,{sessions:[],total_duration_seconds:0}).then(r=>{if(live)setReport(r)});return()=>{live=false}},[date]);
  return <>
    <ActivityLog cameras={cameras}/>
    {!report?<p>Loading attendance…</p>:<section className="cu-settings-panel">
      <h2>Attendance</h2>
      <p>Time spent inside the plant per person, {date}</p>
      <div className="cu-camera-metrics"><article><div><h2>Closed sessions</h2><strong>{report.sessions.length}</strong></div></article><article><div><h2>Total time inside</h2><strong>{fmtDuration(report.total_duration_seconds)}</strong></div></article></div>
      {report.sessions.length?<div className="cu-table-wrap"><table className="cu-table"><thead><tr><th>Person</th><th>Gate</th><th>Entry</th><th>Exit</th><th>Duration</th><th>Status</th></tr></thead>
        <tbody>{report.sessions.map(s=><tr key={s.id}><td><strong>{employeeName(s)}</strong></td><td>{s.gate}</td><td>{fmtClock(s.entry_time)}</td><td>{fmtClock(s.exit_time)}</td><td>{fmtDuration(s.duration_seconds||0)}</td><td><Pill value={s.status}/></td></tr>)}</tbody>
      </table></div>:<p className="cu-empty">No closed attendance sessions for {date}. Entry/exit crossings on a face-recognition gate will appear here once both a Entry and Exit line are recorded.</p>}
    </section>}
  </>;
}

function VehicleTrafficTab({date}){
  const [report,setReport]=useState(null);
  useEffect(()=>{let live=true;safeGet(`reports/vehicle-traffic?date=${encodeURIComponent(date)}`,{sessions:[],entered_count:0,exited_count:0}).then(r=>{if(live)setReport(r)});return()=>{live=false}},[date]);
  if(!report)return <p>Loading vehicle traffic…</p>;
  return <section className="cu-settings-panel">
    <h2>Vehicle traffic</h2>
    <p>ANPR gate entries and exits, {date}</p>
    <div className="cu-camera-metrics"><article><div><h2>Vehicles entered</h2><strong>{report.entered_count}</strong></div></article><article><div><h2>Vehicles exited</h2><strong>{report.exited_count}</strong></div></article></div>
    {report.sessions.length?<div className="cu-table-wrap"><table className="cu-table"><thead><tr><th>Plate</th><th>Gate</th><th>Entry</th><th>Exit</th><th>Status</th></tr></thead>
      <tbody>{report.sessions.map(s=><tr key={s.id}><td className="mono"><strong>{s.plate_text}</strong></td><td>{s.gate}</td><td>{fmtClock(s.entry_time)}</td><td>{fmtClock(s.exit_time)}</td><td><Pill value={s.status}/></td></tr>)}</tbody>
    </table></div>:<p className="cu-empty">No vehicle sessions. ANPR reads on a gate line will appear here once recorded.</p>}
  </section>;
}

export function ReportsPage({cameras=[]}){
  const [tab,setTab]=useState('attendance');
  const [date,setDate]=useState(todayLocal());
  return <>
    <div className="cu-section-title"><h2>Reports</h2></div>
    <p className="cu-notice">Attendance (time spent inside vs outside the plant) and daily vehicle entry/exit, derived from configured gate lines.</p>
    <div className="cu-toolbar" style={{display:'flex',justifyContent:'space-between',alignItems:'center',marginBottom:16}}>
      <div className="cu-tabs cu-settings-tabs" style={{marginBottom:0}}><button className={tab==='attendance'?'active':''} onClick={()=>setTab('attendance')}>Attendance</button><button className={tab==='vehicles'?'active':''} onClick={()=>setTab('vehicles')}>Vehicle traffic</button></div>
      <label><input type="date" value={date} max={todayLocal()} onChange={e=>setDate(e.target.value)}/></label>
    </div>
    {tab==='attendance'?<AttendanceTab date={date} cameras={cameras}/>:<VehicleTrafficTab date={date}/>}
    <p className="cu-notice">Sourced live from apexfabric-control's attendance/vehicle-traffic aggregation. Gates only appear here once a camera has a saved Entry and Exit line (see a camera's Zones &amp; Lines tab).</p>
  </>;
}
