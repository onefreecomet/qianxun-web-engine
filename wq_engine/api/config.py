"""API 客户端配置与凭据加载。"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class BrainConfig:
    """WQ BRAIN API 客户端配置。

    凭据加载优先级（从高到低）：
      1. 环境变量 WQ_USERNAME / WQ_PASSWORD
      2. 系统 keyring（预留扩展点，本版本仅环境变量）
      3. 抛 AuthError（必须配置）
    """

    base_url: str = "https://api.worldquantbrain.com"
    username: str = ""
    password: str = ""

    timeout: float = 30.0
    max_retries: int = 5
    backoff_factor: float = 1.5
    backoff_max: float = 60.0

    page_size: int = 50
    search_max_results: int = 500  # 替代原 100 硬编码

    def __post_init__(self) -> None:
        # 参数校验：max_retries<=0 会立即失败，timeout<=0 会无限阻塞
        if self.max_retries < 1:
            raise ValueError(f"max_retries 必须 >= 1，实际 {self.max_retries}")
        if self.timeout <= 0:
            raise ValueError(f"timeout 必须 > 0，实际 {self.timeout}")
        if self.backoff_max <= 0:
            raise ValueError(f"backoff_max 必须 > 0，实际 {self.backoff_max}")

    @classmethod
    def from_env(cls) -> "BrainConfig":
        """从环境变量加载配置；缺失时回退系统 keyring（GUI 登录时已存）。

        keyring 约定（与 login_dialog 一致）：
        - service="alpha-machine"，key="wq_username" 存当前账号
        - key=<username> 存该账号密码
        """
        username = os.environ.get("WQ_USERNAME", "").strip()
        password = os.environ.get("WQ_PASSWORD", "").strip()
        if not username or not password:
            try:
                import keyring
                u = keyring.get_password("alpha-machine", "wq_username")
                if u:
                    username = u
                    password = keyring.get_password("alpha-machine", u) or ""
            except Exception:
                pass
        return cls(username=username, password=password)

    def is_authenticated(self) -> bool:
        return bool(self.username and self.password)