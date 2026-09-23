"""千寻 MCP Server（v66）。

WorkBuddy（或其他 MCP 客户端）通过本服务调用千寻引擎：
  - login / status / submit / wait / analyze / radar / resume

启动方式：
  python -m wq_engine.mcp_server
  # 或直接：
  python wq_engine/mcp_server.py

设计：
- 默认连最新 dist_vNN 的 SQLite（避免用户多版本混淆）
- 凭据优先环境变量（WQ_USERNAME/WQ_PASSWORD），回退系统 keyring
- settings 自动补全 13 字段（缺关键字段会 400）
- 长任务（submit/wait）走 BatchScheduler，进程内调用无断链
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("qianxun.mcp")

# 让脚本既能 python -m wq_engine.mcp_server 也能 python wq_engine/mcp_server.py 跑
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    # 标准官方 mcp 包
    from mcp.server.fastmcp import FastMCP  # noqa: E402
except ImportError:  # pragma: no cover - 兼容某些环境下的别名
    from mcp.server.mcpserver import MCPServer as FastMCP  # noqa: E402

from wq_engine.api.config import BrainConfig  # noqa: E402
from wq_engine.api.client import APIClient  # noqa: E402
from wq_engine.storage.database import Storage, expression_key  # noqa: E402
from wq_engine.scheduler.ra_common import (  # noqa: E402
    is_ra_batch as _is_ra_batch,
    ra_settings as _ra_settings,
    ra_backfill_one,
    row_is_ra,
)
from wq_engine.scheduler.super_channel import (  # noqa: E402
    is_super_batch as _is_super_batch,
    submit_super_batch as _submit_super_batch,
    super_todo,
)
from wq_engine.scheduler.runner import BatchScheduler  # noqa: E402


# ---------------- 路径与默认 ----------------

def _project_root() -> Path:
    """定位项目根目录，兼容源码 / PyInstaller 打包（frozen _internal）两种模式。

    - 源码：__file__=wq_engine/mcp_server.py → parent.parent = 项目根
    - frozen：__file__=_internal/wq_engine/mcp_server.py → parent.parent = _internal

    返回值用途：定位 wq_engine 源码包（仅源码用），以及 cwd（frozen 用）。
    """
    p = Path(__file__).resolve().parent.parent
    return p


ROOT = _project_root()


def _work_dir() -> Path:
    """运行工作目录，frozen = exe 所在目录（含 data/、logs/），源码 = 项目根。

    frozen exe 跑 --mcp 时 cwd 自动 = exe 所在目录；源码跑 mcp_server.py 时 cwd
    通常是项目根（用户从项目根调起来）。无论哪种，dist_v* 都在 cwd 或 cwd 父目录的
    兄弟目录。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent  # exe 所在目录
    return Path.cwd()  # 源码模式：用户启动 mcp_server.py 时的 cwd


def _latest_db() -> Path:
    """找最新 dist_vNN 的 db。QIANXUN_DB 环境变量优先；否则从 ROOT（脚本所在项目根）
    和 cwd 上溯 5 级两个方向找，取数字最大的 dist_vNN。

    - 源码模式：ROOT=项目根 → 直接命中 dist_v*/（不依赖 WorkBuddy 的 cwd）
    - frozen 模式：ROOT=_internal（无 dist_v*）→ 靠 cwd 上溯（exe 所在目录的父的父）
    """
    env_db = os.environ.get("QIANXUN_DB", "").strip()
    if env_db and Path(env_db).exists():
        return Path(env_db)
    search_dirs: list[Path] = []
    if ROOT.exists() and ROOT not in search_dirs:
        search_dirs.append(ROOT)
    cur = _work_dir()
    for _ in range(5):
        if cur.exists() and cur not in search_dirs:
            search_dirs.append(cur)
        cur = cur.parent
    best, best_ver = None, -1
    for search in search_dirs:
        if not search.exists():
            continue
        for d in search.glob("dist_v*"):
            if not d.is_dir():
                continue
            m = re.search(r"dist_v(\d+)", d.name)
            if m:
                ver = int(m.group(1))
                db = d / "AlphaMachine" / "data" / "alpha_machine.db"
                if db.exists() and ver > best_ver:
                    best_ver, best = ver, d
        if best:
            break
    if best:
        return best / "AlphaMachine" / "data" / "alpha_machine.db"
    return ROOT / "data" / "alpha_machine.db"


# 连接复用：与 web 侧一致。Storage 单连接线程安全（_ThreadSafeConn），
# 多个 MCP 调用共享一个实例，避免每次调用都 new 连接 + executescript(SCHEMA) 去抢 SQLite 写锁。
_storage_cache: dict[str, Storage] = {}
_storage_cache_lock = threading.Lock()


def _storage() -> Storage:
    p = _latest_db()
    key = str(p)
    with _storage_cache_lock:
        cached = _storage_cache.get(key)
        if cached is not None:
            return cached
        if not p.exists():
            # 空库初始化（首次启动）
            p.parent.mkdir(parents=True, exist_ok=True)
        s = Storage(p)
        _storage_cache[key] = s
        return s


# 共享 client（复用登录态）
# ⚠️ 关键：绝不能每次调用都 new APIClient + authenticate()。
# BRAIN 的 /authentication 有严格限流：批量操作（如 Osmosis 写 25 个 alpha）
# 会打成 25 次登录 → 先 429 rate limit → 再升级为 400 captcha required，
# 最终整批失败。此处做模块级单例，APIClient 内部在收到 401 时会自动重新登录。
_client_singleton: APIClient | None = None
_client_lock = threading.Lock()


def _client() -> APIClient:
    """返回共享的 APIClient 实例（复用登录态）。"""
    global _client_singleton
    with _client_lock:
        if _client_singleton is not None:
            return _client_singleton
        cfg = BrainConfig.from_env()
        if not cfg.is_authenticated():
            raise RuntimeError("未配置凭据：请设置环境变量 WQ_USERNAME / WQ_PASSWORD（或系统 keyring alpha-machine）")
        c = APIClient(cfg)
        c.authenticate()
        _client_singleton = c
        return _client_singleton


def _reset_client() -> None:
    """丢弃共享 client（凭据变更、或认证彻底失效需要重建时调用）。"""
    global _client_singleton
    with _client_lock:
        if _client_singleton is None:
            return
        try:
            _client_singleton.close()
        except Exception:
            pass
        _client_singleton = None


DEFAULT_SETTINGS = {
    "instrumentType": "EQUITY",
    "region": "USA",
    "universe": "TOP3000",
    "delay": 1,
    "decay": 1,
    "neutralization": "SUBINDUSTRY",
    "truncation": 0.08,
    "pasteurization": "ON",
    "testPeriod": "P0Y",
    "unitHandling": "VERIFY",
    "nanHandling": "ON",
    "language": "FASTEXPR",
    "visualization": False,
}


def _normalize_settings(s: dict) -> dict:
    out = dict(DEFAULT_SETTINGS)
    out.update({k: v for k, v in s.items() if v is not None})
    # v82 Quick Simulate（平台 260921 上线）：simulationMode 只接受 QUICK / FULL。
    # FULL = 平台默认 → 从 settings 里剔除，保证发给平台的 payload 与历史版本
    # 逐字节一致（expr_key 哈希、老批次去重都不受影响）；QUICK 原样透传。
    # 非法值直接抛错，上层（MCP/web）转成用户可读的错误信息。
    mode = out.pop("simulationMode", None)
    if mode is not None:
        mode = str(mode).strip().upper()
        if mode not in ("QUICK", "FULL"):
            raise ValueError(
                f"simulationMode 非法：{mode!r}，只支持 QUICK（初筛）或 FULL（完整回测）"
            )
        if mode == "QUICK":
            out["simulationMode"] = "QUICK"
    return out


def _sim_mode_of(settings: dict) -> str:
    """settings → 批次模式（QUICK / FULL）。FULL 已被 _normalize_settings 剔除。"""
    return "QUICK" if settings.get("simulationMode") == "QUICK" else "FULL"


def _quick_mode_block_reason(expressions: list, settings: dict) -> str:
    """QUICK 模式暂不支持的批次形态 → 返回拦截原因；支持则返回空串。

    平台 QUICK Mode 官方示例只给了 REGULAR（帖 43668784587031），
    SUPER×QUICK、RA×QUICK 均未证实 —— 未经实测不放出去烧配额（260923 纪律）。
    """
    if _sim_mode_of(settings) != "QUICK":
        return ""
    if _is_super_batch(expressions):
        return "QUICK 模式暂只支持 REGULAR 批次（SUPER × QUICK 平台未证实，待实测后放开）"
    if _is_ra_batch(expressions, settings):
        return "QUICK 模式暂只支持 REGULAR 批次（RA × QUICK 平台未证实，待实测后放开）"
    return ""


def _event_cb(events: list):
    """收集 BatchScheduler 进度事件，等 run 完后批量回写。"""
    def cb(event: str, payload: dict) -> None:
        events.append((event, payload))
    return cb


# ---------------- SUPER（SuperAlpha）专用通道 ----------------
# v82：实现已抽出为公共模块 wq_engine/scheduler/super_channel.py（MCP 与 web 共用），
# 本文件顶部以 _is_super_batch / _submit_super_batch 别名导入，此处不再重复实现。
# 相对 260921 原版的增强：selection+combo+settings 去重（alphas.expr_key）、
# 预筛组件 <10 自动跳过、并发 3 分波投、进度事件回调。

# ---------------- RA（Region Agnostic / ARC2026）----------------
# v83（260923）：RA 已**原生内建** BatchScheduler（冰神质询「必须走旁路吗」后的
# 工程裁定）—— 入口在此幂等修正 settings，payload type 由 runner 按 region=ALL
# 推导，回填自动收 Child（ra_common.ra_backfill_one）。本地旁路实现已删除；
# SUPER 因 payload 无 regular 字段仍走 super_channel 旁路。

# ---------------- MCP Server ----------------

server = FastMCP("qianxun-engine")


@server.tool(
    name="qianxun_login",
    description="登录测试，返回账号标识。凭据来源：环境变量 WQ_USERNAME/WQ_PASSWORD 优先，回退系统 keyring alpha-machine。",
)
def qianxun_login() -> str:
    try:
        client = _client()
        return json.dumps({"ok": True, "username": client.config.username, "db": str(_latest_db())},
                          ensure_ascii=False)
    except Exception as e:
        return json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)


@server.tool(
    name="qianxun_status",
    description="查询 AI 批次状态。不传 batch_no 列最近 10 条；传 batch_no 查单批详情。",
)
def qianxun_status(batch_no: str = "") -> str:
    try:
        st = _storage()
        if batch_no:
            b = st.get_ai_batch(batch_no)
            return json.dumps({"ok": True, "batch": b}, default=str, ensure_ascii=False)
        rows = st.list_ai_batches(limit=10)
        return json.dumps({"ok": True, "batches": [dict(r) for r in rows]}, default=str, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)


@server.tool(
    name="qianxun_submit",
    description=(
        "提交一批表达式回测。输入 JSON 路径（必须是 {settings, expressions[]} 格式），"
        "自动建批次号 B+YYYYMMDD-NNN，去重已回测过的表达式，自动回填 alpha 详情入库。"
        "batch_size 默认 8、concurrent 默认 3（与 v66 GUI 一致）。"
        "expressions 元素支持三种形态："
        "① REGULAR —— {slot?, expression, decay?}；"
        "② SUPER（SuperAlpha）—— {selection, combo}，检测到即整批走 SUPER 独立通道，"
        "payload 为 {type:SUPER, settings, selection, combo}；按 selection+combo+settings "
        "去重（alphas.expr_key）、预筛组件 <10 自动跳过、并发 ≤3 分波投（v82 起）；"
        "combo 语法坑（260923 实测）：不支持 f(x).y 链式取属性，"
        "如 ts_ir(generate_stats(alpha).returns,250) 会报 \"Unexpected character '.'\"，"
        "必须先 stats=generate_stats(alpha); 赋值再用 stats.returns，"
        "或直接用平台内置 combo_a(alpha)；"
        "③ RA（Region Agnostic Alpha / ARC2026）—— settings.region=ALL（或元素带 "
        "type:REGION_AGNOSTIC / region_agnostic:true）。v83（260923）起**原生走通用批次流**："
        "runner 自动推导 payload type=REGION_AGNOSTIC、强制单条成批、轮询预算 4200s，"
        "入口幂等修正 delay=1、universe∈LARGE|MEDIUM|SMALL、ALL 下 COUNTRY→STATISTICAL；"
        "RA Parent 自身无指标，回填入库的是各区域 RA Child（type='RA'），响应带 mode/parents。"
        "④ settings 支持 simulationMode: \"QUICK\"（Quick Simulate 初筛，平台 260921 上线）——"
        "只允许 REGULAR 批次；QUICK 结果仅供初筛、不可直接提交，入围后需以 FULL 复验；"
        "不写或写 FULL 时与旧行为完全一致（payload 不变）。"
        "返回批次号与提交状态。"
    ),
)
def qianxun_submit(
    json_path: str,
    producer: str = "阿法",
    batch_size: int = 8,
    concurrent: int = 3,
    db_path: str = "",
    no_backfill: bool = False,
) -> str:
    try:
        data = json.loads(Path(json_path).read_text(encoding="utf-8"))
        if "settings" not in data or "expressions" not in data:
            return json.dumps({"ok": False, "error": "JSON 格式错误：需要 {settings, expressions[]}"},
                              ensure_ascii=False)
        settings = _normalize_settings(data["settings"])
        expressions = data["expressions"]

        # ★260923：QUICK 模式拦截前置 —— SUPER/RA 分支在下面，先挡再分流。
        quick_block = _quick_mode_block_reason(expressions, settings)
        if quick_block:
            return json.dumps({"ok": False, "error": quick_block}, ensure_ascii=False)
        sim_mode = _sim_mode_of(settings)

        # ★260923 v83：RA 原生——入口先幂等修正 settings（region=ALL 系），
        # 之后与 REGULAR 完全同流（payload type 由 runner 推导、回填自动收 Child）
        ra_mode = _is_ra_batch(expressions, settings)
        if ra_mode:
            settings = _ra_settings(settings)

        st = Storage(Path(db_path)) if db_path else _storage()

        # ★260921：SUPER（selection/combo）批次尽早分流 —— 下面按 regular 语义
        # 取 e["expression"]，而 SUPER 元素没有该键，不先分流会直接 KeyError。
        # ★260923：分流后先归一化+去重（alphas.expr_key，含同批重复），全部重复则
        # 不建批次直接返回（对齐 regular 分支行为）；提交走 super_channel 公共模块。
        if _is_super_batch(expressions):
            todo_super, skipped_super = super_todo(expressions, settings, st)
            if not todo_super:
                return json.dumps(
                    {"ok": True, "batch_no": None, "skipped": skipped_super,
                     "message": "全部 SUPER 表达式已回测过，无需提交"},
                    ensure_ascii=False)
            batch_no = st.next_batch_no()
            st.create_ai_batch(
                batch_no=batch_no,
                producer=producer,
                dataset_id=str(settings.get("dataset_id", "")),
                region=str(settings["region"]),
                expression_count=len(todo_super),
                note=(f"SUPER 批次（selection/combo）经 MCP Server 提交 "
                      f"{Path(json_path).name}（跳过 {skipped_super}）"),
            )
            st.update_ai_batch_status(batch_no, "running")
            res = _submit_super_batch(
                _client(), st, todo_super, settings, batch_no,
                max_workers=max(1, min(3, int(concurrent))),
            )
            st.update_ai_batch_status(batch_no, "completed")
            return json.dumps({
                "ok": True,
                "batch_no": batch_no,
                "mode": "SUPER",
                "submitted": res["submitted"],
                "completed": res["completed"],
                "failed": res["failed"],
                "skipped": skipped_super,
                "skipped_prescreen": res.get("skipped_prescreen", 0),
                "errors": res.get("errors", []),
            }, ensure_ascii=False)

        # ★260921 的 RA 独立旁路已随 v83（260923）废弃：RA 原生内建 runner，
        # settings 已在上方修正，此处**自然落入下方通用批次流**（去重/task_run/
        # BatchScheduler/回填全复用；响应通过 ra_mode 附加 mode/parents 字段）。

        done = st.completed_expression_keys()
        todo = [(e["expression"], e.get("decay", settings["decay"]), settings)
                for e in expressions
                if expression_key(e["expression"], settings) not in done]
        skipped = len(expressions) - len(todo)
        if not todo:
            return json.dumps({"ok": True, "batch_no": None, "skipped": skipped,
                              "message": "全部表达式已回测过，无需提交"}, ensure_ascii=False)

        batch_no = st.next_batch_no()
        tid = st.create_task_run(
            name=f"AI batch {batch_no}",
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
            sim_mode=sim_mode,
            note=(f"通过 MCP Server 提交 {Path(json_path).name}（跳过 {skipped}）"
                  + ("（RA/REGION_AGNOSTIC）" if ra_mode else "")),
        )

        events: list = []
        client = _client()
        sched = BatchScheduler(
            client, st,
            progress_cb=_event_cb(events),
            max_concurrent_batches=concurrent,
            batch_size=batch_size,
        )
        st.update_ai_batch_status(batch_no, "running")
        sched.run(tid, todo)
        sched.join()
        completed = sum(1 for e, p in events if e == "sim_completed")
        failed = sum(1 for e, p in events if e == "sim_failed")

        # 回填 alpha 详情（v83：RA 批回填 **Children** —— Parent 无指标不入库，
        # 同时收集 parents 映射供响应；REGULAR 路径逐行不变）
        backfilled = 0
        parents: list[dict] = []
        if not no_backfill:
            sims = st.list_completed_simulations(tid)
            for i, s in enumerate(sims, 1):
                try:
                    if row_is_ra(s):
                        info = ra_backfill_one(st, client, s, batch_no)
                        if info:
                            parents.append(info)
                            backfilled += len(info["children"])
                        continue
                    detail = client.get_alpha_details(s["alpha_id"])
                    alpha = client.extract_alpha_metrics(detail)
                    if alpha.get("alpha_id"):
                        st.upsert_alpha(alpha, batch_no=batch_no)
                        backfilled += 1
                except Exception:
                    pass

        st.update_ai_batch_status(batch_no, "completed")

        # v69：把平台真实配额写入 platform_meta（跨进程共享，GUI 回测槽显示用）
        try:
            rl = getattr(client, "last_ratelimit", None)
            if rl and rl.get("limit", 0) > 0:
                st.set_meta("sim_quota", json.dumps(rl))
        except Exception:
            pass

        return json.dumps({
            "ok": True,
            "batch_no": batch_no,
            "task_run_id": tid,
            "submitted": len(todo),
            "completed": completed,
            "failed": failed,
            "skipped": skipped,
            "backfilled": backfilled,
            **({"mode": "RA", "parents": parents} if ra_mode else {}),
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)


@server.tool(
    name="qianxun_wait",
    description="等待批次完成（轮询 ai_batches.status）。timeout 默认 3600 秒；返回最终状态。",
)
def qianxun_wait(batch_no: str, timeout: int = 3600) -> str:
    try:
        st = _storage()
        deadline = time.time() + timeout
        while time.time() < deadline:
            b = st.get_ai_batch(batch_no)
            if b and b["status"] in ("completed", "failed"):
                return json.dumps({"ok": True, "batch_no": batch_no, "status": b["status"]},
                                  ensure_ascii=False)
            time.sleep(10)
        return json.dumps({"ok": False, "error": f"等待超时 {timeout}s，批次仍在跑"},
                          ensure_ascii=False)
    except Exception as e:
        return json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)


@server.tool(
    name="qianxun_analyze",
    description=(
        "读库输出批次结果（按 |sharpe| 降序）。返回 markdown 表格字符串，"
        "列：alpha_id/sharpe/fitness/turnover/margin_bps/region/check。"
    ),
)
def qianxun_analyze(batch_no: str, limit: int = 50) -> str:
    try:
        st = _storage()
        rows = st.list_alphas_by_batch(batch_no, limit=limit)
        if not rows:
            return json.dumps({"ok": True, "message": f"批次 {batch_no} 无已回填结果"},
                              ensure_ascii=False)
        rows.sort(key=lambda a: abs(a.get("sharpe") or 0), reverse=True)
        lines = [f"### 批次 {batch_no}：{len(rows)} 条结果（按 |sharpe| 降序）",
                 "",
                 "| alpha_id | sharpe | fitness | turnover | margin_bps | region | check |",
                 "|---|---:|---:|---:|---:|---|---:|"]
        for a in rows:
            lines.append(f"| {a.get('alpha_id','')} | {a.get('sharpe') or 0:.2f} | "
                         f"{a.get('fitness') or 0:.2f} | {a.get('turnover') or 0:.2f} | "
                         f"{(a.get('margin') or 0) * 10000:.1f} | "
                         f"{a.get('region') or ''} | {a.get('check_status') or ''} |")
        return json.dumps({"ok": True, "markdown": "\n".join(lines)},
                          ensure_ascii=False)
    except Exception as e:
        return json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)


def _load_radar_module():
    """动态加载 direction_radar.py（源码 scripts/ 或 frozen _internal/scripts/），返回模块或 None。

    注意：exec 前必须先注册进 sys.modules，否则模块内 @dataclass 装饰器
    （sys.modules.get(cls.__module__)）拿不到模块对象直接崩。
    """
    import importlib.util
    candidates = [
        ROOT / "scripts" / "direction_radar.py",
        Path(sys.executable).resolve().parent / "_internal" / "scripts" / "direction_radar.py",
        ROOT / "direction_radar.py",
    ]
    for cand in candidates:
        if cand.exists():
            try:
                spec = importlib.util.spec_from_file_location("direction_radar", cand)
                mod = importlib.util.module_from_spec(spec)
                sys.modules["direction_radar"] = mod  # 必须先注册（dataclass 依赖）
                spec.loader.exec_module(mod)
                return mod
            except Exception:
                continue
    return None


@server.tool(
    name="qianxun_radar",
    description=(
        "方向雷达四色信号（GREEN/YELLOW/RED/DEAD + DSI + 护栏 + 建议）。"
        "passed/total 可选；给的话参与 DSI 公式。进程内调用，不依赖外部脚本。"
    ),
)
def qianxun_radar(batch_no: str, passed: int = 0, total: int = 0) -> str:
    try:
        radar = _load_radar_module()
        if radar is None:
            return json.dumps({"ok": False, "error": "direction_radar.py 未找到"}, ensure_ascii=False)
        db = str(_latest_db())
        sharpe, exprs = radar._load_from_db(db, batch_no)
        if not sharpe:
            return json.dumps({"ok": True, "message": f"批次 {batch_no} 无已回填结果，先跑回填再导航",
                               "stdout": ""}, ensure_ascii=False)
        r = radar.evaluate_batch(
            sharpe, passed=passed or None, total=total or None, expressions=exprs or None,
        )
        emoji = {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴", "DEAD": "⚫"}
        lines = [
            f"从库读取批次 {batch_no}：{len(sharpe)} 个 alpha",
            "",
            "=" * 50,
            f"信号：{emoji.get(r.signal, '?')} {r.signal} ｜ DSI={r.dsi:.3f}",
            "=" * 50,
            f"样本 n={r.n}｜均值 {r.mean:.3f}｜最高 {r.max_sharpe:.3f}｜p={r.p_value:.4f}",
            f"显著性 {r.s_ttest:.2f}｜天花板 {r.s_ceiling:.2f}｜通过率下界 {r.pass_rate_lower:.2f}｜稳定性 {r.s_consist:.2f}",
        ]
        if r.family_coverage:
            lines.append(f"算子族覆盖 {r.family_coverage} 种：{', '.join(sorted(r.families))}")
        if r.guards_triggered:
            lines.append(f"护栏触发：{len(r.guards_triggered)}")
            for g in r.guards_triggered:
                lines.append(f"  - {g}")
        if r.advice:
            lines.append(f"建议：{r.advice}")
        return json.dumps({"ok": True, "stdout": "\n".join(lines)}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)


@server.tool(
    name="qianxun_resume",
    description="断点续跑：按批次号找回 task_run，补跑 pending 模拟。",
)
def qianxun_resume(batch_no: str) -> str:
    try:
        st = _storage()
        with st._lock:
            row = st._conn.execute(
                "SELECT id FROM task_runs WHERE batch_no=?", (batch_no,)
            ).fetchone()
        if not row:
            return json.dumps({"ok": False, "error": f"批次 {batch_no} 没有对应任务"},
                              ensure_ascii=False)
        tid = row["id"]
        client = _client()
        sched = BatchScheduler(client, st, progress_cb=_event_cb([]))
        st.update_ai_batch_status(batch_no, "running")
        sched.continue_run(tid)
        sched.join()
        st.update_ai_batch_status(batch_no, "completed")
        return json.dumps({"ok": True, "batch_no": batch_no, "task_run_id": tid},
                          ensure_ascii=False)
    except Exception as e:
        return json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)


def main() -> int:
    p = argparse.ArgumentParser(description="千寻 MCP Server（v66）")
    p.add_argument("--transport", choices=("stdio", "sse", "streamable-http"), default="stdio")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    args = p.parse_args()

    # stdio 默认；sse/http 模式供远程测试用
    if args.transport == "stdio":
        server.run(transport="stdio")
    elif args.transport == "sse":
        server.run(transport="sse", host=args.host, port=args.port)
    else:
        server.run(transport="streamable-http", host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
