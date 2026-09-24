// Admin-only (/apexfabricdashboard) infrastructure views. Data comes solely
// from apexfabric-control's :8088 API (`status`, `catalog`,
// `workload-telemetry`) — never tvt_edge (AGENTS.md §1/§6).
import React,{useState} from 'react';
import {RefreshCw,Server,X} from 'lucide-react';
import {api,formatIST} from './api';

const name=x=>x.metadata?.name||'Unknown';
const condition=(x,type)=>x.status?.conditions?.find(c=>c.type===type)?.status==='True';
const podReady=p=>p.status?.phase==='Running'&&(p.status?.containerStatuses||[]).every(c=>c.ready);
const deploymentReady=d=>(d.status?.readyReplicas||0)>=(d.spec?.replicas??1);
function Badge({ok,label}){return <span className={`badge ${ok?'green':'red'}`}><i/>{label||(ok?'Healthy':'Needs attention')}</span>}
function Table({head,rows,empty}){return rows.length?<table><thead><tr>{head.map(h=><th key={h}>{h}</th>)}</tr></thead><tbody>{rows}</tbody></table>:<p className="muted padded">{empty}</p>}

const TABS=[['nodes','Nodes'],['deployments','Deployments'],['pods','Pods'],['services','Services'],['replicas','ReplicaSets'],['storage','Storage'],['events','Events']];

export function Cluster({status,devices,error}){
 const [tab,setTab]=useState('nodes'),[nodeDetail,setNodeDetail]=useState(null),[telemetry,setTelemetry]=useState(null);
 const reports=status.node_reports||[];
 async function loadTelemetry(deployment){setTelemetry({deployment,loading:true});try{setTelemetry(await api(`workload-telemetry?name=${encodeURIComponent(deployment)}`))}catch(e){setTelemetry({deployment,available:false,error:e.message})}}
 const nodes=status.nodes||[],deployments=status.deployments||[],pods=status.pods||[],services=status.services||[],replicas=status.replica_sets||[],claims=status.persistent_volume_claims||[];
 const events=[...(status.events||[])].sort((a,b)=>(b.lastTimestamp||b.eventTime||b.metadata?.creationTimestamp||'').localeCompare(a.lastTimestamp||a.eventTime||a.metadata?.creationTimestamp||''));
 return <>
  <section className="panel padded"><div className="section-heading"><div><h2>K3s API {error?'unavailable':'connected'}</h2><p>Single-node edge control plane · namespace apexfabric</p></div><Badge ok={!error&&nodes.length>0&&nodes.every(n=>condition(n,'Ready'))&&deployments.every(deploymentReady)}/></div></section>
  <div className="tabs">{TABS.map(([id,label])=><button key={id} className={tab===id?'active':''} onClick={()=>setTab(id)}>{label}</button>)}</div>
  <section className="panel table-wrap">
   {tab==='nodes'&&<Table head={['Box','Status','Role','Operating system','Kubernetes','Profile','Qualification','Camera streams','']} empty="Node status unavailable. The admin UI remains available while K3s recovers." rows={nodes.map(n=>{
    const labels=n.metadata?.labels||{},d=devices.find(x=>x.metadata?.node_uid===n.metadata?.uid)||devices.find(x=>x.device_id===name(n)),report=reports.find(r=>r.metadata?.name===name(n)),qualified=labels['apexfabric.com/qualified']==='true';
    const roles=Object.keys(labels).filter(k=>k.startsWith('node-role.kubernetes.io/')).map(k=>k.slice(24)).sort();
    const streams=k=>n.status?.[k]?.['apexfabric.com/camera-streams']??'—';
    return <tr key={name(n)}><td className="table-name"><Server size={18}/><div><strong>{name(n)}</strong><small>{d?.site_id||'Edge node'}</small></div></td><td><Badge ok={condition(n,'Ready')} label={condition(n,'Ready')?'Ready':'Not ready'}/></td><td>{roles.join(', ')||'Control plane + worker'}</td><td>{n.status?.nodeInfo?.osImage||d?.metadata?.operating_system||'—'}</td><td>{n.status?.nodeInfo?.kubeletVersion||d?.metadata?.kubelet_version||'—'}<small>{labels['kubernetes.io/arch']||''}</small></td><td>{labels['apexfabric.com/hardware-profile']||'—'}</td><td><Badge ok={qualified} label={qualified?'Qualified':'Not qualified'}/><small>{report?.status?.reason||''}</small></td><td>{streams('allocatable')} / {streams('capacity')}<small>allocatable / capacity</small></td><td><button className="button" onClick={()=>setNodeDetail({node:n,report,qualified})}>Details</button></td></tr>})}/>}
   {tab==='deployments'&&<Table head={['Deployment','Ready','Replicas','Image','']} empty="No deployments are present in the apexfabric namespace." rows={deployments.map(d=><tr key={name(d)}><td><strong>{name(d)}</strong><small>{d.metadata?.labels?.['apexfabric.com/application']||d.metadata?.labels?.['apexfabric.com/deployment-id']||''}</small></td><td><Badge ok={deploymentReady(d)} label={deploymentReady(d)?'Ready':'Progressing'}/></td><td>{d.status?.readyReplicas??0}/{d.spec?.replicas??0}<small>{d.status?.availableReplicas??0} available</small></td><td className="mono">{d.spec?.template?.spec?.containers?.[0]?.image||'—'}</td><td><button className="button" onClick={()=>loadTelemetry(name(d))}>Telemetry</button></td></tr>)}/>}
   {tab==='pods'&&<Table head={['Pod','Phase','Ready','Restarts','Node','Created']} empty="No Pods are present." rows={pods.map(p=><tr key={name(p)}><td><strong>{name(p)}</strong><small>{p.metadata?.labels?.['apexfabric.com/application']||''}</small></td><td>{p.status?.phase||'Unknown'}</td><td><Badge ok={podReady(p)} label={podReady(p)?'Yes':'No'}/></td><td>{(p.status?.containerStatuses||[]).reduce((s,c)=>s+(c.restartCount||0),0)}</td><td>{p.spec?.nodeName||'Pending'}</td><td>{formatIST(p.metadata?.creationTimestamp)}</td></tr>)}/>}
   {tab==='services'&&<Table head={['Service','Type','Cluster IP','Ports']} empty="No services are present." rows={services.map(s=><tr key={name(s)}><td><strong>{name(s)}</strong></td><td>{s.spec?.type||'—'}</td><td className="mono">{s.spec?.clusterIP||'—'}</td><td>{(s.spec?.ports||[]).map(p=>`${p.port}/${p.protocol||'TCP'}`).join(', ')||'—'}</td></tr>)}/>}
   {tab==='replicas'&&<Table head={['ReplicaSet','Desired','Ready','Available']} empty="No ReplicaSets are present." rows={replicas.map(r=><tr key={name(r)}><td><strong>{name(r)}</strong></td><td>{r.spec?.replicas??0}</td><td>{r.status?.readyReplicas??0}</td><td>{r.status?.availableReplicas??0}</td></tr>)}/>}
   {tab==='storage'&&<Table head={['Claim','Status','Capacity','Storage class']} empty="No PersistentVolumeClaims are present." rows={claims.map(c=><tr key={name(c)}><td><strong>{name(c)}</strong></td><td><Badge ok={c.status?.phase==='Bound'} label={c.status?.phase||'Unknown'}/></td><td>{c.status?.capacity?.storage||c.spec?.resources?.requests?.storage||'—'}</td><td>{c.spec?.storageClassName||'default'}</td></tr>)}/>}
   {tab==='events'&&<Table head={['Type','Object','Reason','Message','Count','Last seen']} empty="Kubernetes events will appear here." rows={events.map((e,i)=><tr key={e.metadata?.uid||i}><td><Badge ok={e.type!=='Warning'} label={e.type||'Normal'}/></td><td>{e.involvedObject?.kind}/{e.involvedObject?.name}</td><td>{e.reason||'—'}</td><td>{e.message||'—'}</td><td>{e.count??1}</td><td>{formatIST(e.lastTimestamp||e.eventTime||e.metadata?.creationTimestamp)}</td></tr>)}/>}
  </section>

  {nodeDetail&&<div className="scrim" onClick={()=>setNodeDetail(null)}><div className="modal wide" onClick={e=>e.stopPropagation()}><div className="modal-head"><div><span className="eyebrow">NODE REPORT</span><h2>{name(nodeDetail.node)}</h2></div><button className="icon-button" onClick={()=>setNodeDetail(null)} aria-label="Close"><X size={20}/></button></div>
   <p className="muted">Controller qualification and reporter-discovered host capabilities.</p>
   <p><Badge ok={condition(nodeDetail.node,'Ready')} label={condition(nodeDetail.node,'Ready')?'Ready':'Not ready'}/> <Badge ok={nodeDetail.qualified} label={nodeDetail.qualified?'Qualified':'Not qualified'}/></p>
   <p>Reason: {nodeDetail.report?.status?.reason||'No controller reason reported'}<br/>Report observed: {formatIST(nodeDetail.report?.spec?.observedAt)}</p>
   <h3>Discovered capabilities</h3><pre className="diagnostic-output">{JSON.stringify(nodeDetail.report?.spec?.capabilities||{status:'No ApexNodeStatus report'},null,2)}</pre>
  </div></div>}

  {telemetry&&<div className="scrim" onClick={()=>setTelemetry(null)}><div className="modal wide" onClick={e=>e.stopPropagation()}><div className="modal-head"><div><span className="eyebrow">WORKLOAD TELEMETRY</span><h2>{telemetry.deployment}</h2></div><button className="icon-button" onClick={()=>setTelemetry(null)} aria-label="Close"><X size={20}/></button></div>
   {telemetry.loading?<p className="muted"><RefreshCw className="spin" size={14}/> Loading telemetry…</p>:<>
    <p><Badge ok={telemetry.available} label={telemetry.available?'Live':'Unavailable'}/> {telemetry.error}</p>
    <h3>Health</h3><pre className="diagnostic-output">{JSON.stringify(telemetry.health||{status:'unavailable'},null,2)}</pre>
    <h3>Readiness</h3><pre className="diagnostic-output">{JSON.stringify(telemetry.readiness||{ready:false},null,2)}</pre>
    <h3>Kubernetes state</h3><pre className="diagnostic-output">{JSON.stringify(telemetry.kubernetes||{},null,2)}</pre>
    <h3>Metrics</h3><pre className="diagnostic-output">{telemetry.metrics||'No metrics response.'}</pre>
   </>}
  </div></div>}
 </>;
}

export function Catalog({solutions,onChanged}){
 const [busy,setBusy]=useState(false),[error,setError]=useState('');
 async function refresh(){setBusy(true);setError('');try{await api('catalog/refresh',{});await onChanged()}catch(e){setError(e.message)}finally{setBusy(false)}}
 return <section className="panel table-wrap"><div className="section-heading"><div><h2>Solution catalog</h2><p>Approved Solution Pack images known to apexfabric-control</p></div><button className="button" disabled={busy} onClick={refresh}><RefreshCw size={14} className={busy?'spin':''}/>Refresh catalog</button></div>
  {error&&<div className="status-banner warning">{error}</div>}
  <Table head={['Solution','Hardware','Image','Digest','Status']} empty="Catalog is empty. Run the trusted catalog seed and refresh workflow." rows={solutions.map(s=><tr key={s.catalog_id}><td><strong>{s.contract?.ui?.displayName||s.name}</strong><small>{s.version}</small></td><td>{s.contract?.hardwareProfile||'—'}</td><td className="mono">{s.image?.repository}:{s.image?.tag}</td><td className="mono">{s.image?.digest?`${s.image.digest.slice(0,19)}…`:'—'}</td><td><Badge ok={s.status==='available'} label={s.status}/>{s.last_error&&<small>{s.last_error}</small>}</td></tr>)}/>
 </section>;
}
