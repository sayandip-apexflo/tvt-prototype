import React,{useEffect,useState,useRef} from 'react';
import {api,base,formatIST,formatISTTime} from './api';
export {api,formatIST,formatISTTime};
export function useSite(){
 const [data,setData]=useState(null),[error,setError]=useState('');
 const busy=useRef(false);
 async function refresh(){if(busy.current)return;busy.current=true;try{
  const [site,events]=await Promise.all([api('customer'),api('telemetry/events?limit=1000')]);
  setData({...site,events:events.events});setError('');
 }catch(e){setError(e.message)}finally{busy.current=false}}
 useEffect(()=>{refresh();const t=setInterval(()=>{if(!document.hidden)refresh()},5000);return()=>clearInterval(t)},[]);
 return {data,error,refresh};
}
export function useTheme(){const [theme,setTheme]=useState(document.documentElement.dataset.theme||'light');useEffect(()=>{document.documentElement.dataset.theme=theme;try{localStorage.setItem('apexfabric-theme',theme)}catch{}},[theme]);return[theme,()=>setTheme(t=>t==='dark'?'light':'dark')]}
export function Pill({value,label}){const v=(value??'').toString().toLowerCase();return <span className={`cu-pill cu-pill-${v}`}>{label??value}</span>}
export function relativeTime(value){if(!value)return 'Never';const seconds=Math.round((Date.now()-new Date(value).getTime())/1000);if(seconds<60)return `${Math.max(0,seconds)}s ago`;if(seconds<3600)return `${Math.round(seconds/60)}m ago`;if(seconds<86400)return `${Math.round(seconds/3600)}h ago`;return `${Math.round(seconds/86400)}d ago`}
export const titleCase=v=>(v||'').replaceAll('_',' ').replace(/\b\w/g,c=>c.toUpperCase());
export function Feed({cameraId}){
 const [image,setImage]=useState(null),[error,setError]=useState(''),[time,setTime]=useState('');
 useEffect(()=>{let stopped=false,url=null,busy=false;const controller=new AbortController();
 async function frame(){if(busy||document.hidden)return;busy=true;try{const r=await fetch(`${base}/api/cameras/snapshot?camera_id=${encodeURIComponent(cameraId)}`,{signal:controller.signal});if(!r.ok)throw Error('Camera frame unavailable');const blob=await r.blob();if(stopped)return;if(url)URL.revokeObjectURL(url);url=URL.createObjectURL(blob);setImage(url);setTime(formatISTTime(new Date()));setError('')}catch(e){if(!stopped){setError(e.message);setImage(null)}}finally{busy=false}}
 setImage(null);frame();const timer=setInterval(frame,5000);return()=>{stopped=true;controller.abort();clearInterval(timer);if(url)URL.revokeObjectURL(url)}},[cameraId]);
 return <div className="real-feed">{image?<img src={image} alt={`Camera ${cameraId}`}/>:<div className="feed-empty">{error||'Connecting to camera…'}</div>}<span>{image?`Latest frame ${time} · refreshes every 5 seconds`:error}</span></div>
}
export function CameraForm({camera,onSaved}){
 const [error,setError]=useState(''),[busy,setBusy]=useState(false);
 return <form className="cu-camera-form" onSubmit={async e=>{e.preventDefault();setBusy(true);setError('');const v=Object.fromEntries(new FormData(e.currentTarget));if(!v.rtsp_url)delete v.rtsp_url;try{await api('cameras',v);e.target.reset();onSaved()}catch(e){setError(e.message)}finally{setBusy(false)}}}>
 <label>Camera name<input name="name" required maxLength={80} defaultValue={camera?.name}/></label><label>Camera ID<input name="camera_id" required pattern="[a-z0-9]([-a-z0-9]*[a-z0-9])?" maxLength={63} readOnly={!!camera} defaultValue={camera?.camera_id}/></label><label>RTSP stream<input name="rtsp_url" type="password" required={!camera} autoComplete="new-password" placeholder={camera?'Leave blank to keep current stream':'rtsp://camera/stream'}/></label><p>Stream credentials are stored in a Kubernetes Secret on this box.</p>{error&&<p role="alert">{error}</p>}<button className="cu-btn cu-primary" disabled={busy}>Save camera</button></form>
}
export function Rules({data,refresh}){
 const [chosen,setChosen]=useState(''),[edit,setEdit]=useState(null),[error,setError]=useState(''),[busy,setBusy]=useState(false);
 const template=data.templates.find(t=>t.id===chosen),rule=edit||template?.rule;
 async function mutate(body){setBusy(true);setError('');try{await api('alert-rules',body);setEdit(null);await refresh()}catch(e){setError(e.message)}finally{setBusy(false)}}
 return <section className="cu-settings-panel"><h2>Alert configuration</h2><p>Rules run on the box and apply to new events. Notifications appear in this dashboard.</p>{error&&<p role="alert">{error}</p>}
 <label>Alert type<select value={chosen} onChange={e=>{setChosen(e.target.value);setEdit(null)}}><option value="">Choose an alert type</option>{data.templates.map(t=><option key={t.id} value={t.id}>{t.label}</option>)}</select></label>
 {template&&<p>{template.description}{!template.available?' Requires a compatible event producer; this rule will wait for matching events.':''}</p>}
 {rule&&<form key={edit?.id||chosen} onSubmit={e=>{e.preventDefault();const values=Object.fromEntries(new FormData(e.currentTarget));mutate({rule:{...rule,...values,enabled:edit?.enabled??true,cooldown_seconds:Number(values.cooldown_seconds),threshold:rule.operator==='event'?null:rule.operator==='plate_equals'?values.threshold:Number(values.threshold)}})}}>
 <label>Rule name<input name="name" required defaultValue={rule.name||template?.label}/></label>
 <label>Camera<select name="camera_id" defaultValue={rule.camera_id||''}><option value="">All cameras</option>{data.cameras.map(c=><option key={c.camera_id} value={c.camera_id}>{c.name}</option>)}</select></label>
 <label>Deployment<select name="deployment_id" defaultValue={rule.deployment_id||''}><option value="">All deployments</option>{data.deployments.map(d=><option key={d.name}>{d.name}</option>)}</select></label>
 {rule.operator!=='event'&&<label>{rule.operator==='plate_equals'?'Plate number':'Trigger above'}<input name="threshold" required type={rule.operator==='plate_equals'?'text':'number'} step="any" defaultValue={rule.threshold}/></label>}
 <label>Cooldown (seconds)<input name="cooldown_seconds" type="number" min="0" max="86400" defaultValue={rule.cooldown_seconds??300} required/></label><button className="cu-btn cu-primary" disabled={busy}>Save rule</button></form>}
 {data.rules.map(r=><div className="cu-rule" key={r.id}><div className="cu-rule-heading"><h3>{r.name}</h3><button disabled={busy} role="switch" aria-label={r.name} aria-checked={r.enabled} className={`cu-toggle ${r.enabled?'on':''}`} onClick={()=>mutate({rule:{...r,enabled:!r.enabled}})}><span/></button></div><p>{r.event_type} · {r.camera_id||'All cameras'} · {r.operator==='event'?'On detection':`${r.operator==='plate_equals'?'Plate matches':r.operator} ${r.threshold}`} · cooldown {r.cooldown_seconds}s</p><button className="cu-btn" onClick={()=>setEdit(r)}>Edit</button> <button className="cu-btn" disabled={busy} onClick={()=>{if(confirm(`Delete ${r.name}?`))mutate({action:'delete',id:r.id})}}>Delete</button></div>)}
 {!data.rules.length&&<p>No rules enabled yet. Choose a template to create one.</p>}</section>
}
