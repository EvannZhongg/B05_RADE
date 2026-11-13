# Agent Runtime

`agent_runtime` 是 RADE 唯一的应用 Runtime 装配层。它读取应用配置和运行状态，把 [`agent_core`](../agent_core/README.md) 的机制组装成 Bridge 可调用的运行实例，但不实现 Agent 决策逻辑。

## Runtime 入口

`RuntimeHost` 是共享入口，组合三个主要控制器：

```text
RuntimeHost
├── SessionRuntimeController   运行、Session、Provider 与计划
├── ExecutionPlaneManager      Agent、Tool、MCP 连接、Memory、Job 等原子装配
└── SceneTaskRunner            场景任务编排、控制与结果交付
```

常驻 Bridge 为每个通道创建独立 `RuntimeHost`，在进程内共享 `RuntimeServices` 提供的环境重载与设置读取。各 Host 分别关闭自己的执行面和 Journal。

默认 Provider 的代码、tokenizer 与 TLS 信任上下文在进程启动时预热，不发送模型请求。

## Execution Plane

`ExecutionPlane` 持有一次完整执行所需的对象：

- 运行文件目录与选定的 Provider 名称
- 已解析的 Agent 配置及配置 fingerprint
- Runtime Instructions 及其 fingerprint
- 已装配的 Agent（含其 Tool）与执行路由
- Memory、MCP Client Manager、后台 Job 和上下文窗口

`ExecutionPlaneManager.ensure()` 在首个 `rethink` 场景任务或显式刷新时装配。复用条件是运行、Provider 名称、解析后的 Agent 配置、配置 fingerprint、Instructions fingerprint 与有效 MCP 配置全部一致；任一变化时旧 plane 整体关闭并在下一次使用时重新创建。关闭时由执行面关闭其 MCP 连接及 stdio 子进程。

空闲时可调用 `RuntimeHost.refresh_runtime()` 显式强制重新装配，即使上述输入未变化也会重建执行面，重新读取 Prompt、Skill、Plugin Agent，并重连 MCP、重新发现工具。该操作保留当前 Session、Journal 和 Plan，不调用模型、不创建 Turn；结果通过 `starting → idle/failed` 反馈。装配失败不会发布半成品，可修正后再次刷新；旧执行面关闭后的失败不恢复旧资源。正常 Turn 仍沿用原有变更检测。

装配阶段负责：

- 创建当前 Session 选中的主 Provider
- 使用 OpenAI SDK 按配置装配 Chat Completions 或 Responses
- 为执行面建立一个以当前运行的 `files/` 为工作目录的宿主命令执行器
- 从内置 Catalog 按主 Agent 和子 Agent 角色选择 Tool
- 注入 Session、Plan、Memory、命令执行器和用户交互能力
- 加载 standalone Skill、Plugin Skill、Plugin Agent 与 MCP Server
- 构造子 Agent 角色注册表和后台 Job Manager
- 启动 MCP Server 并汇总非致命 Runtime warning

## MCP 生命周期

每个 Execution Plane 创建自己的 Core `McpClientManager`，直接连接 stdio 或 streamable HTTP Server。各执行面的连接与 stdio 子进程相互独立。

Manager 在后台线程中并发连接 Server，装配其他依赖后通过 `wait_ready()` 获取经过 allowlist 过滤的 `McpTool`。启动超时或失败仅将对应 Server 标记为 unavailable；工具名冲突仅拒绝冲突 Server 的整组工具，不留下部分注册结果。

主 Agent 与子 Agent 通过同一执行面的 Tool Catalog 和上下文调用 MCP，取消走共享的 Turn 控制。切换运行时重建执行面。Provider、配置、Instructions 或有效 MCP 配置变化时，旧执行面连同 MCP 连接一起关闭，随后重新启动和发现工具。

Server 初始化返回的 `instructions` 由 Manager 在成功注册时保存为执行面内的固定快照。主 Agent 与子 Agent 按实际选出的 MCP ToolSet 取得相关 Server 的完整说明，在创建 Agent 时追加到 system prompt 的 Runtime Instructions 与 Skill 目录之后、Memory 和压缩摘要之前。说明保留 Server 身份及原文，不另行列举工具名，不覆盖执行、授权或任务约束；无可见工具、无说明或注册失败的 Server 不注入。说明顺序不受连接完成顺序影响，不随 Turn、计划或 Jobs 更新，也不由历史压缩摘要替代。

stdio 的相对 cwd 基于运行文件目录解析，未配置时使用该目录；Plugin Server 的 cwd 由 PluginLoader 解析到 Plugin 根目录。streamable HTTP 直接使用当前 Runtime 的环境与配置 headers。

### 子 Agent 的 MCP 选择

`agent_config.json` 的 `subagent_roles.<role>.tools` 声明内置工具，独立的 `mcp` 字段声明 MCP 引用，默认 `{}`，不授予任何 MCP 工具：

```json
{
  "description": "Search documentation.",
  "tools": {"read_file": true},
  "mcp": {
    "analyzer": true,
    "docs": {"search": true},
    "review-kit:writer": {"write": false}
  }
}
```

服务器键使用完整身份（Plugin Server 为 `plugin:server`），工具键使用服务器返回的原始名，不使用模型可见的 `mcp__...` 限定名。服务器值为 `true` 时允许当前全部可用工具；`false` 或未声明时不允许。工具映射全部为 `true` 时是白名单，只允许列出的工具；全部为 `false` 时是黑名单，允许除列出工具外的其他工具。单个服务器的空映射、混用 `true`/`false` 或非 boolean 值均为配置错误。

配置解析只检查上述结构；Runtime 在 MCP 发现、上游 `tools.enabled` 过滤和完整 Catalog 装配后统一验证引用并展开。未知服务器、不可用的显式工具引用（含黑名单项）会报告对应角色的配置错误。配置中已知但禁用或不可用的服务器以 `true` 选择时得到空工具集；任何选择都不能绕过服务器启用状态和上游 allowlist。

展开结果按内置工具固定顺序、MCP 限定名字典序保存为执行面快照，再交给 Core 生成 ToolSet。白名单不会自动授予新增工具；黑名单和 `true` 会在下一次执行面重建时纳入服务器新增且上游允许的工具。复用同一执行面时，切换 Session 或执行 Turn 不会重新展开。

## Session 与 Turn 生命周期

`SessionRuntimeController` 在 `open_session` 时完成轻量控制面初始化：

- 创建或恢复运行与 Session
- 选择显式、Session 已保存或默认的 Provider
- 连接 append-only Journal sink
- 恢复计划

此时不会创建 Provider、MCP、Tool 或 Agent。真正的执行面由首个 Turn 延迟创建。

切换 Session 时先准备目标会话并取得 Journal 写入权，再由 `RuntimeHost` 释放原执行面的会话资源并提交新绑定。目标准备失败不会释放原会话的 Journal、计划或执行状态；重新选择当前 Session 则复用已持有的 Journal 写者。

`SceneTaskRunner` 保证一个 Host 同时只有一个活动场景任务，并统一处理：

- 尚在排队或已经运行的 `rethink` 任务的 steer
- 排队取消、活动取消和 Host shutdown
- Runtime phase 回调
- 通过现有实验执行器运行基线；`rethink` 额外运行一次 Agent Turn，并从本次实验选出期望值最高的合法分配
- 原子写入 `output/<mode>/<输入文件名>`，将任务终态写入 Session Journal
- `rethink` 成功交付后的长期记忆 reconcile

`RuntimeCallbacks` 是 Runtime 对宿主的窄接口，用于发布事件、phase、计划与 MCP 状态，以及请求授权或向用户提问。

### 反思与知识

`rethink` Turn 提供 `knowledge_query`、`reflection_propose` 和 `knowledge_explain`。
Agent 提交假设、结论、两个本次任务的实验 ID 和相对期望值变化方向。
Runtime 从 Journal 与实验文件取得证据，`rade_tasks.knowledge.KnowledgeService`
校验来源和分配，复算期望值并验证变化方向。假设和结论原文作为反思记录保留。

SQLite 文件 `knowledge.sqlite3` 位于配置目录，由多个 Session 共享。
正式知识以场景范围和两个策略配置版本为键。检索要求处理单元相对数量和候选密度的类别一致，
优先返回同规模证据，也可返回另一规模的经验供 Agent 在当前场景复测。同一策略对优先采用
当前规模的实测结论。当前规模的证据发生冲突时，该策略对也会从当前规模的检索中排除。
同场景、同方向的重复证据保留来源并指向原记录。新观察产生新的知识版本。
观测方向不一致时，当前版本标记为冲突并退出常规检索。通过 `knowledge_explain`
可按 ID 查阅验证状态、历史版本及关联实验。

## 配置与默认资源

`config.py` 默认在启动时所在的项目根目录初始化和读取 `.RADE/`：

| 路径 | 用途 |
| --- | --- |
| `provider_config.json` | Provider 定义与主 Agent 的默认 Provider |
| `agent_config.json` | Tool、上下文、记忆、子 Agent、MCP、Instructions、场景任务 |

`agent_config.json` 的 `scene_task` 配置场景任务。两种模式共用同一基线，每次任务开始时读取配置。

```json
{
  "scene_task": {
    "budget_seconds": 600,
    "baseline_strategy_id": "marginal_greedy",
    "baseline_config_id": "default"
  }
}
```

`budget_seconds` 是任务执行预算。到达截止时间时，Runtime 通过取消机制停止求解和模型等待。结果在预算内提交后，长期记忆合并也只使用剩余预算。
| `.env` | `${ENV_NAME}` 引用的密钥，由 `settings.py` 读写 |
| `prompts/` | `Soul.md`、`SubAgent.md`、`Consolidator.md`、`RunMemory.md` |
| `skills/` | standalone Skill |
| `plugins/` | Plugin 安装目录 |
| `AGENTS.md` | 应用级运行指令 |

初始化时从仓库内的 [`defaults/`](defaults) 补齐缺失的配置、Prompt 和 Skill，并创建用于手动安装插件的 `plugins/` 目录。

Skill 仅从配置目录下的 `skills/` 和 `plugins/` 加载，默认分别为 `.RADE/skills/` 和 `.RADE/plugins/`。先注册 standalone Skill，再注册启用的插件通过 `plugin.json` 声明的 Skill。插件 Skill 使用 `<plugin>:<skill>` 标识，同一标识保留最先注册的 Skill。

子 Agent（含插件角色）、上下文压缩和长期记忆合并共用当前 Session 选中的主 Provider。上下文压缩复用原请求前缀。压缩和记忆调用直接写入所属 Session 的 JSONL Journal，记录实际模型、任务、状态、耗时和用量，不进入对话上下文；子 Agent 调用保存在子 Session 中。模型请求超时和额外重试次数来自 `agent_config.json` 的 `provider` 字段。

每个 `provider_config.json` 的 `providers.<name>` 条目可以配置 `api: "responses"` 或 `api: "chat_completions"`，按协议选择原生 OpenAI SDK；与中转域名无关，也不在失败后切换协议。显式选择协议时，除 `deepseek/` 路由前缀会移除外，`model` 是原样发送的服务端模型 ID，例如 `gpt-6-astra` 或 `openai/gpt-5`（后者适用于要求该 ID 的网关）。

省略 `api` 时，DeepSeek 模型默认使用 Chat Completions，其余模型默认使用 Responses。自动选择时会移除 `openai/` 前缀。DeepSeek 模型在两种协议下均移除 `deepseek/` 前缀。其他带服务商前缀的模型可以使用 LiteLLM 的 token 上限和文本 token 估算。

Qwen 和 Ollama 预设使用 Chat Completions。使用本地 Ollama 时，请将 `max_context_tokens` 与服务实际分配给模型的上下文大小对齐。端点格式见 [Qwen](https://www.alibabacloud.com/help/en/model-studio/model-calling-in-sub-workspace) 和 [Ollama](https://docs.ollama.com/api/openai-compatibility) 文档。

可选的 `reasoning_effort` 值为 `none`、`minimal`、`low`、`medium`、`high`、`xhigh` 或 `max`；具体模型只支持其中的子集。原生 Responses 发送 `reasoning.effort`，原生 Chat Completions 发送 `reasoning_effort`。省略该字段时使用上游默认值。`reasoning_parameters` 可以覆盖推理参数，`extra_body` 用于网关扩展字段，例如 `{"extra_body": {"enable_thinking": true}}`；原生 Chat 的额外推理字段也通过 SDK 的扩展 body 发送。

## 设置服务

`SettingsStore` 是配置读取和写入的统一应用服务：

- 生成已脱敏的 Provider、路由、Agent、Skill、Plugin、Agent component 和 MCP 快照
- 校验并原子写入 Provider、路由和 Agent 配置
- 把 API key 写入 `.env`，不在快照中返回正文
- 使用 revision 防止基于旧快照覆盖新配置
- 计算配置 fingerprint，供 Execution Plane 判断是否失效

GUI HTTP API 和 Bridge 设置消息都复用该服务。

## Runtime Instructions

`instructions.py` 读取 `.RADE/AGENTS.md`，以内容和来源计算 fingerprint。指令进入主 Agent 与子 Agent 的 system prompt。修改后在下一个执行面检查点重新装配。

## Plugin 与扩展发现

`PluginManager` 从 `.RADE/plugins/<plugin-name>/plugin.json` 发现 Plugin。Plugin 是分发单元，不建立新的执行系统；其组件被加 namespace 后交给已有 Skill、MCP、子 Agent 和 Hook 子系统。

```json
{
  "name": "example",
  "version": "1.0.0",
  "description": "Example capability package",
  "capabilities": ["skills", "mcp", "agents", "hooks"],
  "components": {
    "skills": ["skills"],
    "mcp": [".mcp.json"],
    "agents": ["agents/reviewer.md"],
    "hooks": ["hooks/hooks.json"]
  }
}
```

- Skill 注册为 `<plugin>:<skill>`，每个直接子目录使用标准 `SKILL.md`。
- MCP Server 保存为 `<plugin>:<server>`；是否加载仍受 `agent_config.json.mcp.enabled` 总开关控制。
- Agent component 使用 YAML frontmatter 声明 `name`、`description`，可选 `tools`；注册为 `<plugin>:<agent>`。
- Agent 的 `tools` 保留内置角色工具，并可显式选择完整 MCP 工具名（例如
  `mcp__example_analyzer__analyze_code`）；省略时仍只使用原有内置角色工具。
  Runtime 在 MCP 发现与 allowlist 过滤后校验引用，缺失工具仅跳过受影响角色并报告来源。
- Hook component 声明命令检查点，注册为 `<plugin>:<hook>`；Runtime 只负责解析和装配，
  检查点、决策、命令执行与 Journal 由 Core 负责。

standalone Skill 与 MCP 使用同一套 Core registry / manager，不存在 Plugin 专用执行路径。
装配、静态校验、错误隔离与文件变更的完整边界见 [Plugin 组件互通](../docs/plugin-interop.md)。

## 运行目录

`.RADE/runs/<run_id>/` 保存当前运行。现阶段协议的 `session_id` 即 `run_id`。目录根部存放 JSONL Journal、`session.json`、记忆和子 Agent 记录。文件工具和 Shell 以 `files/` 为工作目录，场景输入快照、工具超长输出及求解文件都在其中。Runtime 启动场景 Turn 时将只读 `SceneState` 绑定到该 Turn 的 `query_scene`。

## 算法实验

策略实现在 `rade_tasks/algorithms/implementations/`，通过 `rade_tasks/algorithms/registry.py` 的 `BUILTIN_STRATEGIES` 注册。每种策略声明版本、参数 JSON Schema 和 `module:function` 入口。每个配置声明参数值、随机种子和独立进程的超时秒数。入口实现 `algorithms/base.py` 中的 `Solver`，接收不可变的 `SceneSnapshot`，按原始 `P` 的行顺序返回分配，每行填原始任务列的零基索引或 `None`。外部程序可以由遵循同一签名的适配器接入。

内置 `marginal_greedy` 的 `default` 配置无可调参数，声明 30 秒求解上限。它按原始顺序逐个处理单元，将其分配给能带来最大正期望价值增量的任务。增量相同则选原始列索引较小的任务，没有正增量则留空。每个实验启动时，以配置声明的上限和任务剩余时间中的较小值限制求解进程，排队时间也消耗任务预算。

- `strategy_list` 返回已注册策略的参数范围和通过校验的配置。
- `experiment_run` 在场景 Turn 内接受 `strategy_id` 和 `config_id`，提交后台 Job 后立即返回 `experiment_id`、状态和当前耗时。Runtime 在独立进程内运行策略，独立评估器用原始 `P` 和 `v` 校验分配并复算期望处理价值。
- `experiment_get` 查询本次运行的实验记录。带上 `baseline_experiment_id` 可比较同一输入快照的期望处理价值。求解产物写入 `files/experiments/<id>/allocation.json`。成功结果的 `result_ref` 相对于 `files/`，可直接传给 `read_file`。实验状态和指标随 Session Journal 保存。

## 模块索引

| 模块 | 职责 |
| --- | --- |
| `host.py` | RuntimeHost 公共入口和控制器协调 |
| `services.py` | 进程内共享的配置服务、环境重载与 Settings |
| `execution_plane.py` | 一次原子装配的资源容器 |
| `plane_manager.py` | Execution Plane 创建、复用、失效与关闭 |
| `session_controller.py` | 运行、Session、Provider 与计划绑定 |
| `scene_task_runner.py` | 编排基线求解和反思，处理任务取消与超时，导出结果并持久化终态 |
| `role_tools.py` | 把角色声明在装配完成的 Catalog 与 MCP 选择上展开为 ToolSet |
| `config.py` | 配置目录初始化、Provider 与 Prompt 读取 |
| `settings.py` | 配置快照、校验、写入和 fingerprint |
| `plugins.py` | Plugin manifest、Skill、MCP、Agent 与 Hook component 发现 |
| `instructions.py` | Runtime Instructions 来源读取与 fingerprint |
| `scene_input.py` | 场景输入、快照与用户消息 |
| `callbacks.py` | Runtime 对宿主的回调接口 |

## Plugin Hooks

`components.hooks` 声明版本化的 command Hook 文件。Runtime 解析与装配，Core 在输入、工具执行前后和正常结束前应用决策，复用现有 Tool 和宿主进程执行链。声明变更通过 `/refresh` 生效。公开契约和测试边界见 [Plugin Hooks](../docs/plugin-hooks.md)。
