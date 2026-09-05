"""字段预处理：MATRIX/VECTOR 包装 winsorize + ts_backfill。"""

from __future__ import annotations

VEC_OPS_DEFAULT = ("vec_avg", "vec_sum")


def process_datafields(
    fields: list[dict],
    vec_ops: tuple[str, ...] = VEC_OPS_DEFAULT,
    backfill_days: int = 120,
    winsorize_std: float = 4.0,
) -> list[str]:
    """把原始字段列表包装成可直接组装的表达式片段。

    输入：BrainClient.get_datafields 返回的字段字典列表（含 type 字段）
    输出：已 winsorize + ts_backfill 包装的字段表达式

    VECTOR 类型字段先用 vec_ops 转 MATRIX，再走统一包装。
    注意：winsorize 的 std 参数格式化用 {std:g}，输出 "4" 而非 "4.0"，
    与原 machine_lib.process_datafields 的 "std=4" 一致。
    """
    matrix_ids = [
        f["id"] for f in fields
        if f.get("type") == "MATRIX" and f.get("id")
    ]

    vec_expressions: list[str] = []
    for f in fields:
        if f.get("type") != "VECTOR":
            continue
        fid = f.get("id")
        if not fid:
            continue  # 缺 id 的字段跳过，不 KeyError
        for vec_op in vec_ops:
            # vec_choose 留扩展点（当前 MVP 不支持 nth 参数）
            if vec_op == "vec_choose":
                continue
            vec_expressions.append(f"{vec_op}({fid})")

    all_field_exprs = matrix_ids + vec_expressions
    return [
        f"winsorize(ts_backfill({expr}, {backfill_days}), std={winsorize_std:g})"
        for expr in all_field_exprs
    ]