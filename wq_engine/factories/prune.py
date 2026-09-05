"""表达式剪枝：按字段前缀去重，每字段保留 top N（按 |sharpe|）。

语义还原自原 machine_lib.prune：
- 正负 sharpe 分开统计：同字段的正向 alpha 与反向 alpha（负 sharpe）各自保留
  keep_num 个，反向信号不会被正向挤占。
改进：显式按 |sharpe| 降序取 top N（原代码是"先到先得"计数法，依赖 API
按 sharpe 降序返回的隐式排序，显式排序更稳）。
"""

from __future__ import annotations

from collections import defaultdict


def prune_expressions(
    alphas: list[dict],
    prefix: str,
    keep_num: int,
) -> list[tuple[str, int]]:
    """从 alpha 指标记录中按数据字段前缀剪枝。

    输入：alphas 列表，每项含 alpha_id / expression / sharpe / decay（storage 行或 dict）。
    输出：[(expression, decay)] 每字段（正负分开）保留 top keep_num 个（按 |sharpe| 降序）。
    """
    if not alphas:
        return []

    # 按"字段 + 正负号"分组（负 sharpe 标记 -field，原 machine_lib 语义）
    grouped: dict[str, list[dict]] = defaultdict(list)
    for alpha in alphas:
        exp = alpha.get("expression") or ""
        if not isinstance(exp, str) or prefix not in exp:
            continue
        # 字段提取：从 prefix 出现处向后扩展到完整标识符。
        # 修复原 split(",")[0] 的缺陷：① 嵌套函数（vec_avg(mws82_sentiment)）
        # 尾部是 ")" 导致字段提取成 ")"；② vec_avg/vec_sum 变体被拆成两组。
        i = exp.find(prefix)
        j = i + len(prefix)
        while j < len(exp) and (exp[j].isalnum() or exp[j] == "_"):
            j += 1
        field = exp[i:j]
        if not field:
            continue
        sharpe = alpha.get("sharpe") or 0
        try:
            negative = float(sharpe) < 0
        except (TypeError, ValueError):
            negative = False
        key = f"-{field}" if negative else field
        grouped[key].append(alpha)

    output: list[tuple[str, int]] = []
    for field, records in grouped.items():
        # 组内按 |sharpe| 降序取前 keep_num（显式排序，不依赖 API 返回顺序）
        def _abs_sharpe(a) -> float:
            try:
                return abs(float(a.get("sharpe") or 0))
            except (TypeError, ValueError):
                return 0.0

        records_sorted = sorted(records, key=_abs_sharpe, reverse=True)
        for rec in records_sorted[:max(0, keep_num)]:
            output.append((rec["expression"], rec.get("decay") or 1))
    return output