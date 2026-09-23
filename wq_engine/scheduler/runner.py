"""任务调度器。

设计：
- 一个 batch = 多个 alpha（multi-sim 的 children）
- BatchScheduler 用 threading.Thread 控制并发 batch 数
  （MVP 不强依赖 PySide6；UI 层可替换为 QThreadPool，逻辑一致）
- 每个 batch 走"批量提交 → 轮询进度 → 写回 SQLite"流程
- 支持 pause / resume / cancel
- 断点续跑：从 SQLite 读 pending simulations 继续
- 进度通过回调函数传出（UI 层把回调接到 Qt 信号）
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable

import httpx
from loguru import logger

from ..api.client import APIClient, BrainClientError, RateLimitError
from ..api.config import BrainConfig
from ..storage.database import Storage
from .ra_common import (
    build_sim_payload,
    group_pending_rows,
    is_ra_settings,
    poll_budget_for,
)


ProgressCallback = Callable[[str, dict], None]
"""进度回调签名 (event_name, payload)。

event_name 取值：
  - batch_started   {batch_idx, total_batches, batch_size}
  - batch_submitted {sim_id, progress_url}
  - sim_completed   {sim_id, alpha_id, progress}
  - sim_failed      {sim_id, error}
  - batch_done      {batch_idx, total_batches, success, failed}
  - task_done       {task_run_id, success, failed, status}
  - paused / resumed / cancelled
"""


@dataclass
class BatchResult:
    """单个 batch 运行结果。"""
    success: int = 0
    failed: int = 0

    def merge(self, other: "BatchResult") -> None:
        self.success += other.success
        self.failed += other.failed


class _BatchRunnable:
    """单个 batch 的运行逻辑（multi-sim 批量提交）。

    与原 machine_lib.multi_simulate 对齐：一个 batch = 一次批量 POST
    （body 数组），只占 1 个并发名额，避免逐个提交撞平台并发上限。
    """

    def __init__(
        self,
        scheduler: "BatchScheduler",
        batch_idx: int,
        sim_records: list[dict],   # [{sim_id, expression, decay, settings}]
    ):
        self.scheduler = scheduler
        self.batch_idx = batch_idx
        self.sim_records = sim_records
        # 单条批次标记：提交/轮询/取结果三步都必须走"单模拟"路径，不能混用 multi-sim。
        # 2026-08-29 只改了提交侧（create_simulation 修 400），轮询侧仍按 multi-sim
        # 找 children，而单模拟响应体没有 children → 一律判"平台未返回该模拟的结果"。
        # 实测全库 41 个单条批次 41 个全败（成功率 0.0%），2026-09-02 补齐后两步。
        self._single = len(sim_records) == 1

    def _wait_if_paused(self) -> None:
        while self.scheduler._pause_event.is_set() and not self.scheduler._stop_event.is_set():
            # 0.5s 短轮询已足够快，保持响应取消
            time.sleep(0.5)

    def run(self) -> BatchResult:
        scheduler = self.scheduler
        client = scheduler.client
        result = BatchResult()

        scheduler._emit("batch_started", {
            "batch_idx": self.batch_idx,
            "total_batches": scheduler.total_batches,
            "batch_size": len(self.sim_records),
        })

        # 占并发名额（1 个 multi-sim = 1 个名额）；满则阻塞等待
        slot = scheduler._sim_slots.acquire(timeout=0.5)
        while not slot and not scheduler._stop_event.is_set():
            self._wait_if_paused()
            slot = scheduler._sim_slots.acquire(timeout=0.5)
        if not slot:
            return result  # stop 且无名额
        with scheduler._lock:
            scheduler._active_slots += 1

        try:
            if scheduler._stop_event.is_set():
                return result
            self._wait_if_paused()

            # --- 批量提交（multi-sim，一次 POST 全部） ---
            # v83 RA 原生（260923）：type 由 settings 推导（region=ALL → REGION_AGNOSTIC，
            # RA 附带幂等 settings 修正与轮询预算放宽）；REGULAR 分支与旧内联构建
            # 逐字段一致（见 ra_common.build_sim_payload），region!=ALL 时零漂移。
            is_ra = bool(self.sim_records) and all(
                is_ra_settings(rec.get("settings") or {}) for rec in self.sim_records)
            poll_budget = poll_budget_for(is_ra, scheduler.max_poll_seconds)
            sim_data_list = [build_sim_payload(rec, is_ra) for rec in self.sim_records]
            try:
                # 平台要求：单条 sim 必须不带数组包装直接 POST（2026-08-29 实测：
                # multi-sim 数组只有 1 条时返回 400 "Single simulations are required
                # to be submitted without the wrapping array"）
                if self._single:
                    progress_url = client.create_simulation(sim_data_list[0])
                else:
                    progress_url = client.create_multi_simulations(sim_data_list)
                # v28：把响应头的每日配额同步到 scheduler（UI 回测槽显示用）
                try:
                    rl = getattr(client, "last_ratelimit", None)
                    if rl:
                        scheduler.sim_quota = rl
                except Exception:
                    pass
            except (BrainClientError, httpx.HTTPStatusError, httpx.HTTPError) as e:
                # 批量提交失败：整批标记失败
                # （HTTPStatusError/HTTPError 兜底：平台 4xx 与纯网络错误
                #   都不能裸逃逸，否则 batch 线程崩溃、sim 永久卡 submitted）
                for rec in self.sim_records:
                    scheduler.storage.mark_simulation_failed(rec["sim_id"], str(e))
                    scheduler._emit("sim_failed", {"sim_id": rec["sim_id"], "error": str(e), "batch_idx": self.batch_idx})
                result.failed += len(self.sim_records)
                return result

            # 记录 progress_url（同一 multi-sim 共享）
            for rec in self.sim_records:
                scheduler.storage.mark_simulation_submitted(rec["sim_id"], progress_url)

            # 批量提交完成（post done），供 UI 实时进度/读秒展示
            scheduler._emit("batch_submitted", {
                "batch_idx": self.batch_idx,
                "sim_id": progress_url,
                "batch_size": len(self.sim_records),
            })

            # --- 轮询 multi-sim 直到 COMPLETE/ERROR ---
            # 真实平台行为（用户实测 + 原 machine_lib 对照）：
            # 模拟运行期间 GET 返回 Retry-After（实测 ~5s），按次数预算（60×5s≈5 分钟）
            # 会在模拟完成（5-10 分钟）前耗尽 → 整批误判"轮询超时"。
            # 原 machine_lib 是 while True 无限轮询（Retry-After 消失才停）。
            # 这里对齐：时间预算（默认 30 分钟），Retry-After 至少等 15s。
            children: list | None = None
            final_status = ""
            data: dict = {}          # 兜底：轮询一次都没跑时下面引用 data 不会 NameError
            poll_deadline = time.time() + poll_budget
            attempt = 0
            while time.time() < poll_deadline and attempt < scheduler.max_poll_attempts:
                attempt += 1
                if scheduler._stop_event.is_set():
                    break
                try:
                    # 单条走单模拟进度接口（响应体直接含 alpha，无 children）
                    if self._single:
                        data = client.get_simulation_progress(progress_url)
                    else:
                        data = client.get_multi_sim_progress(progress_url)
                except RateLimitError as e:
                    # v28：记录平台限流重置时刻（UI 回测槽倒计时用）
                    try:
                        scheduler.rate_limit_reset_at = time.time() + max(
                            e.retry_after if e.retry_after else scheduler.min_rate_limit_sleep,
                            15.0,
                        )
                    except Exception:
                        pass
                    # Retry-After 尊重但至少等 15s（避免 5s 快速轮询烧预算）
                    sleep_s = max(
                        min(
                            e.retry_after if e.retry_after else scheduler.min_rate_limit_sleep,
                            60.0,
                        ),
                        scheduler.min_rate_limit_sleep,
                    )
                    # 可中断等待：取消时立即退出（否则要等 15s+ 才能响应取消）
                    if scheduler._stop_event.wait(sleep_s):
                        break
                    continue
                except BrainClientError as e:
                    # 取消时 client 退避重试被中断会抛"任务已取消"
                    if scheduler._stop_event.is_set():
                        break
                    # 修复：非取消的持续失败不能 re-raise（否则 sim 永留 submitted 孤儿），
                    # 整批标失败退出
                    for rec in self.sim_records:
                        scheduler.storage.mark_simulation_failed(rec["sim_id"], str(e))
                        scheduler._emit("sim_failed", {"sim_id": rec["sim_id"], "error": str(e), "batch_idx": self.batch_idx})
                    result.failed += len(self.sim_records)
                    return result
                except httpx.HTTPStatusError as e:
                    # 平台 4xx：不能裸逃逸杀线程，整批标失败
                    for rec in self.sim_records:
                        scheduler.storage.mark_simulation_failed(rec["sim_id"], str(e))
                        scheduler._emit("sim_failed", {"sim_id": rec["sim_id"], "error": str(e), "batch_idx": self.batch_idx})
                    result.failed += len(self.sim_records)
                    return result
                final_status = data.get("status") or ""
                # v82（260923 实测）：WARNING 也是终态 —— 反转类表达式平台会给
                # 「may not accept these alphas in the future」提示但模拟已成功
                # （响应体带 alpha）。白名单漏了它会让批次死等到轮询超时。
                if final_status in ("COMPLETE", "ERROR", "WARNING"):
                    if final_status == "WARNING":
                        logger.warning(
                            "模拟完成但带 WARNING：{}",
                            str(data.get("message") or "")[:200],
                        )
                    children = data.get("children") or []
                    break
                # RUNNING/QUEUED 中间态：真实模拟 5-10 分钟，慢慢等（可中断）
                if scheduler._stop_event.wait(scheduler.multi_sim_poll_interval):
                    break

            # --- 单条批次：直接从响应体取 alpha，不去 multi-sim 里找 children ---
            # （2026-09-02 修复）走到这里说明已 COMPLETE/ERROR/WARNING 或超时。
            if self._single:
                rec0 = self.sim_records[0]
                if children is None:
                    if scheduler._stop_event.is_set():
                        scheduler.storage.mark_simulation_cancelled(rec0["sim_id"], "任务取消")
                        scheduler._emit("sim_failed", {"sim_id": rec0["sim_id"], "error": "任务取消", "batch_idx": self.batch_idx})
                        return result  # 取消不计入失败统计
                    reason = f"轮询超时（{poll_budget:.0f}s 预算）"
                    scheduler.storage.mark_simulation_failed(rec0["sim_id"], reason)
                    scheduler._emit("sim_failed", {"sim_id": rec0["sim_id"], "error": reason, "batch_idx": self.batch_idx})
                    result.failed += 1
                    return result
                alpha_id = (data or {}).get("alpha")
                if alpha_id:
                    scheduler.storage.mark_simulation_completed(rec0["sim_id"], alpha_id, progress=100)
                    scheduler._emit("sim_completed", {
                        "sim_id": rec0["sim_id"], "alpha_id": alpha_id, "progress": 100,
                        "batch_idx": self.batch_idx, "expression": rec0.get("expression", ""),
                    })
                    result.success += 1
                else:
                    msg = (data or {}).get("message") or (data or {}).get("status") or "模拟失败"
                    scheduler.storage.mark_simulation_failed(rec0["sim_id"], str(msg))
                    scheduler._emit("sim_failed", {"sim_id": rec0["sim_id"], "error": str(msg), "batch_idx": self.batch_idx})
                    result.failed += 1
                return result

            # --- 逐个子模拟查结果（children 顺序与提交顺序对应） ---
            if children is None:
                # 区分"取消"与"真超时"，避免取消时误报轮询超时
                if scheduler._stop_event.is_set():
                    reason = "任务取消"
                    for rec in self.sim_records:
                        scheduler.storage.mark_simulation_cancelled(rec["sim_id"], reason)
                        scheduler._emit("sim_failed", {"sim_id": rec["sim_id"], "error": reason, "batch_idx": self.batch_idx})
                    return result  # 取消不计入失败统计
                reason = f"轮询超时（{poll_budget:.0f}s 预算）"
                for rec in self.sim_records:
                    scheduler.storage.mark_simulation_failed(rec["sim_id"], reason)
                    scheduler._emit("sim_failed", {"sim_id": rec["sim_id"], "error": reason, "batch_idx": self.batch_idx})
                result.failed += len(self.sim_records)
                return result

            # children 少于 sim_records：平台漏返回的 sim 不能静默悬挂
            if len(children) < len(self.sim_records):
                for rec in self.sim_records[len(children):]:
                    msg = "平台未返回该模拟的结果"
                    scheduler.storage.mark_simulation_failed(rec["sim_id"], msg)
                    scheduler._emit("sim_failed", {"sim_id": rec["sim_id"], "error": msg, "batch_idx": self.batch_idx})
                    result.failed += 1

            for i, child_id in enumerate(children):
                if i >= len(self.sim_records):
                    break
                rec = self.sim_records[i]
                try:
                    child_data = client.get_single_sim_alpha(child_id)
                except (BrainClientError, httpx.HTTPStatusError,
                        AttributeError, TypeError) as e:
                    scheduler.storage.mark_simulation_failed(rec["sim_id"], str(e))
                    scheduler._emit("sim_failed", {"sim_id": rec["sim_id"], "error": str(e), "batch_idx": self.batch_idx})
                    result.failed += 1
                    continue
                if not isinstance(child_data, dict):
                    msg = "平台返回了非预期结构"
                    scheduler.storage.mark_simulation_failed(rec["sim_id"], msg)
                    scheduler._emit("sim_failed", {"sim_id": rec["sim_id"], "error": msg, "batch_idx": self.batch_idx})
                    result.failed += 1
                    continue
                alpha_id = child_data.get("alpha")
                if alpha_id:
                    scheduler.storage.mark_simulation_completed(
                        rec["sim_id"], alpha_id, progress=100,
                    )
                    scheduler._emit("sim_completed", {
                        "sim_id": rec["sim_id"], "alpha_id": alpha_id, "progress": 100,
                        "batch_idx": self.batch_idx, "expression": rec.get("expression", ""),
                    })
                    result.success += 1
                else:
                    msg = child_data.get("message") or child_data.get("status") or "模拟失败"
                    scheduler.storage.mark_simulation_failed(rec["sim_id"], str(msg))
                    scheduler._emit("sim_failed", {"sim_id": rec["sim_id"], "error": str(msg), "batch_idx": self.batch_idx})
                    result.failed += 1
        finally:
            with scheduler._lock:
                scheduler._active_slots = max(0, scheduler._active_slots - 1)
            scheduler._sim_slots.release()  # 释放名额
            # batch_done 放 finally：提前 return / 异常路径也保证事件完整
            scheduler._emit("batch_done", {
                "batch_idx": self.batch_idx,
                "total_batches": scheduler.total_batches,
                "success": result.success,
                "failed": result.failed,
            })
        return result


class BatchScheduler:
    """批量模拟调度器。

    用法：
        scheduler = BatchScheduler(client, storage, progress_cb=print_progress)
        scheduler.run(task_run_id, expressions)
        # 或断点续跑：
        scheduler.continue_run(task_run_id)

    控制方法：
        scheduler.pause() / scheduler.resume() / scheduler.cancel()
    """

    def __init__(
        self,
        client: APIClient,
        storage: Storage,
        progress_cb: ProgressCallback | None = None,
        max_concurrent_batches: int = 3,
        batch_size: int = 10,
        poll_interval: float = 2.0,
        max_retries: int = 5,
        backoff_factor: float = 1.5,
        max_poll_attempts: int = 240,
        max_poll_seconds: float = 1800.0,
        multi_sim_poll_interval: float = 15.0,
        min_rate_limit_sleep: float = 15.0,
        max_concurrent_simulations: int = 4,
    ):
        self.client = client
        self.storage = storage
        self._cb = progress_cb or (lambda *_: None)
        self.max_concurrent_batches = max(1, max_concurrent_batches)
        self.batch_size = batch_size
        self.poll_interval = poll_interval
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.max_poll_attempts = max_poll_attempts
        self.max_poll_seconds = max_poll_seconds
        self.multi_sim_poll_interval = multi_sim_poll_interval
        self.min_rate_limit_sleep = min_rate_limit_sleep
        # 全局并发 multi-sim 名额（1 个 multi-sim = 1 个名额，对齐原 machine_lib 批量提交）
        self.max_concurrent_simulations = max_concurrent_simulations
        self._sim_slots = threading.BoundedSemaphore(max_concurrent_simulations)

        self.total_batches = 0
        self._pause_event = threading.Event()
        self._stop_event = threading.Event()
        self._workers: list[threading.Thread] = []
        self._batch_results: list[BatchResult] = []
        self._lock = threading.Lock()
        # v28：最近一次平台限流（429）的重置时刻（time.time() 时间戳），UI 显示倒计时用
        self.rate_limit_reset_at: float | None = None
        # v28：最近一次 /simulations 提交响应头的每日回测配额（真实值，插件同款）
        self.sim_quota: dict | None = None
        # 实时在飞批次/已用槽计数：acquire 名额时 +1，release 时 -1（web 并发卡实时显示用）
        self._active_slots: int = 0

    def _emit(self, event: str, payload: dict) -> None:
        try:
            self._cb(event, payload)
        except Exception as e:
            logger.warning("progress_cb 异常：{}", e)

    # -------- control --------

    def pause(self) -> None:
        self._pause_event.set()
        self._emit("paused", {})

    def resume(self) -> None:
        self._pause_event.clear()
        self._emit("resumed", {})

    def cancel(self) -> None:
        self._stop_event.set()
        self._emit("cancelled", {})

    def join(self, timeout: float | None = None) -> None:
        for w in self._workers:
            w.join(timeout)

    # -------- run --------

    def run(
        self,
        task_run_id: int,
        expressions: Iterable[tuple[str, int, dict]],
    ) -> None:
        """启动批量模拟（首次跑）。

        expressions: (expression, decay, settings) 迭代器
        """
        # 复位控制事件：同一 scheduler 取消/暂停后仍可再次 run/continue_run
        self._stop_event.clear()
        self._pause_event.clear()
        count = self.storage.bulk_create_simulations(task_run_id, expressions)
        self.storage.update_task_run_status(
            task_run_id, "running", success=0, failed=0,
        )
        logger.info("task_run {} 共 {} 个 simulation 入库", task_run_id, count)
        self._run_pending(task_run_id)

    def continue_run(self, task_run_id: int) -> None:
        """断点续跑：从 SQLite 读 pending simulations 继续。"""
        self._stop_event.clear()
        self._pause_event.clear()
        self.storage.update_task_run_status(task_run_id, "running")
        self._run_pending(task_run_id)

    def _run_pending(self, task_run_id: int) -> None:
        # 修复：孤儿 submitted（上次进程中断，已提交平台但本地无结果）——
        # progress_url 无法跨进程恢复轮询，标 cancelled 并提示，防永久卡死/重复烧配额
        try:
            orphan = self.storage.list_orphan_submitted(task_run_id)
            for s in orphan:
                self.storage.mark_simulation_cancelled(
                    s["id"], "进程中断：模拟可能已在平台运行，请到平台确认结果"
                )
                self._emit("sim_failed", {"sim_id": s["id"], "error": "进程中断（孤儿 submitted）", "batch_idx": 0})
            if orphan:
                logger.warning("task_run {} 有 {} 条孤儿 submitted 已标取消", task_run_id, len(orphan))
        except Exception:
            pass
        all_pending = self.storage.list_pending_simulations(
            task_run_id, limit=10_000,
            max_retries=max(1, self.max_retries),
        )
        # 修复：超过 1 万条时循环分批（已处理的从 pending 移除，下一轮取到剩余；
        # 每轮上限 10_000 防单次查询过大）
        if not all_pending:
            logger.info("task_run {} 无 pending simulation，结束", task_run_id)
            self.storage.update_task_run_status(task_run_id, "completed")
            # 修复：空任务也必须发 task_done，否则 UI 卡在"运行中…"（timer 不停、按钮不恢复）；
            # success/failed 读 DB 累计值（续跑已完成任务时不再显示 0/0）
            _s = _f = 0
            try:
                _t = self.storage.get_task_run(task_run_id)
                if _t:
                    _s = int(_t.get("success") or 0)
                    _f = int(_t.get("failed") or 0)
            except Exception:
                pass
            self._emit("task_done", {
                "task_run_id": task_run_id,
                "success": _s,
                "failed": _f,
                "status": "completed",
            })
            return

        # v83 RA 原生（260923）：RA 行独占成批（单条 POST——multi 数组对
        # REGION_AGNOSTIC 未验证，社区与原旁路均单条）；无 RA 行时与旧切法逐位一致
        batches_data = group_pending_rows(all_pending, self.batch_size)
        self.total_batches = len(batches_data)
        logger.info(
            "task_run {} 共 {} 个 simulation，分 {} 个 batch，并发 {}",
            task_run_id, len(all_pending), self.total_batches, self.max_concurrent_batches,
        )

        sim_records_per_batch: list[list[dict]] = []
        for batch in batches_data:
            records: list[dict] = []
            for row in batch:
                try:
                    settings = json.loads(row.get("settings_json") or "{}")
                except (json.JSONDecodeError, TypeError):
                    # 单行 settings 损坏：标记失败跳过，不让整个 run() 崩溃
                    msg = "settings 数据损坏"
                    self.storage.mark_simulation_failed(row["id"], msg)
                    self._emit("sim_failed", {"sim_id": row["id"], "error": msg})
                    continue
                records.append({
                    "sim_id": row["id"],
                    "expression": row["expression"],
                    "decay": row["decay"],
                    "settings": settings,
                })
            # 修复：整批全损坏时跳过空批次（不能向平台发空 body POST）
            if records:
                sim_records_per_batch.append(records)

        def worker(idx: int, records: list[dict]) -> None:
            if self._stop_event.is_set():
                return
            runnable = _BatchRunnable(self, idx, records)
            result = runnable.run()
            with self._lock:
                self._batch_results.append(result)

        # 固定线程池（并发 = max_concurrent_batches），替代每 batch 一个裸线程
        # v83 RA 原生：RA 批走**独立单 worker 池**（1 条 RA = 1 Parent + 4 Child =
        # 5 个平台并发槽，批内并行必 429），与常规池同时运行、互不占用对方名额；
        # 非 RA 批的提交顺序、batch_idx 与行为不变。
        from concurrent.futures import ThreadPoolExecutor
        self._workers = []
        with ThreadPoolExecutor(max_workers=max(1, self.max_concurrent_batches)) as pool, \
                ThreadPoolExecutor(max_workers=1) as ra_pool:
            futures = []
            for idx, records in enumerate(sim_records_per_batch):
                is_ra_rec = bool(records) and is_ra_settings(records[0].get("settings") or {})
                target = ra_pool if is_ra_rec else pool
                futures.append(target.submit(worker, idx, records))
            for f in futures:
                exc = f.exception()  # 内部已处理；兜底记录，避免静默
                if exc is not None:
                    logger.warning("batch worker 未处理异常：{}", exc)

        total = BatchResult()
        for r in self._batch_results:
            total.merge(r)
        self._batch_results.clear()

        if self._stop_event.is_set():
            final_status = "cancelled"
        elif total.failed > 0 and total.success == 0:
            final_status = "failed"
        else:
            final_status = "completed"
        # 修复：续跑时累加历史计数（否则"首跑 10 成功→续跑 5 成功"显示 5 而非 15）
        prev_s = prev_f = 0
        try:
            prev = self.storage.get_task_run(task_run_id)
            if prev:
                prev_s = int(prev.get("success") or 0)
                prev_f = int(prev.get("failed") or 0)
        except Exception:
            pass
        self.storage.update_task_run_status(
            task_run_id, final_status,
            success=prev_s + total.success, failed=prev_f + total.failed,
        )
        # 修复：task_done 与 DB 一致用累计值（与空任务分支对齐）
        self._emit("task_done", {
            "task_run_id": task_run_id,
            "success": prev_s + total.success,
            "failed": prev_f + total.failed,
            "status": final_status,
        })