"""表达式生成器：把 Top K 高分算子组装成可回测的 BRAIN alpha 表达式。

设计原则：
- 只生成 BRAIN 平台接受的标准语法：op(field, [arg]) / op(op(field, arg), arg)。
- 算子按 category 区分签名（TS / CROSS_SECTION / VECTOR_NEUTRALIZE / GROUP / SCALAR）。
- 所有产出都带千寻 runner 期望的 settings 字段（neutralization/truncation/decay 等）。
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from typing import Iterable

# 横截面算子：无 ts_ 前缀，单参数
CROSS_SECTION_OPS = {
    "rank", "zscore", "quantile", "normalize",
    "inverse", "reverse", "scale_down", "sigmoid",
    "log", "sign", "abs", "power", "signed_power",
}

# 时序算子：需一个数字窗口
TS_OPS = {
    "ts_rank", "ts_zscore", "ts_delta", "ts_sum", "ts_delay",
    "ts_std_dev", "ts_mean", "ts_arg_min", "ts_arg_max", "ts_scale",
    "ts_quantile", "ts_min", "ts_max", "ts_product",
    "ts_decay_linear", "ts_decay_exp_window", "ts_moment",
}

# 向量化（横截面+时序混合）
VECTORIZED_OPS = {"vector_neut", "vector_norm", "vector_sum"}


@dataclass(frozen=True)
class GeneratedItem:
    expression: str
    decay: int
    settings: dict


def _wrap(op_name: str, field_expr: str, window: int | None) -> str:
    """按算子类型生成签名。"""
    if op_name in TS_OPS:
        return f"{op_name}({field_expr}, {window})"
    if op_name in CROSS_SECTION_OPS:
        return f"{op_name}({field_expr})"
    if op_name in VECTORIZED_OPS:
        return f"{op_name}({field_expr}, {field_expr})"
    # 未知算子：默认按横截面调用（1 参数），失败由回测兜底提示
    return f"{op_name}({field_expr})"


def _score_op(op_name: str, score: int, category: str) -> tuple[str, int]:
    """返回算子所属家族 + 该算子适合的窗口（TS 才有）。"""
    cat = (category or "").lower()
    if op_name in TS_OPS or "time series" in cat or cat.startswith("ts_"):
        return "ts", 1
    if op_name in CROSS_SECTION_OPS or "cross" in cat or "rank" in cat:
        return "cs", 0
    if op_name in VECTORIZED_OPS or "vector" in cat:
        return "vec", 0
    return "cs", 0


def build_from_top_operators(
    top_operators: list[dict],
    *,
    field_exprs: list[str],
    default_settings: dict,
    windows: Iterable[int] = (5, 22, 66, 120, 240),
    decay: int = 1,
    min_score: int = 6,
    max_items: int = 50,
    include_raw_field: bool = True,
) -> list[GeneratedItem]:
    """根据 Top K 算子 + 数据字段，生成一批可回测表达式。

    输入 top_operators: [{"operator": str, "category": str, "score": int, ...}, ...]
    输出: [GeneratedItem, ...]

    容量控制：max_items 硬上限，避免一次灵感出几千条把回测塞爆。
    """
    out: list[GeneratedItem] = []
    seen: set[str] = set()
    win_list = list(windows)
    if not win_list:
        win_list = [22]

    def push(expr: str) -> None:
        if expr in seen or len(out) >= max_items:
            return
        seen.add(expr)
        out.append(GeneratedItem(expression=expr, decay=decay, settings=dict(default_settings)))

    # 1) 裸字段
    if include_raw_field:
        for f in field_exprs:
            push(f)

    # 2) 高分算子 × 字段（窗口型算子枚举 windows，其它只用一份）
    for op in top_operators:
        if len(out) >= max_items:
            break
        name = op.get("operator")
        if not name:
            continue
        score = int(op.get("score", 0) or 0)
        if score < min_score:
            continue
        kind, _ = _score_op(name, score, op.get("category", ""))
        wins = win_list if kind == "ts" else [None]
        for f, w in product(field_exprs, wins):
            expr = _wrap(name, f, w if kind == "ts" else None)
            push(expr)
            if len(out) >= max_items:
                break

    # 3) 套娃：rank(ts_op(field, w)) —— 给高分 TS 算子套一层截面（反因子经典手法）
    for op in top_operators:
        if len(out) >= max_items:
            break
        name = op.get("operator")
        if not name or name not in TS_OPS:
            continue
        if int(op.get("score", 0) or 0) < min_score:
            continue
        for f, w in product(field_exprs, win_list):
            inner = f"{name}({f}, {w})"
            push(f"rank({inner})")
            push(f"ts_rank({inner}, 60)")
            if len(out) >= max_items:
                break

    return out


def default_settings_from(
    *,
    region: str = "USA",
    universe: str = "TOP3000",
    neutralization: str = "subindustry",
    truncation: float = 0.08,
    delay: int = 1,
    decay: int | None = None,
    visualization: bool = False,
    nan_handling: str = "ON",
) -> dict:
    """千寻 runner 期望的完整 settings（与一阶 Tab _build_config 对齐，2026-08-09 修）。

    之前只给 region/neutralization/truncation/delay/visualization/nanHandling，
    缺 universe/instrumentType/pasteurization/testPeriod/unitHandling/language，
    导致批量模拟提交 400 全失败、任务秒"完成"。
    """
    return {
        "instrumentType": "EQUITY",
        "region": region,
        "universe": universe,
        "delay": int(delay),
        "decay": int(decay) if decay is not None else 1,
        "neutralization": neutralization,
        "truncation": float(truncation),
        "pasteurization": "ON",
        "testPeriod": "P0Y",
        "unitHandling": "VERIFY",
        "nanHandling": nan_handling,
        "language": "FASTEXPR",
        "visualization": bool(visualization),
    }