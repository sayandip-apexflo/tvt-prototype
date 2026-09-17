(() => {
'use strict';
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const section = document.createElement('section');
section.className = 'page-card';
const today = new Date().toISOString().slice(0, 10);
section.innerHTML = `<h2>Reports</h2><p id="reports-error" role="status"></p>
<h3>Attendance</h3>
<label>Date <input id="attendance-date" type="date" value="${esc(today)}"></label>
<p id="attendance-total"></p>
<div id="attendance-table"></div>
<h3>Vehicle traffic</h3>
<label>Date <input id="vehicle-date" type="date" value="${esc(today)}"></label>
<p id="vehicle-total"></p>
<div id="vehicle-table"></div>`;
document.querySelector('.customer-main').append(section);
const error = section.querySelector('#reports-error');

async function api(path) {
 const response = await fetch('/dashboard/api/' + path);
 const result = await response.json();
 if (!response.ok) throw Error(result.error || 'Reports are unavailable');
 return result;
}

function personLabel(row) {
 if (row.display_name) return esc(row.display_name);
 return `Unnamed (${esc(String(row.person_id).slice(0, 8))})`;
}

function formatDuration(seconds) {
 if (seconds == null) return '—';
 const minutes = Math.round(seconds / 60);
 return minutes < 60 ? `${minutes} min` : `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

function formatTime(value) {
 return value == null ? '—' : new Date(value * 1000).toLocaleString();
}

async function refreshAttendance() {
 try {
  const date = section.querySelector('#attendance-date').value;
  const result = await api(`reports/attendance?date=${encodeURIComponent(date)}`);
  section.querySelector('#attendance-total').textContent = `Total time on site: ${formatDuration(result.total_duration_seconds)}`;
  section.querySelector('#attendance-table').innerHTML = result.sessions.map(row => `<article><strong>${personLabel(row)}</strong><p>${esc(row.gate)} · ${formatTime(row.entry_time)} → ${formatTime(row.exit_time)} · ${formatDuration(row.duration_seconds)}</p>${row.person_status === 'auto_enrolled' ? `<form data-rename="${esc(row.person_id)}"><input name="display_name" placeholder="Name this person" required maxlength="160"><button>Save</button></form>` : ''}</article>`).join('') || '<p>No attendance sessions for this date.</p>';
  error.textContent = '';
 } catch (e) { error.textContent = e.message; }
}

async function refreshVehicles() {
 try {
  const date = section.querySelector('#vehicle-date').value;
  const result = await api(`reports/vehicle-traffic?date=${encodeURIComponent(date)}`);
  section.querySelector('#vehicle-total').textContent = `Entered: ${result.entered_count} · Exited: ${result.exited_count}`;
  section.querySelector('#vehicle-table').innerHTML = result.sessions.map(row => `<article><strong>${esc(row.plate_text)}</strong><p>${esc(row.gate)} · ${formatTime(row.entry_time)} → ${formatTime(row.exit_time)}</p></article>`).join('') || '<p>No vehicle sessions for this date.</p>';
 } catch (e) { error.textContent = e.message; }
}

section.querySelector('#attendance-date').onchange = refreshAttendance;
section.querySelector('#vehicle-date').onchange = refreshVehicles;
section.addEventListener('submit', async event => {
 const form = event.target.closest('[data-rename]');
 if (!form) return;
 event.preventDefault();
 const button = form.querySelector('button');
 button.disabled = true;
 try {
  await fetch('/dashboard/api/persons/rename', {
   method: 'POST',
   headers: {'Content-Type': 'application/json'},
   body: JSON.stringify({person_id: form.dataset.rename, display_name: form.display_name.value}),
  }).then(async r => { if (!r.ok) throw Error((await r.json()).error || 'Rename failed'); });
  await refreshAttendance();
 } catch (e) { error.textContent = e.message; button.disabled = false; }
});

refreshAttendance(); refreshVehicles();
setInterval(refreshAttendance, 15000); setInterval(refreshVehicles, 15000);
})();
