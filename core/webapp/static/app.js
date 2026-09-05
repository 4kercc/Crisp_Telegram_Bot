/* Crisp Telegram Bot 控制台前端 */
'use strict';

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

const state = {
  view: 'dashboard',
  logAfter: 0,
  eventAfter: 0,
  eventCount: 0,
  logCount: 0,
  engineBusy: false,
  configLoaded: false,
  statusTimer: null,
  logTimer: null,
};

/* ---------- 基础 ---------- */

async function api(path, options = {}) {
  const opts = { headers: {}, ...options };
  if (opts.body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(opts.body);
  }
  const resp = await fetch(path, opts);
  let data = {};
  try { data = await resp.json(); } catch (err) { /* ignore */ }
  if (resp.status === 401) {
    const st = await fetch('/api/auth/state').then((r) => r.json()).catch(() => ({}));
    showAuth(st.setup_required ? 'setup' : 'login');
    throw new Error(data.error || '未登录');
  }
  return { ok: resp.ok, status: resp.status, data };
}

let toastTimer = null;
function toast(message, isError = false) {
  const el = $('#toast');
  el.textContent = message;
  el.classList.toggle('err', isError);
  el.classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add('hidden'), 2800);
}

function fmtTime(ts) {
  const d = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}
function fmtFull(ts) {
  const d = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getMonth() + 1}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}
function fmtUptime(sec) {
  if (!sec) return '—';
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h > 0 ? `${h}时${m}分` : (m > 0 ? `${m}分${s}秒` : `${s}秒`);
}

/* ---------- 认证 ---------- */

function showAuth(mode) {
  stopPolling();
  $('#app').classList.add('hidden');
  $('#auth-overlay').classList.remove('hidden');
  const isSetup = mode === 'setup';
  $('#auth-title').textContent = isSetup ? '初始化控制台' : '控制台登录';
  $('#auth-sub').textContent = isSetup
    ? '首次使用，请先设置控制台访问密码（至少 6 位）'
    : '请输入控制台密码';
  $('#auth-password2').classList.toggle('hidden', !isSetup);
  $('#auth-submit').textContent = isSetup ? '创建密码并进入' : '登 录';
  $('#auth-error').textContent = '';
  $('#auth-password').value = '';
  $('#auth-password2').value = '';
  $('#auth-submit').dataset.mode = mode;
  setTimeout(() => $('#auth-password').focus(), 50);
}

async function submitAuth() {
  const btn = $('#auth-submit');
  const mode = btn.dataset.mode;
  const password = $('#auth-password').value;
  const errEl = $('#auth-error');
  errEl.textContent = '';
  if (mode === 'setup' && password !== $('#auth-password2').value) {
    errEl.textContent = '两次输入的密码不一致';
    return;
  }
  btn.disabled = true;
  try {
    const { ok, data } = await api('/api/auth/' + (mode === 'setup' ? 'setup' : 'login'), {
      method: 'POST',
      body: { password },
    });
    if (!ok) {
      errEl.textContent = data.error || '操作失败';
      return;
    }
    enterApp();
  } catch (err) {
    errEl.textContent = err.message === 'Failed to fetch' ? '无法连接控制台服务' : err.message;
  } finally {
    btn.disabled = false;
  }
}

function enterApp() {
  $('#auth-overlay').classList.add('hidden');
  $('#app').classList.remove('hidden');
  startPolling();
  refreshStatus();
  loadConfigOnce();
}

function stopPolling() {
  clearInterval(state.statusTimer);
  clearInterval(state.logTimer);
}

function startPolling() {
  stopPolling();
  state.statusTimer = setInterval(refreshStatus, 2000);
  state.logTimer = setInterval(pollRecords, 1000);
}

/* ---------- 状态 ---------- */

const STATE_TEXT = { running: '运行中', starting: '启动中…', stopping: '停止中…', stopped: '已停止', error: '异常' };
const STATE_CLASS = { running: 'pill-green', starting: 'pill-amber', stopping: 'pill-amber', stopped: 'pill-gray', error: 'pill-red' };

async function refreshStatus() {
  let resp;
  try {
    resp = await api('/api/status');
  } catch (err) { return; }
  const data = resp.data;
  if (!data.authed) { return; }

  $('#version-label').textContent = 'v' + (data.version || '3.0');

  const engine = data.engine || {};
  const st = engine.state || 'stopped';
  const pill = $('#engine-pill');
  pill.textContent = STATE_TEXT[st] || st;
  pill.className = 'pill ' + (STATE_CLASS[st] || 'pill-gray');

  // 引擎卡片
  const stateEl = $('#engine-state');
  stateEl.textContent = STATE_TEXT[st] || st;
  stateEl.style.color = st === 'running' ? 'var(--green)' : (st === 'error' ? 'var(--red)' : 'var(--text-dim)');
  $('#engine-mode').textContent = engine.mode ? `消息模式：${engine.mode === 'rtm' ? 'RTM 实时推送' : 'REST 定时轮询'}` : '';
  const errEl = $('#engine-error');
  if (engine.last_error) {
    errEl.textContent = '最近错误：' + engine.last_error;
    errEl.classList.remove('hidden');
  } else {
    errEl.classList.add('hidden');
  }

  $('#btn-start').disabled = state.engineBusy || st === 'running' || st === 'starting';
  $('#btn-stop').disabled = state.engineBusy || st === 'stopped' || st === 'stopping';
  $('#btn-restart').disabled = state.engineBusy || st === 'starting' || st === 'stopping';

  // 运行信息
  const info = [];
  info.push(['Telegram 机器人', engine.tg_username ? '@' + engine.tg_username : '—']);
  info.push(['Crisp 网站', engine.crisp_website_name || '—']);
  info.push(['运行时长', fmtUptime(engine.uptime)]);
  info.push(['控制台地址', `${data.console_host}:${data.console_port}`]);
  if (!data.config_ready) {
    info.push(['配置状态', '⚠️ 配置不完整，请先完成参数配置']);
  }
  $('#run-info').innerHTML = info.map(([k, v]) =>
    `<div class="kv-row"><span class="k">${k}</span><span class="v">${v}</span></div>`).join('');

  // 安全提示
  const warn = $('#host-warning');
  if (data.console_host === '0.0.0.0' || data.console_host === '::') {
    warn.textContent = '⚠️ 控制台正监听 0.0.0.0（所有网卡），任何能访问该端口的人都会看到登录页。请务必设置高强度密码，或改用 127.0.0.1 / 防火墙限制来源。';
    warn.classList.remove('hidden');
  } else {
    warn.classList.add('hidden');
  }

  // 消息统计
  const c = data.counters || {};
  const stats = [
    ['in', c.message_in, 'Crisp → TG'],
    ['out', c.message_out, 'TG → Crisp'],
    ['auto', c.autoreply, '自动回复'],
    ['err', c.error, '错误'],
  ];
  $('#msg-stats').innerHTML = stats.map(([cls, val, label]) =>
    `<div class="stat ${cls}"><div class="num">${val ?? 0}</div><div class="label">${label}</div></div>`).join('');

  // 令牌用量
  if (engine.tokens) {
    renderTokenUsage(engine.tokens.tokens, engine.tokens.current, engine.tokens.limit);
  }
}

async function engineAction(action) {
  state.engineBusy = true;
  const buttons = ['#btn-start', '#btn-stop', '#btn-restart'];
  buttons.forEach((s) => { $(s).disabled = true; });
  try {
    const { ok, data } = await api('/api/engine/' + action, { method: 'POST' });
    if (!ok) {
      toast(data.error || '操作失败', true);
    } else {
      const st = (data.engine || {}).state;
      toast(`引擎已${action === 'start' ? '启动' : action === 'stop' ? '停止' : '重启'}${st === 'error' ? '，但出现错误，请查看状态详情' : ''}`, st === 'error');
    }
  } catch (err) {
    // 401 已由 api() 处理
  } finally {
    state.engineBusy = false;
    refreshStatus();
  }
}

/* ---------- 配置 ---------- */

async function loadConfigOnce() {
  if (state.configLoaded) return;
  try {
    const { ok, data } = await api('/api/config');
    if (!ok) return;
    state.configLoaded = true;
    fillConfigForm(data.config);
  } catch (err) { /* 未登录时静默 */ }
}

function fillConfigForm(cfg) {
  const tokenInput = $('#cfg-token');
  tokenInput.value = '';
  tokenInput.placeholder = cfg.bot.token_set ? '已保存（留空保持不变）' : '';
  $('#hint-token').textContent = cfg.bot.token_set ? '当前 Token 已保存，留空表示不修改' : '';
  $('#cfg-admin').value = (cfg.bot.admin_id || []).join('\n');
  $('#cfg-proxy').value = cfg.bot.proxy || '';

  $('#cfg-crisp-id').value = cfg.crisp.id || '';
  const keyInput = $('#cfg-crisp-key');
  keyInput.value = '';
  keyInput.placeholder = cfg.crisp.key_set ? '已保存（留空保持不变）' : '';
  $('#hint-crisp-key').textContent = cfg.crisp.key_set ? '当前 Key 已保存，留空表示不修改' : '';
  $('#cfg-crisp-website').value = cfg.crisp.website || '';

  renderTokenPoolRows(cfg.crisp.tokens || []);

  $$('input[name="cfg-msgapi"]').forEach((el) => { el.checked = el.value === cfg.crisp.msgapi; });
  $('#cfg-poll-interval').value = cfg.crisp.poll_interval || 60;
  updatePollIntervalVisibility();

  renderAutoreplyRows(cfg.autoreply || {});
}

function renderTokenPoolRows(tokens) {
  const wrap = $('#tokenpool-rows');
  wrap.innerHTML = '';
  tokens.forEach((t) => addTokenRow(t.id || '', '', !!t.key_set));
}

function addTokenRow(id = '', key = '', keySet = false) {
  const wrap = $('#tokenpool-rows');
  const row = document.createElement('div');
  row.className = 'token-row';
  const idInput = document.createElement('input');
  idInput.placeholder = 'Identifier（UUID）';
  idInput.value = id;
  const keyInput = document.createElement('input');
  keyInput.type = 'password';
  keyInput.placeholder = keySet ? '已保存（留空保持不变）' : 'Key';
  keyInput.value = key;
  const del = document.createElement('button');
  del.type = 'button';
  del.className = 'ghost';
  del.textContent = '删除';
  del.addEventListener('click', () => row.remove());
  row.append(idInput, keyInput, del);
  wrap.appendChild(row);
}

function collectTokenPool() {
  return Array.from($$('#tokenpool-rows .token-row')).map((row) => {
    const inputs = row.querySelectorAll('input');
    return { id: inputs[0].value.trim(), key: inputs[1].value.trim() };
  });
}

function renderTokenUsage(tokens, current, limit) {
  const wrap = $('#token-usage');
  if (!tokens || tokens.length === 0) {
    wrap.textContent = '未配置令牌池（使用上方 Crisp ID/Key 单令牌）';
    return;
  }
  wrap.innerHTML = '';
  tokens.forEach((t, i) => {
    const pct = Math.min(100, Math.round((t.used / (limit || 500)) * 100));
    const row = document.createElement('div');
    row.className = 'token-usage-row';
    const head = document.createElement('div');
    head.className = 'tu-head';
    const name = document.createElement('span');
    name.textContent = t.identifier.slice(0, 8) + '…';
    const badge = document.createElement('span');
    if (i === current) {
      badge.className = 'token-badge current';
      badge.textContent = '使用中';
    } else if (t.exhausted) {
      badge.className = 'token-badge exhausted';
      badge.textContent = '已耗尽';
    }
    const used = document.createElement('span');
    used.className = 'used';
    used.textContent = `${t.used} / ${limit}（剩余 ${t.remaining}）`;
    head.append(name, badge, used);
    const bar = document.createElement('div');
    bar.className = 'usage-bar';
    const fill = document.createElement('div');
    fill.className = 'fill' + (pct >= 96 ? ' full' : (pct >= 70 ? ' warn' : ''));
    fill.style.width = pct + '%';
    bar.appendChild(fill);
    row.append(head, bar);
    wrap.appendChild(row);
  });
}

function updatePollIntervalVisibility() {
  const mode = document.querySelector('input[name="cfg-msgapi"]:checked');
  $('#field-poll-interval').style.display = (mode && mode.value === 'rest') ? '' : 'none';
}

function renderAutoreplyRows(rules) {
  const wrap = $('#autoreply-rows');
  wrap.innerHTML = '';
  Object.entries(rules).forEach(([pattern, reply]) => addAutoreplyRow(pattern, reply));
}

function addAutoreplyRow(pattern = '', reply = '') {
  const wrap = $('#autoreply-rows');
  const row = document.createElement('div');
  row.className = 'autoreply-row';
  const p = document.createElement('input');
  p.placeholder = '关键词，可用 | 分隔';
  p.value = pattern;
  const r = document.createElement('input');
  r.placeholder = '自动回复内容';
  r.value = reply;
  const del = document.createElement('button');
  del.type = 'button';
  del.className = 'ghost';
  del.textContent = '删除';
  del.addEventListener('click', () => row.remove());
  row.append(p, r, del);
  wrap.appendChild(row);
}

async function saveConfig() {
  const autoreply = {};
  $$('#autoreply-rows .autoreply-row').forEach((row) => {
    const inputs = row.querySelectorAll('input');
    const pattern = inputs[0].value.trim();
    if (pattern) autoreply[pattern] = inputs[1].value;
  });

  const body = {
    bot: {
      token: $('#cfg-token').value.trim(),
      admin_id: $('#cfg-admin').value.split('\n').map((s) => s.trim()).filter(Boolean),
      proxy: $('#cfg-proxy').value.trim(),
    },
    crisp: {
      id: $('#cfg-crisp-id').value.trim(),
      key: $('#cfg-crisp-key').value.trim(),
      website: $('#cfg-crisp-website').value.trim(),
      msgapi: document.querySelector('input[name="cfg-msgapi"]:checked').value,
      poll_interval: parseInt($('#cfg-poll-interval').value, 10) || 60,
      tokens: collectTokenPool(),
    },
    autoreply,
  };

  const btn = $('#btn-save-config');
  btn.disabled = true;
  btn.textContent = '保存中…';
  try {
    const { ok, data } = await api('/api/config', { method: 'POST', body });
    if (!ok) {
      const problems = (data.problems || []).join('；');
      toast(problems || data.error || '保存失败', true);
      return;
    }
    if (data.restart_error) {
      toast('配置已保存，但引擎重启失败：' + data.restart_error, true);
    } else if (data.engine) {
      toast('配置已保存，引擎已重启应用新配置');
    } else {
      toast('配置已保存');
    }
    fillConfigForm((await api('/api/config')).data.config);
    refreshStatus();
  } finally {
    btn.disabled = false;
    btn.textContent = '保存配置';
  }
}

/* ---------- 连通性测试 ---------- */

function renderTestResult(elId, result) {
  const el = $(elId);
  el.classList.remove('hidden', 'ok', 'fail');
  if (result.ok) {
    el.classList.add('ok');
    el.innerHTML = `✔ ${result.detail || '连接成功'}<span class="latency">延迟 ${result.latency_ms} ms</span>`;
  } else {
    el.classList.add('fail');
    el.innerHTML = `✘ ${result.error || '连接失败'}<span class="latency">耗时 ${result.latency_ms} ms</span>`;
  }
}

async function runTest(kind) {
  const btn = kind === 'crisp' ? $('#btn-test-crisp') : $('#btn-test-telegram');
  const resultEl = kind === 'crisp' ? '#result-crisp' : '#result-telegram';
  btn.disabled = true;
  const oldText = btn.textContent;
  btn.textContent = '测试中…';
  try {
    const body = kind === 'crisp' ? {
      id: $('#test-crisp-id').value.trim(),
      key: $('#test-crisp-key').value.trim(),
      website: $('#test-crisp-website').value.trim(),
    } : {
      token: $('#test-tg-token').value.trim(),
    };
    const { ok, status, data } = await api('/api/test/' + kind, { method: 'POST', body });
    if (!ok && status === 400) {
      $(resultEl).classList.remove('hidden', 'ok', 'fail');
      $(resultEl).classList.add('fail');
      $(resultEl).textContent = data.error || '请求失败';
    } else {
      renderTestResult(resultEl, data);
    }
  } finally {
    btn.disabled = false;
    btn.textContent = oldText;
  }
}

async function prefillTestForm() {
  try {
    const { data } = await api('/api/config');
    const cfg = data.config;
    $('#test-crisp-id').value = cfg.crisp.id || '';
    $('#test-crisp-website').value = cfg.crisp.website || '';
  } catch (err) { /* ignore */ }
}

/* ---------- 实时记录 ---------- */

const LEVEL_RANK = { DEBUG: 10, INFO: 20, WARNING: 30, ERROR: 40, CRITICAL: 50 };

async function pollRecords() {
  // 运行日志
  if (!$('#logtab-logs').classList.contains('hidden') && !$('#log-pause').checked) {
    try {
      const { data } = await api('/api/logs?after=' + state.logAfter);
      const minLevel = $('#log-level').value;
      const view = $('#log-view');
      const nearBottom = view.scrollHeight - view.scrollTop - view.clientHeight < 60;
      let appended = 0;
      for (const item of data.items || []) {
        state.logAfter = Math.max(state.logAfter, item.seq);
        if (minLevel !== 'ALL' && (LEVEL_RANK[item.level] || 0) < LEVEL_RANK[minLevel]) continue;
        appendLogLine(item);
        appended++;
      }
      state.logCount += appended;
      $('#log-count').textContent = `共 ${state.logCount} 条`;
      if (appended > 0 && $('#log-autoscroll').checked && nearBottom) {
        view.scrollTop = view.scrollHeight;
      }
    } catch (err) { /* ignore */ }
  }
  // 消息记录
  if (!$('#logtab-events').classList.contains('hidden') && !$('#event-pause').checked) {
    try {
      const { data } = await api('/api/events?after=' + state.eventAfter);
      const tbody = $('#events-body');
      for (const item of data.items || []) {
        state.eventAfter = Math.max(state.eventAfter, item.seq);
        state.eventCount++;
        appendEventRow(item, tbody, true);
      }
      $('#event-count').textContent = `共 ${state.eventCount} 条`;
    } catch (err) { /* ignore */ }
  }
}

function appendLogLine(item) {
  const view = $('#log-view');
  const line = document.createElement('div');
  line.className = 'log-line';
  const time = document.createElement('span');
  time.className = 't';
  time.textContent = fmtTime(item.ts) + ' ';
  const level = document.createElement('span');
  level.className = 'lv-' + item.level;
  level.textContent = `[${item.level}]`.padEnd(10, ' ');
  const src = document.createElement('span');
  src.className = 'log-src';
  src.textContent = ` ${item.source}: `;
  const msg = document.createElement('span');
  msg.textContent = item.message;
  line.append(time, level, src, msg);
  view.appendChild(line);
  while (view.childElementCount > 2000) view.removeChild(view.firstChild);
}

const EVENT_KIND = {
  message_in: ['Crisp → TG', 'badge-in'],
  message_out: ['TG → Crisp', 'badge-out'],
  autoreply: ['自动回复', 'badge-auto'],
  error: ['错误', 'badge-error'],
  system: ['系统', 'badge-system'],
};

function appendEventRow(item, tbody, prepend) {
  const tr = document.createElement('tr');
  const tdTime = document.createElement('td');
  tdTime.textContent = fmtFull(item.ts);
  const [label, badgeClass] = EVENT_KIND[item.kind] || [item.kind, 'badge-system'];
  const tdKind = document.createElement('td');
  const badge = document.createElement('span');
  badge.className = 'badge ' + badgeClass;
  badge.textContent = label;
  tdKind.appendChild(badge);
  const tdSession = document.createElement('td');
  const sess = item.session_id || '';
  tdSession.className = 'evt-session';
  tdSession.textContent = sess;
  tdSession.title = sess;
  const tdEmail = document.createElement('td');
  const email = item.email || '';
  tdEmail.className = 'evt-email';
  tdEmail.textContent = email || '—';
  tdEmail.title = email;
  const tdContent = document.createElement('td');
  tdContent.className = 'evt-content';
  tdContent.textContent = item.message || '';
  tr.append(tdTime, tdKind, tdSession, tdEmail, tdContent);
  if (prepend && tbody.firstChild) {
    tbody.insertBefore(tr, tbody.firstChild);
  } else {
    tbody.appendChild(tr);
  }
  while (tbody.childElementCount > 2000) tbody.removeChild(tbody.lastChild);
}

function resetLogView() {
  state.logAfter = 0;
  state.logCount = 0;
  $('#log-view').innerHTML = '';
  $('#log-count').textContent = '';
}

/* ---------- 视图切换 ---------- */

const VIEW_TITLES = { dashboard: '状态总览', config: '参数配置', test: '连通性测试', logs: '实时记录' };

function switchView(view) {
  state.view = view;
  $$('.nav-item').forEach((el) => el.classList.toggle('active', el.dataset.view === view));
  $$('.view').forEach((el) => el.classList.add('hidden'));
  $('#view-' + view).classList.remove('hidden');
  $('#view-title').textContent = VIEW_TITLES[view] || view;
  if (view === 'test') prefillTestForm();
}

/* ---------- 初始化 ---------- */

function bindEvents() {
  $('#auth-submit').addEventListener('click', submitAuth);
  $('#auth-password').addEventListener('keydown', (e) => { if (e.key === 'Enter') submitAuth(); });
  $('#auth-password2').addEventListener('keydown', (e) => { if (e.key === 'Enter') submitAuth(); });

  $$('.nav-item').forEach((el) => el.addEventListener('click', () => switchView(el.dataset.view)));

  $('#btn-start').addEventListener('click', () => engineAction('start'));
  $('#btn-stop').addEventListener('click', () => engineAction('stop'));
  $('#btn-restart').addEventListener('click', () => engineAction('restart'));

  $('#logout-btn').addEventListener('click', async () => {
    await api('/api/auth/logout', { method: 'POST' }).catch(() => {});
    showAuth('login');
  });

  $$('input[name="cfg-msgapi"]').forEach((el) => el.addEventListener('change', updatePollIntervalVisibility));
  $('#btn-add-token').addEventListener('click', () => addTokenRow());
  $('#btn-add-autoreply').addEventListener('click', () => addAutoreplyRow());
  $('#btn-save-config').addEventListener('click', saveConfig);

  $('#btn-test-crisp').addEventListener('click', () => runTest('crisp'));
  $('#btn-test-telegram').addEventListener('click', () => runTest('telegram'));

  $$('.subtab').forEach((el) => el.addEventListener('click', () => {
    $$('.subtab').forEach((s) => s.classList.toggle('active', s === el));
    $('#logtab-logs').classList.toggle('hidden', el.dataset.logtab !== 'logs');
    $('#logtab-events').classList.toggle('hidden', el.dataset.logtab !== 'events');
  }));

  $('#log-level').addEventListener('change', resetLogView);
  $('#btn-clear-events').addEventListener('click', () => {
    $('#events-body').innerHTML = '';
    state.eventCount = 0;
    $('#event-count').textContent = '';
  });
}

async function init() {
  bindEvents();
  try {
    const { data } = await api('/api/auth/state');
    if (data.setup_required) showAuth('setup');
    else if (data.authed) enterApp();
    else showAuth('login');
  } catch (err) {
    showAuth('login');
    $('#auth-error').textContent = '无法连接控制台服务，请确认服务已启动';
  }
}

init();
