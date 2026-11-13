# TUI

`interfaces/tui` 是使用 TypeScript、Ink 和 React 实现的终端交互层。它启动独立 Bridge 子进程，渲染协议消息并采集输入，不运行 Agent Loop，也不直接执行 Tool。

## 启动

```bash
RADE
```

`interfaces/launch.py` 负责在项目根目录初始化 `.RADE/`、定位 Node 和已构建的 TUI bundle，并通过 `RADE_PYTHON` 告诉前端应使用哪个 Python 解释器启动 Bridge。Bridge 继承启动目录，运行记录保存在 `.RADE/runs/<run_id>/`。

## 交互模型

TUI 在启动或切换 Session 时先发送 `open_session`，收到首个 `runtime_state` 后才允许提交命令。从项目根目录启动 `RADE`，输入 `/input` 打开场景列表，用 `↑` / `↓` 选择并按 `Enter` 运行。场景文件位于启动目录的 `input/` 下，命名为 `scene<S>_<I>.json`，其中 `S` 为 1 或 2，`I` 为 1–5。切换会话工作区时继续使用同一个输入目录。

一个 TUI 持有一个常驻 Bridge 进程。每个已打开的 Session 分配独立通道与 Runtime，切换界面不会关闭其他会话的执行面或 MCP。切回已打开会话时使用 `session_snapshot` 取得原 Runtime 的权威快照，不重绑 Session 或重建 Agent；仅把当前通道的事件渲染到界面。关闭 TUI 时统一关闭进程及所有 Runtime。

界面由结构化协议状态驱动，展示：

- assistant 文本与 reasoning 流
- Tool 调用、结果和错误
- Context Window 占用、token 用量、缓存命中率、计划及后台 Job
- Provider 和 Runtime phase
- 历史 Session 列表和恢复结果

主要命令：

| 命令 | 作用 |
| --- | --- |
| `/sessions` | 列出并切换历史运行 |
| `/new` | 新建运行 |
| `/input` | 从启动目录的 `input/` 选择并运行赛题场景 |
| `/model` | 切换当前 Session 的 Provider |
| `/provider` | 查看 Provider、Skill、Plugin 与 MCP 配置概览 |
| `/refresh` | 空闲时重新加载当前会话的配置、Prompt、Skill、Plugin 与 MCP 连接，保留对话和计划 |

目标会话被其他窗口占用或打开失败时，保留当前会话；按 `Esc` 关闭选择列表后可继续原会话。首次打开失败时仍需选择可用的会话或工作区。

常用按键：

| 操作 | 作用 |
| --- | --- |
| `Enter` | 提交输入或确认选项 |
| `Ctrl+J` | 输入换行 |
| `Shift+Enter` | 支持 kitty keyboard protocol 时输入换行 |
| `↑` / `↓` | 在候选列表中上下选择（输入光标用 `←` / `→` 移动） |
| `Esc` | 关闭列表或取消当前 Turn |
| `Ctrl+C` | 取消当前 Turn；空闲时清空草稿，空输入时退出 |
| `Ctrl+D` | 空输入且空闲时退出 |

TUI 只在已知支持的终端环境启用 kitty keyboard protocol，不进行可能把探测响应写入输入框的主动探测。

## 模块索引

| 模块 | 职责 |
| --- | --- |
| `src/cli.tsx` | Node CLI 参数和 Ink 根节点 |
| `src/app.tsx` | Bridge 生命周期、界面组合和用户操作路由 |
| [`@rade/client`](../client/README.md) | Bridge 子进程与 NDJSON 收发 |
| `src/state.ts` | 协议消息驱动的 TUI 状态归约 |
| `src/input.tsx` | 多行输入、光标和键盘行为 |
| `src/commands.ts` | slash command 定义与匹配 |
| `src/renderer.tsx` | transcript、Tool、状态和交互卡片渲染 |
| `build.mjs` | esbuild bundle |

共享协议来自 [`interfaces/protocol`](../protocol/README.md)，Bridge 行为见 [`interfaces/bridge`](../bridge/README.md)。

## 开发

先在仓库根目录执行 `npm run build --workspace @rade/protocol` 与
`npm run build --workspace @rade/client`，或直接使用根目录的 `npm run build`。

```bash
npm run build --workspace interfaces/tui
npm run typecheck --workspace interfaces/tui
npm test --workspace interfaces/tui
```

构建入口为 `src/cli.tsx`，产物为 `dist/app.js`，并作为 Python package data 随 RADE 安装。
