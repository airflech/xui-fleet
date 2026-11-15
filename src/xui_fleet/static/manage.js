// Removing servers and the profiles menu.
// Classic scripts in order (see ui.html): top-level names are shared between the files.

// ---------- removing servers ----------
let removeTarget = null;
function openRemove(node) {
  removeTarget = node;  // null: the master
  const f = $('removeForm'); resetForm(f);
  const master = !node, m = overview.master, title = master ? (m.display || 'Главный сервер') : (node.display || node.name);
  $('rmTitle').textContent = master ? 'Удалить главный сервер «' + title + '»?' : 'Удалить сервер ' + title + '?';
  $('rmLead').textContent = master
    ? 'Это вся сеть профиля «' + curProfile().name + '»: подписки пользователей перестанут обновляться, профиль откроется с окна первого запуска.'
    : 'Подключения через этот сервер исчезнут из подписок, каскад к нему будет убран с главного сервера.';
  $('rmSoft').textContent = master
    ? 'Программа забудет эту сеть. Серверы и 3x-ui продолжат работать, пользователи — подключаться; управлять ими можно в самой панели.'
    : 'Сервер убирается с главного сервера и из программы. 3x-ui на нём остаётся и работает сам по себе.';
  $('rmHard').textContent = (master ? 'С главного сервера' : 'С сервера') + ' удаляются 3x-ui с базой, сайт-витрина, сертификаты, правила файрвола для его портов. '
    + 'SSH-доступ, nginx и чужие правила не трогаются.';
  $('rmMasterOpts').classList.toggle('hidden', !master);
  const nodes = (overview.nodes || []).length;
  $('rmNodes').closest('label').classList.toggle('hidden', !nodes);
  $('rmNodesText').textContent = 'И с зарубежных серверов (' + nodes + ')';
  $('rmSyncBox').classList.add('hidden');
  if (master) api('/api/sync').then((d) => $('rmSyncBox').classList.toggle('hidden', !(d.enabled))).catch(() => {});
  $('rmAsk').textContent = 'Для подтверждения введите имя главного сервера: ' + m.name;
  $('rm_confirm').placeholder = m.name;
  $('rmNote').textContent = 'Перед удалением делается бэкап главного сервера' + (master ? ' — он остаётся на этом компьютере, из него сеть можно восстановить на новом сервере.' : '.');
  syncRemove();
  $('dlgRemove').showModal();
}
function syncRemove() {
  const f = $('removeForm'), hard = f.purge.value === '1', master = !removeTarget;
  $('rmNodes').disabled = !hard; if (!hard) $('rmNodes').checked = false;
  f.syncData.disabled = !hard; if (!hard) f.syncData.checked = false;
  $('rmGo').textContent = hard ? 'Удалить вместе с 3x-ui' : 'Удалить';
  $('rmGo').disabled = master && $('rm_confirm').value.trim().toLowerCase() !== overview.master.name.toLowerCase();
}
$('removeForm').addEventListener('change', syncRemove);
$('rm_confirm').addEventListener('input', syncRemove);
$('btnMasterRemove').addEventListener('click', () => { closeMenus(); openRemove(null); });
$('removeForm').addEventListener('submit', (e) => {
  e.preventDefault();
  const f = e.target, purge = f.purge.value === '1';
  if (removeTarget) return start('/api/node/remove', { name: removeTarget.name, purge }, f);
  const body = { purge, purgeNodes: f.purgeNodes.checked, syncData: f.syncData.checked, confirm: $('rm_confirm').value };
  checkMasterThenRemove(body, f);
});
async function checkMasterThenRemove(body, f) {
  // The backup is what brings the network back: when the panel does not answer there is none, so ask first.
  const go = $('rmGo'), label = go.textContent, err = f.querySelector('.formerr');
  err.textContent = ''; go.disabled = true; go.textContent = 'Проверяю главную панель…';
  let probe;
  try { probe = await api('/api/master/probe'); } catch (e) { probe = { alive: false, error: e.message }; }
  go.textContent = label; syncRemove();
  if (probe.busy) { err.textContent = 'Идёт другая операция — дождитесь её окончания.'; return; }
  if (probe.alive) return start('/api/master/remove', body, f);
  const last = probe.lastBackup ? 'Последний бэкап на этом компьютере — от ' + fmtWhen(probe.lastBackup * 1000) + ': из него можно восстановить сеть в том виде, какой она была тогда.'
    : 'Сохранённых бэкапов этой сети на компьютере нет: пользователей и подключения потом не восстановить.';
  confirmAction('Удалить без бэкапа?',
    'Главная панель не отвечает (' + (probe.error || 'нет ответа') + '), поэтому бэкап перед удалением сделать нельзя. ' + last,
    () => start('/api/master/remove', Object.assign({ withoutBackup: true }, body), f), 'Удалить без бэкапа', true);
}

// ---------- profiles ----------
function curProfile() { return (overview && overview.profile) || { id: 'default', name: 'Основной' }; }
function renderProfiles() {
  if (!overview || !overview.profiles) return;
  const cur = curProfile();
  $('profileName').textContent = cur.name;
  $('profileBtn').classList.remove('hidden');
  const box = $('profileList'); box.textContent = '';
  for (const p of overview.profiles) {
    const b = el('button'); b.setAttribute('role', 'menuitemradio'); b.setAttribute('aria-checked', String(p.active));
    b.appendChild(icon('check', 'tick'));
    const t = el('span', null, p.name); t.appendChild(el('small', null, p.configured ? (p.master || 'настроен') : 'не настроен — окно первого запуска'));
    b.appendChild(t);
    b.addEventListener('click', () => { closeMenus(); if (!p.active) profileAction('switch', { id: p.id }); });
    box.appendChild(b);
  }
  $('btnProfileLogout').querySelector('span').firstChild.textContent = 'Выйти из профиля «' + cur.name + '»';
}
async function profileAction(action, body, form) {
  const err = form ? form.querySelector('.formerr') : null;
  if (err) err.textContent = '';
  try { await api('/api/profile/' + action, body); }
  catch (e) { if (err) err.textContent = e.message; else toast(e.message); return false; }
  location.reload();  // a different profile: start the page over with its data
  return true;
}
$('profileBtn').addEventListener('click', (e) => toggleMenu($('profileMenu'), e.currentTarget));
function openProfileDlg(rename) {
  const f = $('profileForm'); resetForm(f);
  $('profileDlgTitle').textContent = rename ? 'Переименовать профиль' : 'Новый профиль';
  $('profileDlgText').textContent = rename ? 'Название видно только на этом компьютере.'
    : 'Отдельный набор серверов со своими доступами, бэкапами и синхронизацией. Текущий профиль останется как есть — между ними можно переключаться в шапке.';
  $('profileDlgGo').textContent = rename ? 'Сохранить' : 'Создать';
  if (rename) $('p_name').value = curProfile().name;
  f.dataset.rename = rename ? '1' : '';
  $('dlgProfile').showModal(); $('p_name').select();
}
$('btnProfileNew').addEventListener('click', () => { closeMenus(); openProfileDlg(false); });
$('btnProfileRename').addEventListener('click', () => { closeMenus(); openProfileDlg(true); });
$('profileForm').addEventListener('submit', (e) => {
  e.preventDefault();
  const f = e.target, name = $('p_name').value.trim();
  if (f.dataset.rename) profileAction('rename', { id: curProfile().id, name }, f);
  else profileAction('create', { name }, f);
});
let logoutMode = 'remove';
function openLogout(mode) {
  logoutMode = mode;
  const cur = curProfile(), all = mode === 'wipe', f = $('logoutForm');
  resetForm(f);
  const others = (overview.profiles || []).filter((p) => !p.active);
  $('logoutTitle').textContent = all ? 'Стереть всё с этого компьютера?' : 'Выйти из профиля «' + cur.name + '»?';
  $('logoutLead').textContent = all
    ? 'Программа забудет все профили и откроется окно первого запуска, как после установки.'
    : (others.length ? 'Профиль будет удалён, откроется профиль «' + others[0].name + '».'
      : 'Это последний профиль: после выхода откроется окно первого запуска, как после установки.');
  const gone = $('logoutGone'); gone.textContent = '';
  const items = all
    ? ['все профили: настройки серверов (fleet.yaml)', 'SSH-пароли и ключи доступа к панелям', 'бэкапы главных серверов',
       'настройки и пароль синхронизации', 'журнал последней операции и лог программы']
    : ['настройки серверов (fleet.yaml)', 'SSH-пароли и ключи доступа к панелям', 'бэкапы главного сервера', 'настройки и пароль синхронизации'];
  for (const t of items) gone.appendChild(el('li', null, t));
  $('logoutExportBox').classList.toggle('hidden', all || !(overview && overview.configured));
  const word = all ? 'стереть' : cur.name;
  $('logoutAsk').textContent = 'Для подтверждения введите ' + (all ? 'слово «стереть»' : 'название профиля «' + cur.name + '»');
  $('lo_confirm').placeholder = word; f.dataset.word = word;
  $('logoutGo').textContent = all ? 'Стереть всё' : 'Выйти и стереть';
  $('logoutGo').disabled = true;
  $('dlgLogout').showModal();
}
$('lo_confirm').addEventListener('input', () => {
  const norm = (v) => v.replace(/\s+/g, ' ').trim().toLowerCase();
  $('logoutGo').disabled = norm($('lo_confirm').value) !== norm($('logoutForm').dataset.word || '');
});
$('btnProfileLogout').addEventListener('click', () => { closeMenus(); openLogout('remove'); });
$('btnProfileWipe').addEventListener('click', () => { closeMenus(); openLogout('wipe'); });
$('logoutExport').addEventListener('click', () => { resetForm($('appExportForm')); $('dlgAppExport').showModal(); });
$('logoutForm').addEventListener('submit', (e) => {
  e.preventDefault();
  const confirm = $('lo_confirm').value;
  if (logoutMode === 'wipe') profileAction('wipe', { confirm }, e.target);
  else profileAction('remove', { id: curProfile().id, confirm }, e.target);
});
