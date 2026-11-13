<div align="center">
  <img src="docs/RADE-banner.png" alt="RADE" width="560">
  <br><br>
  <a href="README.md">English</a> &middot; <b>简体中文</b>
</div>

RADE 是一个开源的轻量级个人 Agent 应用。它提供本地 Workspace 操作、会话持久化、上下文管理、长期记忆、计划、子 Agent 和 Skill、Plugin、MCP 扩展能力，并提供 TUI 与 GUI。

```text
TUI ────────────────── Bridge ── agent_runtime ── agent_core
RADE-server（常驻宿主）
├─ HTTP / WebSocket── GUI
└─ Session Host ── Bridge ── agent_runtime ── agent_core
```

- `agent_core` 提供与前端无关的 Agent 机制。
- `agent_runtime` 读取应用配置并把 Core 组装成可运行实例。
- Bridge 作为独立进程，是前端驱动 Runtime 的唯一通道。
- TUI 与 GUI通过 Bridge 协议共享执行状态和交互语义；Bridge 子进程通道使用 newline-delimited JSON。

## 主要能力

- **直接本地执行**：Shell 和命令 Hook 以当前用户的宿主权限运行，工作目录为当前 Workspace。内置文件 Tool 在 Workspace 内解析操作路径。
- **完整 Agent Loop**：支持流式响应、Tool 批次、上下文压缩、steer、取消、计划和后台 Job。
- **可恢复 Session**：对话和执行事件写入 append-only JSONL Journal，TUI 与 GUI 可读取同一类 Session。
- **长期记忆**按 Workspace 保存偏好、事实与决策。
- **模型与媒体**通过 OpenAI SDK 使用 Chat Completions 或 Responses。子 Agent、上下文压缩和记忆使用当前主 Agent 选中的 Provider。支持视觉输入的模型可以读取 Workspace 图片。
- **扩展系统**：支持 standalone Skill、Plugin 提供的 Skill / Agent / MCP / [Hook](docs/plugin-hooks.md) 组件，以及独立 MCP Server。
- **两个交互入口**分别是终端中的 TUI 和浏览器中的 GUI。GUI 提供多会话管理、附件、文件树、设置和记忆。

## 快速开始

环境要求：Python >= 3.11、Node.js >= 22 和 [uv](https://docs.astral.sh/uv/)。

```bash
git clone https://github.com/EvannZhongg/B05_RADE.git
cd B05_RADE
npm install
npm run build
uv tool install --editable ".[server]"
```

需要通过项目环境使用 GUI 的 HTTP / WebSocket 接入时，安装 server extra：

```bash
uv sync --extra server
```

先在项目根目录启动 TUI。

```bash
RADE
```

首次启动会在项目根目录初始化 `.RADE/`。退出 TUI 后，创建 `.RADE/.env` 并填入默认 Provider 所需的环境变量，例如：

```dotenv
DASHSCOPE_API_KEY=your-api-key
```

配置完成后重新启动 TUI：

```bash
RADE
```

也可以启动 GUI 服务。

```bash
RADE-server
```

GUI 默认监听 <http://127.0.0.1:8736>。

`RADE-server` 管理 HTTP 服务、活动 Session 和 Bridge。同一配置目录通过进程锁保证单实例。

## 模块文档

第三方前端可通过已安装的 `RADE-bridge` 命令使用
[公开 stdio 协议](docs/bridge-protocol.md)，或使用轻量的
[`@rade/client` TypeScript SDK](interfaces/client/README.md)。

公开契约：

| 文档 | 负责范围 |
| --- | --- |
| [`docs/bridge-protocol`](docs/bridge-protocol.md) | 第三方客户端的 stdio 传输、命令、事件、Runtime phase、transcript 检查点与错误规则 |
| [`docs/bridge-api-design`](docs/bridge-api-design.md) | 哪些消息属于公开或内部、各自的所有者以及版本决策 |
| [`docs/plugin-hooks`](docs/plugin-hooks.md) | 命令 Hook 的声明、检查点、调用载荷与决策 |
| [`docs/plugin-interop`](docs/plugin-interop.md) | Plugin 组件的装配、静态校验、错误隔离与重新装配 |

Runtime 层：

| 文档 | 负责范围 |
| --- | --- |
| [`agent_core`](agent_core/README.md) | Agent Loop、上下文、Session、Tool、执行、记忆、计划、Job、Provider、Skill 与 MCP 机制 |
| [`agent_runtime`](agent_runtime/README.md) | 配置、Prompt、Plugin 发现、Runtime 装配、Execution Plane、Session 绑定和 Turn 入口 |

前端与 SDK：

| 文档 | 负责范围 |
| --- | --- |
| [`interfaces`](interfaces/README.md) | 共享宿主模块、启动入口、Bridge 进程与通道附着，以及允许复用 Core 的边界 |
| [`interfaces/bridge`](interfaces/bridge/README.md) | 独立 Bridge 进程、协议路由、事件转换和前端交互转发 |
| [`interfaces/protocol`](interfaces/protocol/README.md) | TypeScript 协议包：导出面、构建与改动清单 |
| [`interfaces/client`](interfaces/client/README.md) | 公开 Node/TypeScript stdio 客户端、进程生命周期及集成示例 |
| [`interfaces/tui`](interfaces/tui/README.md) | Ink 终端界面、命令、输入状态和 Bridge 子进程接入 |
| [`interfaces/gui`](interfaces/gui/README.md) | FastAPI 服务、React 界面、多会话连接、HTTP / WebSocket API 与媒体访问 |

## 配置与数据

`agent_runtime` 首次启动时创建配置目录，并负责读取与更新其中的应用配置：

| 路径 | 内容 |
| --- | --- |
| `.RADE/provider_config.json` | Provider 定义与主 Agent 的默认 Provider |
| `.RADE/agent_config.json` | Tool、上下文、记忆、子 Agent、MCP、Workspace Instructions 与 scratch Workspace 设置 |
| `.RADE/.env` | 配置引用的密钥和环境变量 |
| `.RADE/prompts/` | 主 Agent、子 Agent、压缩与记忆 Prompt |
| `.RADE/skills/` | standalone Skill |
| `.RADE/plugins/` | Plugin capability package |
| `.RADE/AGENTS.md` | 项目级 Workspace Instruction |
| `.RADE/sessions/` | Session Journal、Workspace 记忆、Artifact、子 Agent 记录与 Session 级辅助调用用量日志 |
| `<workspace>/.RADE/attachments/` | 上传附件和 Agent 生成的图片 |

默认配置与内置资源位于 [`agent_runtime/defaults/`](agent_runtime/defaults)。具体装配方式见 [`agent_runtime/README.md`](agent_runtime/README.md)。

## 开发与测试

Python 测试使用 Mock Provider，不需要真实 API Key：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[gui,test]"
python -m pytest --durations=20
```

在 Windows 上改用 `.venv\Scripts\Activate.ps1` 激活环境。

Pytest 默认用 4 个独立进程按测试类并行运行；串行调试使用 `-n 0`，资源较少时使用 `-n 2`。完整测试包含安装包验证及真实本地 MCP 和 Shell 进程；日常迭代可用 `--ignore=tests/test_bridge_client.py` 跳过安装包验证。独立插件测试包的运行方式见 [docs/plugin-interop.md](docs/plugin-interop.md)。

server extra 包含 FastAPI、Uvicorn、WebSocket 后端和 multipart 支持；Windows、macOS 与 Linux 使用相同的安装方式。TUI 使用基础安装。

前端构建、类型检查和测试：

```bash
npm run build
npm run typecheck
npm test
```


Windows 上 Shell Tool 使用 PowerShell 7，因此需要另外安装 PowerShell 7。

## Contributors

- [AKArrok](https://github.com/AKArrok)：提出并验证 Session Journal 尾记录追加问题（[#2](https://github.com/EvannZhongg/Nosis/pull/2)）。
- [George Pickett](https://github.com/georgeatparallel)：贡献 Parallel Search MCP 集成（[#4](https://github.com/EvannZhongg/Nosis/pull/4)）。
- [Pocket99](https://github.com/Pocket99)：贡献最初的 GUI，含会话、Workspace 浏览与模型切换（[#1](https://github.com/EvannZhongg/Nosis/pull/1)）。

## 许可

[MIT](LICENSE)
