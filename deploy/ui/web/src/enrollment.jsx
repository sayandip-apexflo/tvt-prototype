import React,{useEffect,useRef,useState} from 'react';
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

export function EnrollmentPanel({camera}){
  const deploymentId=camera.assigned_to?.[0];
  const [designatedCameraId,setDesignatedCameraId]=useState(undefined);
  const [session,setSession]=useState(null);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  const [name,setName]=useState('');
  const timer=useRef(null);

  useEffect(()=>{
    setDesignatedCameraId(undefined);setSession(null);
    if(!deploymentId)return;
    let live=true;
    edgeApi(`deployments/${encodeURIComponent(deploymentId)}/enrollment/camera`)
      .then(x=>{if(live)setDesignatedCameraId(x.camera_id||null)})
      .catch(e=>{if(live)setError(e.message)});
    return()=>{live=false};
  },[deploymentId]);

  useEffect(()=>{
    if(timer.current)clearTimeout(timer.current);
    if(!deploymentId||designatedCameraId!==camera.camera_id)return;
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
  },[deploymentId,designatedCameraId,camera.camera_id]);

  if(designatedCameraId===undefined||designatedCameraId!==camera.camera_id)return null;

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
    <div className="cu-section-title"><h2>Face enrollment</h2></div>
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
