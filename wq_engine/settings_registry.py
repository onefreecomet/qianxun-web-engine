"""Region → Universe / Neutralization 联动注册表。

数据来源：BRAIN 平台官方 get_platform_setting_options（2026-08-07 实测抓取），
AMR 为用户提供补充（平台选项里无 AMR）。
平台无 TWN/JPN/EDU 选项数据，用通用默认集兜底。
"""

from __future__ import annotations

# 平台 regions_by_type（EQUITY），加上用户补充的 AMR / 旧版 TWN JPN EDU
REGIONS = (
    "USA", "GLB", "EUR", "ASI", "CHN", "KOR", "HKG",
    "IND", "MEA", "DEU", "GBR", "AMR", "TWN", "JPN", "EDU",
)

_GENERIC_UNIVERSES = ("TOP3000", "TOP2000", "TOP1000", "TOP500", "TOP200")

# 平台权威 Universe（用户提供清单与平台一致，平台另有 KOR/HKG 一并收录）
REGION_UNIVERSES: dict[str, tuple[str, ...]] = {
    "USA": ("TOP3000", "TOP2000", "TOP1000", "TOP500", "TOP200",
            "ILLIQUID_MINVOL1M", "TOPSP500"),
    "GLB": ("TOP3000", "MINVOL1M", "MINVOL10M", "TOPDIV3000"),
    "EUR": ("TOP2500", "TOP1200", "TOP800", "TOP400",
            "ILLIQUID_MINVOL1M", "TOPCS1600"),
    "ASI": ("MINVOL1M", "MINVOL10M", "ILLIQUID_MINVOL1M", "TOP500"),
    "CHN": ("TOP2000U",),
    "KOR": ("TOP600",),
    "HKG": ("TOP800", "TOP500"),
    "IND": ("TOP500",),
    "MEA": ("TOP400", "TOP300"),
    "DEU": ("TOP500",),
    "GBR": ("TOP700",),
    "AMR": ("TOP600",),          # 用户提供（平台无 AMR 选项）
    "TWN": _GENERIC_UNIVERSES,   # 平台无数据，兜底
    "JPN": _GENERIC_UNIVERSES,
    "EDU": _GENERIC_UNIVERSES,
}

# 平台权威 Neutralization（全大写；注意平台没有 RAM，用户记忆中的 RAM 未收录）
_NEUT_STD = (
    "NONE", "REVERSION_AND_MOMENTUM", "STATISTICAL", "CROWDING",
    "FAST", "SLOW", "MARKET", "SECTOR", "INDUSTRY", "SUBINDUSTRY",
    "SLOW_AND_FAST",
)
_NEUT_STD_COUNTRY = _NEUT_STD + ("COUNTRY",)
_NEUT_MEA = ("NONE", "MARKET", "SECTOR", "INDUSTRY", "SUBINDUSTRY", "COUNTRY")
_NEUT_BASIC = ("NONE", "MARKET", "SECTOR", "INDUSTRY", "SUBINDUSTRY")

REGION_NEUTRALIZATIONS: dict[str, tuple[str, ...]] = {
    "USA": _NEUT_STD,
    "GLB": _NEUT_STD_COUNTRY,
    "EUR": _NEUT_STD_COUNTRY,
    "ASI": _NEUT_STD_COUNTRY,
    "CHN": _NEUT_STD,
    "KOR": _NEUT_STD,
    "HKG": _NEUT_STD,
    "IND": _NEUT_STD,
    "MEA": _NEUT_MEA,
    "DEU": _NEUT_STD,
    "GBR": _NEUT_STD,
    "AMR": _NEUT_BASIC,       # 用户提供（平台无 AMR）
    "TWN": _NEUT_STD,         # 平台无数据，兜底
    "JPN": _NEUT_STD,
    "EDU": _NEUT_STD,
}

# 默认 region（一阶打开时的初始值）
DEFAULT_REGION = "USA"


def universes_for(region: str) -> tuple[str, ...]:
    return REGION_UNIVERSES.get(region, _GENERIC_UNIVERSES)


def neutralizations_for(region: str) -> tuple[str, ...]:
    return REGION_NEUTRALIZATIONS.get(region, _NEUT_BASIC)


def default_universe(region: str) -> str:
    """每个 region 的默认 universe（列表第一个）。"""
    return universes_for(region)[0]


def default_neutralization(region: str) -> str:
    """默认中性化：优先 SUBINDUSTRY，没有则 MARKET，再没有则 NONE。"""
    neuts = neutralizations_for(region)
    for prefer in ("SUBINDUSTRY", "INDUSTRY", "SECTOR", "MARKET", "NONE"):
        if prefer in neuts:
            return prefer
    return neuts[0] if neuts else "NONE"