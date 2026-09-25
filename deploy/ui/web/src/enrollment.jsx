import React,{useEffect,useMemo,useRef,useState} from 'react';
import {edgeApi} from './api';

const ACTIVE_STATUSES=['activating','capturing','restoring'];
const UNNAMED_DISCARDED='No record created for unnamed person';
// Keep polling while capturing or while a captured face waits for a name
// (the server discards it after the naming timeout).
const needsPolling=session=>!!session&&(ACTIVE_STATUSES.includes(session.status)||session.naming_status==='pending_name');
const STATUS_TEXT={
  activating:'Switching the camera to enrollment mode…',
  capturing:'Ready — ask the person to look directly at the camera.',
  restoring:'Restoring the camera to its prior configuration…',
};
const RESULT_TEXT={
  timed_out:'No face was captured before the capture window closed.',
  cancelled:'Enrollment was cancelled.',
  activation_failed:'The camera could not be switched into enrollment mode.',
};
const OBSERVER_TEXT={
  sync_queued:'Enrollment change queued',
  claimed:'Synchronizer accepted the change',
  pulling_images:'Checking the vision runtime image',
  applying_secrets:'Updating the runtime configuration',
  applying_bundle:'Reconciling the vision workload',
  waiting_runtime_configuration:'Waiting for the runtime to load the new revision',
  waiting_deployment_rollout:'Waiting for the vision workload to become ready',
  restarting_deployments:'Restarting the vision runtime',
  completed:'Configuration applied',
  waiting_for_face:'Runtime ready — waiting for a face event',
  capture_rejected:'A face event arrived but did not pass enrollment checks',
  restore_queued:'Restoration queued',
  timed_out:'Enrollment timed out and the camera was restored',
  cancelled:'Enrollment cancelled and the camera was restored',
  failed:'Enrollment failed',
};

export function EnrollmentDesignationControl({deployment,tvtCameras}){
  const deploymentId=deployment.deployment_id;
  const eligible=useMemo(()=>tvtCameras.filter(camera=>camera.assignments?.some(assignment=>
    assignment.deployment_id===deploymentId&&assignment.apps?.includes('face_recognition')
  )),[deploymentId,tvtCameras]);
  const eligibleKey=eligible.map(camera=>camera.camera_id).join('\n');
  const [designation,setDesignation]=useState(undefined);
  const [status,setStatus]=useState(null);
  const [selected,setSelected]=useState('');
  const [loading,setLoading]=useState(true);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  const [message,setMessage]=useState('');

  useEffect(()=>{
    let live=true;
    setLoading(true);setError('');setMessage('');
    Promise.all([
      edgeApi(`deployments/${encodeURIComponent(deploymentId)}/enrollment/camera`),
      edgeApi(`deployments/${encodeURIComponent(deploymentId)}/enrollment/status`),
    ]).then(([camera,currentStatus])=>{
      if(!live)return;
      const cameraId=camera.camera_id||null;
      setDesignation(cameraId);setStatus(currentStatus);
      setSelected(eligible.some(item=>item.camera_id===cameraId)?cameraId:eligible[0]?.camera_id||'');
    }).catch(e=>{if(live)setError(e.message)}).finally(()=>{if(live)setLoading(false)});
    return()=>{live=false};
  },[deploymentId,eligibleKey]);

  useEffect(()=>{
    setSelected(current=>eligible.some(camera=>camera.camera_id===current)?current:eligible[0]?.camera_id||'');
  },[eligibleKey]);

  const active=ACTIVE_STATUSES.includes(status?.session?.status);
  useEffect(()=>{
    if(!active)return;
    let live=true;
    const timer=setInterval(()=>{
      edgeApi(`deployments/${encodeURIComponent(deploymentId)}/enrollment/status`)
        .then(value=>{if(live)setStatus(value)})
        .catch(e=>{if(live)setError(e.message)});
    },2000);
    return()=>{live=false;clearInterval(timer)};
  },[active,deploymentId]);

  async function save(e){
    e.preventDefault();
    if(!selected||active)return;
    setBusy(true);setError('');setMessage('');
    try{
      const result=await edgeApi(`deployments/${encodeURIComponent(deploymentId)}/enrollment/camera`,{camera_id:selected});
      setDesignation(result.camera_id);
      setMessage('Enrollment camera saved.');
    }catch(e){setError(e.message)}finally{setBusy(false)}
  }

  const current=tvtCameras.find(camera=>camera.camera_id===designation);
  return <div className="enrollment-designation" data-deployment-id={deploymentId}>
    <div className="enrollment-designation-copy">
      <strong>Enrollment camera</strong>
      {loading?<small>Loading designation…</small>:designation?<small>Current: {current?.friendly_name||designation} · {designation}</small>:<small>No camera designated.</small>}
      <p>ANPR and face recognition run normally. During an enrollment session they are temporarily replaced by face enrollment, then restored.</p>
    </div>
    {eligible.length?<form onSubmit={save}>
      <label>Camera
        <select aria-label={`Enrollment camera for ${deploymentId}`} value={selected} disabled={loading||busy||active} onChange={e=>{setSelected(e.target.value);setMessage('')}}>
          {eligible.map(camera=><option key={camera.camera_id} value={camera.camera_id}>{camera.friendly_name} · {camera.camera_id}</option>)}
        </select>
      </label>
      <button className="cu-btn cu-primary" disabled={loading||busy||active||!selected||selected===designation}>{busy?'Saving…':designation?'Change enrollment camera':'Designate enrollment camera'}</button>
    </form>:<p className="enrollment-designation-empty">Assign Face recognition to a camera first.</p>}
    {active&&<p className="cu-notice" role="status">An enrollment session is {status.session.status}; the designation cannot be changed yet.</p>}
    {error&&<p className="cu-notice" role="alert">{error}</p>}
    {message&&<p className="cu-notice" role="status">{message}</p>}
  </div>;
}

function EnrollmentDesignationMatch({deploymentId,cameraId}){
  const [designated,setDesignated]=useState(undefined);
  useEffect(()=>{
    let live=true;
    setDesignated(undefined);
    edgeApi(`deployments/${encodeURIComponent(deploymentId)}/enrollment/camera`)
      .then(value=>{if(live)setDesignated(value.camera_id===cameraId)})
      .catch(()=>{if(live)setDesignated(false)});
    return()=>{live=false};
  },[deploymentId,cameraId]);
  return designated?<EnrollmentDeploymentPanel deploymentId={deploymentId}/>:null;
}

export function EnrollmentPanel({camera}){
  const deploymentIds=[...new Set((camera.tvt?.assignments||[]).map(assignment=>assignment.deployment_id).filter(Boolean))];
  return deploymentIds.map(deploymentId=><EnrollmentDesignationMatch key={deploymentId} deploymentId={deploymentId} cameraId={camera.camera_id}/>);
}

export function EnrollmentDeploymentPanel({deploymentId}){
  const [session,setSession]=useState(null);
  const [observer,setObserver]=useState(null);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  const [name,setName]=useState('');
  const timer=useRef(null);

  useEffect(()=>{
    if(timer.current)clearTimeout(timer.current);
    let live=true;
    async function poll(){
      try{
        const x=await edgeApi(`deployments/${encodeURIComponent(deploymentId)}/enrollment/status`);
        if(!live)return;
        setSession(x.session);setObserver(x.observer||null);setError('');
        if(needsPolling(x.session))timer.current=setTimeout(poll,2000);
      }catch(e){if(live){setError(e.message);timer.current=setTimeout(poll,5000)}}
    }
    poll();
    return()=>{live=false;if(timer.current)clearTimeout(timer.current)};
  },[deploymentId]);

  async function pollAgain(){
    if(timer.current)clearTimeout(timer.current);
    try{
      const x=await edgeApi(`deployments/${encodeURIComponent(deploymentId)}/enrollment/status`);
      setSession(x.session);setObserver(x.observer||null);
      if(needsPolling(x.session))timer.current=setTimeout(pollAgain,2000);
    }catch(e){setError(e.message);timer.current=setTimeout(pollAgain,5000)}
  }
  async function start(){
    setBusy(true);setError('');
    try{
      setSession(await edgeApi(`deployments/${encodeURIComponent(deploymentId)}/enrollment/sessions`,{}));setObserver(null);
      if(timer.current)clearTimeout(timer.current);
      timer.current=setTimeout(pollAgain,2000);
    }catch(e){setError(e.message)}finally{setBusy(false)}
  }
  // Stopping before the person is named never creates a record; the API
  // answers with the "No record created for unnamed person" error.
  async function stop(){
    setBusy(true);setError('');
    try{setSession(await edgeApi(`deployments/${encodeURIComponent(deploymentId)}/enrollment/sessions/${session.session_id}/cancel`,{}))}
    catch(e){setError(e.message)}
    finally{setBusy(false);await pollAgain()}
  }
  async function saveName(e){
    e.preventDefault();setBusy(true);setError('');
    try{
      setSession(await edgeApi(`deployments/${encodeURIComponent(deploymentId)}/enrollment/sessions/${session.session_id}/name`,{display_name:name}));setName('');
    }catch(e){setError(e.message)}
    finally{setBusy(false);await pollAgain()}
  }

  const status=session?.status,active=!!session&&ACTIVE_STATUSES.includes(status);
  const awaitingName=session?.naming_status==='pending_name';
  const discarded=session?.naming_status==='discarded';

  return <section className="cu-settings-panel">
    <div className="cu-section-title"><h2>Face enrollment</h2><span>{deploymentId}</span></div>
    <p>This camera is designated for face enrollment. Starting a session switches it to enrollment mode for a short window, captures a few frames of the person, then restores it automatically. The person is enrolled only once you name them.</p>
    {error&&<p className="cu-notice" role="alert">{error}</p>}
    {!active&&!awaitingName&&<button className="cu-btn cu-primary" disabled={busy} onClick={start}>{busy?'Starting…':'Start enrollment'}</button>}
    {active&&<p>{STATUS_TEXT[status]}</p>}
    {observer&&<div className="enrollment-observer" role="status">
      <strong>{OBSERVER_TEXT[observer.stage]||observer.stage.replaceAll('_',' ')}</strong>
      <small>Runtime: {observer.runtime_workload}</small>
      {observer.target_revision!=null&&<small>Revision {observer.applied_revision??'—'} / {observer.target_revision}</small>}
      {observer.safe_reason&&<small>Diagnostic: {observer.safe_reason.replaceAll('_',' ')}</small>}
      {!!observer.timeline?.length&&<ol>{observer.timeline.map((item,index)=><li key={`${item.stage}-${index}`}><span>{OBSERVER_TEXT[item.stage]||item.stage.replaceAll('_',' ')}</span><time>{new Date(item.occurred_at).toLocaleTimeString()}</time></li>)}</ol>}
    </div>}
    {awaitingName&&<p className="cu-notice" role="status">Face captured ({session.capture_count} {session.capture_count===1?'frame':'frames'}). Enter a name to enroll this person — no record is created until you do.</p>}
    {session?.capture_result==='duplicate'&&<p className="cu-notice" role="status">This face is already enrolled — no new record created.</p>}
    {!active&&session?.result_code&&session.result_code!=='ok'&&<p className="cu-notice">{RESULT_TEXT[session.result_code]||`Enrollment ${session.result_code.replaceAll('_',' ')}.`}</p>}
    {discarded&&!error&&<p className="cu-notice" role="alert">{UNNAMED_DISCARDED}</p>}
    {awaitingName&&<form className="cu-site-form" onSubmit={saveName}>
      <label>Name this person<input value={name} onChange={e=>setName(e.target.value)} required maxLength={160} placeholder="e.g. Jane Doe"/></label>
      <button className="cu-btn cu-primary" disabled={busy}>{busy?'Saving…':'Save name'}</button>
    </form>}
    {(status==='capturing'||awaitingName)&&<button className="cu-btn" disabled={busy} onClick={stop}>Stop enrollment</button>}
    {session?.naming_status==='named'&&<p className="cu-notice" role="status">Name saved.</p>}
  </section>;
}
