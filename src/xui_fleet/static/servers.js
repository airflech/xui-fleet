// The «Серверы» tab: the main server and node cards, «Требует внимания», the domains dialog helpers.
// Classic scripts in order (see ui.html): top-level names are shared between the files.

// ---------- servers ----------
function meter(pct) { const m = el('div', 'meter'), i = el('i', pct >= 85 ? 'hi' : ''); i.style.width = Math.min(100, pct || 0) + '%'; m.appendChild(i); return m; }
function plural(n, one, few, many) {
  const a = Math.abs(n) % 100, b = a % 10;
  return n + ' ' + (a > 10 && a < 20 ? many : b === 1 ? one : b >= 2 && b <= 4 ? few : many);
}
// Errors from the panel come as raw Go messages with URLs (and the secret panel path): say what it means instead.
function humanError(msg) {
  const m = (msg || '').toLowerCase();
  if (/deadline exceeded|timeout|timed out|i\/o timeout/.test(m)) return 'Панель сервера не отвечает — истекло время ожидания';
  if (/connection refused/.test(m)) return 'Сервер отклонил подключение к панели';
  if (/no route|unreachable|network is/.test(m)) return 'Сервер недоступен по сети';
  if (/certificate|x509|tls/.test(m)) return 'Ошибка защищённого соединения с панелью';
  if (/eof|reset by peer/.test(m)) return 'Соединение с панелью оборвалось';
  if (/xray/.test(m)) return 'Xray на сервере не запущен';
  return (msg || '').replace(/https?:\/\/\S+/g, '').replace(/\s+/g, ' ').trim().slice(0, 140) || 'Неизвестная ошибка';
}
function problem(title, hint) { const p = el('div', 'problem'); p.appendChild(el('b', null, title)); if (hint) p.appendChild(document.createTextNode(hint)); return p; }
function stat(k, v, pct) { const s = el('div', 'stat'); s.appendChild(el('div', 'k', k)); s.appendChild(el('div', 'v', v)); if (pct != null) s.appendChild(meter(pct)); return s; }

// "Needs attention": what is wrong right now, across the whole network, one click from where to fix it.
// Text cut with "…" shows in full on hover: any element that is narrower than its text gets a tooltip.
document.addEventListener('mouseover', (e) => {
  const x = e.target;
  if (!(x instanceof HTMLElement) || x.title || x.children.length || !x.textContent.trim()) return;
  if (x.scrollWidth > x.clientWidth + 1) x.title = x.textContent.trim();
});
function renderAttention() {
  const box = $('attention'); box.textContent = '';
  const items = [];
  const add = (text, bad, fn) => items.push({ text, bad, fn });
  if (overview && overview.configured && live && live.ready && !live.busy && !live.failed) {
    const m = overview.master;
    if (live.master && live.master.xray && live.master.xray !== 'running') add('Главный сервер: Xray остановлен', true, null);
    if (m.domain && domainState(m.domain) === false) add('Подписка не открывается по ' + m.domain, true, () => { renderDomains(); $('dlgDomains').showModal(); });
    for (const n of overview.nodes || []) {
      const i = live.nodes && live.nodes[n.name];
      if (i && i.status !== 'online') add((splitFlag(n.display || n.name)[1] || n.name) + ': не в сети', true, null);
    }
    if (userStats) {
      const need = Object.keys(live.users || {}).filter((n) => attentionOf(userStats[n]).length);
      if (need.length) add('Пользователи: ' + need.length + ' — срок, трафик или лимиты', false, () => {
        userFilter = 'attention';
        document.querySelectorAll('[data-filter]').forEach((x) => x.setAttribute('aria-pressed', String(x.dataset.filter === 'attention')));
        showTab('users'); renderUsers();
      });
    }
    const lb = m.lastBackup;
    if (m.prepared && (!lb || Date.now() / 1000 - lb > 3 * 86400)) add(lb ? 'Последнему бэкапу больше 3 дней' : 'Бэкапов главного сервера ещё нет', false, () => showTab('backups'));
  }
  box.classList.toggle('hidden', !items.length);
  if (!items.length) return;
  box.appendChild(el('div', 'h', 'Требует внимания'));
  for (const it of items) {
    const b = el('button', it.bad ? 'bad' : null); b.type = 'button';
    b.appendChild(el('span', 'dot ' + (it.bad ? 'bad' : 'warn'))); b.appendChild(document.createTextNode(it.text));
    if (it.fn) b.addEventListener('click', it.fn); else b.style.cursor = 'default';
    box.appendChild(b);
  }
}
function renderServers() {
  renderAttention();
  const box = $('nodeCards');
  box.textContent = '';
  const nodes = (overview && overview.nodes) || [];
  $('cntServers').textContent = nodes.length + 1;
  $('noNodes').classList.toggle('hidden', nodes.length > 0 || !!(overview && overview.master && overview.master.vpn));
  if (overview && overview.master) box.appendChild(masterCard(overview.master));
  for (const n of nodes) {
    const info = live && live.nodes ? live.nodes[n.name] : null;
    const card = el('article', 'card' + (info && info.status !== 'online' ? ' down' : ''));
    const h = el('div', 'card-h');
    const [fl, text] = splitFlag(n.display || n.label || n.name);
    h.appendChild(el('div', 'flag', fl || '🖥'));
    const g = el('div', 'grow');
    g.appendChild(el('div', 'title', text || n.name));
    g.appendChild(el('div', 'meta mono', n.name + ' · ' + n.host));
    h.appendChild(g);
    const more = el('button', 'btn ghost sm icon'); more.setAttribute('aria-haspopup', 'menu'); more.setAttribute('aria-label', 'Действия с сервером');
    more.dataset.access = n.name; more.appendChild(icon('more'));
    more.addEventListener('click', (e) => toggleMenu($('nodeMenu'), e.currentTarget, () => { menuNode = n; }));
    h.appendChild(more);
    card.appendChild(h);

    // status line
    const st = el('div', 'row');
    st.style.cssText = 'display:flex;align-items:center;gap:8px;flex-wrap:wrap';
    if (!live) { const s = el('div', 'skeleton'); s.style.width = '140px'; st.appendChild(s); }
    else if (live.busy) st.appendChild(el('span', 'tag', 'идёт операция'));
    else if (live.failed) st.appendChild(el('span', 'tag', 'нет данных — повторяю…'));
    else if (!info) st.appendChild(el('span', 'tag warn', n.prepared ? 'не подключён к главной панели' : 'не настроен'));
    else {
      const ok = info.status === 'online';
      const t = el('span', 'tag ' + (ok ? 'ok' : 'bad')); t.appendChild(el('span', 'dot ' + (ok ? 'ok' : 'bad'))); t.lastChild.style.boxShadow = 'none';
      t.appendChild(document.createTextNode(ok ? 'В сети' : 'Недоступен')); st.appendChild(t);
      if (ok) st.appendChild(el('span', 'muted small num', info.latency + ' мс от RU'));
    }
    card.appendChild(st);
    if (info && info.status !== 'online') card.appendChild(problem(humanError(info.error),
      'Проверьте, что сервер включён, затем нажмите «Применить» — сервис перепроверит его. Подключения к другим серверам работают.'));
    else if (info && info.error) card.appendChild(problem(humanError(info.error)));

    if (info && info.status === 'online') {
      const s = el('div', 'stats');
      // Users connected straight to this server (from the user statistics); people who come through RU
      // are counted on the main server. Before the statistics arrive: the panel's own counter.
      const direct = userStats ? Object.values(userStats).filter((u) => (u.servers || []).includes(n.name)).length : null;
      const os = stat(direct == null ? 'Онлайн' : 'Напрямую', String(direct == null ? (info.online ?? 0) : direct));
      os.title = direct == null ? 'Клиентов на сервере сейчас (по данным панели)'
        : 'Пользователей, подключённых прямо к этому серверу; кто идёт через RU — в «Онлайн всего» главного сервера';
      s.appendChild(os);
      s.appendChild(stat('CPU', (info.cpu ?? 0) + '%', info.cpu));
      s.appendChild(stat('Память', (info.mem ?? 0) + '%', info.mem));
      card.appendChild(s);
    }
    const f = el('div', 'card-f');
    const way = n.cascade && n.direct ? 'через RU и напрямую' : n.cascade ? 'только через RU' : n.direct ? 'только напрямую' : 'подключений нет';
    f.appendChild(el('span', null, way));
    if (info && info.panel) { const sp = el('span'); sp.style.flex = '1'; f.appendChild(sp); const v = el('span', 'mono', 'v' + info.panel); v.title = 'Версия 3x-ui'; f.appendChild(v); }
    card.appendChild(f);
    box.appendChild(card);
  }
}

function uptimeText(sec) {
  if (!sec) return '';
  const d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600);
  return d ? d + ' д ' + h + ' ч' : h ? h + ' ч' : Math.max(1, Math.round(sec / 60)) + ' мин';
}
function masterCard(m) {
  const info = live && live.master;
  const card = el('article', 'card main-srv');
  const h = el('div', 'card-h');
  const [fl, text] = splitFlag(m.display || '');
  const fb = el('div', 'flag', fl);
  if (!fl) { fb.appendChild(icon('net')); fb.firstChild.style.cssText = 'width:24px;height:24px;color:var(--accent)'; }
  h.appendChild(fb);
  const g = el('div', 'grow');
  g.appendChild(el('div', 'title', 'Главный сервер' + (m.vpn && text ? ' · ' + text : '')));
  g.appendChild(el('div', 'meta mono', m.name + ' · ' + (m.domain || m.host)));
  h.appendChild(g);
  const more = el('button', 'btn ghost sm icon'); more.setAttribute('aria-haspopup', 'menu'); more.setAttribute('aria-label', 'Действия с главным сервером');
  more.dataset.access = m.name; more.appendChild(icon('more'));
  more.addEventListener('click', (e) => toggleMenu($('masterMenu'), e.currentTarget));
  h.appendChild(more);
  card.appendChild(h);

  const roles = el('div', 'chips');
  const chip = (t, on) => { const c = el('span', 'tag' + (on ? ' ok' : ''), t); roles.appendChild(c); };
  chip('подписки', true);
  if (m.vpn) chip('VPN-сервер', true);
  if (m.cascades) chip('каскады → ' + plural((overview.nodes || []).length || 0, 'сервер', 'сервера', 'серверов'), true);
  card.appendChild(roles);

  const st = el('div'); st.style.cssText = 'display:flex;align-items:center;gap:8px;flex-wrap:wrap';
  if (!m.prepared) st.appendChild(el('span', 'tag warn', 'не настроен'));
  else if (!live) { const s = el('div', 'skeleton'); s.style.width = '140px'; st.appendChild(s); }
  else if (live.busy) st.appendChild(el('span', 'tag', 'идёт операция'));
  else if (!info || !info.xray) st.appendChild(el('span', 'tag', live.failed ? 'нет данных — повторяю…' : 'нет данных'));
  else {
    const ok = info.xray === 'running';
    const t = el('span', 'tag ' + (ok ? 'ok' : 'bad')); const d = el('span', 'dot ' + (ok ? 'ok' : 'bad')); d.style.boxShadow = 'none';
    t.appendChild(d); t.appendChild(document.createTextNode(ok ? 'Работает' : 'Xray остановлен')); st.appendChild(t);
    if (info.uptime) st.appendChild(el('span', 'muted small', 'включён ' + uptimeText(info.uptime)));
  }
  card.appendChild(st);
  if (info && info.xrayError) card.appendChild(problem(humanError(info.xrayError), 'Нажмите «Применить» — сервис перезапустит Xray и проверит настройки.'));
  if (m.domain && domainState(m.domain) === false) {
    const r0 = (m.reserveDomains || [])[0];
    card.appendChild(problem('Подписка не открывается по ' + m.domain + ' из вашей сети. ',
      r0 && domainState(r0) ? 'Резервный ' + r0 + ' работает: «⋯» → «Домены и адреса».' : 'Проверьте DNS домена и что сервер включён.'));
  }
  if (info && info.cpu != null) {
    const s = el('div', 'stats');
    const online = live && live.online ? live.online.filter((n) => n in (live.users || {})).length : 0;
    const os = stat('Онлайн всего', String(online)); os.title = 'Пользователей подключено сейчас — на любом сервере сети'; s.appendChild(os);
    s.appendChild(stat('CPU', info.cpu + '%', info.cpu));
    const mem = stat('Память', (info.mem ?? 0) + '%', info.mem); mem.title = 'Диск занят на ' + (info.disk ?? 0) + '%';
    s.appendChild(mem);
    card.appendChild(s);
  }
  const f = el('div', 'card-f');
  const rsv = (m.reserveDomains || [])[0];
  const dom = el('span', null, m.domain ? m.domain + (rsv ? ' · резерв ' + rsv : '') : 'без домена · ' + m.host); dom.title = dom.textContent;
  f.appendChild(dom);
  if (info && info.panel) { const sp = el('span'); sp.style.flex = '1'; f.appendChild(sp); const v = el('span', 'mono', 'v' + info.panel); v.title = 'Версия 3x-ui'; f.appendChild(v); }
  card.appendChild(f);
  return card;
}

function openRole() {
  closeMenus();
  const m = overview.master, form = $('roleForm');
  resetForm(form);
  form.querySelector('[name=vpn]').checked = !!m.vpn;
  form.querySelector('[name=cascades]').checked = !!m.cascades;
  form.querySelector('[name=label]').value = splitFlag(m.labelText)[1];
  const sel = form.querySelector('[name=flag]'); fillFlags(sel, m.country); sel.value = m.flag || 'auto';
  for (const k of ['direct', 'xhttp', 'hy2', 'warp']) form.querySelector('[name=show_' + k + ']').checked = !(m.hide || []).includes(k);
  const sync = () => $('roleOwn').classList.toggle('hidden', !form.querySelector('[name=vpn]').checked);
  form.querySelector('[name=vpn]').onchange = sync; sync();
  $('dlgRole').showModal();
}
