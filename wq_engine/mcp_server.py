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
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

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


def _storage() -> Storage:
    p = _latest_db()
    if not p.exists():
        # 空库初始化（首次启动）
        p.parent.mkdir(parents=True, exist_ok=True)
    return Storage(p)


def _client() -> APIClient:
    cfg = BrainConfig.from_env()
    if not cfg.is_authenticated():
        raise RuntimeError("未配置凭据：请设置环境变量 WQ_USERNAME / WQ_PASSWORD（或系统 keyring alpha-machine）")
    c = APIClient(cfg)
    c.authenticate()
    return c


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
    return out


def _event_cb(events: list):
    """收集 BatchScheduler 进度事件，等 run 完后批量回写。"""
    def cb(event: str, payload: dict) -> None:
        events.append((event, payload))
    return cb


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
        "batch_size 默认 8、concurrent 默认 3（与 v66 GUI 一致）。返回批次号与提交状态。"
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

        st = Storage(Path(db_path)) if db_path else _storage()
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
            note=f"通过 MCP Server 提交 {Path(json_path).name}（跳过 {skipped}）",
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

        # 回填 alpha 详情
        backfilled = 0
        if not no_backfill:
            sims = st.list_completed_simulations(tid)
            for i, s in enumerate(sims, 1):
                try:
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
