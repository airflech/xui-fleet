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
// ---------- route map ----------
// The signature of the page: who goes where. Users → the RU server → each node abroad, with the latency on
// every line. A working route "flows" (moving dashes), a broken one is a red static line, unknown is grey.
const SVGNS = 'http://www.w3.org/2000/svg';
function svgEl(tag, attrs, text) {
  const e = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) if (v != null) e.setAttribute(k, v);
  if (text != null) e.textContent = text;
  return e;
}
function title2(x, y, flag, text) {  // a flag and a name, with a real gap between them
  const t = svgEl('text', { x, y, class: 'rm-t' });
  if (flag) t.appendChild(svgEl('tspan', null, flag));
  t.appendChild(svgEl('tspan', flag ? { dx: 7 } : null, text));
  return t;
}
function focusCard(key) {
  const c = document.querySelector('#nodeCards [data-node="' + CSS.escape(key) + '"]');
  if (!c) return;
  c.scrollIntoView({ behavior: 'smooth', block: 'center' });
  c.classList.remove('flash'); void c.offsetWidth; c.classList.add('flash');
}
function renderMap() {
  const box = $('routeMap');
  if (!overview || !overview.configured || !overview.master) { box.classList.add('hidden'); return; }
  box.classList.remove('hidden'); box.textContent = '';
  const m = overview.master, nodes = (overview.nodes || []).filter((n) => n.cascade || n.direct);
  const ready = live && live.ready && !live.busy && !live.failed;
  const mi = ready ? live.master : null;
  const masterOk = mi ? mi.xray === 'running' : null;
  const online = ready ? (live.online || []).filter((n) => n in (live.users || {})).length : null;
  if (box.clientWidth && box.clientWidth < 560) return renderMapNarrow(box, m, nodes, ready, mi, masterOk, online);
  const W = 1000, rowH = 74, H = Math.max(170, nodes.length * rowH + 50);
  const cy = H / 2, MX = 190, MW = 200, MH = 78, NX = 575, NW = 225, NH = 58, EX = 872;
  const svg = svgEl('svg', { viewBox: `0 0 ${W} ${H}`, class: 'rm', role: 'img' });
  svg.appendChild(svgEl('title', null, 'Маршруты: пользователи → главный сервер → зарубежные серверы'));
  const state = (ok) => (ok == null ? 'idle' : ok ? 'live' : 'down');
  const pill = (x, y, text, st) => {
    const g = svgEl('g', { class: 'rm-pill ' + st });
    const w = 14 + text.length * 7.2;
    g.appendChild(svgEl('rect', { x: x - w / 2, y: y - 11, width: w, height: 22, rx: 11 }));
    g.appendChild(svgEl('text', { x, y: y + 4, 'text-anchor': 'middle' }, text));
    svg.appendChild(g);
  };
  // users
  const ux = 70;
  const ug = svgEl('g', { class: 'rm-users' });
  for (const [dx, dy] of [[-16, -10], [12, -14], [0, 12], [-22, 14], [22, 10]]) ug.appendChild(svgEl('circle', { cx: ux + dx, cy: cy + dy, r: 7 }));
  svg.appendChild(ug);
  svg.appendChild(svgEl('text', { x: ux, y: cy + 46, class: 'rm-cap', 'text-anchor': 'middle' }, 'пользователи'));
  // users → master
  const inSt = state(masterOk);
  svg.appendChild(svgEl('path', { d: `M ${ux + 34} ${cy} L ${MX} ${cy}`, class: 'rm-line ' + inSt }));
  if (online != null) pill((ux + 34 + MX) / 2, cy, online + ' онлайн', inSt);
  // master → nodes (drawn before the boxes so lines tuck under them)
  const ny = (i) => (H - nodes.length * rowH) / 2 + i * rowH + rowH / 2;
  nodes.forEach((n, i) => {
    const info = ready && live.nodes ? live.nodes[n.name] : null;
    const ok = info ? info.status === 'online' : null;
    const st = n.cascade ? state(ok) : 'off';
    const y = ny(i), x0 = MX + MW, x1 = NX;
    svg.appendChild(svgEl('path', { d: `M ${x0} ${cy} C ${x0 + 90} ${cy}, ${x1 - 120} ${y}, ${x1} ${y}`, class: 'rm-line ' + st }));
    // the latency sits on the straight end of each line, so labels never collide near the hub
    if (n.cascade) pill(x1 - 44, y, info ? (ok ? (info.latency || 0) + ' мс' : 'нет связи') : '…', st);
  });
  // node → exits: straight to the internet from the node's own address, or through Cloudflare WARP
  const anyWarp = nodes.some((n) => n.warp);
  const spread = Math.min(46, Math.max(30, H * 0.2));
  const inetY = anyWarp ? cy - spread : cy, warpY = cy + spread;
  nodes.forEach((n, i) => {
    const info = ready && live.nodes ? live.nodes[n.name] : null;
    const st = state(info ? info.status === 'online' : null), y = ny(i), x0 = NX + NW;
    svg.appendChild(svgEl('path', { d: `M ${x0} ${y} C ${x0 + 40} ${y}, ${EX - 50} ${inetY}, ${EX - 16} ${inetY}`, class: 'rm-line exit ' + st }));
    if (n.warp) svg.appendChild(svgEl('path', { d: `M ${x0} ${y} C ${x0 + 40} ${y}, ${EX - 50} ${warpY}, ${EX - 16} ${warpY}`, class: 'rm-line warp ' + (st === 'live' ? 'live' : st) }));
  });
  // master box
  const mg = svgEl('g', { class: 'rm-box master ' + inSt, tabindex: 0 });
  mg.appendChild(svgEl('rect', { x: MX, y: cy - MH / 2, width: MW, height: MH, rx: 14 }));
  const [mfl, mtext] = splitFlag(m.display || m.labelText || 'RU');
  mg.appendChild(title2(MX + 18, cy - 8, mfl, (mtext || 'RU') + ' · главный'));
  const sub = mi && mi.cpu != null ? `cpu ${mi.cpu}% · mem ${mi.mem ?? 0}%` : (m.domain || m.host);
  mg.appendChild(svgEl('text', { x: MX + 18, y: cy + 16, class: 'rm-s' }, sub));
  mg.appendChild(svgEl('title', null, 'Главный сервер ' + m.host + (m.domain ? ' · ' + m.domain : '')));
  mg.addEventListener('click', () => focusCard('__master'));
  svg.appendChild(mg);
  // node boxes
  nodes.forEach((n, i) => {
    const info = ready && live.nodes ? live.nodes[n.name] : null;
    const ok = info ? info.status === 'online' : null;
    const y = ny(i);
    const g = svgEl('g', { class: 'rm-box ' + state(ok), tabindex: 0 });
    g.appendChild(svgEl('rect', { x: NX, y: y - NH / 2, width: NW, height: NH, rx: 12 }));
    const [fl, text] = splitFlag(n.display || n.label || n.name);
    g.appendChild(title2(NX + 16, y - 4, fl, text || n.name));
    const direct = userStats ? Object.values(userStats).filter((u) => (u.servers || []).includes(n.name)).length : null;
    const bits = [n.name];
    if (info && ok && info.cpu != null) bits.push('cpu ' + info.cpu + '%');
    if (direct != null && n.direct) bits.push(direct + ' напрямую');
    g.appendChild(svgEl('text', { x: NX + 16, y: y + 16, class: 'rm-s' }, bits.join(' · ')));
    g.appendChild(svgEl('title', null, (text || n.name) + ' · ' + n.host + (n.cascade ? '' : ' · без каскада через RU')));
    g.addEventListener('click', () => focusCard(n.name));
    svg.appendChild(g);
  });
  if (nodes.length) {
    const ig = svgEl('g', { class: 'rm-exit' });
    ig.appendChild(svgEl('circle', { cx: EX, cy: inetY, r: 15 }));
    ig.appendChild(svgEl('path', { d: `M ${EX - 15} ${inetY} H ${EX + 15} M ${EX} ${inetY - 15} C ${EX - 9} ${inetY - 6}, ${EX - 9} ${inetY + 6}, ${EX} ${inetY + 15} C ${EX + 9} ${inetY + 6}, ${EX + 9} ${inetY - 6}, ${EX} ${inetY - 15}`, class: 'rm-globe' }));
    ig.appendChild(svgEl('text', { x: EX + 24, y: inetY + 4, class: 'rm-cap strong' }, 'интернет'));
    ig.appendChild(svgEl('title', null, 'Обычный выход: сайты видят адрес зарубежного сервера'));
    svg.appendChild(ig);
    if (anyWarp) {
      const wg = svgEl('g', { class: 'rm-exit warp', tabindex: 0, role: 'button' });
      wg.appendChild(svgEl('path', { d: `M ${EX - 14} ${warpY + 8} a 7 7 0 0 1 1 -13.5 a 9.5 9.5 0 0 1 18 -2 a 7 7 0 0 1 9 7.5 a 4.5 4.5 0 0 1 -1.5 8 Z`, class: 'rm-cloud' }));
      wg.appendChild(svgEl('text', { x: EX + 24, y: warpY + 1, class: 'rm-cap strong' }, 'Cloudflare WARP'));
      wg.appendChild(svgEl('text', { x: EX + 24, y: warpY + 17, class: 'rm-cap link' }, 'как это работает?'));
      wg.appendChild(svgEl('title', null, 'Выход через Cloudflare WARP — нажмите, чтобы узнать, как это работает'));
      wg.addEventListener('click', () => $('dlgWarp').showModal());
      wg.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); $('dlgWarp').showModal(); } });
      svg.appendChild(wg);
    }
  }
  if (!nodes.length) {
    const y = cy, x0 = MX + MW;
    svg.appendChild(svgEl('path', { d: `M ${x0} ${y} L ${NX} ${y}`, class: 'rm-line ' + inSt }));
    svg.appendChild(svgEl('text', { x: NX + 10, y: y + 5, class: 'rm-cap' }, m.vpn ? 'интернет — напрямую с этого сервера' : 'добавьте зарубежный сервер'));
  }
  box.appendChild(svg);
  mapWidth = box.clientWidth;
  const legend = el('div', 'rm-legend');
  for (const [cls, txt] of [['live', 'маршрут работает'], ['down', 'нет связи'], ['idle', 'нет данных']].concat(nodes.some((n) => n.warp) ? [['warp', 'выход через WARP']] : [])) {
    const s = el('span'); s.appendChild(el('i', cls)); s.appendChild(document.createTextNode(txt)); legend.appendChild(s);
  }
  if (nodes.some((n) => n.warp)) {
    const q = el('button', 'linkbtn', 'Что такое WARP?'); q.type = 'button'; q.addEventListener('click', () => $('dlgWarp').showModal());
    legend.insertBefore(q, legend.firstChild);
  }
  box.appendChild(legend);
}
// Phones: the same story top to bottom — users, the RU server, then a trunk line feeding every node.
function renderMapNarrow(box, m, nodes, ready, mi, masterOk, online) {
  const state = (ok) => (ok == null ? 'idle' : ok ? 'live' : 'down');
  const W = 360, top = 128, rowH = 64, H = top + Math.max(1, nodes.length) * rowH + 8, inSt = state(masterOk);
  const svg = svgEl('svg', { viewBox: `0 0 ${W} ${H}`, class: 'rm', role: 'img' });
  svg.appendChild(svgEl('title', null, 'Маршруты: пользователи → главный сервер → зарубежные серверы'));
  const ug = svgEl('g', { class: 'rm-users' });
  for (const [dx, dy] of [[-10, -4], [8, -6], [0, 8], [-16, 8], [16, 6]]) ug.appendChild(svgEl('circle', { cx: 40 + dx, cy: 22 + dy, r: 5 }));
  svg.appendChild(ug);
  svg.appendChild(svgEl('text', { x: 70, y: 27, class: 'rm-cap' }, online != null ? 'пользователи · ' + online + ' онлайн' : 'пользователи'));
  svg.appendChild(svgEl('path', { d: 'M 40 38 L 40 56', class: 'rm-line ' + inSt }));
  const mg = svgEl('g', { class: 'rm-box master ' + inSt, tabindex: 0 });
  mg.appendChild(svgEl('rect', { x: 14, y: 56, width: W - 28, height: 52, rx: 12 }));
  const [mfl, mtext] = splitFlag(m.display || m.labelText || 'RU');
  mg.appendChild(title2(30, 79, mfl, (mtext || 'RU') + ' · главный'));
  mg.appendChild(svgEl('text', { x: 30, y: 97, class: 'rm-s' }, mi && mi.cpu != null ? `cpu ${mi.cpu}% · mem ${mi.mem ?? 0}%` : (m.domain || m.host)));
  mg.addEventListener('click', () => focusCard('__master'));
  svg.appendChild(mg);
  nodes.forEach((n, i) => {
    const info = ready && live.nodes ? live.nodes[n.name] : null;
    const ok = info ? info.status === 'online' : null, st = n.cascade ? state(ok) : 'off';
    const y = top + i * rowH + rowH / 2;
    svg.appendChild(svgEl('path', { d: `M 40 108 L 40 ${y - 14} Q 40 ${y} 54 ${y} L 64 ${y}`, class: 'rm-line ' + st }));
    const g = svgEl('g', { class: 'rm-box ' + state(ok), tabindex: 0 });
    g.appendChild(svgEl('rect', { x: 64, y: y - 24, width: W - 78, height: 48, rx: 10 }));
    const [fl, text] = splitFlag(n.display || n.label || n.name);
    g.appendChild(title2(78, y - 3, fl, text || n.name));
    g.appendChild(svgEl('text', { x: 78, y: y + 15, class: 'rm-s' }, n.name));
    if (n.cascade) {
      const t = info ? (ok ? (info.latency || 0) + ' мс' : 'нет связи') : '…';
      g.appendChild(svgEl('text', { x: W - 26, y: y - 1, class: 'rm-lat ' + st, 'text-anchor': 'end' }, t));
    }
    if (n.warp) g.appendChild(svgEl('text', { x: W - 26, y: y + 15, class: 'rm-s warp', 'text-anchor': 'end' }, '+ WARP'));
    g.addEventListener('click', () => focusCard(n.name));
    svg.appendChild(g);
  });
  box.appendChild(svg);
  if (nodes.some((n) => n.warp)) {
    const legend = el('div', 'rm-legend');
    const q = el('button', 'linkbtn', 'Что такое WARP?'); q.type = 'button'; q.addEventListener('click', () => $('dlgWarp').showModal());
    legend.appendChild(q); box.appendChild(legend);
  }
}
let mapWidth = 0;
window.addEventListener('resize', () => {
  const w = $('routeMap').clientWidth;
  if ((w < 560) !== (mapWidth < 560)) renderMap();
  mapWidth = w;
});
function renderServers() {
  renderAttention();
  renderMap();
  const box = $('nodeCards');
  box.textContent = '';
  const nodes = (overview && overview.nodes) || [];
  $('cntServers').textContent = nodes.length + 1;
  $('noNodes').classList.toggle('hidden', nodes.length > 0 || !!(overview && overview.master && overview.master.vpn));
  if (overview && overview.master) box.appendChild(masterCard(overview.master));
  for (const n of nodes) {
    const info = live && live.nodes ? live.nodes[n.name] : null;
    const card = el('article', 'card' + (info && info.status !== 'online' ? ' down' : ''));
    card.dataset.node = n.name;
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
      'Проверьте, что сервер включён, затем «Обслуживание» → «Перенастроить все серверы». Подключения к другим серверам работают.'));
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
  card.dataset.node = '__master';
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
  if (info && info.xrayError) card.appendChild(problem(humanError(info.xrayError), '«Обслуживание» → «Перенастроить все серверы» — сервис перезапустит Xray и проверит настройки.'));
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
