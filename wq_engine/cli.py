"""AlphaMachine 命令行入口。

用法：
    # 启动桌面 GUI
    python -m wq_engine.cli ui

    # 命令行跑一阶流水线（YAML 配置，可无 UI 验证引擎）
    python -m wq_engine.cli run --config inputs/first_order_usa.yaml

    # 命令行跑 AI 批次（AI 生成 JSON 后直接驱动回测，无需 UI）
    python -m wq_engine.cli ai-batch --input outputs/gem_xxx.json [--producer 阿法]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from loguru import logger


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="alpha-machine", description=__doc__)
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("ui", help="启动桌面 GUI")

    run = sub.add_parser("run", help="命令行跑一阶流水线")
    run.add_argument("--config", required=True, help="YAML 配置文件路径")

    ai = sub.add_parser("ai-batch", help="命令行跑 AI 批次（JSON 直接驱动回测）")
    ai.add_argument("--input", required=True, help="AI 批次 JSON 文件（settings + expressions）")
    ai.add_argument("--producer", default="阿法", help="生产者标识（默认 阿法）")
    ai.add_argument("--max-concurrent", type=int, default=3, help="并发批次（默认 3）")
    ai.add_argument("--batch-size", type=int, default=10, help="每批表达式数（默认 10）")
    ai.add_argument("--max-retries", type=int, default=5, help="单条重试次数（默认 5）")
    ai.add_argument("--db", default=None, help="alpha_machine.db 路径（默认自动探测最新 dist_v*）")
    ai.add_argument("--dry-run", action="store_true", help="只解析/去重/建批次，不真正跑模拟")

    return parser


def cmd_ai_batch(
    input_path: str,
    producer: str,
    max_concurrent: int,
    batch_size: int,
    max_retries: int,
    db_path: str | None,
    dry_run: bool,
) -> int:
    """命令行跑 AI 批次：读 JSON → 登录 → 去重 → 建批次 → 调度回测 → 回填。"""
    import json

    from wq_engine.api.client import APIClient
    from wq_engine.api.config import BrainConfig
    from wq_engine.scheduler.runner import BatchScheduler
    from wq_engine.storage.database import Storage

    # 1. 读 AI 批次 JSON
    try:
        payload = json.loads(Path(input_path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.error(f"输入文件不存在：{input_path}")
        return 1
    except json.JSONDecodeError as e:
        logger.error(f"JSON 解析失败：{e}")
        return 1
    settings = payload.get("settings") or {}
    exprs = payload.get("expressions") or []
    if not exprs:
        logger.error("expressions 为空，请检查文件格式")
        return 1
    logger.info("读取 {} 个表达式，settings={}", len(exprs), settings)

    # 2. 凭据 + 登录
    cfg = BrainConfig.from_env()
    if not cfg.is_authenticated():
        logger.error("未配置凭据：请设置环境变量 WQ_USERNAME / WQ_PASSWORD")
        return 1
    client = APIClient(cfg)
    client.authenticate()
    logger.info("登录成功")

    # 3. 库路径：显式指定 > 最新 dist_v*（GUI exe 实际使用的库）> 源码 data/
    #    关键修复：用户现在跑打包版 exe，GUI 读 dist_vNN/AlphaMachine/data/。
    #    CLI 必须与 exe 同一库，否则批次写在源码 data/ 里、GUI「批次对账」看不到。
    if db_path is None:
        db_path = _latest_dist_db()
    logger.info("数据库：{}", db_path)

    # 4. 去重 + 建批次（与 GUI ai_batch_tab 同逻辑）
    from wq_engine.dedup import filter_unverified
    storage = Storage(db_path)
    s = dict(settings)
    s.setdefault("instrumentType", "EQUITY")
    s.setdefault("pasteurization", "ON")
    s.setdefault("testPeriod", "P0Y")
    s.setdefault("unitHandling", "VERIFY")
    s.setdefault("nanHandling", "ON")
    s.setdefault("language", "FASTEXPR")
    s.setdefault("visualization", False)
    s.setdefault("truncation", 0.08)
    triples = [(it["expression"], int(it.get("decay") or s.get("decay", 1)), dict(s))
               for it in exprs]
    completed = storage.completed_expression_keys()
    triples, skipped = filter_unverified(triples, completed)
    logger.info("去重跳过 {} 条，待跑 {} 条", skipped, len(triples))
    if not triples:
        logger.info("全部已回测过，无需跑")
        storage.close()
        return 0

    # 5. 建批次 + 任务
    batch_no = storage.next_batch_no()
    storage.create_ai_batch(
        batch_no=batch_no,
        producer=producer,
        region=str(s.get("region", "")),
        expression_count=len(triples),
        note=f"CLI 导入 {len(exprs)} 条，去重跳过 {skipped}",
    )
    task_run_id = storage.create_task_run(
        name=f"CLI_AI批次_{batch_no}",
        kind="ai_batch",
        config={"total": len(triples), "settings": triples[0][2] if triples else {}},
        total=len(triples),
        batch_no=batch_no,
    )
    logger.info("批次 {} 已创建（任务 #{}），待跑 {} 条", batch_no, task_run_id, len(triples))

    if dry_run:
        storage.update_ai_batch_status(batch_no, "pending")
        logger.info("[dry-run] 已创建批次 {}（pending），未启动模拟", batch_no)
        storage.close()
        return 0

    # 6. 调度回测
    scheduler = BatchScheduler(
        client=client,
        storage=storage,
        max_concurrent_batches=max_concurrent,
        batch_size=batch_size,
        max_retries=max_retries,
    )

    def on_progress(event: str, payload: dict) -> None:
        if event == "batch_started":
            logger.info("Batch {}/{} 开始", payload["batch_idx"] + 1, payload["total_batches"])
        elif event == "sim_completed":
            logger.info("完成 #{} alpha_id={}", payload["sim_id"], payload["alpha_id"])
        elif event == "sim_failed":
            logger.warning("失败 #{}：{}", payload["sim_id"], payload["error"])
        elif event == "task_done":
            logger.info("任务完成：成功 {}，失败 {}，状态 {}", payload["success"], payload["failed"], payload["status"])

    scheduler._cb = on_progress
    scheduler.run(task_run_id, triples)
    scheduler.join()

    # 7. 更新批次状态
    ok_count = storage.count_alpha_by_batch(batch_no) if hasattr(storage, "count_alpha_by_batch") else None
    storage.update_ai_batch_status(batch_no, "completed")
    logger.info("批次 {} 完成，落库 alpha {}", batch_no, ok_count or "?")
    storage.close()
    return 0


def _latest_dist_db() -> str:
    """按 dist_vNN 版本号取最新库（mtime 不可靠：打包会初始化新库覆盖时间戳）。"""
    import re
    root = _app_root()
    best, best_ver = None, -1
    for d in root.glob("dist_v*/AlphaMachine/data/alpha_machine.db"):
        m = re.search(r"dist_v(\d+)", str(d))
        if m:
            ver = int(m.group(1))
            if ver > best_ver:
                best_ver, best = ver, d
    return str(best) if best else str(root / "data" / "alpha_machine.db")


def cmd_ui() -> int:
    from wq_engine.ui.main_window import main
    return main()


def cmd_run(config_path: str) -> int:
    import yaml

    from wq_engine.api.client import APIClient
    from wq_engine.api.config import BrainConfig
    from wq_engine.factories.field_prep import process_datafields
    from wq_engine.factories.first_order import (
        FirstOrderConfig,
        build_first_order_expressions,
    )
    from wq_engine.scheduler.runner import BatchScheduler
    from wq_engine.storage.database import Storage

    try:
        config = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.error(f"配置文件不存在：{config_path}")
        return 1
    if not isinstance(config, dict):
        logger.error(f"配置文件格式错误（应为 YAML 对象）：{config_path}")
        return 1

    # 1. 凭据
    cfg = BrainConfig.from_env()
    if not cfg.is_authenticated():
        logger.error("未配置凭据：请设置环境变量 WQ_USERNAME / WQ_PASSWORD")
        return 1

    # 2. 登录 + 拉字段
    client = APIClient(cfg)
    client.authenticate()
    logger.info("登录成功")
    fields = client.get_datafields(
        region=config.get("region", "USA"),
        universe=config.get("universe", "TOP3000"),
        delay=config.get("delay", 1),
        dataset_id=config.get("dataset_id", ""),
    )
    logger.info("拉取字段 {} 个", len(fields))
    field_exprs = process_datafields(fields)

    # 3. 生成一阶表达式
    fo_cfg = FirstOrderConfig(
        field_exprs=field_exprs,
        cross_section_ops=tuple(config.get("cross_section_ops", ())),
        ts_ops=tuple(config.get("ts_ops", ("ts_rank",))),
        windows=tuple(config.get("windows", (5, 22))),
        include_raw_field=config.get("include_raw_field", True),
        initial_decay=config.get("initial_decay", 1),
    )
    try:
        exprs = build_first_order_expressions(fo_cfg)
    except ValueError as e:
        logger.error(f"配置校验失败：{e}")
        return 1
    logger.info("生成一阶表达式 {} 个", len(exprs))
    if not exprs:
        logger.error("表达式为空，请检查配置")
        return 1

    settings = {
        "instrumentType": "EQUITY",
        "region": config.get("region", "USA"),
        "universe": config.get("universe", "TOP3000"),
        "delay": config.get("delay", 1),
        "decay": config.get("initial_decay", 1),
        "neutralization": config.get("neutralization", "SUBINDUSTRY"),
        "truncation": 0.08,
        "pasteurization": "ON",
        "testPeriod": "P0Y",
        "unitHandling": "VERIFY",
        "nanHandling": "ON",
        "language": "FASTEXPR",
        "visualization": False,
    }

    # 4. 入库 + 调度
    storage = Storage(_app_root() / "data" / "alpha_machine.db")
    task_run_id = storage.create_task_run(
        name=f"CLI_一阶_{config.get('region', 'USA')}",
        kind="first_order",
        config=config,
        total=len(exprs),
    )
    scheduler = BatchScheduler(
        client=client,
        storage=storage,
        max_concurrent_batches=config.get("max_concurrent", 3),
        batch_size=config.get("batch_size", 10),
        max_retries=config.get("max_retries", 5),
    )

    def on_progress(event: str, payload: dict) -> None:
        if event == "batch_started":
            logger.info(
                "Batch {}/{} 开始", payload["batch_idx"] + 1, payload["total_batches"]
            )
        elif event == "sim_completed":
            logger.info("完成 #{} alpha_id={}", payload["sim_id"], payload["alpha_id"])
        elif event == "sim_failed":
            logger.warning("失败 #{}：{}", payload["sim_id"], payload["error"])
        elif event == "task_done":
            logger.info(
                "任务完成：成功 {}，失败 {}，状态 {}",
                payload["success"], payload["failed"], payload["status"],
            )

    scheduler._cb = on_progress
    scheduler.run(task_run_id, [(e, d, settings) for e, d in exprs])
    scheduler.join()

    storage.close()
    return 0


def _app_root() -> Path:
    """数据根目录：exe 模式下用 exe 所在目录，源码模式下用项目根。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    # 日志到 stderr + 文件
    log_dir = _app_root() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.add(sys.stderr, level="INFO")
    logger.add(log_dir / "alpha_machine.log", rotation="10 MB", level="INFO")

    # 崩溃兜底：windowed 模式 stderr 不可见，异常/段错误堆栈落盘 logs/crash.log
    crash_path = log_dir / "crash.log"
    try:
        import faulthandler
        with open(crash_path, "a", encoding="utf-8") as fh:
            faulthandler.enable(fh)
    except Exception:
        pass

    def _excepthook(tp, val, tb):
        import traceback
        try:
            with open(crash_path, "a", encoding="utf-8") as fh:
                fh.write("=" * 30 + " UNCAUGHT " + "=" * 30 + "\n")
                traceback.print_exception(tp, val, tb, file=fh)
        except Exception:
            pass

    sys.excepthook = _excepthook

    if args.command == "ui":
        return cmd_ui()
    if args.command == "run":
        return cmd_run(args.config)
    if args.command == "ai-batch":
        return cmd_ai_batch(
            args.input, args.producer, args.max_concurrent,
            args.batch_size, args.max_retries, args.db, args.dry_run,
        )
    _build_parser().print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())