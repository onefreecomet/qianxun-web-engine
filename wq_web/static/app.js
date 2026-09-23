// 千寻 web v81 —— 客户端逻辑（无任何外部依赖）

// ====== State ======
const state = {
  batches: [],
  currentBatch: null,
  ws: null,
  wsRetry: 0,
};

// ====== Utilities ======
const $ = (id) => document.getElementById(id);
const fmtNum = (v, d = 2) => v == null ? '—' : Number(v).toFixed(d);
const fmtSharpe = (v) => {
  if (v == null) return '<span class="sharpe-zero">—</span>';
  const n = Number(v);
  if (n > 0) return `<span class="sharpe-pos">${n.toFixed(2)}</span>`;
  if (n < 0) return `<span class="sharpe-neg">${n.toFixed(2)}</span>`;
  return `<span class="sharpe-zero">0.00</span>`;
};
const fmtBps = (v) => v == null ? '—' : (Number(v) * 10000).toFixed(1);
// 把 UTC 时间戳转成北京时间（UTC+8）显示，避免直接截 UTC 字符串
const fmtCST = (iso, withSec = false) => {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;            // 解析失败原样返回
  d.setTime(d.getTime() + 8 * 3600 * 1000);       // 推到东八区
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} `
       + `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`
       + (withSec ? `:${p(d.getUTCSeconds())}` : '');
};

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

// ====== Init（单页版：三个区块全部直接加载） ======

// ====== WebSocket ======
function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const url = `${proto}://${location.host}/ws/progress`;
  state.ws = new WebSocket(url);
  const ind = $('wsIndicator'), st = $('wsStatus');

  state.ws.onopen = () => {
    ind.className = 'ws-indicator connected';
    st.textContent = '已连接';
    state.wsRetry = 0;
  };
  state.ws.onmessage = (e) => {
    try {
      const msg = JSON.parse(e.data);
      handleWS(msg);
    } catch (err) { console.warn('ws parse', err); }
  };
  state.ws.onclose = () => {
    ind.className = 'ws-indicator disconnected';
    st.textContent = '断开，重连…';
    setTimeout(connectWS, Math.min(2000 * ++state.wsRetry, 10000));
  };
  state.ws.onerror = () => state.ws.close();
}

function handleWS(msg) {
  if (msg.event === 'snapshot') {
    state.batches = msg.batches || [];
    renderBatchList();
    return;
  }
  // PnL 同步进度推送
  if (msg.event === 'pnl_sync_progress') {
    refreshSyncStatus();
    return;
  }
  // sim_completed / sim_failed / batch_done / batch_error 都触发列表刷新
  if (['sim_completed', 'sim_failed', 'sim_submitted', 'batch_done', 'batch_error'].includes(msg.event)) {
    // 只显示 toast，不每条都重渲染（避免抖动）
    if (msg.event === 'batch_done') {
      toast(`✅ 批次 ${msg.batch_no} 完成：${msg.completed} 条，${msg.backfilled || 0} 已回填`, 'success');
    } else if (msg.event === 'batch_error') {
      toast(`❌ 批次 ${msg.batch_no} 出错：${msg.error}`, 'error');
    }
    // 节流刷新
    clearTimeout(state._refreshTimer);
    state._refreshTimer = setTimeout(() => {
      loadBatches(true);
      if (state.currentBatch === msg.batch_no) refreshDetailProgress();
    }, 500);
  }
}

// ====== Dashboard ======
async function loadStats() {
  try {
    const r = await api('/api/stats');
    const s = r.stats;
    // 顶栏五卡已全部换成平台指标（见 refreshGlobalMetrics），此处只回显 db 路径
    $('dbPathLabel').textContent = '📁 ' + r.db_path;
  } catch (e) { toast('加载统计失败：' + e.message, 'error'); }
}

// ====== 每日配额（顶栏徽章） ======
let _quotaCountdownTimer = null;

function fmtReset(sec) {
  if (!sec || sec <= 0) return '即将重置';
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h > 0 ? `${h}时${m}分` : `${m}分${s}秒`;
}

function renderQuota() {
  // stale 态：每秒刷新「距下次重置」的倒计时
  if (window._quotaStaleData) { renderQuotaStale(window._quotaStaleData); return; }
  // 有真实值时每秒本地倒计时重画（不重新请求）
  if (window._quotaData && window._quotaData.limit != null) {
    const q = window._quotaData;
    const elapsed = Math.floor(Date.now() / 1000 - q._fetchedAt);
    const left = Math.max(q.reset_sec_left - elapsed, 0);
    // 本地倒计时归零 = 快照跨越了重置点，旧数字失效，别再显示
    if (left <= 0) { renderQuotaStale(q); return; }
    const pct = q.limit > 0 ? q.remaining / q.limit : 1;
    // 更新数字
    $('quotaValue').textContent = q.remaining;
    $('quotaSub').textContent = `/${q.limit} · ${fmtReset(left)}`;
    // 更新环形
    const ring = $('quotaRingFg');
    if (ring) {
      const circumference = 2 * Math.PI * 8; // r=8
      const offset = circumference * (1 - pct);
      ring.setAttribute('stroke-dashoffset', offset);
      ring.classList.remove('warn', 'danger');
      if (pct <= 0.05) ring.classList.add('danger');
      else if (pct <= 0.2) ring.classList.add('warn');
    }
  }
}

// 快照已过期（跨越重置点）：显示占位而非旧数字
function renderQuotaStale(q) {
  const d = q || {};
  window._quotaStaleData = d;
  let sub = '/已重置';
  if (d.reset_passed_at) {
    const nextReset = _nextResetFrom(d.reset_passed_at);
    const left = Math.max(Math.floor(nextReset - Date.now() / 1000), 0);
    sub = `/已重置 · ${fmtReset(left)}`;
  } else if (d.next_reset_at) {
    const left = Math.max(Math.floor(d.next_reset_at - Date.now() / 1000), 0);
    sub = `/已重置 · ${fmtReset(left)}`;
  }
  $('quotaValue').textContent = '--';
  $('quotaSub').textContent = sub;
  const ring = $('quotaRingFg');
  if (ring) {
    ring.classList.remove('warn', 'danger');
    ring.setAttribute('stroke-dashoffset', 0);
  }
  const badge = $('quotaBadge');
  if (badge) {
    badge.classList.add('stale');
    const t = d.stale_at ? new Date(d.stale_at * 1000).toLocaleString('zh-CN', { hour12: false }) : '未知';
    badge.title = `配额快照已过期（${t} 的读数，已跨过重置点）。平台每日 12:00 重置，下次回测后自动更新为真实值。`;
  }
  window._quotaData = null;
}

// 由某个已过去的 12:00 重置点推出「下一个」12:00
function _nextResetFrom(passedAt) {
  const d = new Date((passedAt + 1) * 1000);
  d.setHours(12, 0, 0, 0);
  // setHours 落回同一个（已过去的）12:00 时，再往后推一天
  while (d.getTime() / 1000 <= Date.now() / 1000) {
    d.setDate(d.getDate() + 1);
    d.setHours(12, 0, 0, 0);
  }
  return d.getTime() / 1000;
}

async function refreshQuota() {
  try {
    const r = await api('/api/quota');
    const badge = $('quotaBadge');
    if (r.limit != null && !r.stale) {
      r._fetchedAt = Date.now() / 1000;
      window._quotaData = r;
      window._quotaStaleData = null;
      if (badge) {
        badge.classList.remove('stale');
        badge.title = '每日回测配额（响应头真实值）';
      }
      renderQuota();
    } else if (r.stale) {
      renderQuotaStale(r);
    } else {
      window._quotaData = null;
      window._quotaStaleData = null;
      $('quotaValue').textContent = '--';
      $('quotaSub').textContent = `本周期 ${r.used_local || 0} 次`;
      if (badge) {
        badge.classList.remove('stale');
        badge.title = '暂无平台配额读数；显示本配额周期（北京 12:00 起算）内本地提交的 sim 数';
      }
    }
  } catch (e) { /* 静默：配额显示失败不干扰主流程 */ }
}

// 60s 拉一次真实值 + 每秒本地倒计时
setInterval(refreshQuota, 60000);
setInterval(renderQuota, 1000);
refreshQuota();

// 实时回测并发状态（在飞批次 / 已用槽 / 限流）
async function refreshSchedulerState() {
  try {
    const r = await api('/api/scheduler/state');
    if (!r.ok) return;
    $('liveBatches').textContent = r.active_batches;
    $('liveConcurrent').textContent = r.concurrent;
    $('liveSlotsUsed').textContent = r.slots_used;
    $('liveSlotsTotal').textContent = r.sim_slots;
    const pct = r.sim_slots > 0 ? Math.min(100, Math.round(r.slots_used / r.sim_slots * 100)) : 0;
    const bar = $('liveSlotsBar');
    if (bar) bar.style.width = pct + '%';
    const chip = $('liveRate');
    const txt = $('liveRateText');
    if (r.rate_limited) {
      chip.classList.add('limited');
      txt.textContent = `限流 ${r.rate_limit_reset_sec}s`;
    } else {
      chip.classList.remove('limited');
      txt.textContent = r.running ? '运行中' : '空闲';
    }
  } catch (e) { /* 静默：实时状态失败不干扰主流程 */ }
}
setInterval(refreshSchedulerState, 1500);
refreshSchedulerState();

// 批次列表自动轮询：覆盖 MCP 独立进程提交的 AI 批次（web 收不到其 WS 进度事件）
setInterval(() => {
  const c = $('batchList');
  const top = c ? c.scrollTop : 0;
  loadBatches(true).then(() => { if (c) c.scrollTop = top; });
}, 4000);

// 打开的批次详情：实时进度（只更新进度条元素，不整页重渲染，避免清空提交框输入）
async function refreshDetailProgress() {
  const no = state.currentBatch;
  if (!no) return;
  try {
    const r = await api('/api/batches/' + no + '/progress');
    if (!r.ok) return;
    // 批次跑完（completed/failed）且之前在跑 -> 整页刷新看回填结果
    if ((r.status === 'completed' || r.status === 'failed') && state._detailRunning) {
      state._detailRunning = false;
      loadBatchDetail(no);
      return;
    }
    state._detailRunning = (r.status === 'running');
    const fill = $('detailProgressFill');
    const txt = $('detailProgressText');
    if (fill) fill.style.width = r.pct + '%';
    if (txt) txt.textContent = `完成 ${r.completed} / 失败 ${r.failed} / 总 ${r.total} (${r.pct}%)`;
  } catch (e) { /* 静默 */ }
}
setInterval(refreshDetailProgress, 2500);

// ====== Batches ======
async function loadBatches(silent = false) {
  try {
    const r = await api('/api/batches?limit=100');
    state.batches = r.batches;
    renderBatchList();
  } catch (e) { if (!silent) toast('加载批次失败：' + e.message, 'error'); }
}

function renderBatchList() {
  // diff 保护：批次数据与选中态都没变时跳过重绘，避免 4 秒轮询无谓重建 DOM（闪烁 + 小 GC）
  const key = (state.currentBatch || '') + '|' + state.batches.map(b =>
    [b.batch_no, b.status, b.sim_mode, b.sim_done, b.sim_total, b.sim_completed, b.sim_failed,
     b.success, b.failed, b.total, b.expression_count].join(':')
  ).join('|');
  if (key === state._lastListKey) return;
  state._lastListKey = key;
  const container = $('batchList');
  if (state.batches.length === 0) {
    container.innerHTML = '<div class="muted">暂无批次</div>';
    return;
  }
  container.innerHTML = state.batches.map(b => {
    const total = b.sim_total || b.total || b.expression_count || 0;
    const done = (b.sim_done != null ? b.sim_done : (b.success || 0) + (b.failed || 0));
    const pct = total > 0 ? Math.min(100, done / total * 100) : 0;
    const cls = b.status === 'completed' ? 'completed' : b.status === 'failed' ? 'failed' : '';
    const selected = state.currentBatch === b.batch_no ? 'selected' : '';
    return `
      <div class="batch-item ${selected}" data-batch="${b.batch_no}">
        <div class="batch-item-header">
          <span class="batch-no">${b.batch_no}</span>
          ${b.sim_mode === 'QUICK' ? '<span class="mode-badge mode-quick" title="Quick Simulate 初筛批次：仅供筛选，入围后需以 FULL 复验才能提交">QUICK</span>' : ''}
          ${(b.note || '').startsWith('SUPER') ? '<span class="mode-badge mode-super" title="SuperAlpha 批次：selection+combo 独立通道">SUPER</span>' : ''}
          ${b.region === 'ALL' ? '<span class="mode-badge mode-ra" title="Region Agnostic 批次（ARC2026 RA，入库 Child）">RA</span>' : ''}
          <span class="status-badge status-${b.status}">${b.status}</span>
        </div>
        <div class="batch-meta">${b.region || '—'} · ${b.expression_count || 0} 条 · ${b.producer || ''}</div>
        <div class="batch-meta">${fmtCST(b.created_at)}</div>
        ${total > 0 ? `
          <div class="progress-bar"><div class="progress-bar-fill ${cls}" style="width:${pct.toFixed(1)}%"></div></div>
          <div class="batch-meta" style="margin-top:4px">${done}/${total} (${pct.toFixed(0)}%)</div>
        ` : ''}
      </div>
    `;
  }).join('');
  container.querySelectorAll('.batch-item').forEach(el => {
    el.addEventListener('click', () => selectBatch(el.dataset.batch));
  });
}

async function selectBatch(batchNo) {
  state.currentBatch = batchNo;
  renderBatchList();
  await loadBatchDetail(batchNo);
}

async function loadBatchDetail(batchNo) {
  $('batchDetailTitle').textContent = `📄 批次详情：${batchNo}`;
  $('batchDetail').innerHTML = '<div class="muted">加载中…</div>';
  try {
    const r = await api('/api/batches/' + batchNo);
    const { batch, task_run, alphas, simulations } = r;
    const isSuper = !!r.is_super;
    const ups = r.upgrades || [];

    const statusClass = batch.status === 'completed' ? 'check-pass' :
                        batch.status === 'failed' ? 'check-fail' : 'check-warn';

    // 实时进度以 simulations 表逐条状态为准（task_run.success/failed 仅批次结束才写）
    const sims = simulations || [];
    let simTotal = sims.length || (task_run ? (task_run.total || 0) : 0);
    let simCompleted = sims.filter(s => s.status === 'completed').length;
    let simFailed = sims.filter(s => ['failed', 'cancelled', 'error'].includes(s.status)).length;
    // SUPER 批次不写 simulations 表 → 用 alphas 回填数当进度（与后端 /progress 兜底同口径）
    if (simTotal === 0 && (batch.expression_count || 0) > 0) {
      simTotal = batch.expression_count;
      simCompleted = alphas.length;
      simFailed = 0;
    }
    const simDone = simCompleted + simFailed;
    const total = simTotal;
    const success = simCompleted, failed = simFailed;
    const pct = total > 0 ? Math.min(100, simDone / total * 100) : 0;
    state._detailRunning = (batch.status === 'running');

    let html = `
      <div class="detail-section">
        <h3>基本信息</h3>
        <div class="kv-grid">
          <div><span>批次编号</span><span style="font-family:monospace;font-size:15px">${batch.batch_no}</span></div>
          <div><span>状态</span><span class="${statusClass}" style="font-size:14px;font-weight:600">${batch.status}</span></div>
          <div><span>创建者</span><span style="font-size:14px">${batch.producer || '—'}</span></div>
          <div><span>区域</span><span style="font-size:14px">${batch.region || '—'}</span></div>
          <div><span>数据集</span><span style="font-size:14px">${batch.dataset_id || '—'}</span></div>
          <div><span>表达式数</span><span style="font-size:14px">${batch.expression_count || 0}</span></div>
          <div><span>模式</span><span>${isSuper ? '<span class="mode-badge mode-super">SUPER</span>' : (batch.region === 'ALL' ? '<span class="mode-badge mode-ra">RA</span>' : (batch.sim_mode === 'QUICK' ? '<span class="mode-badge mode-quick">QUICK</span>' : '<span class="mode-badge mode-full">FULL</span>'))}</span></div>
          <div><span>创建时间</span><span style="font-size:13px">${fmtCST(batch.created_at)}</span></div>
          <div><span>备注</span><span style="font-size:13px">${batch.note || '—'}</span></div>
        </div>
      </div>
    `;

    if (isSuper) {
      const sa = alphas.find(a => a.selection) || {};
      html += `
        <div class="detail-section">
          <h3>SuperAlpha 结构</h3>
          <div class="sa-grid">
            <div class="sa-card"><div class="sa-label">selection（组件筛选）</div><pre class="sa-expr">${escapeHtml(sa.selection || '—')}</pre></div>
            <div class="sa-card"><div class="sa-label">combo（组合权重）</div><pre class="sa-expr">${escapeHtml(sa.combo || '—')}</pre></div>
          </div>
          <div class="muted" style="margin-top:8px">独立通道：预筛组件 ≥10 才提交 · 并发 ≤3 · 跨批按 selection+combo+settings 去重</div>
        </div>
      `;
    }

    if (total > 0) {
      const barCls = batch.status === 'completed' ? 'completed' : batch.status === 'failed' ? 'failed' : '';
      html += `
        <div class="detail-section">
          <h3>进度</h3>
          <div class="progress-bar" style="height:8px"><div class="progress-bar-fill ${barCls}" id="detailProgressFill" style="width:${pct.toFixed(1)}%"></div></div>
          <div id="detailProgressText" style="margin-top:8px; color:var(--text-dim); font-size:13px">
            完成 <span style="color:var(--green)">${success}</span> / 失败 <span style="color:var(--red)">${failed}</span> / 总 ${total} (${pct.toFixed(1)}%)
          </div>
        </div>
      `;
    }

    if (batch.sim_mode === 'QUICK') {
      html += `
        <div class="detail-section">
          <h3>QUICK → FULL 漏斗</h3>
          <div class="funnel-grid">
            <div class="funnel-card"><div class="funnel-label">① 本批初筛</div><div class="funnel-num">${alphas.length}</div></div>
            <div class="funnel-card"><div class="funnel-label">② 过阈值（实时）</div><div class="funnel-num" id="fnPassed">${alphas.filter(a => Math.abs(a.sharpe || 0) >= 1 && (a.fitness || 0) >= 1).length}</div></div>
            <div class="funnel-card"><div class="funnel-label">③ 已升级 FULL</div><div class="funnel-num">${ups.length}</div>${ups.map(u => `<a class="funnel-link" href="#" data-b="${u.batch_no}">${u.batch_no}（${u.status}）</a>`).join('')}</div>
            <div class="funnel-card"><div class="funnel-label">④ 提交保护</div><div class="funnel-note">未复验的 QUICK alpha<br>提交将被 409 拦截</div></div>
          </div>
          <div class="form-row" style="margin-top:12px">
            <label>|Sharpe| ≥ <input type="number" id="upSharpe" value="1" step="0.1" style="width:90px"></label>
            <label>Fitness ≥ <input type="number" id="upFitness" value="1" step="0.1" style="width:90px"></label>
            <button class="btn btn-primary" id="btnUpgrade">筛选并升级 FULL</button>
            <span class="muted" id="upStatus" style="font-size:12px"></span>
          </div>
        </div>
      `;
    }

    if (alphas.length > 0) {
      const avgSharpe = alphas.reduce((s, a) => s + (a.sharpe || 0), 0) / alphas.length;
      const topSharpe = Math.max(...alphas.map(a => Math.abs(a.sharpe || 0)));
      const hasCorr = alphas.some(a => a.max_corr != null);
      html += `
        <div class="detail-section">
          <h3>回填结果（${alphas.length} 个 alpha）</h3>
          <div class="kv-grid">
            <div><span>平均 Sharpe</span><span>${fmtSharpe(avgSharpe)}</span></div>
            <div><span>最高 |Sharpe|</span><span>${topSharpe.toFixed(2)}</span></div>
            <div><span>已算 Corr</span><span>${alphas.filter(a => a.max_corr != null).length}/${alphas.length}</span></div>
          </div>
          <table class="data-table" style="margin-top:12px">
            <thead><tr>
              <th>Alpha ID <span class="muted" style="font-size:10px">(点跳官网)</span></th>
              <th>Sharpe</th><th>Fitness</th><th>Turnover</th><th>Margin (bps)</th>
              ${hasCorr ? '<th>Max Corr</th>' : ''}
              <th>Region</th><th>Neut</th><th>Check</th>
            </tr></thead>
            <tbody>${alphas.map(a => {
              const mc = a.max_corr, mcSrc = a.max_corr_source || '';
              let mcClass = 'check-pass';
              if (mc != null) { if (mc >= 0.7) mcClass = 'check-fail'; else if (mc >= 0.5) mcClass = 'check-warn'; }
              const colCount = 8 + (hasCorr ? 1 : 0);
              const payload = JSON.stringify({ expression: a.expression || '', settings_json: a.settings_json || null });
              return `<tr class="alpha-row" data-cols="${colCount}" data-payload="${payload.replace(/"/g, '&quot;')}" title="点击展开完整表达式">
                <td>${alphaLink(a.alpha_id)}</td>
                <td>${fmtSharpe(a.sharpe)}</td>
                <td>${fmtNum(a.fitness)}</td>
                <td>${fmtNum(a.turnover, 3)}</td>
                <td>${fmtBps(a.margin)}</td>
                ${hasCorr ? `<td class="${mcClass}" title="${mcSrc ? '来源: ' + mcSrc : '无 corr 数据'}">${mc == null ? '—' : mc.toFixed(3) + (mcSrc ? ' (' + mcSrc + ')' : '')}</td>` : ''}
                <td>${a.region || '—'}</td>
                <td>${a.neutralization || '—'}</td>
                <td class="${a.check_status === 'pass' ? 'check-pass' : a.check_status === 'warn' ? 'check-warn' : 'check-fail'}">${a.check_status || '—'}</td>
              </tr>`;
            }).join('')}</tbody>
          </table>
        </div>
      `;
    } else {
      html += '<div class="detail-section"><h3>回填结果</h3><div class="muted">暂无 alpha 结果（批次可能还在跑，或回填未完成）</div></div>';
    }

    $('batchDetail').innerHTML = html;
    // v82 第三刀：漏斗阈值实时刷新 + 升级复验 + 升级链跳转
    const upBtn = $('btnUpgrade');
    if (upBtn) {
      const refreshPassed = () => {
        const el = $('fnPassed');
        if (!el) return;
        const mas = parseFloat($('upSharpe').value) || 1;
        const mfit = parseFloat($('upFitness').value) || 1;
        el.textContent = alphas.filter(a =>
          Math.abs(a.sharpe || 0) >= mas && (a.fitness || 0) >= mfit).length;
      };
      $('upSharpe').addEventListener('input', refreshPassed);
      $('upFitness').addEventListener('input', refreshPassed);
      upBtn.addEventListener('click', async () => {
        const th = {
          min_abs_sharpe: parseFloat($('upSharpe').value) || 1,
          min_fitness: parseFloat($('upFitness').value) || 1,
        };
        $('upStatus').textContent = '提交中…';
        try {
          const rr = await api(`/api/batches/${batchNo}/upgrade`, {
            method: 'POST',
            body: JSON.stringify({ thresholds: th }),
          });
          if (rr.batch_no) {
            toast(`✅ 升级 ${rr.promoted} 条 → ${rr.batch_no}（阈值跳过 ${rr.skipped_threshold} / 去重 ${rr.skipped_dup}）`, 'success');
            loadBatches();
            setTimeout(() => selectBatch(rr.batch_no), 600);
          } else {
            $('upStatus').textContent = rr.message || '无可升级';
          }
        } catch (e) {
          $('upStatus').textContent = '';
          toast('升级失败：' + e.message, 'error');
        }
      });
    }
    $('batchDetail').querySelectorAll('.funnel-link').forEach(el => {
      el.addEventListener('click', (ev) => {
        ev.preventDefault();
        selectBatch(el.dataset.b);
      });
    });
  } catch (e) {
    $('batchDetail').innerHTML = `<div class="muted">加载失败：${e.message}</div>`;
  }
}

$('refreshBatches').addEventListener('click', loadBatches);

// ====== 并发设置（全局，点确认才生效） ======
async function loadConcurrency() {
  try {
    const r = await api('/api/concurrency');
    $('cfgConcurrent').value = r.concurrent;
    $('cfgSimSlots').value = r.sim_slots;
  } catch (e) { /* 忽略，用 HTML 默认值 */ }
}

$('btnApplyConcurrency').addEventListener('click', async () => {
  const concurrent = parseInt($('cfgConcurrent').value);
  const sim_slots = parseInt($('cfgSimSlots').value);
  if (!concurrent || !sim_slots || concurrent < 1 || sim_slots < 1) {
    toast('并发批数和并发槽都要 ≥ 1', 'error');
    return;
  }
  $('cfgStatus').textContent = '// 应用中…';
  try {
    const r = await api('/api/concurrency', {
      method: 'POST',
      body: JSON.stringify({ concurrent, sim_slots }),
    });
    $('cfgStatus').textContent = `// 已生效：${r.concurrent} 批 × ${r.sim_slots} 槽`;
    toast(`⚙️ 并发设置已生效：${r.concurrent} 批 × ${r.sim_slots} 槽`, 'success');
  } catch (e) {
    toast('应用失败：' + e.message, 'error');
    $('cfgStatus').textContent = '// 全局生效，下次提交即用';
  }
});

// ====== Submit ======
$('btnSubmit').addEventListener('click', async () => {
  const raw = $('submitJson').value.trim();
  if (!raw) { toast('请粘贴 JSON', 'error'); return; }
  let parsed;
  try { parsed = JSON.parse(raw); }
  catch (e) { toast('JSON 解析失败：' + e.message, 'error'); return; }

  const payload = {
    expressions: parsed.expressions || [],
    settings: parsed.settings || {},
    producer: $('submitProducer').value || '阿法',
    no_backfill: $('submitNoBackfill').checked,
  };
  if (payload.expressions.length === 0) { toast('expressions 不能为空', 'error'); return; }

  $('submitStatus').textContent = '提交中…';
  try {
    const r = await api('/api/batches', { method: 'POST', body: JSON.stringify(payload) });
    if (r.batch_no) {
      toast(`✅ 批次 ${r.batch_no} 已启动：提交 ${r.submitted} 条，跳过 ${r.skipped}`, 'success');
      $('submitStatus').textContent = '';
      loadBatches();
      setTimeout(() => selectBatch(r.batch_no), 500);
    } else {
      toast(r.message || '提交完成', 'success');
      $('submitStatus').textContent = '';
    }
  } catch (e) {
    toast('提交失败：' + e.message, 'error');
    $('submitStatus').textContent = '';
  }
});

// ====== Alpha List ======
// ====== PnL 同步控制 ======
const PLATFORM_BASE = 'https://platform.worldquantbrain.com/alpha/';

function alphaLink(id) {
  return `<a href="${PLATFORM_BASE}${id}" target="_blank" rel="noopener" class="alpha-id" title="点击跳官网 alpha 详情">${id}</a><button class="copy-btn" data-copy="${id}" title="复制 ID">⧉</button>`;
}

// 复制按钮（事件委托，全局一次）
document.addEventListener('click', async (e) => {
  const btn = e.target.closest('.copy-btn');
  if (!btn) return;
  const text = btn.dataset.copy;
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    const old = btn.textContent;
    btn.textContent = '✓';
    btn.classList.add('copied');
    setTimeout(() => { btn.textContent = old; btn.classList.remove('copied'); }, 1200);
  } catch (err) {
    // fallback：临时 textarea + execCommand
    const ta = document.createElement('textarea');
    ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); } catch (_) {}
    document.body.removeChild(ta);
    btn.textContent = '✓';
    setTimeout(() => { btn.textContent = '⧉'; }, 1200);
  }
});

// 回填结果行：点击展开/收起完整表达式 + settings（事件委托，全局一次）
document.addEventListener('click', (e) => {
  // 点链接跳官网、点复制按钮时不触发展开
  if (e.target.closest('.copy-btn') || e.target.closest('a.alpha-id')) return;
  const row = e.target.closest('tr.alpha-row');
  if (!row) return;
  const next = row.nextElementSibling;
  if (next && next.classList.contains('expr-detail-row')) {
    next.remove();
    row.classList.remove('expanded');
    return;
  }
  let data = {};
  try { data = JSON.parse(row.dataset.payload || '{}'); } catch (_) { data = {}; }
  const expr = data.expression || '（无表达式）';
  let settingsText = '（无 settings 记录）';
  if (data.settings_json) {
    try { settingsText = JSON.stringify(JSON.parse(data.settings_json), null, 2); }
    catch (_) { settingsText = String(data.settings_json); }
  }
  const cols = row.dataset.cols || '9';
  const detail = document.createElement('tr');
  detail.className = 'expr-detail-row';
  detail.innerHTML = `<td colspan="${cols}">
    <div class="expr-detail-box">
      <div class="expr-detail-label">表达式</div>
      <pre class="expr-detail-code">${escapeHtml(expr)}</pre>
      <div class="expr-detail-label">Settings</div>
      <pre class="expr-detail-code">${escapeHtml(settingsText)}</pre>
    </div>
  </td>`;
  row.after(detail);
  row.classList.add('expanded');
});

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

async function refreshSyncStatus() {
  try {
    const r = await api('/api/sync/pnl/status');
    const running = r.running;
    state._syncRunning = running;
    const prog = r.progress || {};
    const phase = prog.phase || '';
    const cur = prog.current || 0, tot = prog.total || 0;
    const msg = prog.message || '';
    const success = prog.success || 0, failed = prog.failed || 0;
    // 标题 + 描述
    $('syncStatus').textContent = running ? '运行中…' : (phase === 'completed' ? '✅ 已完成' : (phase === 'error' ? '❌ 出错' : '未运行'));
    $('syncProgress').textContent = msg || (running ? '进行中…' : '空闲');
    // 仪表盘 SVG
    const pct = tot > 0 ? Math.min(100, cur / tot * 100) : (phase === 'completed' ? 100 : 0);
    const circumference = 2 * Math.PI * 52; // r=52
    const gauge = $('syncGaugeFg');
    if (gauge) {
      gauge.setAttribute('stroke-dashoffset', circumference * (1 - pct / 100));
      gauge.classList.remove('completed', 'failed');
      if (phase === 'completed') gauge.classList.add('completed');
      else if (phase === 'error') gauge.classList.add('failed');
    }
    $('syncPercent').textContent = Math.round(pct) + '%';
    const phaseMap = { alphas: 'SCAN', 'pnl': 'PNL', writing: 'WRITE', corr: 'CORR', completed: 'DONE', error: 'FAIL' };
    $('syncPhaseLabel').textContent = phaseMap[phase] || 'READY';
    // live 标签
    const liveTag = $('syncLiveTag');
    if (liveTag) {
      liveTag.className = running ? 'live-tag on' : 'live-tag';
      liveTag.lastChild.textContent = running ? 'LIVE' : 'IDLE';
    }
    // 结果三联
    if (r.last_result && r.last_result.total) {
      $('syncResult').style.display = 'grid';
      $('syncResultTotal').textContent = r.last_result.total;
      $('syncResultCorr').textContent = r.last_result.corr_computed || 0;
      $('syncResultFail').textContent = (r.last_result.failed_ids || []).length;
    }
  } catch (e) { /* 忽略 */ }
  return !!state._syncRunning;
}

function renderSyncResult(r) {
  const el = $('syncResult');
  if (!r || !r.total) { el.innerHTML = ''; return; }
  const reasons = r.failed_reasons || [];
  let html = `
    <div class="kv-grid">
      <div><span>总数</span><span>${r.total}</span></div>
      <div><span>成功</span><span style="color:var(--green)">${r.success}</span></div>
      <div><span>失败</span><span style="color:var(--red)">${(r.failed_ids || []).length}</span></div>
      <div><span>Corr 计算</span><span>${r.corr_computed || 0}</span></div>
    </div>
  `;
  if (reasons.length > 0) {
    html += `<h3 style="margin-top:14px;font-size:13px;color:var(--text-dim)">失败原因（按频率排序）</h3>`;
    html += `<table class="data-table"><thead><tr><th>模式</th><th>次数</th><th>样例 ID</th></tr></thead><tbody>`;
    for (const fr of reasons.slice(0, 10)) {
      html += `<tr><td class="muted">${fr.pattern}</td><td>${fr.count}</td><td class="muted" style="font-size:12px">${fr.sample_ids.join(', ')}</td></tr>`;
    }
    html += `</tbody></table>`;
  }
  el.innerHTML = html;
}

$('btnStartSync').addEventListener('click', async () => {
  $('syncStatus').textContent = '启动中…';
  try {
    // 默认增量只拉未拉过的；勾选「强制全量」则重拉所有
    const forceFull = document.getElementById('syncForceFull')?.checked || false;
    const r = await api('/api/sync/pnl', { method: 'POST', body: JSON.stringify({ compute_corr: false, force_full: forceFull }) });
    if (!r.ok) { toast(r.error || '启动失败', 'error'); $('syncStatus').textContent = '未运行'; return; }
    toast('PnL 同步已启动' + (forceFull ? '（强制全量）' : '（增量）'), 'success');
    if (window._syncTimer) clearInterval(window._syncTimer);
    window._syncTimer = setInterval(refreshSyncStatus, 1500);
    refreshSyncStatus();
  } catch (e) {
    const msg = (e && e.message) ? e.message : '';
    // 刷新页面后点启动：后端已在跑，恢复进度轮询即可，不报错
    if (msg.includes('已在运行')) {
      if (window._syncTimer) clearInterval(window._syncTimer);
      window._syncTimer = setInterval(refreshSyncStatus, 1500);
      refreshSyncStatus();
      toast('同步已在后台运行，已恢复进度轮询', 'success');
    } else {
      toast('启动失败：' + msg, 'error');
      $('syncStatus').textContent = '未运行';
    }
  }
});
$('btnStopSync').addEventListener('click', async () => {
  try {
    await api('/api/sync/pnl/stop', { method: 'POST' });
    toast('已发送停止信号', 'success');
  } catch (e) { toast('停止失败：' + e.message, 'error'); }
});

// ====== 手动算 Self/PPA corr（按需触发） ======
$('btnComputeCorr').addEventListener('click', async () => {
  const alphaId = ($('corrAlphaId')?.value || '').trim();
  if (!alphaId) { toast('先填 alpha ID', 'error'); return; }
  const el = $('corrResult');
  const btn = $('btnComputeCorr');
  el.textContent = '计算中…';
  btn.disabled = true;
  try {
    const r = await api('/api/corr/compute', {
      method: 'POST',
      body: JSON.stringify({ alpha_id: alphaId }),
    });
    const max = r.max_corr;
    const src = r.max_corr_source || '—';
    const sm = r.self ? r.self.max : null;
    const pm = r.ppa ? r.ppa.max : null;
    const pool = r.pool_size ?? 0;
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
    btn.disabled = false;
  }
});

// ====== Alpha 备忘录 ======
const memoState = { rows: [], sortKey: null, sortDir: -1 };

async function loadMemos() {
  try {
    const r = await api('/api/memo');
    memoState.rows = r.memos;
    renderMemos();
  } catch (e) { toast('加载备忘录失败：' + e.message, 'error'); }
}

function memoSortedRows(rows) {
  const k = memoState.sortKey, dir = memoState.sortDir;
  if (!k) return rows;
  const sorted = [...rows].sort((a, b) => {
    const va = a[k], vb = b[k];
    if (va == null && vb == null) return 0;
    if (va == null) return 1;
    if (vb == null) return -1;
    if (typeof va === 'number' && typeof vb === 'number') return (va - vb) * dir;
    return String(va).localeCompare(String(vb)) * dir;
  });
  return sorted;
}

function renderMemos() {
  const el = $('memoGroups');
  const rows = memoState.rows;
  if (!rows.length) {
    el.innerHTML = '<div class="muted" style="padding:14px">暂无备忘录，先添加一个 alpha</div>';
    return;
  }
  // 预计算排序辅助字段
  for (const r of rows) r.themes_str = (r.themes_list || []).join('/');
  // 按 region 分组
  const groups = {};
  for (const r of rows) {
    const g = r.region || '未知';
    (groups[g] = groups[g] || []).push(r);
  }
  const sortKeys = [
    ['alpha_id', 'ID'], ['status', '状态'], ['sharpe', 'Sharpe'],
    ['fitness', 'Fitness'], ['turnover', 'Tvr'], ['margin', 'Margin'],
  ];
  const ind = (key) => memoState.sortKey === key
    ? `<span class="sort-ind">${memoState.sortDir === 1 ? '▲' : '▼'}</span>` : '';

  // 金字塔主题下拉选项（categories 对齐官网 pyramid 列表，value=主题缩写）
  const THEME_REGIONS = ['ASI', 'IND', 'USA', 'GLB', 'EUR', 'CHN', 'JPN', 'KOR', 'TWN', 'HKG', 'AMR'];
  const THEME_CATEGORIES = [
    { v: 'ANL', label: 'Analyst' },
    { v: 'BRO', label: 'Broker' },
    { v: 'EARN', label: 'Earnings' },
    { v: 'FUND', label: 'Fundamental' },
    { v: 'IMB', label: 'Imbalance' },
    { v: 'INS', label: 'Insiders' },
    { v: 'INST', label: 'Institutions' },
    { v: 'MACRO', label: 'Macro' },
    { v: 'MODEL', label: 'Model' },
    { v: 'NEWS', label: 'News' },
    { v: 'OPT', label: 'Option' },
    { v: 'OTHER', label: 'Other' },
    { v: 'PV', label: 'Price Volume' },
    { v: 'RISK', label: 'Risk' },
    { v: 'SENT', label: 'Sentiment' },
    { v: 'SI', label: 'Short Interest' },
    { v: 'SOCIAL', label: 'Social Media' },
  ];

  let html = '';
  for (const [region, groupRows] of Object.entries(groups)) {
    const sorted = memoSortedRows(groupRows);
    html += `
      <div class="memo-group">
        <div class="memo-group-head">
          <span>${region}</span>
          <span class="memo-group-count">${groupRows.length} 个</span>
        </div>
        <table class="data-table memo-table">
          <thead><tr>
            ${sortKeys.map(([k, label]) => `<th data-sort="${k}">${label}${ind(k)}</th>`).join('')}
            <th>金字塔主题（手动）${ind('themes_str')}</th>
            <th>备注</th>
            <th></th>
          </tr></thead>
          <tbody>
            ${sorted.map(r => {
              const themeChips = (r.themes_list || []).map((t, ti) =>
                `<span class="theme-chip">${t}<button class="theme-del" data-theme-idx="${ti}" data-theme-id="${r.alpha_id}" title="删除此主题">✕</button></span>`
              ).join('');
              const regionOpts = THEME_REGIONS.map(rg =>
                `<option value="${rg}" ${rg === r.region ? 'selected' : ''}>${rg}</option>`
              ).join('');
              const catOpts = THEME_CATEGORIES.map(c =>
                `<option value="${c.v}">${c.label}</option>`
              ).join('');
              return `
              <tr>
                <td><span class="alpha-id-wrap">${alphaLink(r.alpha_id)}</span></td>
                <td><span class="status-badge status-${(r.status || '').toLowerCase().startsWith('active') ? 'completed' : 'pending'}">${r.status || '—'}</span></td>
                <td>${fmtSharpe(r.sharpe)}</td>
                <td>${fmtNum(r.fitness)}</td>
                <td>${fmtNum(r.turnover, 3)}</td>
                <td>${fmtBps(r.margin)}</td>
                <td class="themes-cell" style="min-width:280px">
                  <div style="display:flex;flex-wrap:wrap;gap:4px;margin-bottom:4px">${themeChips || '<span class="muted" style="font-size:11px">无主题</span>'}</div>
                  <div style="display:flex;gap:4px;align-items:center">
                    <select class="theme-sel" data-theme-region="${r.alpha_id}">${regionOpts}</select>
                    <span style="color:var(--text-mute);font:500 11px var(--mono)">/D1/</span>
                    <select class="theme-sel" data-theme-cat="${r.alpha_id}">${catOpts}</select>
                    <button class="btn-mini theme-add" data-add-id="${r.alpha_id}" title="添加主题">＋</button>
                    <span class="theme-preview" data-preview-id="${r.alpha_id}" style="font:500 11px var(--mono);color:var(--text-mute)"></span>
                  </div>
                </td>
                <td style="min-width:160px">
                  <input class="memo-note-input" data-note-id="${r.alpha_id}" value="${(r.note || '').replace(/"/g, '&quot;')}" placeholder="备注…">
                </td>
                <td><button class="btn-mini memo-del" data-del-id="${r.alpha_id}" title="移除">✕</button></td>
              </tr>`;
            }).join('')}
          </tbody>
        </table>
      </div>
    `;
  }
  el.innerHTML = html;

  // 表头排序
  el.querySelectorAll('th[data-sort]').forEach(th => {
    th.addEventListener('click', () => {
      const k = th.dataset.sort;
      if (memoState.sortKey === k) { memoState.sortDir *= -1; }
      else { memoState.sortKey = k; memoState.sortDir = -1; }
      renderMemos();
    });
  });
  // 备注保存（失焦即存）
  el.querySelectorAll('.memo-note-input').forEach(inp => {
    inp.addEventListener('change', async () => {
      try {
        await api('/api/memo/note', { method: 'POST', body: JSON.stringify({ alpha_id: inp.dataset.noteId, note: inp.value }) });
        const row = memoState.rows.find(r => r.alpha_id === inp.dataset.noteId);
        if (row) row.note = inp.value;
        toast('备注已保存', 'success');
      } catch (e) { toast('备注保存失败：' + e.message, 'error'); }
    });
  });
  // 主题预览：下拉一变就显示拼好的完整主题串
  function refreshThemePreview(alphaId) {
    const region = el.querySelector(`select[data-theme-region="${alphaId}"]`).value;
    const cat = el.querySelector(`select[data-theme-cat="${alphaId}"]`).value;
    const prev = el.querySelector(`[data-preview-id="${alphaId}"]`);
    if (prev) prev.textContent = `${region}/D1/${cat}`;
  }
  el.querySelectorAll('select.theme-sel').forEach(sel => {
    sel.addEventListener('change', () => {
      const id = sel.dataset.themeRegion || sel.dataset.themeCat;
      refreshThemePreview(id);
    });
  });
  el.querySelectorAll('[data-preview-id]').forEach(p => {
    refreshThemePreview(p.dataset.previewId);
  });
  // 主题添加：拼 {region}/D1/{category}，追加保存
  el.querySelectorAll('.theme-add').forEach(btn => {
    btn.addEventListener('click', async () => {
      const alphaId = btn.dataset.addId;
      const region = el.querySelector(`select[data-theme-region="${alphaId}"]`).value;
      const cat = el.querySelector(`select[data-theme-cat="${alphaId}"]`).value;
      const theme = `${region}/D1/${cat}`;
      const row = memoState.rows.find(r => r.alpha_id === alphaId);
      if (!row) return;
      const list = row.themes_list || [];
      if (list.includes(theme)) { toast('这个主题已存在', 'error'); return; }
      const newThemes = [...list, theme];
      try {
        await api('/api/memo/themes', {
          method: 'POST',
          body: JSON.stringify({ alpha_id: alphaId, themes: newThemes }),
        });
        row.themes_list = newThemes;
        renderMemos();
        toast(`✅ 已加主题 ${theme}`, 'success');
      } catch (e) { toast('加主题失败：' + e.message, 'error'); }
    });
  });
  // 主题删除（chip 上的 ✕）
  el.querySelectorAll('.theme-del').forEach(btn => {
    btn.addEventListener('click', async () => {
      const alphaId = btn.dataset.themeId;
      const idx = parseInt(btn.dataset.themeIdx);
      const row = memoState.rows.find(r => r.alpha_id === alphaId);
      if (!row) return;
      const newThemes = (row.themes_list || []).filter((_, i) => i !== idx);
      try {
        await api('/api/memo/themes', {
          method: 'POST',
          body: JSON.stringify({ alpha_id: alphaId, themes: newThemes }),
        });
        row.themes_list = newThemes;
        renderMemos();
        toast('已删主题', 'success');
      } catch (e) { toast('删主题失败：' + e.message, 'error'); }
    });
  });
  // 删除
  el.querySelectorAll('.memo-del').forEach(btn => {
    btn.addEventListener('click', async () => {
      try {
        await api('/api/memo/' + btn.dataset.delId, { method: 'DELETE' });
        memoState.rows = memoState.rows.filter(r => r.alpha_id !== btn.dataset.delId);
        renderMemos();
        toast('已移除', 'success');
      } catch (e) { toast('移除失败：' + e.message, 'error'); }
    });
  });
}

$('btnMemoAdd').addEventListener('click', async () => {
  const id = ($('memoAddId').value || '').trim();
  if (!id) { toast('先填 alpha ID', 'error'); return; }
  $('btnMemoAdd').disabled = true;
  try {
    const r = await api('/api/memo', { method: 'POST', body: JSON.stringify({ alpha_id: id }) });
    toast(`✅ ${id} 已加入备忘录（${r.row.region}）`, 'success');
    $('memoAddId').value = '';
    loadMemos();
  } catch (e) { toast('添加失败：' + e.message, 'error'); }
  finally { $('btnMemoAdd').disabled = false; }
});
$('memoAddId').addEventListener('keydown', e => { if (e.key === 'Enter') $('btnMemoAdd').click(); });

$('btnMemoSync').addEventListener('click', async () => {
  const btn = $('btnMemoSync');
  btn.disabled = true;
  btn.textContent = '⟳ 同步中…';
  try {
    const r = await api('/api/sync/pnl/status', { method: 'GET' }).catch(() => null);
    const res = await api('/api/memo/sync', { method: 'POST', body: '{}' });
    memoState.rows = res.memos;
    renderMemos();
    toast(`✅ 同步完成：${res.synced} 成功 / ${res.failed} 失败`, 'success');
  } catch (e) { toast('同步失败：' + e.message, 'error'); }
  finally { btn.disabled = false; btn.textContent = '⟳ 同步状态'; }
});

$('btnMemoSubmit').addEventListener('click', async () => {
  const id = ($('memoSubmitId').value || '').trim();
  if (!id) { toast('先填 alpha ID', 'error'); return; }
  if (!confirm(`确认真实提交 ${id}？这是不可撤销操作。`)) return;
  const btn = $('btnMemoSubmit');
  btn.disabled = true;
  try {
    const r = await api('/api/memo/submit', { method: 'POST', body: JSON.stringify({ alpha_id: id }) });
    if (r.ok) {
      toast(`✅ ${id} 提交成功（HTTP ${r.status_code}）`, 'success');
    } else {
      toast(`⚠️ ${id} 提交返回 HTTP ${r.status_code}：${(r.message || '').slice(0, 120)}`, 'error');
    }
    $('memoSubmitId').value = '';
    loadMemos();
  } catch (e) { toast('提交失败：' + e.message, 'error'); }
  finally { btn.disabled = false; }
});

// ====== 提示词库 ======
const promptState = { list: [], editingId: null };

async function loadPrompts() {
  try {
    const r = await api('/api/prompts');
    promptState.list = r.prompts;
    renderPrompts();
  } catch (e) { /* 静默 */ }
}

function renderPrompts() {
  const el = $('promptList');
  if (!promptState.list.length) {
    el.innerHTML = '<div class="muted" style="padding:8px 0">暂无提示词，点右上角新建</div>';
    return;
  }
  el.innerHTML = promptState.list.map(p => `
    <div class="prompt-item" data-pid="${p.id}" title="点击编辑">
      <span class="prompt-name">${p.name}</span>
      <span class="prompt-preview">${(p.content || '').slice(0, 120)}</span>
      <button class="btn-mini prompt-copy" data-copy-id="${p.id}" title="复制全文">⧉ 复制</button>
    </div>
  `).join('');

  // 点击条目 → 编辑弹窗
  el.querySelectorAll('.prompt-item').forEach(item => {
    item.addEventListener('click', (e) => {
      if (e.target.closest('.prompt-copy')) return; // 复制按钮不触发编辑
      const p = promptState.list.find(x => x.id === item.dataset.pid);
      if (p) openPromptModal(p.id);
    });
  });
  // 复制按钮
  el.querySelectorAll('.prompt-copy').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const p = promptState.list.find(x => x.id === btn.dataset.copyId);
      if (!p) return;
      try {
        await navigator.clipboard.writeText(p.content || '');
        const old = btn.textContent;
        btn.textContent = '✓ 已复制';
        setTimeout(() => { btn.textContent = old; }, 1200);
      } catch (err) { toast('复制失败', 'error'); }
    });
  });
}

function openPromptModal(id = null) {
  promptState.editingId = id;
  const p = id ? promptState.list.find(x => x.id === id) : null;
  $('promptModalName').value = p ? p.name : '';
  $('promptModalContent').value = p ? p.content : '';
  $('btnPromptDelete').style.display = p ? '' : 'none';
  $('promptModal').style.display = 'grid';
  $('promptModalName').focus();
}

function closePromptModal() {
  $('promptModal').style.display = 'none';
  promptState.editingId = null;
}

$('btnPromptNew').addEventListener('click', () => openPromptModal(null));
$('btnPromptModalClose').addEventListener('click', closePromptModal);
$('btnPromptModalCancel').addEventListener('click', closePromptModal);
// 点 overlay 空白处关闭
$('promptModal').addEventListener('click', (e) => {
  if (e.target === $('promptModal')) closePromptModal();
});

$('btnPromptSave').addEventListener('click', async () => {
  const name = $('promptModalName').value.trim();
  const content = $('promptModalContent').value;
  if (!name) { toast('名字不能为空', 'error'); return; }
  try {
    const r = await api('/api/prompts', {
      method: 'POST',
      body: JSON.stringify({ id: promptState.editingId, name, content }),
    });
    promptState.list = r.prompts;
    renderPrompts();
    closePromptModal();
    toast('提示词已保存', 'success');
  } catch (e) { toast('保存失败：' + e.message, 'error'); }
});

$('btnPromptDelete').addEventListener('click', async () => {
  if (!promptState.editingId) return;
  if (!confirm('确认删除这条提示词？')) return;
  try {
    const r = await api('/api/prompts', {
      method: 'POST',
      body: JSON.stringify({ delete_id: promptState.editingId }),
    });
    promptState.list = r.prompts;
    renderPrompts();
    closePromptModal();
    toast('已删除', 'success');
  } catch (e) { toast('删除失败：' + e.message, 'error'); }
});

// ====== Osmosis 分配器 ======
async function loadOsmosisRules() {
  try {
    const r = await api('/api/osmosis/rules');
    if (!r.ok) { $('osmosisRulesBody').innerHTML = `<div class="muted">规则加载失败：${r.error || '未知错误'}</div>`; return; }
    renderOsmosisRules(r.rules);
  } catch (e) {
    $('osmosisRulesBody').innerHTML = `<div class="muted">规则加载失败：${e.message}</div>`;
  }
}

function renderOsmosisRules(rules) {
  const listItems = (arr) => arr.map(x => `<li><strong>${x.title}：</strong>${x.content}</li>`).join('');
  const timelineItems = (arr) => arr.map(x => `<li><strong>${x.day}：</strong>${x.event}</li>`).join('');
  $('osmosisRulesBody').innerHTML = `
    <div style="font-size:13px;line-height:1.7;color:var(--text)">
      <p style="margin-bottom:12px;padding:10px;background:var(--bg-2);border-left:3px solid var(--accent);border-radius:4px">${rules.summary}</p>
      <h3 style="font-size:14px;color:var(--accent);margin:14px 0 8px">三条硬规则 + 两个时间点</h3>
      <ul style="padding-left:18px;margin:0 0 12px;color:var(--text-dim)">${listItems(rules.rules)}</ul>
      <h3 style="font-size:14px;color:var(--accent);margin:14px 0 8px">它怎么影响你的收入</h3>
      <ul style="padding-left:18px;margin:0 0 12px;color:var(--text-dim)">${listItems(rules.impact)}</ul>
      <h3 style="font-size:14px;color:var(--accent);margin:14px 0 8px">一次分配的时间线</h3>
      <ul style="padding-left:18px;margin:0 0 12px;color:var(--text-dim)">${timelineItems(rules.timeline)}</ul>
      <p style="margin-top:14px;padding:10px;background:var(--bg-2);border-radius:4px;color:var(--text-dim);font-size:12px">${rules.golden_rule}</p>
    </div>
  `;
}

async function loadOsmosisTracks() {
  // 动态填充赛道下拉框：硬编码 region 列表漏过 GLB、HKG，
  // 改为从平台拉账号实际有 alpha 的（region/delay）组合。
  const sel = $('osmosisRegion');
  try {
    const r = await api('/api/osmosis/tracks');
    if (!r.ok || !r.tracks || r.tracks.length === 0) return; // 降级：保留静态选项
    const prev = sel.value;
    sel.innerHTML = r.tracks.map(t => {
      const label = `${t.region} / D${t.delay} · 可分配 ${t.compensated}/${t.total}`;
      return `<option value="${t.region}" data-delay="${t.delay}">${label}</option>`;
    }).join('');
    const match = r.tracks.find(t => t.region === prev);
    if (match) sel.value = prev;
    $('osmosisDelay').value = String(match ? match.delay : r.tracks[0].delay);
  } catch (e) {
    // 静默降级，保留 HTML 里的静态选项
  }
}

$('osmosisRegion').addEventListener('change', () => {
  const opt = $('osmosisRegion').selectedOptions[0];
  if (opt && opt.dataset.delay) $('osmosisDelay').value = opt.dataset.delay;
});

async function previewOsmosis() {
  const region = $('osmosisRegion').value;
  const delay = parseInt($('osmosisDelay').value, 10);
  const useRealCorr = $('osmosisRealCorr').checked;
  const useRealData = $('osmosisRealData') ? $('osmosisRealData').checked : false;
  $('osmosisStatus').style.display = 'block';
  const busyNote = (useRealCorr || useRealData) ? '（真实数据模式，逐个 alpha 拉取，请耐心）' : '';
  $('osmosisStatusBody').innerHTML = `<span class="live-tag on" style="margin-right:8px">RUNNING</span> 正在计算 ${region}/D${delay} 的 Osmosis 分配方案…${busyNote}`;
  $('osmosisResult').style.display = 'none';
  try {
    const r = await api('/api/osmosis/preview', {
      method: 'POST',
      body: JSON.stringify({
        region,
        delay,
        fetch_external_correlations: useRealCorr,
        fetch_yearly_stats: useRealData,
        fetch_pnl_for_diversity: useRealData,
        fetch_alpha_details_for_os: useRealData,
      }),
    });
    if (!r.ok) {
      $('osmosisStatusBody').innerHTML = `<span class="live-tag" style="margin-right:8px;background:var(--neg-soft);color:var(--bad)">FAIL</span> ${r.error || '预览失败'}`;
      return;
    }
    renderOsmosisPlan(r);
    $('osmosisStatusBody').innerHTML = `<span class="live-tag on" style="margin-right:8px">DONE</span> ${r.scope} 分配方案已生成，共 ${r.selected_count} 个 alpha，总分 ${r.total_points.toLocaleString()}。`;
    $('osmosisResult').style.display = 'block';
  } catch (e) {
    $('osmosisStatusBody').innerHTML = `<span class="live-tag" style="margin-right:8px;background:var(--neg-soft);color:var(--bad)">FAIL</span> ${e.message}`;
  }
}

async function allocateOsmosis() {
  if (!$('osmosisConfirm').checked) {
    toast('请先勾选「确认写入」才能执行写入', 'error');
    return;
  }
  const region = $('osmosisRegion').value;
  const delay = parseInt($('osmosisDelay').value, 10);
  if (!confirm(`确认把 ${region}/D${delay} 的 Osmosis 方案写入平台？这会先清空本赛道旧分。`)) return;
  $('osmosisStatus').style.display = 'block';
  $('osmosisStatusBody').innerHTML = `<span class="live-tag on" style="margin-right:8px">WRITING</span> 正在写入 ${region}/D${delay}…`;
  try {
    const r = await api('/api/osmosis/allocate', {
      method: 'POST',
      body: JSON.stringify({ region, delay, confirm: true }),
    });
    if (!r.ok) {
      $('osmosisStatusBody').innerHTML = `<span class="live-tag" style="margin-right:8px;background:var(--neg-soft);color:var(--bad)">FAIL</span> ${r.error || '写入失败'}`;
      return;
    }
    const cleared = r.cleared || {};
    const writes = r.writes || [];
    const fails = writes.filter(w => !w.ok);
    let failHtml = '';
    if (fails.length > 0) {
      failHtml = `
        <div style="margin-top:12px;max-height:260px;overflow:auto;border:1px solid var(--border);border-radius:8px;padding:10px;background:var(--bg-1)">
          <div style="margin-bottom:8px;color:var(--text-dim);font-size:12px">失败详情（${fails.length} 个）：</div>
          ${fails.map(w => `
            <div style="margin-bottom:10px;font-family:var(--mono);font-size:12px;line-height:1.4">
              <div style="color:var(--bad)">${w.alpha_id}${w.status_code ? ` · HTTP ${w.status_code}` : ''}</div>
              <div style="color:var(--text-dim);white-space:pre-wrap">${(w.error || '').slice(0, 200)}${(w.response_body || '').slice(0, 200)}</div>
            </div>
          `).join('')}
        </div>
      `;
    }
    const abortMsg = r.aborted || cleared.aborted;
    let abortHtml = '';
    if (abortMsg) {
      abortHtml = `
        <div style="margin-top:12px;padding:10px 12px;border-radius:8px;background:var(--neg-soft);color:var(--bad);font-size:13px;line-height:1.5">
          ⚠️ ${abortMsg}
          <div style="margin-top:6px;color:var(--text-dim);font-size:12px">多半是短时间内登录/请求过于频繁，被平台限流并升级成验证码保护。请到浏览器打开 BRAIN 正常登录一次（有验证码先完成），等几分钟再重试。</div>
        </div>
      `;
    }
    $('osmosisStatusBody').innerHTML = `
      <span class="live-tag on" style="margin-right:8px">DONE</span>
      已写入 ${r.scope}：清空旧分 ${cleared.cleared || 0} 个，写入新分 ${r.written || 0} 个，失败 ${r.failed || 0} 个。
      ${abortHtml}
      ${failHtml}
    `;
  } catch (e) {
    $('osmosisStatusBody').innerHTML = `<span class="live-tag" style="margin-right:8px;background:var(--neg-soft);color:var(--bad)">FAIL</span> ${e.message}`;
  }
}

function renderOsmosisPlan(plan) {
  const totalOk = plan.total_assigned === plan.total_points;
  const metricCard = (label, value, sub) => `
    <div class="metric-card">
      <div class="metric-label">${label}</div>
      <div class="metric-value">${value}</div>
      ${sub ? `<div class="metric-sub">${sub}</div>` : ''}
    </div>
  `;
  $('osmosisOverview').innerHTML = `
    <div class="osmosis-metrics">
      ${metricCard('作用域', `<span style="font-family:var(--mono);font-size:15px">${plan.scope}</span>`, `候选 ${plan.candidate_count}`)}
      ${metricCard('入选 ALPHA', plan.selected_count, `REGULAR ${plan.regular_count} · SUPER ${plan.super_count}`)}
      ${metricCard('分配总分', `<span style="color:${totalOk ? 'var(--good)' : 'var(--bad)'}">${plan.total_assigned.toLocaleString()}</span>`, totalOk ? '已凑满' : `目标 ${plan.total_points.toLocaleString()}`)}
      ${metricCard('加权 SHARPE', fmtSharpe(plan.weighted_sharpe), `平均 ${fmtSharpe(plan.avg_sharpe)}`)}
      ${metricCard('加权 MARGIN', fmtNum(plan.weighted_margin, 4), `加权换手 ${fmtNum(plan.weighted_turnover, 3)}`)}
      ${(() => {
        const src = plan.correlation_source;
        const real = src === 'platform';
        const unavailable = src === 'unavailable';
        return metricCard(
          real ? '最大两两相关' : '最大代码相似度',
          fmtNum(plan.max_pairwise_corr, 3),
          real
            ? `平台真实相关性（${plan.correlation_fetched} 个 alpha）`
            : (unavailable ? '平台暂无相关性数据，退回结构相似' : '表达式结构相似，非 PnL 相关'),
        );
      })()}
      ${metricCard('当前已分配', plan.currently_assigned ?? 0, '个 alpha 有旧分')}
    </div>
  `;
  renderOsmosisTable(plan.selected);
  renderOsmosisChart(plan.selected);
}

function renderOsmosisTable(selected) {
  const html = selected.map(a => `
    <tr>
      <td style="font-family:var(--mono);font-size:12px">${a.alpha_id}</td>
      <td><span class="type-badge ${a.type === 'SUPER' ? 'super' : 'regular'}">${a.type === 'SUPER' ? 'SUPER' : 'REG'}</span></td>
      <td style="font-weight:600;color:var(--text)">${(a.osmosis_new ?? 0).toLocaleString()}</td>
      <td>${fmtNum(a.adjusted_quality, 3)}</td>
      <td>${fmtSharpe(a.sharpe)}</td>
      <td>${fmtNum(a.fitness)}</td>
      <td>${fmtNum(a.margin)}</td>
      <td>${fmtNum(a.turnover, 3)}</td>
      <td>${fmtNum(a.self_corr)}</td>
      <td>${fmtNum(a.prod_corr)}</td>
      <td><span class="muted">${a.selection_reason === 'filler' ? '补位' : '主选'}</span></td>
    </tr>
  `).join('');
  $('osmosisTableBody').innerHTML = html || '<tr><td colspan="11" class="muted" style="text-align:center;padding:14px">无数据</td></tr>';
}

function renderOsmosisChart(selected) {
  const chartEl = $('osmosisChart');
  chartEl.innerHTML = '<p style="margin:0 0 14px;font-size:12px;color:var(--text-mute)">条形长度 = 分配分数；颜色区分 Regular / Super。</p>';
  if (!selected || selected.length === 0) {
    chartEl.innerHTML += '<div class="muted">无数据</div>';
    return;
  }
  const maxPoints = Math.max(...selected.map(a => a.osmosis_new || 0));
  const rows = selected.map(a => {
    const pct = maxPoints > 0 ? (a.osmosis_new / maxPoints) * 100 : 0;
    const barClass = a.type === 'SUPER' ? 'osmosis-bar-super' : 'osmosis-bar-regular';
    return `
      <div class="osmosis-bar-row">
        <div class="osmosis-bar-label" title="${a.alpha_id}">${a.alpha_id}</div>
        <div class="osmosis-bar-track">
          <div class="osmosis-bar-fill ${barClass}" style="width:${pct.toFixed(1)}%"></div>
        </div>
        <div class="osmosis-bar-score">${(a.osmosis_new || 0).toLocaleString()}</div>
        <div class="osmosis-bar-metric">${fmtSharpe(a.sharpe)}</div>
      </div>
    `;
  }).join('');
  chartEl.innerHTML += `
    <div class="osmosis-chart-legend">
      <div><span class="osmosis-legend-dot regular"></span>Regular</div>
      <div><span class="osmosis-legend-dot super"></span>Super</div>
    </div>
    <div class="osmosis-chart-body">${rows}</div>
  `;
}

$('btnOsmosisPreview').addEventListener('click', previewOsmosis);
$('btnOsmosisAllocate').addEventListener('click', allocateOsmosis);
$('btnOsmosisRulesToggle').addEventListener('click', () => {
  const el = $('osmosisRules');
  const visible = el.style.display !== 'none';
  el.style.display = visible ? 'none' : 'block';
  $('btnOsmosisRulesToggle').innerHTML = visible
    ? '<svg class="ico" viewBox="0 0 16 16"><circle cx="8" cy="8" r="7"/><path d="M8 7v4M8 5h0"/></svg>Osmosis 是什么'
    : '<svg class="ico" viewBox="0 0 16 16"><circle cx="8" cy="8" r="7"/><path d="M5 8h6"/></svg>收起 Osmosis 规则';
});

// ====== Init ======
loadStats();
loadBatches();
loadConcurrency();
loadMemos();
loadPrompts();
loadOsmosisRules();
loadOsmosisTracks();
// 直达批次详情：/?batch=B... （收藏直达 & 无头截图用，260923 第三刀）
const _batchParam = new URLSearchParams(location.search).get('batch');
if (_batchParam) setTimeout(() => selectBatch(_batchParam), 500);
refreshSyncStatus().then(running => {
  // 刷新页面后若后端同步仍在运行，自动恢复进度轮询（定时器随页面销毁）
  if (running && !window._syncTimer) window._syncTimer = setInterval(refreshSyncStatus, 1500);
});
connectWS();

// ====== 锚点 active 状态跟随滚动 ======
// 物理顺序 → 导航锚点索引（导航：总览0 / PnL同步1 / AI批次2 / 备忘录3 / 设置4 / Osmosis5）
const _sectionAnchor = [
  ['sec-dashboard', 0],
  ['sec-concurrency', 1], // 同步内容在并发区里
  ['sec-batches', 2],
  ['sec-memo', 3],
  ['sec-osmosis', 5],
  ['sec-arc', 6],
];
const _sections = _sectionAnchor
  .map(([id]) => document.getElementById(id))
  .filter(Boolean);
window.addEventListener('scroll', () => {
  let activeIdx = 0;
  const y = window.scrollY + 100;
  for (let i = 0; i < _sections.length; i++) {
    if (_sections[i].offsetTop <= y) activeIdx = _sectionAnchor[i][1];
  }
  document.querySelectorAll('.anchors a').forEach((a, i) => {
    a.classList.toggle('active', i === activeIdx);
  });
}, { passive: true });

// ===================== ARC 回测（Region-Agnostic） =====================
// 平台语义：投递 type=REGION_AGNOSTIC → RA_PARENT（只有 checks）+ N 个 RA_CHILD（真实指标在各 region）
let arcInventory = [];
let arcRows = [];
let arcTimer = null;

function arcSetStatus(html, show = true) {
  $('arcStatus').style.display = show ? 'block' : 'none';
  $('arcStatusBody').innerHTML = html;
}

function arcTag(text, kind) {
  const bg = kind === 'on' ? 'var(--pos-soft)' : kind === 'bad' ? 'var(--neg-soft)' : 'var(--bg-3)';
  const fg = kind === 'on' ? 'var(--good)' : kind === 'bad' ? 'var(--bad)' : 'var(--text-dim)';
  return `<span class="live-tag" style="margin-right:8px;background:${bg};color:${fg}">${text}</span>`;
}

async function scanArcInventory() {
  arcSetStatus(arcTag('SCANNING', 'on') + '正在扫描已提交 alpha…');
  try {
    const r = await api('/api/arc/inventory');
    if (!r.ok) throw new Error(r.error || '扫描失败');
    arcInventory = r.items || [];
    renderArcInventory();
    renderArcRegions(r.regions || {});
    $('arcInvCount').textContent = `共 ${arcInventory.length} 条`;
    arcSetStatus(arcTag('DONE') + `扫描完成，${arcInventory.length} 条可重跑（OS + REGULAR）。`);
  } catch (e) {
    arcSetStatus(arcTag('FAIL', 'bad') + escapeHtml(e.message));
  }
}

function renderArcInventory() {
  const minS = parseFloat($('arcMinSharpe').value);
  const regs = arcSelectedRegions();
  const rows = arcInventory.filter(it => {
    if (!isNaN(minS) && (it.src_sharpe == null || it.src_sharpe < minS)) return false;
    if (regs.length && !regs.includes(String(it.src_region || '').toUpperCase())) return false;
    return true;
  });
  $('arcInvBody').innerHTML = rows.map(it => `
    <tr>
      <td><input type="checkbox" class="arc-pick" data-id="${escapeHtml(it.src_id)}"
                 style="width:14px;height:14px;accent-color:var(--accent)"></td>
      <td><a class="alpha-link" href="https://platform.worldquantbrain.com/alpha/${escapeHtml(it.src_id)}"
             target="_blank">${escapeHtml(it.src_id)}</a></td>
      <td style="font:500 12px var(--mono)">${escapeHtml(it.src_region)}/${escapeHtml(it.src_universe)}</td>
      <td>${fmtSharpe(it.src_sharpe)}</td>
      <td>${fmtNum(it.src_fitness)}</td>
      <td>${it.src_turnover == null ? '—' : (Number(it.src_turnover) * 100).toFixed(2) + '%'}</td>
      <td class="muted">${escapeHtml(it.date_submitted || '')}</td>
      <td class="mono-trunc" title="${escapeHtml(it.expr_head || '')}"
          style="max-width:380px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
        ${escapeHtml(it.expr_head || '')}…</td>
    </tr>`).join('') || '<tr><td colspan="8" class="muted" style="padding:14px">没有符合条件的 alpha</td></tr>';
}

function renderArcRegions(regions) {
  const box = $('arcRegions');
  const keys = Object.keys(regions).sort((a, b) => regions[b] - regions[a]);
  box.innerHTML = keys.map(k => `
    <label style="display:flex;gap:5px;align-items:center;cursor:pointer;color:var(--text-dim)">
      <input type="checkbox" class="arc-region" value="${escapeHtml(k)}"
             style="width:13px;height:13px;accent-color:var(--accent)">
      ${escapeHtml(k)}<span class="muted">${regions[k]}</span>
    </label>`).join('');
  box.querySelectorAll('.arc-region').forEach(cb => {
    cb.addEventListener('change', renderArcInventory);
  });
}

function arcSelectedRegions() {
  return Array.from(document.querySelectorAll('.arc-region:checked')).map(c => c.value.toUpperCase());
}

function arcPickedIds() {
  const picked = Array.from(document.querySelectorAll('.arc-pick:checked')).map(c => c.dataset.id);
  return picked.length ? picked : null;
}

async function startArc() {
  const body = {
    universe: $('arcUniverse').value,
    inherit_settings: $('arcInherit').checked,
    decay: $('arcInherit').checked ? null : parseInt($('arcDecay').value, 10),
    neutralization: $('arcInherit').checked ? null : $('arcNeutral').value,
    truncation: $('arcInherit').checked ? null : parseFloat($('arcTrunc').value),
    limit: parseInt($('arcLimit').value, 10),
    min_sharpe: parseFloat($('arcMinSharpe').value),
    regions: arcSelectedRegions(),
    src_ids: arcPickedIds(),
  };
  arcSetStatus(arcTag('STARTING', 'on') + '正在启动…');
  try {
    const r = await api('/api/arc/run', { method: 'POST', body: JSON.stringify(body) });
    if (!r.ok) throw new Error(r.error || '启动失败');
    toast(`ARC 已启动，计划 ${r.planned} 条`);
    arcSetStatus(arcTag('RUNNING', 'on') +
      `已启动：${r.planned} 条 · ALL/${r.universe} · checkpoint ${escapeHtml(String(r.checkpoint).split(/[\\/]/).pop())}`);
    if (!arcTimer) arcTimer = setInterval(pollArcStatus, 4000);
  } catch (e) {
    arcSetStatus(arcTag('FAIL', 'bad') + escapeHtml(e.message));
  }
}

async function pollArcStatus() {
  let r;
  try {
    r = await api('/api/arc/status');
  } catch (e) {
    return;
  }
  const p = r.progress || {};
  const done = p.done || 0, total = p.total || 0;
  const pct = total ? Math.round(done / total * 100) : 0;
  const bar = `<div style="height:4px;background:var(--bg-3);border-radius:2px;margin-top:8px;overflow:hidden">
      <div style="height:100%;width:${pct}%;background:var(--accent);transition:width .3s"></div></div>`;
  arcSetStatus(arcTag(r.running ? 'RUNNING' : 'IDLE', r.running ? 'on' : '') +
    escapeHtml(p.message || (r.running ? '运行中…' : '空闲')) + bar);
  if (!r.running && arcTimer) {
    clearInterval(arcTimer);
    arcTimer = null;
    refreshArcResults();
  }
}

async function runArcByIds() {
  const raw = $('arcIds').value.trim();
  if (!raw) { toast('请先填 alpha ID', 'warn'); return; }
  const ids = raw.split(/[\s,;]+/).map(s => s.trim()).filter(Boolean);
  $('arcIdsHint').textContent = `解析到 ${ids.length} 个 ID，正在拉取…`;
  const body = {
    alpha_ids: ids,
    universe: $('arcUniverse').value,
    inherit_settings: $('arcInherit').checked,
    decay: $('arcInherit').checked ? null : parseInt($('arcDecay').value, 10),
    neutralization: $('arcInherit').checked ? null : $('arcNeutral').value,
    truncation: $('arcInherit').checked ? null : parseFloat($('arcTrunc').value),
  };
  arcSetStatus(arcTag('RUNNING', 'on') + `正在按 ID 拉取 ${ids.length} 个 alpha…`);
  try {
    const r = await api('/api/arc/run-ids', { method: 'POST', body: JSON.stringify(body) });
    if (!r.ok) {
      const errs = (r.errors || []).map(e => `${e.src_id}: ${e.error}`).join('；');
      arcSetStatus(arcTag('FAIL', 'bad') + escapeHtml(r.error || '启动失败') +
        (errs ? `<div class="muted" style="margin-top:6px">${escapeHtml(errs)}</div>` : ''));
      return;
    }
    const errs = r.errors || [];
    $('arcIdsHint').textContent = `已启动 ${r.planned} 条` +
      (errs.length ? `（${errs.length} 条跳过）` : '');
    toast(`按 ID 重跑已启动，${r.planned} 条`);
    arcSetStatus(arcTag('RUNNING', 'on') +
      `已启动：${r.planned} 条 · ALL/${r.universe} · checkpoint ${escapeHtml(String(r.checkpoint).split(/[\\/]/).pop())}` +
      (errs.length
        ? `<div class="muted" style="margin-top:6px">跳过 ${errs.length} 条：` +
          escapeHtml(errs.map(e => `${e.src_id}(${e.error})`).join('；')) + '</div>'
        : ''));
    if (!arcTimer) arcTimer = setInterval(pollArcStatus, 4000);
  } catch (e) {
    $('arcIdsHint').textContent = '';
    arcSetStatus(arcTag('FAIL', 'bad') + escapeHtml(e.message));
  }
}

async function stopArc() {
  try {
    const r = await api('/api/arc/stop', { method: 'POST' });
    if (!r.ok) toast(r.error || '停止失败', 'warn');
    else toast('已发送停止信号');
  } catch (e) {
    toast(e.message, 'warn');
  }
}

async function refreshArcResults() {
  try {
    const r = await api('/api/arc/results');
    if (!r.ok) {
      $('arcResult').style.display = 'none';
      return;
    }
    arcRows = r.rows || [];
    renderArcResults(r);
    $('arcResult').style.display = 'block';
  } catch (e) {
    /* 尚无结果文件时静默 */
  }
}

function renderArcResults(data) {
  const rows = data.rows || [];
  $('arcOverview').innerHTML = `
    <div style="display:flex;gap:22px;flex-wrap:wrap;font-size:13px">
      <span>checkpoint <code>${escapeHtml(data.checkpoint || '')}</code></span>
      <span>更新 <b>${escapeHtml(data.updated_at || '')}</b></span>
      <span>共 <b>${data.count}</b> 条</span>
      <span class="sharpe-pos">COMPLETE <b>${data.complete}</b></span>
      <span class="sharpe-neg">失败 <b>${data.failed}</b></span>
      <span>子区域 F≥1.0 且 S≥1.58 的 <b>${data.qualified}</b> 条</span>
    </div>`;

  $('arcResBody').innerHTML = rows.map((r, i) => {
    const ok = r.status === 'COMPLETE';
    const childRows = (r.children || []).map(c => `
      <tr>
        <td></td>
        <td><a class="alpha-link" href="https://platform.worldquantbrain.com/alpha/${escapeHtml(c.child_id)}"
               target="_blank">${escapeHtml(c.child_id)}</a></td>
        <td style="font:500 12px var(--mono)">${escapeHtml(c.region)}/${escapeHtml(c.universe)}</td>
        <td>${fmtSharpe(c.sharpe)} / ${fmtNum(c.fitness)}</td>
        <td>${fmtNum(c.turnover, 4)}</td>
        <td>${fmtNum(c.returns, 4)}</td>
        <td>${fmtBps(c.margin)} bps</td>
        <td class="muted">${escapeHtml(c.type || '')}</td>
      </tr>`).join('');
    const detail = childRows ? `
      <tr id="arcChild${i}" style="display:none;background:var(--bg-2)">
        <td colspan="10" style="padding:10px 18px">
          <div class="muted" style="margin-bottom:6px">RA_CHILD 明细（各 region 真实指标）</div>
          <table class="data-table" style="font-size:12px">
            <thead><tr>
              <th style="width:30px"></th><th>子 Alpha</th><th>Region/Univ</th>
              <th>S / F</th><th>Turnover</th><th>Returns</th><th>Margin</th><th>类型</th>
            </tr></thead>
            <tbody>${childRows}</tbody>
          </table>
          ${r.error ? `<div class="muted" style="margin-top:6px">${escapeHtml(r.error)}</div>` : ''}
        </td>
      </tr>` : '';
    return `
      <tr>
        <td>${childRows ? `<span style="cursor:pointer;color:var(--accent)" onclick="toggleArcChild(${i})">＋</span>` : ''}</td>
        <td><a class="alpha-link" href="https://platform.worldquantbrain.com/alpha/${escapeHtml(r.src_id)}"
               target="_blank">${escapeHtml(r.src_id)}</a></td>
        <td style="font:500 12px var(--mono)">${escapeHtml(r.src_region)}/${escapeHtml(r.src_universe)}</td>
        <td>${fmtSharpe(r.src_sharpe)} / ${fmtNum(r.src_fitness)}</td>
        <td>${r.n_children ?? '—'}</td>
        <td>${fmtNum(r.sharpe_avg)} <span class="muted">(${fmtNum(r.sharpe_min)}~${fmtNum(r.sharpe_max)})</span></td>
        <td>${fmtNum(r.fitness_avg)}</td>
        <td>${r.fitness_pass ? `<span class="sharpe-pos">${r.fitness_pass}</span>` : '<span class="muted">0</span>'}
            <span class="muted">/${r.n_children ?? 0}</span></td>
        <td>${fmtNum(r.turnover_avg, 4)}</td>
        <td>${ok ? '<span class="sharpe-pos">COMPLETE</span>'
                 : `<span class="sharpe-neg">${escapeHtml(String(r.status || ''))}</span>`}</td>
        <td>${r.ra_parent
              ? `<a class="alpha-link" href="https://platform.worldquantbrain.com/alpha/${escapeHtml(r.ra_parent)}"
                     target="_blank">${escapeHtml(r.ra_parent)}</a>`
              : `<span class="muted" title="${escapeHtml(r.error || '')}">${escapeHtml(String(r.error || '')).slice(0, 40)}</span>`}</td>
      </tr>${detail}`;
  }).join('') || '<tr><td colspan="11" class="muted" style="padding:14px">暂无结果</td></tr>';
}

function toggleArcChild(i) {
  const el = $('arcChild' + i);
  if (el) el.style.display = el.style.display === 'none' ? '' : 'none';
}

// 事件绑定
$('btnArcScan').addEventListener('click', scanArcInventory);
$('btnArcRun').addEventListener('click', startArc);
$('btnArcRunIds').addEventListener('click', runArcByIds);
$('btnArcStop').addEventListener('click', stopArc);
$('btnArcRefresh').addEventListener('click', refreshArcResults);
$('arcMinSharpe').addEventListener('input', renderArcInventory);
$('arcSelAll').addEventListener('change', (e) => {
  document.querySelectorAll('.arc-pick').forEach(c => { c.checked = e.target.checked; });
});
$('arcInherit').addEventListener('change', (e) => {
  const off = e.target.checked;
  ['arcDecay', 'arcTrunc', 'arcNeutral'].forEach(id => { $(id).disabled = off; });
});
// 打开页面时若后端已有结果，直接回显
refreshArcResults();
pollArcStatus();

// ====== 顶栏平台指标（从 simulator 顶栏平移）======
// Osmosis Rank / VF（BRAIN 实时）+ Total Payment / Signals（平台聚合）
// 数据源与 simulator 同一接口：GET /api/simulator/platform-stats
async function refreshGlobalMetrics() {
  try {
    const r = await api('/api/simulator/platform-stats');
    const set = (id, v) => { const el = $(id); if (el) el.textContent = v; };
    // 当日提交数：BRAIN 按美东日界统计（北京时间每日 12:00 重置），30s 轮询天然覆盖跨点
    set('gTodaySubmitted', r.today_submitted == null ? '—' : Number(r.today_submitted).toLocaleString());
    set('gOsmosisRank', r.osmosis_rank == null ? '—' : Number(r.osmosis_rank).toFixed(2));
    set('gVF', r.vf == null ? '—' : Number(r.vf).toFixed(2));
    set('gCommunity', r.total_payment == null ? '—' : '$' + Number(r.total_payment).toFixed(2));
    set('gSignals', r.signals == null ? '—' : r.signals);
  } catch (e) {
    // 静默：顶栏指标拉取失败不打扰主流程
  }
}
refreshGlobalMetrics();
setInterval(refreshGlobalMetrics, 30000);

// =====================================================================
// 以下为从 simulator.js 平移的三套指标弹窗（Osmosis / Signals / Payment）
// 包进 IIFE：首页 app.js 已有同名 renderOsmosisChart（Osmosis 分配方案图），
// 不隔离会互相覆盖，导致首页原有的分配方案图表失效。
// =====================================================================
(function initMetricModals() {
const urlParams = new URLSearchParams(location.search);
// 段外依赖补齐：simulator.js 头部定义的格式化工具，首页 app.js 里没有同名实现
// （app.js 的 fmtBps 不带 ‱ 后缀，语义不同，此处不借用）
const fmtPct = (v, d = 2) => v == null ? '—' : (Number(v) * 100).toFixed(d) + '%';
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
// 首页不做 ?osm=1 直达，已移除（原 simulator 行为）

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
// 首页不做 ?signals=1 直达，已移除（原 simulator 行为）

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
// 首页不做 ?pay=1 直达，已移除（原 simulator 行为）
})();
