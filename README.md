# 千寻 web 引擎（Qianxun Web Engine）

本地运行的 WorldQuant BRAIN alpha 回测引擎，带 Web 指挥中心界面，并原生支持 **AI Agent（MCP）对接**。

- **Web 界面**：浏览器里管理 AI 批次、看回测进度、同步 PnL、写 Alpha 备忘录、存提示词库
- **MCP Server**：任何支持 MCP 协议的 AI Agent（如 WorkBuddy / Claude Desktop / Cursor）都能直接调用来做回测
- **零凭据落盘**：BRAIN 账号走环境变量或系统 keyring，代码里没有密码

---

## 特性

| 模块 | 说明 |
|---|---|
| AI 批次 | 提交表达式批量回测，WebSocket 实时进度推送，断点续跑 |
| 并发控制 | 全局并发批数 + 并发槽，可随时调整 |
| 配额显示 | 顶部实时显示 BRAIN 每日回测配额（响应头真实值） |
| PnL 同步 | 拉取 PnL、本地计算 Self/PPA 相关性（手动触发，不自动烧资源） |
| Alpha 备忘录 | 按 region 分组的候选 alpha 跟踪，金字塔主题、提交状态、手写备注、直接提交 |
| 提示词库 | 可自定义名字的提示词列表，一键复制 |
| MCP Server | 7 个工具，覆盖登录 / 提交 / 等待 / 分析全流程 |

---

## 架构

```
┌──────────────┐    ┌──────────────────────┐    ┌────────────────────┐
│   浏览器      │    │  本仓库（Python）      │    │  WorldQuant BRAIN  │
│  Web 界面     │───▶│  FastAPI (8090)      │───▶│  api.worldquant    │
│  或 AI Agent  │    │  + WebSocket 进度     │    │  brain.com         │
└──────────────┘    │  + MCP Server (stdio) │    └────────────────────┘
                    │  + SQLite 本地存储     │
                    └──────────────────────┘
                         │
                    data/alpha_machine.db
                    （首次运行自动创建）
```

两个入口，跑一个就能用：

```text
run_web.py   → Web 指挥中心（浏览器操作）
run_mcp.py   → MCP Server（AI Agent 对接，stdio 默认）
```

两者共享同一个 SQLite 数据库，可以同时启动：Web 负责看进度，Agent 负责提交和分析。

---

## 快速开始

### 1. 安装依赖

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 配置凭据

二选一：

```bash
# 方式 A：环境变量（推荐，临时生效）
export WQ_USERNAME="你的BRAIN账号"
export WQ_PASSWORD="你的密码"
python run_web.py

# 方式 B：系统 keyring（持久，与千寻 GUI 共享）
python -c "import keyring; keyring.set_password('alpha-machine', 'wq_username', '你的账号'); keyring.set_password('alpha-machine', '你的账号', '你的密码')"
```

> 代码里没有任何硬编码凭据。环境变量缺失时自动回退 keyring，两者都没有则调用 BRAIN API 时报认证错误。

### 3. 启动

```bash
python run_web.py                # Web 界面 → http://127.0.0.1:8090
python run_mcp.py                # MCP Server（stdio，等待 agent 连接）

# 自定义端口 / 指定已有数据库
QW_PORT=9000 python run_web.py
QIANXUN_DB=/path/to/alpha_machine.db python run_mcp.py
```

### 4. 数据库

- 首次运行自动在项目根 `data/` 下创建空库并建表，无需手动初始化
- 已在使用千寻 GUI（AlphaMachine）？把 `QIANXUN_DB` 指到它的数据库（形如 `dist_v71/AlphaMachine/data/alpha_machine.db`），Web / Agent 就能直接看到历史批次和 alpha，两边数据互通

---

## 与 AI Agent 对接（核心）

### 方式一：MCP 协议（推荐）

MCP（Model Context Protocol）是 AI Agent 与本地工具通信的标准协议。本项目的 `run_mcp.py` 启动一个 MCP Server，agent 以子进程方式拉起它，然后就能直接调用回测工具。

#### WorkBuddy 配置示例

在 `~/.workbuddy/mcp.json` 的 `mcpServers` 里加一项（路径换成你的实际位置）：

```json
{
  "mcpServers": {
    "qianxun": {
      "command": "python",
      "args": ["C:/path/to/qianxun-web-engine/run_mcp.py"],
      "env": {
        "WQ_USERNAME": "你的账号",
        "WQ_PASSWORD": "你的密码"
      }
    }
  }
}
```

> Windows 下如果 `python` 不在 PATH，用 venv 里的完整路径，例如 `C:/path/to/.venv/Scripts/python.exe`。
> 配好后在 WorkBuddy 的连接器管理页面对该项点「信任」即可生效。

#### 工具清单

| 工具 | 参数 | 作用 |
|---|---|---|
| `qianxun_login` | 无 | 登录测试，返回账号和当前数据库路径 |
| `qianxun_submit` | `json_path`, `producer="阿法"`, `batch_size=8`, `concurrent=3`, `db_path=""`, `no_backfill=false` | 提交一批表达式回测 |
| `qianxun_wait` | `batch_no`, `timeout=3600` | 阻塞等待批次跑完（默认最多 1 小时） |
| `qianxun_status` | `batch_no=""`（空则全局） | 查批次或全局状态 |
| `qianxun_analyze` | `batch_no`, `limit=50` | 分析回测结果，输出 Sharpe / Fitness / Turnover 等 |
| `qianxun_radar` | `batch_no`, `passed=0`, `total=0` | 检查通过率与风险护栏 |
| `qianxun_resume` | `batch_no` | 断点续跑：补跑 pending 的模拟 |

#### submit 的 JSON 文件格式

`qianxun_submit` 接收一个 JSON 文件路径，格式如下（表达式和 settings 同 BRAIN 平台规则）：

```json
{
  "settings": {
    "instrumentType": "EQUITY",
    "region": "USA",
    "universe": "TOP3000",
    "delay": 1,
    "decay": 4,
    "neutralization": "INDUSTRY",
    "truncation": 0.08,
    "pasteurization": "ON",
    "unitHandling": "VERIFY",
    "nanHandling": "OFF",
    "language": "FASTEXPR",
    "visualization": "OFF"
  },
  "expressions": [
    {"expression": "rank(ts_delta(close, 5))", "decay": 4},
    {"expression": "-rank(ts_delta(close, 5))", "decay": 4}
  ]
}
```

#### Agent 完整回测流程（对话示例）

一个 AI Agent 完成「生成 → 回测 → 分析」的典型调用序列：

```
1. qianxun_login                     # 确认连接和账号
2. 生成表达式，写入 /tmp/batch1.json
3. qianxun_submit("/tmp/batch1.json", concurrent=3)
   → 返回 {"ok": true, "batch_no": "B128", ...}
4. qianxun_wait("B128")              # 阻塞直到跑完（内部自动轮询）
5. qianxun_analyze("B128")           # 拿到每个 alpha 的指标
6. qianxun_radar("B128", passed=8, total=8)  # 检查提交门槛
```

> 注意：`qianxun_submit` 里的 `concurrent` 是 MCP 调用自身的并发，与 Web 界面里的全局并发（`/api/concurrency`）相互独立。

### 方式二：HTTP API

Web 服务（`run_web.py`）同时暴露 REST API，适合脚本 / curl 直接调用。

#### 端点总览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | Web 界面 |
| GET | `/api/stats` | 全局统计（alpha 数 / 批次 / 回测数） |
| GET | `/api/batches?limit=50` | 批次列表 |
| GET | `/api/batches/{batch_no}` | 批次详情 |
| POST | `/api/batches` | 提交 AI 批次（见下方 Body 示例） |
| GET | `/api/alphas` | alpha 列表（可按 batch_no 过滤） |
| GET | `/api/alphas/{alpha_id}` | 单个 alpha 详情 |
| GET | `/api/concurrency` | 读取全局并发设置 |
| POST | `/api/concurrency` | 修改并发（`{"concurrent":8,"sim_slots":6}`） |
| GET | `/api/quota` | 今日平台配额（limit / remaining / reset） |
| POST | `/api/sync/pnl` | 启动 PnL 同步 |
| GET | `/api/sync/pnl/status` | 同步进度 |
| POST | `/api/sync/pnl/stop` | 停止同步 |
| POST | `/api/corr/compute` | 单个 alpha 算 Self/PPA 相关性 |
| GET/POST | `/api/memo` | 备忘录列表 / 添加 |
| POST | `/api/memo/sync` | 同步全部 alpha 提交状态 |
| POST | `/api/memo/submit` | 直接提交 alpha（真实操作） |
| POST | `/api/memo/note` | 写备注 |
| POST | `/api/memo/themes` | 设置金字塔主题 |
| DELETE | `/api/memo/{alpha_id}` | 从备忘录删除 |
| GET/POST | `/api/prompts` | 提示词库列表 / 增删改 |
| WS | `/ws/progress` | 回测进度实时推送 |

#### 提交回测（curl 示例）

```bash
curl -X POST http://127.0.0.1:8090/api/batches \
  -H "Content-Type: application/json" \
  -d '{
    "settings": {"region": "USA", "decay": 4, "neutralization": "INDUSTRY"},
    "expressions": [{"expression": "rank(ts_delta(close, 5))"}],
    "producer": "my-agent",
    "batch_size": 8,
    "no_backfill": false
  }'
```

#### 订阅实时进度（WebSocket）

```python
# 需要 websockets 库
import asyncio, json, websockets

async def watch():
    async with websockets.connect("ws://127.0.0.1:8090/ws/progress") as ws:
        while True:
            msg = json.loads(await ws.recv())
            print(msg)  # {type: "batch"|"sim"|"done", batch_no, alpha_id, ...}

asyncio.run(watch())
```

---

## 常见问题

**Q：没有装 pywebview 能用吗？**
能。pywebview 只服务于可选的桌面窗口模式（`run_native.py`），浏览器访问不需要它。

**Q：回测报 401 / 认证错误？**
凭据没配好。先 `python run_mcp.py` 模式下调 `qianxun_login` 看返回，或确认环境变量是否在当前终端生效。

**Q：如何让 Agent 和千寻 GUI 看到同一批数据？**
把 `QIANXUN_DB` 指向 GUI 的数据库文件（`dist_vNN/AlphaMachine/data/alpha_machine.db`），两边共用。

**Q：MCP 端口冲突？**
stdio 模式不占端口。`--transport sse` / `streamable-http` 模式默认 8765，可用 `--port` 改。

---

## 许可证

MIT
