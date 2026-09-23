"""ARC 批量重跑执行器。

设计要点：
  1. 逐条串行投递（RA 一次一条，避免并发撞 CONCURRENT_SIMULATION_LIMIT_EXCEEDED）
  2. 每条出结果后**立刻**写 checkpoint 文件，中断/续跑都不丢
  3. 终态集合与文章一致：COMPLETE / ERROR / FAIL / CANCELLED / POST_ERROR
     POST_ERROR 分两种：可重试的队列错 vs 不可恢复的请求错，都记下来，
     但续跑时**只跳过有终态的**，POST_ERROR 允许重跑
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from wq_engine.api.client import RateLimitError

# 文章给出的已验证 RA settings
RA_SETTINGS_BASE: dict[str, Any] = {
    "instrumentType": "EQUITY",
    "region": "ALL",
    "delay": 1,
    "decay": 10,
    "neutralization": "STATISTICAL",
    "truncation": 0.08,
    "pasteurization": "ON",
    "unitHandling": "VERIFY",
    "nanHandling": "OFF",
    "language": "FASTEXPR",
    "visualization": False,
}

TERMINAL_OK = {"COMPLETE"}
TERMINAL_FAIL = {"ERROR", "FAIL", "CANCELLED"}
# 不可恢复的请求错（字段不覆盖 / 语法）：续跑默认跳过
TERMINAL_HARD = {"POST_ERROR_HARD"}
TERMINAL_ALL = TERMINAL_OK | TERMINAL_FAIL | TERMINAL_HARD

# 队列类错误关键词：这些是临时的，不该当成已处理
SOFT_ERROR_KEYS = (
    "CONCURRENT_SIMULATION",
    "429",
    "RATE_LIMIT",
    "TIMEOUT",
)

ARC_DIR = Path(__file__).resolve().parent.parent.parent / "outputs" / "arc"


def expr_of(a: dict) -> str:
    """从 alpha 记录取表达式：REGULAR 型在 regular.code。"""
    reg = a.get("regular")
    if isinstance(reg, dict):
        return str(reg.get("code") or "")
    if isinstance(reg, str):
        return reg
    return ""


def build_inventory(client, *, stage: str = "OS", max_scan: int = 3000) -> list[dict]:
    """列出可重跑的已提交 alpha（默认 stage=OS 且 type=REGULAR）。"""
    records = client.list_all_submitted_alphas_unscoped(max_scan=max_scan)
    out: list[dict] = []
    for r in records:
        if str(r.get("stage") or "").upper() != stage.upper():
            continue
        if str(r.get("type") or "").upper() != "REGULAR":
            continue
        expr = expr_of(r)
        if not expr:
            continue
        st = r.get("settings") or {}
        is_ = r.get("is") or {}
        out.append({
            "src_id": r.get("id"),
            "expr": expr,
            "src_region": st.get("region"),
            "src_universe": st.get("universe"),
            "src_delay": st.get("delay"),
            "src_decay": st.get("decay"),
            "src_neutralization": st.get("neutralization"),
            "src_truncation": st.get("truncation"),
            "src_sharpe": is_.get("sharpe"),
            "src_fitness": is_.get("fitness"),
            "src_turnover": is_.get("turnover"),
            "date_submitted": (r.get("dateSubmitted") or "")[:10],
        })
    return out


def fetch_alphas_by_ids(client, ids: list[str]) -> tuple[list[dict], list[dict]]:
    """按 alpha ID 直接取表达式与 settings（不拉全量清单）。

    返回 (items, errors)。items 与 build_inventory 结构一致，可直接喂 ArcRunner。
    SUPER 型、无表达式、拉取失败的进 errors。
    """
    items: list[dict] = []
    errors: list[dict] = []
    for raw in ids:
        aid = str(raw or "").strip()
        if not aid:
            continue
        try:
            d = client.get_alpha_details(aid)
        except Exception as e:
            errors.append({"src_id": aid, "error": f"拉取失败：{str(e)[:160]}"})
            continue
        atype = str(d.get("type") or "").upper()
        if atype != "REGULAR":
            errors.append({"src_id": aid, "error": f"不是 REGULAR 型（type={atype or '?'}）"})
            continue
        expr = expr_of(d)
        if not expr:
            errors.append({"src_id": aid, "error": "取不到表达式"})
            continue
        st = d.get("settings") or {}
        is_ = d.get("is") or {}
        items.append({
            "src_id": d.get("id") or aid,
            "expr": expr,
            "src_region": st.get("region"),
            "src_universe": st.get("universe"),
            "src_delay": st.get("delay"),
            "src_decay": st.get("decay"),
            "src_neutralization": st.get("neutralization"),
            "src_truncation": st.get("truncation"),
            "src_sharpe": is_.get("sharpe"),
            "src_fitness": is_.get("fitness"),
            "src_turnover": is_.get("turnover"),
            "stage": d.get("stage"),
            "date_submitted": (d.get("dateSubmitted") or "")[:10],
        })
        time.sleep(0.3)
    return items, errors


def build_ra_payload(expr: str, *, universe: str = "LARGE", **overrides) -> dict:
    """构造 RA 请求体。overrides 可覆盖任意 settings 键（decay/neutralization/…）。"""
    settings = {**RA_SETTINGS_BASE, "universe": universe}
    settings.update({k: v for k, v in overrides.items() if v is not None})
    return {
        "type": "REGION_AGNOSTIC",
        "settings": settings,
        "regular": expr,
    }


def _classify_post_error(msg: str) -> str:
    upper = (msg or "").upper()
    if any(k in upper for k in SOFT_ERROR_KEYS):
        return "POST_ERROR_SOFT"
    return "POST_ERROR_HARD"


class ArcRunner:
    """串行重跑一批 alpha，边跑边写 checkpoint。"""

    def __init__(
        self,
        client,
        *,
        universe: str = "LARGE",
        inherit_settings: bool = False,
        decay: int | None = None,
        neutralization: str | None = None,
        truncation: float | None = None,
        poll_timeout: int = 900,
        stop_event=None,
        progress_cb: Callable[[dict], None] | None = None,
        checkpoint_path: Path | None = None,
    ) -> None:
        self.client = client
        self.universe = universe
        self.inherit_settings = inherit_settings
        self.decay = decay
        self.neutralization = neutralization
        self.truncation = truncation
        self.poll_timeout = poll_timeout
        self.stop_event = stop_event
        self.progress_cb = progress_cb or (lambda p: None)
        self.checkpoint_path = checkpoint_path or (
            ARC_DIR / f"ra_run_{time.strftime('%Y%m%d_%H%M%S')}.json"
        )
        self.results: list[dict] = []
        self._done_ids: set[str] = set()

    # -------- checkpoint --------

    def load_prior(self) -> int:
        """续跑：读已有 checkpoint，跳过有终态的条目。返回已跳过条数。"""
        p = self.checkpoint_path
        if not p.exists():
            return 0
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return 0
        rows = data.get("results", []) if isinstance(data, dict) else []
        self.results = [r for r in rows if isinstance(r, dict)]
        self._done_ids = {
            r.get("src_id") for r in self.results
            if r.get("status") in TERMINAL_ALL
        }
        return len(self._done_ids)

    def _flush(self) -> None:
        p = self.checkpoint_path
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "universe": self.universe,
                    "results": self.results,
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
        tmp.replace(p)

    # -------- 单条 --------

    def _settings_for(self, item: dict) -> dict:
        """继承模式下用源 alpha 的 decay/中性化/truncation，只改 region/universe/delay。"""
        if self.inherit_settings:
            return {
                "decay": item.get("src_decay"),
                "neutralization": item.get("src_neutralization"),
                "truncation": item.get("src_truncation"),
            }
        return {
            "decay": self.decay,
            "neutralization": self.neutralization,
            "truncation": self.truncation,
        }

    def _poll(self, loc: str) -> dict:
        t0 = time.time()
        while time.time() - t0 < self.poll_timeout:
            try:
                data = self.client.get_simulation_progress(loc)
            except RateLimitError as e:
                wait = min(e.retry_after or 15.0, 60.0)
                if self._stopped():
                    return {"status": "CANCELLED", "message": "用户中止"}
                time.sleep(wait)
                continue
            status = str(data.get("status") or "")
            if status in TERMINAL_OK or status in TERMINAL_FAIL:
                return data
            if self._stopped():
                return {"status": "CANCELLED", "message": "用户中止"}
            time.sleep(15)
        return {"status": "TIMEOUT", "message": f"轮询超过 {self.poll_timeout}s"}

    def _collect_children(self, parent_id: str) -> list[dict]:
        detail = self.client.get_alpha_details(parent_id)
        rows: list[dict] = []
        for cid in (detail.get("children") or []):
            try:
                cd = self.client.get_alpha_details(cid)
            except Exception as e:
                rows.append({"child_id": cid, "error": str(e)[:200]})
                continue
            cst = cd.get("settings") or {}
            cis = cd.get("is") or {}
            rows.append({
                "child_id": cid,
                "region": cst.get("region"),
                "universe": cst.get("universe"),
                "delay": cst.get("delay"),
                "type": cd.get("type"),
                "sharpe": cis.get("sharpe"),
                "fitness": cis.get("fitness"),
                "turnover": cis.get("turnover"),
                "returns": cis.get("returns"),
                "margin": cis.get("margin"),
                "long_count": cis.get("longCount"),
                "short_count": cis.get("shortCount"),
            })
            time.sleep(0.3)
        return rows

    @staticmethod
    def _summarize(children: list[dict]) -> dict:
        def nums(key: str) -> list[float]:
            out = []
            for c in children:
                v = c.get(key)
                if isinstance(v, (int, float)):
                    out.append(float(v))
            return out

        shp, fit, tvr, ret = nums("sharpe"), nums("fitness"), nums("turnover"), nums("returns")
        return {
            "n_children": len(children),
            "sharpe_avg": round(sum(shp) / len(shp), 4) if shp else None,
            "sharpe_min": round(min(shp), 4) if shp else None,
            "sharpe_max": round(max(shp), 4) if shp else None,
            "fitness_avg": round(sum(fit) / len(fit), 4) if fit else None,
            "fitness_min": round(min(fit), 4) if fit else None,
            "fitness_pass": sum(1 for f in fit if f >= 1.0),
            "turnover_avg": round(sum(tvr) / len(tvr), 4) if tvr else None,
            "returns_avg": round(sum(ret) / len(ret), 4) if ret else None,
        }

    def _stopped(self) -> bool:
        return bool(self.stop_event is not None and self.stop_event.is_set())

    # -------- 主流程 --------

    def run(self, items: list[dict]) -> dict:
        total = len(items)
        skipped = self.load_prior()
        started = time.time()
        done = 0

        for idx, item in enumerate(items, 1):
            src_id = item.get("src_id")
            if src_id in self._done_ids:
                done += 1
                continue
            if self._stopped():
                self.progress_cb({"phase": "stopped", "done": done, "total": total,
                                  "message": "已中止"})
                break

            self.progress_cb({
                "phase": "running", "done": done, "total": total,
                "current": src_id,
                "message": f"[{idx}/{total}] 投递 {src_id}（原 {item.get('src_region')}）",
            })

            row: dict[str, Any] = {
                "src_id": src_id,
                "src_region": item.get("src_region"),
                "src_universe": item.get("src_universe"),
                "src_sharpe": item.get("src_sharpe"),
                "src_fitness": item.get("src_fitness"),
                "src_turnover": item.get("src_turnover"),
                "date_submitted": item.get("date_submitted"),
                "universe": self.universe,
                "started_at": time.strftime("%H:%M:%S"),
            }

            payload = build_ra_payload(
                item.get("expr", ""),
                universe=self.universe,
                **self._settings_for(item),
            )

            try:
                loc = self.client.create_simulation(payload)
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                row.update({
                    "status": _classify_post_error(msg),
                    "error": msg[:300],
                    "children": [],
                    **self._summarize([]),
                })
                self.results.append(row)
                self._flush()
                done += 1
                continue

            row["sim_location"] = loc
            sim = self._poll(loc)
            status = str(sim.get("status") or "")

            if status in TERMINAL_OK:
                parent_id = sim.get("alpha")
                row["ra_parent"] = parent_id
                try:
                    children = self._collect_children(parent_id)
                except Exception as e:
                    children = []
                    row["error"] = f"拉 children 失败：{str(e)[:200]}"
                row["children"] = children
                row.update(self._summarize(children))
            else:
                row["children"] = []
                row.update(self._summarize([]))
                row["error"] = str(sim.get("message") or sim.get("error") or status)[:300]

            row["status"] = status
            self.results.append(row)
            self._flush()
            done += 1

            self.progress_cb({
                "phase": "running", "done": done, "total": total, "current": src_id,
                "message": f"[{idx}/{total}] {src_id} → {status}"
                           + (f"（F均值 {row.get('fitness_avg')}）" if row.get("fitness_avg") is not None else ""),
            })

        elapsed = int(time.time() - started)
        summary = {
            "ok": True,
            "total": total,
            "skipped": skipped,
            "processed": done - skipped,
            "elapsed_sec": elapsed,
            "checkpoint": str(self.checkpoint_path),
            "complete": sum(1 for r in self.results if r.get("status") == "COMPLETE"),
            "failed": sum(1 for r in self.results if r.get("status") not in TERMINAL_OK),
        }
        self.progress_cb({"phase": "completed", "done": done, "total": total,
                          "message": f"完成：{summary['complete']} 条 COMPLETE，"
                                     f"{summary['failed']} 条失败，用时 {elapsed}s",
                          "summary": summary})
        return summary
