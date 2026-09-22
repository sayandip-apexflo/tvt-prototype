import React,{useCallback,useEffect,useState} from 'react';
import {X} from 'lucide-react';
import {edgeApi} from './api';
import {Pill} from './shared';

function fmt(value){return value?new Date(value).toLocaleString('en-IN',{timeZone:'Asia/Kolkata',dateStyle:'medium',timeStyle:'medium'})+' IST':'—'}

const TABS=[['nodes','Nodes'],['deployments','Deployments'],['pods','Pods'],['services','Services'],['replicas','ReplicaSets'],['storage','Storage'],['events','Events']];

export function ClusterPage(){
  const [cluster,setCluster]=useState(null);
  const [error,setError]=useState('');
  const [tab,setTab]=useState('nodes');
  const [nodeDetail,setNodeDetail]=useState(null);
  const [telemetry,setTelemetry]=useState(null);

  const load=useCallback(async()=>{try{setCluster(await edgeApi('cluster'));setError('')}catch(e){setError(e.message)}},[]);
  useEffect(()=>{load();const t=setInterval(load,5000);return()=>clearInterval(t)},[load]);
  async function loadTelemetry(name){try{setTelemetry(await edgeApi(`cluster/workloads/${encodeURIComponent(name)}/telemetry`))}catch(e){setTelemetry({deployment:name,available:false,error:e.message})}}

  const workload=cluster?.workloads;

  return <>
    <div className="cu-section-title"><h2>K3s cluster</h2></div>
    {error&&<p className="cu-notice" role="alert">{error}</p>}
    <section className="cu-settings-panel">
      <div className="cu-device-row"><div><strong>K3s API {cluster?.api?.status==='healthy'?'connected':'unavailable'}</strong><small>Single-node edge control plane · namespace apexfabric</small></div><Pill value={cluster?.status||'unavailable'}/></div>
    </section>
    <div className="cu-tabs cu-settings-tabs">{TABS.map(([id,label])=><button key={id} className={tab===id?'active':''} onClick={()=>setTab(id)}>{label}</button>)}</div>
    <section className="cu-settings-panel cu-table-wrap">
      {tab==='nodes'&&(cluster?.nodes.items.length?<table className="cu-table"><thead><tr><th>Node</th><th>Status</th><th>Architecture</th><th>Profile</th><th>Qualification</th><th>Camera streams</th><th></th></tr></thead>
        <tbody>{cluster.nodes.items.map(n=><tr key={n.name}><td><strong>{n.name}</strong><small>{n.roles?.join(', ')||'control-plane, worker'}</small></td><td><Pill value={n.ready?'ready':'unavailable'} label={n.ready?'Ready':'Not ready'}/></td><td>{n.architecture||'—'}</td><td>{n.hardware_profile||'—'}</td><td><Pill value={n.qualified} label={n.qualified?'Qualified':'Not qualified'}/><small>{n.qualification_reason||''}</small></td><td>{n.camera_streams.allocatable??'—'} / {n.camera_streams.capacity??'—'}<small>allocatable / capacity</small></td><td><button className="cu-btn" onClick={()=>setNodeDetail(n)}>Details</button></td></tr>)}</tbody>
      </table>:<p className="cu-empty">Node status unavailable. The edge UI remains available while K3s recovers.</p>)}
      {tab==='deployments'&&(workload?.deployments.items.length?<table className="cu-table"><thead><tr><th>Deployment</th><th>Ready</th><th>Replicas</th><th>Image</th><th></th></tr></thead>
        <tbody>{workload.deployments.items.map(item=><tr key={item.name}><td><strong>{item.name}</strong><small>{item.application||item.deployment_id}</small></td><td><Pill value={item.ready} label={item.ready?'Ready':'Progressing'}/></td><td>{item.ready_replicas}/{item.desired_replicas}<small>{item.available_replicas} available</small></td><td className="mono">{item.image||'—'}</td><td><button className="cu-btn" onClick={()=>loadTelemetry(item.name)}>Telemetry</button></td></tr>)}</tbody>
      </table>:<p className="cu-empty">No managed Solution Pack workloads are present.</p>)}
      {tab==='pods'&&(workload?.pods.items.length?<table className="cu-table"><thead><tr><th>Pod</th><th>Phase</th><th>Ready</th><th>Restarts</th><th>Node</th><th>Created</th></tr></thead>
        <tbody>{workload.pods.items.map(item=><tr key={item.name}><td><strong>{item.name}</strong><small>{item.application||item.deployment_id}</small></td><td><Pill value={item.phase}/></td><td>{item.ready?'Yes':'No'}</td><td>{item.restart_count}</td><td>{item.node||'Pending'}</td><td>{fmt(item.created_at)}</td></tr>)}</tbody>
      </table>:<p className="cu-empty">No managed Pods are present.</p>)}
      {tab==='services'&&(workload?.services?.items.length?<table className="cu-table"><thead><tr><th>Service</th><th>Type</th><th>Cluster IP</th><th>Ports</th></tr></thead>
        <tbody>{workload.services.items.map(item=><tr key={item.name}><td><strong>{item.name}</strong></td><td>{item.type}</td><td className="mono">{item.cluster_ip||'—'}</td><td>{item.ports.join(', ')||'—'}</td></tr>)}</tbody>
      </table>:<p className="cu-empty">No services are present in the product namespace.</p>)}
      {tab==='replicas'&&(workload?.replica_sets?.items.length?<table className="cu-table"><thead><tr><th>ReplicaSet</th><th>Desired</th><th>Ready</th><th>Available</th></tr></thead>
        <tbody>{workload.replica_sets.items.map(item=><tr key={item.name}><td><strong>{item.name}</strong></td><td>{item.desired}</td><td>{item.ready}</td><td>{item.available}</td></tr>)}</tbody>
      </table>:<p className="cu-empty">No ReplicaSets are present.</p>)}
      {tab==='storage'&&(workload?.persistent_volume_claims?.items.length?<table className="cu-table"><thead><tr><th>Claim</th><th>Status</th><th>Capacity</th><th>Storage class</th><th>Retention</th></tr></thead>
        <tbody>{workload.persistent_volume_claims.items.map(item=><tr key={item.name}><td><strong>{item.name}</strong></td><td><Pill value={item.phase}/></td><td>{item.capacity||'—'}</td><td>{item.storage_class||'default'}</td><td>{item.retention||'managed'}</td></tr>)}</tbody>
      </table>:<p className="cu-empty">No retained workload storage is present.</p>)}
      {tab==='events'&&(workload?.events?.items.length?<table className="cu-table"><thead><tr><th>Type</th><th>Object</th><th>Reason</th><th>Message</th><th>Count</th><th>Last seen</th></tr></thead>
        <tbody>{workload.events.items.map((item,i)=><tr key={`${item.object}-${item.reason}-${i}`}><td><Pill value={item.type==='Warning'?'warning':'healthy'} label={item.type}/></td><td>{item.object}</td><td>{item.reason||'—'}</td><td>{item.message||'—'}</td><td>{item.count}</td><td>{fmt(item.last_seen)}</td></tr>)}</tbody>
      </table>:<p className="cu-empty">Kubernetes events will appear here.</p>)}
    </section>

    {nodeDetail&&<div className="cu-event-scrim" onClick={()=>setNodeDetail(null)}><article className="cu-event-modal" onClick={e=>e.stopPropagation()}>
      <div className="cu-event-modal-head"><div><span className="cu-eyebrow">NODE REPORT</span><h2>{nodeDetail.name}</h2><p>Controller qualification and reporter-discovered host capabilities</p></div><button onClick={()=>setNodeDetail(null)} aria-label="Close"><X/></button></div>
      <dl className="cu-alert-detail" style={{border:0,padding:0}}>
        <div><dt>Node readiness</dt><dd><Pill value={nodeDetail.ready?'ready':'unavailable'}/></dd></div>
        <div><dt>Controller decision</dt><dd><Pill value={nodeDetail.qualified} label={nodeDetail.qualified?'Qualified':'Not qualified'}/></dd></div>
        <div><dt>Reason</dt><dd>{nodeDetail.qualification_reason||'No controller reason reported'}</dd></div>
        <div><dt>Report observed</dt><dd>{fmt(nodeDetail.reporter_observed_at)}</dd></div>
        <div><dt>Hardware profile</dt><dd>{nodeDetail.hardware_profile||'—'}</dd></div>
        <div><dt>Architecture</dt><dd>{nodeDetail.architecture||'—'}</dd></div>
      </dl>
      <h3>Discovered capabilities</h3>
      <pre className="mono" style={{whiteSpace:'pre-wrap',fontSize:10,background:'#faf8fc',padding:12,borderRadius:7}}>{JSON.stringify(nodeDetail.capabilities||{status:'No ApexNodeStatus report'},null,2)}</pre>
    </article></div>}

    {telemetry&&<div className="cu-event-scrim" onClick={()=>setTelemetry(null)}><article className="cu-event-modal" onClick={e=>e.stopPropagation()}>
      <div className="cu-event-modal-head"><div><span className="cu-eyebrow">WORKLOAD TELEMETRY</span><h2>{telemetry.deployment}</h2><p>Allowlisted health, readiness, metrics, and Kubernetes state</p></div><button onClick={()=>setTelemetry(null)} aria-label="Close"><X/></button></div>
      <div className="cu-device-row"><Pill value={telemetry.available?'healthy':'unavailable'} label={telemetry.available?'Live':'Unavailable'}/>{telemetry.error&&<span>{telemetry.error}</span>}</div>
      <h3>Health</h3><pre className="mono" style={{whiteSpace:'pre-wrap',fontSize:10,background:'#faf8fc',padding:12,borderRadius:7}}>{JSON.stringify(telemetry.health||{status:'unavailable'},null,2)}</pre>
      <h3>Readiness</h3><pre className="mono" style={{whiteSpace:'pre-wrap',fontSize:10,background:'#faf8fc',padding:12,borderRadius:7}}>{JSON.stringify(telemetry.readiness||{ready:false},null,2)}</pre>
      <h3>Kubernetes state</h3><pre className="mono" style={{whiteSpace:'pre-wrap',fontSize:10,background:'#faf8fc',padding:12,borderRadius:7}}>{JSON.stringify(telemetry.kubernetes||{},null,2)}</pre>
      <h3>Metrics</h3><pre className="mono" style={{whiteSpace:'pre-wrap',fontSize:10,background:'#faf8fc',padding:12,borderRadius:7}}>{telemetry.metrics||'No metrics response.'}</pre>
    </article></div>}
  </>;
}
