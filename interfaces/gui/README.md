# GUI

`interfaces/gui` 是 RADE 的图形交互层，由 HTTP / WebSocket 适配器和 React + assistant-ui 前端组成。FastAPI 管理浏览器连接、文件与设置 API；常驻 Bridge 由独立于 FastAPI 生命周期的应用宿主管理，每个活动 Session 使用独立 Runtime 通道。浏览器不直接运行 Agent 或 Tool。

`RADE-server` 通过 `interfaces/application_host.py` 管理 HTTP、活动 Session 和 Bridge。同一配置目录由进程锁保证单实例。

HTTP 依赖是可选依赖。使用项目环境启动前先安装 server extra：

```bash
uv sync --extra server
# 或：python -m pip install -e ".[server]"
```

其中包含 Uvicorn 的 WebSocket 后端，Windows 上还包含用于通过 Explorer 打开运行文件夹的 `pywin32`；三个平台使用相同命令。若缺少 WebSocket 后端，HTTP 适配器会提示安装命令。

## 启动

```bash
RADE-server
```

默认监听 <http://127.0.0.1:8736>。`interfaces/application_host.py` 初始化 `.RADE/`、Session Store 和 Settings Store，然后启动 Bridge 和 HTTP 服务。

从项目根目录启动 `RADE-server` 后，在对话区的 `选择 input/ 场景` 下拉框中选择、预览并运行场景。列表读取启动目录下的 `input/`，文件名使用 `scene<S>_<I>.json`，其中 `S` 为 1 或 2，`I` 为 1–5。

## 服务端

`interfaces/application_host.py` 管理Session Host 和 HTTP 的生命周期，并向 `server.py` 注入活动 Session 注册表；`server.py` 组装 GUI 应用和路由，管理 HTTP 监听与 Uvicorn 的启停。HTTP 适配器职责包括：

- 列出、读取和删除运行历史
- 通过共享 Session Host 为活动 Session 创建、复用和关闭通道
- 通过 WebSocket 双向转发共享协议
- 管理单 Session 的页面附着、事件续传和显式 takeover
- 提供已脱敏设置快照及配置更新 API
- 展示长期记忆
- 为运行中的工具图片签发短期访问 URL
- 提供限制在当前运行的 `files/` 内的文件树，并在系统文件管理器中打开运行文件目录
- 列出 `input/` 场景并提供 JSON 预览

`ActiveSessionRegistry` 按 Session 保存后台运行的 `ActiveSession`。页面切换或临时断线不会取消 Turn；重新连接可以从 Bridge 分配的 `event_sequence` 继续。宿主缓存 Bridge 的完整运行快照和 transcript 检查点。一个 Session 同时只有一个控制页面，新页面只有显式 takeover 才替换现有 attachment。

HTTP / WebSocket 路由按模块拆分：

| 模块 | 路由范围 |
| --- | --- |
| `routes/sessions.py` | Session 查询与历史搜索、删除、active session 与 `/api/session` WebSocket |
| `routes/run_files.py` | 浏览和打开当前运行的文件目录 |
| `routes/media.py` | 工具图片签名与展示 |
| `routes/settings.py` | 模型、设置、路由更新和长期记忆读取 |

服务端非路由模块：

| 模块 | 职责 |
| --- | --- |
| `../application_host.py` | 配置目录级进程锁、Session Host 与 HTTP 生命周期 |
| `server.py` | FastAPI 应用与路由组装、HTTP 监听与 Uvicorn 生命周期 |
| `../session_host.py` | 活动 Session 注册表、控制附着与事件续传 |
| `windows_folders.py` | 通过 Windows Explorer 定位并打开运行文件夹 |

常驻 Bridge 进程的生命周期、通道收发与故障广播由宿主侧模块 `interfaces/bridge_process.py` 提供；GUI 只决定何时创建、附着与关闭，不实现协议语义。

服务端复用 Core 的运行记录和文件路径解析能力。Agent 操作经 Bridge 协议执行。

## 前端

React 应用提供可折叠的 Session 列表、对话区和运行文件树。多个 Session 会稳定挂载，因此可以并行执行并在页面内切换。

切换到其他会话时，只断开空闲会话的 WebSocket，保留其 Bridge、Provider 和 MCP；返回后附着已有 Runtime。离开浏览器页面时释放该页面持有的空闲会话，删除会话和关闭应用宿主也会释放资源。关闭会话通道不会结束常驻 Bridge；新会话复用已加载的 SDK、tokenizer 和 TLS 上下文，仍有独立执行面。应用宿主退出时才关闭进程及全部通道。

对话层由 `useSessionRuntime` 统一管理：

- 建立和恢复 Session WebSocket
- 等待首个 `runtime_state` 同步屏障
- 回放 Journal 并把协议事件投影为 assistant-ui 消息
- 管理流式文本、reasoning、Tool、计划、Job 和 Context Window
- 选择并预览 `input/` 场景，提交后可停止运行。支持修改 Provider
- 在重连期间合并待应用的本地选择与服务端权威状态

主要前端模块：

| 模块 | 职责 |
| --- | --- |
| `src/main.tsx` | React 入口 |
| `src/App.tsx` | 应用布局、多 Session、设置入口与运行历史 |
| `src/Chat.tsx` | 单 Session 对话容器 |
| `src/chat/useSessionRuntime.ts` | 协议连接与 assistant-ui Runtime 适配 |
| `src/chat/runtimeState.ts` | Job 与计划的运行状态投影 |
| `src/transcript.ts` | 协议事件到 transcript 的纯状态投影 |
| `src/chat/Transcript.tsx` | transcript 视口与滚动 |
| `src/chat/TurnNavigation.tsx` | 按 Turn 跳转的导航条 |
| `src/session.ts` | 浏览器到服务端的 WebSocket client 与 JSON 消息接口 |
| `src/chat/Composer.tsx` | 场景选择与 Provider 控件 |
| `src/chat/Messages/` | assistant、user、Tool 与媒体消息渲染 |
| `src/chat/RuntimeIndicators/` | Context Window、计划和后台 Job |
| `src/RunFiles.tsx` | 运行文件树与逐项复制相对路径 |
| `src/CopyTextButton.tsx` | 复制按钮（代码块、表格与文件树共用） |
| `src/Settings.tsx` | Provider、Agent、Memory、Skill、Plugin、MCP、Schedule 设置页 |
| `src/api.ts` | HTTP API 类型与请求函数 |

## 工具媒体

工具产出的运行图片通过短期签名 URL 展示。图片路径和文件树请求都以该运行的 `files/` 目录为根。

## 设置与运行状态

Settings 页面覆盖 Provider、默认路由、Agent Tool、上下文、Memory、Skill、Plugin、MCP 和 Schedule。API key 只写入 `.RADE/.env`，服务端返回的设置快照只包含凭据来源与是否已配置。

对话输入框首字符为 `/` 时展开命令菜单。`/refresh` 在当前会话空闲时重新加载运行环境，保留对话和计划，并重连 MCP；支持点击、Enter 执行、Tab 补全、Esc 收起。运行中不能执行刷新，草稿保持不变。配置变化仍会在下一轮对话自动检查并应用。

Context Window 指示器展开后显示当前输入、缓存命中率、可用输入上限、剩余输入空间、压缩触发点、模型总窗口、压缩次数与当前压缩总结。后台任务指示器显示任务数量、类型、Job ID 与状态，shell Job 展开命令、subagent Job 展开角色与任务描述。Assistant 消息把 reasoning 与 Tool 调用折叠为可展开的推理与执行过程区块：Turn 运行且尚未出现回答文本时默认展开，回答开始后自动折叠，回答本身始终可见。

执行过程完全由结构化协议渲染，不从 assistant 自由文本推断 Tool、计划或状态。停止操作会取消当前 Turn，但不会撤销已完成的 Tool 操作。

## 开发

先启动 Python 服务，再运行 Vite：

```bash
RADE-server
npm run dev --workspace interfaces/gui
```

访问 <http://127.0.0.1:5173>，Vite 将 API 和 WebSocket 代理到 FastAPI。

```bash
npm run build --workspace interfaces/gui
npm run typecheck --workspace interfaces/gui
npm test --workspace interfaces/gui
```

前端源码位于 `src/`，Vite 构建产物位于 `static/`，并作为 Python package data 随 RADE 安装。
