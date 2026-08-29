"""任务调度器（QThreadPool）。

设计：
- 一个 batch = 多个 alpha（multi-sim 的 children）
- BatchScheduler 用 QThreadPool 控制并发 batch 数
- 每个 batch 是一个 Runnable，提交 + 轮询 + 持久化
- 支持 pause / resume / cancel 标志位
- 断点续跑：从 SQLite 读 pending simulations 继续
- 进度通过 Qt signal 回调给 UI（也支持无 Qt 模式用回调函数）
"""

from .runner import BatchScheduler, BatchResult, ProgressCallback

__all__ = ["BatchScheduler", "BatchResult", "ProgressCallback"]