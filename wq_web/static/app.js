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
      loadBatches();
      if (state.currentBatch === msg.batch_no) loadBatchDetail(msg.batch_no);
    }, 500);
  }
}

// ====== Dashboard ======
async function loadStats() {
  try {
    const r = await api('/api/stats');
    const s = r.stats;
    $('statAlphas').textContent = s.alphas.toLocaleString();
    $('statBatches').textContent = s.batches.toLocaleString();
    $('statRunning').textContent = s.batches_running.toLocaleString();
    $('statSims').textContent = s.simulations.toLocaleString();
    $('statSimsDone').textContent = s.simulations_completed.toLocaleString();
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
  // 有真实值时每秒本地倒计时重画（不重新请求）
  if (window._quotaData && window._quotaData.limit != null) {
    const q = window._quotaData;
    const elapsed = Math.floor(Date.now() / 1000 - q._fetchedAt);
    const left = Math.max(q.reset_sec_left - elapsed, 0);
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

async function refreshQuota() {
  try {
    const r = await api('/api/quota');
    if (r.limit != null) {
      r._fetchedAt = Date.now() / 1000;
      window._quotaData = r;
      renderQuota();
    } else {
      $('quotaValue').textContent = '--';
      $('quotaSub').textContent = `今日本地 ${r.used_local || 0} 次`;
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

// ====== Batches ======
async function loadBatches() {
  try {
    const r = await api('/api/batches?limit=100');
    state.batches = r.batches;
    renderBatchList();
  } catch (e) { toast('加载批次失败：' + e.message, 'error'); }
}

function renderBatchList() {
  const container = $('batchList');
  if (state.batches.length === 0) {
    container.innerHTML = '<div class="muted">暂无批次</div>';
    return;
  }
  container.innerHTML = state.batches.map(b => {
    const total = b.total || b.expression_count || 0;
    const done = (b.success || 0) + (b.failed || 0);
    const pct = total > 0 ? Math.min(100, done / total * 100) : 0;
    const cls = b.status === 'completed' ? 'completed' : b.status === 'failed' ? 'failed' : '';
    const selected = state.currentBatch === b.batch_no ? 'selected' : '';
    return `
      <div class="batch-item ${selected}" data-batch="${b.batch_no}">
        <div class="batch-item-header">
          <span class="batch-no">${b.batch_no}</span>
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

    const statusClass = batch.status === 'completed' ? 'check-pass' :
                        batch.status === 'failed' ? 'check-fail' : 'check-warn';

    const sim = task_run || {};
    const success = sim.success || 0, failed = sim.failed || 0, total = sim.total || 0;
    const pct = total > 0 ? Math.min(100, (success + failed) / total * 100) : 0;

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
          <div><span>创建时间</span><span style="font-size:13px">${fmtCST(batch.created_at)}</span></div>
          <div><span>备注</span><span style="font-size:13px">${batch.note || '—'}</span></div>
        </div>
      </div>
    `;

    if (total > 0) {
      html += `
        <div class="detail-section">
          <h3>进度</h3>
          <div class="progress-bar" style="height:8px"><div class="progress-bar-fill ${batch.status === 'completed' ? 'completed' : ''}" style="width:${pct.toFixed(1)}%"></div></div>
          <div style="margin-top:8px; color:var(--text-dim); font-size:13px">
            完成 <span style="color:var(--green)">${success}</span> / 失败 <span style="color:var(--red)">${failed}</span> / 总 ${total} (${pct.toFixed(1)}%)
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
              return `<tr>
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

    if (simulations.length > 0) {
      html += `
        <div class="detail-section">
          <h3>表达式状态（${simulations.length} 条）</h3>
          <table class="data-table">
            <thead><tr><th>#</th><th>状态</th><th>Alpha ID</th><th>Decay</th><th>重试</th><th>错误</th></tr></thead>
            <tbody>${simulations.map(s => `
              <tr>
                <td>${s.id}</td>
                <td><span class="status-badge status-${s.status}">${s.status}</span></td>
                <td>${s.alpha_id ? alphaLink(s.alpha_id) : '—'}</td>
                <td>${s.decay || '—'}</td>
                <td>${s.retry_count || 0}</td>
                <td class="muted" style="font-size:12px">${(s.last_error || '').slice(0, 60)}</td>
              </tr>
            `).join('')}</tbody>
          </table>
        </div>
      `;
    }

    $('batchDetail').innerHTML = html;
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

async function refreshSyncStatus() {
  try {
    const r = await api('/api/sync/pnl/status');
    const running = r.running;
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
    // 默认只拉 PnL，不算 corr；corr 走手动单点触发，节省资源
    const r = await api('/api/sync/pnl', { method: 'POST', body: JSON.stringify({ compute_corr: false }) });
    if (!r.ok) { toast(r.error || '启动失败', 'error'); $('syncStatus').textContent = '未运行'; return; }
    toast('PnL 同步已启动', 'success');
    if (window._syncTimer) clearInterval(window._syncTimer);
    window._syncTimer = setInterval(refreshSyncStatus, 1500);
    refreshSyncStatus();
  } catch (e) { toast('启动失败：' + e.message, 'error'); $('syncStatus').textContent = '未运行'; }
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
    el.innerHTML = max != null
      ? `max_corr = <span style="color:var(--accent);font-weight:600">${max.toFixed(3)}</span> (${src})`
      : '算完但池子为空';
    toast(`✅ ${alphaId} corr 已算：${max?.toFixed(3) || 'N/A'} (${src})`, 'success');
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

// ====== Init ======
loadStats();
loadBatches();
loadConcurrency();
loadMemos();
loadPrompts();
refreshSyncStatus();
connectWS();

// ====== 锚点 active 状态跟随滚动 ======
// 物理顺序 → 导航锚点索引（导航：总览0 / PnL同步1 / AI批次2 / 备忘录3 / 设置4）
const _sectionAnchor = [
  ['sec-dashboard', 0],
  ['sec-concurrency', 1], // 同步内容在并发区里
  ['sec-batches', 2],
  ['sec-memo', 3],
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