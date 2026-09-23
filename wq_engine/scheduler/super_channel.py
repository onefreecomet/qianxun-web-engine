"""SUPER（SuperAlpha）批���专用通道（v82 从 mcp_server 抽出为公共模块）。

背景（260921）：平台 SUPER 的 payload 是 {type:SUPER, settings, selection, combo}，
与 regular 的 {type:REGULAR, settings, regular} 结构不同；BatchScheduler 的
sim_records 只认 `regular` 表达式，传 selection/combo 会直接 KeyError。所以 SUPER
走独立通道，不改动 scheduler 的任何逻辑；**不写 simulations 表**（该表按 regular
语义建模，qianxun_resume 的 continue_run 会把它当 regular 重建 → 污染），
结果直接进 alphas 表，qianxun_analyze / 批次对账按 batch_no 照常可用。

v82 起本模块同时服务 MCP（qianxun_submit）与 web（POST /api/batches）两条入口：
- 去重：alphas.expr_key = expression_key("SA\\x1fselection\\x1fcombo", settings)
  （SUPER 不进 simulations，去重指纹落在 alphas 上，Storage.completed_super_keys 读取）
- 预筛：GET /simulations/super-selection（project033 实测端点），组件数 <10 直接跳过
  （平台硬门槛 "At least 10 component alphas are required"），预筛端点异常时放行
  （尽力而为，平台自己是最终裁判）
- 并发：线程池 max_workers=min(3, 用户并发配置)（平台并发模拟上限 3，
  超限 429 由 APIClient._request_with_retry 的 CONCURRENT_SIMULATION 分支兜底）
- 轮询：get_simulation_progress 无 Retry-After 才返回 body（在跑时抛 RateLimitError），
  与 project033「GET Location 直到无 Retry-After」口径一致
"""

from __future__ import annotations

import logging
import threading
import time

from wq_engine.api.client import BrainClientError, RateLimitError
from wq_engine.storage.database import expression_key

log = logging.getLogger("qianxun.super")

#: 平台硬门槛：SA 至少 10 个组件 alpha
MIN_COMPONENTS = 10
#: 预筛端点只认这几个 settings 键（project033 实测参数表）
_PRESCREEN_KEYS = ("region", "delay", "instrumentType", "selectionLimit", "selectionHandling")


def is_super_batch(expressions: list) -> bool:
    """批次元素带 selection / combo 键 → 视为 SUPER 批次。"""
    return any(
        isinstance(e, dict) and ("selection" in e or "combo" in e)
        for e in expressions
    )


def super_expr_key(selection: str, combo: str, settings: dict) -> str:
    """SUPER 去重指纹：selection + combo + settings（复用 expression_key 的字段子集）。"""
    return expression_key(f"SA\x1f{selection}\x1f{combo}", settings)


def super_todo(expressions: list, settings: dict, st=None) -> tuple[list[dict], int]:
    """归一化 + 去重，返回 (todo, skipped)。

    - combo 缺省/空 → "1"（等权 baseline）
    - selection 缺省/空、非 dict 元素 → 丢弃（计入 skipped）
    - 同批内 (selection, combo, settings) 重复 → 丢弃（网格组合文件常见）
    - st 传入时对照 alphas.expr_key 过滤跨批已回测项（st=None 仅做归一化，供单测）
    """
    done = st.completed_super_keys() if st is not None else set()
    seen: set[str] = set()
    todo: list[dict] = []
    skipped = 0
    for e in expressions:
        if not isinstance(e, dict):
            skipped += 1
            log.warning("SUPER 元素非 dict，丢弃：%r", e)
            continue
        sel = str(e.get("selection") or "").strip()
        if not sel:
            skipped += 1
            log.warning("SUPER 元素缺 selection，丢弃：%r", e)
            continue
        combo = str(e.get("combo") or "").strip() or "1"
        key = super_expr_key(sel, combo, settings)
        if key in done:
            skipped += 1
            continue
        if key in seen:
            skipped += 1
            continue
        seen.add(key)
        todo.append({"selection": sel, "combo": combo, "expr_key": key})
    return todo, skipped


def prescreen_params(selection: str, settings: dict) -> dict:
    """组预筛端点查询参数（只带 settings 里实际存在的键）。"""
    params = {k: settings[k] for k in _PRESCREEN_KEYS if k in settings}
    params["selection"] = selection
    return params


def parse_prescreen_count(data) -> int | None:
    """预筛响应 → 组件数。形状未知/无法解析返回 None（= 未知 → 放行）。"""
    if isinstance(data, list):
        return len(data)
    if not isinstance(data, dict):
        return None
    for k in ("count", "total", "num"):
        v = data.get(k)
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            return int(v)
    for k in ("results", "alphas", "items"):
        v = data.get(k)
        if isinstance(v, list):
            return len(v)
    return None


def _prescreen(client, selection: str, settings: dict, cache: dict, lock) -> int | None:
    """线程内预筛（带缓存）。端点异常/计算中 → None（放行）。"""
    params = prescreen_params(selection, settings)
    cache_key = tuple(sorted(params.items()))
    with lock:
        if cache_key in cache:
            return cache[cache_key]
    cnt: int | None = None
    try:
        cnt = parse_prescreen_count(client.get_super_selection(params))
    except Exception as e:  # noqa: BLE001  预筛是尽力而为，绝不因它挂掉整批
        log.warning("SA 预筛端点异常（放行，交给平台裁判）：%s", e)
    with lock:
        cache[cache_key] = cnt
    if cnt is not None:
        log.info("SA 预筛 selection=%.60s… → 组件 %s", selection, cnt)
    return cnt


def _poll_until_done(client, progress_url: str, budget: float, interval: float):
    """轮询单条 SA 模拟到终态。返回 (data, None) 或 (None, 错误原因)。"""
    deadline = time.time() + budget
    last_status = ""
    while time.time() < deadline:
        try:
            data = client.get_simulation_progress(progress_url) or {}
        except RateLimitError as e:
            # 平台在跑时给建议等待秒数（= 无 Retry-After 才有 body，与 project033 口径一致）。
            # 注意 0 是合法建议值：必须判 is not None，按真值判会把 0 当缺失睡满 interval；
            # 地板 0.5s 防止平台回 Retry-After: 0 时打成紧循环轰炸
            wait = float(e.retry_after) if e.retry_after is not None else interval
            time.sleep(max(0.5, min(wait, 60.0)))
            continue
        except BrainClientError:
            time.sleep(interval)
            continue
        status = str(data.get("status") or "")
        last_status = status
        if status == "COMPLETE":
            return data, None
        # WARNING = 终态（260923 实测：反转类表达式带合规提示但模拟已成功，
        # 响应体含 alpha；漏了它会死等到轮询超时 —— 与 runner 同步修）
        if status == "WARNING":
            return data, None
        if status == "ERROR":
            return None, str(data.get("message") or "平台返回 ERROR")
        if data.get("alpha"):
            return data, None
        time.sleep(interval)
    return None, f"轮询超时 {budget:.0f}s（最后状态：{last_status or '未知'}）"


def submit_super_batch(
    client,
    st,
    expressions: list,
    settings: dict,
    batch_no: str,
    *,
    max_workers: int = 3,
    poll_budget: float = 1800.0,
    poll_interval: float = 15.0,
    event_cb=None,
) -> dict:
    """SUPER 批量提交：归一化去重 → 预筛 → 线程池并发单条 POST → 轮询 → 回填入库。

    返回：{submitted, completed, failed, skipped_dup, skipped_prescreen,
           backfilled, alphas, errors}
    - submitted/completed/failed 语义与旧版 MCP 通道兼容
    - skipped_dup：同批重复 + 跨批已回测（alphas.expr_key）
    - skipped_prescreen：预筛组件数 <10 被拦下（未消耗配额）
    - errors：[{selection 摘要, reason}]，供 web/MCP 带回给用户
    """
    todo, skipped_dup = super_todo(expressions, settings, st)

    stats_lock = threading.Lock()
    stats = {
        "submitted": 0,
        "completed": 0,
        "failed": 0,
        "skipped_prescreen": 0,
        "backfilled": 0,
    }
    errors: list[dict] = []
    alphas: list[dict] = []
    prescreen_cache: dict = {}

    def _emit(event: str, payload: dict) -> None:
        if event_cb is None:
            return
        try:
            event_cb(event, {"batch_no": batch_no, **payload})
        except Exception:  # noqa: BLE001  事件回调绝不影响回测主流程
            pass

    def _fail(item: dict, reason: str) -> None:
        """统一失败出口：计数 + errors + 日志 + 事件（260923 探针教训：
        平台 ERROR 的 message 之前只进返回值，MCP 响应和日志全丢了，没法定位。）"""
        brief = item["selection"][:80]
        with stats_lock:
            stats["failed"] += 1
            errors.append({"selection": brief, "reason": reason})
        log.warning("SA 失败（%s，combo=%.40s…）：%s", batch_no, item["combo"], reason)
        _emit("sim_failed", {"error": reason})

    _emit("batch_started", {"total_batches": 1, "batch_size": len(todo)})
    log.info("SUPER 批 %s：待跑 %d，跳过 %d（去重），并发 %d",
             batch_no, len(todo), skipped_dup, max_workers)

    def _run_one(item: dict) -> None:
        sel_brief = item["selection"][:80]
        try:
            cnt = _prescreen(client, item["selection"], settings,
                             prescreen_cache, stats_lock)
            if cnt is not None and cnt < MIN_COMPONENTS:
                reason = f"预筛组件 {cnt} < {MIN_COMPONENTS}，平台建不了 SA"
                with stats_lock:
                    stats["skipped_prescreen"] += 1
                    errors.append({"selection": sel_brief, "reason": reason})
                _emit("sim_failed", {"error": reason})
                log.warning("SA 预筛拦下 %s：%s", batch_no, reason)
                return

            payload = {
                "type": "SUPER",
                "settings": settings,
                "selection": item["selection"],
                "combo": item["combo"],
            }
            progress_url = client.create_simulation(payload)
            with stats_lock:
                stats["submitted"] += 1

            data, err = _poll_until_done(client, progress_url, poll_budget, poll_interval)
            if err:
                _fail(item, err)
                return
            alpha_id = (data or {}).get("alpha")
            if not alpha_id:
                _fail(item, "模拟完成但未返回 alpha id")
                return
            with stats_lock:
                stats["completed"] += 1

            # 回填（重试一次：SA 刚完成时详情接口偶发空）；upsert 后补写
            # type/selection/combo/expr_key —— expr_key 是后续去重的依据
            row = None
            for attempt in range(2):
                try:
                    detail = client.get_alpha_details(alpha_id)
                    row = client.extract_alpha_metrics(detail)
                    if row.get("alpha_id"):
                        break
                except Exception as e:  # noqa: BLE001
                    log.warning("SA 详情回填异常（alpha=%s，第 %s 次）：%s",
                                alpha_id, attempt + 1, e)
                row = None
                time.sleep(3)
            if row and row.get("alpha_id"):
                try:
                    st.upsert_alpha(row, batch_no=batch_no)
                    st.mark_super_alpha(
                        row["alpha_id"],
                        selection=item["selection"],
                        combo=item["combo"],
                        expr_key=item["expr_key"],
                    )
                    with stats_lock:
                        stats["backfilled"] += 1
                        alphas.append(row)
                except Exception as e:  # noqa: BLE001
                    log.error("SA 入库失败（alpha=%s）：%s → 该 selection 无去重指纹，"
                              "重跑会重复提交", alpha_id, e)
            else:
                log.error("SA 详情两次为空（alpha=%s）→ 未入库、无去重指纹", alpha_id)
            _emit("sim_completed", {"alpha_id": alpha_id})
        except Exception as e:  # noqa: BLE001  线程内绝不裸抛
            _fail(item, f"{type(e).__name__}: {e}")
            log.exception("SA 单条执行异常：%s", sel_brief)

    if todo:
        workers = max(1, min(int(max_workers), len(todo), 3))
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sa") as pool:
            futures = [pool.submit(_run_one, item) for item in todo]
            for f in as_completed(futures):
                _ = f.result()  # 异常已在 _run_one 内兜住；此处仅为传播兜底

    return {
        "submitted": stats["submitted"],
        "completed": stats["completed"],
        "failed": stats["failed"],
        "skipped_dup": skipped_dup,
        "skipped_prescreen": stats["skipped_prescreen"],
        "backfilled": stats["backfilled"],
        "alphas": alphas,
        "errors": errors,
    }
