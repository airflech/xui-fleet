// The «Пользователи» list: search, filters, sorting, links.
// Classic scripts in order (see ui.html): top-level names are shared between the files.

// ---------- users ----------
function lastSeen(ts) {
  if (!ts) return 'ещё не подключался';
  const ms = ts > 1e12 ? ts : ts * 1000;
  const min = Math.round((Date.now() - ms) / 60000);
  if (min < 1) return 'был только что';
  if (min < 60) return 'был ' + min + ' мин назад';
  const h = Math.round(min / 60);
  if (h < 48) return 'был ' + h + ' ч назад';
  return 'был ' + new Date(ms).toLocaleDateString('ru-RU');
}
function deviceOf(name) {
  const m = name.toLowerCase().match(/[-_ .](iphone|phone|android|ipad|tablet|pc|laptop|notebook|macbook|mac-?os|mac|windows|win|linux|tv)$/);
  if (!m) return '';
  return { iphone: 'телефон', phone: 'телефон', android: 'телефон', ipad: 'планшет', tablet: 'планшет', pc: 'компьютер', laptop: 'ноутбук',
    notebook: 'ноутбук', macbook: 'Mac', 'mac-os': 'Mac', macos: 'Mac', mac: 'Mac', windows: 'Windows', win: 'Windows', linux: 'Linux', tv: 'ТВ' }[m[1]] || '';
}
function initials(name) {
  const parts = name.replace(/[_.@]/g, '-').split('-').filter((p) => p && !/^(main|phone|pc|ios|android|mac|win|windows|my)$/i.test(p));
  const w = parts.length ? parts : [name];
  return (w[0][0] + (w[1] ? w[1][0] : (w[0][1] || ''))).toUpperCase();
}
// What needs the admin's hand: an expiring or expired subscription, traffic running out, limits exceeded, switched off.
const DAY = 86400000;
function attentionOf(st) {
  if (!st) return [];
  const out = [], now = Date.now();
  if (!st.enabled) out.push('выключен');
  if (st.expiry > 0) {
    const days = Math.ceil((st.expiry - now) / DAY);
    if (days <= 0) out.push('срок истёк'); else if (days <= 7) out.push('истекает через ' + days + ' дн.');
  }
  if (st.quota) { if (st.used >= st.quota) out.push('трафик исчерпан'); else if (st.used >= 0.9 * st.quota) out.push('трафик на исходе'); }
  if (st.over) out.push('больше лимита устройств или IP');
  return out;
}
let userSort = 'activity';
try { userSort = localStorage.getItem('xf-user-sort') || 'activity'; } catch (e) {}
$('userSort').value = userSort;
$('userSort').addEventListener('change', () => { userSort = $('userSort').value; try { localStorage.setItem('xf-user-sort', userSort); } catch (e) {} renderUsers(); });
let linkTarget = null;
$('linkMenu').addEventListener('click', (e) => {
  const b = e.target.closest('[data-link]'); if (!b || !linkTarget) return;
  closeMenus();
  if (b.dataset.link === 'main') copy(linkTarget.url, 'Подписка ' + linkTarget.name);
  else copy(linkTarget.alt, 'Резервная подписка ' + linkTarget.name);
});
function renderUsers() {
  const box = $('userRows');
  box.textContent = '';
  const msg = (b, t) => { const e = el('div', 'empty'); if (b) e.appendChild(el('b', null, b)); if (t) e.appendChild(document.createTextNode(t)); box.appendChild(e); };
  if (!live) {
    for (let i = 0; i < 4; i++) { const r = el('div', 'item'); r.appendChild(el('div', 'avatar')); const s = el('div', 'skeleton'); s.style.width = (120 + i * 30) + 'px'; r.appendChild(s); box.appendChild(r); }
    return;
  }
  if (live.busy) return msg('Идёт операция', 'Список обновится, когда она закончится.');
  if (live.failed) { const e = el('div', 'empty err', 'Не удалось связаться с главным сервером: ' + live.failed); box.appendChild(e); return; }
  if (live.ready === false) return msg('Главный сервер ещё не настроен', 'Нажмите «Применить» на вкладке «Серверы».');
  const all = Object.entries(live.users || {}).sort((a, b) => a[0].localeCompare(b[0], 'ru'));
  const online = new Set((live.online || []).filter((n) => n in (live.users || {})));
  const seen = live.lastOnline || {};
  $('cntUsers').textContent = all.length;
  $('onlineTag').classList.toggle('hidden', !all.length);
  $('onlineText').textContent = 'онлайн ' + online.size + ' из ' + all.length;
  if (!all.length) return msg('Пользователей пока нет', 'Добавьте первого — он получит ссылку со всеми подключениями.');
  const q = $('userSearch').value.trim().toLowerCase();
  const st0 = (n) => (userStats && userStats[n]) || null;
  const nAttention = userStats ? all.filter(([n]) => attentionOf(st0(n)).length).length : 0;
  $('cntAttention').textContent = nAttention ? String(nAttention) : '';
  const byName = (a, b) => a[0].localeCompare(b[0], 'ru');
  const sorters = {
    activity: (a, b) => (online.has(b[0]) - online.has(a[0])) || ((seen[b[0]] || 0) - (seen[a[0]] || 0)) || byName(a, b),
    name: byName,
    traffic: (a, b) => (((st0(b[0]) || {}).used || 0) - ((st0(a[0]) || {}).used || 0)) || byName(a, b),
    // the nearest end first; without a date — at the bottom
    expiry: (a, b) => { const ea = (st0(a[0]) || {}).expiry || 0, eb = (st0(b[0]) || {}).expiry || 0;
      return ((ea > 0 ? ea : Infinity) - (eb > 0 ? eb : Infinity)) || byName(a, b); },
  };
  const users = all.filter(([n]) => (!q || n.toLowerCase().includes(q))
      && (userFilter === 'all' || (userFilter === 'online' ? online.has(n) : attentionOf(st0(n)).length > 0)))
    .sort(sorters[userSort] || sorters.activity);
  if (!users.length) return msg('Никого не найдено', q ? 'Измените запрос.'
    : userFilter === 'attention' ? (userStats ? 'Ни у кого не истекает срок, не кончается трафик и не превышены лимиты.' : 'Загружаю статистику…')
    : 'Сейчас никто не подключён.');
  for (const [name, url] of users) {
    const on = online.has(name);
    const r = el('div', 'item click'); r.title = 'Трафик, устройства и лимиты';
    r.addEventListener('click', (e) => { if (!e.target.closest('button')) openUserInfo(name); });
    const av = el('div', 'avatar', initials(name)); av.appendChild(el('span', 'dot ' + (on ? 'ok' : ''))); av.title = on ? 'сейчас подключён' : 'не подключён';
    let hue = 0; for (const ch of name) hue = (hue * 31 + ch.codePointAt(0)) % 360; av.style.setProperty('--h', hue);
    r.appendChild(av);
    const g = el('div', 'grow');
    const t = el('div', 'title', name);
    const dev = deviceOf(name); if (dev) t.appendChild(el('span', 'device', dev));
    g.appendChild(t);
    const st = userStats && userStats[name];
    g.appendChild(el('div', 'meta', on ? 'онлайн сейчас' + (st && st.servers.length ? ' · ' + st.servers.map(serverLabel).join(', ') : '') : lastSeen(seen[name])));
    if (st) g.appendChild(statLine(st));
    r.appendChild(g);
    const b = el('button', 'btn sm'); b.appendChild(icon('copy')); b.appendChild(document.createTextNode('Ссылка'));
    b.title = 'Скопировать ссылку-подписку';
    b.addEventListener('click', () => copy(url, 'Подписка ' + name));
    const reserve = ((overview && overview.master && overview.master.reserveDomains) || [])[0];
    if (reserve) {
      // the reserve link is one click away, not a button in every row
      const alt = url.replace(/^(https?:\/\/)[^/:]+/, '$1' + reserve);
      const split = el('div', 'splitbtn'); split.appendChild(b);
      const mb = el('button', 'btn sm'); mb.appendChild(icon('chevron')); mb.title = 'Основная или резервная ссылка';
      mb.setAttribute('aria-haspopup', 'menu'); mb.setAttribute('aria-label', 'Другие ссылки');
      mb.addEventListener('click', (e) => toggleMenu($('linkMenu'), e.currentTarget, () => {
        linkTarget = { name, url, alt };
        $('linkMain').textContent = new URL(url).hostname; $('linkReserve').textContent = reserve;
      }));
      split.appendChild(mb); r.appendChild(split);
    } else r.appendChild(b);
    box.appendChild(r);
  }
}
$('userSearch').addEventListener('input', renderUsers);
document.querySelectorAll('[data-filter]').forEach((b) => b.addEventListener('click', () => {
  userFilter = b.dataset.filter;
  document.querySelectorAll('[data-filter]').forEach((x) => x.setAttribute('aria-pressed', String(x === b)));
  renderUsers();
}));
