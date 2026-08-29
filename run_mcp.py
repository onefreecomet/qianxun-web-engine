"""千寻 web 引擎 · MCP Server 入口（Agent 对接）。

用法：
    python run_mcp.py                          # 默认 stdio（推荐给 WorkBuddy 等桌面 agent）
    python run_mcp.py --transport sse          # SSE，供远程调试
    python run_mcp.py --transport streamable-http --port 8765

说明：
- stdio 模式：agent 通过 MCP 客户端以子进程方式拉起本脚本，走 JSON-RPC over stdio
- sse / streamable-http：网络模式，供 MCP 客户端远程连接
- 凭据：环境变量 WQ_USERNAME / WQ_PASSWORD，缺失时回退系统 keyring（service=alpha-machine）
"""
from __future__ import annotations

import sys
from pathlib import Path

# 保证从任意 cwd 启动都能找到 wq_engine 包
sys.path.insert(0, str(Path(__file__).resolve().parent))

from wq_engine.mcp_server import main  # noqa: E402


if __name__ == "__main__":
    sys.exit(main())
