// The «Входящие» tab.
// Classic scripts in order (see ui.html): top-level names are shared between the files.

// ---------- inbounds ----------
let inbounds = null;
const KIND = { direct: 'прямое TCP', xhttp: 'XHTTP', hy2: 'Hysteria2', warp: 'WARP', cascade: 'каскад через главный (TCP)',
  rhy2: 'каскад через главный (Hysteria2)', rxh: 'каскад через главный (XHTTP)', rwarp: 'каскад → WARP', warpup: 'служебный вход каскада → WARP' };
async function loadInbounds() {
  const box = $('inboundRows'); box.textContent = '';
  const wait = el('div', 'panel'); const e = el('div', 'empty'); e.appendChild(el('span', 'spin')); e.appendChild(document.createTextNode('  Загружаю с главного сервера…')); wait.appendChild(e); box.appendChild(wait);
  try {
    const d = await api('/api/inbounds');
    if (d.busy) { box.textContent = ''; const p = el('div', 'panel'); p.appendChild(el('div', 'empty', 'Идёт операция — список обновится, когда она закончится.')); box.appendChild(p); return; }
    inbounds = d.inbounds; renderInbounds();
  } catch (ex) { box.textContent = ''; const p = el('div', 'panel'); p.appendChild(el('div', 'empty err', ex.message)); box.appendChild(p); }
}
$('btnInbounds').addEventListener('click', loadInbounds);
function renderInbounds() {
  const box = $('inboundRows'); box.textContent = '';
  const groups = {};
  for (const i of inbounds) (groups[i.server] = groups[i.server] || []).push(i);
  if (!inbounds.length) { const p = el('div', 'panel'); p.appendChild(el('div', 'empty', 'Входящих пока нет.')); box.appendChild(p); return; }
  for (const [server, rows] of Object.entries(groups)) {
    const panel = el('div', 'panel'); panel.style.marginBottom = '16px';
    const h = el('div', 'panel-h'); h.appendChild(el('b', null, serverLabel(server))); h.appendChild(el('span', 'muted', ' · ' + plural(rows.length, 'входящее', 'входящих', 'входящих')));
    panel.appendChild(h);
    const list = el('div', 'list');
    for (const i of rows) {
      const r = el('div', 'item');
      const g = el('div', 'grow');
      const t = el('div', 'title', i.remark || i.tag || ('#' + i.id));
      if (i.managed) t.appendChild(el('span', 'device', 'xui-fleet'));
      if (!i.enabled) t.appendChild(el('span', 'tag bad', 'выключено'));
      else if (i.hidden) t.appendChild(el('span', 'tag', 'скрыто из подписки'));
      g.appendChild(t);
      const proto = [i.managed ? KIND[i.kind] : null, i.protocol, i.network, i.security].filter(Boolean).join(' · ');
      g.appendChild(el('div', 'meta', proto + ' · порт ' + i.port + (i.protocol === 'hysteria' ? '/udp' : '/tcp')));
      const st = el('div', 'ustats');
      const add = (ic, text, title) => { const sp = el('span'); sp.appendChild(icon(ic)); sp.appendChild(document.createTextNode(text)); sp.title = title; st.appendChild(sp); };
      add('chart', fmtBytes(i.up + i.down), 'Трафик через это входящее: отдано ' + fmtBytes(i.up) + ', получено ' + fmtBytes(i.down));
      add('user', String(i.clients), 'Клиентов в этом входящем');
      g.appendChild(st);
      r.appendChild(g);
      if (!i.service) {
        const eb = el('button', 'btn sm'); eb.appendChild(icon('edit')); eb.appendChild(document.createTextNode('Изменить'));
        eb.addEventListener('click', () => openInbound(i)); r.appendChild(eb);
        const db = el('button', 'btn ghost sm danger icon'); db.appendChild(icon('trash'));
        const dt = !i.managed ? 'Удалить' : i.kind === 'direct' ? 'Скрыть из подписок' : 'Выключить у сервера (не будет создано снова)';
        db.title = dt; db.setAttribute('aria-label', dt);
        db.addEventListener('click', () => deleteInbound(i)); r.appendChild(db);
      }
      list.appendChild(r);
    }
    panel.appendChild(list); box.appendChild(panel);
  }
}
let ibCur = null;
function openInbound(i) {
  ibCur = i; const f = $('inboundForm'); resetForm(f);
  $('ibTitle').textContent = i.remark || i.tag;
  $('ibLead').textContent = serverLabel(i.server) + ' · ' + (i.managed ? 'подключение xui-fleet (' + KIND[i.kind] + ')' : 'ваше входящее');
  f.remark.value = i.remark; f.remark.disabled = i.managed;
  $('ibRemarkHint').classList.toggle('hidden', !i.managed);
  f.port.value = i.port; f.enabled.checked = i.enabled;
  $('ibPortHint').textContent = i.managed
    ? 'После смены порта программа сама перестроит каскад и откроет порт в файрволе. Пользователям нужно обновить подписку.'
    : 'Пользователям этого входящего нужно будет обновить подписку или ссылку.';
  $('dlgInbound').showModal();
}
$('inboundForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const f = e.target;
  const body = { id: ibCur.id, label: ibCur.remark || ibCur.tag, port: f.port.value, enabled: f.enabled.checked };
  if (!ibCur.managed) body.remark = f.remark.value;
  if (await start('/api/inbound/edit', body, f)) inbounds = null;
});
function deleteInbound(i) {
  const name = i.remark || i.tag;
  const text = !i.managed
    ? 'Входящее будет удалено из панели 3x-ui вместе с привязкой клиентов к нему. Перед удалением делается бэкап главного сервера.'
    : i.kind === 'direct'
      ? 'Это прямое подключение сервера: через него идут каскады, поэтому оно будет скрыто из подписок, но останется работать. Чтобы вернуть — «Название и подключения» сервера.'
      : 'Этот тип подключения (' + KIND[i.kind] + ') будет выключен у сервера ' + serverLabel(i.server) + ' в настройках, программа удалит его и не создаст снова. Перед удалением делается бэкап главного сервера.';
  confirmAction('Удалить «' + name + '»?', text, () => start('/api/inbound/delete', { id: i.id, label: name }), i.kind === 'direct' ? 'Скрыть' : 'Удалить', true);
}
