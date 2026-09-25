# AlphaMachine — WQ BRAIN 桌面端

> WorldQuant BRAIN alpha 批量挖掘可视化客户端（PySide6）

## 项目信息

| 字段 | 内容 |
|------|------|
| **项目编号** | project026 |
| **开始日期** | 2026-08-07 |
| **完成日期** | 进行中 |
| **状态** | 🟡 进行中（MVP 第一版） |

## 项目目标

把 `D:\ALPHA GOGOGO\consultant\machine_lib.py` 的笔记本手动流水线改造为桌面客户端：
- PySide6 桌面 GUI，按"基本/一阶/二阶/三阶/高级"五 Tab 组织配置
- 异步多线程批量调度模拟任务，支持暂停/续跑/取消
- SQLite 本地持久化所有任务、模拟、alpha 指标，断点续跑不再丢
- 重构原 machine_lib.py 的死代码和安全问题（明文凭据、while True 无限重试、POST 失败丢任务等）

## 文件夹说明

- `inputs/`：输入配置（YAML / JSON）
- `outputs/`：运行结果（导出的 alpha 列表 CSV、截图、日志）
- `notes/`：架构笔记、决策记录、API 备忘
- `logs/`：运行日志（按日期）
- `wq_engine/`：核心引擎包
  - `api/`：WQ BRAIN REST API 客户端（重试、限流、token 缓存）
  - `factories/`：表达式工厂（一阶、二阶、三阶）
  - `scheduler/`：任务调度（QThreadPool）
  - `storage/`：SQLite 持久化
  - `ui/`：PySide6 界面

## 凭据配置（安全）

**不要把账号密码写在代码里**。本项目凭据通过环境变量传入：

```bash
# Windows PowerShell
$env:WQ_USERNAME = "your@email.com"
$env:WQ_PASSWORD = "your_password"
```

UI 启动后未读到环境变量时弹窗提示输入，可选保存到系统 keyring（`keyring` 包）。

## 运行方式（MVP 阶段）

### 方式一：直接双击 exe（推荐）

`dist/AlphaMachine.exe`（约 49MB，已打包 PySide6，双击即用，无需装 Python）：

- 首次启动弹出登录框，输入邮箱密码（可勾选保存到系统 keyring）
- 数据自动存在 exe 旁边 `data/`（SQLite）和 `inputs/`，不会丢失
- 重新打包：`python scripts/build_exe.py`（目录模式加 `--onedir`）

### 方式二：源码运行

```bash
# 命令行跑一阶流水线（无需 UI，先验证引擎）
cd project026_AlphaMachine
uv sync
uv run python -m wq_engine.cli run --config inputs/first_order_usa.yaml

# 启动桌面 GUI
uv run python -m wq_engine.ui.main_window
```

## 积分签到页（web · `/credits`）的使用与依赖

> ### ⚠️ 这一页不是纯网页功能，它依赖本机安装过 WorkDaddy 桌面客户端
>
> **没装 WorkDaddy**：`/credits` 拿不到任何数据 —— 健康检查返回
> `{"ok":false,"dataDir":null,"degradedUsable":false}`，账号接口返回
> `503 {"error":"WorkDaddy 数据目录里没有可用的账号备份"}`，页面显示「未连接」红条。
> 但页面本身仍能打开（HTTP 200），**其余页面（指挥中心 `/`、模拟器 `/simulator`）
> 完全不受影响**，不会因此报错或起不来（已实测：三页 + `/api/quota`、`/api/batches`、
> `/api/stats` 全部 200，启动日志零错误）。
>
> **装过但没运行**：仍可用，但会降级（见下表），代价是「今日用量」不可用、
> token 过期时需回客户端续期。
>
> 也就是说：**只想要 alpha 挖掘与回测功能，可以不装 WorkDaddy**；
> 想用积分签到页，才需要它。

### 这一页做什么

把本机 WorkDaddy 客户端里**已登录的账号**读出来，一屏展示每个账号的积分余额、
过期时间与「今日用量」，并支持一键批量领取每日签到积分。

### 依赖什么：两条取数通道

| | 主通道 | 降级通道 |
|---|---|---|
| 触发条件 | WorkDaddy 客户端**正在运行** | 客户端**装过但没运行** |
| 取数方式 | 转发 WorkDaddy daemon 本地 API（`127.0.0.1:<ui-port>`，`x-workdaddy-token` 认证） | 直接读 `accounts/*.info` 里的 `accessToken`，直连官方接口 |
| 页面状态 | 绿点 `已连接 · N 账号 · :端口` | 黄点 `降级模式` |
| 代价 | 无 | **「今日用量」不可用**；token 过期必须**回客户端续期**，本页不会替你刷新 |

两条通道都要求存在这个目录（Windows）：

```
%APPDATA%\WorkDaddy\
├── accounts\*.info      # 账号与登录凭据 —— 两条通道都要读
├── .api-token           # 主通道：daemon 本地 API 令牌
└── ui-port.json         # 主通道：daemon 监听端口
```

所以**最小要求是「WorkDaddy 装过、且至少登录过一个账号」**；
想要完整功能（含今日用量）则需要客户端正在运行。

### 已知限制

- **仅 Windows**。数据目录定位写死了 `%APPDATA%\WorkDaddy` 与
  `~/AppData/Roaming/WorkDaddy`（见 `wq_web/workdaddy_proxy.py` 的 `_candidate_data_dirs()`），
  macOS / Linux 上这一页不可用。
- **需要能直连官方接口**。降级通道会访问 `www.workbuddy.cn`、`www.codebuddy.cn`
  等域名；受限网络下签到会失败。
- **不自动续期 token**。过期只报「登录身份过期」，需回 WorkDaddy 重新登录。
  这是**刻意**的：刷新 token 要 POST 官方 auth 接口并回写 `.info`，属于越界操作。
- **不写 WorkDaddy 的任何状态**。本页只读它的配置，不改账号状态、不写它的签到缓存。
  降级通道没有每日缓存，重复点「领取」会重复打接口；官方对已签到返回
  `10001 今天已签到`，幂等安全，只是多一次请求。
- **重复运行会共存但不冲突**：daemon 与降级通道各有自己的判定，
  客户端稍后启动不会导致数据错乱。

### 安全说明

- access token **只在进程内存里拼请求头**，不落盘、不打印、不写日志。
- 本页不采集、不上传任何凭据到第三方；也不修改 WorkDaddy 的配置或账号状态。

### 排障对照表

页面状态由 `/api/xgj/health` 的返回字段决定，按信号定位：

| 页面显示 | 接口信号 | 原因 | 处理 |
|---|---|---|---|
| 红条 ·「未连接」+ `未找到 WorkDaddy 数据目录` | `dataDir: null`、`degradedUsable: false` | 没装 WorkDaddy，或装在非默认位置 | 安装并登录 WorkDaddy 客户端 |
| 红条 ·「未连接」+ 其他 daemon 错误 | `degradedUsable: false` | 装过但没运行，且没有可用账号备份 | 启动 WorkDaddy 客户端 |
| 黄点 ·「降级模式」 | `degradedUsable: true`、`backupCount: N` | 客户端没运行，走的是直连通道 | 想恢复「今日用量」就启动客户端 |
| 某账号「登录身份过期」 | — | 该账号 token 过期 | 回 WorkDaddy 重新登录该账号 |
| 签到报网络错误 | — | 官方接口不可达 | 检查网络/代理是否放行上述域名 |

> 自查接口（只读，返回上面三态所需的全部字段）：
> `curl http://127.0.0.1:8090/api/xgj/health`
> 字段含义：`ok`（daemon 是否可达）、`dataDir`（定位到的数据目录，`null` = 没找到）、
> `daemonPort`、`hasToken`、`error`、`degradedUsable`（降级通道是否可用）、`backupCount`（可用账号备份数）。

## 进展记录

- [2026-08-11] 全量代码审查（4 路并行）发现 47 项问题并全部修复（git commit c152256）：
  - 高危 10 项：排序后行号错位导致提交/回测送错 alpha（3 处，改为按表格第 0 列读 alpha_id）、
    二阶/三阶切 Region 不刷新数据源（旧 Region 表达式按新 Region 提交）、
    runner 裸 HTTPStatusError 杀线程（sim 卡 submitted）、运行中重复启动换掉 scheduler、
    流水线 stop 按钮悬挂、SQLite REPLACE 抹 check 结果（改 UPSERT）
  - 中危 15 项：client 线程安全/Retry-After 容错/401 退避、failed 可重试（断点续跑真正生效）、
    取消语义 cancelled、线程池替代每 batch 一裸线程、API Key 掩码+可清除、
    PnL 缓存不被覆盖、closeEvent 完整清理等
  - 低危 22 项：year 默认当前年、get_alphas 去重、评分类型容错、字段提取、
    providers 分数解析容错、文案修正等
  - 测试：修复 3 个陈旧测试（引用已移除功能的幽灵类/属性），12 套全量回归全绿


- [2026-08-07] 项目启动，MVP 第一版完成：引擎层（api/factories/scheduler/storage）+ UI 层（主窗口/登录/一阶 Tab）+ CLI + 端到端测试全部通过
- [2026-08-07] exe 打包完成：PyInstaller 单文件 `dist/AlphaMachine.exe`（49MB），启动测试通过，数据目录落在 exe 旁

## 主要交付物（MVP + v2 + v3）

### MVP（第一版）
- [x] wq_engine.api（重试、限流、凭据环境变量 + keyring）
- [x] wq_engine.factories（一阶，消死代码）
- [x] wq_engine.storage（SQLite 三表 + 迁移）
- [x] wq_engine.scheduler（并发调度 + 断点续跑 + 暂停/取消）
- [x] PySide6 主窗口骨架 + 登录 + 一阶 Tab
- [x] CLI 命令行入口
- [x] 端到端测试（tests/test_e2e_engine.py，Mock API 验证全链路）

### v2（第二版）
- [x] 二阶 Tab（group 算子 × 分组，区域分组清单去重修复）
- [x] 三阶 Tab（trade_when，区域事件死代码修复：USA 舆情等按区域生效）
- [x] 高级 Tab（get_alphas 拉取 + 指标回填 + submission check + PnL 缓存）
- [x] 结果可视化（matplotlib 嵌入：sharpe 分布直方图 + PnL 曲线，中文字体适配）
- [x] 任务面板暂停 / 继续 / 取消按钮（按钮状态机）
- [x] v2 集成测试（tests/test_e2e_v2.py：剪枝/二阶/三阶/回填/暂停取消全通过）

### v3（第三版）
- [x] Alpha 推荐评分引擎（wq_engine/scoring.py：sharpe/fitness/turnover/margin 加权 + 否决项）
- [x] 推荐 Tab（评分排序表 + 阈值配置 + 自动/批量 Submission Check）
- [x] 一键提交（submit API + **二次确认对话框，绝不静默提交** + 逐条回传状态）
- [x] 导出报告（Markdown 推荐报告 + CSV，输出到 outputs/）
- [x] v3 集成测试（tests/test_e2e_v3.py：评分/否决/提交/报告/边界全通过）

### v4（第四版，工程完善）
- [x] 提交历史记录（submissions 表：alpha_id/时间/状态/消息，提交自动落库 + 查看弹窗）
- [x] 推荐表标记已提交 alpha（📤 已提交）
- [x] 自动 PnL 巡检（高级 Tab「批量拉取 PnL 前 50」，QThread + 进度 + 缓存落库）
- [x] 推荐 Tab check 改 QThread（修复 v3 自动 check 卡 UI 的已知限制）
- [x] 多账号切换（keyring 分账号存储 + 登录框历史账号下拉 + 自动填充密码）
- [x] v4 集成测试（tests/test_e2e_v4.py：提交落库/PnL 巡检/check 异步/keyring 容错全通过）

### v7（阶段导出版）
- [x] 按阶段手动导出：高级 Tab「按阶段导出通过的 Alpha」下拉（一阶/二阶/三阶）+ 导出 CSV 按钮
- [x] 只导出回测成功（completed）的 alpha，失败不计入；LEFT JOIN alphas 表带出指标（sharpe/fitness/turnover/margin 等）
- [x] 手动触发（非自动），弹保存对话框选路径
- [x] 阶段导出测试（tests/test_export_kind.py：一阶/二阶过滤、失败排除、指标联表、CSV 列全通过）

### v6（去重版）
- [x] 表达式级去重（simulations 表 expr_key 指纹：表达式+settings 哈希，跨任务记忆）
- [x] 一阶 Tab 增加「跳过已回测表达式」开关 + 「随机采样 N 个」输入（从未回测池采样，杜绝重复回测浪费配额）
- [x] 换 settings（decay/中性化/region 等）重新回测不算重复（指纹含 settings）
- [x] 旧库自动迁移（expr_key 列 + 索引）
- [x] 去重测试（tests/test_dedup.py：指纹稳定/去重/采样/storage 集成全通过）

### v5（打磨版）
- [x] 统一深色主题（QSS 全局样式：深色面板/表格/Tab/按钮，主操作按钮高亮）
- [x] 提交确认界面增强：逐条可勾选对话框（显示排名/分数/sharpe/fitness/turnover，全选/全不选）
- [x] PnL 定时巡检（QTimer 自动触发，间隔可配置 1-720 分钟，未登录自动停止）
- [x] v5 集成测试（tests/test_e2e_v5.py：主题/确认框勾选/定时器全通过）

## 测试

```bash
# 端到端引擎测试（Mock API，无需凭据/网络）
python tests/test_e2e_engine.py
python tests/test_e2e_v2.py
python tests/test_e2e_v3.py
python tests/test_e2e_v4.py
python tests/test_e2e_v5.py
```

## 后续迭代

- v6 候选：一键提交批量确认界面增强（逐条可勾选）、自动 PnL 巡检定时任务、统一主题样式