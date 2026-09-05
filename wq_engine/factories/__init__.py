"""WQ BRAIN 表达式工厂。

- 一阶：FirstOrderConfig / build_first_order_expressions
- 二阶：SecondOrderConfig / build_second_order_expressions（group 算子，区域分组）
- 三阶：ThirdOrderConfig / build_third_order_expressions（trade_when，区域事件生效）
- 字段预处理：process_datafields
- 剪枝：prune_expressions
"""

from .field_prep import process_datafields
from .first_order import FirstOrderConfig, build_first_order_expressions, estimate_count
from .prune import prune_expressions
from .second_order import (
    GROUP_OPS,
    SecondOrderConfig,
    build_second_order_expressions,
    estimate_count as estimate_second_count,
)
from .third_order import (
    ThirdOrderConfig,
    build_third_order_expressions,
    estimate_count as estimate_third_count,
)

__all__ = [
    "FirstOrderConfig",
    "build_first_order_expressions",
    "estimate_count",
    "SecondOrderConfig",
    "build_second_order_expressions",
    "estimate_second_count",
    "GROUP_OPS",
    "ThirdOrderConfig",
    "build_third_order_expressions",
    "estimate_third_count",
    "process_datafields",
    "prune_expressions",
]