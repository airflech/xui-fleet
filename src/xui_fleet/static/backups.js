// The «Панели 3x-ui» and «Бэкапы» tabs.
// Classic scripts in order (see ui.html): top-level names are shared between the files.

// ---------- panels ----------
let panelsAfterJob = false;
async function loadPanels() {
  const box = $('panelRows');
  box.textContent = '';
  const wait = el('div', 'panel'); const e = el('div', 'empty'); e.appendChild(el('span', 'spin')); e.appendChild(document.createTextNode('  Подключаюсь к серверам и проверяю вход…'));
  wait.appendChild(e); box.appendChild(wait);
  $('btnPanels').disabled = true;
  let data;
  try { data = await api('/api/panels'); }
  catch (err) { box.textContent = ''; box.appendChild(el('div', 'notice bad', 'Не удалось получить данные: ' + err.message)); $('btnPanels').disabled = false; return; }
  $('btnPanels').disabled = false;
  $('btnPanels').lastChild.textContent = 'Обновить';
  box.textContent = '';
  if (data.busy) { box.appendChild(el('div', 'notice', 'Идёт операция — попробуйте после её завершения.')); return; }
  const grid = el('div', 'creds');
  for (const p of data.panels) {
    const c = el('article', 'card');
    const h = el('div', 'card-h');
    const [fl, text] = splitFlag(p.title);
    h.appendChild(el('div', 'flag', fl || '🖥'));
    const g = el('div', 'grow'); g.appendChild(el('div', 'title', text || p.title)); g.appendChild(el('div', 'meta mono', p.name)); h.appendChild(g);
    if (p.valid) h.appendChild(el('span', 'tag ok', 'вход проверен'));
    c.appendChild(h);
    const kv = el('div', 'kv');
    const row = (k, valueNode, actions) => { kv.appendChild(el('div', 'k', k)); kv.appendChild(valueNode); const a = el('div'); a.style.cssText = 'display:flex;gap:4px'; (actions || []).forEach((x) => a.appendChild(x)); kv.appendChild(a); };
    const iconBtn = (name, label, fn) => { const b = el('button', 'btn ghost sm icon'); b.appendChild(icon(name)); b.title = label; b.setAttribute('aria-label', label); b.addEventListener('click', fn); return b; };
    if (p.url) {
      const a = el('a', 'secret', p.url.replace(/^https?:\/\//, '')); a.href = p.url; a.target = '_blank'; a.rel = 'noopener noreferrer';
      row('Адрес', a, [iconBtn('ext', 'Открыть панель', () => window.open(p.url, '_blank', 'noopener')), iconBtn('copy', 'Скопировать адрес', () => copy(p.url, 'Адрес'))]);
    }
    if (p.username) row('Логин', el('span', 'secret', p.username), [iconBtn('copy', 'Скопировать логин', () => copy(p.username, 'Логин'))]);
    if (p.password) {
      const s = el('span', 'secret', '••••••••••••');
      const eye = iconBtn('eye', 'Показать пароль', () => { const hid = s.textContent.startsWith('•'); s.textContent = hid ? p.password : '••••••••••••'; });
      row('Пароль', s, [eye, iconBtn('copy', 'Скопировать пароль', () => copy(p.password, 'Пароль'))]);
    }
    c.appendChild(kv);
    if (p.selfSigned) c.appendChild(el('div', 'hint', 'Сертификат самоподписанный: браузер предупредит — это ожидаемо.'));
    if (p.error) c.appendChild(el('div', 'err', p.error));
    if (p.url) {
      const f = el('div', 'card-f');
      const reset = el('button', 'btn ghost sm', 'Задать новый пароль'); reset.dataset.access = p.name;
      reset.addEventListener('click', () => confirmAction('Новый пароль для ' + (text || p.name) + '?',
        'Логин останется прежним, пароль станет случайным из 16 символов. Панель перезапустится на несколько секунд — VPN-подключения это не затронет.',
        async () => { if (await start('/api/panel/password', { name: p.name })) panelsAfterJob = true; }));
      f.appendChild(reset); c.appendChild(f);
    }
    grid.appendChild(c);
  }
  box.appendChild(grid);
}

// ---------- backups ----------
let backupList = [];
async function loadBackups() {
  const box = $('backupRows');
  if (!overview || !overview.master || !overview.master.prepared) { box.textContent = ''; box.appendChild(el('div', 'empty', 'Бэкапы появятся после настройки главного сервера.')); return; }
  let data;
  try { data = await api('/api/backups'); } catch (e) { box.textContent = ''; box.appendChild(el('div', 'empty err', e.message)); return; }
  backupList = data.backups || [];
  $('backupDir').textContent = data.dir || '';
  box.textContent = '';
  if (!backupList.length) { const e = el('div', 'empty'); e.appendChild(el('b', null, 'Бэкапов ещё нет')); e.appendChild(document.createTextNode('Нажмите «Сделать бэкап».')); box.appendChild(e); return; }
  backupList.forEach((b, i) => {
    const r = el('div', 'item');
    const d = new Date(b.created * 1000);
    const when = el('div', 'when');
    when.appendChild(el('div', 'title', d.toLocaleDateString('ru-RU', { day: 'numeric', month: 'short' }) + ', ' + d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' })));
    when.appendChild(el('div', 'meta', i === 0 ? 'последний' : ''));
    r.appendChild(when);
    const g = el('div', 'grow');
    const why = (b.reason || 'причина не записана').replace(/^./, (c) => c.toUpperCase());
    const w = el('div', 'title', why); w.title = why; w.style.fontWeight = '550';
    g.appendChild(w);
    g.appendChild(el('div', 'meta', [plural(b.clients, 'клиент', 'клиента', 'клиентов'), plural(b.inbounds, 'подключение', 'подключения', 'подключений'),
      plural(b.nodes, 'сервер', 'сервера', 'серверов'), b.domain || b.host].join(' · ')));
    r.appendChild(g);
    r.appendChild(el('div', 'muted small num size', Math.round(b.size / 1024) + ' КБ'));
    box.appendChild(r);
  });
}
function openMigrate() {
  const form = $('migrateForm'); resetForm(form);
  const sel = $('m_backup'); sel.textContent = '';
  const fresh = el('option', null, 'Свежий со старого сервера, иначе последний'); fresh.value = ''; sel.appendChild(fresh);
  for (const b of backupList) { const o = el('option', null, new Date(b.created * 1000).toLocaleString('ru-RU') + ' — клиентов ' + b.clients); o.value = b.file; sel.appendChild(o); }
  $('dlgMigrate').showModal();
}
