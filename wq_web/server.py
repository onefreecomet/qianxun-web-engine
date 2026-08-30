"""千寻 web 版（v81）——FastAPI 后端。

定位：v66 GUI 后端 + MCP Server 的 web 形态。只暴露两件事
  1. AI 批次（list / create / detail / live progress）
  2. Alpha 列表（按 batch_no 或全库查，按 |sharpe| 排序）

v81+ 扩展：
  3. PnL 同步 + 本地 Self/PPA Corr 计算（参考 ProdMemo 移植）

复用原则：Storage / BatchScheduler / _normalize_settings / expression_key 全部直接
import 现有实现，绝不复制粘贴改一份。
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# 让 web server 既能从项目根（python wq_web/server.py）也能从 -m（python -m wq_web.server）跑
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from fastapi.templating import Jinja2Templates  # noqa: E402

from wq_engine.mcp_server import _client, _latest_db, _normalize_settings  # noqa: E402
from wq_engine.scheduler.runner import BatchScheduler  # noqa: E402
from wq_engine.storage.database import Storage, expression_key  # noqa: E402


# ---------------- 应用初始化 ----------------

WEB_DIR = Path(__file__).resolve().parent
app = FastAPI(title="千寻 web v81")
app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))


def _storage() -> Storage:
    """统一入口：web 后端始终连 _latest_db() 自动选最新 dist_vNN。"""
    p = _latest_db()
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
    return Storage(p)


def _serialize(row: dict | None) -> dict | None:
    """SQLite row → JSON-friendly dict：datetime 转 ISO、bytes 转 str。"""
    if row is None:
        return None
    out = {}
    for k, v in row.items():
        if isinstance(v, datetime):
            out[k] = v.isoformat()
        elif isinstance(v, bytes):
            out[k] = v.decode("utf-8", errors="replace")
        else:
            out[k] = v
    return out


# ---------------- PnL 同步状态机 ----------------


class PnlSyncState:
    """跟踪当前 PnL 同步任务的状态，单实例。"""

    def __init__(self) -> None:
        self.running: bool = False
        self.thread: threading.Thread | None = None
        self.stop_event: threading.Event = threading.Event()
        self.last_result: dict | None = None
        self.last_progress: dict | None = None

    def start(self, target: Callable[[Callable], dict]) -> dict:
        if self.running:
            return {"ok": False, "error": "PnL 同步已在运行中"}
        self.running = True
        self.stop_event = threading.Event()
        self.last_progress = {"phase": "starting", "message": "准备开始…"}

        def _on_progress(payload: dict) -> None:
            self.last_progress = payload
            bus.push("pnl_sync_progress", payload)

        def _run() -> None:
            try:
                self.last_result = target(_on_progress)
                _on_progress({
                    "phase": "completed",
                    "message": f"完成：{self.last_result.get('success', 0)} 个 PnL，"
                               f"{self.last_result.get('corr_computed', 0)} 个 corr",
                })
            except Exception as e:
                self.last_result = {"ok": False, "error": str(e)}
                _on_progress({"phase": "error", "message": str(e)})
            finally:
                self.running = False

        self.thread = threading.Thread(target=_run, daemon=True)
        self.thread.start()
        return {"ok": True, "message": "已启动 PnL 同步"}

    def stop(self) -> dict:
        if not self.running:
            return {"ok": False, "error": "PnL 同步未在运行"}
        self.stop_event.set()
        return {"ok": True, "message": "已发送停止信号"}


pnl_sync_state = PnlSyncState()


# ---------------- 并发设置（全局，提交批次时使用） ----------------

_concurrency_lock = threading.Lock()
_concurrency = {"concurrent": 8, "sim_slots": 6}  # 默认 8 并发批、6 并发槽

# 本进程还活着的 BatchScheduler 引用（弱持有，跑完即清），用于 /api/quota 读真实配额
_live_schedulers: list[BatchScheduler] = []


def _get_concurrency() -> dict:
    with _concurrency_lock:
        return dict(_concurrency)


def _set_concurrency(concurrent: int, sim_slots: int) -> dict:
    concurrent = max(1, min(16, int(concurrent)))
    sim_slots = max(1, min(10, int(sim_slots)))
    with _concurrency_lock:
        _concurrency["concurrent"] = concurrent
        _concurrency["sim_slots"] = sim_slots
        return dict(_concurrency)


# ---------------- 实时进度总线（WebSocket） ----------------


class ProgressBus:
    """全应用单例的进度推送总线。

    BatchScheduler 在另一线程跑（callback 同步），WebSocket handler 在 asyncio loop。
    用 asyncio.run_coroutine_threadsafe 把同步回调桥到 loop，连接集合用 set。
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._clients: set[WebSocket] = set()

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.add(ws)
        # 连接时立刻推一帧最近状态（让刚打开页面的用户看到已有进度）
        await self._send_snapshot(ws)

    async def disconnect(self, ws: WebSocket) -> None:
        self._clients.discard(ws)

    def push(self, event: str, payload: dict) -> None:
        """同步线程调用：把事件转发到所有 ws 客户端。"""
        if not self._loop or not self._clients:
            return
        msg = json.dumps({"event": event, **payload}, ensure_ascii=False, default=str)
        for ws in list(self._clients):
            try:
                asyncio.run_coroutine_threadsafe(ws.send_text(msg), self._loop)
            except Exception:
                self._clients.discard(ws)

    async def _send_snapshot(self, ws: WebSocket) -> None:
        """连接快照：最近 5 个批次 + 当前 running 任务的 success/failed。"""
        try:
            st = _storage()
            batches = st.list_ai_batches(limit=5)
            snap = []
            for b in batches:
                bid = b["batch_no"]
                tid = st.find_resumable_task_run.__self__ if False else None  # 占位
                # 找 task_run：直接按 batch_no 查
                snap.append({
                    "batch_no": bid,
                    "status": b["status"],
                    "producer": b.get("producer", ""),
                    "region": b.get("region", ""),
                    "dataset_id": b.get("dataset_id", ""),
                    "expression_count": b.get("expression_count", 0),
                    "note": b.get("note", ""),
                    "created_at": b.get("created_at", ""),
                })
            await ws.send_text(json.dumps({"event": "snapshot", "batches": snap}, ensure_ascii=False))
        except Exception as e:
            await ws.send_text(json.dumps({"event": "error", "message": str(e)}))


bus = ProgressBus()


# ---------------- 页面路由 ----------------


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """单页应用入口，server-rendered shell + 客户端 JS 拉数据。"""
    db_path = str(_latest_db())
    return templates.TemplateResponse(
        request,
        "index.html",
        context={
            "db_path": db_path,
            "version": "v81 web alpha",
        },
    )


# ---------------- REST: 批次 ----------------


@app.get("/api/batches")
async def list_batches(limit: int = 50) -> dict:
    st = _storage()
    rows = st.list_ai_batches(limit=limit)
    # 补 task_run 进度（ai_batches 不存 success/failed）
    for r in rows:
        tr = st.find_resumable_task_run.__self__ if False else None
        # 直接按 batch_no 找对应 task_run（最近一条）
        try:
            with st._lock:
                trow = st._conn.execute(
                    "SELECT * FROM task_runs WHERE batch_no=? ORDER BY id DESC LIMIT 1",
                    (r["batch_no"],),
                ).fetchone()
            if trow:
                t = dict(trow)
                r["success"] = t.get("success") or 0
                r["failed"] = t.get("failed") or 0
                r["total"] = t.get("total") or 0
                r["task_status"] = t.get("status", "")
                r["task_run_id"] = t.get("id")
            else:
                r["success"] = 0
                r["failed"] = 0
                r["total"] = 0
                r["task_status"] = ""
                r["task_run_id"] = None
        except Exception:
            r["success"] = r["failed"] = r["total"] = 0
            r["task_run_id"] = r.get("task_run_id")
    # 实时进度：按 simulations 表逐条状态统计（task_runs.success/failed 仅批次结束才更新）
    ids = [r.get("task_run_id") for r in rows if r.get("task_run_id")]
    if ids:
        qmarks = ",".join("?" * len(ids))
        try:
            with st._lock:
                sim_rows = st._conn.execute(
                    f"SELECT task_run_id, status, COUNT(*) c FROM simulations "
                    f"WHERE task_run_id IN ({qmarks}) GROUP BY task_run_id, status",
                    ids,
                ).fetchall()
            sim_counts = {}
            for srow in sim_rows:
                sim_counts.setdefault(srow["task_run_id"], {})[srow["status"]] = srow["c"]
            for r in rows:
                sc = sim_counts.get(r.get("task_run_id")) or {}
                stotal = sum(sc.values())
                scompleted = sc.get("completed", 0)
                sfailed = sc.get("failed", 0) + sc.get("cancelled", 0) + sc.get("error", 0)
                r["sim_total"] = stotal
                r["sim_completed"] = scompleted
                r["sim_failed"] = sfailed
                r["sim_done"] = scompleted + sfailed
        except Exception:
            pass
    return {"ok": True, "batches": [_serialize(r) for r in rows]}


@app.get("/api/batches/{batch_no}")
async def batch_detail(batch_no: str) -> dict:
    st = _storage()
    b = st.get_ai_batch(batch_no)
    if not b:
        raise HTTPException(404, f"批次不存在：{batch_no}")
    # 关联 task_run
    with st._lock:
        tr = st._conn.execute(
            "SELECT * FROM task_runs WHERE batch_no=? ORDER BY id DESC LIMIT 1",
            (batch_no,),
        ).fetchone()
    task_run = dict(tr) if tr else None
    # 关联 alphas（已回填的）
    alphas = st.list_alphas_by_batch(batch_no, limit=5000)
    alphas.sort(key=lambda a: abs(a.get("sharpe") or 0), reverse=True)
    # 关联 simulations（原始 sim 记录，便于看每条表达式状态）
    if task_run:
        with st._lock:
            sims = st._conn.execute(
                "SELECT id, expression, decay, status, progress, alpha_id, retry_count, last_error "
                "FROM simulations WHERE task_run_id=? ORDER BY id",
                (task_run["id"],),
            ).fetchall()
        sim_rows = [dict(s) for s in sims]
    else:
        sim_rows = []
    return {
        "ok": True,
        "batch": _serialize(b),
        "task_run": _serialize(task_run),
        "alphas": [_serialize(a) for a in alphas],
        "simulations": [_serialize(s) for s in sim_rows],
    }


@app.get("/api/batches/{batch_no}/progress")
async def batch_progress(batch_no: str) -> dict:
    """实时进度：按 simulations 表逐条状态统计，跨进程（MCP/web）通用。
    task_runs.success/failed 仅在批次结束才写库，故进度以 simulations 为准。"""
    st = _storage()
    with st._lock:
        tr = st._conn.execute(
            "SELECT * FROM task_runs WHERE batch_no=? ORDER BY id DESC LIMIT 1",
            (batch_no,),
        ).fetchone()
    if not tr:
        return {"ok": False, "error": "no task_run", "batch_no": batch_no}
    t = dict(tr)
    tid = t["id"]
    with st._lock:
        rows = st._conn.execute(
            "SELECT status, COUNT(*) c FROM simulations WHERE task_run_id=? GROUP BY status",
            (tid,),
        ).fetchall()
    counts = {r["status"]: r["c"] for r in rows}
    total = sum(counts.values())
    completed = counts.get("completed", 0)
    failed = counts.get("failed", 0) + counts.get("cancelled", 0) + counts.get("error", 0)
    running = counts.get("running", 0)
    pending = counts.get("pending", 0) + counts.get("submitted", 0)
    done = completed + failed
    pct = round(min(100, done / total * 100), 1) if total > 0 else 0
    return {
        "ok": True,
        "batch_no": batch_no,
        "task_run_id": tid,
        "status": t.get("status"),
        "total": total,
        "completed": completed,
        "failed": failed,
        "running": running,
        "pending": pending,
        "done": done,
        "pct": pct,
    }


@app.post("/api/batches")
async def create_batch(req: Request) -> dict:
    """提交 AI 批次。

    Body: {
      "expressions": [{"expression": "...", "decay": 4}, ...],
      "settings": {"region": "USA", "decay": 4, ...},
      "producer": "阿法",
      "batch_size": 8,
      "no_backfill": false
    }
    并发批数 / 并发槽用全局设置（/api/concurrency），不在提交里传。
    """
    body = await req.json()
    expressions_raw = body.get("expressions", [])
    if not expressions_raw:
        raise HTTPException(400, "expressions 不能为空")
    settings = _normalize_settings(body.get("settings") or {})
    producer = body.get("producer", "阿法")
    glob = _get_concurrency()
    concurrent = glob["concurrent"]
    sim_slots = glob["sim_slots"]
    batch_size = int(body.get("batch_size", 8))
    no_backfill = bool(body.get("no_backfill", False))

    st = _storage()
    # 去重
    done = st.completed_expression_keys()
    todo = [
        (e["expression"], e.get("decay", settings["decay"]), settings)
        for e in expressions_raw
        if expression_key(e["expression"], settings) not in done
    ]
    skipped = len(expressions_raw) - len(todo)
    if not todo:
        return {"ok": True, "skipped": skipped, "message": "全部表达式已回测过"}

    batch_no = st.next_batch_no()
    tid = st.create_task_run(
        name=f"web batch {batch_no}",
        kind="ai_batch",
        config={"producer": producer, **settings},
        total=len(todo),
        batch_no=batch_no,
    )
    st.create_ai_batch(
        batch_no=batch_no,
        producer=producer,
        dataset_id=str(settings.get("dataset_id", "")),
        region=str(settings["region"]),
        expression_count=len(todo),
        note=f"web v81 提交（跳过 {skipped}）",
    )

    # 后台跑 BatchScheduler（同步线程），进度通过 bus.push 推到 WebSocket
    def _cb(event: str, payload: dict) -> None:
        # 把进度事件广播出去
        bus.push(event, {"batch_no": batch_no, "task_run_id": tid, **payload})

    client = _client()
    sched = BatchScheduler(
        client, st,
        progress_cb=_cb,
        max_concurrent_batches=concurrent,
        batch_size=batch_size,
        max_concurrent_simulations=sim_slots,
    )
    _live_schedulers.append(sched)
    st.update_ai_batch_status(batch_no, "running")

    def _run_in_thread() -> dict:
        """在线程里跑 run + 回填，结束推 final 事件。"""
        try:
            sched.run(tid, todo)
            sched.join()
            completed = sum(1 for e in st.list_completed_simulations(tid))
            backfilled = 0
            if not no_backfill:
                sims = st.list_completed_simulations(tid)
                for s in sims:
                    try:
                        detail = client.get_alpha_details(s["alpha_id"])
                        alpha = client.extract_alpha_metrics(detail)
                        if alpha.get("alpha_id"):
                            st.upsert_alpha(alpha, batch_no=batch_no)
                            backfilled += 1
                    except Exception:
                        pass
            st.update_ai_batch_status(batch_no, "completed")
            # v69 同款：把平台真实配额写入 platform_meta，跨进程/跨批次共享
            try:
                rl = getattr(client, "last_ratelimit", None)
                if rl and rl.get("limit", 0) > 0:
                    st.set_meta("sim_quota", json.dumps(rl))
            except Exception:
                pass
            try:
                _live_schedulers.remove(sched)
            except ValueError:
                pass
            bus.push("batch_done", {
                "batch_no": batch_no,
                "task_run_id": tid,
                "completed": completed,
                "backfilled": backfilled,
                "skipped": skipped,
            })
            return {"ok": True, "completed": completed, "backfilled": backfilled}
        except Exception as e:
            st.update_ai_batch_status(batch_no, "failed")
            try:
                _live_schedulers.remove(sched)
            except ValueError:
                pass
            bus.push("batch_error", {"batch_no": batch_no, "error": str(e)})
            return {"ok": False, "error": str(e)}

    import threading
    th = threading.Thread(target=_run_in_thread, daemon=True)
    th.start()

    return {
        "ok": True,
        "batch_no": batch_no,
        "task_run_id": tid,
        "submitted": len(todo),
        "skipped": skipped,
    }


# ---------------- REST: Alpha 列表 ----------------


@app.get("/api/alphas")
async def list_alphas(
    batch_no: str | None = None,
    limit: int = 200,
    sort: str = "abs_sharpe",
) -> dict:
    """Alpha 列表。支持按 batch_no 过滤；排序支持 abs_sharpe / sharpe / margin / recent。"""
    st = _storage()
    if batch_no:
        rows = st.list_alphas_by_batch(batch_no, limit=limit)
    else:
        rows = st.list_alphas(limit=limit)
    if sort == "sharpe":
        rows.sort(key=lambda a: a.get("sharpe") or 0, reverse=True)
    elif sort == "margin":
        rows.sort(key=lambda a: a.get("margin") or 0, reverse=True)
    elif sort == "recent":
        rows.sort(key=lambda a: a.get("date_created") or "", reverse=True)
    elif sort == "max_corr":
        # max_corr 升序（最干净的在最上面，投稿自检排序友好）
        rows.sort(key=lambda a: a.get("max_corr") if a.get("max_corr") is not None else -1)
    else:  # abs_sharpe
        rows.sort(key=lambda a: abs(a.get("sharpe") or 0), reverse=True)
    out = []
    for r in rows:
        d = _serialize(r)
        # 把 corr_top5 JSON 字符串解开成对象，方便前端直接展示
        for k in ("self_corr_top5", "ppa_corr_top5"):
            if d.get(k):
                try:
                    d[k] = json.loads(d[k])
                except Exception:
                    pass
        out.append(d)
    return {"ok": True, "alphas": out}


@app.get("/api/alphas/{alpha_id}")
async def alpha_detail(alpha_id: str) -> dict:
    st = _storage()
    a = st.get_alpha(alpha_id)
    if not a:
        raise HTTPException(404, f"alpha 不存在：{alpha_id}")
    pnl = st.get_alpha_pnl(alpha_id)
    a_out = _serialize(a)
    # 解开 corr top5
    for k in ("self_corr_top5", "ppa_corr_top5"):
        if a_out.get(k):
            try:
                a_out[k] = json.loads(a_out[k])
            except Exception:
                pass
    return {"ok": True, "alpha": a_out, "pnl": pnl}


# ---------------- 并发设置接口 ----------------


@app.get("/api/concurrency")
async def get_concurrency() -> dict:
    return {"ok": True, **_get_concurrency()}


@app.post("/api/concurrency")
async def set_concurrency(req: Request) -> dict:
    """手动改并发设置。Body: {"concurrent": 8, "sim_slots": 6}"""
    body = await req.json()
    cur = _get_concurrency()
    concurrent = body.get("concurrent", cur["concurrent"])
    sim_slots = body.get("sim_slots", cur["sim_slots"])
    try:
        out = _set_concurrency(concurrent, sim_slots)
    except (TypeError, ValueError):
        raise HTTPException(400, "concurrent / sim_slots 必须是整数")
    bus.push("concurrency_changed", out)
    return {"ok": True, **out}


@app.get("/api/scheduler/state")
async def get_scheduler_state() -> dict:
    """实时回测并发状态（web 并发卡实时显示用）。

    在飞数据从数据库取——web 与 MCP 两个进程都写同一份 alpha_machine.db，
    所以无论批次从哪条通道提交（web 提交面板 / MCP qianxun_submit），
    在飞批次数、在飞模拟数都准确。
    （本进程 _live_schedulers 只反映 web 自己提交的批次，MCP 提交的看不到，
     故不作为主计数来源，仅用于聚合 web 进程的限流倒计时。）
    """
    import time as _t
    st = _storage()
    # 数据库唯一真相源：跨进程（Storage 的 execute 在内部 _ThreadSafeConn 上，经 st._conn 调用）
    running_batches = st._conn.execute(
        "SELECT COUNT(*) FROM task_runs WHERE status = 'running'"
    ).fetchone()[0]
    inflight_sims = st._conn.execute(
        "SELECT COUNT(*) FROM simulations WHERE status IN ('pending','submitted','running')"
    ).fetchone()[0]
    # 本进程 web 批次的限流/配额（MCP 进程的限流 web 看不到，仅作补充）
    rate_limit_reset_at = None
    sim_quota = None
    for sch in list(_live_schedulers):
        rl = getattr(sch, "rate_limit_reset_at", None)
        if rl and (rate_limit_reset_at is None or rl > rate_limit_reset_at):
            rate_limit_reset_at = rl
        qq = getattr(sch, "sim_quota", None)
        if qq and qq.get("limit", 0) > 0:
            sim_quota = qq
    cfg = _get_concurrency()
    now = _t.time()
    is_rate_limited = bool(rate_limit_reset_at and rate_limit_reset_at > now)
    reset_sec = max(0, int(rate_limit_reset_at - now)) if rate_limit_reset_at else 0
    # 在飞批次：以数据库为准（含 MCP），再与 web 进程内存调度器取大值兜底
    active = max(running_batches, len(_live_schedulers))
    return {
        "ok": True,
        "running": active > 0,
        "concurrent": cfg["concurrent"],
        "sim_slots": cfg["sim_slots"],
        "active_batches": active,
        "slots_used": inflight_sims,
        "slots_free": max(0, cfg["sim_slots"] - inflight_sims),
        "rate_limited": is_rate_limited,
        "rate_limit_reset_sec": reset_sec,
        "sim_quota": sim_quota,
    }


# ---------------- 每日配额接口 ----------------


@app.get("/api/quota")
async def get_quota() -> dict:
    """每日回测配额。

    优先级（复用 v28/v69 现成链路，不重复造轮子）：
    1. 本进程 web 批次跑过 → BatchScheduler.sim_quota（响应头真实值）
    2. platform_meta 里的共享配额（MCP submit 写入，6h 内有效）
    3. 本地兜底：只报今日已提交 sim 数（诚实提示无真实剩余）
    """
    st = _storage()

    # 收集本进程所有还活着的 scheduler 的配额
    q: dict | None = None
    for sch in list(_live_schedulers):
        qq = getattr(sch, "sim_quota", None)
        if qq and qq.get("limit", 0) > 0:
            q = qq
            break

    source = "scheduler"
    if not q:
        try:
            meta = st.get_meta("sim_quota")
            if meta:
                mq = json.loads(meta)
                import time as _t
                if mq.get("updated_at", 0) and _t.time() - mq["updated_at"] < 6 * 3600:
                    q = mq
                    source = "platform_meta"
        except Exception:
            pass

    out: dict = {"ok": True, "source": source}
    if q and q.get("limit", 0) > 0:
        import time as _t
        limit = q["limit"]
        remaining = q["remaining"]
        left = q.get("reset_sec", 0) - (_t.time() - q.get("updated_at", 0))
        out.update({
            "limit": limit,
            "remaining": remaining,
            "used": max(limit - remaining, 0),
            "reset_sec_left": max(int(left), 0),
            "updated_at": q.get("updated_at"),
        })
    else:
        # 本地兜底：今日已用（UTC 日界，与 count_simulations_since 统计口径一致）
        from datetime import datetime, timezone
        today_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0,
        ).isoformat()
        out.update({
            "limit": None,
            "remaining": None,
            "used_local": st.count_simulations_since(today_start),
        })
    return out


# ---------------- PnL 同步接口 ----------------


@app.post("/api/sync/pnl")
async def start_pnl_sync(req: Request) -> dict:
    """启动 PnL 同步 + 本地 corr 计算。

    Body: {"compute_corr": true}（默认 true），{"alpha_ids": ["...", "..."]}（可选，只同步这些），
          {"force_full": true}（可选，强制全量重拉，跳过增量）
    """
    body = {}
    try:
        body = await req.json()
    except Exception:
        pass
    compute_corr = bool(body.get("compute_corr", False))
    force_full = bool(body.get("force_full", False))
    alpha_id_filter = set(body.get("alpha_ids") or []) if body.get("alpha_ids") else None

    if pnl_sync_state.running:
        raise HTTPException(409, "PnL 同步已在运行中，先停掉再启")

    from wq_engine.api.client import APIClient
    from wq_engine.sync.pnl_sync import sync_pnls

    st = _storage()

    def _target(progress_cb: Callable) -> dict:
        client = _client()
        return sync_pnls(
            client, st,
            progress_cb=progress_cb,
            stop_event=pnl_sync_state.stop_event,
            compute_corr=compute_corr,
            alpha_id_filter=alpha_id_filter,
            force_full=force_full,
        )

    return pnl_sync_state.start(_target)


@app.get("/api/sync/pnl/status")
async def pnl_sync_status() -> dict:
    return {
        "ok": True,
        "running": pnl_sync_state.running,
        "progress": pnl_sync_state.last_progress,
        "last_result": pnl_sync_state.last_result,
    }


@app.post("/api/sync/pnl/stop")
async def pnl_sync_stop() -> dict:
    return pnl_sync_state.stop()


# ---------------- 手动算 corr（单 alpha，按需触发，节省资源） ----------------


@app.post("/api/corr/compute")
async def compute_corr_one(req: Request) -> dict:
    """手动对单个已同步 PnL 的 alpha 计算 Self + PPA corr，写回 alphas 表。

    Body: {"alpha_id": "Xg7NRLgm"}
    """
    body = await req.json() if req.headers.get("content-type", "").startswith("application/json") else {}
    alpha_id = body.get("alpha_id", "").strip()
    if not alpha_id:
        raise HTTPException(400, "alpha_id 必填")

    from wq_engine.api.client import APIClient
    from wq_engine.api.local_corr import calculate_correlation
    from wq_engine.api.config import BrainConfig

    st = _storage()
    alpha = st.get_alpha(alpha_id)
    if not alpha:
        raise HTTPException(404, f"alpha 不存在：{alpha_id}")

    pnl_json = alpha.get("pnl_json")
    if not pnl_json:
        raise HTTPException(400, f"{alpha_id} 缺 PnL，先做 PnL 同步")

    # 从 pnl_json 还原 records + region
    if isinstance(pnl_json, str):
        import json as _json
        pnl_json = _json.loads(pnl_json)
    records = (pnl_json or {}).get("records") if isinstance(pnl_json, dict) else None
    if not records:
        raise HTTPException(400, f"{alpha_id} 的 PnL 数据为空")

    settings_raw = alpha.get("settings")  # alpha 表里有独立的 region 列，没有 settings JSON
    if isinstance(settings_raw, str):
        import json as _json
        try:
            settings_raw = _json.loads(settings_raw)
        except Exception:
            settings_raw = {}
    settings = settings_raw if isinstance(settings_raw, dict) else {}
    region = settings.get("region") or alpha.get("region")
    if not region:
        raise HTTPException(400, f"{alpha_id} 缺 region，无法算 corr")

    pnls_by_id = {alpha_id: records}

    # 拉同 region 已同步 PnL 的其他 alpha 做池子（最多 2000 个够用了）
    with st._lock:
        rows = st._conn.execute(
            "SELECT alpha_id, pnl_json FROM alphas WHERE region=? AND pnl_json IS NOT NULL AND alpha_id != ? LIMIT 2000",
            (region, alpha_id),
        ).fetchall()
    import json as _json
    pool_alphas = []
    for r in rows:
        aid = r["alpha_id"]
        try:
            data = _json.loads(r["pnl_json"]) if isinstance(r["pnl_json"], str) else r["pnl_json"]
            recs = data.get("records") if isinstance(data, dict) else None
            if recs:
                pnls_by_id[aid] = recs
                pool_alphas.append({"id": aid, "settings": {"region": region}})
        except Exception:
            pass
    pool_alphas.insert(0, {"id": alpha_id, "settings": {"region": region}})

    # 算
    try:
        self_r = calculate_correlation(alpha_id, "SELF", pool_alphas, pnls_by_id)
    except Exception as e:
        self_r = None
        self_err = str(e)
    else:
        self_err = None
    try:
        ppa_r = calculate_correlation(alpha_id, "PPA", pool_alphas, pnls_by_id)
    except Exception as e:
        ppa_r = None
        ppa_err = str(e)
    else:
        ppa_err = None

    if not self_r and not ppa_r:
        raise HTTPException(422, f"corr 算失败：self={self_err} ppa={ppa_err}")

    # 写回
    def _pick_max(s, p):
        candidates = []
        if s: candidates.append(("self", abs(s["max"])))
        if p: candidates.append(("ppa", abs(p["max"])))
        if candidates:
            return max(candidates, key=lambda x: x[1])
        return None

    from datetime import datetime, timezone
    mc_src = _pick_max(self_r, ppa_r)
    with st._lock:
        st._conn.execute(
            """UPDATE alphas SET
                self_corr_min=?, self_corr_max=?, self_corr_top5=?,
                ppa_corr_min=?, ppa_corr_max=?, ppa_corr_top5=?,
                max_corr=?, max_corr_source=?, corr_computed_at=?
               WHERE alpha_id=?""",
            (
                self_r["min"] if self_r else None,
                self_r["max"] if self_r else None,
                _json.dumps(self_r["top5"]) if self_r else None,
                ppa_r["min"] if ppa_r else None,
                ppa_r["max"] if ppa_r else None,
                _json.dumps(ppa_r["top5"]) if ppa_r else None,
                mc_src[1] if mc_src else None,
                mc_src[0] if mc_src else None,
                datetime.now(timezone.utc).isoformat(),
                alpha_id,
            ),
        )
        st._conn.commit()

    return {
        "ok": True,
        "alpha_id": alpha_id,
        "self": self_r,
        "ppa": ppa_r,
        "max_corr": mc_src[1] if mc_src else None,
        "max_corr_source": mc_src[0] if mc_src else None,
    }


# ---------------- Alpha 备忘录（watchlist + 提交 + 备注） ----------------


def _ensure_memo_table(st) -> None:
    """幂等建备忘录表（web 层自己的表，不动核心 Storage schema）。"""
    with st._lock:
        st._conn.execute("""
            CREATE TABLE IF NOT EXISTS alpha_memo (
                alpha_id TEXT PRIMARY KEY,
                region TEXT,
                status TEXT,
                sharpe REAL, fitness REAL, turnover REAL, margin REAL,
                themes TEXT,
                note TEXT DEFAULT '',
                added_at TEXT,
                synced_at TEXT
            )
        """)
        st._conn.commit()


def _memo_upsert_from_detail(st, detail: dict) -> dict:
    """从平台 alpha 详情 upsert 备忘录行，返回行数据。

    注意：平台 themes 字段经常为空；手动维护的主题不能被 sync 冲掉，
    所以 detail.themes 为空时保留库里的旧值。
    """
    from wq_engine.api.client import APIClient
    m = APIClient.extract_alpha_metrics(detail)
    themes_names = [
        t.get("name", "") for t in (detail.get("themes") or []) if isinstance(t, dict)
    ]
    now = datetime.now(timezone.utc).isoformat()
    region = m.get("region") or (detail.get("settings") or {}).get("region") or ""
    status = detail.get("status") or ""

    # 平台 themes 为空时保留手动维护的旧值
    if not themes_names:
        with st._lock:
            old = st._conn.execute(
                "SELECT themes FROM alpha_memo WHERE alpha_id=?", (m["alpha_id"],)
            ).fetchone()
        if old and old["themes"]:
            try:
                themes_names = json.loads(old["themes"])
            except Exception:
                pass

    with st._lock:
        st._conn.execute(
            """INSERT INTO alpha_memo
                 (alpha_id, region, status, sharpe, fitness, turnover, margin,
                  themes, note, added_at, synced_at)
               VALUES (?,?,?,?,?,?,?,?,COALESCE((SELECT note FROM alpha_memo WHERE alpha_id=?),''),?,?)
               ON CONFLICT(alpha_id) DO UPDATE SET
                 region=excluded.region, status=excluded.status,
                 sharpe=excluded.sharpe, fitness=excluded.fitness,
                 turnover=excluded.turnover, margin=excluded.margin,
                 themes=excluded.themes, synced_at=excluded.synced_at""",
            (
                m["alpha_id"], region, status,
                m.get("sharpe"), m.get("fitness"), m.get("turnover"), m.get("margin"),
                json.dumps(themes_names, ensure_ascii=False),
                m["alpha_id"], now, now,
            ),
        )
        st._conn.commit()
    return {"alpha_id": m["alpha_id"], "region": region, "status": status, "themes": themes_names}


def _memo_rows(st) -> list[dict]:
    with st._lock:
        rows = st._conn.execute("SELECT * FROM alpha_memo ORDER BY added_at DESC").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["themes_list"] = json.loads(d["themes"]) if d["themes"] else []
        except Exception:
            d["themes_list"] = []
        out.append(d)
    return out


@app.get("/api/memo")
async def memo_list() -> dict:
    st = _storage()
    _ensure_memo_table(st)
    return {"ok": True, "memos": _memo_rows(st)}


@app.post("/api/memo")
async def memo_add(req: Request) -> dict:
    """输入 alpha id 拉详情入列。Body: {"alpha_id": "..."}"""
    body = await req.json()
    alpha_id = (body.get("alpha_id") or "").strip()
    if not alpha_id:
        raise HTTPException(400, "alpha_id 必填")
    st = _storage()
    _ensure_memo_table(st)
    try:
        detail = _client().get_alpha_details(alpha_id)
    except Exception as e:
        raise HTTPException(404, f"拉取 {alpha_id} 失败：{e}")
    row = _memo_upsert_from_detail(st, detail)
    return {"ok": True, "row": row}


@app.post("/api/memo/sync")
async def memo_sync() -> dict:
    """同步备忘录全部 alpha 的提交状态与指标。"""
    st = _storage()
    _ensure_memo_table(st)
    rows = _memo_rows(st)
    client = _client()
    ok, fail = 0, 0
    for r in rows:
        try:
            detail = client.get_alpha_details(r["alpha_id"])
            _memo_upsert_from_detail(st, detail)
            ok += 1
        except Exception:
            fail += 1
    return {"ok": True, "synced": ok, "failed": fail, "memos": _memo_rows(st)}


@app.post("/api/memo/submit")
async def memo_submit(req: Request) -> dict:
    """通过 API 真实提交 alpha。Body: {"alpha_id": "..."}"""
    body = await req.json()
    alpha_id = (body.get("alpha_id") or "").strip()
    if not alpha_id:
        raise HTTPException(400, "alpha_id 必填")
    st = _storage()
    _ensure_memo_table(st)
    try:
        resp = _client().submit_alpha(alpha_id)
    except Exception as e:
        raise HTTPException(502, f"提交请求失败：{e}")
    # 提交后立刻拉详情刷新状态
    try:
        detail = _client().get_alpha_details(alpha_id)
        _memo_upsert_from_detail(st, detail)
    except Exception:
        pass
    code = resp.get("status_code")
    return {
        "ok": code in (200, 201),
        "status_code": code,
        "message": resp.get("message", ""),
        "alpha_id": alpha_id,
    }


@app.post("/api/memo/note")
async def memo_note(req: Request) -> dict:
    """保存备注。Body: {"alpha_id": "...", "note": "..."}"""
    body = await req.json()
    alpha_id = (body.get("alpha_id") or "").strip()
    note = body.get("note", "")
    if not alpha_id:
        raise HTTPException(400, "alpha_id 必填")
    st = _storage()
    _ensure_memo_table(st)
    with st._lock:
        st._conn.execute(
            "UPDATE alpha_memo SET note=? WHERE alpha_id=?", (note, alpha_id)
        )
        st._conn.commit()
    return {"ok": True}


@app.post("/api/memo/themes")
async def memo_themes(req: Request) -> dict:
    """手动维护金字塔主题。Body: {"alpha_id": "...", "themes": ["ASI/D1/MODEL", ...]}"""
    body = await req.json()
    alpha_id = (body.get("alpha_id") or "").strip()
    themes = body.get("themes") or []
    if not alpha_id:
        raise HTTPException(400, "alpha_id 必填")
    if not isinstance(themes, list):
        raise HTTPException(400, "themes 必须是数组")
    themes = [str(t).strip() for t in themes if str(t).strip()]
    st = _storage()
    _ensure_memo_table(st)
    with st._lock:
        cur = st._conn.execute(
            "SELECT 1 FROM alpha_memo WHERE alpha_id=?", (alpha_id,)
        ).fetchone()
        if not cur:
            raise HTTPException(404, f"备忘录里没有 {alpha_id}，先添加")
        st._conn.execute(
            "UPDATE alpha_memo SET themes=? WHERE alpha_id=?",
            (json.dumps(themes, ensure_ascii=False), alpha_id),
        )
        st._conn.commit()
    return {"ok": True, "themes": themes}


@app.delete("/api/memo/{alpha_id}")
async def memo_delete(alpha_id: str) -> dict:
    st = _storage()
    _ensure_memo_table(st)
    with st._lock:
        st._conn.execute("DELETE FROM alpha_memo WHERE alpha_id=?", (alpha_id,))
        st._conn.commit()
    return {"ok": True}


# ---------------- 提示词库 ----------------


def _prompts_get(st) -> list[dict]:
    raw = st.get_meta("prompt_library")
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _prompts_set(st, prompts: list[dict]) -> None:
    st.set_meta("prompt_library", json.dumps(prompts, ensure_ascii=False))


@app.get("/api/prompts")
async def prompts_list() -> dict:
    st = _storage()
    return {"ok": True, "prompts": _prompts_get(st)}


@app.post("/api/prompts")
async def prompts_save(req: Request) -> dict:
    """保存/新建/删除提示词。Body: {"id": "...", "name": "...", "content": "..."} 或 {"delete_id": "..."}"""
    body = await req.json()
    st = _storage()
    prompts = _prompts_get(st)

    delete_id = body.get("delete_id")
    if delete_id:
        prompts = [p for p in prompts if p.get("id") != delete_id]
        _prompts_set(st, prompts)
        return {"ok": True, "prompts": prompts}

    pid = body.get("id") or f"p{int(time.time()*1000)}"
    name = (body.get("name") or "").strip() or "未命名"
    content = body.get("content") or ""
    found = False
    for p in prompts:
        if p.get("id") == pid:
            p["name"] = name
            p["content"] = content
            found = True
            break
    if not found:
        prompts.append({"id": pid, "name": name, "content": content})
    _prompts_set(st, prompts)
    return {"ok": True, "prompts": prompts}


# ---------------- 统计仪表盘数据 ----------------


@app.get("/api/stats")
async def stats() -> dict:
    """仪表盘首页用的总览统计。"""
    st = _storage()
    with st._lock:
        a_total = st._conn.execute("SELECT COUNT(*) c FROM alphas").fetchone()["c"]
        b_total = st._conn.execute("SELECT COUNT(*) c FROM ai_batches").fetchone()["c"]
        b_running = st._conn.execute(
            "SELECT COUNT(*) c FROM ai_batches WHERE status='running'"
        ).fetchone()["c"]
        s_total = st._conn.execute("SELECT COUNT(*) c FROM simulations").fetchone()["c"]
        s_completed = st._conn.execute(
            "SELECT COUNT(*) c FROM simulations WHERE status='completed'"
        ).fetchone()["c"]
        # 全库 top5
        top = st._conn.execute(
            "SELECT alpha_id, expression, sharpe, fitness, turnover, margin, region "
            "FROM alphas WHERE sharpe IS NOT NULL "
            "ORDER BY ABS(sharpe) DESC LIMIT 5"
        ).fetchall()
    return {
        "ok": True,
        "stats": {
            "alphas": a_total,
            "batches": b_total,
            "batches_running": b_running,
            "simulations": s_total,
            "simulations_completed": s_completed,
        },
        "top": [_serialize(dict(r)) for r in top],
        "db_path": str(_latest_db()),
    }


# ---------------- WebSocket ----------------


@app.websocket("/ws/progress")
async def ws_progress(ws: WebSocket) -> None:
    bus.bind_loop(asyncio.get_event_loop())
    await bus.connect(ws)
    try:
        while True:
            # 客户端发啥都无所谓，关键是保持连接；ping/pong 也行
            data = await ws.receive_text()
            if data == "ping":
                await ws.send_text("pong")
    except WebSocketDisconnect:
        pass
    finally:
        await bus.disconnect(ws)


if __name__ == "__main__":
    import uvicorn

    print(f"千寻 web v81 启动：访问 http://127.0.0.1:8090")
    print(f"数据源：{_latest_db()}")
    uvicorn.run(app, host="127.0.0.1", port=8090, log_level="info")