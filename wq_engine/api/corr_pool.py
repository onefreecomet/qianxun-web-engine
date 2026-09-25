"""OS 池子加载与缓存 —— 供本地 Self/PPA Corr 使用（v81+）。

背景（为什么要单独缓存）：
  Self/PPA 的池子 = 账号下**已提交(stage=OS)**、同 region 的 alpha 的 PnL。
  这批数据不属于 `alphas` 候选表（那张表存的是模拟出来的候选，引擎拿它做
  去重/统计/投稿清单）。把 OS 池子塞进 `alphas` 会污染候选库（top 榜、导出、
  golden-bag 统计都会被 OS 行混进来），所以池子单独落盘缓存。

缓存目录：<project>/outputs/corr_pool/
  os_alphas.json        list[alpha]（含 id / stage / classifications / settings）
  pnl/<alpha_id>.json   [[date, pnl], ...]
  meta.json             {"updated_at": ..., "count": n}

首次点「算 corr」会拉一遍池子 PnL（USA 53 / IND 20 / HKG 10 条量级），
所以第一次慢（几十秒），之后 24h 内秒回。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Callable

_PROJECT = Path(__file__).resolve().parent.parent.parent
_DIR = _PROJECT / "outputs" / "corr_pool"
_PNL_DIR = _DIR / "pnl"
_ALPHAS = _DIR / "os_alphas.json"
_META = _DIR / "meta.json"

DEFAULT_TTL_H = 24.0

_lock = threading.Lock()


def _ensure_dir() -> None:
    _PNL_DIR.mkdir(parents=True, exist_ok=True)


def cache_dir() -> Path:
    return _DIR


def _read_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _age_hours(p: Path) -> float:
    try:
        return (time.time() - p.stat().st_mtime) / 3600.0
    except Exception:
        return 1e9


def load_os_alphas(
    client=None,
    *,
    max_age_h: float | None = DEFAULT_TTL_H,
    force: bool = False,
) -> list[dict]:
    """取账号已提交 alpha 清单。未过期直接读缓存；过期/缺失且有 client 时重拉。"""
    with _lock:
        fresh = (not force) and _ALPHAS.exists() and max_age_h is not None \
            and _age_hours(_ALPHAS) <= max_age_h
        if fresh:
            data = _read_json(_ALPHAS)
            if data:
                return data
        if client is None:
            # 没 client：能用旧的就用旧的，返回空列表也行（调用方会显示「池子为空」）
            return _read_json(_ALPHAS) or []
        alphas = client.list_all_submitted_alphas(page_size=100)
        _ensure_dir()
        _ALPHAS.write_text(json.dumps(alphas, ensure_ascii=False), encoding="utf-8")
        _META.write_text(json.dumps({
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "count": len(alphas),
        }, ensure_ascii=False), encoding="utf-8")
        return alphas


def get_pnl(client, alpha_id: str, *, max_age_h: float | None = DEFAULT_TTL_H,
            force: bool = False) -> list | None:
    """取单个 alpha 的日度 PnL（[[date, pnl], ...]），带磁盘缓存。"""
    p = _PNL_DIR / f"{alpha_id}.json"
    if (not force) and p.exists() and (max_age_h is None or _age_hours(p) <= max_age_h):
        recs = _read_json(p)
        if recs:
            return recs
    try:
        pnl = client.get_alpha_pnl(alpha_id)
    except Exception:
        return None
    if not isinstance(pnl, list):
        return None
    recs = [[r.get("date") or r.get("Date"), r.get("pnl") or r.get("PnL")]
            for r in pnl if isinstance(r, dict)]
    recs = [r for r in recs if r and r[0] is not None and r[1] is not None]
    if not recs:
        return None
    _ensure_dir()
    p.write_text(json.dumps(recs), encoding="utf-8")
    return recs


def build_pool(
    client,
    region: str,
    *,
    on_progress: Callable[[int, int], None] | None = None,
    max_age_h: float | None = DEFAULT_TTL_H,
) -> tuple[list[dict], dict[str, dict]]:
    """构建某 region 的 OS 池子。

    返回 (pool_alphas, pnls)：
      pool_alphas  同 region 的 OS alpha（含 stage/classifications，供 select_pool 判据）
      pnls         {alpha_id: {"records": [[date, pnl], ...]}}
    """
    alphas = load_os_alphas(client, max_age_h=max_age_h)
    pool = [a for a in alphas
            if a.get("stage") == "OS"
            and (a.get("settings") or {}).get("region") == region]
    pnls: dict[str, dict] = {}
    for i, a in enumerate(pool, 1):
        recs = get_pnl(client, a["id"], max_age_h=max_age_h)
        if recs:
            pnls[a["id"]] = {"records": recs}
        if on_progress:
            try:
                on_progress(i, len(pool))
            except Exception:
                pass
    return pool, pnls


def stats() -> dict:
    """缓存概况（给前端显示用）。"""
    alphas = _read_json(_ALPHAS) or []
    meta = _read_json(_META) or {}
    n_pnl = len(list(_PNL_DIR.glob("*.json"))) if _PNL_DIR.exists() else 0
    return {
        "os_alphas": len(alphas),
        "pnl_cached": n_pnl,
        "updated_at": meta.get("updated_at"),
        "cache_dir": str(_DIR),
    }
