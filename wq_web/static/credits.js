/* 积分签到 · 账号积分 —— 前端逻辑（千寻 web 版） */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const API_BASE = '/api/xgj';
  const state = {
    accounts: [],
    credits: {},        // uid -> 积分明细视图
    expanded: new Set(),
    loading: {},        // uid -> true 表示该账号积分请求在飞（请求去重）
    busy: false,
  };

  // ---------- 工具 ----------
  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function fmtNum(v, digits) {
    if (v == null || !isFinite(v)) return '—';
    const n = Number(v);
    const d = digits == null ? (Math.abs(n % 1) > 0.001 ? 2 : 0) : digits;
    return n.toLocaleString('zh-CN', { minimumFractionDigits: d, maximumFractionDigits: d });
  }

  function fmtClock(ms) {
    if (!ms) return '—';
    const d = new Date(Number(ms));
    const z = (n) => String(n).padStart(2, '0');
    return `${z(d.getHours())}:${z(d.getMinutes())}:${z(d.getSeconds())}`;
  }

  async function api(path, opts) {
    const res = await fetch(path, Object.assign({ headers: { 'content-type': 'application/json' } }, opts || {}));
    let data = null;
    try { data = await res.json(); } catch (_) { data = {}; }
    if (!res.ok) {
      const err = new Error((data && data.error) || `请求失败 (${res.status})`);
      err.payload = data;
      err.status = res.status;
      throw err;
    }
    return data;
  }

  function toast(msg, kind, ms) {
    const el = document.createElement('div');
    el.className = 'xgj-toast ' + (kind || 'info');
    el.textContent = msg;
    $('toastStack').appendChild(el);
    setTimeout(() => {
      el.style.transition = 'opacity .25s, transform .25s';
      el.style.opacity = '0';
      el.style.transform = 'translateX(18px)';
      setTimeout(() => el.remove(), 260);
    }, ms || 3600);
  }

  function banner(msg, kind) {
    const el = $('banner');
    if (!msg) { el.hidden = true; el.innerHTML = ''; return; }
    el.hidden = false;
    el.className = 'xgj-banner ' + (kind || 'info');
    el.innerHTML = msg;
  }

  // ---------- 概览 ----------
  function renderSummary() {
    const accs = state.accounts;
    $('statAccounts').textContent = accs.length || '—';

    const done = accs.filter((a) => a.checkin && (a.checkin.state === 'done' || a.checkin.state === 'already')).length;
    $('statChecked').textContent = accs.length ? `${done} / ${accs.length}` : '—';

    let sum = 0, hasSum = false;
    let soonSum = 0, soonN = 0, hasSoon = false;
    Object.keys(state.credits).forEach((uid) => {
      const c = state.credits[uid];
      if (!c || !c.ok) return;
      if (typeof c.totalRemaining === 'number') { sum += c.totalRemaining; hasSum = true; }
      if (c.soonSum) { soonSum += c.soonSum; soonN += (c.soonCount || 0); hasSoon = true; }
    });
    $('statCredits').textContent = hasSum ? fmtNum(sum) : '—';
    const soonEl = $('statSoon');
    soonEl.textContent = hasSoon ? `${fmtNum(soonSum)} · ${soonN} 段` : '—';
    soonEl.className = 'xgj-stat-value ' + (soonN > 0 ? 'warn' : '');

    const failed = accs.filter((a) => a.checkin && a.checkin.state === 'fail').length;
    // 凭据告警优先级高于签到失败：凭据坏了签到必然失败，先让用户看到根因
    const credBad = accs.filter((a) => a.credential && a.credential.state === 'expired');
    const credWarn = accs.filter((a) => a.credential && a.credential.state === 'refresh');

    if (credBad.length) {
      const names = credBad.map((a) => esc(a.nickname)).join('、');
      const tail = state.degraded
        ? '请到 WorkDaddy 重新登录后再刷新（WorkDaddy 未运行时无法自动续期）。'
        : '请到 WorkDaddy 重新登录后再刷新。';
      banner(`<span><strong>${credBad.length} 个账号凭据已失效</strong>（${names}）：积分与签到都无法进行，${tail}</span>`, 'err');
    } else if (failed) {
      banner(`<span>有 <strong>${failed}</strong> 个账号签到失败，展开对应账号查看错误信息；若是登录身份过期，请到 WorkDaddy 重新登录该账号。</span>`, 'err');
    } else if (credWarn.length) {
      const names = credWarn.map((a) => esc(a.nickname)).join('、');
      banner(`<span><strong>${credWarn.length} 个账号凭据临期</strong>（${names}）：refreshToken 快到期了，签到一次即可自动续期，否则过期后需重新登录。</span>`, 'info');
    } else {
      banner('');
    }
  }

  // ---------- 账号行 ----------
  // ---------- 凭据健康度 ----------
  // accessToken 短命是正常的（靠 refreshToken 滚动续期），所以它临期不算事，
  // 只提示「下次签到会自动续期」；真正要用户动手的是 refreshToken 过期。
  const CRED_META = {
    ok: { cls: 'xgj-tag-cred-ok', label: '凭据正常', tip: '登录凭据健康，无需操作' },
    soon: { cls: 'xgj-tag-cred-soon', label: '待续期', tip: '登录凭据临期，下次签到会自动续期' },
    refresh: { cls: 'xgj-tag-cred-refresh', label: '凭据临期', tip: 'refreshToken 快到期了，尽快签一次到以续期' },
    expired: { cls: 'xgj-tag-cred-expired', label: '需重新登录', tip: '登录凭据已失效，需回 WorkDaddy 重新登录该账号' },
  };

  function credMeta(a) {
    const c = a.credential || {};
    return CRED_META[c.state] || null;
  }

  function fmtDays(d) {
    if (d == null || !isFinite(d)) return '—';
    if (d < 0) return '已过期';
    if (d < 1) return `${Math.max(1, Math.round(d * 24))} 小时`;
    return `${d.toFixed(1)} 天`;
  }

  function credBarHtml(a) {
    const c = a.credential || {};
    if (!c.state) return '';
    const st = c.state;
    const noRenew = !!c.noAutoRenew;   // 降级通道：没人帮忙自动续期
    const cls = st === 'expired' ? 'bad' : (st === 'ok' ? '' : 'warn');
    const vcls = (d) => (d == null ? '' : d < 0 ? 'bad' : (d <= 3 ? 'warn' : (d > 10 ? 'ok' : '')));

    const hint = st === 'expired'
      ? `<span class="xgj-cred-hint crit">凭据已失效，请回 WorkDaddy 重新登录该账号${noRenew ? '（当前 WorkDaddy 未运行，无法自动续期）' : ''}</span>`
      : st === 'refresh'
        ? '<span class="xgj-cred-hint act">refreshToken 临期，签到一次即可自动续期</span>'
        : st === 'soon'
          ? `<span class="xgj-cred-hint">accessToken 临期，${noRenew ? 'WorkDaddy 恢复运行后会在签到时自动续期' : '下次签到会自动续期，无需操作'}</span>`
          : '<span class="xgj-cred-hint">凭据健康</span>';

    return `
      <div class="xgj-cred-bar ${cls}">
        <span class="xgj-cred-item">
          <span class="xgj-cred-k">Access Token</span>
          <span class="xgj-cred-v ${vcls(c.accessDays)}">${fmtDays(c.accessDays)}</span>
        </span>
        <span class="xgj-cred-item">
          <span class="xgj-cred-k">Refresh Token</span>
          <span class="xgj-cred-v ${vcls(c.refreshDays)}">${fmtDays(c.refreshDays)}</span>
        </span>
        ${hint}
      </div>`;
  }

  function segRowHtml(s) {
    const total = Number(s.total || 0);
    const remain = Number(s.remaining || 0);
    const pct = total > 0 ? Math.max(0, Math.min(100, (remain / total) * 100)) : 100;
    const barCls = s.state === 'soon' ? 'soon' : (pct < 15 ? 'low' : '');
    return `
      <tr class="${s.state === 'soon' ? 'seg-soon' : ''}${s.state === 'expired' ? 'seg-expired' : ''}">
        <td><div class="xgj-seg-src" title="${esc(s.source)}">${esc(s.source)}</div></td>
        <td class="r"><span class="xgj-seg-remain${remain <= 0 ? ' zero' : ''}">${fmtNum(remain)}</span></td>
        <td class="r" style="color:var(--text-mute)">${fmtNum(total)}</td>
        <td style="width:100px">
          <div class="xgj-bar-wrap"><div class="xgj-bar-fill ${barCls}" style="width:${pct.toFixed(1)}%"></div></div>
        </td>
        <td class="r"><span class="xgj-seg-exp ${s.state === 'soon' ? 'soon' : ''}${s.state === 'expired' ? 'expired' : ''}">${esc(s.expiresText)}</span></td>
        <td class="r"><span class="xgj-seg-days">${esc(s.daysText)}</span></td>
      </tr>`;
  }

  function detailHtml(uid, acc) {
    // 凭据健康度永远显示，即使积分读取失败（凭据失效时积分必然读不到，
    // 那恰恰是用户最需要看到这条提示的场景）
    const credBar = acc ? credBarHtml(acc) : '';
    const c = state.credits[uid];
    if (!c) {
      return `<div class="xgj-detail-loading"><span class="xgj-spinner-sm"></span>正在读取积分明细…</div>`;
    }
    if (!c.ok) {
      return `${credBar}<div class="xgj-err-inline">积分读取失败：${esc(c.error || '未知错误')}</div>`;
    }
    if (c.unlimited) {
      return `${credBar}<div class="xgj-detail-empty">该账号为企业版不限量额度</div>`;
    }
    const segs = c.segments || [];
    if (!segs.length) {
      return `${credBar}<div class="xgj-detail-empty">没有可用的积分段</div>`;
    }
    const warnParts = [];
    if (c.meterError) warnParts.push(`计量：${c.meterError}`);
    if (c.packageError) warnParts.push(`礼包：${c.packageError}`);
    return `
      ${credBar}
      <div class="xgj-detail-head">
        <span class="xgj-detail-title">积分段 · ${segs.length} 段 · 按过期时间升序</span>
        <span class="xgj-detail-hint">剩余合计 ${fmtNum(c.totalRemaining)}${c.todayUsage ? ` · 今日已用 ${fmtNum(c.todayUsage.used)}（${c.todayUsage.count} 次）` : ''}</span>
      </div>
      ${warnParts.length ? `<div class="xgj-err-inline">部分数据获取异常 · ${esc(warnParts.join(' | '))}</div>` : ''}
      <table class="xgj-seg-table">
        <thead>
          <tr>
            <th>来源</th>
            <th class="r">剩余</th>
            <th class="r">总额</th>
            <th>余量</th>
            <th class="r">过期时间</th>
            <th class="r">剩余时长</th>
          </tr>
        </thead>
        <tbody>${segs.map(segRowHtml).join('')}</tbody>
      </table>`;
  }

  function accountRowHtml(a) {
    const ck = a.checkin || {};
    const ckCls = {
      done: 'xgj-tag-ck-ok', already: 'xgj-tag-ck-ok',
      fail: 'xgj-tag-ck-fail', inactive: 'xgj-tag-ck-inactive',
    }[ck.state] || 'xgj-tag-ck-pending';

    const c = state.credits[a.uid];
    const creditKnown = c && c.ok && typeof c.totalRemaining === 'number';
    const creditTxt = creditKnown ? fmtNum(c.totalRemaining) : (c && c.ok && c.unlimited ? '不限量' : '…');
    const creditCls = creditKnown ? 'accent' : '';
    const soonN = creditKnown ? (c.soonCount || 0) : 0;

    const cred = credMeta(a);
    const credTag = cred && a.credential.state !== 'ok'
      ? `<span class="xgj-tag xgj-cred-tag ${cred.cls}" title="${esc(cred.tip)}">${esc(cred.label)}</span>`
      : '';

    const tags = [
      `<span class="xgj-tag ${ckCls}">${esc(ck.label || '未签到')}</span>`,
      a.isCurrent ? `<span class="xgj-tag xgj-tag-cur">当前</span>` : '',
      a.type && a.type !== 'personal' ? `<span class="xgj-tag xgj-tag-ent">${esc(a.enterpriseName || a.type)}</span>` : '',
      a.authValid ? '' : `<span class="xgj-tag xgj-tag-ck-fail">登录过期</span>`,
      credTag,
    ].filter(Boolean).join('');

    const usage = a.todayUsage
      ? `<span class="xgj-num-val small">${fmtNum(a.todayUsage.used)} / ${a.todayUsage.count} 次</span>`
      : `<span class="xgj-num-val small">—</span>`;

    return `
      <div class="xgj-account-row${state.expanded.has(a.uid) ? ' open' : ''}" data-uid="${esc(a.uid)}">
        <div class="xgj-acc-main">
          <div class="xgj-acc-id">
            <span class="xgj-acc-expand">▶</span>
            <span class="xgj-acc-name">${esc(a.nickname)}</span>
            ${tags}
            <span class="xgj-acc-sub">${esc(a.phone || a.uid8)}</span>
          </div>
          <div class="xgj-acc-nums">
            <div class="xgj-num-block">
              <span class="xgj-num-label">今日用量</span>
              ${usage}
            </div>
            <div class="xgj-num-block xgj-credited">
              <span class="xgj-num-label">可用积分</span>
              <span class="xgj-num-val ${creditCls}">${creditTxt}</span>
            </div>
            <div class="xgj-num-block">
              <span class="xgj-num-label">7 天内过期</span>
              <span class="xgj-num-val ${soonN > 0 ? 'warn' : 'small'}">${creditKnown ? (soonN ? `${soonN} 段` : '无') : '—'}</span>
            </div>
          </div>
        </div>
        <div class="xgj-acc-detail">${state.expanded.has(a.uid) ? detailHtml(a.uid, a) : ''}</div>
      </div>`;
  }

  function renderAccounts() {
    const list = $('accountList');
    if (!state.accounts.length) {
      list.innerHTML = `<div class="xgj-detail-empty">没有读到账号。请在 WorkDaddy 客户端里登录至少一个 WorkBuddy 账号。</div>`;
      return;
    }
    list.innerHTML = state.accounts.map(accountRowHtml).join('');
  }

  // ---------- 数据加载 ----------
  async function loadHealth() {
    const el = $('daemonStatus');
    try {
      const h = await api(API_BASE + '/health');
      if (h.ok) {
        el.className = 'xgj-daemon ok';
        $('daemonText').textContent = `已连接 · ${h.accountCount} 账号 · :${h.daemonPort}`;
        return true;
      }
      $('daemonText').textContent = h.degradedUsable ? '降级模式' : '未连接';
      el.className = 'xgj-daemon ' + (h.degradedUsable ? 'warn' : 'bad');
      if (h.degradedUsable) {
        banner(`<span><strong>WorkDaddy 客户端未运行</strong>：已切换为直连官方接口（${h.backupCount} 个账号备份可用）。`
             + `积分与签到正常，但<strong>今日用量不可用</strong>，且 token 过期时需回客户端续期。</span>`, 'info');
        return true;
      }
      banner(`<span><strong>无法连接 WorkDaddy 本地服务</strong>：${esc(h.error || '未知原因')}。请确认 WorkDaddy 客户端正在运行后刷新。</span>`, 'err');
      return false;
    } catch (e) {
      el.className = 'xgj-daemon bad';
      $('daemonText').textContent = '未连接';
      banner(`<span><strong>健康检查失败</strong>：${esc(e.message)}</span>`, 'err');
      return false;
    }
  }

  async function loadAccounts() {
    const res = await api(API_BASE + '/accounts');
    state.accounts = res.accounts || [];
    state.degraded = !!res.degraded;
    const alive = new Set(state.accounts.map((a) => a.uid));
    Object.keys(state.credits).forEach((uid) => { if (!alive.has(uid)) delete state.credits[uid]; });
    Object.keys(state.loading).forEach((uid) => { if (!alive.has(uid)) delete state.loading[uid]; });
    renderAccounts();
    renderSummary();
  }

  async function loadCredit(uid, force) {
    if (!force && state.credits[uid]) return state.credits[uid];
    const res = await api(API_BASE + '/credits', {
      method: 'POST',
      body: JSON.stringify({ uid: uid }),
    });
    state.credits[uid] = res;
    return res;
  }

  /**
   * 批量拉取积分：一次请求并发拉完，避免 N 个账号 N 次串行往返（进页面慢的主因）。
   * 结果先整体回填到 state.credits，再逐行重绘。
   */
  async function loadCreditsBatch(uids) {
    const list = (uids || []).filter(Boolean);
    if (!list.length) return;
    const res = await api(API_BASE + '/credits-batch', {
      method: 'POST',
      body: JSON.stringify({ uids: list }),
    });
    const views = res.views || {};
    list.forEach((uid) => {
      if (views[uid]) state.credits[uid] = views[uid];
      else state.credits[uid] = { ok: false, error: (res.failed || {})[uid] || '未返回数据' };
    });
  }

  function refreshRow(uid) {
    const row = document.querySelector(`.xgj-account-row[data-uid="${CSS.escape(uid)}"]`);
    if (!row) return;
    const idx = state.accounts.findIndex((a) => a.uid === uid);
    if (idx < 0) return;
    // accountRowHtml 已按 state.expanded 决定是否带 open / 是否渲染明细区，
    // 这里绝不能再按 DOM 旧状态补 class（否则展开态会与 state 脱节，
    // 表现为「收起后点不开」）。
    row.outerHTML = accountRowHtml(state.accounts[idx]);
  }

  async function expandAccount(uid) {
    state.expanded.add(uid);
    refreshRow(uid);
    // 已有缓存且成功：上面的 refreshRow 已按缓存渲染出明细，直接结束。
    // 若历史失败（ok:false）则重试一次，避免一次偶发失败永久卡住。
    if (state.credits[uid] && state.credits[uid].ok) return;
    if (state.loading[uid]) return;      // 已有同账号请求在飞，不重复打接口
    state.loading[uid] = true;
    try {
      await loadCredit(uid, true);
    } catch (e) {
      state.credits[uid] = { ok: false, error: e.message };
    } finally {
      delete state.loading[uid];
    }
    // 用户在请求期间可能已收起/刷新：只在仍处于展开态时才重绘，
    // 避免用过期结果覆盖用户的最新操作。
    if (state.expanded.has(uid)) refreshRow(uid);
    renderSummary();
  }

  function collapseAccount(uid) {
    state.expanded.delete(uid);
    refreshRow(uid);
  }

  // ---------- 一键领取 ----------
  async function claimAll() {
    if (state.busy) return;
    state.busy = true;
    const btn = $('btnClaim');
    btn.disabled = true;
    btn.classList.add('loading');

    try {
      const res = await api(API_BASE + '/claim', { method: 'POST', body: '{}' });
      if (res.mode === 'direct') {
        // 降级直连模式：给出逐账号结果
        const parts = [];
        if (res.success) parts.push(`${res.success} 个新领取`);
        if (res.already) parts.push(`${res.already} 个已签到`);
        if (res.failed) parts.push(`${res.failed} 个失败`);
        toast((res.ok ? '领取完成：' : '领取部分失败：') + (parts.join('，') || '无结果'),
              res.ok ? 'ok' : 'err', res.ok ? 3600 : 7000);
        if (res.failed) {
          const bad = (res.results || []).filter((x) => !x.ok)
            .map((x) => `${x.nickname}（${x.message}）`).join('；');
          banner(`<span><strong>${res.failed} 个账号签到失败</strong>：${esc(bad)}</span>`, 'err');
        }
      } else if (res.ok) {
        toast('领取完成，全部账号已处理', 'ok');
      } else {
        toast('领取任务未成功：' + (res.error || res.status || '未知'), 'err', 6000);
      }
    } catch (e) {
      toast('领取失败：' + e.message, 'err', 6000);
    } finally {
      btn.disabled = false;
      btn.classList.remove('loading');
      state.busy = false;
    }

    try {
      await loadAccounts();
      const openUids = Array.from(state.expanded);
      if (openUids.length) {
        await loadCreditsBatch(openUids).catch(() => {});
        openUids.forEach(refreshRow);
      }
      renderSummary();
    } catch (e) {
      toast('刷新账号状态失败：' + e.message, 'err', 5000);
    }
  }

  // ---------- 刷新全部 ----------
  async function refreshAll(silent) {
    const btn = $('btnRefresh');
    btn.disabled = true;
    try {
      // health 与 accounts 并发，省掉一次串行等待
      const [ok] = await Promise.all([
        loadHealth(),
        loadAccounts().catch((e) => { banner(`<span><strong>加载失败</strong>：${esc(e.message)}</span>`, 'err'); }),
      ]);
      if (!ok) return;
      if (!state.accounts.length) return;

      // 账号列表已渲染（骨架屏被替换），积分走一次批量请求，不阻塞首屏
      const autoExpand = $('autoExpand').checked;
      const targets = autoExpand ? state.accounts.map((a) => a.uid) : Array.from(state.expanded);
      if (autoExpand) {
        targets.forEach((uid) => state.expanded.add(uid));
        renderAccounts();   // 先展开成 loading 态，让用户立刻看到骨架
      }
      if (!targets.length) {
        if (!silent) toast('已刷新 · ' + fmtClock(Date.now()), 'ok', 2200);
        return;
      }

      try {
        await loadCreditsBatch(targets);   // 一次请求拿完所有账号积分
      } catch (e) {
        targets.forEach((uid) => { state.credits[uid] = { ok: false, error: e.message }; });
        banner(`<span><strong>积分加载失败</strong>：${esc(e.message)}</span>`, 'err');
      }
      targets.forEach(refreshRow);
      renderSummary();

      if (!silent) toast('已刷新 · ' + fmtClock(Date.now()), 'ok', 2200);
    } catch (e) {
      banner(`<span><strong>加载失败</strong>：${esc(e.message)}</span>`, 'err');
    } finally {
      btn.disabled = false;
    }
  }

  // ---------- 事件绑定 ----------
  document.addEventListener('click', (ev) => {
    const main = ev.target.closest('.xgj-acc-main');
    if (!main) return;
    const row = main.closest('.xgj-account-row');
    if (!row) return;
    const uid = row.getAttribute('data-uid');
    if (row.classList.contains('open')) {
      collapseAccount(uid);
    } else {
      expandAccount(uid);
    }
  });

  $('btnClaim').addEventListener('click', claimAll);
  $('btnRefresh').addEventListener('click', () => refreshAll(false));

  $('autoExpand').addEventListener('change', () => {
    if ($('autoExpand').checked) {
      refreshAll(true);
    } else {
      state.expanded.clear();
      renderAccounts();
    }
  });

  refreshAll(true);
})();
