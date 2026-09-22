import React,{useEffect,useMemo,useRef,useState} from 'react';
import {edgeApi} from './api';

const ACTIVE_STATUSES=['activating','capturing','restoring'];
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
        setSession(x.session);setError('');
        if(x.session&&ACTIVE_STATUSES.includes(x.session.status))timer.current=setTimeout(poll,2000);
      }catch(e){if(live){setError(e.message);timer.current=setTimeout(poll,5000)}}
    }
    poll();
    return()=>{live=false;if(timer.current)clearTimeout(timer.current)};
  },[deploymentId]);

  async function pollAgain(){
    try{
      const x=await edgeApi(`deployments/${encodeURIComponent(deploymentId)}/enrollment/status`);
      setSession(x.session);
      if(x.session&&ACTIVE_STATUSES.includes(x.session.status))timer.current=setTimeout(pollAgain,2000);
    }catch(e){setError(e.message);timer.current=setTimeout(pollAgain,5000)}
  }
  async function start(){
    setBusy(true);setError('');
    try{
      setSession(await edgeApi(`deployments/${encodeURIComponent(deploymentId)}/enrollment/sessions`,{}));
      if(timer.current)clearTimeout(timer.current);
      timer.current=setTimeout(pollAgain,2000);
    }catch(e){setError(e.message)}finally{setBusy(false)}
  }
  async function cancel(){
    setBusy(true);setError('');
    try{setSession(await edgeApi(`deployments/${encodeURIComponent(deploymentId)}/enrollment/sessions/${session.session_id}/cancel`,{}))}
    catch(e){setError(e.message)}finally{setBusy(false)}
  }
  async function saveName(e){
    e.preventDefault();setBusy(true);setError('');
    try{
      await edgeApi(`enrollment/people/${session.person_id}/name`,{display_name:name});
      const x=await edgeApi(`deployments/${encodeURIComponent(deploymentId)}/enrollment/status`);
      setSession(x.session);setName('');
    }catch(e){setError(e.message)}finally{setBusy(false)}
  }

  const status=session?.status,active=!!session&&ACTIVE_STATUSES.includes(status);

  return <section className="cu-settings-panel">
    <div className="cu-section-title"><h2>Face enrollment</h2><span>{deploymentId}</span></div>
    <p>This camera is designated for face enrollment. Starting a session switches it to enrollment mode for a short window, then restores it automatically.</p>
    {error&&<p className="cu-notice" role="alert">{error}</p>}
    {!active&&<button className="cu-btn cu-primary" disabled={busy} onClick={start}>{busy?'Starting…':'Start enrollment'}</button>}
    {active&&<p>{STATUS_TEXT[status]}</p>}
    {active&&status==='capturing'&&<button className="cu-btn" disabled={busy} onClick={cancel}>Cancel</button>}
    {session?.capture_result&&<p className="cu-notice" role="status">{session.capture_result==='created'?'Face captured — new person.':'Face captured — matched an existing person.'}</p>}
    {!active&&session?.result_code&&session.result_code!=='ok'&&<p className="cu-notice">{RESULT_TEXT[session.result_code]||`Enrollment ${session.result_code.replaceAll('_',' ')}.`}</p>}
    {session?.naming_status==='pending_name'&&<form className="cu-site-form" onSubmit={saveName}>
      <label>Name this person<input value={name} onChange={e=>setName(e.target.value)} required maxLength={160} placeholder="e.g. Jane Doe"/></label>
      <button className="cu-btn cu-primary" disabled={busy}>{busy?'Saving…':'Save name'}</button>
    </form>}
    {session?.naming_status==='named'&&<p className="cu-notice" role="status">Name saved.</p>}
  </section>;
}
