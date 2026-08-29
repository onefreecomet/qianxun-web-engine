"""SQLite 本地持久化层。

表结构：
- task_runs：任务批次（一阶/二阶/三阶），存配置和汇总状态
- simulations：单个模拟，存表达式、settings、alpha_id、状态、进度
- alphas：拉取的 alpha 指标，支持查询和导出

线程安全：所有写入通过 self._lock 串行化（SQLite 单连接模式）。
"""

from .database import Storage, StorageError

__all__ = ["Storage", "StorageError"]