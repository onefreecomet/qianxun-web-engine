"""ARC（All Region Competition 2026）批量重跑：把已提交 Regular alpha 以 ALL/D1 重投。

平台语义（2026-09-09 实测）：
  - 普通模拟请求把 region 改成 ALL **无效**，必须显式 "type": "REGION_AGNOSTIC"，
    表达式放在 "regular" 字段（不是 "regular.code"）。
  - ALL 下 universe 只有 LARGE / MEDIUM / SMALL 三档，delay 固定 1。
  - 投递成功返回 RA_PARENT（type=RA_PARENT），它的 is 是 {"checks":[...]}，
    **没有 sharpe/fitness**；真实指标在 children（RA_CHILD）上，
    每个子 alpha 落在具体 region（如 USA/TOP3000、EUR/TOP2500、ASI/MINVOL1M、GLB/MINVOL1M）。

边界：只创建 IS 模拟与记录结果，不设置属性、不提交 alpha。
"""
from .runner import (
    ARC_DIR,
    RA_SETTINGS_BASE,
    TERMINAL_OK,
    TERMINAL_FAIL,
    ArcRunner,
    build_inventory,
    build_ra_payload,
    expr_of,
    fetch_alphas_by_ids,
)

__all__ = [
    "ARC_DIR",
    "RA_SETTINGS_BASE",
    "TERMINAL_OK",
    "TERMINAL_FAIL",
    "ArcRunner",
    "build_inventory",
    "build_ra_payload",
    "expr_of",
    "fetch_alphas_by_ids",
]
