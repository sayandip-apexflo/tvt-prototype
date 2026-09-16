(() => {
'use strict';
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const section = document.createElement('section');
section.className = 'page-card';
section.innerHTML = `<h2>Alerts</h2><p>Notify this dashboard when a numeric event value crosses a threshold. Rules apply to new events. A non-matching value rearms the rule.</p><p id="alerts-error" role="status"></p><details><summary>Create an alert rule</summary><form id="alert-rule-form" class="form-grid">
<label>Rule name<input name="name" required maxlength="160"></label>
<label>Event type<select name="event_type" required></select></label>
<label>Numeric value<select name="field" required></select></label>
<label>Camera<select name="camera_id"><option value="">All cameras</option></select></label>
<label>Deployment<select name="deployment_id"><option value="">All deployments</option></select></label>
<label>Comparison<select name="operator"><option value="gt">Greater than</option><option value="gte">At least</option><option value="lt">Less than</option><option value="lte">At most</option><option value="eq">Equals</option></select></label>
<label>Threshold<input name="threshold" type="number" step="any" required></label>
<label>Cooldown (seconds)<input name="cooldown_seconds" type="number" min="0" max="86400" value="300" required></label>
<p>Event types and values come from recently received events. This compares each event's value; it does not total events over time. Pop-ups require this dashboard to be open; outstanding alerts remain available when you return.</p><button>Create rule</button></form></details>
<h3>Rules</h3><div id="alert-rules"></div><h3>Recent alerts</h3><div id="alert-history"></div>`;
document.querySelector('.customer-main').append(section);
const dialog = document.createElement('dialog');
dialog.setAttribute('aria-label', 'New alerts');
dialog.innerHTML = '<h2>New alerts</h2><div id="alert-popup-items"></div><button id="alert-close">Close</button>';
document.body.append(dialog);
dialog.querySelector('#alert-close').onclick = () => dialog.close();
const form = section.querySelector('form'), error = section.querySelector('#alerts-error');
let rules = [], busy = false;
const shown = new Set(), fields = new Map();
async function api(path, body) {
 const response = await fetch('/dashboard/api/' + path, body ? {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)} : {});
 const result = await response.json();
 if (!response.ok) throw Error(result.error || 'Alerts are unavailable');
 return result;
}
function numericPaths(value, prefix = '', depth = 0) {
 if (!value || typeof value !== 'object' || Array.isArray(value) || depth > 7) return [];
 return Object.entries(value).flatMap(([key, item]) => {
  if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)) return [];
  const path = prefix ? prefix + '.' + key : key;
  return typeof item === 'number' && Number.isFinite(item) ? [path] : numericPaths(item, path, depth + 1);
 });
}
function options(select, values, empty) {
 const previous = select.value;
 select.innerHTML = (empty ? `<option value="">${esc(empty)}</option>` : '') + [...values].sort().map(v => `<option value="${esc(v)}">${esc(v)}</option>`).join('');
 if ([...select.options].some(o => o.value === previous)) select.value = previous;
}
function updateFields() { options(form.elements.field, fields.get(form.elements.event_type.value) || [], 'Select a numeric value'); }
form.elements.event_type.onchange = updateFields;
window.addEventListener('apexfabric-events', ({detail}) => {
 const cameras = new Set(), deployments = new Set();
 fields.clear();
 for (const event of detail) {
  const p = event.payload || {}, type = p.type || p.event_type || p.event;
  if (typeof type !== 'string') continue;
  if (!fields.has(type)) fields.set(type, new Set());
  numericPaths(p).forEach(path => fields.get(type).add(path));
  const camera = p.camera_id || p.data?.camera_id || p.camera?.camera_id;
  if (typeof camera === 'string') cameras.add(camera);
  deployments.add(event.deployment_id);
 }
 options(form.elements.event_type, fields.keys(), fields.size ? 'Select an event type' : 'Waiting for events');
 options(form.elements.camera_id, cameras, 'All cameras');
 options(form.elements.deployment_id, deployments, 'All deployments');
 updateFields();
});
function alertCard(a) {
 const e = a.evidence, r = e.rule;
 return `<article><strong>${esc(r.name)}</strong><p>${esc(e.camera_id || 'No camera ID')} · ${esc(e.deployment_id)} · ${esc(r.field)} = ${esc(e.value)}</p><small>${esc(new Date(a.created_at * 1000).toLocaleString())}</small> ${a.acknowledged_at ? '<span>Acknowledged</span>' : `<button data-ack="${esc(a.id)}">Acknowledge</button>`}</article>`;
}
async function refresh() {
 if (busy || document.hidden) return;
 busy = true;
 try {
  const [r, a] = await Promise.all([api('alert-rules'), api('alerts')]);
  rules = r.rules;
  section.querySelector('#alert-rules').innerHTML = rules.map(r => `<article><strong>${esc(r.name)}</strong><p>${esc(r.event_type)} · ${esc(r.field)} ${esc({gt:'>',gte:'≥',lt:'<',lte:'≤',eq:'='}[r.operator])} ${esc(r.threshold)} · ${r.enabled ? 'Enabled' : 'Disabled'}</p><button data-toggle="${esc(r.id)}">${r.enabled ? 'Disable' : 'Enable'}</button> <button data-delete="${esc(r.id)}">Delete</button></article>`).join('') || '<p>No rules configured.</p>';
  section.querySelector('#alert-history').innerHTML = a.alerts.map(alertCard).join('') || '<p>No alerts triggered.</p>';
  const fresh = a.alerts.filter(a => !a.acknowledged_at && !shown.has(a.id));
  fresh.forEach(a => shown.add(a.id));
  if (fresh.length) {
   dialog.querySelector('#alert-popup-items').insertAdjacentHTML('beforeend', fresh.map(alertCard).join(''));
   if (!dialog.open) dialog.showModal();
  }
  error.textContent = '';
 } catch (e) { error.textContent = e.message; }
 finally { busy = false; }
}
form.onsubmit = async event => {
 event.preventDefault();
 const rule = Object.fromEntries(new FormData(form));
 rule.threshold = Number(rule.threshold); rule.cooldown_seconds = Number(rule.cooldown_seconds);
 const button = form.querySelector('button'); button.disabled = true;
 try { await api('alert-rules', {rule}); form.elements.name.value = ''; await refresh(); }
 catch (e) { error.textContent = e.message; }
 finally { button.disabled = false; }
};
async function action(event) {
 const button = event.target.closest('button');
 if (!button) return;
 button.disabled = true;
 try {
  if (button.dataset.ack) {
   await api('alerts/acknowledge', {id:button.dataset.ack});
   dialog.querySelectorAll('[data-ack]').forEach(b => { if (b.dataset.ack === button.dataset.ack) b.closest('article').remove(); });
   if (!dialog.querySelector('[data-ack]') && dialog.open) dialog.close();
  } else if (button.dataset.toggle) {
   const rule = rules.find(r => r.id === button.dataset.toggle);
   await api('alert-rules', {rule:{...rule, enabled:!rule.enabled}});
  } else if (button.dataset.delete) {
   if (!confirm('Delete this rule? Existing alerts will be retained.')) return;
   await api('alert-rules', {action:'delete', id:button.dataset.delete});
  } else return;
  await refresh();
 } catch (e) { error.textContent = e.message; }
 finally { button.disabled = false; }
}
section.addEventListener('click', action); dialog.addEventListener('click', action);
refresh(); setInterval(refresh, 5000);
})();
