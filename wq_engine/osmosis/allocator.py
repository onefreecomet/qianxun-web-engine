#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Osmosis 分配器核心算法（Web 可调用版）。

基于 osmosis_allocator.py 的核心评分、过滤、相关性、贪心选择与分数分配逻辑，
改造为无全局状态、无 stdout 输出、返回 JSON 可序列化结果的纯函数。

关闭慢速可选项（yearly-stats / pnl-series / external-correlations），
使用列表 payload 自带的字段进行快速分配预览/写入。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

import pandas as pd
from loguru import logger


@dataclass
class OsmosisConfig:
    """Osmosis 分配器配置。默认值复刻原 osmosis_allocator.py。"""

    # 赛道
    region: str = "USA"
    delay: int = 1

    # 总分
    total_points: int = 100_000

    # 选择数量
    target_alpha_count: int = 20
    min_alpha_count: int = 10
    max_alpha_count: int = 25
    fill_to_min_alpha_count: bool = True
    filler_quality_discount: float = 0.35

    # SUPER 预算
    super_point_share: float = 0.22
    super_max_alpha_count: int = 6
    regular_min_point_share: float = 0.65

    # 单 alpha 分数上下限
    min_points_per_alpha: int = 500
    regular_max_points_per_alpha: int = 15_000
    super_max_points_per_alpha: int = 10_000

    # 相关性阈值
    max_pnl_corr: float = 0.70
    soft_pnl_corr: float = 0.45
    max_self_corr: float = 0.75
    max_prod_corr: float = 0.75

    # 硬过滤阈值
    regular_min_sharpe: float = 1.20
    regular_min_fitness: float = 0.80
    regular_max_drawdown: float = 0.70
    relaxed_regular_min_sharpe: float = 0.80
    relaxed_regular_min_fitness: float = 0.40
    relaxed_regular_max_drawdown: float = 0.90

    # 扫描上限
    max_alpha_scan: int = 1000

    # 慢速可选项（默认关闭，避免大量 API 调用）
    fetch_yearly_stats: bool = False
    fetch_pnl_for_diversity: bool = False
    fetch_external_correlations: bool = False

    # 写平台相关（allocate 时才用）
    clear_existing_first: bool = True


def clean_region(region: Any) -> str:
    return str(region).strip().upper()


def clean_delay(delay: Any) -> int:
    return int(str(delay).strip())


def scope_name(region: str, delay: int) -> str:
    return f"{clean_region(region)}/D{clean_delay(delay)}"


def to_float(value: Any, default: float = math.nan) -> float:
    if value is None:
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(result) or math.isinf(result):
        return default
    return result


def to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def first_present(obj: dict[str, Any], names: Iterable[str]) -> Any:
    for name in names:
        if name in obj and obj[name] is not None:
            return obj[name]
    return None


def alpha_code_blob(record: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("regular", "combo", "selection"):
        value = record.get(key)
        if isinstance(value, dict):
            for subkey in ("code", "description", "expression"):
                if value.get(subkey):
                    parts.append(str(value[subkey]))
        elif value:
            parts.append(str(value))
    return "\n".join(parts)


def alpha_signature(record: dict[str, Any]) -> str:
    settings = record.get("settings") or {}
    code = alpha_code_blob(record).lower()
    tokens = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", code)
    ignored = {
        "rank", "ts_rank", "zscore", "group_neutralize", "winsorize", "scale",
        "ts_mean", "ts_std_dev", "ts_delay", "if_else", "and", "or", "not", "nan",
    }
    useful = [t for t in tokens if t not in ignored and len(t) > 2]
    top_tokens = sorted(set(useful[:80]))[:12]
    basis = [
        str(settings.get("universe", "")),
        str(settings.get("neutralization", "")),
        str(settings.get("decay", "")),
    ]
    return "|".join(basis + top_tokens)


def extract_alpha_type(record: dict[str, Any]) -> str:
    raw = str(record.get("type") or "").upper()
    if raw in ("REGULAR", "SUPER"):
        return raw
    if record.get("combo") or record.get("selection"):
        return "SUPER"
    return "REGULAR"


def extract_corr(record: dict[str, Any], kind: str) -> float:
    if kind == "self":
        names = (
            "selfCorrelation", "selfCorr", "self_corr", "maxSelfCorrelation", "maxSelfCorr",
        )
    else:
        names = (
            "prodCorrelation", "productionCorrelation", "prodCorr", "prod_corr",
            "maxProdCorrelation", "maxProdCorr",
        )

    value = first_present(record, names)
    if value is not None:
        return to_float(value)

    checks = record.get("checks")
    if isinstance(checks, list):
        for item in checks:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("result") or "").lower()
            if kind in name and "correlation" in name:
                value = first_present(
                    item,
                    ("value", "limit", "result", "max", "score", "correlation"),
                )
                parsed = to_float(value)
                if not math.isnan(parsed):
                    return parsed

    return math.nan


def first_metric_block(record: dict[str, Any], names: Iterable[str]) -> dict[str, Any]:
    for name in names:
        value = record.get(name)
        if isinstance(value, dict):
            return value
    return {}


def metric_block_has_core_values(data: dict[str, Any]) -> bool:
    for key in ("sharpe", "fitness", "returns", "margin", "turnover", "drawdown"):
        if not math.isnan(to_float(data.get(key))):
            return True
    return False


def choose_metric_blocks(record: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str]:
    is_data = first_metric_block(record, ("is", "IS", "inSample", "in_sample"))
    os_data = first_metric_block(record, ("os", "OS", "outOfSample", "out_of_sample", "oos", "OOS"))

    if metric_block_has_core_values(os_data):
        return os_data, is_data, os_data, "OS"
    return is_data, is_data, os_data, "IS"


def preferred_metric(preferred: dict[str, Any], fallback: dict[str, Any], key: str) -> float:
    value = to_float(preferred.get(key))
    if not math.isnan(value):
        return value
    return to_float(fallback.get(key))


def flatten_alpha(record: dict[str, Any], region: str, delay: int) -> dict[str, Any]:
    """把 BRAIN alpha 记录拍平为 DataFrame 行。"""
    settings = record.get("settings") or {}
    metric_data, is_data, os_data, metric_source = choose_metric_blocks(record)
    alpha_id = str(record.get("id") or "")
    alpha_type = extract_alpha_type(record)

    return {
        "alpha_id": alpha_id,
        "type": alpha_type,
        "stage": record.get("stage"),
        "status": record.get("status"),
        "region": clean_region(settings.get("region") or region),
        "delay": to_int(settings.get("delay"), delay),
        "scope": scope_name(region, delay),
        "dateSubmitted": record.get("dateSubmitted"),
        "dateCreated": record.get("dateCreated"),
        "color": record.get("color"),
        "osmosisPoints": to_float(record.get("osmosisPoints")),
        "metric_source": metric_source,
        "sharpe": preferred_metric(metric_data, is_data, "sharpe"),
        "fitness": preferred_metric(metric_data, is_data, "fitness"),
        "returns": preferred_metric(metric_data, is_data, "returns"),
        "turnover": preferred_metric(metric_data, is_data, "turnover"),
        "drawdown": preferred_metric(metric_data, is_data, "drawdown"),
        "margin": preferred_metric(metric_data, is_data, "margin"),
        "longCount": preferred_metric(metric_data, is_data, "longCount"),
        "shortCount": preferred_metric(metric_data, is_data, "shortCount"),
        "os_sharpe": to_float(os_data.get("sharpe")),
        "os_fitness": to_float(os_data.get("fitness")),
        "os_returns": to_float(os_data.get("returns")),
        "os_turnover": to_float(os_data.get("turnover")),
        "os_drawdown": to_float(os_data.get("drawdown")),
        "os_margin": to_float(os_data.get("margin")),
        "is_sharpe": to_float(is_data.get("sharpe")),
        "is_fitness": to_float(is_data.get("fitness")),
        "is_returns": to_float(is_data.get("returns")),
        "is_turnover": to_float(is_data.get("turnover")),
        "is_drawdown": to_float(is_data.get("drawdown")),
        "is_margin": to_float(is_data.get("margin")),
        "self_corr": extract_corr(record, "self"),
        "prod_corr": extract_corr(record, "prod"),
        "universe": settings.get("universe"),
        "neutralization": settings.get("neutralization"),
        "decay": settings.get("decay"),
        "truncation": settings.get("truncation"),
        "code_signature": alpha_signature(record),
        "pnl_series": None,
        "year_score": 0.50,
        "raw": record,
    }


def rank01(series: pd.Series, higher_is_better: bool = True) -> pd.Series:
    values = pd.to_numeric(series, errors="coerce")
    if values.notna().sum() <= 1:
        return pd.Series(0.5, index=series.index)
    ranked = values.rank(pct=True, ascending=higher_is_better)
    return ranked.fillna(0.5).clip(0.0, 1.0)


def turnover_quality(value: Any) -> float:
    tv = to_float(value)
    if math.isnan(tv) or tv <= 0:
        return 0.45
    if 0.03 <= tv <= 0.30:
        return 1.0
    if 0.01 <= tv < 0.03:
        return 0.75 + 0.25 * (tv - 0.01) / 0.02
    if 0.30 < tv <= 0.60:
        return max(0.55, 1.0 - (tv - 0.30) / 0.30 * 0.45)
    if tv < 0.01:
        return max(0.35, tv / 0.01 * 0.75)
    return max(0.20, 0.55 - min(tv - 0.60, 1.0) * 0.35)


def corr_quality(row: pd.Series) -> float:
    vals = [to_float(row.get("self_corr")), to_float(row.get("prod_corr"))]
    vals = [v for v in vals if not math.isnan(v)]
    if not vals:
        return 0.65
    worst = max(abs(v) for v in vals)
    return max(0.0, 1.0 - worst)


def add_base_scores(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    out = df.copy()
    out["turnover_quality"] = out["turnover"].map(turnover_quality)
    out["corr_quality"] = out.apply(corr_quality, axis=1)
    out["year_score"] = out.get("year_score", pd.Series(0.5, index=out.index))
    out["pnl_smoothness"] = 0.50

    scored_chunks: list[pd.DataFrame] = []
    for alpha_type, chunk in out.groupby("type", dropna=False):
        part = chunk.copy()
        sharpe_rank = rank01(part["sharpe"], True)
        fitness_rank = rank01(part["fitness"], True)
        returns_rank = rank01(part["returns"], True)
        margin_rank = rank01(part["margin"], True)
        drawdown_rank = rank01(part["drawdown"], False)
        turnover_rank = part["turnover_quality"].fillna(0.45)
        corr_rank = part["corr_quality"].fillna(0.65)

        if alpha_type == "SUPER":
            part["is_quality"] = (
                0.38 * fitness_rank
                + 0.30 * sharpe_rank
                + 0.14 * returns_rank
                + 0.10 * drawdown_rank
                + 0.08 * margin_rank
            )
            part["base_quality_score"] = (
                0.45 * part["is_quality"]
                + 0.25 * turnover_rank
                + 0.20 * corr_rank
                + 0.10 * part["year_score"]
            )
        else:
            part["is_quality"] = (
                0.30 * sharpe_rank
                + 0.30 * fitness_rank
                + 0.15 * returns_rank
                + 0.15 * margin_rank
                + 0.10 * drawdown_rank
            )
            part["base_quality_score"] = (
                0.42 * part["is_quality"]
                + 0.24 * part["year_score"]
                + 0.20 * turnover_rank
                + 0.09 * corr_rank
                + 0.05 * rank01(part["longCount"].fillna(0) + part["shortCount"].fillna(0))
            )

        scored_chunks.append(part)

    scored = pd.concat(scored_chunks, ignore_index=True)
    scored["base_quality_score"] = scored["base_quality_score"].clip(0.0, 1.0)
    return scored


def add_filter_reasons(df: pd.DataFrame, config: OsmosisConfig) -> pd.DataFrame:
    if df.empty:
        return df

    out = df.copy()
    out["filter_reason"] = ""

    invalid_status = out["status"].astype(str).str.upper().isin(["UNSUBMITTED", "IS_FAIL"])
    out.loc[invalid_status, "filter_reason"] += "bad_status;"

    target_region = clean_region(config.region)
    target_delay = clean_delay(config.delay)
    wrong_scope = (out["region"] != target_region) | (out["delay"] != target_delay)
    out.loc[wrong_scope, "filter_reason"] += "wrong_scope;"

    low_regular = (
        (out["type"] == "REGULAR")
        & (
            (out["sharpe"].fillna(-999) < config.regular_min_sharpe)
            | (out["fitness"].fillna(-999) < config.regular_min_fitness)
        )
    )
    out.loc[low_regular, "filter_reason"] += "regular_low_is;"

    bad_drawdown = (
        (out["type"] == "REGULAR")
        & out["drawdown"].notna()
        & (out["drawdown"] > config.regular_max_drawdown)
    )
    out.loc[bad_drawdown, "filter_reason"] += "high_drawdown;"

    high_self = out["self_corr"].notna() & (out["self_corr"].abs() > config.max_self_corr)
    high_prod = out["prod_corr"].notna() & (out["prod_corr"].abs() > config.max_prod_corr)
    out.loc[high_self, "filter_reason"] += "high_self_corr;"
    out.loc[high_prod, "filter_reason"] += "high_prod_corr;"

    super_mask = out["type"] == "SUPER"
    if super_mask.any():
        super_scores = out.loc[super_mask, "base_quality_score"]
        if super_scores.notna().sum() >= 4:
            cutoff = max(0.35, float(super_scores.quantile(0.25)))
            weak_super = super_mask & (out["base_quality_score"] < cutoff)
            out.loc[weak_super, "filter_reason"] += "super_bottom_quartile;"

    return out


def apply_hard_filters(df: pd.DataFrame, config: OsmosisConfig) -> pd.DataFrame:
    if df.empty:
        return df
    out = add_filter_reasons(df, config)
    return out[out["filter_reason"] == ""].copy()


def build_corr_matrix(df: pd.DataFrame) -> pd.DataFrame:
    series_map: dict[str, pd.Series] = {}
    for _, row in df.iterrows():
        alpha_id = str(row["alpha_id"])
        series = row.get("pnl_series")
        if isinstance(series, pd.Series) and len(series.dropna()) >= 8:
            series_map[alpha_id] = series.dropna()

    if len(series_map) < 2:
        return pd.DataFrame()

    aligned = pd.DataFrame(series_map)
    aligned = aligned.dropna(axis=0, thresh=max(2, int(len(series_map) * 0.35)))
    if aligned.empty:
        return pd.DataFrame()
    return aligned.corr().abs().fillna(0.0)


def code_similarity(sig_a: str, sig_b: str) -> float:
    set_a = set(str(sig_a).split("|"))
    set_b = set(str(sig_b).split("|"))
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


def max_selected_corr(
    row: pd.Series,
    selected: list[pd.Series],
    corr_matrix: pd.DataFrame,
) -> float:
    if not selected:
        return 0.0
    alpha_id = str(row["alpha_id"])
    vals: list[float] = []
    for sel in selected:
        sid = str(sel["alpha_id"])
        if (
            not corr_matrix.empty
            and alpha_id in corr_matrix.index
            and sid in corr_matrix.columns
        ):
            vals.append(float(corr_matrix.loc[alpha_id, sid]))
        else:
            vals.append(code_similarity(row.get("code_signature"), sel.get("code_signature")))
    return max(vals) if vals else 0.0


def greedy_select(
    df: pd.DataFrame,
    target_count: int,
    max_count: int,
    corr_matrix: pd.DataFrame,
    config: OsmosisConfig,
) -> pd.DataFrame:
    if df.empty or target_count <= 0:
        return pd.DataFrame(columns=df.columns)

    candidates = df.sort_values("base_quality_score", ascending=False).copy()
    selected: list[pd.Series] = []
    skipped: list[pd.Series] = []

    for _, row in candidates.iterrows():
        if len(selected) >= max_count:
            break
        max_corr = max_selected_corr(row, selected, corr_matrix)
        if max_corr <= config.max_pnl_corr:
            row = row.copy()
            row["diversity_penalty"] = max(0.0, max_corr - config.soft_pnl_corr)
            selected.append(row)
        else:
            skipped.append(row)
        if len(selected) >= target_count:
            break

    if len(selected) < target_count:
        for row in skipped:
            if len(selected) >= min(target_count, max_count):
                break
            row = row.copy()
            row["diversity_penalty"] = 0.25
            selected.append(row)

    if not selected:
        return pd.DataFrame(columns=df.columns)
    result = pd.DataFrame(selected)
    result["selection_rank"] = range(1, len(result) + 1)
    return result


def choose_type_counts(df: pd.DataFrame, config: OsmosisConfig) -> tuple[int, int]:
    regular_available = int((df["type"] == "REGULAR").sum())
    super_available = int((df["type"] == "SUPER").sum())
    if regular_available == 0:
        return 0, min(config.max_alpha_count, super_available)
    if super_available == 0:
        return min(config.max_alpha_count, regular_available), 0

    super_target = min(
        config.super_max_alpha_count,
        max(1, round(config.target_alpha_count * config.super_point_share)),
        super_available,
    )
    regular_target = min(
        config.max_alpha_count - super_target,
        max(config.min_alpha_count, config.target_alpha_count - super_target),
        regular_available,
    )

    total = regular_target + super_target
    if total < config.min_alpha_count:
        need = config.min_alpha_count - total
        can_add_regular = max(0, regular_available - regular_target)
        add_regular = min(need, can_add_regular)
        regular_target += add_regular
        need -= add_regular
        if need > 0:
            can_add_super = max(0, super_available - super_target)
            super_target += min(need, can_add_super, config.super_max_alpha_count - super_target)

    return regular_target, super_target


def select_portfolio(df: pd.DataFrame, config: OsmosisConfig) -> pd.DataFrame:
    if df.empty:
        return df

    regular_target, super_target = choose_type_counts(df, config)
    selected_chunks: list[pd.DataFrame] = []

    for alpha_type, target in (("REGULAR", regular_target), ("SUPER", super_target)):
        pool = df[df["type"] == alpha_type].copy()
        if pool.empty or target <= 0:
            continue
        max_count = config.max_alpha_count if alpha_type == "REGULAR" else config.super_max_alpha_count
        corr_matrix = build_corr_matrix(pool)
        selected_chunks.append(greedy_select(pool, target, max_count, corr_matrix, config))

    if not selected_chunks:
        return pd.DataFrame(columns=df.columns)

    selected = pd.concat(selected_chunks, ignore_index=True)
    selected["adjusted_quality"] = (
        selected["base_quality_score"].astype(float)
        * (1.0 - selected.get("diversity_penalty", 0).fillna(0.0).clip(0.0, 0.6))
    ).clip(0.01, 1.0)

    selected = selected.sort_values(
        ["type", "adjusted_quality"], ascending=[True, False]
    ).reset_index(drop=True)
    selected["selection_reason"] = "primary"
    return selected


def relaxed_fill_candidates(all_scored: pd.DataFrame, selected: pd.DataFrame, config: OsmosisConfig) -> pd.DataFrame:
    if all_scored.empty:
        return all_scored

    selected_ids = set(selected.get("alpha_id", pd.Series(dtype=str)).astype(str))
    pool = all_scored[~all_scored["alpha_id"].astype(str).isin(selected_ids)].copy()
    if pool.empty:
        return pool

    if "filter_reason" not in pool.columns:
        pool = add_filter_reasons(pool, config)

    target_region = clean_region(config.region)
    target_delay = clean_delay(config.delay)
    valid_scope = (pool["region"] == target_region) & (pool["delay"] == target_delay)
    valid_status = ~pool["status"].astype(str).str.upper().isin(["UNSUBMITTED", "IS_FAIL"])
    pool = pool[valid_scope & valid_status].copy()
    if pool.empty:
        return pool

    regular_relaxed = (
        (pool["type"] != "REGULAR")
        | (
            (pool["sharpe"].fillna(-999) >= config.relaxed_regular_min_sharpe)
            & (pool["fitness"].fillna(-999) >= config.relaxed_regular_min_fitness)
            & (
                pool["drawdown"].isna()
                | (pool["drawdown"] <= config.relaxed_regular_max_drawdown)
            )
        )
    )
    corr_relaxed = (
        (pool["self_corr"].isna() | (pool["self_corr"].abs() <= config.max_self_corr))
        & (pool["prod_corr"].isna() | (pool["prod_corr"].abs() <= config.max_prod_corr))
    )
    pool["relaxed_pass"] = regular_relaxed & corr_relaxed

    pool["fill_sort_score"] = pool["base_quality_score"].fillna(0.0)
    pool.loc[~pool["relaxed_pass"], "fill_sort_score"] *= 0.55
    pool.loc[pool["filter_reason"].astype(str).str.contains("high_self_corr|high_prod_corr"), "fill_sort_score"] *= 0.35
    pool.loc[pool["filter_reason"].astype(str).str.contains("high_drawdown"), "fill_sort_score"] *= 0.70

    return pool.sort_values(
        ["relaxed_pass", "fill_sort_score", "base_quality_score"],
        ascending=[False, False, False],
    )


def ensure_minimum_selection(selected: pd.DataFrame, all_scored: pd.DataFrame, config: OsmosisConfig) -> pd.DataFrame:
    if not config.fill_to_min_alpha_count or len(selected) >= config.min_alpha_count:
        return selected

    needed = min(config.min_alpha_count - len(selected), config.max_alpha_count - len(selected))
    if needed <= 0:
        return selected

    pool = relaxed_fill_candidates(all_scored, selected, config)
    if pool.empty:
        return selected

    selected_chunks = [selected.copy()]
    selected_super_count = int((selected["type"] == "SUPER").sum()) if not selected.empty else 0
    fillers: list[pd.Series] = []

    for _, row in pool.iterrows():
        if len(fillers) >= needed:
            break
        if row.get("type") == "SUPER" and selected_super_count >= config.super_max_alpha_count:
            continue
        row = row.copy()
        row["selection_reason"] = "filler"
        row["diversity_penalty"] = max(float(row.get("diversity_penalty", 0) or 0), 0.35)
        row["adjusted_quality"] = max(
            0.01,
            float(row.get("base_quality_score", 0.01) or 0.01) * config.filler_quality_discount,
        )
        fillers.append(row)
        if row.get("type") == "SUPER":
            selected_super_count += 1

    if len(fillers) < needed:
        used = set(str(row["alpha_id"]) for row in fillers)
        for _, row in pool.iterrows():
            if len(fillers) >= needed:
                break
            if str(row["alpha_id"]) in used:
                continue
            row = row.copy()
            row["selection_reason"] = "filler"
            row["diversity_penalty"] = max(float(row.get("diversity_penalty", 0) or 0), 0.45)
            row["adjusted_quality"] = max(
                0.01,
                float(row.get("base_quality_score", 0.01) or 0.01) * config.filler_quality_discount,
            )
            fillers.append(row)

    if not fillers:
        return selected

    filler_df = pd.DataFrame(fillers)
    selected_chunks.append(filler_df)
    out = pd.concat(selected_chunks, ignore_index=True)
    out = out.drop_duplicates(subset=["alpha_id"], keep="first")
    out = out.sort_values(
        ["selection_reason", "adjusted_quality"],
        ascending=[False, False],
    ).reset_index(drop=True)
    return out


def largest_remainder(weights: list[float], total: int) -> list[int]:
    if not weights:
        return []
    clean = [max(0.0, float(w)) for w in weights]
    s = sum(clean)
    if s <= 0:
        base = total // len(clean)
        result = [base] * len(clean)
        result[-1] += total - sum(result)
        return result

    quotas = [w / s * total for w in clean]
    floors = [int(math.floor(q)) for q in quotas]
    remain = total - sum(floors)
    order = sorted(
        range(len(quotas)),
        key=lambda i: (quotas[i] - floors[i], quotas[i]),
        reverse=True,
    )
    for i in range(remain):
        floors[order[i % len(order)]] += 1
    return floors


def allocate_with_caps(
    weights: list[float],
    total: int,
    *,
    min_points: int,
    max_points: int,
) -> list[int]:
    n = len(weights)
    if n == 0:
        return []
    if total <= 0:
        return [0] * n

    min_points = min(min_points, total // n)
    max_points = max(max_points, min_points)
    if max_points * n < total:
        max_points = math.ceil(total / n)

    allocated = [min_points] * n
    remaining_total = total - sum(allocated)
    if remaining_total <= 0:
        allocated[-1] += total - sum(allocated)
        return allocated

    capacity = [max_points - min_points] * n
    active = [i for i, cap in enumerate(capacity) if cap > 0]
    clean_weights = [max(0.0, float(w)) for w in weights]

    while active and remaining_total > 0:
        sub_weights = [clean_weights[i] for i in active]
        proposal = largest_remainder(sub_weights, remaining_total)
        changed = False
        next_active: list[int] = []
        overflow = 0

        for idx, extra in zip(active, proposal):
            give = min(extra, capacity[idx])
            allocated[idx] += give
            capacity[idx] -= give
            overflow += extra - give
            changed = changed or give > 0
            if capacity[idx] > 0:
                next_active.append(idx)

        new_remaining = total - sum(allocated)
        if not changed or new_remaining == remaining_total:
            break
        remaining_total = new_remaining + overflow
        remaining_total = total - sum(allocated)
        active = next_active

    diff = total - sum(allocated)
    if diff > 0:
        order = sorted(range(n), key=lambda i: clean_weights[i], reverse=True)
        for idx in order:
            add = min(diff, max_points - allocated[idx])
            if add > 0:
                allocated[idx] += add
                diff -= add
            if diff == 0:
                break
    elif diff < 0:
        order = sorted(range(n), key=lambda i: clean_weights[i])
        need = -diff
        for idx in order:
            take = min(need, allocated[idx] - min_points)
            if take > 0:
                allocated[idx] -= take
                need -= take
            if need == 0:
                break

    if sum(allocated) != total:
        allocated[-1] += total - sum(allocated)
    return allocated


def rank_decay_weights(n: int) -> list[float]:
    if n <= 0:
        return []
    if n == 1:
        return [1.0]
    return [math.exp(-i / max(3.0, n / 3.0)) for i in range(n)]


def add_points(selected: pd.DataFrame, config: OsmosisConfig) -> pd.DataFrame:
    if selected.empty:
        return selected

    out = selected.copy()
    counts = out["type"].value_counts().to_dict()
    has_regular = counts.get("REGULAR", 0) > 0
    has_super = counts.get("SUPER", 0) > 0

    if has_regular and has_super:
        super_budget = int(round(config.total_points * config.super_point_share))
        regular_budget = config.total_points - super_budget
        min_regular_budget = int(round(config.total_points * config.regular_min_point_share))
        if regular_budget < min_regular_budget:
            regular_budget = min_regular_budget
            super_budget = config.total_points - regular_budget
    elif has_super:
        regular_budget = 0
        super_budget = config.total_points
    else:
        regular_budget = config.total_points
        super_budget = 0

    out["osmosis_new"] = 0
    for alpha_type, budget, max_points in (
        ("REGULAR", regular_budget, config.regular_max_points_per_alpha),
        ("SUPER", super_budget, config.super_max_points_per_alpha),
    ):
        mask = out["type"] == alpha_type
        part = out[mask].sort_values("adjusted_quality", ascending=False)
        if part.empty or budget <= 0:
            continue

        q = part["adjusted_quality"].astype(float).tolist()
        rank_w = rank_decay_weights(len(part))
        weights = [0.70 * q_i + 0.30 * r_i for q_i, r_i in zip(q, rank_w)]
        points = allocate_with_caps(
            weights,
            budget,
            min_points=config.min_points_per_alpha,
            max_points=max_points,
        )
        out.loc[part.index, "osmosis_new"] = points

    total = int(out["osmosis_new"].sum())
    if total != config.total_points and not out.empty:
        best_idx = out["adjusted_quality"].astype(float).idxmax()
        out.loc[best_idx, "osmosis_new"] += config.total_points - total

    return out.sort_values("osmosis_new", ascending=False).reset_index(drop=True)


def _to_json_value(value: Any) -> Any:
    """把 DataFrame 单元格值转为 JSON 可序列化。"""
    if value is None:
        return None
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return round(value, 6)
    if isinstance(value, int):
        return value
    if isinstance(value, (pd.Timestamp, )):
        return value.isoformat()
    return str(value)


def _serialize_row(row: pd.Series) -> dict[str, Any]:
    keep = [
        "alpha_id", "type", "selection_reason", "filter_reason", "osmosis_new",
        "metric_source", "adjusted_quality", "base_quality_score", "year_score",
        "sharpe", "fitness", "returns", "margin", "turnover", "drawdown",
        "os_sharpe", "os_fitness", "os_returns", "os_margin", "os_turnover", "os_drawdown",
        "is_sharpe", "is_fitness", "is_returns", "is_margin", "is_turnover", "is_drawdown",
        "self_corr", "prod_corr", "universe", "neutralization", "decay", "dateSubmitted",
    ]
    out: dict[str, Any] = {}
    for col in keep:
        if col in row.index:
            out[col] = _to_json_value(row[col])
    return out


def build_allocation_plan(
    client: Any,
    region: str,
    delay: int,
    *,
    config: OsmosisConfig | None = None,
    progress_cb: Callable[[str, int, int, int, int], None] | None = None,
) -> dict[str, Any]:
    """生成 Osmosis 分配方案（预览/写入前调用）。"""
    if config is None:
        config = OsmosisConfig(region=region, delay=delay)

    scope = scope_name(region, delay)
    records = client.list_scope_alphas(region, delay, max_scan=config.max_alpha_scan)
    if not records:
        return {"ok": False, "error": f"{scope} 没有返回已提交 alpha", "scope": scope}

    all_df = pd.DataFrame([flatten_alpha(r, region, delay) for r in records])
    all_df = add_base_scores(all_df)
    eligible = apply_hard_filters(all_df, config)

    if progress_cb:
        progress_cb("candidates", len(all_df), len(eligible), 0, 0)

    selected = select_portfolio(eligible, config)
    if config.fill_to_min_alpha_count:
        selected = ensure_minimum_selection(selected, all_df, config)
    selected = add_points(selected, config)

    selected_list = [_serialize_row(row) for _, row in selected.iterrows()]

    regular_count = int((selected["type"] == "REGULAR").sum())
    super_count = int((selected["type"] == "SUPER").sum())

    return {
        "ok": True,
        "scope": scope,
        "region": clean_region(region),
        "delay": clean_delay(delay),
        "total_points": config.total_points,
        "candidate_count": len(all_df),
        "eligible_count": len(eligible),
        "selected_count": len(selected),
        "regular_count": regular_count,
        "super_count": super_count,
        "total_assigned": int(selected["osmosis_new"].sum()) if not selected.empty else 0,
        "selected": selected_list,
    }
