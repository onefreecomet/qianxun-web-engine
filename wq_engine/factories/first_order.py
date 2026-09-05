"""一阶表达式工厂：横截面算子 + 时序算子 × 窗口。

设计：
- FirstOrderConfig dataclass：UI 配置的强类型表达
- build_first_order_expressions(config) -> [(expr, decay)]：纯函数，无副作用
- 死代码清理：原 7 个 ops_set 不存在的分支（ts_percentage/ts_decay_exp_window/ts_moment/ts_entropy/vector/inst_tvr/signed_power）全部不实现
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product

# 横截面算子（无 ts_ 前缀）
CROSS_SECTION_OPS = ("reverse", "inverse", "rank", "zscore", "quantile", "normalize")

# 时序算子（ts_ 前缀），每个都跟一个回看窗口
TS_OPS = (
    "ts_rank", "ts_zscore", "ts_delta", "ts_sum", "ts_delay",
    "ts_std_dev", "ts_mean", "ts_arg_min", "ts_arg_max", "ts_scale", "ts_quantile",
)

# 默认窗口（与原 machine_lib.ts_factory 一致）
DEFAULT_WINDOWS = (5, 22, 66, 120, 240)


@dataclass
class FirstOrderConfig:
    """一阶工厂配置（与 UI 一阶 Tab 一一对应）。"""

    # 数据字段表达式列表（已 winsorize/ts_backfill 包装）
    field_exprs: list[str] = field(default_factory=list)

    # 横截面算子（勾选）
    cross_section_ops: tuple[str, ...] = ()

    # 时序算子（勾选）+ 窗口列表
    ts_ops: tuple[str, ...] = ()
    windows: tuple[int, ...] = DEFAULT_WINDOWS

    # 是否包含裸字段（reverse op 默认行为）
    include_raw_field: bool = True

    # 初始 decay（每个表达式都配 decay=N）
    initial_decay: int = 1

    def validate(self) -> None:
        if not self.field_exprs:
            raise ValueError("field_exprs 不能为空")
        if any(w <= 0 for w in self.windows):
            raise ValueError(f"windows 必须全部为正整数：{self.windows}")
        if self.initial_decay <= 0:
            raise ValueError(f"initial_decay 必须为正整数：{self.initial_decay}")
        # 自定义算子放行：只校验"至少一个算子来自内置集合时拼写正确"，
        # 未知算子（自定义）不做校验（平台提交时会反馈错误）
        bad = set(self.cross_section_ops) - set(CROSS_SECTION_OPS)
        if bad and set(self.cross_section_ops) == bad:
            pass  # 全部是自定义算子，放行
        elif bad:
            pass  # 混用：未知算子按自定义放行（与全自定义一致，非法表达式 sim 失败兜底）
        bad_ts = set(self.ts_ops) - set(TS_OPS)
        if bad_ts and set(self.ts_ops) == bad_ts:
            pass  # 全部是自定义算子，放行
        elif bad_ts:
            pass  # 混用：未知算子按自定义放行（与全自定义一致，非法表达式 sim 失败兜底）
        # 算子全空且不带裸字段：build 会静默返回空列表，直接拦截
        if not self.cross_section_ops and not self.ts_ops and not self.include_raw_field:
            raise ValueError("至少选择一种算子（横截面/时序），或开启裸字段")
        if self.ts_ops and not self.windows:
            raise ValueError("启用时序算子时必须提供窗口列表")


def build_first_order_expressions(
    cfg: FirstOrderConfig,
) -> list[tuple[str, int]]:
    """按配置生成 [(expression, decay)] 列表。

    输出顺序：
      1. raw_field（每个字段一次，仅当 include_raw_field）
      2. cross_section_op(field) （每个算子 × 每个字段）
      3. ts_op(field, window) （每个算子 × 每个窗口 × 每个字段）
    """
    cfg.validate()
    out: list[tuple[str, int]] = []

    # 1. raw field
    if cfg.include_raw_field:
        for field_expr in cfg.field_exprs:
            out.append((field_expr, cfg.initial_decay))

    # 2. cross-section ops
    for op in cfg.cross_section_ops:
        for field_expr in cfg.field_exprs:
            out.append((f"{op}({field_expr})", cfg.initial_decay))

    # 3. ts ops × windows × fields
    for op, window in product(cfg.ts_ops, cfg.windows):
        for field_expr in cfg.field_exprs:
            out.append(
                (f"{op}({field_expr}, {window})", cfg.initial_decay)
            )

    return out


def estimate_count(cfg: FirstOrderConfig) -> int:
    """预估一阶表达式总数（用于 UI 显示）。"""
    n_field = len(cfg.field_exprs)
    raw = n_field if cfg.include_raw_field else 0
    cross = len(cfg.cross_section_ops) * n_field
    ts = len(cfg.ts_ops) * len(cfg.windows) * n_field
    return raw + cross + ts