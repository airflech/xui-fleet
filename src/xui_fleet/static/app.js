// Wiring of buttons and dialogs, version watch and the start of the page. Loaded last.
// Classic scripts in order (see ui.html): top-level names are shared between the files.

// ---------- wiring ----------
document.querySelectorAll('[data-close]').forEach((b) => b.addEventListener('click', () => b.closest('dialog').close()));
document.querySelectorAll('[data-click]').forEach((b) => b.addEventListener('click', () => $(b.dataset.click).click()));
document.querySelectorAll('dialog').forEach((d) => d.addEventListener('click', (e) => { if (e.target === d) d.close(); }));
$('s_tls').addEventListener('change', () => $('s_domain_box').classList.toggle('hidden', $('s_tls').value !== 'domain'));
$('setupForm').addEventListener('submit', (e) => { e.preventDefault(); start('/api/setup', formData(e.target), e.target); });
$('nodeForm').addEventListener('submit', async (e) => { e.preventDefault(); if (await start('/api/node', formData(e.target), e.target)) resetForm(e.target); });
$('userForm').addEventListener('submit', async (e) => { e.preventDefault(); if (await start('/api/user', formData(e.target), e.target)) resetForm(e.target); });
$('accessForm').addEventListener('submit', (e) => { e.preventDefault(); start('/api/access', formData(e.target), e.target); });
$('btnMasterAccess').addEventListener('click', () => { closeMenus(); openAccess(overview.master, 'Доступ к главному серверу'); });
$('btnFixAccess').addEventListener('click', () => openAccess(overview.master, 'Доступ к главному серверу'));
$('btnRetry').addEventListener('click', () => start('/api/apply'));
function openDomain() {
  closeMenus(); $('dlgDomains').close();
  $('domainIp').textContent = overview.master.host;
  resetForm($('domainForm'));
  $('d_domain').value = overview.master.domain || '';
  $('dlgDomain').showModal();
}
$('btnRole').addEventListener('click', openRole);
$('appVer').addEventListener('click', (e) => { e.preventDefault(); openVersions(); });
$('updPill').addEventListener('click', () => openVersions(true));
$('appVer').title = 'Версии и обновления';
function relNotes(t) {
  // The release page's own lines (installation, the compare link, the heading) are not news inside the app.
  return (t || '').replace(/\r/g, '')
    .replace(/^(Установка:|Все изменения:|\*\*Full Changelog\*\*|---\s*$|## Что нового\s*$).*$/gm, '')
    .replace(/^#+\s*/gm, '').replace(/\*\*/g, '')
    .replace(/https:\/\/github\.com\/\S+\/(pull|compare)\/\S+/g, '').replace(/\n{3,}/g, '\n\n').trim();
}
async function openVersions(fresh) {
  const list = $('verList');
  if (!$('dlgVersions').open) $('dlgVersions').showModal();
  list.textContent = ''; const w = el('div', 'empty'); w.appendChild(el('span', 'spin')); w.appendChild(document.createTextNode(' Загружаю список с GitHub…')); list.appendChild(w);
  let d; try { d = await api('/api/versions' + (fresh ? '?fresh=1' : '')); } catch (e) { $('verErr').textContent = e.message; return; }
  $('verErr').textContent = d.error || '';
  $('verAuto').checked = !!d.auto;
  const ban = $('verBanner'); ban.textContent = '';
  const target = (d.channel === 'prerelease' ? d.latestPre || d.latest : d.latest);
  const cmpV = (a, b) => { const p = (v) => { const m = (v || '').match(/(\d+)\.(\d+)\.(\d+)(?:[-.]?(alpha|beta|rc|a|b)\.?(\d*))?/) || [];
    const pre = { a: 0, alpha: 0, b: 1, beta: 1, rc: 2 }; return [+m[1] || 0, +m[2] || 0, +m[3] || 0, m[4] ? pre[m[4]] : 3, +m[5] || 0]; };
    const x = p(a), y = p(b); for (let i = 0; i < 5; i++) if (x[i] !== y[i]) return x[i] - y[i]; return 0; };
  if (target && cmpV(target, d.current) > 0) {
    const b = el('div', 'vbanner' + (updInfo.syncWaits ? ' warn' : ''));
    const g = el('div', 'grow'); g.appendChild(el('b', null, 'Доступна версия ' + target));
    g.appendChild(document.createTextNode(updInfo.syncWaits ? ' — другой компьютер уже на ней, синхронизация продолжится после обновления.'
      : ' — что нового, смотрите в списке ниже.'));
    b.appendChild(g);
    const go = el('button', 'btn primary', 'Обновить'); go.dataset.access = 'version';
    go.addEventListener('click', () => { $('dlgVersions').close(); start('/api/versions/install', { version: target }); });
    b.appendChild(go); ban.appendChild(b);
  }
  $('verGitHub').href = 'https://github.com/' + d.repo + '/releases';
  document.querySelectorAll('[data-chan]').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.chan === d.channel)));
  const sum = $('verSummary'); sum.textContent = '';
  const cell = (k, v, note, action) => { const c = el('div'); c.appendChild(el('div', 'k', k)); c.appendChild(el('div', 'v', v || '—'));
    if (note) c.appendChild(el('div', 'note', note));
    if (action) { const b = el('button', 'btn sm', action[0]); b.style.marginTop = '6px'; b.addEventListener('click', action[1]); c.appendChild(b); }
    sum.appendChild(c); };
  const ahead = d.latest && !d.releases.some((r) => r.version === d.current);
  cell('У вас', d.current, d.pinned ? 'автообновление на паузе' : ahead ? 'новее опубликованной — релиз ещё собирается' : '', d.pinned ? ['Возобновить', async () => {
    await api('/api/versions/settings', { resume: true }); toast('Автообновление возобновлено'); openVersions(); }] : null);
  cell('Последняя стабильная', d.latest);
  cell('Последний пререлиз', d.latestPre, d.latestPre ? 'для проверки новых функций' : '');
  list.textContent = '';
  if (!d.releases.length) { list.appendChild(el('div', 'empty', d.error ? 'Список недоступен' : 'Релизов пока нет')); return; }
  const cmp = (a, b) => { const p = (v) => { const m = v.match(/(\d+)\.(\d+)\.(\d+)(?:[-.]?(alpha|beta|rc|a|b)\.?(\d*))?/) || [];
    const pre = { a: 0, alpha: 0, b: 1, beta: 1, rc: 2 }; return [+m[1] || 0, +m[2] || 0, +m[3] || 0, m[4] ? pre[m[4]] : 3, +m[5] || 0]; };
    const x = p(a), y = p(b); for (let i = 0; i < 5; i++) if (x[i] !== y[i]) return x[i] - y[i]; return 0; };
  for (const r of d.releases) {
    const row = el('div', 'vrow'), top = el('div', 'top');
    top.appendChild(el('b', null, r.version));
    if (r.version === d.current) top.appendChild(el('span', 'tag ok', 'установлена'));
    if (r.version === d.latest) top.appendChild(el('span', 'tag', 'последняя'));
    if (r.prerelease) top.appendChild(el('span', 'tag pre', 'пререлиз'));
    top.appendChild(el('span', 'muted small', r.date ? new Date(r.date).toLocaleDateString('ru-RU', { day: 'numeric', month: 'long', year: 'numeric' }) : ''));
    top.appendChild(el('span', 'spacer'));
    if (r.version !== d.current) {
      const up = cmp(r.version, d.current) > 0;
      const b = el('button', 'btn sm' + (up && !r.prerelease ? ' primary' : ''), up ? 'Установить' : 'Вернуться');
      b.dataset.access = 'version';
      b.addEventListener('click', () => confirmAction((up ? 'Установить ' : 'Вернуться на ') + r.version + '?',
        (r.prerelease ? 'Это пререлиз: в нём новые функции, но возможны ошибки. ' : '') +
        (up ? '' : 'Пока на этом компьютере стоит более старая версия, автообновление будет на паузе (возобновить можно здесь же). ') +
        'Приложение скачает версию с GitHub, проверит контрольную сумму и перезапустится; ваши настройки и данные не меняются.' +
        (r.version.split('.').slice(0, 2).join('.') !== d.current.split('.').slice(0, 2).join('.')
          ? ' Это другая ветка версий: синхронизация с компьютерами на другой ветке будет ждать, пока они не перейдут на неё же.' : ''),
        () => { $('dlgVersions').close(); start('/api/versions/install', { version: r.version }); }, up ? 'Установить' : 'Вернуться'));
      top.appendChild(b);
    }
    row.appendChild(top);
    const notes = relNotes(r.notes);
    if (notes) { const det = el('details'); det.appendChild(el('summary', null, 'Что нового')); det.appendChild(el('pre', null, notes)); row.appendChild(det); }
    list.appendChild(row);
  }
}
document.querySelectorAll('[data-chan]').forEach((b) => b.addEventListener('click', async () => {
  await api('/api/versions/settings', { channel: b.dataset.chan });
  toast(b.dataset.chan === 'prerelease' ? 'Будут приходить и пререлизы' : 'Будут приходить только стабильные версии');
  openVersions();
}));
$('verRefresh').addEventListener('click', () => openVersions(true));
$('verAuto').addEventListener('change', async (e) => {
  await api('/api/versions/settings', { auto: e.target.checked });
  toast(e.target.checked ? 'Новые версии будут ставиться сами' : 'Новые версии будут предлагаться — обновление по кнопке');
});
$('roleForm').addEventListener('submit', (e) => {
  e.preventDefault();
  const d = formData(e.target);
  if (!d.vpn && !d.cascades) { e.target.querySelector('.formerr').textContent = 'Выберите хотя бы одну роль'; return; }
  start('/api/master/role', d, e.target);
});
document.querySelectorAll('#setupRoles input').forEach((r) => r.addEventListener('change', () => {
  $('s_label_box').classList.toggle('hidden', document.querySelector('#setupRoles input:checked').value === 'gateway');
}));
function openReserve() {
  closeMenus(); $('dlgDomains').close();
  const form = $('reserveForm'); resetForm(form);
  const cur = (overview.master.reserveDomains || [])[0] || '';
  $('r_domain').value = cur; $('reserveIp').textContent = overview.master.host;
  $('reserveRemove').classList.toggle('hidden', !cur);
  $('dlgReserve').showModal();
}
$('reserveForm').addEventListener('submit', (e) => { e.preventDefault(); start('/api/domain/reserve', formData(e.target), e.target); });
$('reserveRemove').addEventListener('click', () => start('/api/domain/reserve', { domain: '' }, $('reserveForm')));
function openAddr() {
  closeMenus(); $('dlgDomains').close();
  const m = overview.master, form = $('addrForm'); resetForm(form);
  $('addrMain').textContent = ' (' + m.domain + ')';
  $('addrReserve').textContent = ' (' + ((m.reserveDomains || [])[0] || '') + ')';
  form.querySelector('[value=' + (m.addressMode || 'main') + ']').checked = true;
  $('dlgAddr').showModal();
}
$('addrForm').addEventListener('submit', (e) => {
  e.preventDefault();
  const mode = (e.target.querySelector('input[name=mode]:checked') || {}).value;
  if (mode === (overview.master.addressMode || 'main')) { $('dlgAddr').close(); return; }
  start('/api/master/addresses', { mode }, e.target);
});
const APPS_MODE = { any: 'любые приложения', happ_incy: 'только Happ и INCY' };
function appsMode() { return (overview.master.allowedApps || []).length ? 'happ_incy' : 'any'; }
function openApps() {
  closeMenus(); $('dlgDomains').close();
  const form = $('appsForm'); resetForm(form);
  form.querySelector('[value=' + appsMode() + ']').checked = true;
  $('dlgApps').showModal();
}
$('appsForm').addEventListener('submit', (e) => {
  e.preventDefault();
  const apps = (e.target.querySelector('input[name=apps]:checked') || {}).value;
  if (apps === appsMode()) { $('dlgApps').close(); return; }
  start('/api/master/apps', { apps }, e.target);
});
function openVpnName() {
  closeMenus(); $('dlgDomains').close();
  resetForm($('vpnNameForm')); $('vpn_name').value = overview.master.vpnName || '';
  $('dlgVpnName').showModal();
}
$('vpnNameForm').addEventListener('submit', (e) => {
  e.preventDefault();
  const name = $('vpn_name').value.trim().replace(/\s+/g, ' ');
  if (name === (overview.master.vpnName || '')) { $('dlgVpnName').close(); return; }
  start('/api/master/title', { name }, e.target);
});
function switchToReserve() {
  closeMenus(); $('dlgDomains').close();
  const rsv = overview.master.reserveDomains[0], cur = overview.master.domain;
  confirmAction('Перейти на ' + rsv + '?',
    'Подписка и подключения «через RU» переедут на ' + rsv + ', ' + cur + ' станет резервным. Пользователям нужно открыть резервную ссылку (или обновить подписку, если ' + cur + ' ещё доступен). Подключения напрямую к нодам это не затрагивает.',
    () => start('/api/domain', { domain: rsv }), 'Перейти');
}
// One place for everything about domains: the main one, the reserve, which of them the links use,
// and the emergency switch — with whether each one opens from this computer right now.
const ADDR_MODE = { main: 'только основной домен', both: 'основной и резервный — две ссылки', reserve: 'только резервный домен' };
function domainState(d) {
  const r = live && live.domains; if (!r || !(d in r)) return null;
  return r[d];
}
function renderDomains() {
  const m = overview.master, rsv = (m.reserveDomains || [])[0], box = $('domRows'); box.textContent = '';
  const row = (k, v, state, btnText, fn, cls) => {
    const r = el('div', cls || null); const g = el('div', 'grow');
    g.appendChild(el('div', 'k', k)); const vv = el('div', 'v', v);
    if (state === true) vv.appendChild(el('span', 'tag ok', 'открывается'));
    else if (state === false) vv.appendChild(el('span', 'tag bad', 'не открывается'));
    g.appendChild(vv); r.appendChild(g);
    if (btnText) { const b = el('button', 'btn sm' + (cls ? ' primary' : ''), btnText); b.type = 'button'; b.addEventListener('click', fn); r.appendChild(b); }
    box.appendChild(r);
  };
  const mainDown = m.domain && domainState(m.domain) === false, rsvUp = rsv && domainState(rsv) === true;
  if (mainDown && rsvUp) row('Основной домен не открывается из вашей сети', 'Переведите подписку на ' + rsv + ' — пользователи откроют резервную ссылку', null,
    'Перейти на ' + rsv, switchToReserve, 'alert-row');
  row('Основной домен подписки', m.domain || 'не задан', m.domain ? domainState(m.domain) : null, m.domain ? 'Изменить' : 'Задать', openDomain);
  row('Резервный домен', rsv || 'не задан — подписка держится на одном домене', rsv ? domainState(rsv) : null, rsv ? 'Изменить' : 'Добавить', openReserve);
  row('Название VPN в приложениях', m.vpnName || 'не задано — приложения показывают адрес', null, m.vpnName ? 'Изменить' : 'Задать', openVpnName);
  row('Приложения, которые получают подписку', APPS_MODE[appsMode()], null, 'Изменить', openApps);
  if (rsv) {
    row('Адреса подключений через RU', ADDR_MODE[m.addressMode || 'main'], null, 'Изменить', openAddr);
    if (!(mainDown && rsvUp)) row('Если основной заблокируют', 'Резервный станет основным', null, 'Перейти на ' + rsv, switchToReserve);
  }
  $('domHint').textContent = !live || !live.domains ? 'Доступность появится после загрузки состояния серверов.'
    : 'Проверка с этого компьютера: если здесь включён VPN, результат может отличаться от сети пользователей.';
}
$('btnDomains').addEventListener('click', () => { closeMenus(); renderDomains(); $('dlgDomains').showModal(); });
$('domCheck').addEventListener('click', async () => { await loadLive(true); renderDomains(); });
$('domainForm').addEventListener('submit', (e) => { e.preventDefault(); start('/api/domain', formData(e.target), e.target); });
$('btnAddNode').addEventListener('click', () => {
  resetForm($('nodeForm')); fillFlags($('n_flag')); $('n_flag').value = 'auto';
  const box = $('nodeTemplate'); box.textContent = '';
  box.appendChild(el('b', null, 'Сервер получит тот же набор, что и остальные'));
  const chips = el('div', 'chips');
  for (const t of (overview.template || [])) chips.appendChild(el('span', 'chip', t));
  for (const t of (overview.templateExtras || [])) chips.appendChild(el('span', 'chip x', t));
  box.appendChild(chips);
  $('dlgNode').showModal();
});
$('labelForm').addEventListener('submit', (e) => { e.preventDefault(); start('/api/node/label', formData(e.target), e.target); });
$('btnPanels').addEventListener('click', loadPanels);
$('btnBackup').addEventListener('click', () => start('/api/backup'));
$('btnBackupOpen').addEventListener('click', () => api('/api/backups/open', {}).catch((e) => toast(e.message)));
$('btnCheck').addEventListener('click', () => start('/api/check'));
$('btnMigrate').addEventListener('click', openMigrate);
$('migrateForm').addEventListener('submit', (e) => {
  e.preventDefault();
  const data = formData(e.target);
  confirmAction('Перенести главный сервер на ' + data.host + '?',
    'Новый сервер станет главным: на нём будут клиенты, подписки и каскады из бэкапа. Старый останется как есть. Затем смените A-запись домена.',
    () => start('/api/migrate', data, e.target), 'Перенести', true);
});
$('btnAppExport').addEventListener('click', () => { resetForm($('appExportForm')); $('dlgAppExport').showModal(); });
$('appExportForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const f = e.target, err = f.querySelector('.formerr'), pw = $('x_pass').value;
  if (pw !== $('x_pass2').value) { err.textContent = 'Пароли не совпадают'; return; }
  const btn = f.querySelector('[type=submit]'); btn.disabled = true; err.textContent = '';
  try {
    const res = await fetch('/api/app/export', { method: 'POST', headers: { 'X-Fleet-Token': TOKEN, 'Content-Type': 'application/json' }, body: JSON.stringify({ password: pw }) });
    if ((res.headers.get('Content-Type') || '').includes('json')) { const d = await res.json(); throw new Error(d.error || 'ошибка'); }
    const blob = await res.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'xui-fleet-' + new Date().toISOString().slice(0, 10) + '.xfleet';
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
    $('dlgAppExport').close();
    toast('Копия сохранена в «Загрузки» (' + Math.round(blob.size / 1024) + ' КБ)');
  } catch (ex) { err.textContent = ex.message; }
  btn.disabled = false;
});
let importData = null, importReady = false;
$('btnAppImport').addEventListener('click', () => {
  resetForm($('appImportForm')); importData = null; importReady = false;
  $('importInfo').classList.add('hidden'); $('importGo').textContent = 'Проверить'; $('importGo').className = 'btn primary';
  $('dlgAppImport').showModal();
});
$('i_file').addEventListener('change', () => { importReady = false; $('importGo').textContent = 'Проверить'; $('importInfo').classList.add('hidden'); });
$('appImportForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const f = e.target, err = f.querySelector('.formerr'), btn = $('importGo');
  err.textContent = ''; btn.disabled = true;
  try {
    if (!importData || !importReady) {
      const file = $('i_file').files[0]; if (!file) throw new Error('выберите файл');
      const buf = new Uint8Array(await file.arrayBuffer());
      let bin = ''; for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
      importData = btoa(bin);
      const r = await api('/api/app/inspect', { data: importData, password: $('i_pass').value });
      const i = r.info, box = $('importInfo'); box.textContent = '';
      box.appendChild(el('b', null, 'Копия от ' + new Date((i.created || 0) * 1000).toLocaleString('ru-RU') + (i.from ? ' · ' + i.from : '')));
      box.appendChild(el('div', null, 'Главный сервер ' + i.master + ' · серверы: ' + (i.nodes.join(', ') || '—') + ' · бэкапов мастера: ' + i.masterBackups));
      if (r.configured) box.appendChild(el('div', 'err', 'Текущие настройки этого компьютера будут заменены (их копия останется рядом с файлом fleet.yaml).'));
      box.classList.remove('hidden');
      importReady = true;
      btn.textContent = r.configured ? 'Заменить и восстановить' : 'Восстановить';
      btn.className = 'btn ' + (r.configured ? 'danger' : 'primary');
      f.dataset.replace = r.configured ? '1' : '';
    } else {
      const r = await api('/api/app/import', { data: importData, password: $('i_pass').value, replace: !!f.dataset.replace });
      $('dlgAppImport').close();
      toast('Приложение восстановлено' + (r.backups ? ', бэкапов мастера: ' + r.backups : ''));
      await refresh();
    }
  } catch (ex) { err.textContent = ex.message; importReady = false; btn.textContent = 'Проверить'; btn.className = 'btn primary'; }
  btn.disabled = false;
});
async function loadSync() {
  let d; try { d = await api('/api/sync'); } catch (e) { $('syncText').textContent = e.message; return; }
  window.syncInfo = d;
  const btns = $('syncBtns'); btns.textContent = '';
  $('syncErr').textContent = d.error || '';
  if (!d.enabled) {
    $('syncState').textContent = '';
    $('syncText').textContent = 'Выключена. Включите — и изменения с этого компьютера сами появятся на остальных.';
    const b = el('button', 'btn primary', 'Включить…'); b.id = 'btnSyncOn'; b.addEventListener('click', openSync); btns.appendChild(b);
  } else {
    const when = d.syncedAt ? new Date(d.syncedAt * 1000).toLocaleString('ru-RU', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }) : 'ещё не было';
    const stateBox = $('syncState'); stateBox.textContent = '';
    const kind = d.error ? ['bad', 'Есть проблема'] : d.pending ? ['warn', 'Отправляю изменения…'] : ['ok', 'Всё синхронизировано'];
    const line = el('div', 'sync-state'); const dot = el('span', 'dot ' + kind[0]); line.appendChild(dot); line.appendChild(document.createTextNode(kind[1]));
    stateBox.appendChild(line);
    $('syncText').textContent = 'Сервер ' + d.host + ' · версия данных ' + d.rev + ' · ' + when;
    const now = el('button', 'btn', 'Синхронизировать сейчас'); now.dataset.access = 'sync'; now.addEventListener('click', () => start('/api/sync/now'));
    const off = el('button', 'btn ghost danger', 'Отключить'); off.dataset.access = 'sync';
    off.addEventListener('click', () => confirmAction('Отключить синхронизацию на этом компьютере?', 'Данные на сервере и на других компьютерах останутся. Этот компьютер перестанет получать и отправлять изменения.',
      async () => { await api('/api/sync/disable', {}); loadSync(); }, 'Отключить', true));
    btns.appendChild(now); btns.appendChild(off);
  }
}
function openSync() {
  const form = $('syncForm'); resetForm(form);
  const sel = $('sy_server'); sel.textContent = '';
  for (const s of (window.syncInfo && window.syncInfo.servers) || []) { const o = el('option', null, s.title + ' — ' + s.host); o.value = s.name; sel.appendChild(o); }
  $('dlgSync').showModal();
}
$('syncForm').addEventListener('submit', (e) => {
  e.preventDefault();
  if ($('sy_pass').value !== $('sy_pass2').value) { e.target.querySelector('.formerr').textContent = 'Пароли не совпадают'; return; }
  start('/api/sync/enable', { server: $('sy_server').value, password: $('sy_pass').value }, e.target);
});
$('btnJoin').addEventListener('click', () => { resetForm($('joinForm')); $('dlgJoin').showModal(); });
$('joinForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const f = e.target, err = f.querySelector('.formerr'), btn = f.querySelector('[type=submit]');
  err.textContent = ''; btn.disabled = true; btn.textContent = 'Подключаюсь…';
  try { const r = await api('/api/sync/join', formData(f)); $('dlgJoin').close(); toast('Компьютер подключён: ' + r.message); await refresh(); }
  catch (ex) { err.textContent = ex.message; }
  btn.disabled = false; btn.textContent = 'Подключить';
});
$('btnAddUser').addEventListener('click', () => { resetForm($('userForm')); $('dlgUser').showModal(); });
$('btnRefresh').addEventListener('click', () => loadLive());
$('btnApply').addEventListener('click', syncClick);
$('btnUpgrade').addEventListener('click', () => { closeMenus(); confirmAction('Обновить 3x-ui на всех серверах?',
  'Сначала обновятся зарубежные серверы, затем главный. Подключения кратко прервутся.', () => start('/api/upgrade'), 'Обновить'); });

showTab(['servers', 'users', 'panels', 'backups', 'stats', 'help'].includes(store.get('xf-tab')) ? store.get('xf-tab') : 'servers');
refresh();

// The service restarts itself after an update; the page notices and reloads, so it never needs closing.
let codeVersion = null, dataMark = null, pageApp = null, updInfo = {};
async function watchVersion() {
  try {
    const r = await fetch('/api/version', { headers: { 'X-Fleet-Token': TOKEN }, cache: 'no-store' });
    const info = await r.json(), v = info.version;
    $('offline').classList.add('hidden');
    // The program on this computer was replaced (update, rollback): this page is old — load the new one.
    if (info.app && pageApp && info.app !== pageApp && !(overview && overview.job && overview.job.running)) {
      try { sessionStorage.setItem('xf-updated', '1'); } catch (e) {}
      location.reload(); return;
    }
    if (info.app) { pageApp = pageApp || info.app; $('appVer').textContent = info.app; $('appVer').classList.remove('hidden'); }
    updInfo = info;
    const pill = $('updPill');
    pill.classList.toggle('hidden', !info.available);
    pill.classList.toggle('warn', !!info.syncWaits);
    if (info.available) {
      pill.textContent = (info.syncWaits ? 'Нужно обновление: ' : 'Доступна ') + info.available;
      pill.title = info.syncWaits ? 'Другой компьютер уже на новой версии: синхронизация продолжится после обновления' : 'Посмотреть, что нового, и обновиться';
    }
    if (dataMark !== null && info.data !== dataMark && !(overview && overview.job && overview.job.running)
        && !document.querySelector('dialog[open]')) { toast('Получены изменения с другого компьютера'); refresh(); }
    dataMark = info.data;
    if (codeVersion && v !== codeVersion && !(overview && overview.job && overview.job.running)) {
      try { sessionStorage.setItem('xf-updated', '1'); } catch (e) {}
      location.reload(); return;
    }
    codeVersion = codeVersion || v;
  } catch (e) { $('offline').classList.remove('hidden'); }
  setTimeout(watchVersion, 4000);
}
watchVersion();

// Right after a self-reload: say what arrived, so an update is visible.
(async () => {
  let updated = false;
  try { updated = sessionStorage.getItem('xf-updated') === '1'; sessionStorage.removeItem('xf-updated'); } catch (e) {}
  if (!updated) return;
  try {
    const info = await (await fetch('/api/version', { headers: { 'X-Fleet-Token': TOKEN }, cache: 'no-store' })).json();
    toast('Интерфейс обновился. Новое: ' + (info.whatsNew || 'улучшения'));
    clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').classList.add('hidden'), 8000);
  } catch (e) {}
})();
$('btnMasterRotate').addEventListener('click', () => askRotate(overview.master.name, 'главного сервера', false));

// ---------- first run: one step at a time, the network drawn as it is described ----------
function setupRole() { return (document.querySelector('#setupRoles input:checked') || {}).value || 'gateway'; }
function setupStep(n) {
  document.querySelectorAll('#setupForm .step').forEach((s) => {
    const k = +s.dataset.step;
    s.classList.toggle('active', k === n); s.classList.toggle('done', k < n);
  });
  paintSetupSums(); renderSetupMap();
  const f = document.querySelector('#setupForm .step.active input:not([type=hidden]):not([type=radio]), #setupForm .step.active select');
  if (f && n > 1) setTimeout(() => f.focus(), 30);
}
function paintSetupSums() {
  const f = $('setupForm'), sum = (n, text) => { f.querySelector('[data-step="' + n + '"] .sum').textContent = text || ''; };
  sum(1, { gateway: 'вход для зарубежных', vpn: 'сам VPN', both: 'VPN + каскады' }[setupRole()]);
  sum(2, $('s_host').value.trim() ? $('s_name').value.trim() + ' · ' + $('s_host').value.trim() : '');
  sum(3, { ip: 'сертификат на IP', domain: $('s_domain').value.trim() || 'домен', selfsigned: 'самоподписанный' }[$('s_tls').value]);
  sum(4, $('s_user').value.trim());
}
function setupStepValid(n) {
  const step = document.querySelector('#setupForm [data-step="' + n + '"]'), err = $('setupForm').querySelector('.formerr');
  for (const input of step.querySelectorAll('input[required], input[pattern]')) {
    if (!input.checkValidity()) { input.reportValidity(); return false; }
  }
  if (n === 2) {
    const auth = step.querySelector('[name=sshAuth]').value, pw = step.querySelector('[name=sshPassword]').value, key = step.querySelector('[name=sshKey]').value;
    if (auth === 'password' && !pw) { const i = step.querySelector('[name=sshPassword]'); i.setCustomValidity('Введите пароль SSH'); i.reportValidity(); i.setCustomValidity(''); return false; }
    if (auth === 'key' && !key) { const i = step.querySelector('[name=sshKey]'); i.setCustomValidity('Укажите путь к ключу'); i.reportValidity(); i.setCustomValidity(''); return false; }
  }
  if (n === 3 && $('s_tls').value === 'domain' && !$('s_domain').value.trim()) {
    $('s_domain').setCustomValidity('Введите домен'); $('s_domain').reportValidity(); $('s_domain').setCustomValidity(''); return false;
  }
  if (err) err.textContent = '';
  return true;
}
document.querySelectorAll('#setupForm [data-next]').forEach((b) => b.addEventListener('click', () => {
  const n = +b.closest('.step').dataset.step; if (setupStepValid(n)) setupStep(n + 1);
}));
document.querySelectorAll('#setupForm [data-back]').forEach((b) => b.addEventListener('click', () => setupStep(+b.closest('.step').dataset.step - 1)));
document.querySelectorAll('#setupForm .step-h').forEach((hd) => hd.addEventListener('click', () => {
  const s = hd.closest('.step'); if (s.classList.contains('done')) setupStep(+s.dataset.step);
}));
$('setupForm').addEventListener('input', () => { paintSetupSums(); renderSetupMap(); });
$('setupForm').addEventListener('change', () => { paintSetupSums(); renderSetupMap(); });
function renderSetupMap() {
  const box = $('setupMap'); box.textContent = '';
  const role = setupRole(), host = $('s_host').value.trim(), name = $('s_name').value.trim() || 'main';
  const W = 460, H = 300, cy = 150, svg = svgEl('svg', { viewBox: `0 0 ${W} ${H}`, class: 'rm', role: 'img' });
  svg.appendChild(svgEl('title', null, 'Как будет устроена сеть'));
  const ug = svgEl('g', { class: 'rm-users' });
  for (const [dx, dy] of [[-12, -8], [9, -11], [0, 9], [-17, 11], [17, 8]]) ug.appendChild(svgEl('circle', { cx: 40 + dx, cy: cy + dy, r: 6 }));
  svg.appendChild(ug);
  svg.appendChild(svgEl('text', { x: 40, y: cy + 40, class: 'rm-cap', 'text-anchor': 'middle' }, 'пользователи'));
  const st = host ? 'live' : 'idle';
  svg.appendChild(svgEl('path', { d: `M 68 ${cy} L 130 ${cy}`, class: 'rm-line ' + st }));
  const mg = svgEl('g', { class: 'rm-box master ' + (host ? 'live' : 'ghost') });
  mg.appendChild(svgEl('rect', { x: 130, y: cy - 34, width: 150, height: 68, rx: 13 }));
  mg.appendChild(svgEl('text', { x: 146, y: cy - 6, class: 'rm-t' }, role === 'vpn' ? 'VPN · ' + name : 'RU · ' + name));
  mg.appendChild(svgEl('text', { x: 146, y: cy + 16, class: 'rm-s' }, host || 'адрес сервера'));
  const exits = [];
  if (role !== 'gateway') exits.push(['интернет', 'с адреса сервера', cy - (role === 'both' ? 75 : 0), true]);
  if (role !== 'vpn') {
    exits.push(['зарубежный', 'после установки', role === 'both' ? cy + 5 : cy - 45, false]);
    exits.push(['ещё один', 'сколько угодно', role === 'both' ? cy + 80 : cy + 45, false]);
  }
  for (const [title, sub, y, real] of exits) {
    svg.appendChild(svgEl('path', { d: `M 280 ${cy} C 310 ${cy}, 300 ${y}, 330 ${y}`, class: 'rm-line ' + (real && host ? 'live' : 'idle') }));
    const g = svgEl('g', { class: 'rm-box ' + (real ? (host ? 'live' : 'ghost') : 'ghost') });
    g.appendChild(svgEl('rect', { x: 330, y: y - 24, width: 120, height: 48, rx: 10 }));
    g.appendChild(svgEl('text', { x: 342, y: y - 3, class: 'rm-t', style: 'font-size:13px' }, title));
    g.appendChild(svgEl('text', { x: 342, y: y + 14, class: 'rm-s', style: 'font-size:10.5px' }, sub));
    g.appendChild(svgEl('title', null, title + ' — ' + sub));
    svg.appendChild(g);
  }
  svg.appendChild(mg);  // drawn last: on top of the lines
  box.appendChild(svg);
  $('setupCap').textContent = {
    gateway: 'Пользователи подключаются к серверу в России, а в интернет выходят через ваши зарубежные серверы. Их вы добавите после установки — кнопкой «Добавить сервер».',
    vpn: 'Пользователи подключаются прямо к этому серверу и выходят в интернет с его адреса. Зарубежные серверы можно добавить позже.',
    both: 'У сервера свои подключения с выходом в интернет, и он же — вход для каскадов на зарубежные серверы.',
  }[role] + (host ? '' : ' Схема заполнится, когда вы укажете адрес.');
}
setupStep(1);
