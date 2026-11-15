// User statistics and the user card dialog.
// Classic scripts in order (see ui.html): top-level names are shared between the files.

// ---------- user statistics ----------
function fmtBytes(n) {
  if (!n) return '0';
  const u = ['Б', 'КБ', 'МБ', 'ГБ', 'ТБ']; let i = 0; let v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return (v >= 100 || i === 0 ? Math.round(v) : v.toFixed(1).replace('.', ',')) + ' ' + u[i];
}
function fmtWhen(ts) {
  if (!ts) return '—';
  const d = new Date(ts > 1e12 ? ts : ts * 1000);
  return d.toLocaleString('ru-RU', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
}
function serverLabel(name) {
  if (!overview) return name;
  if (overview.master && overview.master.name === name) return overview.master.display || 'Главный сервер';
  const n = (overview.nodes || []).find((x) => x.name === name);
  return n ? (n.display || n.name) : name;
}
function plural(n, one, few, many) {
  const m10 = n % 10, m100 = n % 100;
  return n + ' ' + (m10 === 1 && m100 !== 11 ? one : m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14) ? few : many);
}
function statLine(st) {
  const box = el('div', 'ustats');
  const add = (ic, text, title, bad) => { const s = el('span', bad ? 'over' : null); s.appendChild(icon(ic)); s.appendChild(document.createTextNode(text)); s.title = title; box.appendChild(s); };
  add('chart', fmtBytes(st.used) + (st.quota ? ' из ' + fmtBytes(st.quota) : ''), 'Трафик за всё время: отдано ' + fmtBytes(st.up) + ', получено ' + fmtBytes(st.down),
    st.quota && st.used >= st.quota);
  if (st.devices !== null && st.devices !== undefined) {
    const overDev = st.limitDevices && st.devices > st.limitDevices;
    add('phone', st.devices + (st.limitDevices ? ' из ' + st.limitDevices : '') + ' устр.', 'Устройств получили подписку' + (st.limitDevices ? ', лимит ' + st.limitDevices : ', без лимита'), overDev);
  }
  const overIp = st.limitIp && st.ips > st.limitIp;
  if (st.ips || st.limitIp) add('globe', 'IP ' + st.ips + (st.limitIp ? ' из ' + st.limitIp : ''), 'Разных IP-адресов за последнее время' + (st.limitIp ? ', лимит ' + st.limitIp : ''), overIp);
  if (st.expiry > 0) {
    const days = Math.ceil((st.expiry - Date.now()) / DAY), d = new Date(st.expiry).toLocaleDateString('ru-RU');
    const s = el('span', days <= 0 ? 'over' : days <= 7 ? 'warn' : null, days <= 0 ? 'истёк ' + d : 'до ' + d);
    s.title = days <= 0 ? 'Срок действия закончился' : 'Действует до ' + d + (days <= 7 ? ' — осталось ' + days + ' дн.' : '');
    box.appendChild(s);
  }
  if (!st.enabled) { const s = el('span', 'over', 'отключён'); box.appendChild(s); }
  return box;
}
async function loadUserStats() {
  try { const d = await api('/api/users/stats'); if (d.stats) { userStats = d.stats; renderUsers(); renderServers(); } }
  catch (e) { /* the list works without statistics */ }
}
let infoUser = null;
async function openUserInfo(name) {
  infoUser = name;
  $('uiName').textContent = name; $('uiLead').textContent = 'Загружаю с главного сервера…';
  const body = $('uiBody'); body.textContent = '';
  // What the list already knows shows at once; devices and IPs follow from the master.
  if (userStats && userStats[name]) renderUserInfo(Object.assign({}, userStats[name], { partial: true }));
  else { const w = el('div', 'empty'); w.appendChild(el('span', 'spin')); body.appendChild(w); }
  if (!$('dlgUserInfo').open) $('dlgUserInfo').showModal();
  try { const u = await api('/api/user?name=' + encodeURIComponent(name)); if (infoUser === name) renderUserInfo(u); }
  catch (e) {
    if (userStats && userStats[name]) { $('uiLead').textContent = 'Не удалось загрузить подробности: ' + e.message; return; }
    body.textContent = ''; body.appendChild(el('div', 'empty err', e.message)); $('uiLead').textContent = '';
  }
}
$('uiRefresh').addEventListener('click', () => { if (infoUser) openUserInfo(infoUser); });
function renderUserInfo(u) {
  if (userStats && !u.partial) { userStats[infoUser] = u; renderUsers(); }
  const on = u.servers.length > 0;
  $('uiLead').textContent = (u.enabled ? '' : 'Отключён в панели. ') + (on ? 'Сейчас подключён: ' + u.servers.map(serverLabel).join(', ') : lastSeen(u.lastOnline))
    + (u.partial ? ' · загружаю устройства…' : '');
  const body = $('uiBody');
  // Keep what was typed while the details were loading.
  const old = body.querySelector('form.uform'), typed = old && old.dataset.dirty ? Object.fromEntries(
    [...old.elements].filter((i) => i.name).map((i) => [i.name, i.type === 'checkbox' ? i.checked : i.value])) : null;
  body.textContent = '';
  const tiles = el('div', 'tiles');
  const tile = (label, value, extra) => { const t = el('div', 'tile'); t.appendChild(el('small', null, label)); t.appendChild(el('b', null, value)); if (extra) t.appendChild(extra); tiles.appendChild(t); };
  let meter = null;
  if (u.quota) { meter = el('div', 'meter' + (u.used >= u.quota ? ' bad' : '')); const i = el('i'); i.style.width = Math.min(100, 100 * u.used / u.quota) + '%'; meter.appendChild(i); }
  tile('Трафик за всё время', fmtBytes(u.used) + (u.quota ? ' из ' + fmtBytes(u.quota) : ''), meter);
  tile('Получено / отдано', fmtBytes(u.down) + ' / ' + fmtBytes(u.up));
  tile('Последнее подключение', on ? 'сейчас' : fmtWhen(u.lastOnline));
  tile('Подписка обновлялась', fmtWhen(u.lastSubFetch));
  body.appendChild(tiles);

  const loadingRow = () => { const r = el('div', 'muted-row'); r.appendChild(el('span', 'spin')); r.appendChild(document.createTextNode(' загружаю с главного сервера…')); return r; };
  // devices
  const real = (u.deviceList || []).filter((d) => !d.service);
  const ds = el('div', 'sect');
  const nDev = u.deviceList ? real.length : (u.devices ?? '…');
  const h = el('h3'); h.appendChild(icon('phone')); h.appendChild(document.createTextNode('Устройства: ' + nDev + (u.limitDevices ? ' из ' + u.limitDevices : '')));
  if (u.limitDevices && nDev > u.limitDevices) h.appendChild(el('span', 'tag bad', 'больше лимита'));
  ds.appendChild(h);
  const rows = el('div', 'rows');
  if (!u.deviceList) rows.appendChild(loadingRow());
  else if (!u.deviceList.length) rows.appendChild(el('div', 'muted-row', 'Пока ни одно приложение не сообщило об устройстве. Их передают Happ, v2RayTun и INCY при обновлении подписки.'));
  for (const d of u.deviceList || []) {
    const r = el('div', d.service ? 'muted-row' : null);
    const g = el('div', 'grow');
    const title = d.service ? 'Служебный запрос 3x-ui (не устройство)' : [d.model, d.os && (d.os + (d.osVersion ? ' ' + d.osVersion : ''))].filter(Boolean).join(' · ') || 'Устройство без описания';
    g.appendChild(el('div', null, title));
    g.appendChild(el('div', 'sub', [d.app, 'впервые ' + fmtWhen(d.firstSeen), 'последний раз ' + fmtWhen(d.lastSeen)].filter(Boolean).join(' · ')));
    r.appendChild(g);
    if (!d.service) {
      const b = el('button', 'btn ghost sm danger', 'Забыть'); b.title = 'Освободить место: устройство вернётся, только если снова получит подписку и лимит это позволит';
      b.addEventListener('click', () => confirmAction('Забыть устройство «' + title + '»?',
        'Место в лимите освободится. Если у устройства осталась подписка, при следующем обновлении оно зарегистрируется снова (если лимит позволит).',
        () => userAction('/api/user/device/forget', { name: infoUser, id: d.id }), 'Забыть', true));
      r.appendChild(b);
    }
    rows.appendChild(r);
  }
  ds.appendChild(rows); body.appendChild(ds);

  // IPs
  const is = el('div', 'sect');
  const hi = el('h3'); hi.appendChild(icon('globe')); hi.appendChild(document.createTextNode('IP-адреса за последнее время: ' + (u.ipList ? u.ipList.length : u.ips) + (u.limitIp ? ' (лимит ' + u.limitIp + ')' : '')));
  if (u.limitIp && u.ips > u.limitIp) hi.appendChild(el('span', 'tag bad', 'больше лимита'));
  is.appendChild(hi);
  const ir = el('div', 'rows');
  if (!u.ipList) ir.appendChild(loadingRow());
  else if (!u.ipList.length) ir.appendChild(el('div', 'muted-row', 'Нет данных: пользователь давно не подключался.'));
  for (const i of u.ipList || []) {
    const r = el('div'); const g = el('div', 'grow');
    g.appendChild(el('div', null, i.ip)); g.appendChild(el('div', 'sub', serverLabel(i.server) + (i.time ? ' · ' + i.time : '')));
    r.appendChild(g); ir.appendChild(r);
  }
  is.appendChild(ir); body.appendChild(is);

  // settings
  const ss = el('div', 'sect');
  ss.appendChild(el('h3', null, 'Настройки'));
  const uf = el('form', 'uform');
  const inp = (label, type, name, value, cls, attrs) => {
    const d = el('div', cls); const l = el('label', 'f', label); const i = el('input'); i.type = type; i.name = name;
    if (type === 'checkbox') i.checked = !!value; else i.value = value;
    Object.assign(i, attrs || {}); l.appendChild(i); d.appendChild(l); return d;
  };
  const day = (ms) => { if (!ms || ms <= 0) return ''; const d = new Date(ms); return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0'); };
  uf.appendChild(inp('Комментарий', 'text', 'comment', u.comment || '', 'wide', { maxLength: 200, placeholder: 'Например: телефон мамы' }));
  uf.appendChild(inp('Лимит трафика, ГБ (0 — без лимита)', 'number', 'totalGb', u.quota ? +(u.quota / 1024 ** 3).toFixed(2) : 0, null, { min: '0', step: 'any' }));
  uf.appendChild(inp('Действует до (пусто — бессрочно)', 'date', 'expiry', day(u.expiry), null));
  uf.appendChild(inp('Устройств (0 — без лимита)', 'number', 'devices', u.limitDevices, null, { min: '0', max: '1000' }));
  uf.appendChild(inp('IP одновременно (0 — без лимита)', 'number', 'ips', u.limitIp, null, { min: '0', max: '1000' }));
  uf.appendChild(el('div', 'hint wide', 'Лишнее устройство не получит подписку. Лишние IP 3x-ui отключает на время. '
    + 'Уже скопированный в приложение конфиг лимит устройств не остановит — для этого есть лимит IP.'));
  const onBox = el('label', 'check wide'); const cb = el('input'); cb.type = 'checkbox'; cb.name = 'enabled'; cb.checked = u.enabled;
  onBox.appendChild(cb); onBox.appendChild(el('span', null, 'Включён — подписка и подключения работают')); uf.appendChild(onBox);
  const usave = el('button', 'btn primary', 'Сохранить'); usave.type = 'submit'; const ub = el('div', 'wide'); ub.appendChild(usave); uf.appendChild(ub);
  const uerr = el('div', 'formerr wide'); uerr.setAttribute('role', 'alert'); uf.appendChild(uerr);
  if (typed) { for (const [k, v] of Object.entries(typed)) { const i = uf.elements[k]; if (i) { if (i.type === 'checkbox') i.checked = v; else i.value = v; } } uf.dataset.dirty = '1'; }
  uf.addEventListener('input', () => { uf.dataset.dirty = '1'; });
  uf.addEventListener('submit', (e) => {
    e.preventDefault();
    const d = uf.expiry.value ? new Date(uf.expiry.value + 'T23:59:59') : null;
    userAction('/api/user/edit', { name: infoUser, comment: uf.comment.value, totalGb: uf.totalGb.value || 0,
      expiry: d ? d.getTime() : 0, enabled: uf.enabled.checked, devices: uf.devices.value || 0, ips: uf.ips.value || 0 }, uerr, 'Сохранено');
  });
  ss.appendChild(uf);
  body.appendChild(ss);


  // dangerous actions, apart from the settings
  const dz = el('div', 'sect danger-zone'); dz.appendChild(el('h3', null, 'Опасные действия'));
  const acts = el('div', 'uactions');
  const reset = el('button', 'btn'); reset.type = 'button'; reset.appendChild(icon('refresh')); reset.appendChild(document.createTextNode('Сбросить трафик'));
  reset.addEventListener('click', () => confirmAction('Сбросить трафик ' + infoUser + '?',
    'Счётчик начнётся с нуля. Если пользователь был выключен из-за лимита трафика, он снова заработает.',
    () => userAction('/api/user/reset', { name: infoUser }, null, 'Трафик сброшен'), 'Сбросить'));
  const del = el('button', 'btn danger'); del.type = 'button'; del.appendChild(icon('trash')); del.appendChild(document.createTextNode('Удалить пользователя'));
  const who = infoUser;
  del.addEventListener('click', () => confirmAction('Удалить пользователя ' + who + '?',
    'Его подписка и все подключения перестанут работать на всех серверах. Перед удалением делается бэкап главного сервера — из него пользователя можно вернуть.',
    () => { $('dlgUserInfo').close(); if (userStats) delete userStats[who]; start('/api/user/delete', { name: who }); }, 'Удалить', true));
  acts.appendChild(reset); acts.appendChild(el('div', 'spacer')); acts.appendChild(del);
  dz.appendChild(acts); body.appendChild(dz);
}
async function userAction(path, body, errBox, done) {
  if (errBox) errBox.textContent = '';
  try {
    const r = await api(path, body);
    const f = $('uiBody').querySelector('form.uform'); if (f) delete f.dataset.dirty;  // saved: show what the server has
    renderUserInfo(r.user); if (done) toast(done);
  }
  catch (e) { if (errBox) errBox.textContent = e.message; else toast(e.message); }
}
