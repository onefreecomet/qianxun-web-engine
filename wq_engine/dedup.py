"""表达式去重与采样工具。

解决"随机采样可能重复回测已回测过的表达式"的资源浪费问题：
1. filter_unverified：从表达式列表剔除已在本地标记为回测成功（completed）的指纹
2. sample_unverified：从未回测的池子里随机采样 N 个（N=0 表示全要）
"""

from __future__ import annotations

import random

from .storage.database import expression_key

ExpressionTriple = tuple[str, int, dict]  # (expression, decay, settings)


def filter_unverified(
    expressions: list[ExpressionTriple],
    completed_keys: set[str],
) -> tuple[list[ExpressionTriple], int]:
    """剔除已回测成功的表达式。

    返回 (未回测列表, 剔除数量)。
    completed_keys: storage.completed_expression_keys() 的结果。
    """
    if not completed_keys:
        return list(expressions), 0

    kept: list[ExpressionTriple] = []
    skipped = 0
    for expr, decay, settings in expressions:
        key = expression_key(expr, settings)
        if key in completed_keys:
            skipped += 1
        else:
            kept.append((expr, decay, settings))
    return kept, skipped


def sample_unverified(
    expressions: list[ExpressionTriple],
    completed_keys: set[str],
    sample_n: int = 0,
    *,
    seed: int | None = None,
) -> tuple[list[ExpressionTriple], int, int]:
    """从未回测池子里随机采样。

    返回 (最终列表, 剔除数量, 未回测池大小)。
    - sample_n <= 0：全部未回测的都跑
    - sample_n > 0：从去重后的池子随机抽 sample_n 个
    """
    unverified, skipped = filter_unverified(expressions, completed_keys)

    if sample_n > 0 and len(unverified) > sample_n:
        rng = random.Random(seed) if seed is not None else random
        chosen = rng.sample(unverified, sample_n)
        return chosen, skipped, len(unverified)

    return unverified, skipped, len(unverified)