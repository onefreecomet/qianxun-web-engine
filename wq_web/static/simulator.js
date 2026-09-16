// Alpha Simulator 页面逻辑

const $ = (id) => document.getElementById(id);
const fmtNum = (v, d = 2) => v == null ? '—' : Number(v).toFixed(d);
const fmtPct = (v, d = 2) => v == null ? '—' : (Number(v) * 100).toFixed(d) + '%';
// Margin 官网按万分率（‱ = bps）显示：0.001985 → 19.85‱
const fmtBps = (v, d = 2) => v == null ? '—' : (Number(v) * 10000).toFixed(d) + '‱';

function toast(msg, type = '') {
  const t = $('toast');
  t.textContent = msg;
  t.className = 'toast show ' + type;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => t.classList.remove('show'), 3500);
}

async function api(path, opts = {}) {
  const r = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({ detail: r.statusText }));
    throw new Error(err.detail || err.error || r.statusText);
  }
  return r.json();
}

// ===== 状态 =====
const state = {
  tabs: [{ id: 0, alphaId: null, data: null }],
  activeTab: 0,
  nextId: 1,
};

function getActiveTab() {
  return state.tabs.find(t => t.id === state.activeTab);
}

// ===== 加载 Alpha =====
async function loadAlpha(alphaId) {
  if (!alphaId) {
    alphaId = prompt('输入 Alpha ID（如 Xg7NRLgm）：');
    if (!alphaId) return;
  }
  alphaId = alphaId.trim();
  const tab = getActiveTab();
  tab.alphaId = alphaId;
  updateTabTitle(tab);
  setLoading(true);

  try {
    const r = await api('/api/simulator/alpha/' + alphaId);
    tab.data = r;
    renderAlpha(r);
    toast(`已加载 ${alphaId}`, 'success');
  } catch (e) {
    toast('加载失败：' + e.message, 'error');
    clearPanel();
  } finally {
    setLoading(false);
  }
}

function setLoading(on) {
  $('exprStatus').textContent = on ? '加载中…' : (getActiveTab()?.alphaId ? '已加载 ' + getActiveTab().alphaId : '等待加载…');
  if (on) $('exprStatus').classList.add('sim-loading');
  else $('exprStatus').classList.remove('sim-loading');
}

function updateTabTitle(tab) {
  const el = document.querySelector(`.sim-tab[data-tab="${tab.id}"] .sim-tab-title`);
  if (el) el.textContent = tab.alphaId ? `Sim ${tab.id + 1}: ${tab.alphaId}` : `Simulate ${tab.id + 1}`;
}

function clearPanel() {
  $('alphaExpression').value = '';
  $('pnlRange').textContent = '—';
  $('pnlCount').textContent = '—';
  $('yearlyBody').innerHTML = '<tr><td colspan="9" class="muted" style="text-align:center;padding:20px">无数据</td></tr>';
  clearMetrics();
  drawEmptyChart();
}

function clearMetrics() {
  ['mSharpe', 'mTurnover', 'mFitness', 'mReturns', 'mDrawdown', 'mMargin'].forEach(id => {
    $(id).textContent = '—';
    $(id).parentElement.classList.remove('pos', 'neg');
  });
  $('regionGrid').innerHTML = '';
}

// ===== 渲染 =====
function renderAlpha(r) {
  const alpha = r.alpha || {};
  const pnl = r.pnl || [];
  const yearly = r.yearly || [];
  const classifications = r.classifications || [];
  const checks = r.checks || [];

  // 表达式
  $('alphaExpression').value = alpha.expression || '';

  // settings
  const settings = alpha.settings_json ? JSON.parse(alpha.settings_json) : {};
  populateSettings({ ...settings, ...alpha });

  // 指标（优先用平台官方 IS 值，本地库缺 drawdown 等列）
  renderMetrics(alpha, r.is_metrics || {});

  // 真实分类
  renderClassifications(classifications);

  // Checks 摘要
  renderChecksSummary(checks);

  // PnL 图
  drawPnlChart(pnl);

  // 年表
  renderYearly(yearly);

  // 更新 footer
  $('fCheck').textContent = alpha.check_status || '—';
}

function renderClassifications(list) {
  const el = $('classificationsList');
  if (!list || list.length === 0) {
    el.innerHTML = '<span class="muted">未分类</span>';
    return;
  }
  el.innerHTML = list.map(c => {
    const id = (c.id || '').toLowerCase();
    let cls = '';
    if (id.includes('power_pool')) cls = 'power-pool';
    else if (id.includes('cluster')) cls = 'cluster';
    return `<span class="sim-class-tag ${cls}">${escapeHtml(c.name)}</span>`;
  }).join('');
}

function renderChecksSummary(checks) {
  if (!checks || checks.length === 0) return;
  const fails = checks.filter(c => c.result === 'FAIL').map(c => c.name);
  const warns = checks.filter(c => c.result === 'WARNING').map(c => c.name);
  const pending = checks.filter(c => c.result === 'PENDING').map(c => c.name);
  const parts = [];
  if (fails.length) parts.push(`${fails.length} 项 FAIL`);
  if (warns.length) parts.push(`${warns.length} 项 WARNING`);
  if (pending.length) parts.push(`${pending.length} 项 PENDING`);
  if (parts.length) {
    $('exprStatus').innerHTML = '<span class="muted">Checks：' + parts.join(' / ') + '</span>';
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;','\'':'&#39;'}[m]));
}

function populateSettings(s) {
  const setIf = (id, val) => {
    const el = $(id);
    if (el && val != null) el.value = val;
  };
  setIf('setRegion', s.region);
  setIf('setUniverse', s.universe);
  setIf('setDelay', s.delay);
  setIf('setDecay', s.decay != null ? s.decay : s.decay_value);
  setIf('setNeutral', s.neutralization);
  setIf('setTruncation', s.truncation);
  setIf('setPasteur', s.pasteurization);
  setIf('setNan', s.nanHandling);
  // maxTrade / maxPosition 平台可能返回 "OFF"
  const mt = s.maxTrade;
  setIf('setMaxTrade', (mt === 'OFF' || mt === 'ON' || mt === true || mt === false || (typeof mt === 'number' && !isNaN(mt))) ? String(mt) : (mt || 'OFF'));
  const mp = s.maxPosition;
  setIf('setMaxPos', (mp === 'OFF' || mp === 'ON' || mp === true || mp === false || (typeof mp === 'number' && !isNaN(mp))) ? String(mp) : (mp || 'OFF'));
  setIf('setLang', s.language);
  setIf('setLookback', s.lookback);
}

function renderMetrics(alpha, ism = {}) {
  const pick = (k, alt) => (ism[k] != null ? ism[k] : alt);
  const m = {
    sharpe: pick('sharpe', alpha.sharpe),
    turnover: pick('turnover', alpha.turnover),
    fitness: pick('fitness', alpha.fitness),
    returns: pick('returns', alpha.returns),
    drawdown: pick('drawdown', alpha.drawdown),
    margin: pick('margin', alpha.margin),
  };

  const paint = (id, text, val) => {
    const el = $(id);
    const cell = el.parentElement;
    el.textContent = text;
    cell.classList.remove('pos', 'neg');
    if (val != null && Number(val) > 0) cell.classList.add('pos');
    if (val != null && Number(val) < 0) cell.classList.add('neg');
  };
  paint('mSharpe', fmtNum(m.sharpe), m.sharpe);
  paint('mTurnover', fmtPct(m.turnover), m.turnover);
  paint('mFitness', fmtNum(m.fitness), m.fitness);
  paint('mReturns', fmtPct(m.returns), m.returns);
  paint('mDrawdown', fmtPct(m.drawdown), m.drawdown);
  paint('mMargin', fmtBps(m.margin), m.margin);

  // region 行（本地单 alpha 只展示它自己的 region）
  const rg = $('regionGrid');
  rg.innerHTML = `
    <div class="sim-region-row">
      <span class="sim-region-name">${alpha.region || '—'}</span>
      <div class="sim-region-cell"><label>Sharpe</label><value>${fmtNum(m.sharpe)}</value></div>
      <div class="sim-region-cell"><label>Turnover</label><value>${fmtPct(m.turnover)}</value></div>
      <div class="sim-region-cell"><label>Fitness</label><value>${fmtNum(m.fitness)}</value></div>
      <div class="sim-region-cell"><label>Returns</label><value>${fmtPct(m.returns)}</value></div>
      <div class="sim-region-cell"><label>Drawdown</label><value>${fmtPct(m.drawdown)}</value></div>
      <div class="sim-region-cell"><label>Margin</label><value>${fmtBps(m.margin)}</value></div>
    </div>
  `;
}

function renderYearly(yearly) {
  const tb = $('yearlyBody');
  if (!yearly || yearly.length === 0) {
    tb.innerHTML = '<tr><td colspan="9" class="muted" style="text-align:center;padding:20px">暂无逐年数据，点「拉取 PnL」同步</td></tr>';
    return;
  }
  // 官方字段：turnover / returns / drawdown 为比率，margin 为万分率，fitness 为数值
  tb.innerHTML = yearly.map(y => {
    const longC = y.longCount != null ? y.longCount : (y.long != null ? y.long : null);
    const shortC = y.shortCount != null ? y.shortCount : (y.short != null ? y.short : null);
    return `
    <tr>
      <td>${y.year ?? '—'}</td>
      <td>${fmtNum(y.sharpe)}</td>
      <td>${fmtNum(y.fitness)}</td>
      <td>${fmtPct(y.turnover)}</td>
      <td>${fmtPct(y.returns)}</td>
      <td>${fmtPct(y.drawdown)}</td>
      <td>${fmtBps(y.margin)}</td>
      <td>${longC ?? '—'}</td>
      <td>${shortC ?? '—'}</td>
    </tr>`;
  }).join('');
}

// ===== PnL 图 =====
function drawEmptyChart() {
  $('pnlCount').textContent = '—';
  const canvas = $('pnlCanvas');
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = Math.max(1, Math.floor(rect.width * dpr));
  canvas.height = Math.max(1, Math.floor(rect.height * dpr));
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, rect.width, rect.height);
  ctx.fillStyle = getComputedStyle(document.body).getPropertyValue('--text-mute');
  ctx.font = '13px var(--mono)';
  ctx.textAlign = 'center';
  ctx.fillText('暂无 PnL 数据', rect.width / 2, rect.height / 2);
}

function drawPnlChart(records) {
  const canvas = $('pnlCanvas');
  if (!records || records.length === 0) {
    drawEmptyChart();
    return;
  }

  const rows = records.map(r => ({
    date: String(r.date || r.Date || ''),
    pnl: Number(r.pnl != null ? r.pnl : r.value),
  })).filter(r => !isNaN(r.pnl)).sort((a, b) => a.date.localeCompare(b.date));

  if (rows.length === 0) {
    drawEmptyChart();
    return;
  }

  // 累积 PnL
  let cum = 0;
  const data = rows.map(r => {
    cum += r.pnl;
    return { date: r.date, cum };
  });

  $('pnlRange').textContent = `${data[0].date} → ${data[data.length - 1].date}`;
  $('pnlCount').textContent = `${data.length} 天`;

  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = Math.max(1, Math.floor(rect.width * dpr));
  canvas.height = Math.max(1, Math.floor(rect.height * dpr));
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);

  const w = rect.width, h = rect.height;
  const pad = { top: 20, right: 20, bottom: 32, left: 48 };
  const gw = w - pad.left - pad.right;
  const gh = h - pad.top - pad.bottom;

  ctx.clearRect(0, 0, w, h);

  const minV = Math.min(...data.map(d => d.cum));
  const maxV = Math.max(...data.map(d => d.cum));
  const range = maxV - minV || 1;
  const xFor = (i) => pad.left + (i / (data.length - 1)) * gw;
  const yFor = (v) => pad.top + gh - ((v - minV) / range) * gh;

  // 网格（虚线，弱化）
  ctx.strokeStyle = getComputedStyle(document.body).getPropertyValue('--border');
  ctx.lineWidth = 1;
  ctx.setLineDash([3, 5]);
  ctx.beginPath();
  for (let i = 0; i <= 4; i++) {
    const y = pad.top + (gh / 4) * i;
    ctx.moveTo(pad.left, y);
    ctx.lineTo(pad.left + gw, y);
  }
  ctx.stroke();
  ctx.setLineDash([]);

  // 区域渐变
  const grad = ctx.createLinearGradient(0, pad.top, 0, pad.top + gh);
  grad.addColorStop(0, 'rgba(34,211,238,0.25)');
  grad.addColorStop(1, 'rgba(34,211,238,0.0)');
  ctx.beginPath();
  ctx.moveTo(xFor(0), yFor(data[0].cum));
  for (let i = 1; i < data.length; i++) ctx.lineTo(xFor(i), yFor(data[i].cum));
  ctx.lineTo(xFor(data.length - 1), pad.top + gh);
  ctx.lineTo(xFor(0), pad.top + gh);
  ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();

  // 曲线（信号辉光）
  ctx.save();
  ctx.beginPath();
  ctx.moveTo(xFor(0), yFor(data[0].cum));
  for (let i = 1; i < data.length; i++) ctx.lineTo(xFor(i), yFor(data[i].cum));
  ctx.strokeStyle = '#22d3ee';
  ctx.lineWidth = 2;
  ctx.shadowColor = 'rgba(34,211,238,0.55)';
  ctx.shadowBlur = 10;
  ctx.stroke();
  ctx.restore();

  // 末端标记点
  const lastIdx = data.length - 1;
  ctx.beginPath();
  ctx.arc(xFor(lastIdx), yFor(data[lastIdx].cum), 3, 0, Math.PI * 2);
  ctx.fillStyle = '#22d3ee';
  ctx.fill();

  // 坐标轴标签
  ctx.fillStyle = getComputedStyle(document.body).getPropertyValue('--text-mute');
  ctx.font = '11px var(--mono)';
  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';
  for (let i = 0; i <= 4; i++) {
    const v = minV + (range / 4) * (4 - i);
    ctx.fillText(v.toFixed(2), pad.left - 8, pad.top + (gh / 4) * i);
  }

  // X 轴年份
  ctx.textAlign = 'center';
  ctx.textBaseline = 'top';
  const yearSet = new Set();
  data.forEach((d, i) => {
    const y = d.date.slice(0, 4);
    if (!yearSet.has(y)) {
      yearSet.add(y);
      ctx.fillText(y, xFor(i), pad.top + gh + 8);
    }
  });
}

// ===== 标签页 =====
function renderTabs() {
  const container = $('simTabs');
  const addBtn = $('btnAddTab');
  container.innerHTML = '';
  state.tabs.forEach(tab => {
    const div = document.createElement('div');
    div.className = 'sim-tab' + (tab.id === state.activeTab ? ' active' : '');
    div.dataset.tab = tab.id;
    div.innerHTML = `
      <span class="sim-tab-dot"></span>
      <span class="sim-tab-title">${tab.alphaId ? `Sim ${tab.id + 1}: ${tab.alphaId}` : `Simulate ${tab.id + 1}`}</span>
      <button class="sim-tab-close" title="关闭">×</button>
    `;
    div.querySelector('.sim-tab-title').addEventListener('click', () => switchTab(tab.id));
    div.querySelector('.sim-tab-dot').addEventListener('click', () => switchTab(tab.id));
    div.querySelector('.sim-tab-close').addEventListener('click', (e) => { e.stopPropagation(); closeTab(tab.id); });
    container.appendChild(div);
  });
  container.appendChild(addBtn);
  $('fTabs').textContent = state.tabs.length;
}

function switchTab(id) {
  state.activeTab = id;
  renderTabs();
  const tab = getActiveTab();
  if (tab && tab.data) renderAlpha(tab.data);
  else clearPanel();
  if (tab && tab.alphaId) updateTabTitle(tab);
}

function addTab() {
  const id = state.nextId++;
  state.tabs.push({ id, alphaId: null, data: null });
  switchTab(id);
}

function closeTab(id) {
  if (state.tabs.length <= 1) return;
  const idx = state.tabs.findIndex(t => t.id === id);
  state.tabs.splice(idx, 1);
  if (state.activeTab === id) {
    const next = state.tabs[Math.max(0, idx - 1)];
    switchTab(next.id);
  } else {
    renderTabs();
  }
}

// ===== 事件绑定 =====
$('btnLoadAlpha').addEventListener('click', () => loadAlpha());

$('btnAddTab').addEventListener('click', addTab);

$('btnCopyExpr').addEventListener('click', async () => {
  const text = $('alphaExpression').value;
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    toast('表达式已复制', 'success');
  } catch (e) { toast('复制失败', 'error'); }
});

$('btnImportExpr').addEventListener('click', async () => {
  const text = prompt('粘贴表达式：');
  if (text == null) return;
  $('alphaExpression').value = text.trim();
  toast('表达式已导入', 'success');
});

$('btnFetchPnl').addEventListener('click', async () => {
  const tab = getActiveTab();
  if (!tab || !tab.alphaId) {
    toast('请先加载一个 alpha', '');
    return;
  }
  setLoading(true);
  try {
    const r = await api(`/api/simulator/alpha/${tab.alphaId}/pnl`, { method: 'POST' });
    if (tab.data) {
      tab.data.pnl = r.pnl;
      tab.data.yearly = r.yearly;
    }
    drawPnlChart(r.pnl);
    renderYearly(r.yearly);
    toast(`已拉取 ${r.count} 条 PnL 记录`, 'success');
    refreshGlobalMetrics();
  } catch (e) {
    toast('拉取 PnL 失败：' + e.message, 'error');
  } finally {
    setLoading(false);
  }
});

$('btnCopySettings').addEventListener('click', async () => {
  const s = collectSettings();
  try {
    await navigator.clipboard.writeText(JSON.stringify(s, null, 2));
    toast('Settings 已复制', 'success');
  } catch (e) { toast('复制失败', 'error'); }
});

$('btnImportSettings').addEventListener('click', async () => {
  const raw = prompt('粘贴 settings JSON：');
  if (!raw) return;
  try {
    const s = JSON.parse(raw);
    populateSettings(s);
    toast('Settings 已导入', 'success');
  } catch (e) { toast('JSON 解析失败', 'error'); }
});

function collectSettings() {
  return {
    region: $('setRegion').value,
    universe: $('setUniverse').value,
    delay: parseInt($('setDelay').value, 10),
    decay: parseInt($('setDecay').value, 10),
    neutralization: $('setNeutral').value,
    truncation: parseFloat($('setTruncation').value),
    pasteurization: $('setPasteur').value,
    nanHandling: $('setNan').value,
    maxTrade: parseFloat($('setMaxTrade').value) || $('setMaxTrade').value || 'OFF',
    maxPosition: parseFloat($('setMaxPos').value) || $('setMaxPos').value || 'OFF',
    language: $('setLang').value,
    lookback: parseInt($('setLookback').value, 10) || 0,
  };
}

$('btnCancelAll').addEventListener('click', () => {
  toast('Simulator 为只读展示，无运行中任务', '');
});

window.addEventListener('resize', () => {
  const tab = getActiveTab();
  if (tab && tab.data) drawPnlChart(tab.data.pnl);
  else drawEmptyChart();
});

// 顶部平台指标：Today Simulated/Submitted（本地库）+ Osmosis Rank/VF（BRAIN 实时）
async function refreshGlobalMetrics() {
  try {
    const r = await api('/api/simulator/platform-stats');
    $('gTodaySim').textContent = r.today_simulated ?? 0;
    $('gTodaySub').textContent = r.today_submitted ?? 0;
    $('gOsmosisRank').textContent = r.osmosis_rank == null ? '—' : Number(r.osmosis_rank).toFixed(2);
    $('gVF').textContent = r.vf == null ? '—' : Number(r.vf).toFixed(2);
    $('gCommunity').textContent = r.community == null ? '—' : Number(r.community).toFixed(2);
    $('gSignals').textContent = r.signals == null ? '—' : r.signals;
    $('gGacRank').textContent = r.gac_rank == null ? '—' : r.gac_rank;
  } catch (e) {
    // 静默：顶部装饰数字失败不打扰
  }
}

// 初始化
renderTabs();
drawEmptyChart();
refreshGlobalMetrics();
setInterval(refreshGlobalMetrics, 30000);

// 如果 URL 带 ?alpha=XXX，自动加载
const urlParams = new URLSearchParams(location.search);
const initialAlpha = urlParams.get('alpha');
if (initialAlpha) loadAlpha(initialAlpha);

// =====================================================================
// 日渗透分趋势弹窗（v82）
// 入口：顶部 Osmosis Rank 指标卡
// 数据：/api/osmosis/history（后端按天快照；BRAIN 只有当日值，无历史接口）
// =====================================================================

const OSM_NS = 'http://www.w3.org/2000/svg';
const osmModal = $('osmModal');

function osmEl(tag, attrs = {}, text = null) {
  const n = document.createElementNS(OSM_NS, tag);
  for (const k in attrs) n.setAttribute(k, String(attrs[k]));
  if (text != null) n.textContent = text;
  return n;
}

const osmFmt = (v) => (v == null || !Number.isFinite(Number(v)) ? '—' : Number(v).toFixed(2));

/** 取「好看」的刻度步长：1 / 2 / 2.5 / 5 / 10 × 10^k。 */
function osmNiceStep(range, ticks) {
  const raw = range / Math.max(1, ticks);
  if (!(raw > 0)) return 1;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  // 必须 round：0.6/6 会得到 1.0000000000000002，不修就会跳到下一档，
  // 刻度从 0.1 变成 0.2（少一半网格线）。
  const norm = Math.round((raw / mag) * 1e6) / 1e6;
  const step = norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10;
  return step * mag;
}

/** Catmull-Rom → 三次贝塞尔，把折线变成平滑曲线。 */
function osmSmoothPath(xs, ys, tension = 0.75) {
  let d = `M ${xs[0].toFixed(2)} ${ys[0].toFixed(2)}`;
  for (let i = 0; i < xs.length - 1; i++) {
    const p0x = xs[i - 1] ?? xs[i], p0y = ys[i - 1] ?? ys[i];
    const p1x = xs[i], p1y = ys[i];
    const p2x = xs[i + 1], p2y = ys[i + 1];
    const p3x = xs[i + 2] ?? xs[i + 1], p3y = ys[i + 2] ?? ys[i + 1];
    const c1x = p1x + ((p2x - p0x) / 6) * tension;
    const c1y = p1y + ((p2y - p0y) / 6) * tension;
    const c2x = p2x - ((p3x - p1x) / 6) * tension;
    const c2y = p2y - ((p3y - p1y) / 6) * tension;
    d += ` C ${c1x.toFixed(2)} ${c1y.toFixed(2)},`
      + ` ${c2x.toFixed(2)} ${c2y.toFixed(2)},`
      + ` ${p2x.toFixed(2)} ${p2y.toFixed(2)}`;
  }
  return d;
}

function renderOsmosisChart(points) {
  const svg = $('osmChart');
  if (!svg) return;
  svg.textContent = '';

  const W = 760, H = 400;
  const padL = 58, padR = 30, padT = 22, padB = 50;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;
  const baseY = padT + plotH;

  // ---- defs：面积渐变 / 线条渐变 / 辉光 ----
  const defs = osmEl('defs');
  const areaGrad = osmEl('linearGradient', { id: 'osmAreaGrad', x1: '0', y1: '0', x2: '0', y2: '1' });
  areaGrad.appendChild(osmEl('stop', { offset: '0%', 'stop-color': '#3B82F6', 'stop-opacity': '0.55' }));
  areaGrad.appendChild(osmEl('stop', { offset: '55%', 'stop-color': '#2563EB', 'stop-opacity': '0.17' }));
  areaGrad.appendChild(osmEl('stop', { offset: '100%', 'stop-color': '#1D4ED8', 'stop-opacity': '0.02' }));
  defs.appendChild(areaGrad);

  const lineGrad = osmEl('linearGradient', { id: 'osmLineGrad', x1: '0', y1: '0', x2: '1', y2: '0' });
  lineGrad.appendChild(osmEl('stop', { offset: '0%', 'stop-color': '#60A5FA' }));
  lineGrad.appendChild(osmEl('stop', { offset: '100%', 'stop-color': '#38BDF8' }));
  defs.appendChild(lineGrad);

  const glow = osmEl('filter', { id: 'osmLineGlow', x: '-20%', y: '-80%', width: '140%', height: '300%' });
  glow.appendChild(osmEl('feGaussianBlur', { stdDeviation: '4', result: 'osmBlur' }));
  const merge = osmEl('feMerge');
  merge.appendChild(osmEl('feMergeNode', { in: 'osmBlur' }));
  merge.appendChild(osmEl('feMergeNode', { in: 'SourceGraphic' }));
  glow.appendChild(merge);
  defs.appendChild(glow);
  svg.appendChild(defs);

  if (!points.length) {
    svg.appendChild(osmEl('text', { x: W / 2, y: H / 2, class: 'osm-empty-text' }, '暂无记录'));
    return;
  }

  // ---- 值域与刻度 ----
  // 渗透分（dailyOsmosisRank）天然落在 0~1。只有 1~2 个快照时按极差取值域会缩成
  // 一条线，所以给一个最小跨度；再吸附到刻度倍数上，并夹回 [0,1]。
  const MIN_SPAN = 0.2;
  // 吸附必须去浮点噪声：0.8/0.1 = 8.000000000000002，直接 ceil 会多出一格 0.9
  const snap = (v) => Math.round(v * 1e6) / 1e6;
  const ranks = points.map((p) => p.rank);
  const rawLo = Math.min(...ranks);
  const rawHi = Math.max(...ranks);
  const mid = (rawLo + rawHi) / 2;
  const span = Math.max(rawHi - rawLo, MIN_SPAN);
  const step = osmNiceStep(span, 6);
  let lo = Math.floor(snap((mid - span / 2) / step)) * step;
  let hi = Math.ceil(snap((mid + span / 2) / step)) * step;
  if (lo < 0) lo = 0;
  if (hi > 1) hi = 1;
  if (hi - lo < step) hi = Math.min(1, lo + step);

  const xAt = (i) => (points.length === 1
    ? padL + plotW / 2
    : padL + (plotW * i) / (points.length - 1));
  const yAt = (v) => padT + plotH * (1 - (v - lo) / (hi - lo));

  // ---- 横向网格 + y 轴刻度 ----
  const ticks = [];
  for (let v = lo; v <= hi + step / 2; v += step) ticks.push(Number(v.toFixed(6)));
  ticks.forEach((v) => {
    const y = yAt(v);
    svg.appendChild(osmEl('line', {
      x1: padL, y1: y, x2: padL + plotW, y2: y, class: 'osm-grid-line',
    }));
    svg.appendChild(osmEl('text', {
      x: padL - 12, y: y + 4, 'text-anchor': 'end', class: 'osm-axis-text',
    }, v.toFixed(2)));
  });

  // ---- 纵向网格 + x 轴日期（点多时抽稀） ----
  const labelEvery = Math.max(1, Math.ceil(points.length / 8));
  points.forEach((p, i) => {
    const x = xAt(i);
    svg.appendChild(osmEl('line', {
      x1: x, y1: padT, x2: x, y2: baseY, class: 'osm-grid-line', opacity: '0.5',
    }));
    if (i % labelEvery === 0 || i === points.length - 1) {
      svg.appendChild(osmEl('text', {
        x, y: baseY + 26, 'text-anchor': 'middle',
        class: 'osm-axis-text' + (i === points.length - 1 ? ' is-latest' : ''),
      }, String(p.day).slice(5).replace('-', '/')));
    }
  });

  // ---- 面积 + 平滑折线（单点退化为纯数据点） ----
  const xs = points.map((_, i) => xAt(i));
  const ys = points.map((p) => yAt(p.rank));
  if (points.length > 1) {
    const lineD = osmSmoothPath(xs, ys);
    svg.appendChild(osmEl('path', {
      d: `${lineD} L ${xs[xs.length - 1].toFixed(2)} ${baseY}`
        + ` L ${xs[0].toFixed(2)} ${baseY} Z`,
      fill: 'url(#osmAreaGrad)',
    }));
    svg.appendChild(osmEl('path', { d: lineD, class: 'osm-line', stroke: 'url(#osmLineGrad)' }));
  }

  // ---- 数据点（白心 + 蓝环 + 光晕，hover 出原生 tooltip） ----
  points.forEach((p, i) => {
    const x = xs[i], y = ys[i];
    const g = osmEl('g');
    g.appendChild(osmEl('circle', { cx: x, cy: y, r: 9.5, class: 'osm-dot-halo' }));
    g.appendChild(osmEl('circle', { cx: x, cy: y, r: 6.2, class: 'osm-dot-ring' }));
    g.appendChild(osmEl('circle', { cx: x, cy: y, r: 4.2, class: 'osm-dot-core' }));
    g.appendChild(osmEl('title', {}, `${p.day}　渗透分 ${Number(p.rank).toFixed(2)}`));
    svg.appendChild(g);
  });
}

function updateOsmosisStats(points, meta = {}) {
  const ranks = points.map((p) => p.rank);
  const latest = points[points.length - 1];
  $('osmLatest').textContent = latest ? osmFmt(latest.rank) : '—';
  $('osmAvg').textContent = ranks.length
    ? osmFmt(ranks.reduce((a, b) => a + b, 0) / ranks.length) : '—';
  $('osmMax').textContent = ranks.length ? osmFmt(Math.max(...ranks)) : '—';
  $('osmDays').textContent = String(points.length);

  const sub = $('osmModalSub');
  if (!points.length) {
    sub.textContent = '还没有任何日快照';
  } else if (points.length === 1) {
    sub.textContent = `${latest.day}　首个快照，明日起自动累积趋势`;
  } else {
    sub.textContent = `${points[0].day} → ${latest.day}　共 ${points.length} 个日快照`;
  }
  if (meta && meta.today && latest && latest.day < meta.today) {
    sub.textContent += `（今日 ${meta.today} 尚未记录）`;
  }
}

async function openOsmosisModal() {
  if (!osmModal) return;
  osmModal.hidden = false;
  $('osmModalSub').textContent = '正在读取本机记录…';
  $('osmChart').textContent = '';
  const card = osmModal.querySelector('.osm-modal-card');
  if (card) card.scrollTop = 0;
  try {
    const r = await api('/api/osmosis/history?days=180');
    const pts = (r.points || [])
      .map((p) => ({ day: p.day, rank: Number(p.rank) }))
      .filter((p) => Number.isFinite(p.rank));
    renderOsmosisChart(pts);
    updateOsmosisStats(pts, r);
  } catch (e) {
    $('osmModalSub').textContent = '读取失败：' + (e.message || e);
    renderOsmosisChart([]);
    updateOsmosisStats([], {});
  }
}

function closeOsmosisModal() {
  if (osmModal) osmModal.hidden = true;
}

// 支持 ?osm=1 直达：进页面就弹趋势图（方便收藏链接 / 无人值守截图）
if (urlParams.get('osm')) openOsmosisModal();

(() => {
  const osmCard = $('osmosisCard');
  if (osmCard) {
    osmCard.addEventListener('click', openOsmosisModal);
    osmCard.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        openOsmosisModal();
      }
    });
  }
  if (!osmModal) return;
  const closeBtn = $('osmModalClose');
  if (closeBtn) closeBtn.addEventListener('click', closeOsmosisModal);
  const backdrop = osmModal.querySelector('[data-osm-close]');
  if (backdrop) backdrop.addEventListener('click', closeOsmosisModal);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !osmModal.hidden) closeOsmosisModal();
  });
})();
