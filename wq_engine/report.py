"""报告导出：Markdown 报告 + CSV。

从 storage 的 alphas 表 + 推荐评分结果生成可交付报告。
输出到 outputs/ 目录，文件名带时间戳。
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from .scoring import ScoredAlpha


def _fmt(v, nd=3) -> str:
    if v is None:
        return "-"
    try:
        return f"{v:.{nd}f}"
    except (TypeError, ValueError):
        return str(v)


def export_alphas_csv(alphas: list[dict], path: str | Path) -> int:
    """导出 alpha 列表到 CSV（含 returns / check 结果列，与高级表格对齐）。"""
    if not alphas:
        return 0
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    cols = ["alpha_id", "expression", "sharpe", "returns", "fitness", "turnover",
            "margin", "long_count", "short_count", "decay", "region",
            "neutralization", "date_created", "check_pc", "check_failed",
            "check_status"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        for a in alphas:
            writer.writerow(a)
    return len(alphas)


def export_recommendation_report(
    scored: list[ScoredAlpha],
    alphas_map: dict[str, dict],
    pnl_cache: dict[str, list[dict]],
    out_path: str | Path,
    *,
    title: str = "AlphaMachine 推荐报告",
) -> Path:
    """生成推荐候选的 Markdown 报告。"""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")
    lines.append(f"推荐候选：**{len(scored)}** 条")
    lines.append("")

    # 统计
    vetoed = [s for s in scored if s.vetoed]
    lines.append("## 概览")
    lines.append("")
    lines.append(f"- 通过候选：{len(scored) - len(vetoed)} 条")
    lines.append(f"- 被否决：{len(vetoed)} 条")
    lines.append("")

    lines.append("## 推荐排序（按分数）")
    lines.append("")
    lines.append("| 排名 | alpha_id | 分数 | sharpe | fitness | turnover | margin | PROD_CORR | 状态 |")
    lines.append("|------|----------|------|--------|---------|----------|--------|-----------|------|")
    for rank, s in enumerate(scored, 1):
        status = "✅ 可提交" if not s.vetoed else "❌ 否决"
        pc = _fmt(s.prod_corr, 3) if s.prod_corr is not None else "-"
        lines.append(
            f"| {rank} | `{s.alpha_id}` | {s.score:.2f} | {_fmt(s.sharpe)} | "
            f"{_fmt(s.fitness)} | {_fmt(s.turnover)} | {_fmt(s.margin)} | {pc} | {status} |"
        )
    lines.append("")

    # 否决原因
    veto_reasons = [s for s in scored if s.vetoed and s.veto_reasons]
    if veto_reasons:
        lines.append("## 否决明细")
        lines.append("")
        for s in veto_reasons:
            lines.append(f"- `{s.alpha_id}`：{'; '.join(s.veto_reasons)}")
        lines.append("")

    # Top 候选 PnL
    top = [s for s in scored if not s.vetoed][:5]
    if top:
        lines.append("## Top 候选 PnL 摘要")
        lines.append("")
        for s in top:
            pnl = pnl_cache.get(s.alpha_id)
            if pnl:
                total = sum(p.get("pnl", 0) or 0 for p in pnl)
                lines.append(
                    f"- `{s.alpha_id}`：{len(pnl)} 天，累计 PnL {total:+.4f}"
                )
            else:
                lines.append(f"- `{s.alpha_id}`：未拉取 PnL")
        lines.append("")

    # 表达式清单
    lines.append("## 候选表达式")
    lines.append("")
    for s in scored:
        if not s.vetoed:
            lines.append(f"### `{s.alpha_id}`")
            lines.append("")
            lines.append(f"```text")
            lines.append(s.expression)
            lines.append(f"```")
            lines.append("")
            lines.append(f"- sharpe={_fmt(s.sharpe)} fitness={_fmt(s.fitness)} "
                         f"turnover={_fmt(s.turnover)} margin={_fmt(s.margin)}")
            lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path