"""Alpha 推荐评分引擎。

多维打分 + 否决项：
- 分数 = w_sharpe * |sharpe| + w_fitness * fitness + w_low_turnover + w_margin
- 否决项（任一命中直接淘汰）：check FAIL / PROD_CORR 超限 / 持仓数过低
- 输出：按分数降序的推荐候选，附评分明细
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ScoringConfig:
    """评分权重与否决阈值。"""

    # 权重
    w_sharpe: float = 1.0      # |sharpe| 系数
    w_fitness: float = 0.5     # fitness 系数
    w_turnover: float = 0.3    # 换手低加分系数（换手越低分越高，见 score）
    w_margin: float = 0.2      # margin 系数

    # 否决阈值
    max_prod_corr: float = 0.7   # |PROD_CORR| 超过则否决
    min_abs_sharpe: float = 1.0  # |sharpe| 低于则否决
    min_fitness: float = 0.5     # fitness 低于则否决
    max_turnover: float = 1.0    # turnover 高于则否决（换手太高不能提交）
    min_count: int = 100         # long+short 持仓数低于则否决


@dataclass
class ScoredAlpha:
    """一条带评分结果的推荐候选。"""

    alpha_id: str
    expression: str
    sharpe: float
    fitness: float
    turnover: float
    margin: float
    score: float
    vetoed: bool = False          # 是否被否决
    veto_reasons: list[str] = field(default_factory=list)
    prod_corr: float | None = None   # check 结果（None = 未检查）
    check_failed: list[str] = field(default_factory=list)


def score_alpha(
    alpha: dict,
    cfg: ScoringConfig,
    *,
    prod_corr: float | None = None,
    check_failed: list[str] | None = None,
) -> ScoredAlpha:
    """对单个 alpha 记录打分。

    alpha: storage.list_alphas 行（alpha_id/expression/sharpe/fitness/turnover/margin/long_count/short_count）。
    prod_corr / check_failed：可选，来自 submission check 结果。
    """
    alpha_id = alpha.get("alpha_id", "")

    def _num(v, default: float = 0.0) -> float:
        try:
            val = float(v) if v is not None else default
            if val != val:  # NaN 防御：NaN 参与比较恒 False，评分/排序结果未定义
                return default
            return val
        except (TypeError, ValueError):
            return default

    sharpe = _num(alpha.get("sharpe"))
    fitness = _num(alpha.get("fitness"))
    turnover = _num(alpha.get("turnover"))
    margin = _num(alpha.get("margin"))
    long_count = int(_num(alpha.get("long_count")))
    short_count = int(_num(alpha.get("short_count")))

    vetoed = False
    reasons: list[str] = []

    # 否决项
    if abs(sharpe) < cfg.min_abs_sharpe:
        vetoed = True
        reasons.append(f"|sharpe|={abs(sharpe):.2f} < {cfg.min_abs_sharpe}")
    if fitness < cfg.min_fitness:
        vetoed = True
        reasons.append(f"fitness={fitness:.2f} < {cfg.min_fitness}")
    if turnover > cfg.max_turnover:
        vetoed = True
        reasons.append(f"turnover={turnover:.2f} > {cfg.max_turnover}")
    if (long_count + short_count) < cfg.min_count:
        vetoed = True
        reasons.append(f"持仓 {long_count}+{short_count} < {cfg.min_count}")
    if prod_corr is not None and abs(prod_corr) > cfg.max_prod_corr:
        vetoed = True
        reasons.append(f"|PROD_CORR|={abs(prod_corr):.3f} > {cfg.max_prod_corr}")
    if check_failed:
        vetoed = True
        reasons.append(f"check FAIL: {check_failed}")

    # 分数（换手低加分：turnover 越低越接近满分 w_turnover）
    turnover_score = w_turnover = cfg.w_turnover
    if turnover > 0 and cfg.max_turnover > 0:
        turnover_score = max(0.0, w_turnover * (1.0 - turnover / cfg.max_turnover))

    score = (
        cfg.w_sharpe * abs(sharpe)
        + cfg.w_fitness * max(0.0, fitness)
        + turnover_score
        + cfg.w_margin * max(0.0, margin)
    )

    return ScoredAlpha(
        alpha_id=alpha_id,
        expression=alpha.get("expression", ""),
        sharpe=sharpe,
        fitness=fitness,
        turnover=turnover,
        margin=margin,
        score=score,
        vetoed=vetoed,
        veto_reasons=reasons,
        prod_corr=prod_corr,
        check_failed=check_failed or [],
    )


def recommend_alphas(
    alphas: list[dict],
    cfg: ScoringConfig | None = None,
    *,
    check_results: dict[str, tuple[float | None, list[str]]] | None = None,
    include_vetoed: bool = False,
) -> list[ScoredAlpha]:
    """批量打分并按分数降序排序。

    check_results: {alpha_id: (prod_corr, failed_list)}，可选。
    include_vetoed=False 时过滤掉被否决的候选。
    """
    cfg = cfg or ScoringConfig()
    check_results = check_results or {}

    scored: list[ScoredAlpha] = []
    for alpha in alphas:
        pc, failed = check_results.get(alpha.get("alpha_id"), (None, []))
        item = score_alpha(alpha, cfg, prod_corr=pc, check_failed=failed)
        scored.append(item)

    # 分数降序；被否决的排后面
    scored.sort(key=lambda s: (0 if not s.vetoed else 1, -s.score))
    if not include_vetoed:
        scored = [s for s in scored if not s.vetoed]
    return scored