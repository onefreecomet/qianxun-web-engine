"""千寻 web v80 启动脚本。

用法：
    python wq_web/run_web.py
    # 或
    python -m wq_web.run_web

默认 8090 端口；自定义：QW_PORT=9000 python wq_web/run_web.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uvicorn  # noqa: E402

from wq_engine.mcp_server import _latest_db  # noqa: E402


def main() -> None:
    port = int(os.environ.get("QW_PORT", "8090"))
    db = _latest_db()
    print("=" * 60)
    print("千寻 web · v80 alpha")
    print("=" * 60)
    print(f"数据源：  {db}")
    print(f"访问地址：http://127.0.0.1:{port}")
    print(f"WebSocket：ws://127.0.0.1:{port}/ws/progress")
    print(f"按 Ctrl+C 停止")
    print()
    uvicorn.run(
        "wq_web.server:app",
        host="127.0.0.1",
        port=port,
        log_level="info",
        reload=False,
    )


if __name__ == "__main__":
    main()