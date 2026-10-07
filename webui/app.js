'use strict';
// Profile names come from the provider's subscription: always insert them as text, never as HTML.

const $ = (id) => document.getElementById(id);
const DAY = 86400;
let csrf = null;
let statusData = null;
let tab = 'status';
let pollTimer = null;
let jobTimer = null;
let jobKey = null;
const cards = {};

const PHASES = {
  running: ['Работает', 'good'], degraded: ['Нет связи', 'critical'], refreshing: ['Обновляет подписку', 'warning'],
  connecting: ['Подключается', 'warning'], starting: ['Запускается', 'warning'], stopping: ['Останавливается', 'warning'],
  stopped: ['Остановлен', ''], failed: ['Сбой, перезапуск', 'critical'], stale: ['Нет данных', 'warning'],
  unknown: ['Нет данных', 'warning'],
};
const REASONS = {
  upstream: 'сервер провайдера не отвечает', profile_missing: 'профиль пропал из подписки',
  country_mismatch: 'сменилась страна выхода', manual: 'переподключение по запросу',
};
const ACTIONS = {
  probe: 'проверка связи', reconnect: 'переподключение', refresh: 'обновление подписки',
  wait: 'ждёт обновления подписки', restart: 'чистый перезапуск',
};
// The same texts as memory_budget.AWG_MODE_TEXT (a test checks it): feelings and percentages, no speeds.
const MEMORY_MODES = [
  [32, 'для экономии памяти: при нехватке процессора загрузки могут идти медленнее'],
  [64, 'рекомендуется: баланс памяти и нагрузки на процессор'],
  [128, 'больше запаса памяти для интенсивной нагрузки'],
];
const EVENTS = {
  start: 'запуск', up: 'связь есть', down: 'нет связи', refresh: 'обновление подписки',
  stop: 'остановка', failed: 'сбой',
};

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') el.className = value;
    else if (key === 'text') el.textContent = value;
    else if (key.startsWith('on')) el.addEventListener(key.slice(2), value);
    else el.setAttribute(key, value === true ? '' : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

async function api(path, body) {
  const options = { credentials: 'same-origin', headers: {} };
  if (body !== undefined) {
    options.method = 'POST';
    options.headers['Content-Type'] = 'application/json';
    options.headers['X-Exitpool-CSRF'] = csrf || '';
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  let data = {};
  try { data = await response.json(); } catch (e) { /* empty body */ }
  if (response.status === 401 && path !== '/api/login') { showLogin(); }
  if (!response.ok) throw new Error(data.error || ('Ошибка ' + response.status));
  return data;
}

function toast(text, bad) {
  const box = $('toast');
  box.textContent = text;
  box.className = 'toast' + (bad ? ' bad' : '');
  box.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { box.hidden = true; }, bad ? 7000 : 3500);
}

function dur(seconds) {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return s + ' с';
  const m = Math.floor(s / 60);
  if (m < 60) return m + ' мин';
  const hours = Math.floor(m / 60);
  if (hours < 48) return hours + ' ч' + (m % 60 ? ' ' + (m % 60) + ' мин' : '');
  return Math.floor(hours / 24) + ' д ' + (hours % 24) + ' ч';
}
function bytes(n) {
  if (n === null || n === undefined) return '?';
  if (n < 1048576) return Math.round(n / 1024) + ' КБ';
  if (n < 1073741824) return (n / 1048576).toFixed(n < 10485760 ? 1 : 0) + ' МБ';
  return (n / 1073741824).toFixed(2) + ' ГБ';
}
const clock = (ts) => new Date(ts * 1000).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
const dateTime = (ts) => new Date(ts * 1000).toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
function happTime(value) {
  if (!value) return null;
  const t = Date.parse(value + 'Z');   // Happ writes container time (UTC) without a zone
  return Number.isNaN(t) ? null : t / 1000;
}

// ---------------------------------------------------------------- session

function showLogin() {
  csrf = null;
  stopPolling();
  $('tabs').hidden = true;
  for (const id of ['view-status', 'view-subscription', 'view-access']) $(id).hidden = true;
  $('login').hidden = false;
  $('updated').textContent = '';
  setTimeout(() => $('login-password').focus(), 50);
}

function showApp() {
  $('login').hidden = true;
  $('tabs').hidden = false;
  switchTab(tab);
  startPolling();
}

async function init() {
  try {
    const session = await api('/api/session');
    if (session.authenticated) { csrf = session.csrf; showApp(); } else showLogin();
  } catch (e) {
    showLogin();
    $('login-error').textContent = e.message;
  }
}

$('login-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  $('login-error').textContent = '';
  const button = event.submitter;
  if (button) button.disabled = true;
  try {
    await api('/api/login', { password: $('login-password').value });
    $('login-password').value = '';
    const session = await api('/api/session');
    csrf = session.csrf;
    showApp();
  } catch (e) {
    $('login-error').textContent = e.message;
  } finally {
    if (button) button.disabled = false;
  }
});

$('logout').addEventListener('click', async () => {
  try { await api('/api/logout', {}); } catch (e) { /* ignore */ }
  showLogin();
});

$('password-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  $('password-error').textContent = '';
  if ($('pw-new').value !== $('pw-repeat').value) {
    $('password-error').textContent = 'Новые пароли не совпадают.';
    return;
  }
  try {
    await api('/api/password', { current: $('pw-current').value, new: $('pw-new').value });
    event.target.reset();
    toast('Пароль изменён. Войдите с новым паролем.');
    showLogin();
  } catch (e) {
    $('password-error').textContent = e.message;
  }
});

// ---------------------------------------------------------------- tabs & polling

for (const button of document.querySelectorAll('.tab')) {
  button.addEventListener('click', () => switchTab(button.dataset.tab));
}

function switchTab(name) {
  tab = name;
  for (const button of document.querySelectorAll('.tab')) button.classList.toggle('active', button.dataset.tab === name);
  $('view-status').hidden = name !== 'status';
  $('view-subscription').hidden = name !== 'subscription';
  $('view-access').hidden = name !== 'access';
  if (name === 'subscription') { renderSubscription(); loadJob(); loadBackups(); }
  if (name === 'access') loadPanel();
}

function startPolling() {
  stopPolling();
  const tick = async () => {
    if (!document.hidden) {
      try {
        statusData = await api('/api/status');
        renderStatus();
        if (tab === 'subscription') renderSubscription();
        const active = ['discovering', 'awaiting_selection', 'applying'].includes(statusData.job && statusData.job.phase);
        if (active && !jobTimer) loadJob();
      } catch (e) {
        if (csrf) $('updated').textContent = 'нет связи с сервером';
      }
    }
    if (csrf) pollTimer = setTimeout(tick, 5000);
  };
  tick();
}

function stopPolling() {
  clearTimeout(pollTimer); pollTimer = null;
  clearTimeout(jobTimer); jobTimer = null;
}

document.addEventListener('visibilitychange', () => {
  if (!document.hidden && csrf) startPolling();
});

// ---------------------------------------------------------------- status view

function tile(value, label) {
  return h('div', { class: 'tile' }, h('div', { class: 'value', text: value }), h('div', { class: 'label', text: label }));
}

function renderStatus() {
  const data = statusData;
  if (!data) return;
  $('updated').textContent = 'обновлено ' + clock(data.now);
  const working = data.instances.filter((i) => i.healthy).length;
  const availability = data.instances.map((i) => i.day.availability).filter((v) => v !== null);
  $('summary').replaceChildren(
    tile(working + ' / ' + data.instances.length, 'выходов работают'),
    tile(availability.length ? Math.min(...availability).toFixed(1) + '%' : '—', 'доступность за 24 ч'),
    tile(data.host.mem_available !== undefined ? data.host.mem_available + ' МиБ' : '—', 'RAM свободно'),
  );
  const names = new Set(data.instances.map((i) => i.name));
  for (const name of Object.keys(cards)) {
    if (!names.has(name)) { cards[name].root.remove(); delete cards[name]; }
  }
  for (const item of data.instances) renderCard(item, data.now);
  const host = data.host;
  const parts = [];
  if (host.mem_total) parts.push(`RAM ${host.mem_available} из ${host.mem_total} МиБ свободно`);
  if (host.disk_free !== undefined) parts.push(`диск свободно ${(host.disk_free / 1024).toFixed(1)} ГБ`);
  if (host.load) parts.push('нагрузка ' + host.load.join(' / '));
  const panel = data.panel || {};
  if (panel.enabled) parts.push(panel.ok ? 'панель 3x-ui: ' + dur(data.now - panel.checked_at) + ' назад' : 'панель 3x-ui: ' + (panel.error || 'ошибка'));
  $('host').textContent = 'Сервер: ' + parts.join(' · ');
  $('awg-add-panel').hidden = !(data.features && data.features.awg);
}

function createCard(name) {
  const card = {};
  card.badge = h('span', { class: 'badge' });
  card.title = h('div', { class: 'card-title', text: name });
  card.meta = h('div', { class: 'card-meta' });
  card.profile = h('div', { class: 'profile' });
  card.line = h('div', { class: 'line' });
  card.error = h('div', { class: 'line error' });
  card.latency = h('div', { class: 'line' });
  card.probe = h('button', { type: 'button', class: 'link', text: 'замерить', onclick: () => act(name, 'probe') });
  card.chips = h('div', { class: 'chips', hidden: true });
  card.stats = h('div', { class: 'stats' });
  card.strip = h('div', { class: 'strip', role: 'img' });
  card.note = h('div', { class: 'strip-note' });
  card.buttons = {
    refresh: h('button', { type: 'button', text: 'Обновить подписку', onclick: () => act(name, 'refresh') }),
    reconnect: h('button', { type: 'button', text: 'Переподключить', onclick: () => act(name, 'reconnect') }),
    restart: h('button', { type: 'button', text: 'Перезапустить', onclick: () => act(name, 'restart', 'Перезапустить выход ' + name + '? Он будет недоступен ~20–40 с.') }),
    toggle: h('button', { type: 'button', class: 'danger' }),
    settings: h('button', { type: 'button', text: 'Настройки', onclick: () => toggleDrawer(name, 'settings') }),
    log: h('button', { type: 'button', text: 'Журнал', onclick: () => toggleDrawer(name, 'log') }),
  };
  card.buttons.toggle.addEventListener('click', () => {
    const running = card.active;
    act(name, running ? 'stop' : 'start', running ? 'Остановить выход ' + name + '? Балансеры панели будут использовать остальные.' : null);
  });
  card.drawer = h('div', { class: 'drawer', hidden: true });
  card.drawerKind = null;
  card.root = h('article', { class: 'card' },
    h('div', { class: 'card-head' }, h('div', {}, card.title, card.meta), card.badge),
    card.profile, card.line, card.error, card.latency, card.chips, card.stats,
    h('div', { class: 'strip-wrap' }, card.strip,
      h('div', { class: 'strip-axis' }, h('span', { text: '−24 ч' }), h('span', { text: '−12 ч' }), h('span', { text: 'сейчас' }))),
    card.note,
    h('div', { class: 'actions' }, Object.values(card.buttons)),
    card.drawer);
  return card;
}

function upSince(item) {
  for (let i = item.events.length - 1; i >= 0; i -= 1) {
    const e = item.events[i];
    if (e.event === 'up') return e.at;
    if (['down', 'failed', 'stop', 'start'].includes(e.event)) break;
  }
  return item.started_at;
}

function renderCard(item, now) {
  if (!cards[item.name]) {
    cards[item.name] = createCard(item.name);
    $('instances').append(cards[item.name].root);
  }
  const card = cards[item.name];
  card.item = item;
  card.active = item.unit.active === 'active' || item.unit.active === 'activating';
  const awg = item.kind === 'awg';
  const tunnel = item.tunnel || {};
  const [label, kind] = PHASES[item.phase] || PHASES.unknown;
  card.badge.className = 'badge ' + kind;
  card.badge.textContent = label;
  card.meta.textContent = (awg ? 'AWG · ' : 'Happ · ') + item.tag + ' · socks ' + (item.address || '127.0.0.1:' + item.port);
  card.profile.textContent = awg
    ? (item.profile || 'AmneziaWG') + (tunnel.endpoint ? ' · сервер ' + tunnel.endpoint : '') +
      (item.country ? ' · только ' + item.country : '')
    : (item.profile || '—') + (item.exact ? '' : ' (поиск по названию)') + (item.country ? ' · только ' + item.country : '');

  let line = '';
  if (item.phase === 'running') {
    const since = upSince(item);
    line = 'Выход ' + (item.exit_country || '?') + (item.exit_ip ? ' · ' + item.exit_ip : '') +
      (since ? ' · без перерыва ' + dur(now - since) : '') +
      (awg && tunnel.handshake_age !== undefined ? ' · рукопожатие ' + dur(tunnel.handshake_age) + ' назад' : '');
  } else if (item.phase === 'degraded') {
    line = 'Нет связи ' + (item.down_since ? dur(now - item.down_since) : '') + ' · ' +
      (REASONS[item.degraded_reason] || item.degraded_reason || '') +
      (item.action ? ' · сейчас: ' + (ACTIONS[item.action] || item.action) : '');
  } else if (item.phase === 'stopped') {
    line = 'Служба остановлена';
  } else if (item.phase === 'stale') {
    line = 'Служба не обновляла статус ' + (item.updated_at ? dur(now - item.updated_at) : '');
  }
  card.line.textContent = line;
  card.error.textContent = item.phase !== 'running' && item.error ? item.error : '';
  card.error.hidden = !card.error.textContent;

  renderLatency(card, item, now);
  renderChips(card, item, now);

  const day = item.day;
  const updated = happTime(item.subscription_updated);
  const stats = [
    '24 ч: ' + (day.availability !== null ? day.availability.toFixed(1) + '%' : 'мало данных') +
      (day.outages ? ' · простоев ' + day.outages + ', ' + dur(day.downtime) : ' · без простоев'),
    'перезапусков службы ' + item.unit.restarts,
  ];
  if (awg && item.memory_mode) {
    stats.push('режим памяти ' + item.memory_mode + ' МиБ (ожидаемо ~' + item.budget_mib + ')' +
      (item.memory_mib ? ', сейчас ' + item.memory_mib + ' МиБ' : ''));
  } else if (item.memory_mib) stats.push('RAM ' + item.memory_mib + ' МиБ');
  if (updated) stats.push('подписка ' + dateTime(updated) + (item.refresh_ok === false ? ' (последнее обновление не удалось)' : ''));
  if (awg && tunnel.rx !== undefined) stats.push('туннель ↓' + bytes(tunnel.rx) + ' ↑' + bytes(tunnel.tx));
  if (awg && tunnel.mtu) stats.push('MTU ' + tunnel.mtu);
  card.stats.replaceChildren(...stats.map((text) => h('span', { text })));

  renderStrip(card, item, now);

  card.buttons.refresh.hidden = awg;
  card.buttons.refresh.disabled = !card.active;
  card.buttons.reconnect.disabled = !card.active;
  card.buttons.toggle.textContent = card.active ? 'Стоп' : 'Старт';
  card.buttons.toggle.className = card.active ? 'danger' : 'primary';
  if (card.drawerKind === 'log') renderEvents(card);
}

function renderLatency(card, item, now) {
  const l = item.latency || {};
  const parts = [];
  if (l.last !== null && l.last !== undefined) {
    const fresh = item.phase === 'running' || item.phase === 'degraded';
    parts.push((fresh && item.healthy ? 'Задержка ' : 'Последняя задержка ') + l.last + ' мс');
    if (l.median !== null && l.samples > 2) parts.push('медиана 30 мин ' + l.median + ' мс');
    if (l.at) parts.push(dur(now - l.at) + ' назад');
  } else {
    parts.push('Задержка ещё не замерена');
  }
  card.latency.replaceChildren(parts.join(' · ') + ' ', card.active ? card.probe : '');
}

function renderChips(card, item, now) {
  const panel = item.panel;
  const global = statusData && statusData.panel;
  if (!panel || !global || !global.enabled || !global.ok) { card.chips.hidden = true; return; }
  const chips = panel.balancers.map((b) => h('span', {
    class: 'chip' + (b.selected ? ' on' : '') + (b.override ? ' warn' : ''),
    text: b.tag + (b.selected ? ': в работе' : ': резерв') + (b.override ? ' (ручной выбор: ' + b.override + ')' : ''),
  }));
  const seen = panel.observatory;
  if (seen) {
    chips.push(h('span', { class: 'chip' + (seen.alive ? '' : ' warn'),
      text: 'балансер видит: ' + (seen.alive ? seen.delay + ' мс' : 'недоступен') }));
  }
  if (!panel.balancers.length) chips.push(h('span', { class: 'chip', text: 'не входит в балансеры' }));
  card.chips.replaceChildren(...chips);
  card.chips.hidden = false;
}

function renderStrip(card, item, now) {
  const start = now - DAY;
  const pieces = [];
  const covered = item.day.covered || 0;
  if (covered < DAY) {
    const unknown = h('div', { class: 'unknown', title: 'нет данных' });
    unknown.style.width = ((DAY - covered) / DAY * 100) + '%';
    pieces.push(unknown);
  }
  for (const [a, b] of item.day.intervals) {
    const text = 'Простой ' + clock(a) + '–' + clock(b) + ' (' + dur(b - a) + ')';
    const gap = h('div', { class: 'gap', title: text, 'aria-label': text, tabindex: '0' });
    gap.style.left = ((a - start) / DAY * 100) + '%';
    gap.style.width = ((b - a) / DAY * 100) + '%';
    const show = () => { card.note.textContent = text; };
    gap.addEventListener('click', show);
    gap.addEventListener('mouseenter', show);
    gap.addEventListener('focus', show);
    pieces.push(gap);
  }
  card.strip.replaceChildren(...pieces);
  card.strip.setAttribute('aria-label', 'Доступность за 24 часа: ' +
    (item.day.outages ? item.day.outages + ' простоев, всего ' + dur(item.day.downtime) : 'без простоев'));
  if (!item.day.intervals.length) card.note.textContent = '';
}

async function act(name, action, question) {
  if (question && !window.confirm(question)) return;
  try {
    await api('/api/instances/' + encodeURIComponent(name) + '/action', { action });
    const texts = {
      refresh: 'Обновление подписки запрошено', reconnect: 'Переподключение запрошено', probe: 'Замер запрошен',
      restart: 'Перезапуск…', stop: 'Остановлен', start: 'Запускается…',
    };
    toast(name + ': ' + texts[action]);
    setTimeout(startPolling, 1200);
  } catch (e) {
    toast(e.message, true);
  }
}

// ---------------------------------------------------------------- drawers

function toggleDrawer(name, kind) {
  const card = cards[name];
  if (card.drawerKind === kind) {
    card.drawerKind = null;
    card.drawer.hidden = true;
    card.drawer.replaceChildren();
    return;
  }
  card.drawerKind = kind;
  card.drawer.hidden = false;
  if (kind === 'settings') (card.item.kind === 'awg' ? buildAwgSettings : buildSettings)(card);
  else buildLog(card);
}

function readProfile(fileInput, textArea) {
  // The profile goes straight to the server; it is never stored in the browser.
  const file = fileInput.files && fileInput.files[0];
  if (!file) return Promise.resolve(textArea.value);
  if (file.size > 16384) return Promise.reject(new Error('Файл больше 16 КБ — это не профиль AWG.'));
  return file.text();
}

function buildAwgSettings(card) {
  const item = card.item;
  const prefix = 'set-' + item.name + '-';
  const s = item.settings;
  const label = h('input', { id: prefix + 'label', maxlength: '80', value: s.label || '', placeholder: 'необязательно' });
  const country = h('input', { id: prefix + 'country', maxlength: '2', autocapitalize: 'characters', value: item.country || '', placeholder: 'любая' });
  const memory = memoryField(prefix + 'memory', s.memory_mib);
  const save = h('button', { type: 'button', class: 'primary wide', text: 'Сохранить и перезапустить выход' });
  save.addEventListener('click', async () => {
    const value = (id) => Number(document.getElementById(prefix + id).value);
    const settings = {
      label: label.value.trim(), country: country.value.trim().toUpperCase(),
      mtu: value('mtu') || null, health_interval: value('interval'),
      failure_threshold: value('threshold'), memory_mib: memory.read(),
    };
    if (!window.confirm('Сохранить? Выход ' + item.name + ' перезапустится (~10–20 с).')) return;
    save.disabled = true;
    try {
      await api('/api/instances/' + encodeURIComponent(item.name) + '/settings', { settings });
      toast(item.name + ': сохранено, перезапуск…');
      toggleDrawer(item.name, 'settings');
      setTimeout(startPolling, 1500);
    } catch (e) {
      toast(e.message, true);
      save.disabled = false;
    }
  });
  const file = h('input', { id: prefix + 'file', type: 'file', accept: '.conf,text/plain' });
  const text = h('textarea', { id: prefix + 'text', rows: '4', autocomplete: 'off', autocapitalize: 'off', spellcheck: 'false', placeholder: '[Interface]…' });
  const replace = h('button', { type: 'button', class: 'wide', text: 'Заменить профиль' });
  replace.addEventListener('click', async () => {
    let profile;
    try { profile = await readProfile(file, text); } catch (e) { toast(e.message, true); return; }
    if (!profile.trim()) { toast('Выберите файл или вставьте текст профиля', true); return; }
    if (!window.confirm('Заменить профиль выхода ' + item.name + '? Если новый не заработает, вернётся прежний.')) return;
    replace.disabled = true;
    try {
      await api('/api/instances/' + encodeURIComponent(item.name) + '/profile', { profile });
      file.value = ''; text.value = '';
      toast(item.name + ': замена профиля…');
      loadJob();
    } catch (e) {
      toast(e.message, true);
    } finally {
      replace.disabled = false;
    }
  });
  const remove = h('button', { type: 'button', class: 'danger wide', text: 'Удалить выход' });
  remove.addEventListener('click', async () => {
    if (!window.confirm('Удалить выход ' + item.name + '? Профиль сохранится в резервной копии. ' +
      'Исходящий ' + item.tag + ' в панели 3x-ui останется — уберите его из балансеров и удалите там.')) return;
    remove.disabled = true;
    try {
      await api('/api/instances/' + encodeURIComponent(item.name) + '/remove', {});
      toast(item.name + ': удалён. Не забудьте убрать ' + item.tag + ' из панели.');
      setTimeout(startPolling, 500);
    } catch (e) {
      toast(e.message, true);
      remove.disabled = false;
    }
  });
  card.drawer.replaceChildren(
    h('div', { class: 'row' },
      h('div', {}, h('label', { for: prefix + 'label', text: 'Подпись' }), label),
      h('div', {}, h('label', { for: prefix + 'country', text: 'Только страна (код)' }), country),
      numberField(prefix + 'mtu', 'MTU (пусто — из профиля или 1280)', s.mtu, 1280, 1500),
      numberField(prefix + 'interval', 'Проверка, с', s.health_interval, 5, 60),
      numberField(prefix + 'threshold', 'Неудач до переподключения', s.failure_threshold, 2, 10)),
    memory,
    h('p', { class: 'muted small', text: 'Тег ' + item.tag + ' и адрес ' + (item.address || item.port) + ' не меняются: на них ссылаются балансеры панели.' }),
    save,
    h('h2', { text: 'Заменить профиль' }),
    h('label', { for: prefix + 'file', text: 'Файл .conf' }), file,
    h('label', { for: prefix + 'text', text: 'или текст профиля' }), text,
    replace,
    h('h2', { text: 'Удаление' }),
    remove);
}

$('awg-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  $('awg-error').textContent = '';
  const button = event.submitter;
  let profile;
  try { profile = await readProfile($('awg-file'), $('awg-text')); } catch (e) { $('awg-error').textContent = e.message; return; }
  if (!profile.trim()) { $('awg-error').textContent = 'Выберите файл или вставьте текст профиля.'; return; }
  const name = $('awg-name').value.trim().toLowerCase();
  if (button) button.disabled = true;
  try {
    const result = await api('/api/awg', {
      name, profile, tag: $('awg-tag').value.trim(), country: $('awg-country').value.trim().toUpperCase(),
      label: $('awg-label').value.trim(), memory_mib: addMemory.read(),
    });
    event.target.reset();
    addMemory.reset();
    toast('Выход ' + name + ': ' + result.tag + ', socks ' + (result.address || '127.0.0.1') + ':' + result.port + ' — запускается…');
    loadJob();
  } catch (e) {
    $('awg-error').textContent = e.message;
  } finally {
    if (button) button.disabled = false;
  }
});

const addMemory = memoryField('awg-memory', 64);
$('awg-memory-slot').replaceWith(addMemory);

$('awg-name').addEventListener('input', () => {
  const name = $('awg-name').value.trim().toLowerCase();
  $('awg-tag').placeholder = name ? (name.startsWith('awg') ? name : 'awg-' + name) : 'awg-имя';
});

function renderAwgJob(job) {
  const titles = { applying: 'Выполняется…', done: 'Готово', failed: 'Не получилось' };
  const what = job.kind === 'awg-replace' ? 'Замена профиля AWG: ' : 'Новый AWG-выход: ';
  $('awg-job').replaceChildren(h('h2', { text: what + (titles[job.phase] || job.phase) }),
    h('ul', { class: 'job-log' }, (job.log || []).map(([at, text]) => h('li', {}, h('time', { text: clock(at) }), text))));
}

function memoryField(id, value) {
  // Ascending 32 → 64 → 128 → custom; 64 preselected.
  const current = Number(value) || 64;
  const select = h('select', { id });
  for (const [mode, text] of MEMORY_MODES) select.append(h('option', { value: String(mode), text: mode + ' МиБ — ' + text }));
  select.append(h('option', { value: 'custom', text: 'своё значение' }));
  const custom = h('input', { id: id + '-custom', type: 'number', inputmode: 'numeric', min: '32', max: '1024', 'aria-label': 'Своё значение, МиБ' });
  const show = (mode) => {
    const known = MEMORY_MODES.some(([m]) => m === mode);
    select.value = known ? String(mode) : 'custom';
    custom.value = String(mode);
    custom.hidden = known;
  };
  select.addEventListener('change', () => { custom.hidden = select.value !== 'custom'; if (!custom.hidden) custom.focus(); });
  show(current);
  const wrap = h('div', { class: 'memory-field' }, h('label', { for: id, text: 'Режим памяти, МиБ' }), select, custom);
  wrap.read = () => (select.value === 'custom' ? Number(custom.value) : Number(select.value));
  wrap.reset = () => show(64);
  return wrap;
}

function numberField(id, label, value, min, max) {
  return h('div', {}, h('label', { for: id, text: label }),
    h('input', { id, type: 'number', inputmode: 'numeric', min, max, value: String(value ?? '') }));
}

function buildSettings(card) {
  const item = card.item;
  const prefix = 'set-' + item.name + '-';
  const counts = {};
  for (const n of item.catalog) counts[n] = (counts[n] || 0) + 1;
  const select = h('select', { id: prefix + 'profile' });
  const options = Object.keys(counts).filter((n) => counts[n] === 1);
  if (!options.includes(item.profile)) {
    select.append(h('option', { value: item.profile, text: item.profile + (item.exact ? ' (нет в списке)' : ' (текущий поиск)') }));
  }
  for (const n of options) select.append(h('option', { value: n, text: n }));
  select.value = item.profile;
  const s = item.settings;
  const country = h('input', { id: prefix + 'country', maxlength: '2', autocapitalize: 'characters', value: item.country || '', placeholder: 'любая' });
  const save = h('button', { type: 'button', class: 'primary wide', text: 'Сохранить и перезапустить выход' });
  save.addEventListener('click', async () => {
    const value = (id) => Number(document.getElementById(prefix + id).value);
    const settings = {
      country: country.value.trim().toUpperCase(),
      update_hours: value('update'), health_interval: value('interval'),
      failure_threshold: value('threshold'), memory_mib: value('memory'),
    };
    if (select.value !== item.profile || item.exact) settings.profile_name = select.value;
    if (!window.confirm('Сохранить? Выход ' + item.name + ' перезапустится (~20–40 с).')) return;
    save.disabled = true;
    try {
      await api('/api/instances/' + encodeURIComponent(item.name) + '/settings', { settings });
      toast(item.name + ': сохранено, перезапуск…');
      toggleDrawer(item.name, 'settings');
      setTimeout(startPolling, 1500);
    } catch (e) {
      toast(e.message, true);
      save.disabled = false;
    }
  });
  card.drawer.replaceChildren(
    h('label', { for: prefix + 'profile', text: 'Профиль подписки' }), select,
    item.catalog.length ? null : h('p', { class: 'muted small', text: 'Список профилей появится после первого обновления подписки.' }),
    h('div', { class: 'row' },
      h('div', {}, h('label', { for: prefix + 'country', text: 'Только страна (код)' }), country),
      numberField(prefix + 'update', 'Обновлять подписку, ч', s.update_hours, 1, 168),
      numberField(prefix + 'interval', 'Проверка, с', s.health_interval, 10, 60),
      numberField(prefix + 'threshold', 'Неудач до переподключения', s.failure_threshold, 2, 10),
      numberField(prefix + 'memory', 'Лимит RAM, МиБ', s.memory_mib, 256, 2048)),
    h('p', { class: 'muted small', text: 'Тег ' + item.tag + ' и порт ' + item.port + ' не меняются: на них ссылаются балансеры панели.' }),
    save);
}

function renderEvents(card) {
  const list = card.drawer.querySelector('.events');
  if (!list) return;
  const items = card.item.events.slice().reverse().map((e) => {
    let text = dateTime(e.at) + ' — ' + (EVENTS[e.event] || e.event);
    if (e.reason) text += ' (' + (REASONS[e.reason] || (e.reason === 'start' ? 'после запуска' : e.reason)) + ')';
    if (e.duration !== undefined) text += ', ' + dur(e.duration);
    if (e.ok === false) text += ': не удалось';
    if (e.error) text += ': ' + e.error;
    return h('li', { text });
  });
  list.replaceChildren(...(items.length ? items : [h('li', { text: 'Событий пока нет' })]));
}

async function buildLog(card) {
  const pre = h('pre', { class: 'log', text: 'Загрузка…' });
  card.drawer.replaceChildren(h('h2', { text: 'События' }), h('ul', { class: 'events' }), h('h2', { text: 'Журнал службы' }), pre);
  renderEvents(card);
  try {
    const data = await api('/api/instances/' + encodeURIComponent(card.item.name) + '/log');
    pre.textContent = data.lines.join('\n') || 'Пусто';
    pre.scrollTop = pre.scrollHeight;
  } catch (e) {
    pre.textContent = e.message;
  }
}

// ---------------------------------------------------------------- 3x-ui panel settings

function panelStateText(state) {
  if (!state || !state.enabled) return state && state.configured ? 'Отключено.' : 'Не настроено.';
  if (!state.ok) return '';
  return 'Связь с панелью есть, проверено ' + clock(state.checked_at) + '.';
}

async function loadPanel() {
  let data;
  try { data = await api('/api/panel'); } catch (e) { return; }
  const s = data.settings;
  $('panel-host').value = s.host || '';
  $('panel-port').value = s.port || '';
  $('panel-base').value = s.base_path || '';
  $('panel-https').checked = !!s.https;
  $('panel-enabled').checked = s.configured ? s.enabled : true;
  $('panel-token').value = '';
  $('panel-token').placeholder = s.configured ? 'задан (отпечаток ' + s.token_fingerprint + '); пусто — не менять' : 'Bearer-токен из настроек панели';
  $('panel-token').required = !s.configured;
  $('panel-clear').hidden = !s.configured;
  $('panel-state').textContent = panelStateText(data.state);
  $('panel-error').textContent = data.state && data.state.enabled && !data.state.ok ? (data.state.error || 'Панель не ответила.') : '';
}

$('panel-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  $('panel-error').textContent = '';
  const button = event.submitter;
  if (button) button.disabled = true;
  try {
    const data = await api('/api/panel', { settings: {
      host: $('panel-host').value.trim(), port: Number($('panel-port').value), base_path: $('panel-base').value.trim(),
      https: $('panel-https').checked, token: $('panel-token').value.trim(), enabled: $('panel-enabled').checked,
    } });
    toast('Панель сохранена');
    await loadPanel();
    $('panel-state').textContent = panelStateText(data.state);
  } catch (e) {
    $('panel-error').textContent = e.message;
  } finally {
    if (button) button.disabled = false;
  }
});

$('panel-clear').addEventListener('click', async () => {
  if (!window.confirm('Удалить адрес и токен панели с сервера?')) return;
  try { await api('/api/panel/clear', {}); toast('Настройки панели удалены'); loadPanel(); } catch (e) { toast(e.message, true); }
});

// ---------------------------------------------------------------- subscription view

function renderSubscription() {
  const data = statusData;
  if (!data) return;
  const sub = data.subscription;
  const updated = data.instances.map((i) => happTime(i.subscription_updated)).filter(Boolean);
  const parts = sub.present ? ['Ключ ' + sub.kind, 'отпечаток ' + sub.fingerprint] : ['Ключ не найден'];
  if (updated.length) parts.push('обновлялась ' + dateTime(Math.max(...updated)));
  $('sub-current').textContent = parts.join(' · ');
}

$('discover').addEventListener('click', async () => {
  const key = $('sub-key').value.trim();
  if (!key) { toast('Вставьте ссылку подписки', true); return; }
  $('discover').disabled = true;
  try {
    await api('/api/subscription/discover', { key });
    $('sub-key').value = '';
    loadJob();
  } catch (e) {
    toast(e.message, true);
  } finally {
    $('discover').disabled = false;
  }
});

async function loadJob() {
  clearTimeout(jobTimer); jobTimer = null;
  let job;
  try { job = await api('/api/job'); } catch (e) { return; }
  if ((job.kind || '').startsWith('awg')) {
    renderAwgJob(job);
    if (job.phase === 'applying') { jobTimer = setTimeout(loadJob, 2000); return; }
    if (jobKey !== job.started_at + ':' + job.phase) {
      jobKey = job.started_at + ':' + job.phase;
      startPolling();
    }
    return;
  }
  renderJob(job);
  if (['discovering', 'awaiting_selection', 'applying'].includes(job.phase)) {
    jobTimer = setTimeout(loadJob, job.phase === 'awaiting_selection' ? 5000 : 2000);
  } else if (jobKey && jobKey.endsWith('applying')) {
    loadBackups();
  }
}

function renderJob(job) {
  const box = $('job');
  const active = ['discovering', 'awaiting_selection', 'applying'].includes(job.phase);
  $('replace-start').hidden = active;
  if (job.phase === 'idle') { box.replaceChildren(); jobKey = null; return; }
  const key = job.started_at + ':' + job.phase;
  const titles = {
    discovering: 'Читаю новую подписку…', awaiting_selection: 'Выберите профили', applying: 'Применяю…',
    done: 'Готово', failed: 'Не получилось', cancelled: 'Отменено',
  };
  const log = h('ul', { class: 'job-log' }, (job.log || []).map(([at, text]) =>
    h('li', {}, h('time', { text: clock(at) }), text)));
  if (job.phase === 'awaiting_selection' && jobKey === key && box.querySelector('.mapping')) {
    box.querySelector('.job-log').replaceWith(log);
    return;   // keep the user's selections
  }
  jobKey = key;
  const children = [h('h2', { text: (job.kind === 'restore' ? 'Восстановление: ' : '') + (titles[job.phase] || job.phase) }), log];
  if (job.phase === 'awaiting_selection') children.push(buildMapping(job));
  box.replaceChildren(...children);
}

function buildMapping(job) {
  const counts = {};
  for (const n of job.catalog) counts[n] = (counts[n] || 0) + 1;
  const unique = job.catalog.filter((n) => counts[n] === 1);
  const instances = statusData ? statusData.instances : [];
  const rows = instances.map((item) => {
    const proposal = (job.proposal || {})[item.name] || {};
    const select = h('select', { 'data-name': item.name, class: 'map-profile' },
      h('option', { value: '', text: '— выберите профиль —' }), unique.map((n) => h('option', { value: n, text: n })));
    select.value = proposal.profile_name || '';
    const country = h('input', { class: 'map-country', maxlength: '2', autocapitalize: 'characters', placeholder: 'любая', value: proposal.country || '' });
    return h('div', { class: 'map-row', 'data-name': item.name },
      h('div', { class: 'small muted', text: item.name + ' · ' + item.tag + ' · сейчас: ' + item.profile }),
      h('div', { class: 'grid' }, select, country));
  });
  const apply = h('button', { type: 'button', class: 'primary wide', text: 'Применить' });
  const cancel = h('button', { type: 'button', class: 'wide', text: 'Отменить' });
  apply.addEventListener('click', async () => {
    const mapping = {};
    for (const row of rows) {
      const name = row.dataset.name;
      mapping[name] = { profile_name: row.querySelector('.map-profile').value, country: row.querySelector('.map-country').value.trim().toUpperCase() };
      if (!mapping[name].profile_name) { toast('Выберите профиль для ' + name, true); return; }
    }
    if (!window.confirm('Заменить подписку? Выходы переключатся по одному; если первый не заработает, всё вернётся как было.')) return;
    apply.disabled = true; cancel.disabled = true;
    try { await api('/api/subscription/apply', { mapping }); loadJob(); } catch (e) { toast(e.message, true); apply.disabled = false; cancel.disabled = false; }
  });
  cancel.addEventListener('click', async () => {
    try { await api('/api/subscription/cancel', {}); loadJob(); } catch (e) { toast(e.message, true); }
  });
  const expires = job.expires_at ? h('p', { class: 'muted small', text: 'Временный Happ ждёт выбора до ' + clock(job.expires_at) + '. Страна — необязательная проверка выхода (например DE).' }) : null;
  return h('div', { class: 'mapping' }, rows, expires, apply, cancel);
}

async function loadBackups() {
  let data;
  try { data = await api('/api/backups'); } catch (e) { return; }
  const box = $('backups');
  if (!data.backups.length) { box.replaceChildren(h('p', { class: 'muted small', text: 'Копий пока нет.' })); return; }
  const labels = { subscription: 'перед заменой подписки', 'before-restore': 'перед восстановлением', upgrade: 'перед обновлением программы (только настройки)' };
  box.replaceChildren(...data.backups.map((b) => {
    const button = h('button', { type: 'button', text: 'Восстановить' });
    button.addEventListener('click', async () => {
      if (!window.confirm('Восстановить копию от ' + dateTime(b.created) + '? Выходы перезапустятся по одному.')) return;
      try { await api('/api/backups/restore', { id: b.id }); loadJob(); } catch (e) { toast(e.message, true); }
    });
    const profiles = Object.entries(b.instances || {}).map(([n, p]) => n + ': ' + p).join(', ');
    return h('div', { class: 'backup' },
      h('div', {}, h('div', { text: dateTime(b.created) + ' — ' + (labels[b.label] || b.label) }),
        h('div', { class: 'muted small', text: 'отпечаток ' + (b.fingerprint || '?') + ' · ' + profiles })),
      button);
  }));
}

init();
