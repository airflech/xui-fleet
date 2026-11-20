// Shared by every part of the page: the token, API calls, small DOM helpers, toasts, tabs, menus, form pieces.
// Classic scripts in order (see ui.html): top-level names are shared between the files.

const TOKEN = new URLSearchParams(location.search).get('t') || '';
const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
const icon = (name, cls) => { const s = document.createElementNS('http://www.w3.org/2000/svg', 'svg'); s.setAttribute('class', 'i' + (cls ? ' ' + cls : ''));
  const u = document.createElementNS('http://www.w3.org/2000/svg', 'use'); u.setAttribute('href', '#i-' + name); s.appendChild(u); return s; };
const store = { get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }, set(k, v) { try { localStorage.setItem(k, v); } catch (e) {} } };

async function api(path, body) {
  const opt = { headers: { 'X-Fleet-Token': TOKEN } };
  if (body !== undefined) { opt.method = 'POST'; opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
  const res = await fetch(path, opt);
  const data = await res.json();
  if (data && data.error) throw new Error(data.error);
  return data;
}

let userStats = null, overview = null, live = null, jobSeen = 0, jobTimer = null, curJob = 0, jobClosed = 0, userFilter = 'all';

// ---------- toast ----------
let toastTimer;
function toast(text) {
  let t = $('toast');
  if (!t) { t = el('div', 'toast'); t.id = 'toast'; t.setAttribute('role', 'status'); document.body.appendChild(t); }
  t.textContent = text; t.classList.remove('hidden');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.add('hidden'), 2600);
}
async function copy(text, what) {
  try { await navigator.clipboard.writeText(text); toast('Скопировано: ' + (what || 'текст').toLowerCase()); }
  catch (e) { toast('Не удалось скопировать'); }
}

// ---------- tabs ----------
function showTab(name) {
  document.querySelectorAll('.tab').forEach((t) => t.setAttribute('aria-selected', String(t.dataset.tab === name)));
  document.querySelectorAll('[data-pane]').forEach((p) => p.classList.toggle('hidden', p.dataset.pane !== name));
  store.set('xf-tab', name);
  if (name === 'backups') { loadBackups(); loadSync(); }
  if (name === 'inbounds') loadInbounds();
  if (name === 'stats') loadStats();
  if (name === 'help') api('/api/sync').then((d) => { $('appVersion').textContent = 'Версия на этом компьютере: ' + d.app + (d.serverApp ? ' · на сервере синхронизации: ' + d.serverApp : ''); }).catch(() => {});
}
document.querySelectorAll('.tab').forEach((t) => t.addEventListener('click', () => showTab(t.dataset.tab)));

// ---------- menus ----------
let openMenu = null;
function toggleMenu(menu, anchor, onOpen) {
  const wasOpen = openMenu === menu;
  closeMenus();
  if (wasOpen) return;
  if (onOpen) onOpen();
  const r = anchor.getBoundingClientRect();
  menu.classList.remove('hidden');
  menu.style.position = 'fixed';
  const w = menu.offsetWidth;
  menu.style.top = (r.bottom + 6) + 'px';
  menu.style.left = Math.max(8, Math.min(r.right - w, innerWidth - w - 8)) + 'px';
  menu.style.right = 'auto';
  openMenu = menu;
  const first = menu.querySelector('button:not(:disabled)'); if (first) first.focus();
}
function closeMenus() { document.querySelectorAll('.menu').forEach((m) => m.classList.add('hidden')); openMenu = null; }
document.addEventListener('click', (e) => { if (openMenu && !openMenu.contains(e.target) && !e.target.closest('[aria-haspopup]')) closeMenus(); });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeMenus(); });
window.addEventListener('resize', closeMenus); window.addEventListener('scroll', closeMenus, true);
$('btnFleetMenu').addEventListener('click', (e) => toggleMenu($('fleetMenu'), e.currentTarget));

let menuNode = null;
$('nodeMenu').addEventListener('click', (e) => {
  const b = e.target.closest('[data-act]'); if (!b || !menuNode) return;
  const n = menuNode; closeMenus();
  if (b.dataset.act === 'label') openLabel(n);
  else if (b.dataset.act === 'access') openAccess(n, 'Доступ к серверу ' + (n.display || n.name));
  else if (b.dataset.act === 'apply') {
    const label = splitFlag(n.display || n.name)[1] || n.name;
    confirmAction('Перенастроить ' + label + '?', 'Приложение зайдёт на сервер по SSH, проверит 3x-ui, подключения и WARP и вернёт то, что разошлось с настройками; '
      + 'затем обновит каскад с главного сервера. Остальные серверы не трогаются. Подключения к этому серверу могут прерваться на несколько секунд.',
      () => start('/api/apply', { name: n.name }), 'Перенастроить');
  } else if (b.dataset.act === 'rotate') askRotate(n.name, splitFlag(n.display || n.name)[1] || n.name, true);
  else openRemove(n);
});

// ---------- SSH block (shared by forms) ----------
document.querySelectorAll('[data-ssh]').forEach((box) => {
  box.appendChild($('sshTpl').content.cloneNode(true));
  const hidden = box.querySelector('[name=sshAuth]');
  const set = (v) => {
    hidden.value = v;
    box.querySelectorAll('[data-auth] button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.v === v)));
    box.querySelector('[data-key]').classList.toggle('hidden', v !== 'key');
    box.querySelector('[data-password]').classList.toggle('hidden', v !== 'password');
  };
  box.querySelectorAll('[data-auth] button').forEach((b) => b.addEventListener('click', () => set(b.dataset.v)));
  box.setAuth = set;
});

function formData(form) {
  const out = {};
  for (const input of form.querySelectorAll('input, select')) {
    if (!input.name) continue;
    if (input.type === 'radio' && !input.checked) continue;
    out[input.name] = input.type === 'checkbox' ? input.checked : input.value.trim();
  }
  if (out.sshAuth === 'key') out.sshPassword = ''; else out.sshKey = '';
  delete out.sshAuth;
  return out;
}
function resetForm(form) { form.reset(); const e = form.querySelector('.formerr'); if (e) e.textContent = '';
  form.querySelectorAll('[data-ssh]').forEach((b) => b.setAuth && b.setAuth('password')); }

function openAccess(server, title) {
  const form = $('accessForm'), a = server.access || {};
  resetForm(form);
  form.querySelector('[name=name]').value = server.name;
  form.querySelector('[name=host]').value = server.host;
  form.querySelector('[name=sshUser]').value = a.sshUser || 'root';
  form.querySelector('[name=sshPort]').value = a.sshPort || 22;
  if (a.sshKey) form.querySelector('[name=sshKey]').value = a.sshKey;
  const mode = a.auth === 'key' && a.sshKey && server.prepared ? 'key' : 'password';  // a key that never worked is not a default
  form.querySelector('[data-ssh]').setAuth(mode);
  $('accessKeep').classList.toggle('hidden', !(a.hasPassword && mode === 'password'));
  $('accessTitle').textContent = title;
  $('dlgAccess').showModal();
}

// ---------- flags / labels ----------
function fillFlags(sel, detected) {
  const keep = sel.value;
  sel.textContent = '';
  const auto = el('option', null, 'Автоматически' + (detected ? ' — ' + flagOf(detected) + ' ' + detected : ' — по IP сервера'));
  auto.value = 'auto'; sel.appendChild(auto);
  const none = el('option', null, 'Без флага'); none.value = 'none'; sel.appendChild(none);
  for (const [iso, fl, nm] of (overview && overview.countries) || []) { const o = el('option', null, fl + ' ' + nm); o.value = iso; sel.appendChild(o); }
  if (keep) sel.value = keep;
}
function flagOf(iso) { return [...(iso || '').toUpperCase()].map((c) => String.fromCodePoint(0x1F1E6 + c.charCodeAt(0) - 65)).join(''); }
function splitFlag(text) { const m = (text || '').match(/^([\u{1F1E6}-\u{1F1FF}]{2})\s*(.*)$/u); return m ? [m[1], m[2]] : ['', text || '']; }

function openLabel(n) {
  const form = $('labelForm');
  resetForm(form);
  form.querySelector('[name=name]').value = n.name;
  form.querySelector('[name=label]').value = splitFlag(n.label)[1];
  const sel = form.querySelector('[name=flag]'); fillFlags(sel, n.country); sel.value = n.flag || 'auto';
  for (const k of ['cascade', 'cascade_xhttp', 'cascade_hy2', 'cascade_warp', 'direct', 'xhttp', 'hy2', 'warp']) {
    const box = form.querySelector('[name=show_' + k + ']');
    box.checked = !(n.hide || []).includes(k);
    const note = box.parentElement.querySelector('.state'); if (note) note.remove();
  }
  // A ticked box does not mean users have it: the entry may be switched off in «Входящие» or not exist at all
  // (that kind is off for the server). Say so next to it, from the panel itself.
  const KINDS = { cascade: 'cascade', cascade_xhttp: 'rxh', cascade_hy2: 'rhy2', cascade_warp: 'rwarp', direct: 'direct', xhttp: 'xhttp', hy2: 'hy2', warp: 'warp' };
  api('/api/inbounds').then((d) => {
    if (!d || !d.inbounds || !$('dlgLabel').open) return;
    for (const [k, kind] of Object.entries(KINDS)) {
      const ib = d.inbounds.find((i) => i.managed && i.owner === n.name && i.kind === kind);
      const text = !ib ? 'нет на сервере' : !ib.enabled ? 'выключено во «Входящих»' : '';
      if (!text) continue;
      const lab = form.querySelector('[name=show_' + k + ']').parentElement;
      const sp = el('small', 'state muted', ' — ' + text); sp.title = !ib ? 'Этот тип подключения у сервера не создан'
        : 'Подключение есть, но выключено: пользователи его не получат, даже с галочкой. Включается во вкладке «Входящие»';
      lab.appendChild(sp);
    }
  }).catch(() => {});
  $('labelTitle').textContent = 'Название и подключения';
  $('labelPreview').textContent = 'Сейчас у пользователей: «' + (n.display || n.name) + '» · ' + n.name;
  $('dlgLabel').showModal();
}

// ---------- theme ----------
// As the system (default), always light or always dark; remembered on this computer.
const THEMES = { auto: ['i-auto', 'Тема: как в системе'], light: ['i-sun', 'Тема: дневная'], dark: ['i-moon', 'Тема: ночная'] };
function currentTheme() { try { return localStorage.getItem('xf-theme') || 'auto'; } catch (e) { return 'auto'; } }
function paintTheme() {
  const th = currentTheme(), [ic, label] = THEMES[th] || THEMES.auto;
  if (th === 'auto') delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = th;
  $('themeBtn').querySelector('use').setAttribute('href', '#' + ic);
  $('themeBtn').title = label + ' — нажмите, чтобы сменить'; $('themeBtn').setAttribute('aria-label', label);
}
$('themeBtn').addEventListener('click', () => {
  const order = ['auto', 'light', 'dark'], next = order[(order.indexOf(currentTheme()) + 1) % order.length];
  try { localStorage.setItem('xf-theme', next); } catch (e) {}
  paintTheme(); toast(THEMES[next][1]);
});
paintTheme();

// ---------- changing the masking ----------
function askRotate(name, label, node) {
  closeMenus();
  confirmAction('Сменить маскировку ' + label + '?',
    'Если подключения к этому серверу начали блокировать — дать им новую «внешность»:\n\n'
    + '• новые ключи Reality и идентификаторы у подключений VLESS;\n'
    + '• новый пароль обфускации у Hysteria2;\n'
    + (node ? '• другой сайт для маскировки из подсети сервера — если найдётся (поиск до минуты).\n' : '')
    + '\nПосле этого пользователям нужно обновить подписку (в Happ — потянуть список вниз): старые ссылки этого сервера '
    + 'перестанут работать. Подключения других серверов не меняются. Перед сменой делается бэкап.',
    () => start('/api/node/rotate', { name }), 'Сменить маскировку');
}
