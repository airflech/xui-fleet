// Loading state from the app: overview, live server data with its background refresh.
// Classic scripts in order (see ui.html): top-level names are shared between the files.

// ---------- data ----------
let liveRetry, liveAt = 0, statsAt = 0, liveLoading = false;
// quiet: a background refresh — the cards keep their numbers until the new ones arrive (no skeletons).
async function loadLive(quiet) {
  clearTimeout(liveRetry);
  if (quiet && liveLoading) return;
  liveLoading = true;
  if (!quiet) { live = null; renderServers(); renderUsers(); }
  const btn = $('btnRefresh'); btn.disabled = true;
  let got;
  try { got = await api('/api/live'); }
  catch (e) { got = { ready: true, nodes: {}, users: {}, failed: e.message }; }
  liveLoading = false;
  btn.disabled = false;
  if (quiet && got.failed && live && !live.failed) { paintLiveAge(); return; }  // keep the last good numbers
  live = got;
  if (!live.failed && live.ready && !live.busy) liveAt = Date.now();
  renderServers(); renderUsers(); paintLiveAge();
  if (live.failed) { clearTimeout(liveRetry); liveRetry = setTimeout(() => loadLive(quiet), 5000); }
  else if (live.ready && !live.busy && (!quiet || Date.now() - statsAt > 120000)) { statsAt = Date.now(); loadUserStats(); }
}
function paintLiveAge() {
  const s = liveAt ? Math.round((Date.now() - liveAt) / 1000) : null, box = $('liveAge');
  box.textContent = s == null ? '' : s < 10 ? 'обновлено только что' : s < 60 ? 'обновлено ' + s + ' с назад'
    : 'обновлено ' + Math.floor(s / 60) + ' мин назад';
  $('btnRefresh').title = 'Обновить состояние' + (box.textContent ? ' (' + box.textContent + ')' : '');
}
// Online, CPU, memory follow the servers by themselves while the window is in front.
const LIVE_EVERY = 45000;
function liveDue() {
  return overview && overview.configured && !(overview.job && overview.job.running) && !liveLoading
    && document.visibilityState === 'visible' && !document.querySelector('dialog[open]');
}
setInterval(() => { paintLiveAge(); if (liveDue() && Date.now() - liveAt >= LIVE_EVERY) loadLive(true); }, 5000);
document.addEventListener('visibilitychange', () => { if (liveDue() && Date.now() - liveAt >= LIVE_EVERY) loadLive(true); });

async function refresh() {
  try { overview = await api('/api/overview'); }
  catch (e) { $('masterBox').classList.remove('hidden'); $('masterline').textContent = 'Нет связи с xui-fleet: ' + e.message; return; }
  const configured = overview.configured;
  renderProfiles();
  if (overview.app) { $('appVer').textContent = overview.app; $('appVer').classList.remove('hidden'); }
  const running = !!(overview.job && overview.job.running);
  $('setup').classList.toggle('hidden', configured || running);
  $('app').classList.toggle('hidden', !configured);
  $('masterBox').classList.toggle('hidden', !configured);
  $('masterMenuWrap').classList.toggle('hidden', !configured);
  if (configured) {
    const m = overview.master;
    $('masterline').textContent = (m.domain || m.host) + ' · 3x-ui ' + overview.version;
    $('masterline').title = 'Главный сервер ' + m.name + ' · ' + m.host;
    $('masterDot').className = 'dot ' + (m.prepared ? 'ok' : 'warn');
    const rsv = (m.reserveDomains || [])[0];
    $('domainsNow').textContent = m.domain ? m.domain + (rsv ? ' · резерв ' + rsv : ' · без резерва') : 'домен не задан';
    $('btnDomains').disabled = !m.prepared;
    if ($('dlgDomains').open) renderDomains();
  }
  $('notready').classList.toggle('hidden', !configured || overview.master.prepared || running);
  renderJob(overview.job);
  if (running) { live = { busy: true }; if (configured) { renderServers(); renderUsers(); } pollJob(); return; }
  if (overview.job) jobSeen = overview.job.id;
  if (configured) { if (!document.querySelector('[data-pane=backups]').classList.contains('hidden')) { loadBackups(); loadSync(); }
    if (!document.querySelector('[data-pane=inbounds]').classList.contains('hidden')) loadInbounds();
    await loadLive(); }
}

async function start(path, body, form) {
  const err = form ? form.querySelector('.formerr') : null;
  if (err) err.textContent = '';
  closeMenus();
  try { await api(path, body || {}); }
  catch (e) { if (err) err.textContent = e.message; else toast(e.message); return false; }
  jobClosed = 0;
  document.querySelectorAll('dialog[open]').forEach((d) => d.close());
  $('setup').classList.add('hidden');
  $('notready').classList.add('hidden');
  live = { busy: true };
  try { overview = await api('/api/overview'); } catch (e) { /* the next poll retries */ }
  if (overview && overview.configured) { $('app').classList.remove('hidden'); renderServers(); renderUsers(); }
  pollJob();
  return true;
}

function confirmAction(title, text, fn, yes, danger) {
  $('confirmTitle').textContent = title;
  $('confirmText').textContent = text;
  const b = $('confirmYes'); b.textContent = yes || 'Продолжить'; b.className = 'btn ' + (danger ? 'danger' : 'primary');
  b.onclick = () => { $('dlgConfirm').close(); fn(); };
  $('dlgConfirm').showModal();
}
