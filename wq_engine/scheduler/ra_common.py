# -*- coding: utf-8 -*-
"""RA（Region Agnostic / ARC2026）**原生支持**的公共小件（v83，260923）。

工程裁定（冰神质询「必须走旁路吗」后复核）：
- **RA 内建进 BatchScheduler**：payload 有 regular 字段，结构上原生兼容；
  收益 = 记账 / slots 可见 / watch_batch / 断点续跑 / 重试 / 事件全部复用 runner
  既有机制，不再需要并行通道、resume 守卫与 reaper；
- **SUPER 维持旁路**（super_channel）：payload 无 regular（selection/combo 顶层），
  simulations 记录层按表达式建模放不下它，属结构性原因。

REGULAR 在 region != ALL 时逐字节走老路：本模块全部为条件分支依赖的纯函数，
入口（web/MCP）先做幂等修正保证「存的 settings = 发的 settings」，
runner 内再兜底修正一次（防 GUI/CLI 等未修正入口）。
"""

from __future__ import annotations

import json
import logging

log = logging.getLogger("qianxun.ra")

#: RA 专属 universe 三档（其它值一律回落 LARGE）
RA_UNIVERSES = ("LARGE", "MEDIUM", "SMALL")

#: RA 轮询预算（秒）：1 条 RA = 1 Parent + 最多 4 Child = 5 个平台并发槽，
#: 排队常态 >40 分钟（ARC skill §9.5 实测），默认 1800s 会假超时。
RA_POLL_SECONDS = 4200.0


def is_ra_settings(settings: dict | None) -> bool:
    """settings 层面判定 RA：region == ALL。"""
    return str((settings or {}).get("region", "")).upper() == "ALL"


def is_ra_batch(expressions: list, settings: dict) -> bool:
    """批次层判定 RA：region == ALL，或元素显式标 REGION_AGNOSTIC / region_agnostic。

    （与 260921 旁路版判定语义一致，quick 拦截与入口修正共用。）
    """
    if is_ra_settings(settings):
        return True
    return any(
        isinstance(e, dict) and (
            str(e.get("type", "")).upper() == "REGION_AGNOSTIC" or e.get("region_agnostic")
        )
        for e in expressions
    )


def ra_settings(settings: dict | None) -> dict:
    """把 settings 幂等修正为 RA 合法组合：region=ALL / delay=1 /
    universe∈{LARGE,MEDIUM,SMALL}（非法回落 LARGE）/ ALL 下 COUNTRY→STATISTICAL。

    只碰这四个键，其余（decay/truncation/maxTrade…）原样保留。
    """
    st2 = dict(settings or {})
    st2["region"] = "ALL"
    st2["delay"] = 1
    if str(st2.get("universe", "")).upper() not in RA_UNIVERSES:
        st2["universe"] = "LARGE"
    else:
        st2["universe"] = str(st2["universe"]).upper()
    if str(st2.get("neutralization", "")).upper() == "COUNTRY":
        st2["neutralization"] = "STATISTICAL"  # ALL 下平台无 COUNTRY 选项
    return st2


def row_is_ra(row: dict) -> bool:
    """simulations 行级判定（容错解析 settings_json；损坏行按非 RA 处理）。"""
    try:
        s = json.loads(row.get("settings_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        return False
    return is_ra_settings(s)


def poll_budget_for(is_ra: bool, default: float) -> float:
    """轮询预算：RA 用 max(默认, RA_POLL_SECONDS)，REGULAR 用默认（逐字节不变）。"""
    return max(float(default), RA_POLL_SECONDS) if is_ra else float(default)


def build_sim_payload(rec: dict, is_ra: bool) -> dict:
    """构造单条平台 payload（type 推导 + decay 覆盖 + RA 幂等修正兜底）。

    REGULAR 分支与 260923 前的内联构建逐字段一致（type=REGULAR、不修正）。
    """
    sim_settings = dict(rec.get("settings") or {})
    sim_settings["decay"] = rec.get("decay", sim_settings.get("decay", 1))
    if is_ra:
        sim_settings = ra_settings(sim_settings)  # dict 拷贝，decay 保留
    return {
        "type": "REGION_AGNOSTIC" if is_ra else "REGULAR",
        "settings": sim_settings,
        "regular": rec["expression"],
    }


def group_pending_rows(all_pending: list[dict], batch_size: int) -> list[list]:
    """把 pending 行切成提交批次：RA 行**独占成批**（单条 POST，multi 数组对
    REGION_AGNOSTIC 未验证，社区/原旁路均单条），其余按 batch_size 顺次切。

    单遍缓冲实现：**不含 RA 行时与旧切法 `all_pending[i:i+batch_size]` 逐位一致**
    （黄金回归：REGULAR 行为零漂移）；含 RA 时保持整体 id 相对顺序。
    """
    batches: list[list] = []
    buf: list[dict] = []
    for row in all_pending:
        if row_is_ra(row):
            if buf:
                batches.append(buf)
                buf = []
            batches.append([row])
        else:
            buf.append(row)
            if len(buf) >= batch_size:
                batches.append(buf)
                buf = []
    if buf:
        batches.append(buf)
    return batches


def sim_row_is_ra(sim_row: dict) -> bool:
    """回填循环用：completed sim 行是否 RA（同 row_is_ra，名字按调用方语义区分）。"""
    return row_is_ra(sim_row)


def ra_backfill_one(st, client, sim_row: dict, batch_no: str) -> dict | None:
    """RA 回填单条：Parent（sim.alpha_id）无指标不入库 → 逐个采集 **Child** 入库。

    Child 才有 sharpe/fitness 等指标（ARC skill §0/§4）；补写 type='RA'、
    expr_key（供去重与后续工具链）、expression 兜底用 Parent 的表达式。
    返回 {"expression", "parent", "children"} 供 MCP 组装 parents 响应；异常返回 None。
    """
    parent = sim_row.get("alpha_id")
    if not parent:
        return None
    expr = sim_row.get("expression") or ""
    expr_key = sim_row.get("expr_key") or ""
    try:
        detail = client.get_alpha_details(parent) or {}
    except Exception as e:  # noqa: BLE001
        log.warning("RA Parent 详情失败（%s）：%s", parent, e)
        return None
    kids = [k for k in (detail.get("children") or []) if isinstance(k, str)]
    kid_ids: list[str] = []
    for kid in kids:
        try:
            kd = client.get_alpha_details(kid)
            alpha = client.extract_alpha_metrics(kd)
            if alpha.get("alpha_id"):
                st.upsert_alpha(alpha, batch_no=batch_no)
                st.mark_ra_alpha(alpha["alpha_id"], expression=expr,
                                 expr_key=expr_key)
                kid_ids.append(alpha["alpha_id"])
        except Exception as e:  # noqa: BLE001
            log.warning("RA Child 回填失败（%s）：%s", kid, e)
    return {"expression": expr, "parent": parent, "children": kid_ids}
