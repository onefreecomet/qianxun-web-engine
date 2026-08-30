"""PnL 同步模块（v80+）。

移植自 ProdMemo（https://github.com/myacgl/ProdMemo）的 inject.js：
- 全量拉已提交 alpha 列表（list_all_submitted_alphas）
- 分批并发拉 PnL（每批 100、并发 3）
- warm-up + retry 三轮（1s/2s/4s）
- 失败 ID 留底，结束返回
- 支持可中断（stop_event）

输入：APIClient + Storage + 进度回调
输出：写 alphas.pnl_json / pnl_fetched_at 字段，触发可选的 local_corr 计算
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Callable

from wq_engine.api.client import APIClient
from wq_engine.api.local_corr import calculate_correlation
from wq_engine.storage.database import Storage

log = logging.getLogger(__name__)

# 协议常量（与 ProdMemo 一致）
BATCH_SIZE = 100
CONCURRENCY = 3
RETRY_DELAYS = (1.0, 2.0, 4.0)


def sync_pnls(
    client: APIClient,
    storage: Storage,
    *,
    progress_cb: Callable[[dict], None] | None = None,
    stop_event: threading.Event | None = None,
    compute_corr: bool = True,
    alpha_id_filter: set[str] | None = None,
    incremental: bool = True,
    force_full: bool = False,
) -> dict:
    """同步 PnL 全流程。

    progress_cb({"phase": ..., "current": N, "total": N, "success": N, "failed": N,
                 "message": str}) 用于 UI/WS 推送。

    alpha_id_filter 非空时只同步这些 alpha（用于增量更新）。

    返回 {"total": N, "success": N, "failed_ids": [...], "corr_computed": N}
    """
    stop_event = stop_event or threading.Event()

    def emit(payload: dict) -> None:
        log.info("pnl_sync %s", payload.get("phase"))
        if progress_cb:
            try:
                progress_cb(payload)
            except Exception as e:
                log.warning("progress_cb 抛异常：%s", e)

    # -------- 阶段 1：拉 alpha 列表 --------
    emit({"phase": "alphas", "current": 0, "total": 0, "success": 0, "failed": 0,
          "message": "拉已提交 alpha 列表…"})

    def _alphas_progress(done: int, total: int, last_id: str | None) -> None:
        emit({"phase": "alphas", "current": done, "total": total, "success": done,
              "failed": 0, "message": f"已扫 {done}/{total} 条"})

    alphas = client.list_all_submitted_alphas(page_size=BATCH_SIZE,
                                              progress_cb=_alphas_progress)
    if stop_event.is_set():
        return {"total": len(alphas), "success": 0, "failed_ids": [], "corr_computed": 0,
                "stopped": True}

    # 过滤：只同步要那些
    if alpha_id_filter:
        alphas = [a for a in alphas if a["id"] in alpha_id_filter]
    # 增量模式（默认开启）：只拉本地从未成功抓取过 PnL 的 alpha，跳过已拉取的
    elif incremental and not force_full:
        try:
            rows = storage._conn.execute(
                "SELECT alpha_id, pnl_fetched_at FROM alphas"
            ).fetchall()
            fetched = {row[0]: row[1] for row in rows}
            before = len(alphas)
            alphas = [a for a in alphas if not fetched.get(a["id"])]
            skipped = before - len(alphas)
            emit({"phase": "alphas", "current": before, "total": before,
                  "success": before, "failed": 0,
                  "message": f"增量模式：跳过 {skipped} 个已拉取的 alpha，待拉 {len(alphas)} 个"})
        except Exception as e:
            log.warning("增量过滤失败，回退全量：%s", e)

    total = len(alphas)
    if total == 0:
        emit({"phase": "completed", "current": 0, "total": 0, "success": 0,
              "failed": 0, "message": "没有需要同步的 alpha"})
        return {"total": 0, "success": 0, "failed_ids": [], "corr_computed": 0}

    # -------- 阶段 2：分批拉 PnL --------
    emit({"phase": "pnl", "current": 0, "total": total, "success": 0,
          "failed": 0, "message": f"开始拉 {total} 个 PnL…"})

    saved_ids: set[str] = set()
    failed_ids: set[str] = set()
    failed_reasons: dict[str, str] = {}  # 失败原因（取每个 alpha 的最后一次报错）
    pnl_records_by_alpha: dict[str, dict] = {}  # 给 corr 计算用

    for start in range(0, total, BATCH_SIZE):
        if stop_event.is_set():
            break
        batch = alphas[start:start + BATCH_SIZE]
        batch_no = start // BATCH_SIZE + 1
        total_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE

        emit({"phase": "pnl-warmup", "current": start, "total": total,
              "success": len(saved_ids), "failed": len(failed_ids),
              "message": f"Warm-up 第 {batch_no}/{total_batches} 批（{len(batch)} 条）…"})

        # warm-up：先所有 ID 拉一遍
        pending = _fetch_pnl_round(client, batch, pnl_records_by_alpha, stop_event,
                                    saved_ids, failed_ids, failed_reasons)
        # retry 三轮
        for round_idx, wait_s in enumerate(RETRY_DELAYS):
            if stop_event.is_set() or not pending:
                break
            emit({"phase": "pnl-retry", "current": start + len(batch) - len(pending),
                  "total": total, "success": len(saved_ids), "failed": len(pending),
                  "message": f"第 {batch_no}/{total_batches} 批：{len(pending)} 个 pending，"
                             f"{wait_s:.0f}s 后重试…"})
            time.sleep(wait_s)
            pending = _fetch_pnl_round(client, pending, pnl_records_by_alpha,
                                        stop_event, saved_ids, failed_ids, failed_reasons)

    if stop_event.is_set():
        return {"total": total, "success": len(saved_ids),
                "failed_ids": sorted(failed_ids), "corr_computed": 0,
                "stopped": True}

    # -------- 阶段 3：写 PnL 到 alphas 表 --------
    emit({"phase": "writing", "current": 0, "total": len(pnl_records_by_alpha),
          "success": len(saved_ids), "failed": len(failed_ids),
          "message": "把 PnL 写进 alphas.pnl_json…"})
    _write_pnls_to_storage(storage, pnl_records_by_alpha)

    # -------- 阶段 4（可选）：本地 corr 计算 --------
    corr_computed = 0
    if compute_corr and saved_ids:
        emit({"phase": "corr", "current": 0, "total": len(saved_ids),
              "success": 0, "failed": 0,
              "message": "本地 Self/PPA Corr 计算…"})
        corr_computed = _compute_and_store_corrs(
            client, storage, alphas, pnl_records_by_alpha, saved_ids, emit, stop_event,
        )

    emit({"phase": "completed", "current": total, "total": total,
          "success": len(saved_ids), "failed": len(failed_ids),
          "message": f"完成：{len(saved_ids)}/{total} 个 PnL，{corr_computed} 个 corr 计算"})
    # 失败原因聚合（按错误模式分组，方便定位主要问题）
    reason_groups: dict[str, list[str]] = {}
    for aid, reason in failed_reasons.items():
        norm = _normalize_reason(reason)
        reason_groups.setdefault(norm, []).append(aid)
    summary = sorted(
        [{"pattern": k, "count": len(v), "sample_ids": v[:5]} for k, v in reason_groups.items()],
        key=lambda x: x["count"], reverse=True,
    )
    return {
        "total": total,
        "success": len(saved_ids),
        "failed_ids": sorted(failed_ids),
        "failed_reasons": summary,
        "corr_computed": corr_computed,
    }


def _normalize_reason(reason: str) -> str:
    """把失败原因标准化为可聚合的模式（去 URL/长 ID/截断）。"""
    import re
    s = re.sub(r"https?://\S+", "<URL>", reason)
    s = re.sub(r"\b[A-Za-z0-9]{15,}\b", "<ID>", s)
    return s[:100]


def _fetch_pnl_round(
    client: APIClient,
    alpha_batch: list[dict],
    pnl_records_by_alpha: dict[str, dict],
    stop_event: threading.Event,
    saved_ids: set[str],
    failed_ids: set[str],
    failed_reasons: dict[str, str] | None = None,
) -> list[dict]:
    """并发拉一批 PnL。返回仍 pending 的 alpha dict 列表（按 array）。

    关键（v80+ bug 修复）：worker 是 while-True 调度循环，处理完一个 alpha 后继续
    拿下一个，直到所有 alpha 处理完。早期的实现里 worker 处理完就 return，导致
    3 个 worker × 1 个 alpha = 只处理 3 个就 join 结束——113 个 alpha 实际只跑了
    几个就退出。
    """
    if failed_reasons is None:
        failed_reasons = {}
    next_idx = 0
    lock = threading.Lock()
    pending: list[dict] = []

    def worker() -> None:
        nonlocal next_idx
        while True:
            if stop_event.is_set():
                return
            with lock:
                if next_idx >= len(alpha_batch):
                    return
                idx = next_idx
                next_idx += 1
            a = alpha_batch[idx]
            aid = a["id"]
            try:
                pnl_list = client.get_alpha_pnl(aid)
                # 转回 ProdMemo 格式 [date, cum_pnl] 二维数组存库
                if pnl_list and isinstance(pnl_list, list):
                    records = [[r.get("date") or r.get("Date"), r.get("pnl") or r.get("PnL")]
                               for r in pnl_list if isinstance(r, dict)]
                    if records:
                        pnl_records_by_alpha[aid] = {"records": records}
                        with lock:
                            saved_ids.add(aid)
                        continue  # 关键：继续拿下一个 alpha，不退出
                    else:
                        with lock:
                            failed_reasons[aid] = "PnL 响应为空（可能是 recordsets 还没生成）"
                else:
                    with lock:
                        failed_reasons[aid] = f"PnL 返回格式异常：{type(pnl_list).__name__}"
            except Exception as e:
                with lock:
                    failed_reasons[aid] = f"{type(e).__name__}: {str(e)[:120]}"
                log.warning("PnL 拉取失败 %s: %s", aid, e)
            # 没成功 → 进 pending 等下一轮 retry
            with lock:
                pending.append(a)
                failed_ids.add(aid)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(min(CONCURRENCY, len(alpha_batch)))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return pending


def _write_pnls_to_storage(storage: Storage, pnl_records_by_alpha: dict[str, dict]) -> None:
    """写 pnl_json + pnl_fetched_at 到 alphas 表。"""
    now = datetime.now(timezone.utc).isoformat()
    with storage._lock:
        for aid, pnl_data in pnl_records_by_alpha.items():
            try:
                storage._conn.execute(
                    "UPDATE alphas SET pnl_json=?, pnl_fetched_at=? WHERE alpha_id=?",
                    (json.dumps(pnl_data), now, aid),
                )
            except Exception as e:
                log.warning("写 pnl_json 失败 %s: %s", aid, e)
        storage._conn.commit()


def _compute_and_store_corrs(
    client: APIClient,
    storage: Storage,
    alphas: list[dict],
    pnl_records_by_alpha: dict[str, dict],
    saved_ids: set[str],
    emit: Callable[[dict], None],
    stop_event: threading.Event,
) -> int:
    """对每个已拉 PnL 的 alpha 算 Self + PPA corr，结果写回 alphas 表。

    只对 OS 阶段、region 已知、PnL 足够长的 alpha 计算。
    """
    # 准备 alpha 元数据：直接用 list_all_submitted 拿到的（已含 stage/classifications/region）
    # 注意：list 给的是 settings.region（在 alpha.settings.region 路径）
    # 但 ProdMemo 用的是 alpha.settings.region（因为 list 接口已经返回完整 settings 对象）
    by_id = {a["id"]: a for a in alphas}
    target_ids = list(saved_ids)
    computed = 0
    total = len(target_ids)
    for i, aid in enumerate(target_ids, 1):
        if stop_event.is_set():
            return computed
        a = by_id.get(aid)
        if not a:
            continue
        # 只算 OS 的（Self 池子条件）
        if a.get("stage") != "OS":
            continue
        settings = a.get("settings") or {}
        if not settings.get("region"):
            continue
        pnl_target = pnl_records_by_alpha.get(aid)
        if not pnl_target:
            continue
        try:
            self_r = calculate_correlation(aid, "SELF", alphas, pnl_records_by_alpha)
        except Exception as e:
            log.warning("Self corr %s 失败: %s", aid, e)
            self_r = None
        try:
            ppa_r = calculate_correlation(aid, "PPA", alphas, pnl_records_by_alpha)
        except Exception as e:
            log.warning("PPA corr %s 失败: %s", aid, e)
            ppa_r = None
        # 更新数据库
        with storage._lock:
            try:
                storage._conn.execute(
                    """UPDATE alphas SET
                        self_corr_min=?, self_corr_max=?, self_corr_top5=?,
                        ppa_corr_min=?, ppa_corr_max=?, ppa_corr_top5=?,
                        max_corr=?, max_corr_source=?, corr_computed_at=?
                       WHERE alpha_id=?""",
                    (
                        self_r["min"] if self_r else None,
                        self_r["max"] if self_r else None,
                        json.dumps(self_r["top5"]) if self_r else None,
                        ppa_r["min"] if ppa_r else None,
                        ppa_r["max"] if ppa_r else None,
                        json.dumps(ppa_r["top5"]) if ppa_r else None,
                        max((abs(self_r["max"]) if self_r else 0),
                            (abs(ppa_r["max"]) if ppa_r else 0),
                            abs(a.get("check_pc") or 0)) if (self_r or ppa_r or a.get("check_pc") is not None) else None,
                        _pick_source(self_r, ppa_r, a.get("check_pc")),
                        datetime.now(timezone.utc).isoformat(),
                        aid,
                    ),
                )
                storage._conn.commit()
                computed += 1
            except Exception as e:
                log.warning("写 corr 失败 %s: %s", aid, e)
        if i % 10 == 0:
            emit({"phase": "corr", "current": i, "total": total,
                  "success": computed, "failed": i - computed,
                  "message": f"corr 计算 {computed}/{total}"})
    return computed


def _pick_source(self_r, ppa_r, prod_pc) -> str | None:
    """挑 max corr 来源（self / ppa / prod）。"""
    candidates = []
    if self_r:
        candidates.append(("self", abs(self_r["max"])))
    if ppa_r:
        candidates.append(("ppa", abs(ppa_r["max"])))
    if prod_pc is not None:
        candidates.append(("prod", abs(prod_pc)))
    if not candidates:
        return None
    return max(candidates, key=lambda c: c[1])[0]