"""SQLite 存储实现。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from loguru import logger


class _ThreadSafeConn:
    """sqlite3 连接线程安全代理：写操作（execute/executemany/commit）串行化。

    背景：Storage 单连接被多线程共享（runner 写 simulations、Backfill/AutoCheck
    worker 写 alphas、UI 线程读），sqlite3 连接本身非线程安全，并发 execute
    可能导致崩溃/数据错乱。锁住写操作即可消除主要风险。
    读操作（__getattr__ 透传）保持原样，读并发安全。
    """

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._lock = threading.Lock()

    def execute(self, sql, params=()):
        with self._lock:
            return self._conn.execute(sql, params)

    def executemany(self, sql, seq):
        with self._lock:
            return self._conn.executemany(sql, seq)

    def executescript(self, script):
        with self._lock:
            return self._conn.executescript(script)

    def commit(self):
        with self._lock:
            return self._conn.commit()

    def close(self):
        with self._lock:
            return self._conn.close()

    def __setattr__(self, name, value):
        if name in ("_conn", "_lock"):
            object.__setattr__(self, name, value)
        else:
            # row_factory 等属性赋值透传到真实连接
            setattr(self._conn, name, value)

    def __getattr__(self, name):
        # row_factory / cursor / isolation_level 等属性透传
        return getattr(self._conn, name)


def expression_key(expression: str, settings: dict) -> str:
    """表达式指纹：表达式 + 关键 settings 的哈希。

    用于跨任务去重。settings 只取影响回测结果的关键字段，
    这样"同一表达式换中性化/decay/region 重新回测"不会被误判为重复。
    """
    payload = json.dumps(
        {
            "expr": expression,
            "region": settings.get("region"),
            "universe": settings.get("universe"),
            "delay": settings.get("delay"),
            "decay": settings.get("decay"),
            "neutralization": settings.get("neutralization"),
            "truncation": settings.get("truncation"),
            "pasteurization": settings.get("pasteurization"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class StorageError(Exception):
    pass


class Storage:
    """本地 SQLite 持久化（单文件、单连接 + 线程锁）。"""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS task_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        kind TEXT NOT NULL,              -- 'first_order' / 'second_order' / 'third_order'
        config_json TEXT NOT NULL,       -- FirstOrderConfig 等的 JSON 序列化
        status TEXT NOT NULL DEFAULT 'pending',  -- pending/running/paused/completed/failed
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        total INTEGER DEFAULT 0,
        success INTEGER DEFAULT 0,
        failed INTEGER DEFAULT 0,
        batch_no TEXT                    -- AI 批次号（可选，对账用）
    );

    CREATE TABLE IF NOT EXISTS simulations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_run_id INTEGER NOT NULL,
        expression TEXT NOT NULL,
        decay INTEGER NOT NULL,
        settings_json TEXT NOT NULL,
        expr_key TEXT,                  -- 表达式+settings 指纹哈希（去重用）
        alpha_id TEXT,                  -- 平台分配，模拟完成后才有
        progress_url TEXT,              -- 提交后的 Location，断点续跑定位用
        status TEXT NOT NULL DEFAULT 'pending',  -- pending/submitted/completed/failed
        progress INTEGER DEFAULT 0,
        retry_count INTEGER DEFAULT 0,
        last_error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (task_run_id) REFERENCES task_runs(id) ON DELETE CASCADE
    );

    CREATE INDEX IF NOT EXISTS idx_sim_task_status
        ON simulations(task_run_id, status);
    CREATE INDEX IF NOT EXISTS idx_sim_alpha_id
        ON simulations(alpha_id);

    CREATE TABLE IF NOT EXISTS alphas (
        alpha_id TEXT PRIMARY KEY,
        expression TEXT NOT NULL,
        sharpe REAL,
        returns REAL,               -- 平台 is.returns（模拟期收益，小数，如 0.0195 = 1.95%）
        fitness REAL,
        turnover REAL,
        margin REAL,
        long_count INTEGER,
        short_count INTEGER,
        decay INTEGER,
        region TEXT,
        neutralization TEXT,
        date_created TEXT,
        pnl_json TEXT,              -- 日度 PnL 的 JSON（[{date, pnl}, ...]）
        check_pc REAL,              -- 最近一次 submission check 的 PROD_CORRELATION
        check_failed TEXT,          -- FAIL 项（逗号分隔；NULL/空 = 无 FAIL）
        check_status TEXT,          -- pass / warn / fail（warn = 无 FAIL 但 |pc|>0.7）
        checked_at TEXT,
        batch_no TEXT,              -- AI 批次号（可选，对账用）
        retrieved_at TEXT NOT NULL,
        -- v80+：本地 Self / PPA Corr 字段（PnL 同步后异步计算）
        pnl_fetched_at TEXT,        -- PnL 上次成功抓取时间
        self_corr_min REAL,         -- Self Corr 池子 min（|min|）
        self_corr_max REAL,         -- Self Corr 池子 max（绝对值最大）
        self_corr_top5 TEXT,        -- JSON 字符串 [{alpha_id, correlation}, ...]
        ppa_corr_min REAL,          -- Power Pool Corr 池子 min
        ppa_corr_max REAL,
        ppa_corr_top5 TEXT,
        max_corr REAL,              -- max(|Self_max|, |PPA_max|, |Prod_PC|)
        max_corr_source TEXT,       -- 'self' / 'ppa' / 'prod'
        corr_computed_at TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_alpha_sharpe ON alphas(sharpe);

    CREATE TABLE IF NOT EXISTS ai_batches (
        batch_no TEXT PRIMARY KEY,          -- 如 B20260813-001
        producer TEXT DEFAULT '',           -- 表达式来源（哪个 AI / 人）
        dataset_id TEXT,
        region TEXT,
        expression_count INTEGER DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'pending',  -- pending/running/completed/failed
        note TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );


    CREATE TABLE IF NOT EXISTS submissions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        alpha_id TEXT NOT NULL,
        status_code INTEGER,
        message TEXT,
        ok INTEGER DEFAULT 0,        -- 1=成功 0=失败
        submitted_at TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_submission_alpha
        ON submissions(alpha_id);

    -- ===== AI 协作（v61）：AI 接管控制 + 命令队列 + 时间线 =====
    CREATE TABLE IF NOT EXISTS ai_control (
        id INTEGER PRIMARY KEY CHECK (id = 1),   -- 单行
        mode TEXT NOT NULL DEFAULT 'manual',     -- manual（手动）/ auto（AI 接管）
        auto_loop INTEGER NOT NULL DEFAULT 0,    -- 0=单轮 / 1=自动循环
        max_rounds INTEGER NOT NULL DEFAULT 5,   -- 自动循环最大轮次
        max_per_round INTEGER NOT NULL DEFAULT 100, -- 单轮最大回测数
        current_round INTEGER NOT NULL DEFAULT 0,-- 已完成的轮次
        ai_state TEXT NOT NULL DEFAULT '空闲',    -- 空闲/生成中/待导入/回测中/分析中
        stop_requested INTEGER NOT NULL DEFAULT 0,-- 1=用户请求停止（AI 检测到即停）
        updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS ai_commands (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        command TEXT NOT NULL,                   -- run_batch / stop / set_mode
        payload TEXT,                            -- JSON：{path, producer, ...}
        status TEXT NOT NULL DEFAULT 'pending',  -- pending/executed/cancelled
        created_at TEXT NOT NULL,
        executed_at TEXT
    );

    CREATE TABLE IF NOT EXISTS ai_timeline (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        level TEXT NOT NULL DEFAULT 'info',      -- info/success/warn
        message TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_ai_commands_status
        ON ai_commands(status);

    -- ===== 平台元信息（v69）：跨进程共享配额等（MCP 写 / GUI 读） =====
    CREATE TABLE IF NOT EXISTS platform_meta (
        key TEXT PRIMARY KEY,
        value TEXT,
        updated_at TEXT NOT NULL
    );
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = _ThreadSafeConn(sqlite3.connect(str(self.db_path), check_same_thread=False))
        self._conn.row_factory = sqlite3.Row
        # 多进程并发（web 服务 + mcp_server 各持有独立连接）共享同一 SQLite 库，
        # 必须启用 WAL 模式 + busy_timeout，否则写冲突直接报 "database is locked"。
        # WAL 是持久化到 db 文件的属性：任一连接设过一次，后续所有连接自动继承，
        # 因此即使其他进程（如常驻 mcp_server）未重启也会受益。
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=30000")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        except Exception as e:  # noqa: BLE001
            logger.warning("设置 SQLite PRAGMA 失败（可忽略，旧库可能只读）：{}", e)
        self._lock = threading.Lock()
        self._conn.executescript(self.SCHEMA)
        self._migrate()
        self._conn.commit()
        logger.info("Storage 初始化完成：db={}", self.db_path)

    def _migrate(self) -> None:
        """轻量迁移：老库补 progress_url / pnl_json / expr_key 列（幂等）。"""
        sim_cols = {
            r[1] for r in self._conn.execute("PRAGMA table_info(simulations)")
        }
        if "progress_url" not in sim_cols:
            self._conn.execute(
                "ALTER TABLE simulations ADD COLUMN progress_url TEXT"
            )
        if "expr_key" not in sim_cols:
            self._conn.execute(
                "ALTER TABLE simulations ADD COLUMN expr_key TEXT"
            )
        if "submitted_at" not in sim_cols:
            self._conn.execute(
                "ALTER TABLE simulations ADD COLUMN submitted_at TEXT"
            )
        # 索引放迁移里（先加列再建索引，兼容旧库）
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sim_expr_key_status "
            "ON simulations(expr_key, status)"
        )
        alpha_cols = {
            r[1] for r in self._conn.execute("PRAGMA table_info(alphas)")
        }
        if "pnl_json" not in alpha_cols:
            self._conn.execute(
                "ALTER TABLE alphas ADD COLUMN pnl_json TEXT"
            )
        # submission check 结果列（旧库迁移，幂等）
        for col, ddl in {
            "check_pc": "ALTER TABLE alphas ADD COLUMN check_pc REAL",
            "check_failed": "ALTER TABLE alphas ADD COLUMN check_failed TEXT",
            "check_status": "ALTER TABLE alphas ADD COLUMN check_status TEXT",
            "checked_at": "ALTER TABLE alphas ADD COLUMN checked_at TEXT",
            "returns": "ALTER TABLE alphas ADD COLUMN returns REAL",
        }.items():
            if col not in alpha_cols:
                self._conn.execute(ddl)
        # check 索引也放迁移里（先加列再建索引，兼容旧库）
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_alpha_check ON alphas(check_status)"
        )
        # v80+：本地 Self/PPA Corr 字段（旧库迁移，幂等）
        for col, ddl in {
            "pnl_fetched_at": "ALTER TABLE alphas ADD COLUMN pnl_fetched_at TEXT",
            "self_corr_min": "ALTER TABLE alphas ADD COLUMN self_corr_min REAL",
            "self_corr_max": "ALTER TABLE alphas ADD COLUMN self_corr_max REAL",
            "self_corr_top5": "ALTER TABLE alphas ADD COLUMN self_corr_top5 TEXT",
            "ppa_corr_min": "ALTER TABLE alphas ADD COLUMN ppa_corr_min REAL",
            "ppa_corr_max": "ALTER TABLE alphas ADD COLUMN ppa_corr_max REAL",
            "ppa_corr_top5": "ALTER TABLE alphas ADD COLUMN ppa_corr_top5 TEXT",
            "max_corr": "ALTER TABLE alphas ADD COLUMN max_corr REAL",
            "max_corr_source": "ALTER TABLE alphas ADD COLUMN max_corr_source TEXT",
            "corr_computed_at": "ALTER TABLE alphas ADD COLUMN corr_computed_at TEXT",
        }.items():
            if col not in alpha_cols:
                self._conn.execute(ddl)
        # max_corr 索引（投稿前自检常用排序）
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_alpha_max_corr ON alphas(max_corr)"
        )
        # submissions / task_runs 补列保护：SCHEMA 里的索引依赖这些列，
        # 老库缺列时 executescript 建索引会抛 no such column 导致启动崩溃
        sub_cols = {
            r[1] for r in self._conn.execute("PRAGMA table_info(submissions)")
        }
        if "submitted_at" not in sub_cols:
            self._conn.execute(
                "ALTER TABLE submissions ADD COLUMN submitted_at TEXT NOT NULL DEFAULT ''"
            )
        # 索引必须在补列之后建（SCHEMA 里不能放：旧库 executescript 阶段缺列会先崩）
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_submission_time ON submissions(submitted_at)"
        )
        task_cols = {
            r[1] for r in self._conn.execute("PRAGMA table_info(task_runs)")
        }
        for col, ddl in {
            "total": "ALTER TABLE task_runs ADD COLUMN total INTEGER DEFAULT 0",
            "success": "ALTER TABLE task_runs ADD COLUMN success INTEGER DEFAULT 0",
            "failed": "ALTER TABLE task_runs ADD COLUMN failed INTEGER DEFAULT 0",
            "batch_no": "ALTER TABLE task_runs ADD COLUMN batch_no TEXT",
        }.items():
            if col not in task_cols:
                self._conn.execute(ddl)
        # alphas 补 batch_no 列 + 批次索引（先加列再建索引，兼容旧库）
        alpha_cols_now = {
            r[1] for r in self._conn.execute("PRAGMA table_info(alphas)")
        }
        if "batch_no" not in alpha_cols_now:
            self._conn.execute("ALTER TABLE alphas ADD COLUMN batch_no TEXT")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_alpha_batch ON alphas(batch_no)"
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -------- task_runs --------

    def create_task_run(
        self,
        name: str,
        kind: str,
        config: dict,
        total: int,
        batch_no: str | None = None,
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO task_runs
                   (name, kind, config_json, status, created_at, updated_at, total, batch_no)
                   VALUES (?, ?, ?, 'pending', ?, ?, ?, ?)""",
                (name, kind, json.dumps(config, ensure_ascii=False), now, now, total, batch_no),
            )
            self._conn.commit()
            return cur.lastrowid

    def update_task_run_status(
        self,
        task_run_id: int,
        status: str,
        *,
        success: int | None = None,
        failed: int | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            if success is not None and failed is not None:
                self._conn.execute(
                    """UPDATE task_runs
                       SET status=?, updated_at=?, success=?, failed=?
                       WHERE id=?""",
                    (status, now, success, failed, task_run_id),
                )
            elif success is not None:
                self._conn.execute(
                    """UPDATE task_runs
                       SET status=?, updated_at=?, success=? WHERE id=?""",
                    (status, now, success, task_run_id),
                )
            elif failed is not None:
                self._conn.execute(
                    """UPDATE task_runs
                       SET status=?, updated_at=?, failed=? WHERE id=?""",
                    (status, now, failed, task_run_id),
                )
            else:
                self._conn.execute(
                    """UPDATE task_runs SET status=?, updated_at=? WHERE id=?""",
                    (status, now, task_run_id),
                )
            self._conn.commit()

    def get_task_run(self, task_run_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM task_runs WHERE id=?", (task_run_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_task_runs(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM task_runs ORDER BY id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def find_resumable_task_run(self, kind: str) -> dict | None:
        """找一个可恢复的（pending/running/paused）同类型任务。"""
        with self._lock:
            row = self._conn.execute(
                """SELECT * FROM task_runs
                   WHERE kind=? AND status IN ('pending','running','paused')
                   ORDER BY id DESC LIMIT 1""",
                (kind,),
            ).fetchone()
        return dict(row) if row else None

    # -------- simulations --------

    def bulk_create_simulations(
        self,
        task_run_id: int,
        simulations: Iterable[tuple[str, int, dict]],
    ) -> int:
        """批量插入 simulation 记录（含 expr_key 指纹）。返回插入条数。"""
        now = datetime.now(timezone.utc).isoformat()
        rows = [
            (
                task_run_id, expr, decay,
                json.dumps(settings, ensure_ascii=False),
                expression_key(expr, settings),
                now, now,
            )
            for expr, decay, settings in simulations
        ]
        with self._lock:
            self._conn.executemany(
                """INSERT INTO simulations
                   (task_run_id, expression, decay, settings_json, expr_key,
                    status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)""",
                rows,
            )
            self._conn.commit()
        return len(rows)

    def completed_expression_keys(self) -> set[str]:
        """返回所有已回测成功（completed）的表达式指纹集合。

        用于任务间去重：已回测过的表达式不再重复提交。
        """
        with self._lock:
            rows = self._conn.execute(
                """SELECT DISTINCT expr_key FROM simulations
                   WHERE status='completed' AND expr_key IS NOT NULL"""
            ).fetchall()
        return {r["expr_key"] for r in rows}

    def count_completed_by_key(self, expr_key: str) -> int:
        """某指纹已成功回测的次数（调试/展示用）。"""
        with self._lock:
            row = self._conn.execute(
                """SELECT COUNT(*) AS n FROM simulations
                   WHERE expr_key=? AND status='completed'""",
                (expr_key,),
            ).fetchone()
        return row["n"]

    def list_completed_by_kind(
        self,
        kind: str,
        limit: int = 10_000,
    ) -> list[dict]:
        """列出某类型任务（first_order/second_order/third_order）下回测成功的 alpha。

        JOIN simulations + task_runs 过滤任务类型，LEFT JOIN alphas 补指标。
        用于「按阶段导出通过的 alpha」功能。
        """
        with self._lock:
            rows = self._conn.execute(
                """SELECT s.alpha_id, s.expression AS sim_expression, s.decay,
                          s.settings_json,
                          a.expression, a.sharpe, a.fitness, a.turnover, a.margin,
                          a.long_count, a.short_count, a.region, a.neutralization,
                          a.date_created, t.name AS task_name, t.created_at AS task_created_at
                   FROM simulations s
                   JOIN task_runs t ON t.id = s.task_run_id
                   LEFT JOIN alphas a ON a.alpha_id = s.alpha_id
                   WHERE t.kind=? AND s.status='completed'
                   ORDER BY s.id DESC LIMIT ?""",
                (kind, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_simulation_submitted(
        self,
        sim_id: int,
        progress_url: str,
    ) -> None:
        """记录已提交（progress_url 暂存到 last_error 字段以便断点续跑定位）。

        v28：同时写 submitted_at（今日回测槽统计用）。
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                """UPDATE simulations
                   SET status='submitted', progress_url=?, updated_at=?, submitted_at=?
                   WHERE id=?""",
                (progress_url, now, now, sim_id),
            )
            self._conn.commit()

    def count_simulations_since(self, iso_start: str) -> int:
        """统计 submitted_at >= iso_start 的模拟提交次数（v28 回测槽统计）。"""
        with self._lock:
            row = self._conn.execute(
                """SELECT COUNT(*) AS n FROM simulations
                   WHERE submitted_at IS NOT NULL AND submitted_at >= ?""",
                (iso_start,),
            ).fetchone()
        return int(row["n"]) if row else 0

    def count_unbackfilled(self, task_run_id: int | None = None) -> int:
        """已完成（有 alpha_id）但 alphas 表还没有的 simulation 数（v31 断点续填用）。

        task_run_id=None = 全库统计；指定时只统计该任务（流水线接力判断用）。
        """
        sql = (
            """SELECT COUNT(*) AS n FROM simulations s
               WHERE s.status='completed'
                 AND s.alpha_id IS NOT NULL AND s.alpha_id != ''
                 AND NOT EXISTS (
                     SELECT 1 FROM alphas a WHERE a.alpha_id = s.alpha_id
                 )"""
        )
        params: list[Any] = []
        if task_run_id is not None:
            sql += " AND s.task_run_id=?"
            params.append(task_run_id)
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return int(row["n"]) if row else 0

    def mark_simulation_completed(
        self,
        sim_id: int,
        alpha_id: str,
        progress: int = 100,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                """UPDATE simulations
                   SET status='completed', alpha_id=?, progress=?, updated_at=?
                   WHERE id=?""",
                (alpha_id, progress, now, sim_id),
            )
            self._conn.commit()

    def mark_simulation_failed(
        self,
        sim_id: int,
        error: str,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                """UPDATE simulations
                   SET status='failed', last_error=?, retry_count=retry_count+1,
                       updated_at=?
                   WHERE id=?""",
                (error[:500], now, sim_id),
            )
            self._conn.commit()

    def list_orphan_submitted(self, task_run_id: int, limit: int = 10_000) -> list[dict]:
        """孤儿 submitted：状态 submitted 但进程已中断、无法恢复轮询的 simulation。

        返回后调用方应标 cancelled（防永久卡死/重复烧配额）。"""
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM simulations
                   WHERE task_run_id=? AND status='submitted'
                   ORDER BY id LIMIT ?""",
                (task_run_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_simulation_cancelled(
        self,
        sim_id: int,
        reason: str = "任务取消",
    ) -> None:
        """把 simulation 标记为取消（cancelled，不算失败、不参与重试）。"""
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                """UPDATE simulations
                   SET status='cancelled', last_error=?, updated_at=?
                   WHERE id=?""",
                (reason[:500], now, sim_id),
            )
            self._conn.commit()

    def list_pending_simulations(
        self,
        task_run_id: int,
        limit: int = 100,
        max_retries: int = 5,
    ) -> list[dict]:
        """返回待处理（pending + failed 重试次数未超限）的 simulation。"""
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM simulations
                   WHERE task_run_id=?
                     AND (status='pending'
                          OR (status='failed' AND retry_count < ?))
                   ORDER BY id LIMIT ?""",
                (task_run_id, max_retries, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_completed_simulations(
        self,
        task_run_id: int | None = None,
    ) -> list[dict]:
        """返回 completed 且含 alpha_id 的 simulation（用于回填 alphas 表）。"""
        sql = (
            "SELECT * FROM simulations "
            "WHERE status='completed' AND alpha_id IS NOT NULL AND alpha_id != ''"
        )
        params: list[Any] = []
        if task_run_id is not None:
            sql += " AND task_run_id=?"
            params.append(task_run_id)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    # -------- alphas --------

    def upsert_alpha(self, alpha: dict, pnl: list[dict] | None = None, batch_no: str | None = None) -> None:
        """入库/更新一条 alpha。pnl 可选（日度 PnL 列表）。

        用 UPSERT 而非 INSERT OR REPLACE：REPLACE 会删行重建，把未列入
        INSERT 的 check_pc/check_failed/check_status/checked_at 抹成 NULL，
        导致已检查的 alpha 被重新判定为未检查、可能被重复提交。
        pnl 为 None 时保留已缓存的 pnl_json（批量回填不丢 PnL 缓存）。
        batch_no 可选：AI 批次号（对账用；已存在行不覆盖旧批次，保留首次归属）。
        """
        # 修复：alpha_id 为空/None 直接跳过——SQLite 允许 NULL 主键，
        # 重复 upsert 会产生不可去重的脏行
        if not alpha.get("alpha_id"):
            return
        now = datetime.now(timezone.utc).isoformat()
        pnl_json = json.dumps(pnl, ensure_ascii=False) if pnl else None
        with self._lock:
            self._conn.execute(
                """INSERT INTO alphas
                   (alpha_id, expression, sharpe, returns, fitness, turnover, margin,
                    long_count, short_count, decay, region, neutralization,
                    date_created, pnl_json, batch_no, retrieved_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(alpha_id) DO UPDATE SET
                       expression=excluded.expression,
                       sharpe=excluded.sharpe,
                       returns=excluded.returns,
                       fitness=excluded.fitness,
                       turnover=excluded.turnover,
                       margin=excluded.margin,
                       long_count=excluded.long_count,
                       short_count=excluded.short_count,
                       decay=excluded.decay,
                       region=excluded.region,
                       neutralization=excluded.neutralization,
                       date_created=excluded.date_created,
                       pnl_json=COALESCE(excluded.pnl_json, alphas.pnl_json),
                       batch_no=COALESCE(alphas.batch_no, excluded.batch_no),
                       retrieved_at=excluded.retrieved_at""",
                (
                    alpha["alpha_id"],
                    alpha.get("expression", ""),
                    alpha.get("sharpe"),
                    alpha.get("returns"),
                    alpha.get("fitness"),
                    alpha.get("turnover"),
                    alpha.get("margin"),
                    alpha.get("long_count"),
                    alpha.get("short_count"),
                    alpha.get("decay"),
                    alpha.get("region"),
                    alpha.get("neutralization"),
                    alpha.get("date_created"),
                    pnl_json,
                    batch_no,
                    now,
                ),
            )
            self._conn.commit()

    def upsert_alphas_bulk(
        self,
        alphas: list[dict],
        progress_cb=None,
    ) -> int:
        """批量入库 alpha（幂等）。返回成功条数。"""
        count = 0
        for alpha in alphas:
            try:
                self.upsert_alpha(alpha)
                count += 1
                if progress_cb and count % 20 == 0:
                    progress_cb(count, len(alphas))
            except Exception as e:
                logger.warning("upsert_alpha 失败 {}：{}", alpha.get("alpha_id"), e)
        return count

    def get_alpha(self, alpha_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM alphas WHERE alpha_id=?", (alpha_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_alpha_pnl(self, alpha_id: str) -> list[dict] | None:
        """读取已缓存的日度 PnL。"""
        row = self.get_alpha(alpha_id)
        if row and row.get("pnl_json"):
            try:
                return json.loads(row["pnl_json"])
            except Exception:
                return None
        return None

    def update_alpha_check(
        self,
        alpha_id: str,
        pc: float | None,
        failed_list: list[str] | None,
    ) -> str:
        """写 submission check 结果。返回 check_status（pass/warn/fail）。

        语义对齐原 machine_lib.check_submission：无 FAIL 即视为可用（gold_bag）；
        |PROD_CORRELATION|>0.7 但无 FAIL 记 warn（仍可用，只是警示）。
        """
        failed = [str(f) for f in failed_list] if failed_list else []
        # 修复：瞬时错误（网络/超时/ERR: 标记）不写成永久 FAIL——
        # 否则一次 504 就让 alpha 永远被否决，且不再自动重查
        transient = any(
            f.startswith("ERR:") or any(k in f.lower() for k in (
                "timeout", "timed out", "connect", "network", "ssl", "retry",
            ))
            for f in failed
        )
        if transient:
            # 瞬时错误：不落 FAIL，仅更新 checked_at（保持未查状态可重查）
            now = datetime.now(timezone.utc).isoformat()
            with self._lock:
                if pc is not None:
                    self._conn.execute(
                        """UPDATE alphas SET checked_at=?, check_pc=?
                           WHERE alpha_id=?""",
                        (now, pc, alpha_id),
                    )
                else:
                    # pc=None 时不覆盖已有 check_pc（数据流修复）
                    self._conn.execute(
                        """UPDATE alphas SET checked_at=? WHERE alpha_id=?""",
                        (now, alpha_id),
                    )
                self._conn.commit()
            # 统一返回 ''（瞬时错误不虚增 pass/fail 统计）
            return ""
        if failed:
            status = "fail"
        elif pc is not None and abs(pc) > 0.7:
            status = "warn"
        else:
            status = "pass"
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                """UPDATE alphas
                   SET check_pc=?, check_failed=?, check_status=?, checked_at=?
                   WHERE alpha_id=?""",
                (pc, ",".join(failed) if failed else None, status, now, alpha_id),
            )
            self._conn.commit()
        return status

    def list_golden_alphas(self, region: str | None = None, limit: int = 1000) -> list[dict]:
        """黄金包：submission check 无 FAIL 的 alpha（pass + warn，按 |sharpe| 降序）。

        对应原 notebook 的 gold_bag（check_submission 通过者集合）。
        region 非空时按区域过滤（修复：此前黄金包不过滤 region，列表与选中 Region 不符）。
        """
        sql = """SELECT * FROM alphas
                   WHERE check_status IN ('pass','warn')"""
        params: list[Any] = []
        if region:
            sql += " AND region=?"
            params.append(region)
        sql += " ORDER BY ABS(sharpe) DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def count_alphas(self, region: str | None = None) -> int:
        """库中 alpha 总数（顶部指标卡用）。"""
        sql = "SELECT COUNT(*) AS n FROM alphas"
        params: list[Any] = []
        if region:
            sql += " WHERE region=?"
            params.append(region)
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return int(row["n"]) if row else 0

    def count_golden_alphas(self) -> int:
        """黄金包（check 无 FAIL：pass + warn）数量（顶部指标卡用）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM alphas WHERE check_status IN ('pass','warn')"
            ).fetchone()
        return int(row["n"]) if row else 0

    def count_submittable(self) -> int:
        """待提交：已通过 check（pass/warn）且未提交过的 alpha 数（顶部指标卡用）。"""
        with self._lock:
            row = self._conn.execute(
                """SELECT COUNT(*) AS n FROM alphas a
                   WHERE a.check_status IN ('pass','warn')
                     AND NOT EXISTS (
                         SELECT 1 FROM submissions s
                         WHERE s.alpha_id = a.alpha_id AND s.ok = 1
                     )"""
            ).fetchone()
        return int(row["n"]) if row else 0

    def list_unchecked_alpha_ids(self, limit: int = 5000, min_sharpe: float = 1.58) -> list[str]:
        """从未做过 submission check 的 alpha_id（按 |sharpe| 降序，先查优质的）。

        v63 优化：只返回 IS 侧合格的（|sharpe| >= min_sharpe 且 fitness >= 1.0）——
        IS 不合格的提交时必被 LOW_SHARPE/LOW_FITNESS 硬卡拒绝，check submission 纯属浪费平台队列。
        min_sharpe 传 0 可恢复全量检查（手动场景）。
        """
        with self._lock:
            rows = self._conn.execute(
                """SELECT alpha_id FROM alphas
                   WHERE check_status IS NULL
                     AND ABS(sharpe) >= ? AND fitness >= 1.0
                   ORDER BY ABS(sharpe) DESC LIMIT ?""",
                (min_sharpe, limit),
            ).fetchall()
        return [r[0] for r in rows]

    def list_alphas(
        self,
        region: str | None = None,
        min_sharpe: float | None = None,
        limit: int = 200,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list[Any] = []
        if region:
            clauses.append("region=?")
            params.append(region)
        if min_sharpe is not None:
            clauses.append("ABS(sharpe)>=?")
            params.append(min_sharpe)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT * FROM alphas {where}
                    ORDER BY ABS(sharpe) DESC LIMIT ?""",
                params,
            ).fetchall()
        return [dict(r) for r in rows]

    def export_alphas_csv(self, path: str | Path) -> int:
        import csv
        rows = self.list_alphas(limit=10_000)
        if not rows:
            return 0
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        return len(rows)

    # -------- ai_batches（AI 协作批次，对账用） --------

    def next_batch_no(self) -> str:
        """生成下一个批次号：B + YYYYMMDD + 当天自增序号（B20260813-001）。"""
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        prefix = f"B{day}-"
        with self._lock:
            row = self._conn.execute(
                """SELECT MAX(batch_no) AS m FROM ai_batches
                   WHERE batch_no LIKE ?""",
                (prefix + "%",),
            ).fetchone()
        last = (row["m"] or "") if row else ""
        seq = int(last.rsplit("-", 1)[-1]) + 1 if last else 1
        return f"{prefix}{seq:03d}"

    def create_ai_batch(
        self,
        *,
        batch_no: str,
        producer: str = "",
        dataset_id: str = "",
        region: str = "",
        expression_count: int = 0,
        note: str = "",
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                """INSERT INTO ai_batches
                   (batch_no, producer, dataset_id, region, expression_count,
                    status, note, created_at, updated_at)
                   VALUES (?,?,?,?,?, 'pending', ?, ?, ?)""",
                (batch_no, producer, dataset_id, region, expression_count, note, now, now),
            )
            self._conn.commit()

    def update_ai_batch_status(self, batch_no: str, status: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                "UPDATE ai_batches SET status=?, updated_at=? WHERE batch_no=?",
                (status, now, batch_no),
            )
            self._conn.commit()

    def get_ai_batch(self, batch_no: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM ai_batches WHERE batch_no=?", (batch_no,),
            ).fetchone()
        return dict(row) if row else None

    def list_ai_batches(self, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM ai_batches ORDER BY created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # -------- AI 协作（v61）--------

    def get_ai_control(self) -> dict:
        """AI 控制状态（单行，不存在则初始化）。"""
        with self._lock:
            row = self._conn.execute("SELECT * FROM ai_control WHERE id=1").fetchone()
            if row is None:
                now = datetime.now(timezone.utc).isoformat()
                self._conn.execute(
                    """INSERT INTO ai_control (id, mode, auto_loop, max_rounds,
                       max_per_round, current_round, ai_state, stop_requested, updated_at)
                       VALUES (1, 'manual', 0, 5, 100, 0, '空闲', 0, ?)""",
                    (now,),
                )
                self._conn.commit()
                row = self._conn.execute("SELECT * FROM ai_control WHERE id=1").fetchone()
        return dict(row)

    def set_ai_control(self, **fields) -> None:
        """更新 ai_control（只更新传入字段）。"""
        allowed = {"mode", "auto_loop", "max_rounds", "max_per_round",
                   "current_round", "ai_state", "stop_requested"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        now = datetime.now(timezone.utc).isoformat()
        updates["updated_at"] = now
        cols = ", ".join(f"{k}=?" for k in updates)
        with self._lock:
            self._conn.execute(
                f"UPDATE ai_control SET {cols} WHERE id=1",
                list(updates.values()),
            )
            self._conn.commit()

    # -------- 平台元信息（v69）：跨进程共享（MCP 写 / GUI 读） --------

    def set_meta(self, key: str, value: str) -> None:
        """写一条平台元信息（UPSERT）。"""
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                """INSERT INTO platform_meta (key, value, updated_at)
                   VALUES (?,?,?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                (key, value, now),
            )
            self._conn.commit()

    def get_meta(self, key: str) -> str | None:
        """读一条平台元信息。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM platform_meta WHERE key=?", (key,)
            ).fetchone()
        return row["value"] if row else None

    def push_ai_command(self, command: str, payload: dict | None = None) -> int:
        """AI 侧提交命令（如 run_batch）。返回命令 id。"""
        import json as _json
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO ai_commands (command, payload, status, created_at) VALUES (?,?,?,?)",
                (command, _json.dumps(payload or {}, ensure_ascii=False), "pending", now),
            )
            self._conn.commit()
        return cur.lastrowid

    def pop_pending_ai_commands(self) -> list[dict]:
        """GUI 取待执行命令（pending → 保持 pending，执行后由 GUI 标记）。"""
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM ai_commands WHERE status='pending'
                   ORDER BY id ASC LIMIT 10""",
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_ai_command(self, cmd_id: int, status: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                "UPDATE ai_commands SET status=?, executed_at=? WHERE id=?",
                (status, now if status != "pending" else None, cmd_id),
            )
            self._conn.commit()

    def log_ai_timeline(self, message: str, level: str = "info") -> None:
        """GUI/AI 记录时间线（限流：最多保留 200 条）。"""
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                "INSERT INTO ai_timeline (ts, level, message) VALUES (?,?,?)",
                (now, level, message),
            )
            self._conn.execute(
                "DELETE FROM ai_timeline WHERE id NOT IN (SELECT id FROM ai_timeline ORDER BY id DESC LIMIT 200)"
            )
            self._conn.commit()

    def get_ai_timeline(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM ai_timeline ORDER BY id DESC LIMIT ?", (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def clear_ai_timeline(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM ai_timeline")
            self._conn.commit()

    def list_alphas_by_batch(self, batch_no: str, limit: int = 5000) -> list[dict]:
        """按批次号列出该批次的 alpha（按 |sharpe| 降序，含 check 状态）。"""
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM alphas WHERE batch_no=?
                   ORDER BY ABS(sharpe) DESC LIMIT ?""",
                (batch_no, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def count_unsubmitted_by_batch(self, batch_no: str) -> int:
        """某批次已回测成功但未提交过的 alpha 数（批次对账用）。"""
        with self._lock:
            row = self._conn.execute(
                """SELECT COUNT(*) AS n FROM alphas a
                   WHERE a.batch_no=?
                     AND NOT EXISTS (
                         SELECT 1 FROM submissions s
                         WHERE s.alpha_id = a.alpha_id AND s.ok = 1
                     )""",
                (batch_no,),
            ).fetchone()
        return int(row["n"]) if row else 0

    # -------- submissions（提交历史） --------

    def record_submission(
        self,
        alpha_id: str,
        *,
        status_code: int | None = None,
        message: str = "",
        ok: bool = False,
    ) -> int:
        """记录一次提交到 submissions 表。返回记录 id。"""
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO submissions
                   (alpha_id, status_code, message, ok, submitted_at)
                   VALUES (?,?,?,?,?)""",
                (alpha_id, status_code, message[:500], 1 if ok else 0, now),
            )
            self._conn.commit()
            return cur.lastrowid

    def list_submissions(self, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM submissions ORDER BY id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_submitted_alpha_ids(self) -> set[str]:
        """已提交过的 alpha_id 集合（用于去重标记）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT alpha_id FROM submissions WHERE ok=1"
            ).fetchall()
        return {r["alpha_id"] for r in rows}