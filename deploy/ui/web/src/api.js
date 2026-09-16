export const base = location.pathname.startsWith('/dashboard') ? '/dashboard' : '/apexfabricdashboard';
export async function api(path, body) {
  const response = await fetch(`${base}/api/${path}`, {headers: body === undefined ? {} : {'Content-Type':'application/json'}, ...(body === undefined ? {} : {method:'POST',body:JSON.stringify(body)})});
  const data = await response.json().catch(()=>({error:`HTTP ${response.status}`}));
  if (!response.ok) throw Error(data.error || data.detail || `HTTP ${response.status}`);
  return data;
}
export async function job(path, body, report=()=>{}) {
  const result=await api(path,body); if(!result.job_id)return result;
  for(let n=0;n<240;n++) {
    const state=await api(`jobs/${result.job_id}`); report(state);
    const phase=state.state||state.status;
    if(phase==='succeeded')return state;
    if(phase==='failed')throw Error(state.error || state.log?.join('\n') || state.logs?.join('\n') || 'Operation failed');
    await new Promise(resolve=>setTimeout(resolve,1000));
  }
  throw Error('Still running. Check the deployment status before retrying.');
}
export const eventName = event => (event.payload?.event_type || 'Event').replaceAll('_',' ');
export const stamp = event => event.occurred_at || new Date(event.received_at*1000).toISOString();
export const snapshotUrl = snapshot => /^\/api\/telemetry\/snapshots\/[a-f0-9]{64}$/.test(snapshot?.url||'') ? base+snapshot.url : null;
