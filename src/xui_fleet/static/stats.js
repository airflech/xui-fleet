// The «Статистика» tab, the route dialog and the latency sparklines on the map. History lives on this computer
// (see history.py): route health every 5 minutes, traffic every hour, while the app runs.
// Charts are plain SVG: one axis, thin marks, hover tooltips, a legend for several series, a table of the numbers.

// Server identity in charts: a fixed order (validated for colour blindness on both themes), by the server's place
// in the list — never by rank, so a filter never repaints a server.
const SERIES = ['var(--s1)', 'var(--s2)', 'var(--s3)', 'var(--s4)', 'var(--s5)', 'var(--s6)'];
function serverColor(name) {
  const names = ((overview && overview.nodes) || []).map((n) => n.name);
  const i = names.indexOf(name);
  return i < 0 ? 'var(--muted)' : i < SERIES.length ? SERIES[i] : 'var(--muted)';  // 7th+ server: "other"
}
function serverText(name) {
  const n = ((overview && overview.nodes) || []).find((x) => x.name === name);
  const [fl, text] = splitFlag(n ? (n.display || n.name) : name);
  return (fl ? fl + ' ' : '') + (text || name);
}

let histHealth = null, histHealthAt = 0;
async function loadHealthHistory(force) {
  if (!force && Date.now() - histHealthAt < 240000) return histHealth;
  try { histHealth = await api('/api/history/health?hours=24'); histHealthAt = Date.now(); } catch (e) { /* no history yet */ }
  return histHealth;
}

// ---------- tooltip shared by every chart ----------
function tip(e, lines) {
  const t = $('chartTip');
  if (!lines) { t.classList.add('hidden'); return; }
  t.textContent = '';
  lines.forEach((l, i) => t.appendChild(el(i ? 'div' : 'b', null, l)));
  t.classList.remove('hidden');
  const x = Math.min(e.clientX + 14, innerWidth - t.offsetWidth - 8), y = Math.max(8, e.clientY - t.offsetHeight - 10);
  t.style.left = x + 'px'; t.style.top = y + 'px';
}
const hhmm = (t) => new Date(t * 1000).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
const dayLabel = (d) => new Date(d + 'T12:00:00').toLocaleDateString('ru-RU', { day: 'numeric', month: 'short' });
const whenLabel = (t, long) => (long ? new Date(t * 1000).toLocaleDateString('ru-RU', { day: 'numeric', month: 'short' }) + ', ' : '') + hhmm(t);

// ---------- latency over time: one line, gaps where the route was down ----------
function latencyChart(box, points, long) {
  box.textContent = '';
  if (!points || points.length < 2) { box.appendChild(el('div', 'empty small', 'История копится: точка каждые 5 минут, пока приложение запущено.')); return; }
  const W = 640, H = 170, L = 40, R = 10, T = 12, B = 24;
  const t0 = points[0][0], t1 = points[points.length - 1][0], span = Math.max(1, t1 - t0);
  const vals = points.map((p) => p[1]).filter((v) => v != null);
  const max = Math.max(50, Math.ceil(Math.max(...vals, 1) * 1.15 / 25) * 25);
  const X = (t) => L + (W - L - R) * (t - t0) / span, Y = (v) => T + (H - T - B) * (1 - v / max);
  const svg = svgEl('svg', { viewBox: `0 0 ${W} ${H}`, class: 'chart', role: 'img' });
  svg.appendChild(svgEl('title', null, 'Задержка ' + hub() + ' → сервер, мс'));
  for (const v of [0, max / 2, max]) {  // recessive grid, one axis
    svg.appendChild(svgEl('line', { x1: L, x2: W - R, y1: Y(v), y2: Y(v), class: 'grid' }));
    svg.appendChild(svgEl('text', { x: L - 6, y: Y(v) + 4, class: 'axis', 'text-anchor': 'end' }, Math.round(v)));
  }
  for (const k of [0, 0.5, 1]) {
    const t = t0 + span * k;
    svg.appendChild(svgEl('text', { x: X(t), y: H - 6, class: 'axis', 'text-anchor': k === 0 ? 'start' : k === 1 ? 'end' : 'middle' }, whenLabel(t, long)));
  }
  // down periods: a band in the status colour, labelled in the tooltip
  points.forEach((p, i) => {
    if (p[1] != null) return;
    const a = X(points[Math.max(0, i - 1)][0]), b = X(points[Math.min(points.length - 1, i + 1)][0]);
    svg.appendChild(svgEl('rect', { x: (a + X(p[0])) / 2, y: T, width: Math.max(2, (b - a) / 2), height: H - T - B, class: 'down' }));
  });
  let d = '', pen = false;
  for (const [t, v] of points) {
    if (v == null) { pen = false; continue; }
    d += (pen ? ' L ' : ' M ') + X(t).toFixed(1) + ' ' + Y(v).toFixed(1); pen = true;
  }
  svg.appendChild(svgEl('path', { d, class: 'lat' }));
  const cross = svgEl('line', { y1: T, y2: H - B, class: 'cross hidden' }), dot = svgEl('circle', { r: 4, class: 'dot hidden' });
  svg.appendChild(cross); svg.appendChild(dot);
  const hit = svgEl('rect', { x: L, y: 0, width: W - L - R, height: H, class: 'hit' });
  hit.addEventListener('mousemove', (e) => {
    const r = svg.getBoundingClientRect(), t = t0 + span * ((e.clientX - r.left) * W / r.width - L) / (W - L - R);
    let best = points[0]; for (const p of points) if (Math.abs(p[0] - t) < Math.abs(best[0] - t)) best = p;
    cross.setAttribute('x1', X(best[0])); cross.setAttribute('x2', X(best[0])); cross.classList.remove('hidden');
    if (best[1] != null) { dot.setAttribute('cx', X(best[0])); dot.setAttribute('cy', Y(best[1])); dot.classList.remove('hidden'); } else dot.classList.add('hidden');
    tip(e, [whenLabel(best[0], long), best[1] == null ? 'нет связи' : best[1] + ' мс']);
  });
  hit.addEventListener('mouseleave', (e) => { cross.classList.add('hidden'); dot.classList.add('hidden'); tip(e, null); });
  svg.appendChild(hit);
  box.appendChild(svg);
}

// ---------- traffic per day, stacked by server ----------
function trafficBars(box, days, series) {
  box.textContent = '';
  const totals = days.map((_d, i) => series.reduce((s, x) => s + x.values[i], 0));
  if (!days.length || !totals.some((v) => v > 0)) { box.appendChild(el('div', 'empty small', 'Нужно хотя бы два часовых замера — график появится сам.')); return; }
  const W = 640, H = 200, L = 52, R = 8, T = 10, B = 24, n = days.length;
  const max = Math.max(...totals) * 1.1, slot = (W - L - R) / n, bw = Math.max(4, Math.min(28, slot * 0.62));
  const Y = (v) => T + (H - T - B) * (1 - v / max);
  const svg = svgEl('svg', { viewBox: `0 0 ${W} ${H}`, class: 'chart', role: 'img' });
  svg.appendChild(svgEl('title', null, 'Трафик по дням'));
  for (const v of [0, max / 2, max]) {
    svg.appendChild(svgEl('line', { x1: L, x2: W - R, y1: Y(v), y2: Y(v), class: 'grid' }));
    svg.appendChild(svgEl('text', { x: L - 6, y: Y(v) + 4, class: 'axis', 'text-anchor': 'end' }, v ? fmtBytes(v) : '0'));
  }
  days.forEach((d, i) => {
    const x = L + slot * i + (slot - bw) / 2;
    if (n <= 10 || i % Math.ceil(n / 8) === 0) svg.appendChild(svgEl('text', { x: x + bw / 2, y: H - 6, class: 'axis', 'text-anchor': 'middle' }, dayLabel(d)));
    let acc = 0;
    const segs = series.filter((s) => s.values[i] > 0);
    segs.forEach((s, k) => {
      const v = s.values[i], y0 = Y(acc), y1 = Y(acc + v);
      const top = k === segs.length - 1;
      // 2px surface gap between stacked segments; the top one gets the rounded data-end
      svg.appendChild(svgEl('rect', { x, y: y1 + (top ? 0 : 1), width: bw, height: Math.max(1, y0 - y1 - (top ? 1 : 2)), rx: top ? 3 : 0,
        style: 'fill:' + s.color, class: 'bar' }));
      acc += v;
    });
    const hit = svgEl('rect', { x: L + slot * i, y: T, width: slot, height: H - T - B, class: 'hit' });
    hit.addEventListener('mousemove', (e) => tip(e, [dayLabel(d) + ' · ' + fmtBytes(totals[i])].concat(
      series.filter((s) => s.values[i] > 0).map((s) => s.label + ': ' + fmtBytes(s.values[i])))));
    hit.addEventListener('mouseleave', (e) => tip(e, null));
    svg.appendChild(hit);
  });
  box.appendChild(svg);
  const lg = el('div', 'legend');
  for (const s of series) { const it = el('span'); const sw = el('i'); sw.style.background = s.color; it.appendChild(sw); it.appendChild(document.createTextNode(s.label)); lg.appendChild(it); }
  box.appendChild(lg);
}

// ---------- a single series as horizontal bars, values written out ----------
function hbars(box, rows) {
  box.textContent = '';
  if (!rows.length) { box.appendChild(el('div', 'empty small', 'Пока нет данных за этот период.')); return; }
  const max = Math.max(...rows.map((r) => r.value)) || 1;
  for (const r of rows) {
    const row = el('div', 'hbar');
    row.appendChild(el('span', 'name', r.label));
    const track = el('span', 'track'); const fill = el('i'); fill.style.width = Math.max(1, 100 * r.value / max) + '%'; track.appendChild(fill);
    row.appendChild(track); row.appendChild(el('span', 'val', fmtBytes(r.value)));
    if (r.onClick) { row.classList.add('click'); row.addEventListener('click', r.onClick); }
    box.appendChild(row);
  }
}

// ---------- the «Статистика» tab ----------
let statDays = 7;
async function loadStats() {
  const [h24, h7, tr] = await Promise.all([
    api('/api/history/health?hours=24').catch(() => null), api('/api/history/health?hours=168').catch(() => null),
    api('/api/history/traffic?days=' + statDays).catch(() => null)]);
  // since when
  const from = [h7 && h7.from, tr && tr.from].filter(Boolean);
  $('statSince').textContent = from.length ? 'История на этом компьютере с ' + new Date(Math.min(...from) * 1000).toLocaleString('ru-RU',
    { day: 'numeric', month: 'long', hour: '2-digit', minute: '2-digit' }) + '. Замеры идут, пока приложение запущено.'
    : 'История только начала собираться: первая точка появится через минуту после запуска приложения.';
  // availability table
  const tb = $('statAvail'); tb.textContent = '';
  const nodes = (overview && overview.nodes) || [];
  const head = el('div', 'arow head'); for (const t of ['Маршрут', 'Доступность 24 ч', 'За 7 дней', 'Задержка, медиана', '95% замеров не хуже']) head.appendChild(el('span', null, t)); tb.appendChild(head);
  for (const n of nodes) {
    const a = h24 && h24.nodes[n.name], b = h7 && h7.nodes[n.name];
    const r = el('div', 'arow click'); r.title = 'График задержки';
    const nm = el('span', 'nm'); const sw = el('i'); sw.style.background = serverColor(n.name); nm.appendChild(sw); nm.appendChild(document.createTextNode(hub() + ' → ' + serverText(n.name))); r.appendChild(nm);
    const pct = (x) => (x == null ? '—' : x.toFixed(x >= 99.95 ? 0 : 1) + '%');
    for (const [v, bad] of [[a && a.uptime, a && a.uptime < 99], [b && b.uptime, b && b.uptime < 99]]) r.appendChild(el('span', bad ? 'num bad' : 'num', pct(v)));
    r.appendChild(el('span', 'num', a && a.median != null ? a.median + ' мс' : '—'));
    r.appendChild(el('span', 'num', a && a.p95 != null ? a.p95 + ' мс' : '—'));
    r.addEventListener('click', () => openRoute(n.name));
    tb.appendChild(r);
  }
  // traffic by day, by server
  if (tr) {
    const names = nodes.map((n) => n.name);
    const series = names.filter((nm) => tr.routes[nm]).map((nm) => ({ label: serverText(nm), color: serverColor(nm),
      values: tr.days.map((_d, i) => tr.routes[nm].ru[i] + tr.routes[nm].d[i]) }));
    trafficBars($('statDaily'), tr.days, series);
    // the numbers behind the chart
    const tt = $('statRoutes'); tt.textContent = '';
    const th = el('div', 'arow head'); for (const t of ['Сервер', 'Через RU', 'Напрямую', 'Всего']) th.appendChild(el('span', null, t)); tt.appendChild(th);
    const sum = (a) => a.reduce((x, y) => x + y, 0);
    for (const nm of names.filter((x) => tr.routes[x])) {
      const r = el('div', 'arow'); const nmEl = el('span', 'nm'); const sw = el('i'); sw.style.background = serverColor(nm); nmEl.appendChild(sw);
      nmEl.appendChild(document.createTextNode(serverText(nm))); r.appendChild(nmEl);
      const ru = sum(tr.routes[nm].ru), d = sum(tr.routes[nm].d);
      for (const v of [ru, d, ru + d]) r.appendChild(el('span', 'num', fmtBytes(v)));
      tt.appendChild(r);
    }
    const users = Object.entries(tr.users).map(([u, v]) => ({ label: u, value: sum(v), onClick: () => { showTab('users'); openUserInfo(u); } }))
      .filter((r) => r.value > 0).sort((a, b) => b.value - a.value).slice(0, 12);
    hbars($('statUsers'), users);
  }
}
document.querySelectorAll('[data-statdays]').forEach((b) => b.addEventListener('click', () => {
  statDays = +b.dataset.statdays;
  document.querySelectorAll('[data-statdays]').forEach((x) => x.setAttribute('aria-pressed', String(x === b)));
  loadStats();
}));

// ---------- one route: RU → node ----------
let routeNode = null, routeHours = 24;
async function openRoute(name) {
  routeNode = name;
  const n = ((overview && overview.nodes) || []).find((x) => x.name === name);
  $('rtTitle').textContent = hub() + ' → ' + serverText(name);
  $('rtLead').textContent = n ? n.name + ' · ' + n.host + (n.warp ? ' · выходы: интернет и Cloudflare WARP' : '') : '';
  if (!$('dlgRoute').open) $('dlgRoute').showModal();
  const [h, tr] = await Promise.all([api('/api/history/health?hours=' + routeHours).catch(() => null),
    api('/api/history/traffic?days=' + (routeHours > 24 ? 7 : 1)).catch(() => null)]);
  if (routeNode !== name) return;
  const s = h && h.nodes[name], info = live && live.nodes ? live.nodes[name] : null;
  const tiles = $('rtTiles'); tiles.textContent = '';
  const tile = (k, v, bad) => { const t = el('div', 'tile'); t.appendChild(el('small', null, k)); t.appendChild(el('b', bad ? 'bad' : null, v)); tiles.appendChild(t); };
  tile('Сейчас', info ? (info.status === 'online' ? info.latency + ' мс' : 'нет связи') : '—', info && info.status !== 'online');
  tile('Доступность', s ? s.uptime + '%' : '—', s && s.uptime < 99);
  tile('Задержка, медиана', s && s.median != null ? s.median + ' мс' : '—');
  const r = tr && tr.routes[name], sum = (a) => (a || []).reduce((x, y) => x + y, 0);
  const per = routeHours > 24 ? 'за 7 дней' : 'сегодня';
  tile('Через RU, ' + per, r ? fmtBytes(sum(r.ru)) : '—');
  tile('Напрямую, ' + per, r ? fmtBytes(sum(r.d)) : '—');
  latencyChart($('rtChart'), s ? s.points : null, routeHours > 24);
}
document.querySelectorAll('[data-rthours]').forEach((b) => b.addEventListener('click', () => {
  routeHours = +b.dataset.rthours;
  document.querySelectorAll('[data-rthours]').forEach((x) => x.setAttribute('aria-pressed', String(x === b)));
  if (routeNode) openRoute(routeNode);
}));
$('rtCard').addEventListener('click', () => { const n = routeNode; $('dlgRoute').close(); showTab('servers'); focusCard(n); });

// ---------- tiny latency line inside a map box ----------
function sparkline(points, x, y, w, h) {
  const g = svgEl('g', { class: 'spark' });
  if (!points || points.length < 3) return g;
  const vals = points.map((p) => p[1]).filter((v) => v != null), max = Math.max(...vals, 1), t0 = points[0][0], span = Math.max(1, points[points.length - 1][0] - t0);
  let d = '', pen = false;
  for (const [t, v] of points) {
    const px = x + w * (t - t0) / span;
    if (v == null) { g.appendChild(svgEl('line', { x1: px, x2: px, y1: y, y2: y + h, class: 'sdown' })); pen = false; continue; }
    d += (pen ? ' L ' : ' M ') + px.toFixed(1) + ' ' + (y + h - h * v / max).toFixed(1); pen = true;
  }
  g.appendChild(svgEl('path', { d }));
  return g;
}
