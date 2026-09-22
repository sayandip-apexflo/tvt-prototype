import React,{useEffect,useState} from 'react';
import {edgeApi} from './api';
import {Pill,titleCase} from './shared';

function fmt(value){return value?new Date(value).toLocaleString('en-IN',{timeZone:'Asia/Kolkata',dateStyle:'medium',timeStyle:'medium'})+' IST':'—'}

export function OperationsPage(){
  const [audit,setAudit]=useState([]);
  const [error,setError]=useState('');
  const [query,setQuery]=useState('');
  useEffect(()=>{
    let live=true;
    edgeApi('audit-events?limit=200').then(x=>{if(live){setAudit(x);setError('')}}).catch(e=>{if(live)setError(e.message)});
    return()=>{live=false};
  },[]);
  const visible=audit.filter(item=>`${item.action} ${item.actor} ${item.target_id} ${item.result}`.toLowerCase().includes(query.toLowerCase()));
  return <>
    <div className="cu-section-title"><h2>Activity <b>{visible.length}</b></h2></div>
    <p className="cu-notice">A redacted, append-only view of management changes and automated recovery actions.</p>
    {error&&<p className="cu-notice" role="alert">{error}</p>}
    <label className="field" style={{display:'block',marginBottom:14}}><input placeholder="Search activity" value={query} onChange={e=>setQuery(e.target.value)}/></label>
    <section className="cu-settings-panel cu-table-wrap">
      {visible.length?<table className="cu-table"><thead><tr><th>Time</th><th>Action</th><th>Target</th><th>Actor</th><th>Result</th><th>Request ID</th></tr></thead>
        <tbody>{visible.map(item=><tr key={item.audit_id}><td>{fmt(item.created_at)}</td><td><strong>{titleCase(item.action.replaceAll('.',' '))}</strong></td><td>{item.target_type}<small>{item.target_id}</small></td><td>{item.actor}</td><td><Pill value={item.result}/></td><td className="mono">{item.request_id.slice(0,16)}</td></tr>)}</tbody>
      </table>:<p className="cu-empty">No matching activity. Management audit events will appear here.</p>}
    </section>
  </>;
}
