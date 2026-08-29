"""本地 corr 算法（v80+）。

移植自 ProdMemo（https://github.com/myacgl/ProdMemo）的 corrWorker.js，做的事情：
- PnL → 日度收益（calendar-window + forward-fill）
- Self Corr / PPA（Power Pool）Corr 同 region 池子过滤
- Pearson 相关系数
- top5 + min/max 输出

为什么移植而不是直接调 ProdMemo：
1. 千寻是 headless Python 后端，ProdMemo 是浏览器扩展（IndexedDB）
2. PnL 同步和 corr 计算是一体的，分两次触发反而绕路
3. 算法本身纯函数，约 200 行 Python，与平台无关

API 输入约束（参考 corrWorker.js）：
  - alpha dict 至少含 id / settings.region / stage / classifications
  - pnl dict 至少含 alphaId / records 字段
  - records 元素为二维数组 [date, cum_pnl]，按 schema.properties 顺序

差异（vs JS 版）：
  - 输入直接吃 dict-of-list 而不是 IndexedDB object store
  - 返回结构同步顶层加 alphaId / corrType，方便入库
  - 抛 ConnectionError 替换 DOMException AbortError
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Iterable

# 常量（与 corrWorker.js 对齐）
YEARS = 4
POWER_POOL_CLASSIFICATION = "POWER_POOL:POWER_POOL_ELIGIBLE"
REGULAR_CLASSIFICATION = "REGULAR:REGULAR"

ALGORITHM_VERSION = 4


# ---------------- 数据适配 ----------------


def normalize_pnl(data: dict | list | None) -> list[tuple[str, float]]:
    """统一 PnL 格式：返回 [(date_str, cum_pnl), ...]，按日期升序。

    支持两种输入：
    1. ProdMemo 原始格式：`{"records": [["2025-01-02", 0.001], ...]}` 二维数组
    2. 千寻 client.get_alpha_pnl 输出：`[{"date": "2025-01-02", "pnl": 0.001}, ...]` 对象数组

    只取每个 record 的前两个字段（date + pnl），过滤空值。
    """
    if not data:
        return []
    # 提取 records 数组
    if isinstance(data, dict):
        records = data.get("records", [])
    else:
        records = data
    if not isinstance(records, list):
        return []
    out = []
    for rec in records:
        try:
            if isinstance(rec, (list, tuple)) and len(rec) >= 2:
                date_str = str(rec[0])[:10]
                pnl = float(rec[1])
            elif isinstance(rec, dict):
                # 兼容 {"date": ..., "pnl": ...} 或 {"Date": ..., "PnL": ...}
                date_str = str(rec.get("date") or rec.get("Date"))[:10]
                pnl = float(rec.get("pnl") or rec.get("PnL"))
            else:
                continue
        except (ValueError, TypeError):
            continue
        if not date_str or not math.isfinite(pnl):
            continue
        out.append((date_str, pnl))
    out.sort(key=lambda x: x[0])
    return out


# ---------------- 窗口与收益 ----------------


def calendar_window_start(records: list[tuple[str, float]]) -> str | None:
    """Self Corr 池子的窗口起点：取最近一年回推 YEARS 年（1月1日起）。

    例：最新记录 2026-08-29 → 起点 2023-01-01。
    """
    if not records:
        return None
    last_year = int(records[-1][0][:4])
    return f"{last_year - YEARS + 1}-01-01"


def calculate_returns(
    records: list[tuple[str, float]],
    start_date: str,
) -> dict[str, float]:
    """目标 alpha 的日收益：相邻两天差值，过滤早于窗口起点的日期。

    returns[date] = value - previous_value
    """
    out: dict[str, float] = {}
    prev = None
    for date, value in records:
        if prev is not None and date >= start_date:
            out[date] = value - prev
        prev = value
    return out


def calculate_forward_filled_returns(
    records: list[tuple[str, float]],
    dates: list[str],
    start_date: str,
) -> dict[str, float]:
    """Peer alpha 的前向填充收益：

    - 在 peer 没记录的日子，保持上一日 value（forward-fill）
    - 只有在看到第一条记录后才开始算收益
    - 同样过滤早于窗口起点的日期

    这是与 ProdMemo 一致的语义：平台算法对缺失日视为持有不变。
    """
    out: dict[str, float] = {}
    record_index = 0
    current_value: float | None = None
    previous_value: float | None = None
    for date in dates:
        while record_index < len(records) and records[record_index][0] <= date:
            current_value = records[record_index][1]
            record_index += 1
        if current_value is None:
            continue
        if previous_value is not None and date >= start_date:
            out[date] = current_value - previous_value
        previous_value = current_value
    return out


# ---------------- 相关系数 ----------------


def pearson(
    target_returns: dict[str, float],
    peer_returns: dict[str, float],
) -> dict | None:
    """Pearson 相关系数（同日期交集）。返回 {value, overlap_count} 或 None（数据不足）。"""
    count = 0
    sum_x = sum_y = 0.0
    sum_xx = sum_yy = sum_xy = 0.0
    for date, x in target_returns.items():
        y = peer_returns.get(date)
        if y is None:
            continue
        count += 1
        sum_x += x
        sum_y += y
        sum_xx += x * x
        sum_yy += y * y
        sum_xy += x * y
    if count < 2:
        return None
    cov = count * sum_xy - sum_x * sum_y
    var_x = count * sum_xx - sum_x * sum_x
    var_y = count * sum_yy - sum_y * sum_y
    denom = math.sqrt(var_x * var_y)
    if not math.isfinite(denom) or denom == 0:
        return None
    value = cov / denom
    if not math.isfinite(value):
        return None
    return {"value": value, "overlap_count": count}


# ---------------- 池子过滤 ----------------


def is_power_pool_alpha(alpha: dict) -> bool:
    return _has_classification(alpha, POWER_POOL_CLASSIFICATION)


def is_regular_alpha(alpha: dict) -> bool:
    return _has_classification(alpha, REGULAR_CLASSIFICATION)


def _has_classification(alpha: dict, target_id: str) -> bool:
    """classifications 数组里的元素可能是 {id: "..."} 字典或纯字符串 id（兼容）。"""
    classifications = alpha.get("classifications") or []
    for item in classifications:
        if isinstance(item, dict):
            if item.get("id") == target_id:
                return True
        elif isinstance(item, str):
            if item == target_id:
                return True
    return False


def select_pool(
    alphas: Iterable[dict],
    pnl_by_id: dict[str, list[tuple[str, float]]],
    target_alpha_id: str,
    region: str,
    corr_type: str,
) -> list[dict]:
    """同 region + corr 类型对应的池子。ProdMemo v2.0.3 规则：

    SELF:
      - alpha.stage == 'OS'
      - 不带 POWER_POOL:POWER_POOL_ELIGIBLE 分类
        或（带 PP + 同时带 REGULAR:REGULAR —— 即"既是 OS regular 也是 PP eligible"）
      - 同 region
    PPA:
      - 带 POWER_POOL:POWER_POOL_ELIGIBLE 分类
      - 同 region
    """
    out = []
    for a in alphas:
        aid = a.get("id")
        if not aid or aid == target_alpha_id:
            continue
        if (a.get("settings") or {}).get("region") != region:
            continue
        if aid not in pnl_by_id:
            continue
        if corr_type == "SELF":
            if a.get("stage") != "OS":
                continue
            # 带 PP 但没 REGULAR 标识 → 排除（v2.0.3 起）
            if is_power_pool_alpha(a) and not is_regular_alpha(a):
                continue
        elif corr_type == "PPA":
            if not is_power_pool_alpha(a):
                continue
        else:
            continue
        out.append(a)
    return out


# ---------------- 顶层算法 ----------------


def calculate_correlation(
    target_alpha_id: str,
    corr_type: str,  # "SELF" / "PPA"
    alphas: list[dict],
    pnls: dict[str, dict],  # alphaId -> {records: [...]} 或 records 数组本身
) -> dict | None:
    """计算单个 alpha 与同 region 池子的 corr。

    返回 dict 或 None（输入不合法 / 池子空 / 无重叠）：
      {
        alpha_id, corr_type, value (pearson), min, max,
        top5: [{alpha_id, correlation}, ...],
        pool_size, window_start, window_end,
        algorithm_version, calculated_at
      }
    """
    if corr_type not in ("SELF", "PPA"):
        raise ValueError(f"corr_type 必须 SELF 或 PPA，got {corr_type}")

    # 找到目标 alpha
    target_alpha = next((a for a in alphas if a.get("id") == target_alpha_id), None)
    if target_alpha is None:
        raise ValueError(f"目标 alpha 不在库中：{target_alpha_id}")
    region = (target_alpha.get("settings") or {}).get("region")
    if not region:
        raise ValueError(f"目标 alpha 缺 region：{target_alpha_id}")

    # 归一化所有 PnL
    pnl_by_id: dict[str, list[tuple[str, float]]] = {}
    for alpha_id, raw in pnls.items():
        normalized = normalize_pnl(raw)
        if len(normalized) >= 2:
            pnl_by_id[alpha_id] = normalized
    if target_alpha_id not in pnl_by_id:
        raise ValueError(f"目标 alpha 缺 PnL：{target_alpha_id}")
    target_records = pnl_by_id[target_alpha_id]
    if len(target_records) < 2:
        raise ValueError(f"目标 alpha PnL 不足 2 条：{target_alpha_id}")

    # 池子
    pool = select_pool(alphas, pnl_by_id, target_alpha_id, region, corr_type)
    if not pool:
        return None

    # 窗口
    target_start = calendar_window_start(target_records)
    target_returns = calculate_returns(target_records, target_start)

    # 全局日期（所有 pool alpha 的并集）
    global_dates_set = set()
    for p in pnl_by_id.values():
        global_dates_set.update(d for d, _ in p)
    global_dates = sorted(global_dates_set)
    if global_dates:
        last_year = int(global_dates[-1][:4])
        pool_start = f"{last_year - YEARS + 1}-01-01"
    else:
        pool_start = target_start
    # 窗口起点取两者中较晚的（与 ProdMemo 一致）
    window_start = max(target_start, pool_start)

    # Pearson 遍历
    correlations = []
    for peer in pool:
        peer_records = pnl_by_id[peer["id"]]
        peer_returns = calculate_forward_filled_returns(peer_records, global_dates, pool_start)
        result = pearson(target_returns, peer_returns)
        if result:
            correlations.append({"alpha": peer, **result})
    correlations.sort(key=lambda c: c["value"], reverse=True)
    if not correlations:
        return None

    values = [c["value"] for c in correlations]
    top5 = [
        {"alpha_id": c["alpha"]["id"], "correlation": round(c["value"], 4)}
        for c in correlations[:5]
    ]
    return {
        "alpha_id": target_alpha_id,
        "corr_type": corr_type,
        "min": round(min(values), 4),
        "max": round(max(values), 4),
        "top5": top5,
        "pool_size": len(pool),
        "corr_count": len(correlations),
        "window_start": window_start,
        "window_end": target_records[-1][0],
        "algorithm_version": ALGORITHM_VERSION,
        "calculated_at": datetime.utcnow().isoformat() + "Z",
    }


# ---------------- 顶层便捷函数 ----------------


def max_corr(self_result: dict | None, ppa_result: dict | None, prod_pc: float | None) -> dict | None:
    """Max Corr = max(|Self_max|, |PPA_max|, |Prod_PC|)。返回 {value, source} 或 None。"""
    candidates = []
    if self_result:
        candidates.append(("self", self_result["max"]))
    if ppa_result:
        candidates.append(("ppa", ppa_result["max"]))
    if prod_pc is not None:
        candidates.append(("prod", prod_pc))
    if not candidates:
        return None
    source, value = max(candidates, key=lambda c: abs(c[1]))
    return {"value": abs(value), "source": source}