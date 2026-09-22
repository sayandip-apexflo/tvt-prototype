import React,{useCallback,useEffect,useState} from 'react';
import {ShieldCheck,X} from 'lucide-react';
import {base,edgeApi} from './api';

const TABS=['Overview','Stream','Credentials','Zones & Lines'];
const mutator=(reload,notifyError)=>async promise=>{try{await promise;await reload();return true}catch(e){notifyError(e.message);return false}};

export function useTvtCameras(){
  const [cameras,setCameras]=useState([]);
  const [error,setError]=useState('');
  const load=useCallback(async()=>{try{setCameras(await edgeApi('cameras'));setError('')}catch(e){setError(e.message)}},[]);
  useEffect(()=>{load()},[load]);
  return {tvtCameras:cameras,tvtError:error,reloadTvtCameras:load};
}

// apexCameras: apexfabric-control's own device-registry list (name, in_use, assigned_to),
// independent of tvt_edge -- see AGENTS.md deploy/ui/ note. Unioned by camera_id so
// cameras created via either path stay visible in one place.
export function mergeCameraLists(tvtCameras,apexCameras){
  const byId=new Map();
  for(const c of apexCameras||[])byId.set(c.camera_id,{camera_id:c.camera_id,name:c.name,in_use:c.in_use,assigned_to:c.assigned_to});
  for(const c of tvtCameras||[]){
    const existing=byId.get(c.camera_id)||{camera_id:c.camera_id};
    byId.set(c.camera_id,{...existing,name:existing.name||c.friendly_name,tvt:c});
  }
  return [...byId.values()];
}

export function CameraCreateModal({close,onCreated}){
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  async function submit(e){
    e.preventDefault();setBusy(true);setError('');
    const f=new FormData(e.currentTarget);
    try{
      await edgeApi('cameras',{camera_id:f.get('camera_id'),friendly_name:f.get('friendly_name'),manufacturer:f.get('manufacturer')||null,model:f.get('model')||null,identifiers:[],rtsp_url:f.get('rtsp_url')||null});
      if(onCreated)await onCreated();
      close();
    }catch(e){setError(e.message)}finally{setBusy(false)}
  }
  return <div className="cu-event-scrim" onClick={close}><article className="cu-event-modal" onClick={e=>e.stopPropagation()}>
    <div className="cu-event-modal-head"><div><span className="cu-eyebrow">NEW CAMERA</span><h2>Add camera</h2><p>Register a camera with tvt_edge. Credentials are entered separately, after this camera exists.</p></div><button onClick={close} aria-label="Close"><X/></button></div>
    <form className="cu-camera-form" onSubmit={submit}>
      <label>Camera ID<input name="camera_id" pattern="[a-z0-9][a-z0-9.-]*" placeholder="camera-01" required/></label>
      <label>Friendly name<input name="friendly_name" placeholder="Main entrance entry" required/></label>
      <label className="cu-stream-field">RTSP URL<input name="rtsp_url" placeholder="rtsp://192.168.20.11:554/live/main" pattern="rtsps?://.+"/><small className="cu-stream-help">No credentials in the URL — set those after creating the camera.</small></label>
      <label>Manufacturer<input name="manufacturer" placeholder="Optional"/></label>
      <label>Model<input name="model" placeholder="Optional"/></label>
      {error&&<p className="cu-camera-error" role="alert">{error}</p>}
      <button className="cu-btn cu-primary" disabled={busy}>{busy?'Adding…':'Add camera'}</button>
    </form>
  </article></div>;
}

function RegisterCameraPanel({cameraId,friendlyName,onRegistered}){
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  async function submit(e){
    e.preventDefault();setBusy(true);setError('');
    const f=new FormData(e.currentTarget);
    try{
      await edgeApi('cameras',{camera_id:cameraId,friendly_name:f.get('friendly_name'),manufacturer:null,model:null,identifiers:[],rtsp_url:f.get('rtsp_url')||null});
      await onRegistered();
    }catch(e){setError(e.message)}finally{setBusy(false)}
  }
  return <section className="cu-settings-panel">
    <h2>Not managed by tvt_edge yet</h2>
    <p>This camera exists in the fleet but has no tvt_edge configuration record. Register it to unlock stream, credentials, and zone/line management.</p>
    {error&&<p className="cu-notice" role="alert">{error}</p>}
    <form className="cu-camera-form" onSubmit={submit}>
      <label className="cu-stream-field">Friendly name<input name="friendly_name" defaultValue={friendlyName||cameraId} required/></label>
      <label className="cu-stream-field">RTSP URL<input name="rtsp_url" placeholder="rtsp://192.168.20.11:554/live/main" pattern="rtsps?://.+"/></label>
      <button className="cu-btn cu-primary" disabled={busy}>{busy?'Registering…':'Register camera'}</button>
    </form>
  </section>;
}

function CameraOverviewTab({camera,act}){
  return <>
    <p>{[camera.manufacturer,camera.model].filter(Boolean).join(' ')||'Unknown vendor'} · Updated {camera.updated_at?new Date(camera.updated_at).toLocaleString():'—'}</p>
    <div className="cu-device-row"><ShieldCheck size={18}/><div><strong>{camera.enabled?'Enabled':'Disabled'}</strong><small>{camera.configured?'Stream configured':'Stream not configured'} · {camera.credentials_configured?'Credentials configured':'No credentials'}</small></div>
      <button className="cu-btn" onClick={()=>act(edgeApi(`cameras/${encodeURIComponent(camera.camera_id)}/enabled`,{enabled:!camera.enabled},'PATCH'))}>{camera.enabled?'Disable':'Enable'}</button>
    </div>
    <h3>Assignments</h3>
    {camera.assignments?.length?camera.assignments.map(a=><div className="cu-device-row" key={a.deployment_id}><div><strong>{a.deployment_id}</strong><small>{a.apps.join(', ')} · {a.fps} FPS</small></div></div>):<p className="cu-empty">Not assigned to a deployment yet.</p>}
  </>;
}

function CameraStreamTab({camera,act}){
  const [busy,setBusy]=useState(false);
  const p=camera.selected_profile||{};
  async function submit(e){
    e.preventDefault();setBusy(true);
    const f=new FormData(e.currentTarget);
    await act(edgeApi(`cameras/${encodeURIComponent(camera.camera_id)}/stream`,{scheme:f.get('scheme'),host:f.get('host'),port:Number(f.get('port')),path:f.get('path'),profile_token:f.get('profile_token'),transport:f.get('transport'),codec:f.get('codec')||null,width:f.get('width')?Number(f.get('width')):null,height:f.get('height')?Number(f.get('height')):null,fps:f.get('fps')?Number(f.get('fps')):null},'PUT'));
    setBusy(false);
  }
  return <form className="cu-camera-form" onSubmit={submit}>
    <label>Scheme<select name="scheme" defaultValue={p.scheme||'rtsp'}><option>rtsp</option><option>rtsps</option></select></label>
    <label>Transport<select name="transport" defaultValue={p.transport||'tcp'}><option>tcp</option><option>udp</option></select></label>
    <label className="cu-stream-field">Host<input name="host" defaultValue={p.host||''} placeholder="192.168.20.11" required/></label>
    <label>Port<input name="port" type="number" defaultValue={p.port||554} required/></label>
    <label>Profile token<input name="profile_token" defaultValue={p.profile_token||'main'} required/></label>
    <label className="cu-stream-field">Path<input name="path" defaultValue={p.path||'/live/main'} required/></label>
    <label>Codec<input name="codec" defaultValue={p.codec||''} placeholder="h264"/></label>
    <label>FPS<input name="fps" type="number" step="0.1" defaultValue={p.fps||''}/></label>
    <label>Width<input name="width" type="number" defaultValue={p.width||''}/></label>
    <label>Height<input name="height" type="number" defaultValue={p.height||''}/></label>
    <button className="cu-btn cu-primary" disabled={busy}>{busy?'Saving…':'Save stream'}</button>
  </form>;
}

function CameraCredentialsTab({camera,act}){
  const [busy,setBusy]=useState(false);
  async function submit(e){
    e.preventDefault();setBusy(true);
    const form=e.currentTarget,f=new FormData(form);
    if(await act(edgeApi(`cameras/${encodeURIComponent(camera.camera_id)}/credentials`,{username:f.get('username')||null,password:f.get('password')||null,query:{}},'PUT')))form.reset();
    setBusy(false);
  }
  async function clear(){
    if(!confirm('Permanently destroy the stored camera credentials?'))return;
    setBusy(true);await act(edgeApi(`cameras/${encodeURIComponent(camera.camera_id)}/credentials`,undefined,'DELETE'));setBusy(false);
  }
  return <>
    <p className="cu-notice"><ShieldCheck size={14}/> Credentials are write-only. Existing values are never returned to this browser.</p>
    <form className="cu-camera-form" autoComplete="off" onSubmit={submit}>
      <label className="cu-stream-field">Username<input name="username" autoComplete="off"/></label>
      <label className="cu-stream-field">Password<input name="password" type="password" autoComplete="new-password"/></label>
      <button className="cu-btn cu-primary" disabled={busy}>{busy?'Saving…':'Replace credentials'}</button>
    </form>
    {camera.credentials_configured&&<button className="cu-btn" disabled={busy} onClick={clear}>Clear credentials</button>}
  </>;
}

function CameraGeometryTab({cameraId,notifyError}){
  const [geometry,setGeometry]=useState(null);
  const [snapshotToken,setSnapshotToken]=useState(()=>Date.now());
  const [snapshotError,setSnapshotError]=useState(false);
  const [mode,setMode]=useState('zone');
  const [draft,setDraft]=useState(null);
  const [insideEndpoint,setInsideEndpoint]=useState('a');

  const load=useCallback(async()=>{setGeometry(await edgeApi(`cameras/${encodeURIComponent(cameraId)}/geometry`))},[cameraId]);
  useEffect(()=>{load()},[load]);
  const act=mutator(load,notifyError);

  function startMode(next){setMode(next);setDraft(null);setInsideEndpoint('a')}
  function onCanvasClick(e){
    const rect=e.currentTarget.getBoundingClientRect();
    const point=[Math.min(1,Math.max(0,(e.clientX-rect.left)/rect.width)),Math.min(1,Math.max(0,(e.clientY-rect.top)/rect.height))];
    setDraft(prev=>{
      const points=prev&&prev.kind===mode?prev.points:[];
      if(mode==='line'&&points.length>=2)return prev;
      return {kind:mode,points:[...points,point]};
    });
  }
  function resetDraft(){setDraft(null);setInsideEndpoint('a')}
  function undoPoint(){setDraft(prev=>prev&&prev.points.length>1?{...prev,points:prev.points.slice(0,-1)}:null)}

  async function saveZone(e){
    e.preventDefault();
    if(!draft||draft.points.length<3)return;
    const name=String(new FormData(e.currentTarget).get('name')||'').trim()||'ANPR zone';
    if(await act(edgeApi(`cameras/${encodeURIComponent(cameraId)}/zones`,{name,points:draft.points})))resetDraft();
  }
  async function saveLine(e){
    e.preventDefault();
    if(!draft||draft.points.length!==2)return;
    const f=new FormData(e.currentTarget);
    const name=String(f.get('name')||'').trim()||'Gate line',roleKey=String(f.get('role_key')||'').trim(),direction=String(f.get('direction')||'entry');
    if(await act(edgeApi(`cameras/${encodeURIComponent(cameraId)}/lines`,{name,points:draft.points,role_key:roleKey,direction,inside_side:insideEndpoint})))resetDraft();
  }
  async function deleteShape(shapeId){
    if(!confirm('Remove this shape? Deployments using it will need reconfiguring.'))return;
    await act(edgeApi(`cameras/${encodeURIComponent(cameraId)}/geometry/${shapeId}`,undefined,'DELETE'));
  }

  const zoneReady=draft?.kind==='zone'&&draft.points.length>=3;
  const lineReady=draft?.kind==='line'&&draft.points.length===2;
  const pct=v=>`${(v*100).toFixed(2)}%`;
  const fmtPoint=p=>`${(p[0]*100).toFixed(0)}, ${(p[1]*100).toFixed(0)}`;

  return <div className="geometry-editor">
    <p className="cu-notice">Zones filter what ANPR records to inside the drawn area. Lines mark a gate crossing — pick which endpoint faces the plant interior so entry and exit are told apart.</p>
    <div className="geometry-toolbar">
      <div className="cu-tabs"><button className={mode==='zone'?'active':''} onClick={()=>startMode('zone')}>Zone (ANPR capture area)</button><button className={mode==='line'?'active':''} onClick={()=>startMode('line')}>Line (gate crossing)</button></div>
      <div className="geometry-toolbar-actions">
        <button type="button" className="cu-btn" onClick={()=>{setSnapshotToken(Date.now());setSnapshotError(false)}}>Refresh preview</button>
        {draft?.points.length>0&&<button type="button" className="cu-btn" onClick={undoPoint}>Undo point</button>}
        {draft&&<button type="button" className="cu-btn" onClick={resetDraft}>Clear draft</button>}
      </div>
    </div>
    <div className="geometry-canvas" onClick={onCanvasClick}>
      {snapshotError
        ?<div className="cu-empty">Camera preview is unavailable. Confirm the camera is enabled and streaming, then refresh.</div>
        :<img src={`${base}/api/v1/cameras/${encodeURIComponent(cameraId)}/snapshot?t=${snapshotToken}`} alt="Camera preview" draggable={false} onError={()=>setSnapshotError(true)}/>}
      <svg viewBox="0 0 1 1" preserveAspectRatio="none" className="geometry-overlay">
        {geometry?.shapes.filter(s=>s.kind==='zone').map(s=><polygon key={s.shape_id} className="geometry-shape geometry-zone" points={s.points.map(p=>`${p[0]},${p[1]}`).join(' ')} vectorEffect="non-scaling-stroke"/>)}
        {geometry?.shapes.filter(s=>s.kind==='line').map(s=><line key={s.shape_id} className={`geometry-line geometry-line-${s.direction}`} x1={s.points[0][0]} y1={s.points[0][1]} x2={s.points[1][0]} y2={s.points[1][1]} vectorEffect="non-scaling-stroke"/>)}
        {draft?.kind==='zone'&&draft.points.length>0&&<polyline className="geometry-draft" points={draft.points.map(p=>`${p[0]},${p[1]}`).join(' ')} vectorEffect="non-scaling-stroke"/>}
        {draft?.kind==='line'&&draft.points.length===2&&<line className="geometry-draft" x1={draft.points[0][0]} y1={draft.points[0][1]} x2={draft.points[1][0]} y2={draft.points[1][1]} vectorEffect="non-scaling-stroke"/>}
        {draft?.points.map((p,i)=><circle key={i} className="geometry-vertex" cx={p[0]} cy={p[1]} r={0.012} vectorEffect="non-scaling-stroke"/>)}
      </svg>
      {draft?.kind==='line'&&draft.points.map((p,i)=><span key={i} className="geometry-point-label" style={{left:pct(p[0]),top:pct(p[1])}}>{i===0?'A':'B'}{insideEndpoint===(i===0?'a':'b')?' · inside':''}</span>)}
    </div>

    {mode==='zone'&&zoneReady&&<form className="cu-camera-form" onSubmit={saveZone}>
      <label className="cu-stream-field">Zone name<input name="name" placeholder="ANPR capture area" required/></label>
      <button type="button" className="cu-btn" onClick={resetDraft}>Cancel</button>
      <button className="cu-btn cu-primary">Save zone ({draft.points.length} points)</button>
    </form>}

    {mode==='line'&&lineReady&&<form className="cu-camera-form" onSubmit={saveLine}>
      <label className="cu-stream-field">Line name<input name="name" placeholder="Main entrance entry" required/></label>
      <label>Gate name<input name="role_key" pattern="[a-z0-9][a-z0-9.-]*" placeholder="main-entrance" required/></label>
      <label>This camera detects<select name="direction"><option value="entry">Entry (into the plant)</option><option value="exit">Exit (out of the plant)</option></select></label>
      <div className="cu-tabs"><button type="button" className={insideEndpoint==='a'?'active':''} onClick={()=>setInsideEndpoint('a')}>A is inside ({fmtPoint(draft.points[0])})</button><button type="button" className={insideEndpoint==='b'?'active':''} onClick={()=>setInsideEndpoint('b')}>B is inside ({fmtPoint(draft.points[1])})</button></div>
      <button type="button" className="cu-btn" onClick={resetDraft}>Cancel</button>
      <button className="cu-btn cu-primary">Save line</button>
    </form>}

    <h3>Saved zones &amp; lines</h3>
    {!geometry?.shapes.length&&<p className="cu-empty">Nothing drawn yet. Draw a zone or line on the preview above, then save it.</p>}
    {geometry?.shapes.map(s=><div className="cu-device-row" key={s.shape_id}><div><strong>{s.name}</strong><small>{s.shape_key} · {s.points.length} points{s.role_key?` · gate ${s.role_key}`:''}</small></div><button className="cu-btn" onClick={()=>deleteShape(s.shape_id)}>Remove</button></div>)}
  </div>;
}

export function CameraManagePanel({cameraId,friendlyName,onChanged}){
  const [tab,setTab]=useState('Overview');
  const [camera,setCamera]=useState(null);
  const [notFound,setNotFound]=useState(false);
  const [error,setError]=useState('');
  const load=useCallback(async()=>{
    try{setCamera(await edgeApi(`cameras/${encodeURIComponent(cameraId)}`));setNotFound(false);setError('')}
    catch(e){setCamera(null);setNotFound(true);setError(e.message)}
  },[cameraId]);
  useEffect(()=>{load()},[load]);
  const reload=async()=>{await load();if(onChanged)await onChanged()};
  const act=mutator(reload,setError);

  if(notFound)return <RegisterCameraPanel cameraId={cameraId} friendlyName={friendlyName} onRegistered={reload}/>;
  if(!camera)return <section className="cu-settings-panel"><p>Loading camera configuration…</p></section>;

  return <section className="cu-settings-panel">
    <div className="cu-tabs cu-settings-tabs">{TABS.map(t=><button key={t} className={tab===t?'active':''} onClick={()=>setTab(t)}>{t}</button>)}</div>
    {error&&<p className="cu-notice" role="alert">{error}</p>}
    {tab==='Overview'&&<CameraOverviewTab camera={camera} act={act}/>}
    {tab==='Stream'&&<CameraStreamTab camera={camera} act={act}/>}
    {tab==='Credentials'&&<CameraCredentialsTab camera={camera} act={act}/>}
    {tab==='Zones & Lines'&&<CameraGeometryTab cameraId={camera.camera_id} notifyError={setError}/>}
  </section>;
}
