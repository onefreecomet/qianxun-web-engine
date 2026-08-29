"""千寻 web 引擎 · Web 指挥中心入口。

用法：
    python run_web.py                # 默认 8090 端口
    python run_web.py --port 9000    # 自定义端口
    QW_PORT=9000 python run_web.py   # 或环境变量

启动后：
    Web 界面     http://127.0.0.1:8090
    WebSocket    ws://127.0.0.1:8090/ws/progress
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# 保证从任意 cwd 启动都能找到 wq_web / wq_engine 包
sys.path.insert(0, str(Path(__file__).resolve().parent))

import uvicorn  # noqa: E402

from wq_engine.mcp_server import _latest_db  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="千寻 web 引擎 · Web 指挥中心")
    parser.add_argument("--port", type=int, default=int(os.environ.get("QW_PORT", "8090")))
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    db = _latest_db()
    print("=" * 60)
    print("千寻 web 引擎 · Web 指挥中心")
    print("=" * 60)
    print(f"数据源：   {db}")
    print(f"Web 界面： http://{args.host}:{args.port}")
    print(f"WebSocket：ws://{args.host}:{args.port}/ws/progress")
    print("按 Ctrl+C 停止")
    print()
    uvicorn.run(
        "wq_web.server:app",
        host=args.host,
        port=args.port,
        log_level="info",
        reload=False,
    )


if __name__ == "__main__":
    main()
