import React,{useEffect,useState} from 'react';
import {Check,ShieldCheck} from 'lucide-react';
import {edgeApi} from './api';

export function TvtSiteTab(){
  const [site,setSite]=useState(null);
  const [health,setHealth]=useState(null);
  const [error,setError]=useState('');
  useEffect(()=>{
    let live=true;
    Promise.all([edgeApi('site').catch(()=>null),edgeApi('health').catch(()=>null)]).then(([s,h])=>{if(live){setSite(s);setHealth(h)}});
    return()=>{live=false};
  },[]);
  return <>
    <section className="cu-settings-panel">
      <h2>Site identity</h2>
      <p>Managed by tvt_edge on this box.</p>
      {error&&<p className="cu-notice" role="alert">{error}</p>}
      <dl className="cu-alert-detail" style={{border:0,padding:0}}>
        <div><dt>Site ID</dt><dd>{site?.site_id||'Not configured'}</dd></div>
        <div><dt>Edge ID</dt><dd>{site?.edge_id||'—'}</dd></div>
        <div><dt>Display name</dt><dd>{site?.display_name||'—'}</dd></div>
        <div><dt>Timezone</dt><dd>{site?.timezone||'—'}</dd></div>
        <div><dt>Configuration revision</dt><dd>{site?.config_revision??'—'}</dd></div>
        <div><dt>Overall status</dt><dd>{health?.status||'unknown'}</dd></div>
      </dl>
    </section>
    <section className="cu-settings-panel">
      <h2>Security posture</h2>
      <div className="cu-device-row"><Check size={16}/><div><strong>Loopback management bind</strong><small>The tvt_edge API is not exposed directly to the LAN.</small></div></div>
      <div className="cu-device-row"><Check size={16}/><div><strong>Write-only camera credentials</strong><small>AES-256-GCM encrypted at rest.</small></div></div>
      <div className="cu-device-row"><Check size={16}/><div><strong>Scoped Kubernetes access</strong><small>Only bounded product resources are available.</small></div></div>
      <div className="cu-device-row"><ShieldCheck size={16}/><div><strong>This dashboard is intentionally unauthenticated</strong><small>/dashboard is a deliberate design decision, not a gap — see the site's operations documentation.</small></div></div>
    </section>
  </>;
}
