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
        # ⚠️ 260928 定案：必须 access_log=False，否则**每个 HTTP 请求恒定慢 2 秒**。
        # 实测（本机 Windows）：开着时 /api/quota 与静态文件都是 2.03s，关掉后 0.003s（约 700 倍）。
        # 已排除：网络（connect 仅 0.5ms）、curl、中间件（本服务没有）、
        # root logger 的 stderr handler 本身（最小 FastAPI 手工挂同样 handler 并不慢）。
        # 只在**走本脚本**时复现：脚本顶层 `from wq_engine.mcp_server import _latest_db`
        # 会在 uvicorn 配置 logging **之前**给 root 挂上 stderr handler，
        # 于是 access log 被写两遍（uvicorn.access 自带 handler + propagate 到 root）。
        # 对照：`python -m uvicorn wq_web.server:app --log-level info` 不会慢。
        # 排障要看访问日志时临时改 True，用完记得关回来。
        access_log=False,
        reload=False,
    )


if __name__ == "__main__":
    main()