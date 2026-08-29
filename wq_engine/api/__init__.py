"""WQ BRAIN REST API 客户端。

封装 authentication / simulations / alphas / check 等接口，修复原 machine_lib 的：
- P0：明文账号密码硬编码（改环境变量 + 可选 keyring）
- P0：while True 无限重试（加最大次数 + 指数退避）
- P1：POST 失败静默丢任务（失败抛异常，让上层调度器决定重试或跳过）
- P1：搜索结果 count=100 硬编码截断
"""

from .client import APIClient, BrainClient, BrainClientError, AuthError, RateLimitError, SimulationError
from .config import BrainConfig

__all__ = [
    "APIClient",
    "BrainClient",
    "BrainClientError",
    "AuthError",
    "RateLimitError",
    "SimulationError",
    "BrainConfig",
]