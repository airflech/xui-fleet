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
    // WARP that stopped passing traffic (the 5-minute check through the real entries), with the fix one click away.
    const wc = live.warp && live.warp.nodes ? live.warp.nodes : {};
    for (const [name, ms] of Object.entries(wc)) {
      if (ms !== null) continue;
      const nd = (overview.nodes || []).find((x) => x.name === name);
      const label = name === m.name ? 'WARP с главного сервера' : (splitFlag(nd ? nd.display || nd.name : name)[1] || name) + ': WARP';
      add(label + ' не работает — починить', true, () => confirmAction('Починить ' + label + '?',
        'Приложение заново выберет точку входа Cloudflare (с выходом за границей), перезапустит Xray на этом сервере и проверит WARP '
        + 'настоящим трафиком. Займёт около минуты; остальные подключения сервера на пару секунд переподключатся.',
        () => start('/api/warp/fix', { name }), 'Починить'));
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
  const ready = live && live.ready && !live.busy && !live.failed;
  // What is really there (the entries on the panel), not what the settings ask for — see realMap()
  const { m, nodes, mVpn, mWarp, carries } = realMap(ready);
  const mi = ready ? live.master : null;
  const masterOk = mi ? mi.xray === 'running' : null;
  const online = ready ? (live.online || []).filter((n) => n in (live.users || {})).length : null;
  if (box.clientWidth && box.clientWidth < 560) return renderMapNarrow(box, m, nodes, ready, mi, masterOk, online);
  // a direct route (users → server, past RU) runs under the master box: rows a bit taller, room below
  const anyDirect = nodes.some((n) => n.direct);
  // the master's own exits (it is a VPN itself) run over the top (internet) and under the bottom (WARP)
  const topLane = mVpn && nodes.length ? 48 : 0, botLane = mWarp && nodes.length ? 48 : 0;
  const W = 1000, rowH = anyDirect ? 84 : 74;
  const H = Math.max(anyDirect ? 200 : 170, nodes.length * rowH + (anyDirect ? 70 : 50)) + 2 * Math.max(topLane, botLane);
  // node boxes a little to the left: the exit lines need room for WARP's own delay
  const cy = H / 2, MX = 190, MW = 200, MH = 78, NX = 540, NW = 215, NH = 58, EX = 872;
  const svg = svgEl('svg', { viewBox: `0 0 ${W} ${H}`, class: 'rm', role: 'img' });
  svg.appendChild(svgEl('title', null, 'Маршруты: пользователи → главный сервер → зарубежные серверы'));
  const state = (ok) => (ok == null ? 'idle' : ok ? 'live' : 'down');
  const pill = (x, y, text, st, tip) => {
    const g = svgEl('g', { class: 'rm-pill ' + st });
    const w = 14 + text.length * 7.2;
    g.appendChild(svgEl('rect', { x: x - w / 2, y: y - 11, width: w, height: 22, rx: 11 }));
    g.appendChild(svgEl('text', { x, y: y + 4, 'text-anchor': 'middle' }, text));
    if (tip) g.appendChild(svgEl('title', null, tip));
    svg.appendChild(g);
  };
  // users
  const ux = 70;
  svg.appendChild(usersIcon(ux, cy, 1));
  svg.appendChild(svgEl('text', { x: ux, y: cy + 46, class: 'rm-cap', 'text-anchor': 'middle' }, 'пользователи'));
  if (online != null) svg.appendChild(svgEl('text', { x: ux, y: cy + 62, class: 'rm-cap', 'text-anchor': 'middle' }, online + ' онлайн'));
  // users → master: the delay from this computer to the RU server; red when it does not answer or Xray is down
  const legs = routeLegs(nodes, masterOk);
  const inSt = legs.master;
  // No traffic through the master (no own VPN, no route via RU): users only fetch the subscription from it
  // ...and when it carries none and there are servers, it is not on the route at all: no box, users go straight
  // to the servers (a note under the users says where the subscription comes from)
  const hubOnMap = carries || !nodes.length;
  const toRu = ready && live.rtt ? live.rtt.master : null;
  if (hubOnMap) {
    const inCls = carries ? inSt : 'sub' + (inSt === 'down' ? ' down' : '');
    svg.appendChild(svgEl('path', { d: `M ${ux + 34} ${cy} L ${MX} ${cy}`, class: 'rm-line ' + inCls }));
    if (!carries && inSt !== 'down') pill((ux + 34 + MX) / 2, cy, 'подписка', 'sub', 'Через главный сервер трафик не идёт: пользователи берут с него только подписку'
      + (toRu != null ? ' (' + toRu + ' мс от этого компьютера)' : ''));
    else if (toRu != null || inSt === 'down') pill((ux + 34 + MX) / 2, cy, toRu != null ? toRu + ' мс' : 'нет связи', inSt, 'От этого компьютера до главного сервера');
  } else {
    // two short lines: the column under the users is narrow
    const note = svgEl('text', { x: ux, y: cy + 80, class: 'rm-cap' + (inSt === 'down' ? ' bad' : ''), 'text-anchor': 'middle' });
    note.appendChild(svgEl('tspan', { x: ux, dy: 0 }, 'подписка —'));
    note.appendChild(svgEl('tspan', { x: ux, dy: 15 }, inSt === 'down' ? 'главный не отвечает' : 'с главного сервера'));
    note.appendChild(svgEl('title', null, 'Трафик через главный сервер не идёт (нет ни своих подключений, ни каскада): пользователи берут с него только подписку и подключаются к серверам напрямую'));
    svg.appendChild(note);
  }
  const ny = (i) => (H - nodes.length * rowH) / 2 + i * rowH + rowH / 2;
  // Each node takes up to two lines on its left: via RU and direct. A node above the middle gets its direct line
  // over the master box and into its upper half, one below — under it and into its lower half: nothing crosses.
  // Three or more servers: the direct lines become one bus — under the master box, then down a column left of
  // the servers, with a short branch into each. Separate curves from the users to many servers would cross the
  // whole fan via RU.
  const directNodes = nodes.filter((n) => n.direct), bus = directNodes.length >= 3, busX = NX - 92;
  const ends = nodes.map((n, i) => {
    const y = ny(i), above = !bus && y < cy - 1;
    if (!n.direct) return { ru: y, direct: null, above };
    if (!n.cascade) return { ru: null, direct: y, above };  // no route via RU: the direct line takes the middle
    return above ? { ru: y + 13, direct: y - 17, above } : { ru: y - 13, direct: y + 17, above };
  });
  const pills = [];
  // The direct line passes the master box on a flat lane with clear air (26 px), then turns to its server:
  // users → under (or over) the box → the server. A single curve would graze the box's edge.
  const laneOff = MH / 2 + 26;
  const directPath = (sgn, toX, toY) => {
    if (!hubOnMap) return `M ${ux + 34} ${cy} C ${ux + 160} ${cy}, ${toX - 170} ${toY}, ${toX} ${toY}`;  // nothing in the way
    const by = cy + sgn * laneOff, sx = ux + 24, sy = cy + sgn * 20;
    return `M ${sx} ${sy} C ${sx + 30} ${by}, ${MX - 30} ${by}, ${MX} ${by} L ${MX + MW} ${by} `
      + `C ${MX + MW + 90} ${by}, ${toX - 110} ${toY}, ${toX} ${toY}`;
  };
  const directMs = (n) => (ready && live.rtt && live.rtt.nodes ? live.rtt.nodes[n.name] : null);
  const directPill = (n, y, st) => {
    const d = directMs(n);
    if (d != null || st === 'down') pills.push([NX - 44, y, d != null ? d + ' мс' : 'нет связи', 'direct ' + st, 'Напрямую с этого компьютера до ' + n.name + ', без ' + hub()]);
  };
  if (bus) {
    const ys = nodes.map((n, i) => (n.direct ? ends[i].direct : null)).filter((v) => v != null);
    const sts = directNodes.map((n) => legs.nodes[n.name].direct);
    const trunk = sts.includes('live') ? 'live' : sts.every((s) => s === 'down') ? 'down' : 'idle';
    const top = Math.min(...ys), bottom = Math.max(...ys);
    // where the line from the users meets the column: under the master box, or level with the users without it
    const join = Math.min(bottom, Math.max(top, hubOnMap ? cy + laneOff + 30 : cy));
    svg.appendChild(svgEl('path', { d: directPath(1, busX, join), class: 'rm-line direct ' + trunk }));
    svg.appendChild(svgEl('path', { d: `M ${busX} ${top} L ${busX} ${bottom}`, class: 'rm-line direct ' + trunk }));
    nodes.forEach((n, i) => {
      if (!n.direct) return;
      const st = legs.nodes[n.name].direct, y = ends[i].direct;
      svg.appendChild(svgEl('path', { d: `M ${busX} ${y} L ${NX} ${y}`, class: 'rm-line direct ' + st }));
      directPill(n, y, st);
    });
  } else {
    // users → node directly, past RU
    nodes.forEach((n, i) => {
      if (!n.direct) return;
      const e = ends[i], st = legs.nodes[n.name].direct, sgn = e.above ? -1 : 1;
      svg.appendChild(svgEl('path', { d: directPath(sgn, NX, e.direct), class: 'rm-line direct ' + st }));
      directPill(n, e.direct, st);
    });
  }
  // master → nodes: a fan, each line leaves the master at its own height
  const fan = nodes.filter((n) => n.cascade);
  nodes.forEach((n, i) => {
    const info = ready && live.nodes ? live.nodes[n.name] : null;
    if (!n.cascade) return;  // traffic never goes through RU to this server: no line from it
    const st = legs.nodes[n.name].ru;
    // the fan stays within the master box's height, however many servers
    const step = fan.length > 1 ? Math.min(14, (MH - 20) / (fan.length - 1)) : 0;
    const k = fan.indexOf(n), sy = k < 0 ? cy : cy + (k - (fan.length - 1) / 2) * step;
    const y = ends[i].ru, x0 = MX + MW, x1 = NX;
    svg.appendChild(svgEl('path', { d: `M ${x0} ${sy} C ${x0 + 70} ${sy}, ${x1 - 120} ${y}, ${x1} ${y}`, class: 'rm-line ' + st }));
    if (n.cascade) pills.push([x1 - 44, y, info ? (st !== 'down' ? ruToNode(n, info) + ' мс' : 'нет связи') : '…', st, 'От главного сервера до ' + n.name]);
  });
  for (const p of pills) pill(...p);
  // node → exits: straight to the internet from the node's own address, or through Cloudflare WARP.
  // Up to two servers: a curve from each. Three or more: two buses (internet, WARP) right of the servers, with a
  // short branch from each server — curves from many servers to two points would make a lattice.
  const anyWarp = nodes.some((n) => n.warp) || mWarp;
  const spread = Math.min(46, Math.max(30, H * 0.2));
  const inetY = anyWarp ? cy - spread : cy, warpY = cy + spread;
  const offered = (n) => n.offered !== false;  // a server with no entry in subscriptions has no exits
  const exitBus = nodes.filter(offered).length >= 3, x0 = NX + NW, busI = x0 + 14, busW = x0 + 74;
  const warpPills = [];
  const anyOf = (sts) => (sts.includes('live') ? 'live' : sts.length && sts.every((v) => v === 'down') ? 'down' : 'idle');
  if (exitBus) {
    const iy = nodes.map((n, i) => (offered(n) ? ny(i) - (n.warp ? 8 : 0) : null));
    const wy = nodes.map((n, i) => (n.warp ? ny(i) + 8 : null));
    const iSt = anyOf(nodes.filter(offered).map((n) => legs.nodes[n.name].exit));
    nodes.forEach((n, i) => { if (iy[i] != null) svg.appendChild(svgEl('path', { d: `M ${x0} ${iy[i]} L ${busI} ${iy[i]}`, class: 'rm-line exit ' + legs.nodes[n.name].exit })); });
    const iys = iy.filter((v) => v != null), iTop = Math.min(...iys, inetY), iBot = Math.max(...iys, inetY);
    svg.appendChild(svgEl('path', { d: `M ${busI} ${iTop} L ${busI} ${iBot} M ${busI} ${inetY} L ${EX - 16} ${inetY}`, class: 'rm-line exit ' + iSt }));
    const wNodes = nodes.filter((n) => n.warp);
    if (wNodes.length) {
      const ws = wy.filter((v) => v != null), wSt = anyOf(wNodes.map((n) => legs.nodes[n.name].warp));
      nodes.forEach((n, i) => {
        if (!n.warp) return;
        const st = legs.nodes[n.name].warp;
        svg.appendChild(svgEl('path', { d: `M ${x0} ${wy[i]} L ${busW} ${wy[i]}`, class: 'rm-line warp ' + st }));
        if (st === 'down') warpPills.push([(busI + busW) / 2, wy[i], '✕', n, 'down']);
        else if (n.warpMs != null) warpPills.push([(busI + busW) / 2, wy[i], '+' + n.warpMs, n]);
      });
      const wTop = Math.min(...ws, warpY), wBot = Math.max(...ws, warpY);
      svg.appendChild(svgEl('path', { d: `M ${busW} ${wTop} L ${busW} ${wBot} M ${busW} ${warpY} L ${EX - 16} ${warpY}`, class: 'rm-line warp ' + wSt }));
    }
  } else {
    nodes.forEach((n, i) => {
      if (!offered(n)) return;
      const l = legs.nodes[n.name], y = ny(i);
      svg.appendChild(svgEl('path', { d: `M ${x0} ${y} C ${x0 + 40} ${y}, ${EX - 50} ${inetY}, ${EX - 16} ${inetY}`, class: 'rm-line exit ' + l.exit }));
      if (n.warp) svg.appendChild(svgEl('path', { d: `M ${x0} ${y} C ${x0 + 40} ${y}, ${EX - 50} ${warpY}, ${EX - 16} ${warpY}`, class: 'rm-line warp ' + l.warp }));
      if (!n.warp) return;
      // WARP's own delay (or a failed check) sits on its line, past the point where it leaves the internet line
      const t = 0.5, u = 1 - t;
      const px = u * u * u * x0 + 3 * u * u * t * (x0 + 40) + 3 * u * t * t * (EX - 50) + t * t * t * (EX - 16);
      const py = u * u * u * y + 3 * u * u * t * y + 3 * u * t * t * warpY + t * t * t * warpY;
      if (l.warp === 'down') warpPills.push([px, py, '✕', n, 'down']);
      else if (n.warpMs != null) warpPills.push([px, py, '+' + n.warpMs, n]);
    });
  }
  // The master as a VPN itself: its own traffic leaves from it — to the internet over the top of the map, to WARP
  // under the bottom (not through the foreign servers)
  if (nodes.length && mVpn) {
    const ly = topLane / 2, sx = MX + MW - 30;
    svg.appendChild(svgEl('path', { d: `M ${sx} ${cy - MH / 2} C ${sx} ${ly + 30}, ${sx + 10} ${ly}, ${sx + 50} ${ly} L ${EX - 60} ${ly} C ${EX - 20} ${ly}, ${EX} ${ly + 10}, ${EX} ${inetY - 16}`, class: 'rm-line exit ' + inSt }));
    pill((sx + EX) / 2, ly, 'выход в интернет с главного', inSt, 'Главный сервер сам VPN-сервер: у его подключений сайты видят его адрес');
  }
  if (nodes.length && mWarp) {
    const ly = H - botLane / 2, sx = MX + MW - 30;
    // its state and delay from the same 5-minute check as the servers' WARP (keyed by the master's name)
    const wc = ready && live.warp ? live.warp : null, mname = overview.master.name;
    const wOk = wc && wc.nodes && mname in wc.nodes ? wc.nodes[mname] != null : null;
    const wSt = inSt === 'down' || wOk === false ? 'down' : 'live';
    const wMs = wc && wc.added ? wc.added[mname] : null;
    svg.appendChild(svgEl('path', { d: `M ${sx} ${cy + MH / 2} C ${sx} ${ly - 30}, ${sx + 10} ${ly}, ${sx + 50} ${ly} L ${EX - 60} ${ly} C ${EX - 20} ${ly}, ${EX} ${ly - 10}, ${EX} ${warpY + 10}`, class: 'rm-line warp ' + wSt }));
    pill((sx + EX) / 2, ly, 'WARP с главного' + (wSt === 'down' ? ' ✕' : wMs != null ? ' +' + wMs + ' мс' : ''), wSt === 'down' ? 'down' : 'warp',
      wSt === 'down' ? 'WARP главного сервера не пропустил трафик при последней проверке (раз в 5 минут)'
        : 'Подключение через главный сервер с выходом через Cloudflare WARP' + (wMs != null ? ': добавляет ' + wMs + ' мс (ping с главного до Cloudflare)' : ''));
  }
  // master box — only when the master is on the route
  if (hubOnMap) {
    const mg = svgEl('g', { class: 'rm-box master ' + inSt, tabindex: 0 });
    mg.appendChild(svgEl('rect', { x: MX, y: cy - MH / 2, width: MW, height: MH, rx: 14 }));
    const [mfl, mtext] = splitFlag(m.display || m.labelText || '');
    mg.appendChild(title2(MX + 18, cy - 8, mfl, mtext ? mtext + ' · главный' : 'Главный сервер'));
    const sub = mi && mi.cpu != null ? `cpu ${mi.cpu}% · mem ${mi.mem ?? 0}%` : (m.domain || m.host);
    mg.appendChild(svgEl('text', { x: MX + 18, y: cy + 16, class: 'rm-s' }, sub));
    mg.appendChild(svgEl('title', null, 'Главный сервер ' + m.host + (m.domain ? ' · ' + m.domain : '')));
    mg.addEventListener('click', () => focusCard('__master'));
    svg.appendChild(mg);
  }
  // node boxes
  nodes.forEach((n, i) => {
    const info = ready && live.nodes ? live.nodes[n.name] : null;
    const ok = info ? info.status === 'online' : null;
    const y = ny(i);
    const g = svgEl('g', { class: 'rm-box ' + state(ok), tabindex: 0 });
    g.appendChild(svgEl('rect', { x: NX, y: y - NH / 2, width: NW, height: NH, rx: 12 }));
    const [fl, text] = splitFlag(n.display || n.label || n.name);
    g.appendChild(title2(NX + 16, y - 4, fl, text || n.name));
    if (!offered(n)) {  // set up, but no entry of it is switched on and in subscriptions
      g.setAttribute('class', 'rm-box off');
      g.appendChild(svgEl('text', { x: NX + 16, y: y + 16, class: 'rm-s' }, 'нет подключений в подписке'));
      g.appendChild(svgEl('title', null, (text || n.name) + ': ни одно подключение не включено или все скрыты из подписок — пользователи его не получают'));
      g.addEventListener('click', () => focusCard(n.name));
      svg.appendChild(g);
      return;
    }
    const direct = userStats ? Object.values(userStats).filter((u) => (u.servers || []).includes(n.name)).length : null;
    const bits = [n.name];
    if (info && ok && info.cpu != null) bits.push('cpu ' + info.cpu + '%');
    if (direct != null && n.direct) bits.push(direct + ' напрямую');
    // the whole route's delay: to RU + RU → here (+ WARP when the entry goes through it)
    const r = routeDelay(n, info);
    // totals of both ways in: via RU and straight (WARP's share is on its own line to the cloud)
    // a way that is down is said so, never added up from a leg that does not answer
    const l = legs.nodes[n.name];
    const ruV = !n.cascade ? null : l.ru === 'down' || legs.master === 'down' ? '✕' : r.via;
    const dV = !n.direct ? null : l.direct === 'down' ? '✕' : r.direct;
    const shown = [ruV, dV].filter((v) => v != null);
    // the longest wording that fits the box (monospace: ~6.9 px a character at this size)
    const fits = (t) => t.length * 6.9 <= NW - 30, H = hub();
    const unit = (v) => (v === '✕' ? '✕' : v + ' мс');
    const forms = shown.length === 2
      ? ['через ' + H + ' ' + unit(ruV) + ' · напрямую ' + unit(dV), 'через ' + H + ' ' + ruV + ' · напрямую ' + unit(dV),
         H + ' ' + ruV + ' · напрямую ' + unit(dV), H + ' ' + ruV + ' · прямо ' + unit(dV), 'каскад ' + ruV + ' · прямо ' + unit(dV)]
      : ruV != null ? ['через ' + H + ' ' + unit(ruV), 'каскад ' + unit(ruV)] : dV != null ? ['напрямую ' + unit(dV), 'прямо ' + unit(dV)] : [];
    const plain = bits.join(' · ');
    const total = forms.find(fits) || forms[forms.length - 1] || (fits(plain) ? plain : n.name);
    g.appendChild(svgEl('text', { x: NX + 16, y: y + 16, class: 'rm-s' + (shown.length ? ' total' : '') }, total));
    const hh = histHealth && histHealth.nodes[n.name];
    if (hh) g.appendChild(sparkline(hh.points, NX + NW - 96, y - 24, 82, 16));
    g.appendChild(svgEl('title', null, (text || n.name) + ' · ' + bits.join(' · ') + ' · ' + n.host + (n.cascade ? '' : ' · без каскада через ' + H)
      + (r.via != null ? ' · через ' + H + ': ' + r.toRu + ' + ' + r.ruNode + ' = ' + r.via + ' мс' + (r.viaWarp != null ? ', с WARP ' + r.viaWarp + ' мс' : '') : '')
      + (hh ? ' · доступность за 24 ч: ' + hh.uptime + '%' : '') + ' — нажмите для графика'));
    g.addEventListener('click', () => openRoute(n.name));
    svg.appendChild(g);
  });
  for (const [px, py, text, n, bad] of warpPills) pill(px, py, text, bad ? 'down' : 'warp', bad
    ? 'WARP на ' + n.name + ' не пропустил трафик при последней проверке (раз в 5 минут, с главного сервера)'
    : 'WARP добавляет ' + n.warpMs + ' мс на ' + n.name + ' (ping с сервера до Cloudflare, раз в 5 минут)');
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
      const wms = warpDelays(nodes);
      wg.appendChild(svgEl('title', null, 'Выход через Cloudflare WARP' + (wms.detail ? '. ' + wms.detail : '') + ' — нажмите, чтобы узнать, как это работает'));
      wg.addEventListener('click', () => $('dlgWarp').showModal());
      wg.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); $('dlgWarp').showModal(); } });
      svg.appendChild(wg);
    }
  }
  if (!nodes.length) {
    const y = cy, x0 = MX + MW;
    svg.appendChild(svgEl('path', { d: `M ${x0} ${y} L ${NX} ${y}`, class: 'rm-line ' + inSt }));
    svg.appendChild(svgEl('text', { x: NX + 10, y: y + 5, class: 'rm-cap' }, mVpn ? 'интернет — напрямую с этого сервера' : 'добавьте зарубежный сервер'));
  }
  box.appendChild(svg);
  mapWidth = box.clientWidth;
  const legend = el('div', 'rm-legend');
  for (const [cls, txt] of [['live', 'маршрут работает'], ['down', 'нет связи'], ['idle', 'нет данных']]
    .concat(nodes.some((n) => n.direct) ? [['direct', 'напрямую, без ' + hub()]] : [])
    .concat(nodes.some((n) => n.warp) ? [['warp', 'выход через WARP']] : [])) {
    const s = el('span'); s.appendChild(el('i', cls)); s.appendChild(document.createTextNode(txt)); legend.appendChild(s);
  }
  if (nodes.some((n) => n.warp)) {
    const q = el('button', 'linkbtn', 'Что такое WARP?'); q.type = 'button'; q.addEventListener('click', () => $('dlgWarp').showModal());
    legend.insertBefore(q, legend.firstChild);
  }
  box.appendChild(legend);
  renderRouteDelays(box, nodes, ready);
}
// Phones: the same story top to bottom — users, the RU server, then a trunk line feeding every node.
function renderMapNarrow(box, m, nodes, ready, mi, masterOk, online) {
  const state = (ok) => (ok == null ? 'idle' : ok ? 'live' : 'down');
  const W = 360, top = 128, rowH = 64, H = top + Math.max(1, nodes.length) * rowH + 8;
  const legs = routeLegs(nodes, masterOk), inSt = legs.master;
  const svg = svgEl('svg', { viewBox: `0 0 ${W} ${H}`, class: 'rm', role: 'img' });
  svg.appendChild(svgEl('title', null, 'Маршруты: пользователи → главный сервер → зарубежные серверы'));
  svg.appendChild(usersIcon(40, 22, 0.62));
  const toRuN = ready && live.rtt ? live.rtt.master : null;
  svg.appendChild(svgEl('text', { x: 70, y: 27, class: 'rm-cap' }, (online != null ? 'пользователи · ' + online + ' онлайн' : 'пользователи')
    + (toRuN != null ? ' · до ' + hub() + ' ' + toRuN + ' мс' : '')));
  svg.appendChild(svgEl('path', { d: 'M 40 38 L 40 56', class: 'rm-line ' + (realMap(ready).carries ? inSt : 'sub' + (inSt === 'down' ? ' down' : '')) }));
  const mg = svgEl('g', { class: 'rm-box master ' + inSt, tabindex: 0 });
  mg.appendChild(svgEl('rect', { x: 14, y: 56, width: W - 28, height: 52, rx: 12 }));
  const [mfl, mtext] = splitFlag(m.display || m.labelText || '');
  mg.appendChild(title2(30, 79, mfl, mtext ? mtext + ' · главный' : 'Главный сервер'));
  // its role as it really is: a VPN itself, only subscriptions, or the hop via RU
  const real = realMap(ready);
  const role = real.mVpn ? ' · выход в интернет' : !real.carries ? ' · только подписка' : '';
  mg.appendChild(svgEl('text', { x: 30, y: 97, class: 'rm-s' }, (mi && mi.cpu != null ? `cpu ${mi.cpu}% · mem ${mi.mem ?? 0}%` : (m.domain || m.host)) + role));
  mg.addEventListener('click', () => focusCard('__master'));
  svg.appendChild(mg);
  nodes.forEach((n, i) => {
    const info = ready && live.nodes ? live.nodes[n.name] : null;
    const ok = info ? info.status === 'online' : null, l = legs.nodes[n.name], st = n.cascade ? l.ru : 'off';
    const y = top + i * rowH + rowH / 2;
    svg.appendChild(svgEl('path', { d: `M 40 108 L 40 ${y - 14} Q 40 ${y} 54 ${y} L 64 ${y}`, class: 'rm-line ' + st }));
    const g = svgEl('g', { class: 'rm-box ' + state(ok), tabindex: 0 });
    g.appendChild(svgEl('rect', { x: 64, y: y - 24, width: W - 78, height: 48, rx: 10 }));
    const [fl, text] = splitFlag(n.display || n.label || n.name);
    g.appendChild(title2(78, y - 3, fl, text || n.name));
    const rn = routeDelay(n, info);
    g.appendChild(svgEl('text', { x: 78, y: y + 15, class: 'rm-s' + (n.direct ? ' direct ' + l.direct : '') },
      n.direct ? (l.direct === 'down' ? 'напрямую: нет связи' : rn.direct != null ? 'напрямую ' + rn.direct + ' мс' : 'напрямую …') : n.name));
    if (n.cascade) {
      const r = routeDelay(n, info);
      const t = info ? (ok && l.ru !== 'down' && inSt !== 'down' ? (r.via != null ? 'итого ' + r.via + ' мс' : ruToNode(n, info) + ' мс') : 'через ' + hub() + ': нет связи') : '…';
      g.appendChild(svgEl('text', { x: W - 26, y: y - 1, class: 'rm-lat ' + (t.includes('нет связи') ? 'down' : st), 'text-anchor': 'end' }, t));
    }
    if (n.warp) g.appendChild(svgEl('text', { x: W - 26, y: y + 15, class: 'rm-s warp' + (l.warp === 'down' ? ' down' : ''), 'text-anchor': 'end' },
      l.warp === 'down' ? 'WARP: нет связи' : n.warpMs != null ? 'WARP +' + n.warpMs + ' мс' : '+ WARP'));
    g.addEventListener('click', () => openRoute(n.name));
    svg.appendChild(g);
  });
  box.appendChild(svg);
  if (nodes.some((n) => n.warp)) {
    const legend = el('div', 'rm-legend');
    const q = el('button', 'linkbtn', 'Что такое WARP?'); q.type = 'button'; q.addEventListener('click', () => $('dlgWarp').showModal());
    legend.appendChild(q); box.appendChild(legend);
  }
  renderRouteDelays(box, nodes, ready);
}
// Users: three people — two behind, one in front (head and shoulders), centred on (x, y), scaled by k.
function usersIcon(x, y, k) {
  const g = svgEl('g', { class: 'rm-users', transform: `translate(${x} ${y}) scale(${k})` });
  const person = (dx, dy, r, cls) => {
    const p = svgEl('g', { class: cls, transform: `translate(${dx} ${dy})` });
    p.appendChild(svgEl('circle', { cx: 0, cy: -r * 1.15, r }));
    p.appendChild(svgEl('path', { d: `M ${-r * 1.75} ${r * 1.55} C ${-r * 1.75} ${r * 0.2}, ${-r * 0.9} ${-r * 0.05}, 0 ${-r * 0.05} `
      + `C ${r * 0.9} ${-r * 0.05}, ${r * 1.75} ${r * 0.2}, ${r * 1.75} ${r * 1.55} Z` }));
    g.appendChild(p);
  };
  person(-15, -2, 6.5, 'back');
  person(15, -2, 6.5, 'back');
  person(0, 4, 8.5, 'front');
  g.appendChild(svgEl('title', null, 'Пользователи'));
  return g;
}
// The master's own name (its label in the settings) — never assume it is a Russian server: "RU" only when it is
// called so. Without a name: «главный».
function hub() {
  const m = overview && overview.master;
  return (m ? splitFlag(m.labelText || '')[1] : '').trim() || 'главный';
}
// The map from what is really on the panel (live.routes): a server's way in counts only if such an entry is switched
// on and goes into subscriptions. Before live data arrives — from the settings. A server with no entry offered stays
// on the map, marked, so it does not just vanish.
function realMap(ready) {
  const m = overview.master, routes = ready && live.routes;
  // what WARP adds: from the 5-minute check (the same request with and without WARP), else the daily measurement
  const added = ready && live.warp && live.warp.added ? live.warp.added : {};
  const nodes = (overview.nodes || []).map((n) => (added[n.name] != null ? Object.assign({}, n, { warpMs: added[n.name] }) : n)).map((n) => {
    if (!routes) return n;
    const r = routes.nodes[n.name] || { cascade: false, direct: false, warp: false };
    return Object.assign({}, n, { cascade: r.cascade, direct: r.direct ? n.direct || 443 : null, warp: r.warp, offered: r.cascade || r.direct });
  }).filter((n) => n.cascade || n.direct || n.offered === false);
  const mVpn = routes ? routes.master.vpn : !!m.vpn, mWarp = routes ? routes.master.warp : false;
  return { m, nodes, mVpn, mWarp, carries: mVpn || mWarp || nodes.some((n) => n.cascade) };
}
// The state of every leg of the map: 'live' (works), 'down' (does not answer — drawn red), 'idle' (no data yet).
//   master — this computer → RU (ping) and Xray on RU; per node: ru — RU → node (the panel sees the node and the
//   master's ping reaches it), direct — this computer → node, exit — the node itself, warp — the last real-traffic
//   check of the node's WARP (every 5 minutes), else the node's own state.
function routeLegs(nodes, masterOk) {
  const ready = live && live.ready && !live.busy && !live.failed;
  const rtt = ready ? live.rtt : null, fm = rtt && rtt.fromMaster ? rtt.fromMaster : null;
  const warp = ready && live.warp && live.warp.nodes ? live.warp.nodes : {};
  const st = (ok) => (ok == null ? 'idle' : ok ? 'live' : 'down');
  const masterReach = rtt ? rtt.master != null : null;
  const out = { master: masterOk === false || masterReach === false ? 'down' : st(masterOk == null && masterReach == null ? null : true), nodes: {} };
  for (const n of nodes) {
    const info = ready && live.nodes ? live.nodes[n.name] : null;
    const nodeOk = info ? info.status === 'online' : null;
    const exit = st(nodeOk);
    const ruReach = fm && Object.keys(fm).length ? fm[n.name] != null : null;
    const ru = nodeOk === false || ruReach === false ? 'down' : st(nodeOk == null ? null : true);
    const direct = !rtt ? 'idle' : nodeOk === false || rtt.nodes[n.name] == null ? 'down' : 'live';
    const w = n.name in warp ? (warp[n.name] == null ? 'down' : 'live') : exit;
    out.nodes[n.name] = { ru, direct, exit, warp: exit === 'down' ? 'down' : w };
  }
  return out;
}
// One route's delay, ms: this computer → RU, RU → the server, their sum, the sum with WARP, and straight there.
function routeDelay(n, info) {
  const rtt = live && live.ready && live.rtt;
  const toRu = rtt ? rtt.master : null;
  const ruNode = rtt && info && info.status === 'online' ? ruToNode(n, info) : null;
  const via = toRu != null && ruNode != null ? toRu + ruNode : null;
  return { toRu, ruNode, via, viaWarp: via != null && n.warpMs != null ? via + n.warpMs : null,
           direct: rtt && rtt.nodes ? rtt.nodes[n.name] : null };
}
// RU → server delay for the map: the network round trip measured on the master, else the panel's own figure.
function ruToNode(n, info) {
  const m = live && live.rtt && live.rtt.fromMaster;
  return m && m[n.name] != null ? m[n.name] : (info && info.latency) || 0;
}
// Under the map: what each route costs in delay, leg by leg. Direct = this computer → the server; via RU =
// this computer → RU + RU → the server (the panel's figure); the difference is what the RU server adds.
// WARP adds its own share on top of either (measured on the server once a day).
function renderRouteDelays(box, nodes, ready) {
  // RU → server: the network round trip measured on the master (the panel's latency is an HTTP call and runs high)
  const rtt = ready && live.rtt, lat = (n) => (rtt && rtt.fromMaster && rtt.fromMaster[n.name] != null ? rtt.fromMaster[n.name] : null);
  if (!nodes.length || !rtt) return;
  const legs = routeLegs(nodes, ready && live.master ? live.master.xray === 'running' : null);
  const wrap = el('div', 'rdelay');
  const head = el('div', 'rdelay-h');
  head.appendChild(el('b', null, 'Задержка до серверов'));
  head.appendChild(el('span', 'muted small', ' — ping с этого компьютера и с главного сервера, туда и обратно. Если на этом компьютере включён VPN, цифры неточные.'));
  wrap.appendChild(head);
  const ms = (v) => (v == null ? '—' : v + ' мс');
  const sign = (v) => (v > 0 ? '+' + v : v < 0 ? '−' + Math.abs(v) : '±0') + ' мс';
  for (const n of nodes) {
    const row = el('div', 'rdelay-row');
    const [fl, text] = splitFlag(n.display || n.label || n.name);
    row.appendChild(el('span', 'rdelay-name', (fl ? fl + ' ' : '') + (text || n.name)));
    const chip = (k, v, cls, tip) => { const c = el('span', 'rchip' + (cls ? ' ' + cls : '')); c.appendChild(el('span', 'k', k)); c.appendChild(el('span', 'v', v)); if (tip) c.title = tip; row.appendChild(c); };
    const l = legs.nodes[n.name], viaDown = legs.master === 'down' || l.ru === 'down';
    const direct = l.direct === 'down' ? null : rtt.nodes ? rtt.nodes[n.name] : null, toRu = rtt.master, ruNode = viaDown ? null : lat(n);
    const via = toRu != null && ruNode != null ? toRu + ruNode : null;
    // totals first: what a user actually gets on each kind of connection; a way that is down says so in red
    if (n.direct) chip('итого напрямую', l.direct === 'down' ? 'нет связи' : ms(direct), l.direct === 'down' ? 'bad' : 'total',
      'Этот компьютер → ' + (text || n.name));
    if (n.cascade) chip('итого через ' + hub(), viaDown ? 'нет связи' : via == null ? '—' : toRu + ' + ' + ruNode + ' = ' + via + ' мс',
      viaDown ? 'bad' : 'total', legs.master === 'down' ? 'Главный сервер не отвечает' : l.ru === 'down' ? 'Главный сервер не достаёт до ' + (text || n.name)
        : 'Этот компьютер → ' + hub() + ' (' + ms(toRu) + ') + ' + hub() + ' → ' + (text || n.name) + ' (' + ms(ruNode) + ')');
    if (n.warp && l.warp === 'down') chip('WARP', 'не работает', 'bad', 'Последняя проверка трафиком через WARP (раз в 5 минут) не прошла');
    else if (n.cascade && n.warp && via != null && n.warpMs != null)
      chip('итого через ' + hub() + ' → WARP', via + ' + ' + n.warpMs + ' = ' + (via + n.warpMs) + ' мс', 'total warp',
        'Путь через ' + hub() + ' (' + via + ' мс) и выход через WARP (+' + n.warpMs + ' мс)');
    if (n.cascade && n.direct && via != null && direct != null) {
      const d = via - direct;
      chip(hub() + ' добавляет', sign(d), d > 25 ? 'warn' : 'ok', 'Насколько путь через ' + hub() + ' дольше прямого (отрицательное — быстрее)');
    }
    if (n.warp && l.warp !== 'down' && !(n.cascade && via != null && n.warpMs != null)) chip('WARP добавляет', n.warpMs != null ? sign(n.warpMs)
      // no check yet (the app has just started): it is running now, the figure comes with the next refresh
      : live.warp && live.warp.at ? 'не измерено' : 'проверяю…', 'warp',
      'Сверх выбранного пути, если подключение идёт через WARP: ping с сервера до Cloudflare, раз в 5 минут.');
    wrap.appendChild(row);
  }
  box.appendChild(wrap);
}
// What WARP adds to the delay (measured on each server once a day): "+12 мс", or "+8–30 мс" for several servers.
function warpDelays(nodes) {
  const ms = nodes.filter((n) => n.warp && n.warpMs != null);
  if (!ms.length) return { text: '', detail: '' };
  const vals = ms.map((n) => n.warpMs), lo = Math.min(...vals), hi = Math.max(...vals);
  const detail = 'Добавляет к задержке: ' + ms.map((n) => (splitFlag(n.display || n.label || n.name)[1] || n.name) + ' +' + n.warpMs + ' мс').join(', ')
    + ' (ping с сервера до Cloudflare, раз в 5 минут)';
  return { text: lo === hi ? '+' + lo + ' мс' : lo === 0 ? 'до +' + hi + ' мс' : '+' + lo + '–' + hi + ' мс', detail };
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
      // the same figure as on the map: ping from the main server (the panel's own probe only as a fallback)
      const ping = live.rtt && live.rtt.fromMaster ? live.rtt.fromMaster[n.name] : null;
      if (ok) st.appendChild(el('span', 'muted small num', (ping != null ? ping : info.latency) + ' мс от ' + hub()));
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
        : 'Пользователей, подключённых прямо к этому серверу; кто идёт через ' + hub() + ' — в «Онлайн всего» главного сервера';
      s.appendChild(os);
      s.appendChild(stat('CPU', (info.cpu ?? 0) + '%', info.cpu));
      s.appendChild(stat('Память', (info.mem ?? 0) + '%', info.mem));
      card.appendChild(s);
    }
    const f = el('div', 'card-f');
    const way = n.cascade && n.direct ? 'через ' + hub() + ' и напрямую' : n.cascade ? 'только через ' + hub() : n.direct ? 'только напрямую' : 'подключений нет';
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
  form.querySelector('[name=warp]').checked = !!m.warp;
  form.querySelector('[name=label]').value = splitFlag(m.labelText)[1];
  const sel = form.querySelector('[name=flag]'); fillFlags(sel, m.country); sel.value = m.flag || 'auto';
  for (const k of ['direct', 'xhttp', 'hy2', 'warp']) form.querySelector('[name=show_' + k + ']').checked = !(m.hide || []).includes(k);
  // a VPN server already has a WARP entry in its set (when the template has WARP): the separate switch is for the rest
  const sync = () => { const vpn = form.querySelector('[name=vpn]').checked;
    $('roleOwn').classList.toggle('hidden', !vpn); $('roleWarpRow').classList.toggle('hidden', vpn); };
  form.querySelector('[name=vpn]').onchange = sync; sync();
  $('dlgRole').showModal();
}
