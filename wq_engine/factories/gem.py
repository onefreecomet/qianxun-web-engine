"""GEM 工厂：多数据字段（Gem）配对 + 六类构造 + 一阶展开。

设计（对齐教程「多数据字段 (GEM)」）：
- 方法 1：按字段命名词根语义配对（call-put / pos-neg / act-est / buy-sell ...）
- 六类构造方式：比率 / 差值 / 乘积 / 加总 / 变化率 / 事件驱动
- 展开复用 first_order.build_first_order_expressions（字段 × 横截面 × 时序 × 窗口）
- 输出与 runner 兼容：[(expression, decay)]，settings 由 UI/调用方附加

典型链路：
    pairs = pair_fields(fields_meta)            # 词根配对
    gems  = build_gem_expressions(pairs, ...)   # 六类构造
    exprs = expand_gems(gems, cfg)              # 工厂展开
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .first_order import (
    CROSS_SECTION_OPS,
    DEFAULT_WINDOWS,
    FirstOrderConfig,
    TS_OPS,
    build_first_order_expressions,
)

# ---------------------------------------------------------------- 词根对立表

# 完整词根对立（命中即视为语义配对）
PAIRS: frozenset[tuple[str, str]] = frozenset({
    ("call", "put"),
    ("positive", "negative"),
    ("pos", "neg"),
    ("actual", "estimated"),
    ("act", "est"),
    ("actual", "expected"),
    ("buy", "sell"),
    ("bid", "ask"),
    ("long", "short"),
    ("high", "low"),
    ("in", "out"),
    ("up", "down"),
    ("up", "dn"),          # down 缩写（analyst15 实测）
    ("consensus", "estimate"),
    ("consensus", "estimates"),
    ("cons", "est"),
    ("acquire", "disposal"),
    ("acquire", "dispose"),
    ("asset", "liability"),
    ("revenue", "expense"),
    ("receivable", "payable"),
    ("current", "noncurrent"),
    ("shortterm", "longterm"),
    ("short_term", "long_term"),
    ("inflow", "outflow"),
    ("credit", "debit"),
    ("profit", "loss"),
    ("gain", "loss"),
    ("borrow", "lend"),
    ("issuer", "holder"),
    ("buying", "selling"),
    ("purchase", "sale"),
    ("bullish", "bearish"),
    ("upgrade", "downgrade"),
    ("open", "close"),
    ("opening", "closing"),
    ("income", "expense"),
    ("liabilities", "assets"),
})

# 前缀对立（一方以 X 开头、另一方以 Y 开头），如 bnum/snum、bvol/svol
PREFIX_PAIRS: frozenset[tuple[str, str]] = frozenset({
    ("b", "s"),      # buy / sell 类缩写
    ("buy", "sell"),
    ("long", "short"),
})


# ---------------------------------------------------------------- 词根配对

def _tokenize(fid: str) -> list[str]:
    return fid.lower().replace("-", "_").split("_")


def _tokens_opposite(a_tokens: list[str], b_tokens: list[str]) -> bool:
    """两个 token 列表里是否存在对立词根（完整对立或前缀对立）。

    前缀对立（b/s、buy/sell、long/short 开头）要求去掉首字母/首词后
    其余部分相同（bnum↔snum、bvol↔svol），排除 bnum↔svol 这类单位错配。
    """
    for ta in a_tokens:
        for tb in b_tokens:
            if (ta, tb) in PAIRS or (tb, ta) in PAIRS:
                return True
            for x, y in PREFIX_PAIRS:
                a_hit, b_hit = ta.startswith(x), tb.startswith(y)
                if a_hit and b_hit and ta[len(x):] == tb[len(y):]:
                    return True
                # 反向：a 以 y 开头、b 以 x 开头
                if tb.startswith(x) and ta.startswith(y) and tb[len(x):] == ta[len(y):]:
                    return True
    return False


def pair_fields(
    fields_meta: list[dict],
    *,
    min_common_tokens: int = 2,
    strict: bool = False,
) -> list[tuple[str, str]]:
    """按字段命名词根做语义配对。

    规则：两个字段 id 拆 token 后，公共前缀 ≥ min_common_tokens 个 token，
    且剩余 token 中存在对立词根（PAIRS / PREFIX_PAIRS），则配对。

    strict=True：要求两个字段除对立 token 外其余 token 完全相同（长度一致、
    只有一处对立），排除跨窗口/跨期错配（如 12m 配 18m、fy1 配 fy2）。
    返回 [(fid_a, fid_b), ...]，同前缀族内自动去重、跳过自身。
    """
    fids = [f.get("id") for f in fields_meta if f.get("id")]
    tokens = {fid: _tokenize(fid) for fid in fids}
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for i in range(len(fids)):
        for j in range(i + 1, len(fids)):
            a, b = fids[i], fids[j]
            ta, tb = tokens[a], tokens[b]
            common = 0
            for x, y in zip(ta, tb):
                if x == y:
                    common += 1
                else:
                    break
            if common < min_common_tokens:
                continue
            ra, rb = ta[common:], tb[common:]
            if strict:
                # 严格：长度一致，且非对立位置全部相同
                if len(ra) != len(rb):
                    continue
                diff_positions = [k for k in range(len(ra)) if ra[k] != rb[k]]
                if len(diff_positions) != 1:
                    continue
                # 只有唯一不同位置是对立词根
                k = diff_positions[0]
                if not _single_opposite(ra[k], rb[k]):
                    continue
            elif not _tokens_opposite(ra, rb):
                continue
            key = (a, b) if a < b else (b, a)
            if key in seen:
                continue
            seen.add(key)
            out.append(key)
    return out


def _single_opposite(a: str, b: str) -> bool:
    """单个 token 是否对立（完整词根或前缀对立）。"""
    if (a, b) in PAIRS or (b, a) in PAIRS:
        return True
    for x, y in PREFIX_PAIRS:
        a_hit, b_hit = a.startswith(x), b.startswith(y)
        if a_hit and b_hit and a[len(x):] == b[len(y):]:
            return True
        if b.startswith(x) and a.startswith(y) and b[len(x):] == a[len(y):]:
            return True
    return False


# ---------------------------------------------------------------- 字段表达式

def _field_expr(fid: str, meta_by_id: dict[str, dict], vec_ops: tuple[str, ...]) -> str:
    """把字段 id 转成可组装的表达式片段（VECTOR 先 vec_avg 转 MATRIX）。"""
    meta = meta_by_id.get(fid) or {}
    if meta.get("type") == "VECTOR" and vec_ops:
        return f"{vec_ops[0]}({fid})"
    return fid


# ---------------------------------------------------------------- 六类构造

def _ratio(a: str, b: str) -> str:
    return f"({a} - {b}) / ({a} + {b})"


def _diff(a: str, b: str) -> str:
    return f"({a}) - ({b})"


def _prod(a: str, b: str) -> str:
    return f"({a}) * ({b})"


def _sum(a: str, b: str) -> str:
    return f"({a}) + ({b})"


def _chg(a: str, b: str, window: int = 20) -> str:
    return f"ts_delta({a}, {window}) - ts_delta({b}, {window})"


def _event(a: str, b: str) -> str:
    return f"if_else({a} > {b}, {a}, 0)"


# 构造方式注册表：key → (展示名, 构造函数)
METHODS: dict[str, tuple[str, callable]] = {
    "ratio": ("比率 (A-B)/(A+B)", _ratio),
    "diff": ("差值 A-B", _diff),
    "prod": ("乘积 A*B", _prod),
    "sum": ("加总 A+B", _sum),
    "chg": ("变化率差", _chg),
    "event": ("事件驱动", _event),
}


def build_gem_expressions(
    pairs: list[tuple[str, str]],
    *,
    fields_meta: list[dict],
    methods: tuple[str, ...] = ("ratio", "diff"),
    vec_ops: tuple[str, ...] = ("vec_avg", "vec_sum"),
    wrap: bool = True,
    backfill_days: int = 120,
    winsorize_std: float = 4.0,
) -> list[str]:
    """按配对 + 构造方式生成 gems 表达式列表（不套算子，供展开）。

    wrap=True 时每个字段片段先包装 winsorize(ts_backfill(...))，与一阶一致；
    wrap=False 时直接用裸字段/vec_avg（对齐教程示例风格）。
    """
    meta_by_id = {f.get("id"): f for f in fields_meta if f.get("id")}
    exprs: list[str] = []
    seen: set[str] = set()

    def wrap_expr(frag: str) -> str:
        if wrap:
            return f"winsorize(ts_backfill({frag}, {backfill_days}), std={winsorize_std:g})"
        return frag

    for a, b in pairs:
        fa = wrap_expr(_field_expr(a, meta_by_id, vec_ops))
        fb = wrap_expr(_field_expr(b, meta_by_id, vec_ops))
        for key in methods:
            fn = METHODS.get(key)
            if fn is None:
                continue
            expr = fn[1](fa, fb)
            if expr not in seen:
                seen.add(expr)
                exprs.append(expr)
    return exprs


# ---------------------------------------------------------------- 一阶展开

@dataclass
class GemExpandConfig:
    """GEM 展开配置（对应 UI 展开区 + 模拟参数）。"""

    # 是否展开裸 gems（不套算子）
    include_raw: bool = True
    # 横截面算子
    cross_section_ops: tuple[str, ...] = ()
    # 时序算子 + 窗口
    ts_ops: tuple[str, ...] = ()
    windows: tuple[int, ...] = DEFAULT_WINDOWS
    # 每个表达式初始 decay
    initial_decay: int = 1

    def validate(self) -> None:
        if not self.ts_ops and not self.cross_section_ops and not self.include_raw:
            raise ValueError("至少选择一种展开方式（横截面/时序/裸 gems）")
        if any(w <= 0 for w in self.windows):
            raise ValueError("windows 必须全部为正整数")
        if self.ts_ops and not self.windows:
            raise ValueError("启用时序算子时必须提供窗口列表")


def expand_gems(
    gems: list[str],
    cfg: GemExpandConfig,
) -> list[tuple[str, int]]:
    """把 gems 表达式池按算子展开成 [(expression, decay)]。

    内部复用 first_order.build_first_order_expressions：
    raw → cs_op(gem) → ts_op(gem, window) × windows。
    """
    if not gems:
        return []
    cfg.validate()
    first_cfg = FirstOrderConfig(
        field_exprs=gems,
        cross_section_ops=cfg.cross_section_ops,
        ts_ops=cfg.ts_ops,
        windows=cfg.windows,
        include_raw_field=cfg.include_raw,
        initial_decay=cfg.initial_decay,
    )
    return build_first_order_expressions(first_cfg)


def estimate_count(gems: list[str], cfg: GemExpandConfig) -> int:
    """预估展开后的表达式总数（与 expand_gems 输出一致）。"""
    if not gems:
        return 0
    cfg.validate()
    n = len(gems)
    raw = n if cfg.include_raw else 0
    cross = len(cfg.cross_section_ops) * n
    ts = len(cfg.ts_ops) * len(cfg.windows) * n
    return raw + cross + ts
