(() => {
'use strict';
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const api = async path => { const r = await fetch('/dashboard/api/' + path); if (!r.ok) throw Error('Site data is unavailable. Please retry.'); return r.json(); };
let refreshing = false, frameBusy = false, frameUrl = null;
async function refresh() {
  if (refreshing || document.hidden) return;
  refreshing = true;
  try {
    const [site, telemetry] = await Promise.all([api('customer'), api('telemetry/events?limit=100')]);
    window.dispatchEvent(new CustomEvent('apexfabric-events', {detail:telemetry.events}));
    $('site').textContent = site.site_id;
    $('error').textContent = '';
    const deployments = new Map(site.deployments.map(d => [d.name, d]));
    $('cameras').innerHTML = site.cameras.map(c => `<article class="camera-tile"><h3>${esc(c.name)}</h3><small>${esc(c.camera_id)}</small>${c.assigned_to.length ? c.assigned_to.map(name => { const d = deployments.get(name); return `<p><strong>${esc(d?.solution || name)}</strong><br>${esc(d?.status || 'Status unavailable')}</p>`; }).join('') : '<p>No solution assigned</p>'}</article>`).join('') || '<p>No cameras configured. Add cameras in the management UI.</p>';
    const selection = $('camera-select').value;
    $('camera-select').innerHTML = '<option value="">Select a camera</option>' + site.cameras.filter(c => c.has_source).map(c => `<option value="${esc(c.camera_id)}">${esc(c.name)}</option>`).join('');
    if (site.cameras.some(c => c.camera_id === selection && c.has_source)) $('camera-select').value = selection;
    else if (selection) clearFrame();
    $('events').innerHTML = telemetry.events.map(e => {
      const p = e.payload || {}, type = p.event_type || p.type || p.event || 'Event';
      const snapshot = e.snapshots?.find(s => /^\/api\/telemetry\/snapshots\/[a-f0-9]{64}$/.test(s.url));
      return `<article><strong>${esc(typeof type === 'string' ? type.replaceAll('_', ' ') : 'Event')}</strong><p>${esc(p.camera_id || p.camera?.camera_id || '')} · ${esc(e.deployment_id)}</p><small>${esc(e.occurred_at || e.received_at)}</small>${snapshot ? `<img loading="lazy" src="/dashboard${esc(snapshot.url)}" alt="Event snapshot">` : ''}<details><summary>Event details</summary><pre>${esc(JSON.stringify(p, null, 2))}</pre></details></article>`;
    }).join('') || '<p>No events received yet.</p>';
  } catch (error) { $('error').textContent = error.message; }
  finally { refreshing = false; }
}
function clearFrame() {
  if (frameUrl) URL.revokeObjectURL(frameUrl);
  frameUrl = null; $('preview').hidden = true; $('preview').removeAttribute('src'); $('preview-empty').hidden = false;
}
async function frame() {
  if (frameBusy || document.hidden) return;
  const camera = $('camera-select').value;
  if (!camera) { clearFrame(); return; }
  frameBusy = true;
  try {
    const result = await fetch('/dashboard/api/cameras/snapshot?camera_id=' + encodeURIComponent(camera));
    if (!result.ok) throw Error('Camera frame unavailable; check camera connectivity.');
    const blob = await result.blob();
    if ($('camera-select').value !== camera) return;
    clearFrame(); frameUrl = URL.createObjectURL(blob);
    $('preview').src = frameUrl; $('preview').hidden = false; $('preview-empty').hidden = true;
    $('frame-status').textContent = 'Latest frame: ' + new Date().toLocaleTimeString() + ' · refreshed snapshots';
  } catch (error) { if ($('camera-select').value === camera) { clearFrame(); $('frame-status').textContent = error.message; } }
  finally { frameBusy = false; }
}
$('camera-select').onchange = () => { clearFrame(); frame(); };
$('refresh').onclick = () => { refresh(); frame(); };
refresh(); setInterval(refresh, 10000); setInterval(frame, 5000);
})();
