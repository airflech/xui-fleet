// Ctrl+K / ⌘K: one line to find a user, a server, a tab or an action and do it — without walking the tabs.
// Loaded after app.js: it only drives what the other files already define.

function norm(s) { return String(s || '').toLowerCase().replace(/ё/g, 'е'); }
// Names are often in Latin (Main-Vasya-phone) and typed in Cyrillic («вася»), or the other way round:
// both the items and the query are also compared in Latin transliteration.
const TRANSLIT = { а: 'a', б: 'b', в: 'v', г: 'g', д: 'd', е: 'e', ж: 'zh', з: 'z', и: 'i', й: 'y', к: 'k', л: 'l', м: 'm', н: 'n',
  о: 'o', п: 'p', р: 'r', с: 's', т: 't', у: 'u', ф: 'f', х: 'h', ц: 'ts', ч: 'ch', ш: 'sh', щ: 'sch', ъ: '', ы: 'y', ь: '', э: 'e', ю: 'yu', я: 'ya' };
function lat(s) { return norm(s).replace(/[а-я]/g, (c) => TRANSLIT[c] ?? c); }
function paletteItems(q) {
  const items = [];
  const add = (group, title, sub, run, keys) => {
    const k = norm([title, sub, keys].join(' '));
    items.push({ group, title, sub: sub || '', run, keys: k + ' ' + lat(k) });
  };
  const click = (id) => () => { const b = $(id); if (b && !b.disabled) b.click(); };
  // users: open, copy the link, reset the traffic
  const users = Object.entries((live && live.users) || {});
  const online = new Set((live && live.online) || []);
  for (const [name, url] of users) {
    const st = userStats && userStats[name];
    const sub = online.has(name) ? 'онлайн' : (st ? fmtBytes(st.used) : '');
    add('Пользователи', name, sub, () => { showTab('users'); openUserInfo(name); }, 'пользователь открыть карточка');
    if (q) {
      add('Пользователи', 'Скопировать ссылку: ' + name, 'подписка', () => copy(url, 'Подписка ' + name), 'ссылка подписка копировать');
      add('Пользователи', 'Сбросить трафик: ' + name, st ? 'сейчас ' + fmtBytes(st.used) : '', () => confirmAction('Сбросить трафик ' + name + '?',
        'Счётчик начнётся с нуля. Если пользователь был выключен из-за лимита трафика, он снова заработает.',
        () => userAction('/api/user/reset', { name }, null, 'Трафик сброшен')), 'сбросить трафик обнулить');
    }
  }
  // servers: show the card (and its place on the map)
  if (overview && overview.configured) {
    const m = overview.master;
    add('Серверы', 'Главный сервер', m.domain || m.host, () => { showTab('servers'); focusCard('__master'); }, 'ru мастер главный ' + m.host);
    for (const n of overview.nodes || []) {
      const [, text] = splitFlag(n.display || n.name);
      const info = live && live.nodes ? live.nodes[n.name] : null;
      add('Серверы', text || n.name, n.name + (info ? ' · ' + (info.status === 'online' ? info.latency + ' мс' : 'нет связи') : ''),
        () => { showTab('servers'); focusCard(n.name); }, 'сервер нода ' + n.host);
      add('Серверы', 'График маршрута: ' + (text || n.name), 'доступность и задержка', () => openRoute(n.name), 'график задержка доступность маршрут');
      if (q) add('Серверы', 'Сменить маскировку: ' + (text || n.name), 'если начали блокировать', () => askRotate(n.name, text || n.name, true), 'маскировка ключи блокировка заблокировали');
    }
  }
  // tabs
  for (const [tab, title] of [['servers', 'Серверы'], ['users', 'Пользователи'], ['inbounds', 'Входящие'], ['panels', 'Панели 3x-ui'],
    ['backups', 'Бэкапы'], ['stats', 'Статистика'], ['help', 'Помощь']]) add('Вкладки', title, 'перейти', () => showTab(tab), 'вкладка открыть');
  // actions
  for (const [title, sub, run, keys] of [
    ['Добавить пользователя', '', () => { showTab('users'); click('btnAddUser')(); }, 'новый создать'],
    ['Добавить сервер', '', () => { showTab('servers'); click('btnAddNode')(); }, 'новый нода'],
    ['Сверить серверы с настройками', drift ? (drift.changes.length ? 'есть расхождения: ' + drift.changes.length : 'всё настроено') : '', () => loadDrift(), 'проверить расхождения исправить применить'],
    ['Перенастроить все серверы', 'по SSH, с проверкой каскадов', click('btnForce'), 'применить починить'],
    ['Проверить подключения', 'трафик через каждую ссылку', click('btnCheck'), 'тест'],
    ['Домены и адреса', '', () => { showTab('servers'); click('btnDomains')(); }, 'домен резерв заблокировали'],
    ['Роль и подключения главного сервера', '', () => { showTab('servers'); click('btnRole')(); }, 'мастер vpn каскад'],
    ['Сделать бэкап', 'главного сервера', () => { showTab('backups'); click('btnBackup')(); }, 'резервная копия'],
    ['История операций', 'журнал', () => { $('drawer').classList.remove('hidden'); openHistory(); }, 'журнал лог'],
    ['Версии приложения', 'обновления', () => openVersions(), 'обновить версия'],
    ['Сменить тему', 'как в системе / дневная / ночная', click('themeBtn'), 'тема ночь день темная светлая'],
    ['Что такое WARP?', '', () => $('dlgWarp').showModal(), 'cloudflare'],
  ]) add('Действия', title, sub, run, keys);
  if (!q) return items.filter((i) => i.group !== 'Пользователи' || online.has(i.title)).slice(0, 40);
  const words = norm(q).split(/\s+/).filter(Boolean);
  return items
    .map((i) => {
      if (!words.every((w) => i.keys.includes(w) || i.keys.includes(lat(w)))) return null;
      const t = norm(i.title), tl = lat(t), w0 = words[0], l0 = lat(w0);
      const score = (t.startsWith(w0) || tl.startsWith(l0) ? 0 : t.includes(w0) || tl.includes(l0) ? 1 : 2) + (i.group === 'Действия' ? 0.5 : 0);
      return [score, i];
    })
    .filter(Boolean).sort((a, b) => a[0] - b[0]).map((x) => x[1]).slice(0, 40);
}
let palSel = 0, palList = [];
function renderPalette() {
  const q = $('palInput').value.trim(), box = $('palList'); box.textContent = '';
  palList = paletteItems(q); palSel = Math.min(palSel, Math.max(0, palList.length - 1));
  if (!palList.length) { box.appendChild(el('div', 'empty', 'Ничего не найдено')); return; }
  let group = '';
  palList.forEach((it, i) => {
    if (it.group !== group) { group = it.group; box.appendChild(el('div', 'pal-g', group)); }
    const b = el('button', 'pal-i' + (i === palSel ? ' sel' : '')); b.type = 'button'; b.setAttribute('role', 'option');
    b.appendChild(el('span', 'pal-t', it.title)); if (it.sub) b.appendChild(el('span', 'pal-s', it.sub));
    b.addEventListener('mousemove', () => { if (palSel !== i) { palSel = i; paintSel(); } });
    b.addEventListener('click', () => runPalette(i));
    box.appendChild(b);
  });
}
function paintSel() {
  $('palList').querySelectorAll('.pal-i').forEach((b, i) => b.classList.toggle('sel', i === palSel));
  const cur = $('palList').querySelectorAll('.pal-i')[palSel]; if (cur) cur.scrollIntoView({ block: 'nearest' });
}
function runPalette(i) { const it = palList[i]; if (!it) return; $('dlgCmd').close(); it.run(); }
function openPalette() {
  if (document.querySelector('dialog[open]')) return;
  closeMenus(); $('palInput').value = ''; palSel = 0; renderPalette(); $('dlgCmd').showModal(); $('palInput').focus();
}
$('palInput').addEventListener('input', () => { palSel = 0; renderPalette(); });
$('palInput').addEventListener('keydown', (e) => {
  if (e.key === 'ArrowDown') { e.preventDefault(); palSel = Math.min(palList.length - 1, palSel + 1); paintSel(); }
  else if (e.key === 'ArrowUp') { e.preventDefault(); palSel = Math.max(0, palSel - 1); paintSel(); }
  else if (e.key === 'Enter') { e.preventDefault(); runPalette(palSel); }
});
document.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === 'k' || (e.ctrlKey || e.metaKey) && e.code === 'KeyK') {
    e.preventDefault(); if ($('dlgCmd').open) $('dlgCmd').close(); else openPalette();
  }
});
$('cmdBtn').addEventListener('click', openPalette);
$('cmdKey').textContent = /Mac|iPhone|iPad/.test(navigator.platform) ? '⌘K' : 'Ctrl K';
