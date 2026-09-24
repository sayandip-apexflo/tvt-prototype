import React,{useCallback,useEffect,useState} from 'react';
import {X} from 'lucide-react';
import {edgeApi} from './api';
import {titleCase} from './shared';

export function useSolutions(){
  const [solutions,setSolutions]=useState([]);
  const [error,setError]=useState('');
  const load=useCallback(async()=>{
    try{setSolutions(await edgeApi('solutions'));setError('')}
    catch(e){setError(e.message)}
  },[]);
  useEffect(()=>{load()},[load]);
  return {solutions,solutionsError:error,reloadSolutions:load};
}

// crypto.randomUUID() requires a secure context (HTTPS or localhost); this
// dashboard is served over plain HTTP on a LAN hostname by design, so use
// getRandomValues (unrestricted) instead.
function randomId(){
  const bytes=crypto.getRandomValues(new Uint8Array(16));
  bytes[6]=(bytes[6]&0x0f)|0x40;bytes[8]=(bytes[8]&0x3f)|0x80;
  const hex=[...bytes].map(b=>b.toString(16).padStart(2,'0'));
  return `${hex.slice(0,4).join('')}-${hex.slice(4,6).join('')}-${hex.slice(6,8).join('')}-${hex.slice(8,10).join('')}-${hex.slice(10,16).join('')}`;
}

export function DeploymentModal({solutions,cameras,close,onDeployed}){
  const [catalogId,setCatalogId]=useState(solutions[0]?.catalog_id||'');
  const [deploymentId,setDeploymentId]=useState('traffic-v4');
  const [inferenceMode,setInferenceMode]=useState('cpu-compatible');
  const [resources,setResources]=useState({cpu_request:'8',cpu_limit:'16',memory_request:'16Gi',memory_limit:'32Gi',state_size:'50Gi'});
  const [selected,setSelected]=useState({});
  const [apps,setApps]=useState({});
  const [fps]=useState({});
  const [geometry,setGeometry]=useState({});
  const [preview,setPreview]=useState(null);
  const [previewPayload,setPreviewPayload]=useState(null);
  const [error,setError]=useState(null);
  const [busy,setBusy]=useState(false);

  const selectedCatalog=solutions.find(s=>s.catalog_id===catalogId);
  const catalogCamera=selectedCatalog?.contract?.ui?.camera;
  // face_enrollment is a temporary operational mode from the camera drawer's
  // Enrollment control, never a co-selectable deployment app.
  const choices=(catalogCamera?.apps&&typeof catalogCamera.apps==='object'?Object.keys(catalogCamera.apps):['anpr','vehicle_counting','pedestrian_counting','wrong_way','illegal_parking']).filter(a=>a!=='face_enrollment');
  const defaultApp=typeof catalogCamera?.defaultApp==='string'&&choices.includes(catalogCamera.defaultApp)?catalogCamera.defaultApp:choices[0];
  const assignableCameras=cameras.filter(c=>c.enabled&&c.configured);

  async function loadGeometry(cameraId){
    setGeometry(prev=>({...prev,[cameraId]:{loading:true}}));
    try{
      const result=await edgeApi(`cameras/${encodeURIComponent(cameraId)}/geometry`);
      setGeometry(prev=>({...prev,[cameraId]:{...result,loading:false}}));
      return result.compiled_config||{};
    }catch(e){
      setGeometry(prev=>({...prev,[cameraId]:{loading:false,error:e.message}}));
      throw e;
    }
  }
  async function buildPayload(){
    const selectedCameras=cameras.filter(c=>selected[c.camera_id]);
    const compiled=await Promise.all(selectedCameras.map(c=>loadGeometry(c.camera_id)));
    return {
      catalog_id:catalogId,deployment_id:deploymentId,namespace:'apexfabric',inference_mode:inferenceMode,
      resources:{cpu_request:resources.cpu_request,cpu_limit:resources.cpu_limit,memory_request:resources.memory_request,memory_limit:resources.memory_limit},
      state_size:resources.state_size,
      assignments:selectedCameras.map((c,index)=>({
        camera_id:c.camera_id,
        apps:apps[c.camera_id]?.length?apps[c.camera_id]:[defaultApp],
        fps:fps[c.camera_id]||8,
        bundle_application:'runtime',
        config:compiled[index],
      })),
    };
  }
  async function createPreview(){
    try{
      const payload=await buildPayload();
      const result=await edgeApi('deployments/preview',payload);
      setPreview(result);setPreviewPayload(payload);setError(null);
    }catch(e){setPreview(null);setPreviewPayload(null);setError(e.message||'Preview failed')}
  }
  async function commit(){
    if(!preview||!previewPayload)return;
    setBusy(true);
    try{
      await edgeApi('deployments',{...previewPayload,preview_bundle_sha256:preview.bundle_sha256,idempotency_key:randomId()});
      await onDeployed();close();
    }catch(e){setError(e.message)}finally{setBusy(false)}
  }
  function toggleCamera(camera,checked){
    setSelected(prev=>({...prev,[camera.camera_id]:checked}));
    setPreview(null);setPreviewPayload(null);
    if(checked&&!apps[camera.camera_id]?.length)setApps(prev=>({...prev,[camera.camera_id]:[defaultApp]}));
    if(checked)loadGeometry(camera.camera_id).catch(e=>setError(e.message));
  }

  return <div className="cu-event-scrim" onClick={close}><article className="cu-event-modal" onClick={e=>e.stopPropagation()}>
    <div className="cu-event-modal-head"><div><span className="cu-eyebrow">DEPLOYMENT</span><h2>Deploy solution</h2><p>Choose a solution and the cameras it should run on. Camera geometry is loaded from each camera's Zones &amp; Lines tab when you preview.</p></div><button onClick={close} aria-label="Close"><X/></button></div>

    <form className="cu-camera-form" onSubmit={e=>e.preventDefault()}>
      <label className="cu-stream-field">Catalog entry<select value={catalogId} onChange={e=>setCatalogId(e.target.value)} required>{solutions.map(s=><option key={s.catalog_id} value={s.catalog_id}>{s.solution_name} · {s.version}</option>)}</select></label>
      <label>Deployment ID<input value={deploymentId} onChange={e=>setDeploymentId(e.target.value)} required/></label>
    </form>
    {/* Hardware/resource tuning stays available but collapsed: site users
        deploy with the defaults and never need to see infrastructure detail. */}
    <details className="deployment-advanced">
      <summary>Advanced settings</summary>
      <div className="cu-camera-form">
        <label>Inference mode<select value={inferenceMode} onChange={e=>setInferenceMode(e.target.value)}><option value="cpu-compatible">CPU compatible</option><option value="gpu-npu">Intel GPU + NPU</option></select></label>
        <label>CPU request<input value={resources.cpu_request} onChange={e=>setResources({...resources,cpu_request:e.target.value})}/></label>
        <label>CPU limit<input value={resources.cpu_limit} onChange={e=>setResources({...resources,cpu_limit:e.target.value})}/></label>
        <label>Memory request<input value={resources.memory_request} onChange={e=>setResources({...resources,memory_request:e.target.value})}/></label>
        <label>Memory limit<input value={resources.memory_limit} onChange={e=>setResources({...resources,memory_limit:e.target.value})}/></label>
        <label>Persistent state<input value={resources.state_size} onChange={e=>setResources({...resources,state_size:e.target.value})}/></label>
      </div>
    </details>

    <h3>Cameras</h3>
    {assignableCameras.length?<div className="deployment-camera-list">{assignableCameras.map(c=><div className="cu-device-row deployment-camera-row" key={c.camera_id}>
      <label className="deployment-camera-choice"><input type="checkbox" checked={!!selected[c.camera_id]} onChange={e=>toggleCamera(c,e.target.checked)}/><div><strong>{c.friendly_name}</strong><small>{c.camera_id}</small></div></label>
      {selected[c.camera_id]&&<div className="deployment-camera-detail">
        <div className="deployment-app-choices">{choices.map(choice=><label key={choice}><input type="checkbox" checked={apps[c.camera_id]?.includes(choice)||false} onChange={e=>setApps({...apps,[c.camera_id]:e.target.checked?[...(apps[c.camera_id]||[]),choice]:(apps[c.camera_id]||[]).filter(x=>x!==choice)})}/>{titleCase(choice)}</label>)}</div>
        <p className="deployment-geometry-summary">{geometry[c.camera_id]?.loading?'Loading camera geometry…':geometry[c.camera_id]?.error?`Geometry unavailable: ${geometry[c.camera_id].error}`:`Geometry managed in Zones & Lines · ${geometry[c.camera_id]?.shapes?.length??0} saved shapes · revision ${geometry[c.camera_id]?.geometry_revision??0}`}</p>
      </div>}
    </div>)}</div>:<p className="cu-empty">No assignable cameras. A camera must be enabled with a configured stream first.</p>}

    {error&&<p className="cu-notice" role="alert">{error}</p>}
    {preview&&<p className="cu-notice">Ready to deploy {selectedCatalog?.solution_name} {selectedCatalog?.version} to {previewPayload.assignments.length} camera{previewPayload.assignments.length===1?'':'s'}.</p>}

    <div className="cu-table-actions" style={{marginTop:16}}>
      <button type="button" className="cu-btn" onClick={close}>Cancel</button>
      <button type="button" className="cu-btn" disabled={!catalogId||!Object.values(selected).some(Boolean)} onClick={createPreview}>Preview deployment</button>
      <button type="button" className="cu-btn cu-primary" disabled={!preview||busy} onClick={commit}>{busy?'Deploying…':'Deploy'}</button>
    </div>
  </article></div>;
}
