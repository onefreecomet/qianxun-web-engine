// 千寻 web —— Total Payment 弹窗（Regular + Super 双线折线图 + odometer 联动）
//
// 260928 抽出：本段原先在 app.js 与 simulator.js 各存一份，331 行逐字节相同，
// 改一处忘另一处必然发生（当天修 v3 口径就改了两遍）。现在两页共用这一份。
//
// 用法：页面在 DOM 就绪后调用
//     PayModal.init({ $: $, api: api });
//   $   : (id) => document.getElementById(id)
//   api : (path, opts) => Promise<json>
//
// 数据：/api/simulator/base-payment（BRAIN /users/self/activities/base-payment，v3 口径）
// ⚠️ v3 口径下 total = regularAlpha + superAlpha，records 为 [date, regular, super] 三元组；
//    旧口径只有 [date, value]，代码需兼容（super 记 0）。
window.PayModal = (function () {
  'use strict';

  let $ = null;       // 由 init 注入
  let api = null;
  let payModal = null;


  // payModal 由模块顶部的 let 声明，在 init 里赋值
  const payState = {
    dates: [],          // 统一日期轴 [date,...]（base + submissions 并集）
    regularMap: {},     // {date: Regular（普通 alpha 的 base payment）}
    superMap: {},       // {date: Super（SuperAlpha 支付）}
    baseMap: {},        // {date: Regular + Super}（当日总额，供 odometer 用）
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
      payState.regularMap = {};
      payState.superMap = {};
      payState.baseMap = {};
      baseRecs.forEach((x) => {
        // v3: [date, regular, super]；旧口径回退成 [date, value]，此时 super 记 0
        const reg = Number(x[1]) || 0;
        const sup = x.length > 2 ? (Number(x[2]) || 0) : 0;
        payState.regularMap[x[0]] = reg;
        payState.superMap[x[0]] = sup;
        payState.baseMap[x[0]] = reg + sup;
      });
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
    // v3 口径的 total 里带 regularAlpha / superAlpha 拆分；旧口径没有则不显示拆分
    const tot = r.total || {};
    const reg = tot.regularAlpha, sup = tot.superAlpha;
    const split = (reg != null && sup != null)
      ? `<i class="pay-sum-split" title="Regular：普通 alpha 的 base payment；Super：SuperAlpha 支付">R $${Number(reg).toFixed(2)} + S $${Number(sup).toFixed(2)}</i>`
      : '';
    $('paySummary').innerHTML =
      `<span>本季 <b>${f(r.current)}</b></span>` +
      `<span>上季 <b>${f(r.previous)}</b></span>` +
      `<span>YTD <b>${f(r.ytd)}</b></span>` +
      `<span>昨日 <b>${f(r.yesterday)}</b></span>` +
      `<span class="pay-sum-total">Total 累计 <b>${f(r.total)}</b>${split}</span>` +
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
    // 刻度按实际画出来的两条线（Regular / Super）的最大值定，总额未画线不参与
    const lineVals = [];
    payState.dates.forEach((d) => {
      if (payState.regularMap[d] != null) lineVals.push(payState.regularMap[d]);
      if (payState.superMap[d] != null) lineVals.push(payState.superMap[d]);
    });
    const dataMax = Math.max.apply(null, lineVals.length ? lineVals : vals);
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
    // 双线：Regular（青，带面积）/ Super（紫，只在有值的日子画标记，避免底部一串 0 点）
    const drawSeries = (map, color, withArea, dotsOnlyWhenPositive) => {
      const pts = [];
      payState.dates.forEach((d, i) => { if (map[d] != null) pts.push([i, map[d]]); });
      if (pts.length < 2) return;
      if (withArea) {
        const grad = ctx.createLinearGradient(0, padT, 0, padT + plotH);
        grad.addColorStop(0, 'rgba(34,211,238,0.28)');
        grad.addColorStop(1, 'rgba(34,211,238,0)');
        ctx.beginPath(); ctx.moveTo(g.xOf(pts[0][0]), yOf(pts[0][1]));
        pts.forEach((p) => ctx.lineTo(g.xOf(p[0]), yOf(p[1])));
        ctx.lineTo(g.xOf(pts[pts.length - 1][0]), padT + plotH);
        ctx.lineTo(g.xOf(pts[0][0]), padT + plotH); ctx.closePath();
        ctx.fillStyle = grad; ctx.fill();
      }
      ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.lineJoin = 'round';
      ctx.beginPath();
      pts.forEach((p, i) => { const x = g.xOf(p[0]), y = yOf(p[1]); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
      ctx.stroke();
      pts.forEach((p) => {
        const i = p[0], v = p[1];
        if (dotsOnlyWhenPositive && !(v > 0)) return;
        const x = g.xOf(i), y = yOf(v);
        const hot = i === payState.hoverIdx;
        ctx.beginPath(); ctx.arc(x, y, hot ? 5.5 : 2.4, 0, Math.PI * 2);
        ctx.fillStyle = hot ? '#fbbf24' : color; ctx.fill();
        if (hot) {
          ctx.strokeStyle = 'rgba(251,191,36,0.9)'; ctx.lineWidth = 2;
          ctx.beginPath(); ctx.arc(x, y, 9, 0, Math.PI * 2); ctx.stroke();
        }
      });
    };
    drawSeries(payState.regularMap, '#22d3ee', true, false);
    drawSeries(payState.superMap, '#a78bfa', false, true);
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
    const reg = payState.regularMap[date], sup = payState.superMap[date] || 0;
    let label = date;
    if (reg != null) {
      label += '  $' + (reg + sup).toFixed(2);
      if (sup > 0) label += '  (R $' + reg.toFixed(2) + ' + S $' + sup.toFixed(2) + ')';
    }
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

  function init(opts) {
    $ = opts.$;
    api = opts.api;
    payModal = $('payModal');
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
  }

  return { init, open: openPayModal };
})();
