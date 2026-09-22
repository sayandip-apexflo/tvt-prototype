import React,{useCallback,useEffect,useState} from 'react';
import {Plus,X} from 'lucide-react';
import {edgeApi} from './api';
import {Pill,titleCase} from './shared';
import {useTvtCameras} from './cameras';
import {EnrollmentDesignationControl} from './enrollment';

export function useSolutions(){
  const [solutions,setSolutions]=useState([]);
  const [deployments,setDeployments]=useState([]);
  const [error,setError]=useState('');
  const load=useCallback(async()=>{
    try{
      const [s,d]=await Promise.all([edgeApi('solutions'),edgeApi('deployments')]);
      setSolutions(s);setDeployments(d);setError('');
    }catch(e){setError(e.message)}
  },[]);
  useEffect(()=>{load()},[load]);
  return {solutions,deployments,solutionsError:error,reloadSolutions:load};
}

function DeploymentModal({deployment,solutions,cameras,close,onDeployed}){
  const [catalogId,setCatalogId]=useState(deployment?.catalog_id||solutions[0]?.catalog_id||'');
  const [deploymentId,setDeploymentId]=useState(deployment?.deployment_id||'traffic-v4');
  const [inferenceMode,setInferenceMode]=useState('cpu-compatible');
  const [resources,setResources]=useState({cpu_request:'8',cpu_limit:'16',memory_request:'16Gi',memory_limit:'32Gi',state_size:'50Gi'});
  const [selected,setSelected]=useState({});
  const [apps,setApps]=useState({});
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

  function buildPayload(){
    return {
      catalog_id:catalogId,deployment_id:deploymentId,namespace:'apexfabric',inference_mode:inferenceMode,
      resources:{cpu_request:resources.cpu_request,cpu_limit:resources.cpu_limit,memory_request:resources.memory_request,memory_limit:resources.memory_limit},
      state_size:resources.state_size,
      assignments:cameras.filter(c=>selected[c.camera_id]).map(c=>{
        let config={};
        const value=geometry[c.camera_id]?.trim();
        if(value)config=JSON.parse(value);
        return {camera_id:c.camera_id,apps:apps[c.camera_id]?.length?apps[c.camera_id]:[defaultApp],fps:8,bundle_application:'runtime',config};
      }),
    };
  }
  async function createPreview(){
    try{
      const payload=buildPayload();
      const result=await edgeApi('deployments/preview',payload);
      setPreview(result);setPreviewPayload(payload);setError(null);
    }catch(e){setPreview(null);setPreviewPayload(null);setError(e.message||'Preview failed')}
  }
  async function commit(){
    if(!preview||!previewPayload)return;
    setBusy(true);
    try{
      await edgeApi('deployments',{...previewPayload,preview_bundle_sha256:preview.bundle_sha256,idempotency_key:crypto.randomUUID()});
      await onDeployed();close();
    }catch(e){setError(e.message)}finally{setBusy(false)}
  }
  function toggleCamera(camera,checked){
    setSelected(prev=>({...prev,[camera.camera_id]:checked}));
    if(checked&&!apps[camera.camera_id]?.length)setApps(prev=>({...prev,[camera.camera_id]:[defaultApp]}));
    if(checked&&!geometry[camera.camera_id]){
      edgeApi(`cameras/${encodeURIComponent(camera.camera_id)}/geometry`).then(result=>{
        if(Object.keys(result.compiled_config||{}).length)setGeometry(prev=>prev[camera.camera_id]?prev:{...prev,[camera.camera_id]:JSON.stringify(result.compiled_config,null,2)});
      }).catch(()=>{});
    }
  }

  return <div className="cu-event-scrim" onClick={close}><article className="cu-event-modal" onClick={e=>e.stopPropagation()}>
    <div className="cu-event-modal-head"><div><span className="cu-eyebrow">DEPLOYMENT</span><h2>{deployment?`Configure ${deployment.deployment_id}`:'Deploy Traffic solution'}</h2><p>Choose an available immutable catalog image, camera workloads, resources, and normalized geometry. Preview is required before commit.</p></div><button onClick={close} aria-label="Close"><X/></button></div>

    <form className="cu-camera-form" onSubmit={e=>e.preventDefault()}>
      <label className="cu-stream-field">Catalog entry<select value={catalogId} onChange={e=>setCatalogId(e.target.value)} required>{solutions.map(s=><option key={s.catalog_id} value={s.catalog_id}>{s.solution_name} · {s.version} · {s.hardware_profile}</option>)}</select></label>
      <label>Deployment ID<input value={deploymentId} onChange={e=>setDeploymentId(e.target.value)} disabled={!!deployment} required/></label>
      <label>Inference mode<select value={inferenceMode} onChange={e=>setInferenceMode(e.target.value)}><option value="cpu-compatible">CPU compatible</option><option value="gpu-npu">Intel GPU + NPU</option></select></label>
      <label>CPU request<input value={resources.cpu_request} onChange={e=>setResources({...resources,cpu_request:e.target.value})}/></label>
      <label>CPU limit<input value={resources.cpu_limit} onChange={e=>setResources({...resources,cpu_limit:e.target.value})}/></label>
      <label>Memory request<input value={resources.memory_request} onChange={e=>setResources({...resources,memory_request:e.target.value})}/></label>
      <label>Memory limit<input value={resources.memory_limit} onChange={e=>setResources({...resources,memory_limit:e.target.value})}/></label>
      <label>Persistent state<input value={resources.state_size} onChange={e=>setResources({...resources,state_size:e.target.value})}/></label>
    </form>

    <h3>Cameras</h3>
    {assignableCameras.length?<div className="deployment-camera-list">{assignableCameras.map(c=><div className="cu-device-row deployment-camera-row" key={c.camera_id}>
      <label className="deployment-camera-choice"><input type="checkbox" checked={!!selected[c.camera_id]} onChange={e=>toggleCamera(c,e.target.checked)}/><div><strong>{c.friendly_name}</strong><small>{c.camera_id}</small></div></label>
      {selected[c.camera_id]&&<div className="deployment-camera-detail">
        <div className="deployment-app-choices">{choices.map(choice=><label key={choice}><input type="checkbox" checked={apps[c.camera_id]?.includes(choice)||false} onChange={e=>setApps({...apps,[c.camera_id]:e.target.checked?[...(apps[c.camera_id]||[]),choice]:(apps[c.camera_id]||[]).filter(x=>x!==choice)})}/>{titleCase(choice)}</label>)}</div>
        <label className="deployment-geometry-field"><span>Geometry config (JSON, normalized 0–1 coordinates) — pre-filled from this camera's Zones &amp; Lines tab; edit here only to override</span><textarea value={geometry[c.camera_id]||''} onChange={e=>setGeometry({...geometry,[c.camera_id]:e.target.value})} placeholder='{"lines":{"vehicle_counting":[{"name":"entry","a":[0.1,0.5],"b":[0.9,0.5]}]}}'/></label>
      </div>}
    </div>)}</div>:<p className="cu-empty">No assignable cameras. A camera must be enabled with a configured stream first.</p>}

    {error&&<p className="cu-notice" role="alert">{error}</p>}
    {preview&&<p className="cu-notice">Immutable preview ready — {preview.image_reference} · bundle {preview.bundle_sha256.slice(0,16)}…</p>}

    <div className="cu-table-actions" style={{marginTop:16}}>
      <button type="button" className="cu-btn" onClick={close}>Cancel</button>
      <button type="button" className="cu-btn" disabled={!catalogId||!Object.values(selected).some(Boolean)} onClick={createPreview}>Preview bundle</button>
      <button type="button" className="cu-btn cu-primary" disabled={!preview||busy} onClick={commit}>{busy?'Committing…':'Commit preview'}</button>
    </div>
  </article></div>;
}

export function SolutionsPage({onChanged}){
  const {solutions,deployments,solutionsError,reloadSolutions}=useSolutions();
  const {tvtCameras}=useTvtCameras();
  const [showDeploy,setShowDeploy]=useState(false);
  const [editing,setEditing]=useState(null);
  const [error,setError]=useState('');
  const available=solutions.filter(s=>s.status==='available'&&s.image.digest);

  async function act(promise){
    try{await promise;await reloadSolutions();if(onChanged)await onChanged();return true}
    catch(e){setError(e.message);return false}
  }
  async function onDeployed(){await reloadSolutions();if(onChanged)await onChanged()}

  return <>
    <div className="cu-section-title"><h2>Solutions <b>{deployments.length}</b></h2><button className="cu-btn cu-primary" disabled={!available.length} onClick={()=>{setEditing(null);setShowDeploy(true)}}><Plus size={15}/>Deploy solution</button></div>
    {(solutionsError||error)&&<p className="cu-notice" role="alert">{solutionsError||error}</p>}

    <section className="cu-settings-panel">
      <h2>Catalog</h2>
      {solutions.length?<div className="cu-table-wrap"><table className="cu-table"><thead><tr><th>Solution</th><th>Hardware</th><th>Image</th><th>Status</th></tr></thead>
        <tbody>{solutions.map(s=><tr key={s.catalog_id}><td><strong>{s.solution_name}</strong><small>{s.version}</small></td><td>{s.hardware_profile}</td><td className="mono">{s.image.reference||`${s.image.repository}:${s.image.tag}`}</td><td><Pill value={s.status}/></td></tr>)}</tbody>
      </table></div>:<p className="cu-empty">Catalog is empty. Run the trusted catalog seed and refresh workflow.</p>}
    </section>

    <section className="cu-settings-panel">
      <h2>Deployments</h2>
      {deployments.length?<div className="cu-table-wrap"><table className="cu-table"><thead><tr><th>Deployment</th><th>Lifecycle</th><th>Sync</th><th>Revision</th><th>Image digest</th><th></th></tr></thead>
        <tbody>{deployments.map(d=>{
          const rollbackTarget=d.bundle_history?.find(e=>e.bundle_sha256!==d.desired_bundle_sha256);
          return <React.Fragment key={d.deployment_id}><tr>
            <td><strong>{d.deployment_id}</strong><small>{d.catalog_id||d.solution_id} · {d.namespace}</small></td>
            <td><Pill value={d.lifecycle_intent}/></td>
            <td><Pill value={d.sync_state}/></td>
            <td>{d.applied_revision??'—'} / {d.desired_revision??'—'}<small>applied / desired</small></td>
            <td className="mono">{d.applied_image_digest||'Not applied'}</td>
            <td><div className="cu-table-actions">
              {d.catalog_id&&<button className="cu-btn" onClick={()=>{setEditing(d);setShowDeploy(true)}}>Configure</button>}
              {rollbackTarget&&<button className="cu-btn" onClick={()=>{if(confirm(`Rollback ${d.deployment_id} to revision ${rollbackTarget.desired_revision}?`))act(edgeApi(`deployments/${encodeURIComponent(d.deployment_id)}/rollback`,{bundle_sha256:rollbackTarget.bundle_sha256}))}}>Rollback</button>}
              {d.lifecycle_intent==='Running'
                ?<button className="cu-btn" onClick={()=>{if(confirm(`Stop ${d.deployment_id}?`))act(edgeApi(`deployments/${encodeURIComponent(d.deployment_id)}/stop`,{}))}}>Stop</button>
                :<button className="cu-btn cu-primary" onClick={()=>act(edgeApi(`deployments/${encodeURIComponent(d.deployment_id)}/start`,{}))}>Start</button>}
            </div></td>
          </tr>
          {d.catalog_id&&<tr className="enrollment-designation-row"><td colSpan="6"><EnrollmentDesignationControl deployment={d} tvtCameras={tvtCameras}/></td></tr>}
          </React.Fragment>;
        })}</tbody>
      </table></div>:<p className="cu-empty">No Solution Packs deployed. Select an available catalog entry and preview a deployment.</p>}
    </section>

    <p className="cu-notice">Camera credentials never appear in bundle previews or this UI. Applying assignments creates a new immutable desired revision.</p>
    {showDeploy&&<DeploymentModal deployment={editing} solutions={available} cameras={tvtCameras} close={()=>setShowDeploy(false)} onDeployed={onDeployed}/>}
  </>;
}
