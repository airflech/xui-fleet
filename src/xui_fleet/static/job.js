// The operation in progress, its log drawer, the history of earlier operations and the «Применить» preview.
// Classic scripts in order (see ui.html): top-level names are shared between the files.

// ---------- job ----------
const BUSY_IDS = ['btnRole', 'btnAppExport', 'btnAppImport', 'btnDomains', 'btnApply', 'btnUpgrade', 'btnAddNode', 'btnAddUser', 'btnRefresh', 'btnMasterAccess', 'btnFixAccess', 'btnRetry', 'btnPanels', 'btnBackup', 'btnMigrate', 'btnCheck', 'btnPlan'];
function setBusy(busy) {
  for (const id of BUSY_IDS) if ($(id)) $(id).disabled = busy;
  document.querySelectorAll('[data-access], [data-click]').forEach((b) => { b.disabled = busy; });
}
function paintLog(lines) {
  const log = $('joblog');
  const stick = log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
  log.textContent = '';
  if (!lines.length) { log.textContent = 'Подключаюсь к серверам…'; return; }
  for (const line of lines) {
    const cls = /^(\[ошибка\]|✗|❌)/.test(line) || /не удалось|ошибк/i.test(line) && /^!/.test(line) ? 'e'
      : /^(⚠|!)/.test(line) ? 'w' : /^(✅|✓)|работает \(/.test(line) ? 'k' : '';
    const s = el('span', cls, line + '\n'); log.appendChild(s);
  }
  if (stick) log.scrollTop = log.scrollHeight;
}
function renderJob(job) {
  if (!job || !job.id) return;
  curJob = job.id;
  const chip = $('jobchip');
  if (!job.running && !job.title) {
    // Cleared on a profile switch or exit: there is no operation to show (not an error).
    chip.classList.add('hidden'); $('drawer').classList.add('hidden'); setBusy(false); return;
  }
  jobShown = job;
  if (!job.running && job.age != null) jobEnded[job.id] = jobEnded[job.id] || Date.now() - job.age * 1000;
  paintChip();
  if (job.running && drawerMode !== 'live') setDrawerMode('live');
  if (drawerMode === 'live') { renderJobHeader(job); paintLog(job.lines || []); paintFoot(job); }
  if (job.running && jobClosed !== job.id) $('drawer').classList.remove('hidden');
  setBusy(job.running);
}
// The header chip is the state of the operation now. A finished one stays in full for a minute (an error —
// until its log was opened), then shrinks to a quiet "journal" button: it is history, not the current state.
let jobShown = null; const jobEnded = {};
function errSeen(id) { try { return localStorage.getItem('xf-err-seen') === String(id); } catch (e) { return false; } }
function paintChip() {
  const job = jobShown, chip = $('jobchip');
  if (!job || (!job.running && !job.title)) return;
  chip.classList.remove('hidden', 'ok', 'bad', 'quiet');
  chip.textContent = '';
  const ended = jobEnded[job.id], mins = ended ? Math.floor((Date.now() - ended) / 60000) : 0;
  const quiet = !job.running && ended && Date.now() - ended > 60000 && (job.ok || errSeen(job.id));
  if (job.running) { chip.appendChild(el('span', 'spin')); chip.appendChild(el('span', 'txt', job.title + '…')); }
  else if (quiet) {
    chip.classList.add('quiet'); chip.appendChild(icon('log'));
    chip.appendChild(el('span', 'txt', 'Журнал'));
    chip.title = 'Последняя операция: «' + job.title + '» — ' + (job.ok ? 'готово' : 'ошибка') + ', ' +
      (mins < 60 ? mins + ' мин назад' : Math.floor(mins / 60) + ' ч назад');
    return;
  } else {
    chip.classList.add(job.ok ? 'ok' : 'bad'); chip.appendChild(icon(job.ok ? 'check' : 'x'));
    chip.appendChild(el('span', 'txt', (job.ok ? 'Готово' : 'Ошибка') + ': ' + job.title));
  }
  chip.title = job.title;
}
setInterval(paintChip, 15000);
$('jobchip').addEventListener('click', () => {
  const d = $('drawer'); d.classList.toggle('hidden');
  if (!d.classList.contains('hidden') && drawerMode !== 'live') setDrawerMode('live');
  if (d.classList.contains('hidden')) jobClosed = curJob; else jobClosed = 0;
  if (jobShown && !jobShown.running && !jobShown.ok) { try { localStorage.setItem('xf-err-seen', String(jobShown.id)); } catch (e) {} }
});
// The drawer shows the current operation ('live'), the list of earlier ones ('list') or one of them ('past').
let drawerMode = 'live';
function setDrawerMode(mode) {
  drawerMode = mode;
  $('joblog').classList.toggle('hidden', mode === 'list');
  $('jobhist').classList.toggle('hidden', mode !== 'list');
  $('jobback').classList.toggle('hidden', mode === 'live');
  $('jobhistBtn').classList.toggle('hidden', mode === 'list');
  if (mode !== 'live') $('jobfoot').classList.add('hidden');
  if (mode === 'live' && jobShown) {
    renderJobHeader(jobShown); paintLog(jobShown.lines || []); paintFoot(jobShown);
  }
}
function renderJobHeader(job) {
  $('jobtitle').textContent = job.title + (job.running ? '…' : job.ok ? ' — готово' : ' — ошибка');
  const ic = $('jobicon'); ic.textContent = '';
  if (job.running) ic.appendChild(el('span', 'spin')); else ic.appendChild(el('span', 'dot ' + (job.ok ? 'ok' : 'bad')));
}
// After «Что изменит «Применить»»: what was found, and the button to apply exactly that.
function paintFoot(job) {
  const f = $('jobfoot'); f.textContent = '';
  const show = !job.running && job.ok && Array.isArray(job.preview);
  f.classList.toggle('hidden', !show);
  if (!show) return;
  const n = job.preview.length;
  f.appendChild(el('span', 'grow', n ? 'Будет изменено: ' + n + ' — список выше, строки «[dry-run]».' : 'Изменений нет — всё уже настроено.'));
  if (n) { const b = el('button', 'btn primary sm', 'Применить'); b.type = 'button'; b.addEventListener('click', () => start('/api/apply')); f.appendChild(b); }
}
function whenText(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000), today = new Date().toDateString() === d.toDateString();
  return (today ? '' : d.toLocaleDateString('ru-RU', { day: 'numeric', month: 'short' }) + ', ') + d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
}
async function openHistory() {
  setDrawerMode('list');
  $('jobtitle').textContent = 'История операций'; $('jobicon').textContent = '';
  const box = $('jobhist'); box.textContent = '';
  const wait = el('div', 'empty'); wait.appendChild(el('span', 'spin')); box.appendChild(wait);
  let jobs = [];
  try { jobs = (await api('/api/jobs')).jobs || []; } catch (e) { box.textContent = ''; box.appendChild(el('div', 'empty err', e.message)); return; }
  box.textContent = '';
  if (!jobs.length) { box.appendChild(el('div', 'empty', 'Операций пока не было.')); return; }
  for (const j of jobs) {
    const b = el('button'); b.type = 'button';
    b.appendChild(el('span', 'dot ' + (j.ok ? 'ok' : 'bad')));
    b.appendChild(el('span', 'grow', j.title));
    const secs = j.started && j.finished ? Math.max(1, Math.round(j.finished - j.started)) : 0;
    b.appendChild(el('span', 'when', whenText(j.finished) + (secs ? ' · ' + (secs < 60 ? secs + ' с' : Math.round(secs / 60) + ' мин') : '')));
    b.addEventListener('click', () => openPast(j.id));
    box.appendChild(b);
  }
}
async function openPast(id) {
  try {
    const j = await api('/api/jobs/one?id=' + id);
    setDrawerMode('past');
    renderJobHeader(j); $('jobtitle').textContent += ' · ' + whenText(j.finished);
    paintLog(j.lines || []);
    $('joblog').scrollTop = 0;
  } catch (e) { toast(e.message); }
}
$('jobhistBtn').addEventListener('click', openHistory);
$('jobback').addEventListener('click', () => (drawerMode === 'past' ? openHistory() : setDrawerMode('live')));
$('btnPlan').addEventListener('click', () => { closeMenus(); start('/api/plan'); });
$('jobhide').addEventListener('click', () => { jobClosed = curJob; $('drawer').classList.add('hidden'); });

async function pollJob() {
  clearTimeout(jobTimer);
  let job;
  try { job = await api('/api/job'); } catch (e) { jobTimer = setTimeout(pollJob, 2000); return; }
  if (!job.id || job.id < curJob) {
    // The app restarted (after an update) and does not know the operation this page was watching: start fresh.
    try { sessionStorage.setItem('xf-updated', '1'); } catch (e) {}
    location.reload(); return;
  }
  renderJob(job);
  if (job.running) { jobTimer = setTimeout(pollJob, 1000); return; }
  if (job.id !== jobSeen) {
    jobSeen = job.id;
    toast(job.ok ? '«' + job.title + '» — готово' : '«' + job.title + '» завершилось с ошибкой — подробности в журнале');
    await refresh();
    if (panelsAfterJob) { panelsAfterJob = false; loadPanels(); }
  }
}
