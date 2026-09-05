"""二阶表达式工厂：group 算子 × densify(group)。

修复原 machine_lib.group_factory 的：
- P1：glb_group_13 变量被二次赋值覆盖（第一份作废）→ 已合并去重
- P1：usa_group_13 重复元素 → 已去重
- P1：group_vector / vectors=["cap"] 死代码 → 不实现

新设计：
- SecondOrderConfig：输入一阶表达式列表 + group 算子 + 区域
- build_second_order_expressions(cfg) -> [(expr, decay)]
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..settings_registry import REGIONS

# group 算子（与一阶 ts 算子组合成二阶）
GROUP_OPS = ("group_neutralize", "group_rank", "group_zscore")

# 动态分组（所有区域通用）
DYNAMIC_GROUPS = [
    "market", "sector", "industry", "subindustry",
    "bucket(rank(cap), range='0.1, 1, 0.1')",
    "bucket(rank(returns),range='0.1, 1, 0.1')",
    "bucket(group_rank(cap, sector),range='0.1, 1, 0.1')",
    "bucket(group_rank(returns, sector),range='0.1, 1, 0.1')",
    "bucket(rank(ts_std_dev(returns,20)),range = '0.1, 1, 0.1')",
    "bucket(rank(close*volume),range = '0.1, 1, 0.1')",
]

# 区域专属分组清单（原 machine_lib 已去重合并）
REGION_GROUPS: dict[str, list[str]] = {
    "CHN": [
        "pv13_h_min2_sector", "pv13_di_6l", "pv13_rcsed_6l", "pv13_di_5l", "pv13_di_4l",
        "pv13_di_3l", "pv13_di_2l", "pv13_di_1l", "pv13_parent", "pv13_level",
        "sta1_top3000c30", "sta1_top3000c20", "sta1_top3000c10", "sta1_top3000c2", "sta1_top3000c5",
        "sta2_top3000_fact4_c10", "sta2_top2000_fact4_c50", "sta2_top3000_fact3_c20",
    ],
    "HKG": [
        "pv13_10_f3_g2_minvol_1m_sector", "pv13_10_minvol_1m_sector", "pv13_20_minvol_1m_sector",
        "pv13_2_minvol_1m_sector", "pv13_5_minvol_1m_sector", "pv13_1l_scibr", "pv13_3l_scibr",
        "pv13_2l_scibr", "pv13_4l_scibr", "pv13_5l_scibr",
        "sta1_allc50", "sta1_allc5", "sta1_allxjp_513_c20", "sta1_top2000xjp_513_c5",
        "sta2_all_xjp_513_all_fact4_c10", "sta2_top2000_xjp_513_top2000_fact3_c10",
        "sta2_allfactor_xjp_513_13", "sta2_top2000_xjp_513_top2000_fact3_c20",
    ],
    "TWN": [
        "pv13_2_minvol_1m_sector", "pv13_20_minvol_1m_sector", "pv13_10_minvol_1m_sector",
        "pv13_5_minvol_1m_sector", "pv13_10_f3_g2_minvol_1m_sector", "pv13_5_f3_g2_minvol_1m_sector",
        "pv13_2_f4_g3_minvol_1m_sector",
        "sta1_allc50", "sta1_allxjp_513_c50", "sta1_allxjp_513_c20", "sta1_allxjp_513_c2",
        "sta1_allc20", "sta1_allxjp_513_c5", "sta1_allxjp_513_c10", "sta1_allc2", "sta1_allc5",
        "sta2_allfactor_xjp_513_0", "sta2_all_xjp_513_all_fact3_c20",
        "sta2_all_xjp_513_all_fact4_c20", "sta2_all_xjp_513_all_fact4_c50",
    ],
    "USA": [
        "pv13_h_min2_3000_sector", "pv13_r2_min20_3000_sector", "pv13_r2_min2_3000_sector",
        "pv13_h_min2_focused_pureplay_3000_sector",
        "sta1_top3000c50", "sta1_allc20", "sta1_allc10", "sta1_top3000c20", "sta1_allc5",
        "sta2_top3000_fact3_c50", "sta2_top3000_fact4_c20", "sta2_top3000_fact4_c10",
        "mdl10_group_name",
    ],
    "ASI": [
        "pv13_20_minvol_1m_sector", "pv13_5_f3_g2_minvol_1m_sector", "pv13_10_f3_g2_minvol_1m_sector",
        "pv13_2_f4_g3_minvol_1m_sector", "pv13_10_minvol_1m_sector", "pv13_5_minvol_1m_sector",
        "sta1_allc50", "sta1_allc10", "sta1_minvol1mc50", "sta1_minvol1mc20",
        "sta1_minvol1m_normc20", "sta1_minvol1m_normc50",
    ],
    "JPN": [
        "pv13_2_minvol_1m_sector", "pv13_2_f4_g3_minvol_1m_sector", "pv13_10_minvol_1m_sector",
        "pv13_10_f3_g2_minvol_1m_sector", "pv13_all_delay_1_parent", "pv13_all_delay_1_level",
        "sta1_alljpn_513_c5", "sta1_alljpn_513_c50", "sta1_alljpn_513_c2", "sta1_alljpn_513_c20",
        "sta2_top2000_jpn_513_top2000_fact3_c20", "sta2_all_jpn_513_all_fact1_c5",
        "sta2_allfactor_jpn_513_9", "sta2_all_jpn_513_all_fact1_c10",
    ],
    "KOR": [
        "pv13_10_f3_g2_minvol_1m_sector", "pv13_5_minvol_1m_sector", "pv13_5_f3_g2_minvol_1m_sector",
        "pv13_2_minvol_1m_sector", "pv13_20_minvol_1m_sector", "pv13_2_f4_g3_minvol_1m_sector",
        "sta1_allc20", "sta1_allc50", "sta1_allc2", "sta1_allc10", "sta1_minvol1mc50",
        "sta1_allxjp_513_c10", "sta1_top2000xjp_513_c50",
        "sta2_all_xjp_513_all_fact1_c50", "sta2_top2000_xjp_513_top2000_fact2_c50",
        "sta2_all_xjp_513_all_fact4_c50", "sta2_all_xjp_513_all_fact4_c5",
    ],
    "EUR": [
        "pv13_5_sector", "pv13_2_sector", "pv13_v3_3l_scibr", "pv13_v3_2l_scibr", "pv13_2l_scibr",
        "pv13_52_sector", "pv13_v3_6l_scibr", "pv13_v3_4l_scibr", "pv13_v3_1l_scibr",
        "sta1_allc10", "sta1_allc2", "sta1_top1200c2", "sta1_allc20", "sta1_top1200c10",
        "sta2_top1200_fact3_c50", "sta2_top1200_fact3_c20", "sta2_top1200_fact4_c50",
    ],
    "GLB": [
        # 原 machine_lib 第一份定义（已作废，未迁移）；使用第二份（有效版）
        "pv13_2_sector", "pv13_10_sector", "pv13_3l_scibr", "pv13_2l_scibr", "pv13_1l_scibr",
        "pv13_52_minvol_1m_all_delay_1_sector", "pv13_52_minvol_1m_sector",
        "sta1_allc20", "sta1_allc10", "sta1_allc50", "sta1_allc5",
        "sta2_all_fact4_c50", "sta2_all_fact4_c20", "sta2_all_fact3_c20", "sta2_all_fact4_c10",
    ],
    "AMR": [
        "pv13_4l_scibr", "pv13_1l_scibr", "pv13_hierarchy_min51_f1_sector",
        "pv13_hierarchy_min2_600_sector", "pv13_r2_min2_sector", "pv13_h_min20_600_sector",
    ],
}


@dataclass
class SecondOrderConfig:
    """二阶工厂配置。"""

    # 一阶表达式列表（剪枝后的 [(expression, decay)]）
    first_order: list[tuple[str, int]] = field(default_factory=list)

    # group 算子（勾选）
    group_ops: tuple[str, ...] = ()

    # 区域（决定用哪组区域专属分组）
    region: str = "USA"

    # 是否包含动态分组（market/sector/industry 等 10 个通用分组）
    include_dynamic: bool = True

    # 是否包含区域专属分组
    include_region_groups: bool = True

    def validate(self) -> None:
        if not self.first_order:
            raise ValueError("first_order 不能为空（请先选择一阶数据源）")
        bad = set(self.group_ops) - set(GROUP_OPS)
        if bad:
            raise ValueError(f"未知 group 算子：{bad}")
        if not self.group_ops:
            raise ValueError("至少选择一个 group 算子")
        # region 合法性以平台 REGIONS 为准（IND/MEA/DEU/GBR/EDU 都是合法区域）；
        # REGION_GROUPS 只是"专属分组增强表"，缺失的区域由 groups_for_region
        # 返回动态分组（market/sector/industry 等 10 个通用分组）兜底，不拦截
        if self.region not in REGIONS:
            raise ValueError(
                f"未知 region：{self.region}（可选：{', '.join(REGIONS)}）"
            )

    def groups_for_region(self) -> list[str]:
        groups = []
        if self.include_dynamic:
            groups += DYNAMIC_GROUPS
        if self.include_region_groups:
            groups += REGION_GROUPS.get(self.region, [])
        # 去重保序
        seen = set()
        out = []
        for g in groups:
            if g not in seen:
                seen.add(g)
                out.append(g)
        return out


def build_second_order_expressions(
    cfg: SecondOrderConfig,
) -> list[tuple[str, int]]:
    """生成二阶表达式：group_op(fo_expr, densify(group))。

    输出顺序：group 算子 × 分组 × 一阶表达式。
    """
    cfg.validate()
    groups = cfg.groups_for_region()
    if not groups:
        raise ValueError(f"区域 {cfg.region} 没有可用分组")

    out: list[tuple[str, int]] = []
    for op in cfg.group_ops:
        for group in groups:
            for expr, decay in cfg.first_order:
                out.append((f"{op}({expr},densify({group}))", decay))
    return out


def estimate_count(cfg: SecondOrderConfig) -> int:
    return len(cfg.group_ops) * len(cfg.groups_for_region()) * len(cfg.first_order)