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
    $('gTodaySub').textContent = r.pyramids_completed == null ? '—' : r.pyramids_completed;
    $('gOsmosisRank').textContent = r.osmosis_rank == null ? '—' : Number(r.osmosis_rank).toFixed(2);
    $('gVF').textContent = r.vf == null ? '—' : Number(r.vf).toFixed(2);
    $('gCommunity').textContent = r.total_payment == null ? '—' : '$' + Number(r.total_payment).toFixed(2);
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

// =====================================================================
// 已提交 Alpha 清单弹窗（v83）
// 入口：顶部 Signals 指标卡
// 数据：/api/simulator/submitted-alphas（BRAIN /users/self/alphas，状态非 UNSUBMITTED/IS_FAIL）
// 口径说明：Signals 卡片显示 submissionsCount（平台聚合值，口径未公开），
//          弹窗显示逐条 alpha 明细。两者可能不一致，故同时在弹窗里标注。
// =====================================================================

const SIG_PAGE_SIZE = 50;

const sigState = {
  loaded: false,     // 明细是否已拉到（本次会话）
  consultantCount: null,  // 顶部卡片的 submissionsCount（80），来自 platform-stats
  loading: false,
  items: [],
  byRegion: {},
  byStatus: {},
  platformCount: null,
  count: null,
  region: '',        // 区域筛选（'' = 全部）
  keyword: '',
  sort: 'dateSubmitted',
  page: 1,
  error: null,
};

const sigModal = $('sigModal');

/** 提交日：按「美东时间」出 YYYY-MM-DD，与 BRAIN 平台显示一致。
 *
 *  ⚠️ 不能直接用本地时区取日期。BRAIN 返回的 ISO 带美东偏移
 *  （如 2026-09-20T21:33:42-04:00），若用 d.getDate() 取「本地（北京 UTC+8）」
 *  日期，则美东 12:00 之后提交的 alpha 日期会整体 +1 天
 *  （美东 21:33 = 北京次日 09:33）——实测 150 条里近半错位。
 *  必须显式按 America/New_York 归日；Intl 会自动处理 EST/EDT 切换，
 *  对 -04:00 / -05:00 / Z 三种输入都能得出正确的美东日期。
 *  （后端 arc/runner.py 用 (iso)[:10] 取同一天，因为偏移量就在字符串里。） */
const SIG_DAY_FMT = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/New_York',
  year: 'numeric', month: '2-digit', day: '2-digit',
});

function sigDay(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso).slice(0, 10);
  const p = {};
  for (const part of SIG_DAY_FMT.formatToParts(d)) p[part.type] = part.value;
  return `${p.year}-${p.month}-${p.day}`;
}

function sigEscape(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

/** 按阈值给数字上色：Sharpe / Fitness 越高越好，Turnover 越低越好。 */
function sigClass(key, v) {
  if (v == null || !Number.isFinite(Number(v))) return 'dim';
  const n = Number(v);
  if (key === 'sharpe') return n >= 1.25 ? 'good' : n >= 1.0 ? 'warn' : 'bad';
  if (key === 'fitness') return n >= 1.0 ? 'good' : n >= 0.7 ? 'warn' : 'bad';
  if (key === 'turnover') return n <= 0.4 ? 'good' : n <= 0.7 ? 'warn' : 'bad';
  return '';
}

function sigSettingsHtml(a) {
  // 单行紧凑展示。列宽有限，故用最短写法：universe · D{delay} · {中性化缩写} · d{decay} · t{trunc}
  // 中性化名太长（如 REVERSION_AND_MOMENTUM），做常识缩写，全名仍放 title
  const NEUT_ABBR = {
    STATISTICAL: 'STAT', SUBINDUSTRY: 'SUBIND', INDUSTRY: 'IND',
    SECTOR: 'SECT', MARKET: 'MKT', COUNTRY: 'CTRY', NONE: 'NONE',
    CROWDING: 'CROWD', FAST: 'FAST', SLOW: 'SLOW',
    REVERSION_AND_MOMENTUM: 'REV_MOM', SLOW_AND_FAST: 'SLW_FST',
  };
  const bits = [];
  if (a.universe) bits.push(sigEscape(a.universe));
  if (a.delay != null) bits.push(`D${a.delay}`);
  if (a.neutralization) bits.push(sigEscape(NEUT_ABBR[a.neutralization] || a.neutralization));
  if (a.decay != null) bits.push(`d${a.decay}`);
  if (a.truncation != null) bits.push(`t${a.truncation}`);
  if (!bits.length) return '<span class="sig-py-none">—</span>';
  const full = [a.universe, a.delay != null ? `D${a.delay}` : null, a.neutralization,
    a.decay != null ? `decay ${a.decay}` : null,
    a.truncation != null ? `trunc ${a.truncation}` : null].filter(Boolean).join(' · ');
  return `<span class="sig-settings" title="${sigEscape(full)}">`
    + `${bits.join('<span class="sep"> · </span>')}</span>`;
}

/** 最多展示 MAX 个金字塔标签，其余折叠成「+N」（title 里列全）。 */
function sigPyramidsHtml(a) {
  const names = a.pyramids || [];
  if (!names.length) return '<span class="sig-py-none">未点亮</span>';
  const mults = a.pyramidMultipliers || [];
  const MAX = 2;
  const shown = names.slice(0, MAX).map((n, i) => {
    const m = mults[i];
    const mm = (m == null) ? '' : `<span class="mult">×${m}</span>`;
    return `<span class="sig-py" title="金字塔 ${sigEscape(n)}（倍率 ×${m == null ? '—' : m}）">`
      + `${sigEscape(n)}${mm}</span>`;
  }).join('');
  const rest = names.length - MAX;
  const restHtml = rest > 0
    ? `<span class="sig-py-more" title="${sigEscape(names.slice(MAX).join('\n'))}">+${rest}</span>`
    : '';
  return `<span class="sig-py-wrap">${shown}${restHtml}</span>`;
}

function sigRowHtml(a) {
  const st = a.status === 'ACTIVE' ? 'is-active' : 'is-decom';
  return `<tr>
    <td><a class="sig-id" href="/simulator?alpha=${encodeURIComponent(a.id || '')}"
           target="_blank" rel="noopener"
           title="在新标签打开 Alpha Simulator 加载 ${sigEscape(a.id)}">${sigEscape(a.id || '—')}</a></td>
    <td class="sig-num dim">${sigDay(a.dateSubmitted)}</td>
    <td><span class="sig-region">${sigEscape(a.region || '—')}</span></td>
    <td class="sig-settings-cell">${sigSettingsHtml(a)}</td>
    <td class="sig-num ${sigClass('sharpe', a.isSharpe)}">${fmtNum(a.isSharpe, 2)}</td>
    <td class="sig-num ${sigClass('fitness', a.isFitness)}">${fmtNum(a.isFitness, 2)}</td>
    <td class="sig-num">${fmtPct(a.isReturns, 2)}</td>
    <td class="sig-num ${sigClass('turnover', a.isTurnover)}">${fmtPct(a.isTurnover, 2)}</td>
    <td class="sig-num">${fmtBps(a.isMargin, 2)}</td>
    <td class="sig-td-py">${sigPyramidsHtml(a)}</td>
    <td><span class="sig-status ${st}">${sigEscape(a.status || '—')}</span></td>
  </tr>`;
}

/** 当前筛选 + 排序后的结果。 */
function sigFiltered() {
  const kw = sigState.keyword.trim().toLowerCase();
  let rows = sigState.items;
  if (sigState.region) rows = rows.filter((a) => (a.region || '') === sigState.region);
  if (kw) {
    rows = rows.filter((a) => {
      const hay = [
        a.id, a.region, a.universe, a.neutralization, a.status,
        (a.pyramids || []).join(' '),
        (a.classifications || []).join(' '),
      ].join(' ').toLowerCase();
      return hay.includes(kw);
    });
  }
  const key = sigState.sort;
  // ⚠️ 下拉框的 value 是短名（sharpe / fitness / …），而接口字段带 is 前缀
  //    （isSharpe / isFitness / …）。曾经漏了这层映射，导致 a[key] 全 undefined、
  //    排序静默失效（看起来像"没排序"而不是报错），极难发现。
  const FIELD = {
    sharpe: 'isSharpe',
    fitness: 'isFitness',
    returns: 'isReturns',
    margin: 'isMargin',
    turnover: 'isTurnover',
  };
  const field = key === 'dateSubmitted' ? 'dateSubmitted' : (FIELD[key] || key);
  // Turnover 越低越好 → 升序；其余指标越高越好 → 降序。
  // 注意：不能用 Infinity / -Infinity 做缺失值哨兵，因为差值会算出 Infinity 或
  // NaN（-Infinity - (-Infinity)），比较器非传递会导致 sort 结果乱序。
  // 正确做法：先按「有无值」分组，再按值排序。
  const asc = field === 'isTurnover';
  const hasVal = (a) => {
    const v = a[field];
    return v != null && Number.isFinite(Number(v));
  };
  const byValue = (x, y) => {
    const nx = Number(x[field]), ny = Number(y[field]);
    return asc ? nx - ny : ny - nx;
  };
  // 按「绝对时刻」降序，而不是按字符串比较。
  // 字符串比较实际在比「美东墙钟」：跨 EST/EDT 切换的那一小时会排错
  // （如 01:15-05:00 其实晚于 01:30-04:00，字符串却判前者更小）。一年一次，
  // 且静默出错、看不出来，所以统一按 Date.parse 的绝对时刻比。
  const ts = (a) => {
    const t = Date.parse(a.dateSubmitted || '');
    return Number.isFinite(t) ? t : null;
  };
  const byDateDesc = (x, y) => {
    const tx = ts(x), ty = ts(y);
    if (tx == null && ty == null) return 0;
    if (tx == null) return 1;    // 无日期的行沉底
    if (ty == null) return -1;
    return ty - tx;
  };
  if (field === 'dateSubmitted') {
    rows = [...rows].sort(byDateDesc);
  } else {
    // 缺失值的行统一沉底，且它们之间保持原有顺序（稳定）
    const withVal = rows.filter(hasVal).sort(byValue);
    const noVal = rows.filter((a) => !hasVal(a));
    rows = withVal.concat(noVal);
  }
  return rows;
}

function sigRenderChips() {
  const box = $('sigRegionFilters');
  if (!box) return;
  const regions = Object.entries(sigState.byRegion || {})
    .sort((a, b) => b[1] - a[1]);
  const total = sigState.items.length;
  let html = `<span class="sig-chip${sigState.region ? '' : ' active'}" data-region="">`
    + `全部 <span class="sig-chip-count">${total}</span></span>`;
  html += regions.map(([r, c]) =>
    `<span class="sig-chip${sigState.region === r ? ' active' : ''}" data-region="${sigEscape(r)}">`
    + `${sigEscape(r)} <span class="sig-chip-count">${c}</span></span>`
  ).join('');
  box.innerHTML = html;
  box.querySelectorAll('.sig-chip').forEach((el) => {
    el.addEventListener('click', () => {
      sigState.region = el.dataset.region || '';
      sigState.page = 1;
      sigRenderChips();
      sigRenderTable();
    });
  });
}

function sigRenderTable() {
  const tbody = $('sigTbody');
  const loading = $('sigLoading');
  if (!tbody) return;
  if (loading) loading.hidden = true;

  const rows = sigFiltered();
  const pages = Math.max(1, Math.ceil(rows.length / SIG_PAGE_SIZE));
  if (sigState.page > pages) sigState.page = pages;
  const start = (sigState.page - 1) * SIG_PAGE_SIZE;
  const slice = rows.slice(start, start + SIG_PAGE_SIZE);

  tbody.innerHTML = slice.length
    ? slice.map(sigRowHtml).join('')
    : '<tr><td colspan="11" class="sig-loading">没有匹配的 alpha</td></tr>';

  sigRenderPager(rows.length, pages);
  sigUpdateSub(rows.length);
}

function sigRenderPager(total, pages) {
  const box = $('sigPager');
  if (!box) return;
  if (total === 0) { box.innerHTML = ''; return; }

  const cur = sigState.page;
  const btn = (label, page, opts = {}) =>
    `<button class="sig-page-btn${opts.active ? ' active' : ''}" data-page="${page}"`
    + `${opts.disabled ? ' disabled' : ''}>${label}</button>`;

  // 页码窗口：首页 … 当前±2 … 末页
  const wanted = new Set([1, pages, cur, cur - 1, cur + 1, cur - 2, cur + 2]);
  const nums = [...wanted].filter((n) => n >= 1 && n <= pages).sort((a, b) => a - b);
  let numsHtml = '';
  let prev = 0;
  for (const n of nums) {
    if (prev && n - prev > 1) numsHtml += '<span class="sig-page-info">…</span>';
    numsHtml += btn(String(n), n, { active: n === cur });
    prev = n;
  }

  box.innerHTML = btn('‹', cur - 1, { disabled: cur <= 1 })
    + numsHtml
    + btn('›', cur + 1, { disabled: cur >= pages })
    + `<span class="sig-page-info">共 ${total} 条 · ${pages} 页</span>`;

  box.querySelectorAll('.sig-page-btn[data-page]').forEach((el) => {
    el.addEventListener('click', () => {
      const p = parseInt(el.dataset.page, 10);
      if (!Number.isFinite(p) || p < 1 || p > pages || p === sigState.page) return;
      sigState.page = p;
      sigRenderTable();
      const wrap = document.querySelector('.sig-table-wrap');
      if (wrap) wrap.scrollTop = 0;
    });
  });
}

function sigUpdateSub(filteredTotal) {
  const sub = $('sigModalSub');
  if (!sub) return;
  const consultant = sigState.consultantCount;   // 顶部卡片 = submissionsCount (80)
  const n = sigState.count == null ? sigState.items.length : sigState.count;  // 明细 136
  const parts = [`明细 ${n} 条`];
  // 三个口径并排，避免再被误认为「80 就是平台数」
  if (consultant != null) parts.push(`卡片 ${consultant}`);
  parts.push('Genius 87');
  if (filteredTotal != null && filteredTotal !== n) parts.push(`当前筛选 ${filteredTotal} 条`);
  sub.textContent = parts.join('　·　');
}

function sigRenderEmpty(msg) {
  const tbody = $('sigTbody');
  const loading = $('sigLoading');
  if (loading) loading.hidden = true;
  if (tbody) tbody.innerHTML = `<tr><td colspan="11" class="sig-loading">${sigEscape(msg)}</td></tr>`;
  const box = $('sigPager');
  if (box) box.innerHTML = '';
}

async function sigLoad(refresh = false) {
  if (sigState.loading) return;
  sigState.loading = true;
  const loading = $('sigLoading');
  if (loading) loading.hidden = false;
  try {
    // 同时拿 consultant 的 submissionsCount（顶部卡片显示的值），让弹窗副标题把三个数并排说清
    const [r, ps] = await Promise.all([
      api('/api/simulator/submitted-alphas' + (refresh ? '?refresh=true' : '')),
      api('/api/simulator/platform-stats').catch(() => ({ signals: null })),
    ]);
    if (!r.ok) throw new Error(r.error || '接口返回失败');
    sigState.items = r.items || [];
    sigState.byRegion = r.byRegion || {};
    sigState.byStatus = r.byStatus || {};
    sigState.count = r.count;
    sigState.platformCount = r.platform_count;
    sigState.consultantCount = (ps && ps.signals != null) ? ps.signals : null;
    sigState.loaded = true;
    sigState.error = null;
    sigRenderChips();
    sigRenderTable();
  } catch (e) {
    sigState.error = e.message || String(e);
    sigRenderEmpty('读取失败：' + sigState.error);
    const sub = $('sigModalSub');
    if (sub) sub.textContent = '读取失败：' + sigState.error;
  } finally {
    sigState.loading = false;
  }
}

async function openSignalsModal() {
  if (!sigModal) return;
  sigModal.hidden = false;
  const card = sigModal.querySelector('.osm-modal-card');
  if (card) card.scrollTop = 0;

  // 先用页面已有的 signals 值占位，避免弹窗顶部短暂显示空
  const gs = $('gSignals');
  if (sigState.platformCount == null && gs && /^\d+$/.test(gs.textContent.trim())) {
    sigState.platformCount = parseInt(gs.textContent.trim(), 10);
    sigUpdateSub(null);
  }
  // 首次打开或上次失败 → 拉数据；否则直接用内存里的明细（有变化点「刷新」）
  if (!sigState.loaded || sigState.error) await sigLoad();
  else { sigRenderChips(); sigRenderTable(); }
}

function closeSignalsModal() {
  if (sigModal) sigModal.hidden = true;
}

(() => {
  const card = $('signalsCard');
  if (card) {
    card.addEventListener('click', openSignalsModal);
    card.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        openSignalsModal();
      }
    });
  }

  // 手动同步：强制绕过缓存重拉 BRAIN 已提交 alpha
  const sigRefreshBtn = $('sigRefreshBtn');
  if (sigRefreshBtn) {
    sigRefreshBtn.addEventListener('click', async () => {
      if (sigRefreshBtn.disabled) return;
      const old = sigRefreshBtn.textContent;
      sigRefreshBtn.disabled = true;
      sigRefreshBtn.textContent = '同步中…';
      try {
        await sigLoad(true);
        toast('已同步 BRAIN 已提交 alpha', '');
      } catch (e) {
        toast('同步失败：' + (e.message || e), '');
      } finally {
        sigRefreshBtn.disabled = false;
        sigRefreshBtn.textContent = old;
      }
    });
  }
  if (!sigModal) return;

  const closeBtn = $('sigModalClose');
  if (closeBtn) closeBtn.addEventListener('click', closeSignalsModal);
  const backdrop = sigModal.querySelector('[data-sig-close]');
  if (backdrop) backdrop.addEventListener('click', closeSignalsModal);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !sigModal.hidden) closeSignalsModal();
  });

  const search = $('sigSearch');
  if (search) {
    let timer = null;
    search.addEventListener('input', () => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        sigState.keyword = search.value || '';
        sigState.page = 1;
        sigRenderTable();
      }, 160);
    });
  }
  const sortSel = $('sigSort');
  if (sortSel) {
    sortSel.addEventListener('change', () => {
      sigState.sort = sortSel.value;
      sigState.page = 1;
      sigRenderTable();
    });
  }
})();

// 支持 ?signals=1 直达：进页面就弹清单
if (urlParams.get('signals')) openSignalsModal();

// =====================================================================
// Total Payment 弹窗：每日 Base Payment 折线图
// 横向滑动（拖拽/滚轮）+ 数据点磁吸 + 顶部 odometer 滚动计数器联动
// 数据：/api/simulator/base-payment（BRAIN /users/self/activities/base-payment）
// =====================================================================
const payModal = $('payModal');
const payState = {
  dates: [],          // 统一日期轴 [date,...]（base + submissions 并集）
  baseMap: {},        // {date: value}
  subMap: {},         // {date: count}
  hoverIdx: null,
  wrap: null,
  cvBase: null, ctxBase: null,
  cvSub: null, ctxSub: null,
  dpr: 1,
  W: 0,
  H1: 240, H2: 150,
  geom: null,
};

function openPayModal() {
  if (!payModal) return;
  payModal.hidden = false;
  const card = payModal.querySelector('.osm-modal-card');
  if (card) card.scrollTop = 0;
  $('payModalSub').textContent = '正在读取 BRAIN…';
  api('/api/simulator/base-payment').then((r) => {
    if (!r.ok) throw new Error(r.error || '接口失败');
    const baseRecs = r.records || [];
    const subRecs = r.sub_records || [];
    payState.baseMap = {};
    baseRecs.forEach((x) => { payState.baseMap[x[0]] = Number(x[1]); });
    payState.subMap = {};
    subRecs.forEach((x) => { payState.subMap[x[0]] = Number(x[1]); });
    const set = new Set();
    baseRecs.forEach((x) => set.add(x[0]));
    subRecs.forEach((x) => set.add(x[0]));
    payState.dates = Array.from(set).sort();
    if (!payState.dates.length) throw new Error('无数据');
    renderPaySummary(r);
    setupPayCanvases();
    payState.hoverIdx = payState.dates.length - 1;   // 默认最新一天
    drawPayChart();
    drawSubChart();
    setPayOdo(payState.hoverIdx, true);
    $('payModalSub').textContent = `BRAIN base-payment / submissions 活动 · 共 ${payState.dates.length} 天 · ${r.currency || 'USD'}`;
  }).catch((e) => {
    $('payModalSub').textContent = '读取失败：' + (e.message || e);
  });
}

function renderPaySummary(r) {
  const f = (o) => (o && o.value != null) ? '$' + Number(o.value).toFixed(2) : '—';
  const g = (o) => (o && o.value != null) ? Number(o.value) : '—';
  $('paySummary').innerHTML =
    `<span>本季 <b>${f(r.current)}</b></span>` +
    `<span>上季 <b>${f(r.previous)}</b></span>` +
    `<span>YTD <b>${f(r.ytd)}</b></span>` +
    `<span>昨日 <b>${f(r.yesterday)}</b></span>` +
    `<span class="pay-sum-total">BP 累计 <b>${f(r.total)}</b></span>` +
    `<span class="pay-sum-total">提交累计 <b>${g(r.sub_total)}</b></span>`;
}

function setupPayCanvases() {
  const wrap = document.querySelector('.pay-charts');
  payState.wrap = wrap;
  const dpr = window.devicePixelRatio || 1;
  payState.dpr = dpr;
  const SPACING = 44;
  const W = Math.max(wrap.clientWidth, payState.dates.length * SPACING);
  payState.W = W;
  const cv1 = $('payChart'), cv2 = $('subChart');
  [cv1, cv2].forEach((cv) => { cv.style.width = W + 'px'; });
  cv1.style.height = payState.H1 + 'px';
  cv1.width = Math.round(W * dpr); cv1.height = Math.round(payState.H1 * dpr);
  cv2.style.height = payState.H2 + 'px';
  cv2.width = Math.round(W * dpr); cv2.height = Math.round(payState.H2 * dpr);
  payState.cvBase = cv1; payState.ctxBase = cv1.getContext('2d');
  payState.cvSub = cv2; payState.ctxSub = cv2.getContext('2d');
  const padL = 46, padR = 18;
  const SP = payState.dates.length > 1 ? (W - padL - padR) / (payState.dates.length - 1) : 0;
  payState.geom = { xOf: (i) => padL + i * SP, SP, n: payState.dates.length, padL, padR };
  wrap.scrollLeft = Math.max(0, W - wrap.clientWidth);  // 默认定位到最新一天
}

function drawPayChart() {
  const cv = payState.cvBase; if (!cv) return;
  const ctx = payState.ctxBase; const dpr = payState.dpr;
  const W = payState.W, H = payState.H1;
  const g = payState.geom;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);
  const padT = 16, padB = 26;
  const plotW = W - g.padL - g.padR, plotH = H - padT - padB;
  const vals = payState.dates.map((d) => payState.baseMap[d]).filter((v) => v != null);
  if (!vals.length) return;
  // 固定刻度标尺：每格 = stepAmt 美元（当前金额小，stepAmt=$0.5），格子高度即可读出金额
  const dataMax = Math.max.apply(null, vals);
  const stepAmt = (() => { let s = 0.5; while (dataMax / s > 8) s *= 2; return s; })();
  const vmax = Math.max(stepAmt, Math.ceil(dataMax / stepAmt) * stepAmt), vmin = 0;
  const yOf = (v) => padT + plotH - ((v - vmin) / (vmax - vmin)) * plotH;
  g.yBase = yOf;
  // 网格
  ctx.font = '13px "IBM Plex Sans", sans-serif'; ctx.textBaseline = 'middle';
  const nSteps = Math.round(vmax / stepAmt);
  for (let k = 0; k <= nSteps; k++) {
    const v = k * stepAmt; const y = yOf(v);
    ctx.strokeStyle = k === 0 ? 'rgba(255,255,255,0.15)' : 'rgba(255,255,255,0.07)';
    ctx.beginPath(); ctx.moveTo(g.padL, y); ctx.lineTo(W - g.padR, y); ctx.stroke();
    ctx.fillStyle = 'rgba(200,214,235,0.65)'; ctx.textAlign = 'right';
    ctx.fillText('$' + v.toFixed(stepAmt >= 1 ? 0 : 1), g.padL - 8, y);
  }
  // 面积 + 折线（仅 baseMap 有值点）
  const pts = [];
  payState.dates.forEach((d, i) => { if (payState.baseMap[d] != null) pts.push([i, payState.baseMap[d]]); });
  if (pts.length > 1) {
    const grad = ctx.createLinearGradient(0, padT, 0, padT + plotH);
    grad.addColorStop(0, 'rgba(34,211,238,0.28)');
    grad.addColorStop(1, 'rgba(34,211,238,0)');
    ctx.beginPath(); ctx.moveTo(g.xOf(pts[0][0]), yOf(pts[0][1]));
    pts.forEach((p) => ctx.lineTo(g.xOf(p[0]), yOf(p[1])));
    ctx.lineTo(g.xOf(pts[pts.length - 1][0]), padT + plotH);
    ctx.lineTo(g.xOf(pts[0][0]), padT + plotH); ctx.closePath();
    ctx.fillStyle = grad; ctx.fill();
    ctx.strokeStyle = '#22d3ee'; ctx.lineWidth = 2; ctx.lineJoin = 'round';
    ctx.beginPath();
    pts.forEach((p, i) => { const x = g.xOf(p[0]), y = yOf(p[1]); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
    ctx.stroke();
  }
  // 数据点
  payState.dates.forEach((d, i) => {
    if (payState.baseMap[d] == null) return;
    const x = g.xOf(i), y = yOf(payState.baseMap[d]);
    const hot = i === payState.hoverIdx;
    ctx.beginPath(); ctx.arc(x, y, hot ? 5.5 : 2.4, 0, Math.PI * 2);
    ctx.fillStyle = hot ? '#fbbf24' : '#22d3ee'; ctx.fill();
    if (hot) {
      ctx.strokeStyle = 'rgba(251,191,36,0.9)'; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.arc(x, y, 9, 0, Math.PI * 2); ctx.stroke();
    }
  });
  drawHoverMark(ctx, g, W, H, padT, padB, plotH);
}

function drawSubChart() {
  const cv = payState.cvSub; if (!cv) return;
  const ctx = payState.ctxSub; const dpr = payState.dpr;
  const W = payState.W, H = payState.H2;
  const g = payState.geom;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);
  const padT = 14, padB = 26;
  const plotW = W - g.padL - g.padR, plotH = H - padT - padB;
  // 固定 5 格：普通 alpha 一天最多 4 个 + super alpha 一天最多 1 个
  const vmax = 5, vmin = 0;
  const yOf = (v) => padT + plotH - ((v - vmin) / (vmax - vmin)) * plotH;
  // 网格（5 格，每格 = 1 个 alpha）
  ctx.font = '13px "IBM Plex Sans", sans-serif'; ctx.textBaseline = 'middle';
  for (let k = 0; k <= 5; k++) {
    const v = k; const y = yOf(v);
    ctx.strokeStyle = k === 0 ? 'rgba(255,255,255,0.14)' : 'rgba(255,255,255,0.07)';
    ctx.beginPath(); ctx.moveTo(g.padL, y); ctx.lineTo(W - g.padR, y); ctx.stroke();
    ctx.fillStyle = 'rgba(200,214,235,0.65)'; ctx.textAlign = 'right';
    ctx.fillText(String(v), g.padL - 8, y);
  }
  // 柱状（按当日提交数，一格一个）
  const bw = Math.max(3, g.SP * 0.62);
  payState.dates.forEach((d, i) => {
    const v = Math.min(payState.subMap[d] || 0, vmax);
    const x = g.xOf(i) - bw / 2, y = yOf(v);
    const hot = i === payState.hoverIdx;
    ctx.fillStyle = hot ? '#fbbf24' : 'rgba(72,184,224,0.82)';
    ctx.fillRect(x, y, bw, padT + plotH - y);
  });
  drawHoverMark(ctx, g, W, H, padT, padB, plotH);
}

function drawHoverMark(ctx, g, W, H, padT, padB, plotH) {
  if (payState.hoverIdx == null) return;
  const i = payState.hoverIdx, x = g.xOf(i);
  ctx.strokeStyle = 'rgba(251,191,36,0.35)'; ctx.setLineDash([4, 4]); ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(x, padT); ctx.lineTo(x, H - padB); ctx.stroke();
  ctx.setLineDash([]);
  const date = payState.dates[i];
  const label = date + (payState.baseMap[date] != null ? '  $' + payState.baseMap[date].toFixed(2) : '');
  ctx.font = '14px "IBM Plex Sans", sans-serif';
  const tw = ctx.measureText(label).width + 18;
  let tx = x - tw / 2; tx = Math.max(g.padL, Math.min(W - g.padR - tw, tx));
  const ty = padT;
  ctx.fillStyle = 'rgba(15,23,33,0.94)'; roundRect(ctx, tx, ty, tw, 24, 6); ctx.fill();
  ctx.fillStyle = '#fbbf24'; ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
  ctx.fillText(label, tx + 9, ty + 12);
}

function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

function xToIdx(x) {
  const g = payState.geom;
  if (!g || g.SP === 0) return null;
  const i = Math.round((x - g.padL) / g.SP);
  return (i >= 0 && i < g.n) ? i : null;
}

// odometer：数字滚动计数器（easeOutCubic 补间，支持小数/整数）
function animateNum(el, from, to, instant, dec) {
  if (instant) {
    el.textContent = dec ? to.toFixed(dec) : String(Math.round(to));
    el.dataset.v = dec ? to : Math.round(to);
    return;
  }
  if (el._anim) cancelAnimationFrame(el._anim);
  const t0 = performance.now();
  const dur = 450;
  const step = (now) => {
    const k = Math.min(1, (now - t0) / dur);
    const e = 1 - Math.pow(1 - k, 3);
    const v = from + (to - from) * e;
    el.textContent = dec ? v.toFixed(dec) : String(Math.round(v));
    if (k < 1) el._anim = requestAnimationFrame(step);
    else { el.dataset.v = dec ? to : Math.round(to); }
  };
  el._anim = requestAnimationFrame(step);
}

function setPayOdo(idx, instant) {
  const date = payState.dates[idx];
  if (!date) return;
  $('payOdoDate').textContent = date;
  const baseEl = $('payOdoVal'), subEl = $('payOdoSub');
  const baseTo = payState.baseMap[date];
  const subTo = payState.subMap[date] || 0;
  if (baseTo == null) { baseEl.textContent = '—'; baseEl.dataset.v = ''; }
  else animateNum(baseEl, parseFloat(baseEl.dataset.v || '0'), baseTo, instant, 2);
  animateNum(subEl, parseInt(subEl.dataset.v || '0', 10), subTo, instant, 0);
}

function bindPayInteractions() {
  // canvas 元素常驻 DOM，直接按 id 取，避免 cvBase/cvSub 在绑定时尚为 null 而导致交互失效
  const cv1 = $('payChart'), cv2 = $('subChart');
  const wrap = document.querySelector('.pay-charts');
  if (!wrap) return;
  // 横向滚轮（绑一次即可）
  wrap.addEventListener('wheel', (e) => {
    if (e.deltaY !== 0) { wrap.scrollLeft += e.deltaY; e.preventDefault(); }
  }, { passive: false });
  [cv1, cv2].forEach((cv) => {
    if (!cv) return;
    let dragging = false, dragX = 0, scroll0 = 0;
    cv.addEventListener('mousedown', (e) => {
      dragging = true; dragX = e.clientX; scroll0 = wrap.scrollLeft;
      cv.style.cursor = 'grabbing';
    });
    cv.addEventListener('mousemove', (e) => {
      if (dragging) { wrap.scrollLeft = scroll0 - (e.clientX - dragX); return; }
      const rect = cv.getBoundingClientRect();
      const idx = xToIdx(e.clientX - rect.left);
      if (idx != null && idx !== payState.hoverIdx) {
        payState.hoverIdx = idx;
        drawPayChart(); drawSubChart(); setPayOdo(idx);
      }
    });
  });
  window.addEventListener('mouseup', () => {
    [cv1, cv2].forEach((cv) => { if (cv) cv.style.cursor = 'default'; });
  });
}

(function bindPayModal() {
  if (!payModal) return;
  const card = $('totalPaymentCard');
  if (card) {
    card.addEventListener('click', openPayModal);
    card.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openPayModal(); }
    });
  }
  const closeBtn = $('payModalClose');
  if (closeBtn) closeBtn.addEventListener('click', () => { payModal.hidden = true; });
  const backdrop = payModal.querySelector('[data-pay-close]');
  if (backdrop) backdrop.addEventListener('click', () => { payModal.hidden = true; });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !payModal.hidden) payModal.hidden = true;
  });
  // 图表交互只绑定一次（canvas 元素常驻 DOM）
  bindPayInteractions();
})();

// 支持 ?pay=1 直达
if (urlParams.get('pay')) openPayModal();

// =====================================================================
// PnL 同步（260924 自指挥中心迁入）
// ---------------------------------------------------------------------
// 与原指挥中心版本的两点差异：
//  1) 指挥中心有 WebSocket 推送 pnl_sync_progress，本页没有 WS，
//     所以运行期用 1500ms 轮询兜住进度（与旧版轮询节奏一致，不引入新机制）。
//  2) 运行态放本模块作用域，不污染本页已有的 state。
// 元素取不到时一律跳过（if 守卫），避免卡片被移除后整页 JS 中断。
// =====================================================================
let syncRunning = false;

function syncSetText(id, val) { const el = $(id); if (el) el.textContent = val; }

async function refreshSyncStatus() {
  try {
    const r = await api('/api/sync/pnl/status');
    syncRunning = !!r.running;
    const prog = r.progress || {};
    const phase = prog.phase || '';
    const cur = prog.current || 0, tot = prog.total || 0;
    const msg = prog.message || '';

    syncSetText('syncStatus', syncRunning ? '运行中…'
      : (phase === 'completed' ? '✅ 已完成' : (phase === 'error' ? '❌ 出错' : '未运行')));
    syncSetText('syncProgress', msg || (syncRunning ? '进行中…' : '空闲'));

    // 仪表盘：总量已知才按比例画，总量未知时不假装进度
    const pct = tot > 0 ? Math.min(100, cur / tot * 100) : (phase === 'completed' ? 100 : 0);
    const gauge = $('syncGaugeFg');
    if (gauge) {
      const c = 2 * Math.PI * 52; // r=52，与 markup 保持一致
      gauge.setAttribute('stroke-dasharray', c);
      gauge.setAttribute('stroke-dashoffset', c * (1 - pct / 100));
      gauge.classList.remove('completed', 'failed');
      if (phase === 'completed') gauge.classList.add('completed');
      else if (phase === 'error') gauge.classList.add('failed');
    }
    syncSetText('syncPercent', Math.round(pct) + '%');
    // 环形下方的线性进度条：整幅卡很宽，光靠 140px 的环看不出进度，条更直观
    const fill = $('syncProgressFill');
    if (fill) fill.style.width = Math.min(Math.max(pct, 0), 100) + '%';
    const phaseMap = { alphas: 'SCAN', pnl: 'PNL', writing: 'WRITE', corr: 'CORR', completed: 'DONE', error: 'FAIL' };
    syncSetText('syncPhaseLabel', phaseMap[phase] || 'READY');

    const liveTag = $('syncLiveTag');
    if (liveTag) {
      liveTag.className = syncRunning ? 'live-tag on' : 'live-tag';
      // 末位是文本节点（<span class="live-dot"></span>空闲），只改文本不动圆点
      if (liveTag.lastChild && liveTag.lastChild.nodeType === 3) {
        liveTag.lastChild.textContent = syncRunning ? 'LIVE' : '空闲';
      }
    }

    if (r.last_result && r.last_result.total) {
      const box = $('syncResult');
      if (box) box.style.display = 'grid';
      syncSetText('syncResultTotal', r.last_result.total);
      syncSetText('syncResultCorr', r.last_result.corr_computed || 0);
      syncSetText('syncResultFail', (r.last_result.failed_ids || []).length);
    }
  } catch (e) { /* 静默：进度显示失败不打断主流程 */ }
  return syncRunning;
}

// 重复调用安全：先清旧定时器再起新的
function startSyncPolling() {
  if (window._syncTimer) clearInterval(window._syncTimer);
  window._syncTimer = setInterval(refreshSyncStatus, 1500);
  refreshSyncStatus();
}

(function bindSyncCard() {
  const startBtn = $('btnStartSync');
  if (!startBtn) return; // 卡片不在本页时直接跳过

  startBtn.addEventListener('click', async () => {
    syncSetText('syncStatus', '启动中…');
    try {
      // 默认增量只拉未拉过的；勾选「强制全量」则重拉所有
      const forceFull = $('syncForceFull') ? $('syncForceFull').checked : false;
      const r = await api('/api/sync/pnl', {
        method: 'POST',
        body: JSON.stringify({ compute_corr: false, force_full: forceFull }),
      });
      if (!r.ok) { toast(r.error || '启动失败', 'error'); syncSetText('syncStatus', '未运行'); return; }
      toast('PnL 同步已启动' + (forceFull ? '（强制全量）' : '（增量）'), 'success');
      startSyncPolling();
    } catch (e) {
      const msg = (e && e.message) ? e.message : '';
      // 刷新页面后点启动：后端已在跑，恢复进度轮询即可，不报错
      if (msg.indexOf('已在运行') >= 0) {
        startSyncPolling();
        toast('同步已在后台运行，已恢复进度轮询', 'success');
      } else {
        toast('启动失败：' + msg, 'error');
        syncSetText('syncStatus', '未运行');
      }
    }
  });

  const stopBtn = $('btnStopSync');
  if (stopBtn) stopBtn.addEventListener('click', async () => {
    try {
      await api('/api/sync/pnl/stop', { method: 'POST' });
      toast('已发送停止信号', 'success');
    } catch (e) { toast('停止失败：' + e.message, 'error'); }
  });

  // 手动算 Self/PPA corr（按需触发）
  const corrBtn = $('btnComputeCorr');
  if (corrBtn) corrBtn.addEventListener('click', async () => {
    const alphaId = ($('corrAlphaId') && $('corrAlphaId').value || '').trim();
    if (!alphaId) { toast('先填 alpha ID', 'error'); return; }
    const el = $('corrResult');
    el.textContent = '计算中…';
    corrBtn.disabled = true;
    try {
      const r = await api('/api/corr/compute', {
        method: 'POST',
        body: JSON.stringify({ alpha_id: alphaId }),
      });
      const max = r.max_corr;
      const src = r.max_corr_source || '—';
      const sm = r.self ? r.self.max : null;
      const pm = r.ppa ? r.ppa.max : null;
      const pool = r.pool_size == null ? 0 : r.pool_size;
      el.innerHTML = max != null
        ? `SELF=<span style="font-weight:600">${sm != null ? sm.toFixed(3) : '—'}</span>`
          + `　PPA=<span style="font-weight:600">${pm != null ? pm.toFixed(3) : '—'}</span>`
          + `　→ max_corr = <span style="color:var(--accent);font-weight:600">${max.toFixed(3)}</span> (${src})`
          + `　池子 ${pool} 条`
        : (r.message || '算完但池子为空（该 region 账号下无已提交 alpha）');
      toast(`✅ ${alphaId} max_corr=${max != null ? max.toFixed(3) : 'N/A'} (${src})`, 'success');
    } catch (e) {
      el.textContent = '';
      toast(`算 corr 失败：${e.message}`, 'error');
    } finally {
      corrBtn.disabled = false;
    }
  });

  // 进页先读一次状态：后端若正在跑，自动接上进度轮询（刷新页面不丢进度）
  refreshSyncStatus().then((running) => {
    if (running && !window._syncTimer) window._syncTimer = setInterval(refreshSyncStatus, 1500);
  });
})();
