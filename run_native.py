"""千寻 web 桌面模式（v81）—— pywebview 套壳启动器。

用法：
    python run_native.py

效果：
- 启 FastAPI 服务（线程里跑 uvicorn）
- 拉起原生 Edge 窗口，加载千寻 web
- 关闭窗口自动停服务（不残留进程、无控制台窗口）

打包成 exe：
    pyinstaller --noconfirm --windowed --name "千寻" ^
        --add-data "wq_web/templates;wq_web/templates" ^
        --add-data "wq_web/static;wq_web/static" ^
        --hidden-import uvicorn --hidden-import webview run_native.py
"""
from __future__ import annotations

import asyncio
import socket
import sys
import threading
import time
from pathlib import Path

# 项目根直接跑
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import uvicorn  # noqa: E402
import webview  # noqa: E402

from wq_web.server import app  # noqa: E402


def _pick_free_port() -> int:
    """找可用端口，避开 8090 偶尔被占用的情况。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> None:
    port = _pick_free_port()

    # 用 uvicorn.Server 实例（不进 install_signal_handlers，子线程会报错）
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port,
        log_level="warning", access_log=False,
    )
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]

    # 子线程跑 server，主线程跑 webview
    t = threading.Thread(target=server.run, daemon=True)
    t.start()

    # 等 server ready
    deadline = time.time() + 8
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                break
        except OSError:
            time.sleep(0.1)
    else:
        sys.stderr.write("千寻 web server 启动超时\n")
        sys.exit(1)

    # 260924 起「PnL 同步」入口已从指挥中心下线（整卡迁到模拟器页），
    # #sec-sync 锚点同时删除，这里改为落到「设置与配额」区
    url = f"http://127.0.0.1:{port}/#sec-concurrency"
    window = webview.create_window(
        title=f"千寻 web · v81 · :{port}",
        url=url,
        width=1480,
        height=920,
        min_size=(1024, 640),
        background_color="#0f1115",
        text_select=False,
        confirm_close=False,
    )
    # 关窗口 → 退出主进程 → daemon server 线程随之结束
    webview.start()
    # 让 server 有机会优雅退出（uvicorn 自身不会在子线程自己 stop）
    server.should_exit = True


if __name__ == "__main__":
    main()
