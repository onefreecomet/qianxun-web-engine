"""三阶表达式工厂：trade_when(open_event, expr, exit_event)。

修复原 machine_lib.trade_when_factory 的：
- P1 重大：region 参数和 7 个区域事件列表（usa/asi/eur/glb/chn/kor/twn events）
  全部是死代码（从未被引用）→ 现在按 region 真正生效
- 区域事件作为 open_events 的扩展，与通用事件合并

新设计：
- ThirdOrderConfig：输入二阶表达式 + region + 是否启用区域事件
- build_third_order_expressions(cfg) -> [(expr, decay)]
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 通用开仓事件（所有区域）
COMMON_OPEN_EVENTS = [
    "ts_arg_max(volume, 5) == 0",
    "ts_corr(close, volume, 20) < 0",
    "ts_corr(close, volume, 5) < 0",
    "ts_mean(volume,10)>ts_mean(volume,60)",
    "group_rank(ts_std_dev(returns,60), sector) > 0.7",
    "ts_zscore(returns,60) > 2",
    "ts_arg_min(volume, 5) > 3",
    "ts_std_dev(returns, 5) > ts_std_dev(returns, 20)",
    "ts_arg_max(close, 5) == 0",
    "ts_arg_max(close, 20) == 0",
    "ts_corr(close, volume, 5) > 0",
    "ts_corr(close, volume, 5) > 0.3",
    "ts_corr(close, volume, 5) > 0.5",
    "ts_corr(close, volume, 20) > 0",
    "ts_corr(close, volume, 20) > 0.3",
    "ts_corr(close, volume, 20) > 0.5",
]

# 区域专属开仓事件（原 machine_lib 死代码，现在启用）
REGION_OPEN_EVENTS: dict[str, list[str]] = {
    "USA": [
        "rank(rp_css_business) > 0.8", "ts_rank(rp_css_business, 22) > 0.8",
        "rank(vec_avg(mws82_sentiment)) > 0.8", "ts_rank(vec_avg(mws82_sentiment),22) > 0.8",
        "rank(vec_avg(nws48_ssc)) > 0.8", "ts_rank(vec_avg(nws48_ssc),22) > 0.8",
        "rank(vec_avg(mws50_ssc)) > 0.8", "ts_rank(vec_avg(mws50_ssc),22) > 0.8",
        "ts_rank(vec_sum(scl12_alltype_buzzvec),22) > 0.9",
        "pcr_oi_270 < 1", "pcr_oi_270 > 1",
    ],
    "ASI": [
        "rank(vec_avg(mws38_score)) > 0.8", "ts_rank(vec_avg(mws38_score),22) > 0.8",
    ],
    "EUR": [
        "rank(rp_css_business) > 0.8", "ts_rank(rp_css_business, 22) > 0.8",
        "rank(vec_avg(oth429_research_reports_fundamental_keywords_4_method_2_pos)) > 0.8",
        "ts_rank(vec_avg(oth429_research_reports_fundamental_keywords_4_method_2_pos),22) > 0.8",
        "rank(vec_avg(mws84_sentiment)) > 0.8", "ts_rank(vec_avg(mws84_sentiment),22) > 0.8",
        "rank(vec_avg(mws85_sentiment)) > 0.8", "ts_rank(vec_avg(mws85_sentiment),22) > 0.8",
        "rank(mdl110_analyst_sentiment) > 0.8", "ts_rank(mdl110_analyst_sentiment, 22) > 0.8",
        "rank(vec_avg(nws3_scores_posnormscr)) > 0.8",
        "ts_rank(vec_avg(nws3_scores_posnormscr),22) > 0.8",
        "rank(vec_avg(mws36_sentiment_words_positive)) > 0.8",
        "ts_rank(vec_avg(mws36_sentiment_words_positive),22) > 0.8",
    ],
    "GLB": [
        "rank(vec_avg(mdl109_news_sent_1m)) > 0.8", "ts_rank(vec_avg(mdl109_news_sent_1m),22) > 0.8",
        "rank(vec_avg(nws20_ssc)) > 0.8", "ts_rank(vec_avg(nws20_ssc),22) > 0.8",
        "vec_avg(nws20_ssc) > 0",
        "rank(vec_avg(nws20_bee)) > 0.8", "ts_rank(vec_avg(nws20_bee),22) > 0.8",
        "rank(vec_avg(nws20_qmb)) > 0.8", "ts_rank(vec_avg(nws20_qmb),22) > 0.8",
    ],
    "CHN": [
        "rank(vec_avg(oth111_xueqiunaturaldaybasicdivisionstat_senti_conform)) > 0.8",
        "ts_rank(vec_avg(oth111_xueqiunaturaldaybasicdivisionstat_senti_conform),22) > 0.8",
        "rank(vec_avg(oth111_gubanaturaldaydevicedivisionstat_senti_conform)) > 0.8",
        "ts_rank(vec_avg(oth111_gubanaturaldaydevicedivisionstat_senti_conform),22) > 0.8",
        "rank(vec_avg(oth111_baragedivisionstat_regi_senti_conform)) > 0.8",
        "ts_rank(vec_avg(oth111_baragedivisionstat_regi_senti_conform),22) > 0.8",
    ],
    "KOR": [
        "rank(vec_avg(mdl110_analyst_sentiment)) > 0.8",
        "ts_rank(vec_avg(mdl110_analyst_sentiment),22) > 0.8",
        "rank(vec_avg(mws38_score)) > 0.8", "ts_rank(vec_avg(mws38_score),22) > 0.8",
    ],
    "TWN": [
        "rank(vec_avg(mdl109_news_sent_1m)) > 0.8", "ts_rank(vec_avg(mdl109_news_sent_1m),22) > 0.8",
        "rank(rp_ess_business) > 0.8", "ts_rank(rp_ess_business,22) > 0.8",
    ],
}

# 平仓事件
EXIT_EVENTS = ("abs(returns) > 0.1", "-1")


@dataclass
class ThirdOrderConfig:
    """三阶工厂配置。"""

    # 二阶表达式列表（剪枝后的 [(expression, decay)]）
    second_order: list[tuple[str, int]] = field(default_factory=list)

    region: str = "USA"

    # 是否启用区域专属事件（死代码修复后的新功能）
    include_region_events: bool = True

    def validate(self) -> None:
        if not self.second_order:
            raise ValueError("second_order 不能为空（请先选择二阶数据源）")
        from ..settings_registry import REGIONS
        if self.region not in REGIONS:
            raise ValueError(
                f"未知 region：{self.region}（可选：{', '.join(REGIONS)}）"
            )

    def open_events(self) -> list[str]:
        """静态开仓事件：通用 + 区域专属。"""
        events = list(COMMON_OPEN_EVENTS)
        if self.include_region_events:
            events += REGION_OPEN_EVENTS.get(self.region, [])
        return events

    @staticmethod
    def regression_events(field: str) -> list[str]:
        """ts_regression 开仓事件（依赖当前表达式，原 machine_lib 有，v7 补回）。"""
        return [
            f"ts_regression(returns, {field}, 5, lag = 0, rettype = 2) > 0",
            f"ts_regression(returns, {field}, 20, lag = 0, rettype = 2) > 0",
            "ts_regression(returns, ts_step(20), 20, lag = 0, rettype = 2) > 0",
            "ts_regression(returns, ts_step(5), 5, lag = 0, rettype = 2) > 0",
        ]


def build_third_order_expressions(
    cfg: ThirdOrderConfig,
) -> list[tuple[str, int]]:
    """生成三阶表达式：trade_when(open_event, expr, exit_event)。

    输出顺序：开仓事件 × 平仓事件 × 二阶表达式。
    开仓事件 = 静态（通用+区域）+ 动态 ts_regression（每个 expr 一组 4 个）。
    """
    cfg.validate()
    static_events = cfg.open_events()
    out: list[tuple[str, int]] = []
    for expr, decay in cfg.second_order:
        events = static_events + cfg.regression_events(expr)
        for open_event in events:
            for exit_event in EXIT_EVENTS:
                out.append((
                    f"trade_when({open_event}, {expr}, {exit_event})",
                    decay,
                ))
    return out


def estimate_count(cfg: ThirdOrderConfig) -> int:
    # 每个二阶表达式额外 4 个 ts_regression 开仓事件
    per_expr = (len(cfg.open_events()) + 4) * len(EXIT_EVENTS)
    return per_expr * len(cfg.second_order)