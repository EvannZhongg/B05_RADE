# AGENTS.md
## 1. 总体原则

* 保持架构简单、清晰、可扩展，优先解决当前明确需求。
* 不进行未经要求的功能扩展、兼容处理或过度抽象。
* 不因潜在需求提前增加复杂的框架、配置层、防御逻辑或中间层。
* 修改应聚焦任务本身，不顺带重构无关代码。
* 优先复用现有实现，避免重复实现相同能力。
* 重构过程无需考虑旧数据兼容，禁止为了旧实现加兼容层。

## 2. 架构要求

```text
TUI (Ink + React) ────────────────── Bridge ── agent_runtime ── Agent Core
GUI (React) ── HTTP / WebSocket ── RADE-server
                                   └─ Session Host ── Bridge ── agent_runtime ── Agent Core
                                                               └─ rade_tasks
```

`agent_runtime` 是共享的应用 Runtime；Bridge 以 `python -m interfaces.bridge` 作为独立进程运行，由前端宿主启动，是前端与 Runtime 之间的唯一通道，两端用 newline-delimited JSON 交换协议消息。

* 依赖方向为 `interfaces/* → agent_runtime → agent_core` 和 `agent_runtime → rade_tasks`。`agent_core` 不得导入 Runtime、领域模块或 `interfaces`，`agent_runtime` 不得导入 `interfaces`，`rade_tasks` 独立于 Core、Runtime、Bridge 和前端。
* 驱动 Agent、执行 Tool只能经由 Bridge 协议；前端不得运行 Agent Loop，也不得执行会改变运行文件或 Session 的 Tool。
* 前端对 Core 的复用仅限只读能力（如 Session 读取、运行文件路径解析、只读 Tool 的文件列举）；执行语义不得在前端复制。
* TUI 与 GUI 共享 Agent Runtime。GUI 宿主通过 `interfaces/session_host.py` 管理 Bridge 进程、活动 Session 和页面附着。
* `RADE-server`（`interfaces/application_host.py`）在初始化配置前取得配置目录级进程锁，负责启动 Bridge 和 HTTP，并在退出时关闭两者。单实例判断使用该锁。
* 取消、Session 与 Tool 执行语义只实现一次，不得由两个前端各维护一套。
* 保持清晰的依赖方向，避免模块间循环依赖。

## 3. Core、Runtime、Bridge 与场景任务

新增能力的落点由职责决定：

* 通用 Agent 执行机制 → `agent_core`。
* 赛题场景、任务生命周期、分配结果和评估 → `rade_tasks`。
* 只决定选择、装配与协议 → `agent_runtime` 与 `interfaces/bridge`（Bridge 进程自身的生命周期由前端宿主管理，见 §2）。
* 只影响单个前端的呈现与交互 → 对应前端；不得反向进入 Core。

### 3.1 Agent Core：机制

`agent_core` 提供与前端无关、可被复用的机制；它不知道前端存在，也不知道配置与进程从何而来：

* Agent Loop、模型调用、错误处理与终止条件
* 上下文管理与上下文压缩
* Tool 调用与结果回灌；超出预算的输出落盘为 Session Artifact
* Session 状态与 append-only JSONL Journal，含 Provider 与当前计划
* 执行事件输出
* Tool 抽象、Tool Catalog 与调用时依赖注入
* Shell 进程执行的超时、输出上限与 spool
* Skill 加载（`SkillSource` 与 Registry；Core 不决定 Skill 目录来源）
* 计划状态与跨 Turn 的计划恢复
* 后台 Jobs 与 Turn 控制（steer、cancel 的语义与终止条件）
* 文件附件与模型上下文中的路径提示
* Runtime Instruction 的数据类型与 fingerprint（读取时机与来源顺序由 `agent_runtime` 决定）
* 子 Agent Runtime 与角色注册表
* Provider 抽象、MCP 客户端机制

约束：

* Agent Loop 应保持显式、可理解和可测试，不隐藏关键执行流程。
* 机制通过已有稳定接口暴露；不引入只服务某个前端的参数、事件或分支。
* Core 不感知 TUI/GUI、终端、浏览器或协议消息。

### 3.2 agent_runtime：Runtime 装配

`agent_runtime` 是唯一把 Core 拼装成可运行 Runtime 的地方，负责：

* 选择 Provider，读取 `provider_config.json` 与 `agent_config.json`，并处理配置写入
* 初始化配置目录、安装内置 Skill 与 Plugin、读取 Prompt 模板与 Runtime Instructions
* 组装 Tool Catalog、按角色 select、注入宿主命令执行器
* MCP Server 生命周期
* 发现 Plugin：把其声明的 Skill、MCP 与 Agent 组件加 namespace 后交给对应子系统
* 子 Agent 角色注册表
* 按 `.RADE/runs/<run_id>/` 存储 Session Journal、运行配置与中间产物。现阶段沿用 Session ID 作为 run ID，文件 Tool 的根目录为运行目录下的 `files/`。
* 一次装配由一个 `ExecutionPlaneManager` 原子持有；运行、Provider、配置或 Instructions 变化时整体重建
* 交互 Turn 生命周期（含取消与 steer）
* 场景输入适配由 `agent_runtime/scene_input.py` 负责，包含文件访问、场景快照保存和消息组装。场景任务接入 Runtime 后，由 Runtime 调用领域逻辑，并接入现有 Agent 执行、Session 持久化和取消机制。

约束：

* `agent_runtime` 不含 Agent 决策逻辑：Agent Loop、上下文压缩、结果回灌都属于 Core。
* Runtime 装配只有一处实现；TUI 与 GUI 不得各建一套。前端只决定进程与附着（何时启动、接管或释放 Bridge），不参与 Runtime 装配。

### 3.3 Bridge：协议适配

Bridge 把 `AgentEvent` 与运行状态翻译为协议消息，并转发用户输入、提问与取消，不持有装配工厂。

* 协议消息的增改必须同时更新 `interfaces/protocol/src/protocol.ts` 与发送方：`protocol.py` 定义大部分消息，其余由 `bridge.py` 与 `__main__.py` 内联发出。
* 允许存在只服务单个前端的协议消息（如 `attach_only`、`takeover`、`attachment_replaced`），但它们只表达呈现与进程附着，不得承载 Agent 语义。

### 3.4 rade_tasks 的领域职责

* `scene.py` 负责场景 JSON 解析、校验与特征提取。以原始 `P` 的行标识处理单元，列标识待分配任务，`v` 与列对齐。
* 后续由 `runner.py` 负责场景任务生命周期，`evaluation.py` 负责评估，`results.py` 负责分配结果校验与导出，`knowledge.py` 负责反思知识。按实际需求添加模块，复用 Runtime 提供的执行能力。

### 3.5 场景任务的完成条件

场景任务是前端面向用户的执行单位。单次运行绑定一个输入场景、一种反思模式及本次运行的标识。Core 的 Turn 仍可作为内部执行单位。当前 `turn_completed` 只表示 Agent 执行结束。迁移后，前端依据场景任务的结构化状态呈现结果。

依据[赛题说明第三、四节](docs/反思式决策赛题信息/反思式决策赛题信息.md)，每个参赛测试用例需要同名的关闭反思和开启反思结果。单次运行交付对应模式的文件，两种模式的文件都验收通过时，该用例的提交才齐备。

1. 校验原始场景，确定本次运行的反思模式和目标路径。赛题容器内使用 `/output/norethink/<输入文件名>` 或 `/output/rethink/<输入文件名>`。
2. 取得本次运行提交的结构化分配结果。结果须能序列化为 JSON，符合主办方的输出格式，并满足原始场景的分配约束。仓库赛题资料给出了目录与命名规则，结果字段、索引约定等以主办方的提交模板为准。
3. 用原始 `P` 和 `v` 复算期望处理价值，记录求解耗时。反思效果和跨场景达标情况属于评估指标。主办方的 `P_set` 用于评估，超过 10 分钟影响速度分，执行截止条件以实际运行预算为准。
4. 分配校验和分数复算成功后，将本次结果写到目标路径。确认写入成功后持久化任务终态与结果路径，再发布完成事件。助手回复、计划状态和旧输出文件均不能作为本次成功的依据。

结果缺失、校验失败或导出失败时记录失败原因。运行预算耗尽时记录超时原因，用户取消时沿用 Runtime 取消机制。终态由服务端根据本次执行和交付结果确定。

## 4. Tool 系统

* 所有工具使用统一接口和注册机制。
* Tool 本身只负责自身能力，不包含 Agent 决策逻辑。
* Shell 和命令 Hook 使用当前用户的宿主权限执行，文件修改通过内置文件 Tool 的 运行文件路径规则。
* 不允许模型绕过 Tool 系统直接执行系统操作。
* Tool 的输入、输出和错误应使用结构化数据。
* 输出超出预算的 Tool 由 Runtime 落盘为 Session Artifact，前端不参与截断或存储决策。

## 5. TUI 与 GUI

* 前端只负责渲染协议消息与采集输入，不得在其中实现或改写 Agent 行为。
* Agent 状态必须由结构化事件驱动，不得从自由文本中解析；如何渲染由各前端自行决定。

## 6. Session 与状态

* 对话和执行过程应围绕 Session 管理。
* Session 以 append-only JSONL Journal 保存对话、执行事件与当前计划。
* Session 状态不得依赖某个具体前端。
* TUI 与 GUI 应能够读取和恢复同一类 Session。
* 持久化数据与运行时对象分离。

### 外部扩展与公开接口

- Core 定义可复用的机制与抽象，Runtime 提供扩展注册入口；不得为插件建立平行的 Agent Loop、Tool 执行或 Session 管理系统。
- 新能力优先复用现有 Skill、MCP、Agent 与 Tool 机制。无法满足明确的第三方开发需求时，允许在现有模块边界内增加最小必要的扩展接口。
- 区分公开扩展契约与内部实现。公开接口应保持明确、精简且稳定；内部接口不因插件需求而默认公开。
- 对外扩展契约的破坏性变更应明确说明和管理版本；内部重构仍遵循现有简化原则。
- 第三方工具与组件必须复用现有生命周期和执行约束，不得绕过 Runtime 直接控制 Agent 或 Session。
- 不为假设性的插件需求提前建立通用 Hook 系统、事件总线、依赖注入框架或额外服务。

## 7. 代码要求

* 优先小模块、明确职责和显式接口。
* 避免无必要的设计模式和层级包装。
* 不创建仅有一次调用、没有实际抽象价值的 wrapper。
* 不为了“未来可能使用”添加当前没有消费者的接口。
* 不保留无用兼容代码、废弃实现或重复逻辑。
* 删除功能时直接删除相关实现，不添加解释性残留代码。
* 注释解释必要的设计原因，不记录开发过程、争论过程或已删除方案。

## 8. 开发行为约束

实现需求时：

1. 先理解现有架构和调用链。
2. 找到最小且正确的修改范围。如有更优的解决方案可以重构相关模块而不是只做最小补丁。
3. 优先修改已有实现，而不是新增平行实现，重构功能时不必兼容旧实现。
4. 完成后检查是否引入重复逻辑、无用抽象或额外功能。
5. 执行相关测试，并修复由本次修改直接导致的问题。

除非明确要求，否则不要：

* 改变无关 UI 或文案。
* 增加新的配置项。
* 增加旧版本兼容逻辑。
* 增加 fallback、retry 或 defensive behavior。
* 新建额外服务或框架层。
* 扩大任务范围。
* 将开发要求、内部约束或实现说明展示给最终用户。
