import React,{useCallback,useEffect,useState} from 'react';
import {AlertTriangle,ChevronDown,ShieldCheck} from 'lucide-react';
import {edgeApi} from './api';
import {relativeTime} from './shared';

export function useAlerts(){
  const [alerts,setAlerts]=useState([]);
  const [error,setError]=useState('');
  const load=useCallback(async()=>{
    try{setAlerts(await edgeApi('alerts?limit=50&include_resolved=false'));setError('')}
    catch(e){setError(e.message)}
  },[]);
  useEffect(()=>{load();const t=setInterval(load,5000);return()=>clearInterval(t)},[load]);
  return {alerts,alertsError:error,reloadAlerts:load};
}

export function AlertsBar(){
  const {alerts,alertsError,reloadAlerts}=useAlerts();
  const [expanded,setExpanded]=useState(null);
  const [notifications,setNotifications]=useState([]);
  const [busy,setBusy]=useState(false);
  const active=alerts.filter(a=>a.state!=='resolved');

  async function toggle(alert){
    if(expanded===alert.alert_id){setExpanded(null);return}
    setExpanded(alert.alert_id);
    try{setNotifications(await edgeApi(`alerts/${encodeURIComponent(alert.alert_id)}/notifications`))}
    catch{setNotifications([])}
  }
  async function acknowledge(alert){
    setBusy(true);
    try{await edgeApi(`alerts/${encodeURIComponent(alert.alert_id)}/acknowledge`,{});await reloadAlerts()}
    catch{/* surfaced via alertsError on next poll */}
    finally{setBusy(false)}
  }

  return <aside className="cu-alert-bar">
    <div className="cu-alert-heading"><div><ShieldCheck size={18}/><h2>Alerts</h2><b>{active.length}</b></div><p>{alertsError?`Alerts unavailable: ${alertsError}`:'Active operational conditions at this site.'}</p></div>
    {!active.length&&!alertsError&&<div className="cu-empty"><ShieldCheck size={30}/><h3>All clear</h3><p>No active alerts are currently recorded.</p></div>}
    <div className="cu-alert-scroll">{active.map(alert=><article className="cu-alert-card" key={alert.alert_id}>
      <button className="cu-alert-summary" onClick={()=>toggle(alert)}>
        <span className="cu-alert-kicker"><i/>{alert.severity}<ChevronDown size={13}/></span>
        <h3>{alert.annotations?.summary||alert.alertname}</h3>
        <p><AlertTriangle size={12}/>{alert.camera_id||alert.service||'System'}</p>
        <time>{relativeTime(alert.last_seen_at)}</time>
      </button>
      {expanded===alert.alert_id&&<div className="cu-alert-detail">
        <dl>
          <div><dt>State</dt><dd>{alert.state}</dd></div>
          <div><dt>Occurrences</dt><dd>{alert.occurrence_count}</dd></div>
          <div><dt>First seen</dt><dd>{relativeTime(alert.first_seen_at)}</dd></div>
          <div><dt>Email notifications</dt><dd>{notifications.length}</dd></div>
        </dl>
        {alert.state==='active'&&<button className="cu-close-alert" disabled={busy} onClick={()=>acknowledge(alert)}>{busy?'Acknowledging…':'Acknowledge'}</button>}
      </div>}
    </article>)}</div>
  </aside>;
}
