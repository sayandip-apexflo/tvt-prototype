import React,{useEffect,useState} from 'react';
import {edgeApi} from './api';
import {Pill} from './shared';

const todayLocal=()=>new Date().toLocaleDateString('en-CA');
const fmtClock=value=>value?new Date(value*1000).toLocaleString('en-IN',{timeZone:'Asia/Kolkata',dateStyle:'short',timeStyle:'medium'}):'—';
function fmtDuration(seconds){
  if(seconds==null)return '—';
  if(!seconds)return '0m';
  const hours=Math.floor(seconds/3600),minutes=Math.round((seconds%3600)/60);
  return hours?`${hours}h ${minutes}m`:`${minutes}m`;
}
async function safeGet(path,fallback){try{return await edgeApi(path)}catch{return fallback}}
const employeeName=person=>person.display_name?.trim()||'Unnamed employee';
const attendanceStatus=person=>person.incomplete_session_count?'incomplete':person.visit_count?'complete':'not observed';
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
  useEffect(()=>{let live=true;safeGet(`reports/attendance?date=${encodeURIComponent(date)}`,{sessions:[],people:[],registered_person_count:0,incomplete_session_count:0,total_duration_seconds:0}).then(r=>{if(live)setReport(r)});return()=>{live=false}},[date]);
  return <>
    <ActivityLog cameras={cameras}/>
    {!report?<p>Loading attendance…</p>:<section className="cu-settings-panel">
      <h2>Attendance</h2>
      <p>Total time inside the plant for every registered person, {date}</p>
      <div className="cu-camera-metrics"><article><div><h2>Registered people</h2><strong>{report.registered_person_count??report.people?.length??0}</strong></div></article><article><div><h2>Total time inside</h2><strong>{fmtDuration(report.total_duration_seconds)}</strong></div></article><article><div><h2>Incomplete visits</h2><strong>{report.incomplete_session_count||0}</strong></div></article></div>
      {report.people?.length?<div className="cu-table-wrap"><table className="cu-table"><thead><tr><th>Person</th><th>First entry</th><th>Last exit</th><th>Visits</th><th>Total inside</th><th>Status</th></tr></thead>
        <tbody>{report.people.map(person=><tr key={person.person_id}><td><strong>{employeeName(person)}</strong></td><td>{fmtClock(person.first_entry_time)}</td><td>{fmtClock(person.last_exit_time)}</td><td>{person.visit_count||0}</td><td>{fmtDuration(person.total_duration_seconds||0)}</td><td><Pill value={attendanceStatus(person)}/>{person.incomplete_session_count?` ${person.incomplete_session_count}`:''}</td></tr>)}</tbody>
      </table></div>:<p className="cu-empty">No registered people are available. Complete face enrollment and name each person before attendance reporting.</p>}
    </section>}
  </>;
}

function VehicleTrafficTab({date}){
  const [report,setReport]=useState(null);
  useEffect(()=>{let live=true;safeGet(`reports/vehicle-traffic?date=${encodeURIComponent(date)}`,{sessions:[],vehicles:[],vehicle_count:0,entered_count:0,exited_count:0}).then(r=>{if(live)setReport(r)});return()=>{live=false}},[date]);
  if(!report)return <p>Loading vehicle traffic…</p>;
  return <section className="cu-settings-panel">
    <h2>Vehicle traffic</h2>
    <p>First-to-last ANPR detection duration per number plate, {date}</p>
    <div className="cu-camera-metrics"><article><div><h2>Vehicles observed</h2><strong>{report.vehicle_count??report.vehicles?.length??0}</strong></div></article><article><div><h2>With duration</h2><strong>{report.vehicles?.filter(vehicle=>vehicle.duration_seconds!=null).length||0}</strong></div></article></div>
    {report.vehicles?.length?<div className="cu-table-wrap"><table className="cu-table"><thead><tr><th>Plate</th><th>First detection</th><th>Last detection</th><th>Detections</th><th>Duration</th><th>Status</th></tr></thead>
      <tbody>{report.vehicles.map(vehicle=><tr key={`${vehicle.report_date}-${vehicle.plate_key}`}><td className="mono"><strong>{vehicle.plate_text}</strong></td><td>{fmtClock(vehicle.first_detection_time)}</td><td>{fmtClock(vehicle.last_detection_time)}</td><td>{vehicle.detection_count}</td><td>{fmtDuration(vehicle.duration_seconds)}</td><td><Pill value={vehicle.status}/></td></tr>)}</tbody>
    </table></div>:<p className="cu-empty">No vehicle detections. Enable ANPR and configure a capture zone or accepted crossing line.</p>}
  </section>;
}

export function ReportsPage({cameras=[]}){
  const [tab,setTab]=useState('attendance');
  const [date,setDate]=useState(todayLocal());
  return <>
    <div className="cu-section-title"><h2>Reports</h2></div>
    <p className="cu-notice">Daily registered-person attendance and first-to-last vehicle detection duration.</p>
    <div className="cu-toolbar" style={{display:'flex',justifyContent:'space-between',alignItems:'center',marginBottom:16}}>
      <div className="cu-tabs cu-settings-tabs" style={{marginBottom:0}}><button className={tab==='attendance'?'active':''} onClick={()=>setTab('attendance')}>Attendance</button><button className={tab==='vehicles'?'active':''} onClick={()=>setTab('vehicles')}>Vehicle traffic</button></div>
      <label><input type="date" value={date} max={todayLocal()} onChange={e=>setDate(e.target.value)}/></label>
    </div>
    {tab==='attendance'?<AttendanceTab date={date} cameras={cameras}/>:<VehicleTrafficTab date={date}/>}
    <p className="cu-notice">Attendance requires face-recognition Entry and Exit lines. Vehicle duration requires at least two accepted ANPR detections for the same normalized plate.</p>
  </>;
}
