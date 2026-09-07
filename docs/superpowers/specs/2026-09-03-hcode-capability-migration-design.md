# Hcode 能力迁移到 TriCoder 的平台化设计

日期：2026-09-03

状态：已确认总体路线与运行数据流，等待按主计划分阶段实施

目标仓库：TriCoder（projects/tricoder-cli）

来源仓库：Hcode（projects/hcode）

## 1. 背景

TriCoder 当前是一个强调安全审批、最小持久化、多 Provider 适配和本地可审计执行的 Coding Agent。Hcode 已具有 MCP、Skills、Hooks、子 Agent、团队任务、Worktree、流式输出和更完整的上下文管理。

本次工作以 TriCoder 为主体，将 Hcode 中经过筛选的能力逐步迁移到 TriCoder，使其演进为完整 Agent 平台。迁移不是合并两个程序，也不是用 Hcode Agent 替换 TriCoder Agent。

只读盘点时的规模参考：

| 项目 | Python 源文件 | Python 源码行数 | 最近记录的完整测试 |
| --- | ---: | ---: | --- |
| TriCoder | 37 | 约 10,659 | 459 通过，2 个 Windows 符号链接跳过 |
| Hcode | 142 | 约 22,475 | 740 通过，2 跳过 |

这些数字仅用于说明迁移规模，执行阶段必须重新建立基线。

## 2. 已确认决策

1. 采用“原生适配迁移”路线。
2. 保留 TriCoder 的 Agent、Provider、ToolRegistry、权限、审计、路径绑定、子进程控制、变更账本和最小化会话持久化作为架构主体。
3. Hcode 提供能力设计、算法和测试场景参考；不得将其执行链直接旁路接入 TriCoder。
4. 当两个项目语义冲突时，TriCoder 的安全与隐私边界优先。
5. 本轮目标包含 MCP、Skills、Hooks、Worktree 和子 Agent，并补齐它们所依赖的流式事件、上下文预算、取消和扩展宿主。
6. Hcode Remote 不进入本轮范围。
7. OS 级沙箱不进入本轮实现；这不允许文档或界面把权限策略描述成进程隔离。
8. 每个阶段必须可以独立测试、审查和回滚，不做一次性整体移植。

## 3. 目标与非目标

### 3.1 目标

- 让 TriCoder 形成统一、可扩展的异步 Agent Runtime。
- 支持真正的 Provider 流式输出和类型化运行事件。
- 以统一 Extension Host 管理 MCP、Skills 和 Hooks。
- 让 MCP 工具通过现有 Tool Gateway 执行并接受审批、审计和资源限制。
- 支持渐进加载、受限来源和参数替换的 Skills。
- 支持可拒绝、可观察、不可旁路权限系统的 Hooks。
- 支持受控 Git Worktree 创建、切换、清理和任务隔离。
- 支持具有权限交集、预算和审计范围的子 Agent。
- 在 CLI、TUI、Eval 和 CI 中提供可验证的完整工作流。

### 3.2 非目标

- 不迁移 Hcode Remote 或提供网络服务端。
- 不迁移 Hcode 的完整自由文本会话落盘。
- 不允许任意 Python 插件导入或在加载时执行代码。
- 不让 Skills 成为代码插件。
- 不让 Hooks、MCP 或子 Agent 绕过 Tool Gateway。
- 不默认复用 Hcode 的用户级目录、配置文件或运行状态。
- 不在迁移中顺带重写 TriCoder 的全部工具和 UI。
- 不宣称已有 OS 级沙箱。

## 4. 总体架构

~~~text
CLI / TUI
    |
    v
Agent Runtime
|- Planner / Context Manager
|- Typed Event Stream
|- Cancellation / Budgets
+- Sub-Agent Coordinator
    |
    v
Extension Host
|- MCP Adapter
|- Skills Loader
|- Hooks Engine
+- Worktree Service
    |
    v
Tool Gateway（唯一执行入口）
|- ToolRegistry
|- Permission / Policy
|- Workspace Path Binding
|- Subprocess Control
|- Audit Logger
+- Change Journal
    |
    v
Filesystem / Git / Processes / MCP Servers
~~~

### 4.1 目录边界

计划中的新增目录：

~~~text
src/tricoder/
  core/
    events.py
    cancellation.py
    budgets.py
  context/
    manager.py
    spill.py
  extensions/
    models.py
    host.py
  mcp/
    client.py
    manager.py
    tool_adapter.py
  skills/
    models.py
    parser.py
    loader.py
  hooks/
    models.py
    engine.py
  worktree/
    models.py
    service.py
  agents/
    models.py
    coordinator.py
    task_manager.py
~~~

现有模块继续负责：

| 模块 | 保留职责 |
| --- | --- |
| policy.py | 工作区边界、敏感路径和命令策略 |
| subprocess_control.py | 受限子进程执行、输出与超时 |
| subprocess_env.py | 子进程敏感环境变量过滤 |
| audit.py | 脱敏结构化审计 |
| changes.py | 任务级变更账本、diff 和 undo |
| tools/ | 内置工具与唯一工具执行入口 |
| sessions.py | 安全结构化会话持久化 |
| session_runtime.py | 会话切换、任务互斥和运行状态 |

不得为了匹配 Hcode 目录而搬迁现有模块。只有当某个阶段自身需要且测试证明无回归时，才拆分当前大文件。

## 5. 核心接口

以下接口名是跨阶段契约。阶段细化计划可以补充字段，但改名或改变语义必须先更新本规范和所有后续计划。

### 5.1 类型化事件

core/events.py 提供不可变事件类型：

~~~python
@dataclass(frozen=True, slots=True)
class TextDelta:
    text: str
    agent_id: str = "root"

@dataclass(frozen=True, slots=True)
class ThinkingDelta:
    text: str
    agent_id: str = "root"

@dataclass(frozen=True, slots=True)
class ToolCallCompleted:
    call: ToolCall
    agent_id: str = "root"

@dataclass(frozen=True, slots=True)
class ToolExecutionCompleted:
    call_id: str
    result: ToolResult
    agent_id: str = "root"

@dataclass(frozen=True, slots=True)
class UsageReported:
    usage: TokenUsage
    agent_id: str = "root"

@dataclass(frozen=True, slots=True)
class RuntimeFailed:
    category: str
    safe_message: str
    agent_id: str = "root"
~~~

AgentEvent 是上述事件及规划、审批、压缩、子 Agent 状态和完成事件的联合类型。动态文本必须始终按纯文本渲染。

### 5.2 Provider

ModelProvider 保留 complete 兼容入口，并增加标准流接口：

~~~python
class ModelProvider(Protocol):
    def complete(
        self,
        messages: list[Message],
        tools: tuple[ToolDefinition, ...] = (),
    ) -> ProviderResponse: ...

    def stream(
        self,
        messages: list[Message],
        tools: tuple[ToolDefinition, ...] = (),
        *,
        cancellation: CancellationToken | None = None,
    ) -> AsyncIterator[ProviderEvent]: ...
~~~

OpenAI、DeepSeek 和 GLM 的供应商差异继续由 Provider profile 与协议适配层吸收。Agent Runtime 不解析厂商原始流事件。

### 5.3 Agent Runtime

CodingAgent 增加异步标准入口并保留同步兼容入口：

~~~python
async def run_with_context_async(
    self,
    task: str,
    context: SessionContext,
    *,
    cancellation: CancellationToken | None = None,
    event_sink: EventSink | None = None,
) -> SessionTurnResult: ...
~~~

现有 run 与 run_with_context 在非异步调用者中作为兼容包装。TUI 不得在已有事件循环中调用 asyncio.run。

### 5.4 扩展宿主

~~~python
@dataclass(frozen=True, slots=True)
class ExtensionDescriptor:
    id: str
    kind: Literal["mcp", "skill", "hook"]
    source: str
    enabled: bool
    trust: ExtensionTrust

class ExtensionHost:
    def discover(self) -> tuple[ExtensionDescriptor, ...]: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    def tool_handlers(self) -> tuple[ToolHandler, ...]: ...
    def prompt_fragments(self) -> tuple[str, ...]: ...
~~~

Extension Host 只管理扩展生命周期，不直接决定权限。

### 5.5 工具执行

ToolRegistry 是所有有副作用能力的唯一入口。动态工具至少携带：

- 规范化唯一名称。
- 来源种类和来源标识。
- JSON Schema。
- 风险分类。
- 是否可能写文件、执行进程或访问网络。
- 可审计的安全元数据。

计划新增的动态注册和异步执行契约：

~~~python
@dataclass(frozen=True, slots=True)
class ToolOrigin:
    kind: Literal["builtin", "mcp", "hook", "worktree", "agent"]
    id: str
    risk: Literal["read", "write", "process", "network", "dangerous"]

class ToolRegistry:
    def register(
        self,
        handler: ToolHandler,
        *,
        origin: ToolOrigin,
    ) -> None: ...

    async def execute_async(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        cancellation: CancellationToken | None = None,
    ) -> ToolResult: ...
~~~

内置工具在构造时使用相同注册入口。重复名称、非法来源和缺少风险声明必须在注册期拒绝。现有同步 execute 在兼容期内保留，并与 execute_async 共享校验、审批、审计和变更账本逻辑。

MCP、Worktree 和 Hook 命令动作必须适配为 ToolHandler 或内部等价的受控工具请求。任何扩展不得直接调用 os.system、shell=True 或未受控 subprocess。

## 6. 完整运行数据流

1. UI 将用户任务交给 SessionRuntime。
2. SessionRuntime 固定本次任务的会话、工作区、Provider、权限和扩展快照。
3. Context Manager 加载安全摘要、项目指令、显式启用的 Skills、工具目录和剩余预算。
4. Planner 生成无副作用计划。
5. Provider 将文本、用量和工具调用流转换为统一 ProviderEvent。
6. Agent Runtime 将 ProviderEvent 转换成 AgentEvent，并交给 UI 和审计。
7. 工具调用先经过 pre_tool_use Hooks。
8. Tool Gateway 校验 schema、工作区、权限、危险等级、审批、超时和输出上限。
9. 内置工具、MCP、Worktree 或子 Agent 执行。
10. 变更账本与验证状态先更新，再运行 post_tool_use Hooks。
11. 规范化工具结果进入上下文；旧上下文按完整回合压缩。
12. Agent 继续下一轮或完成任务。
13. SessionRuntime 仅持久化安全摘要和结构化元数据。
14. 取消信号向 Provider、工具、MCP 和子 Agent 传播。

## 7. 安全不变量

以下条件在全部阶段中不可放宽。

### 7.1 默认拒绝

- 未知工具、未知扩展来源、非法 schema 和未知 Hook 动作一律拒绝。
- 配置解析失败不回退到更宽松权限。
- 扩展启动失败不得静默切换到不受控执行。

### 7.2 权限快照

- 任务启动时固定权限快照。
- 运行中的 UI 权限切换只影响下一项任务。
- 子 Agent 有效权限等于父 Agent 权限、用户授予权限和子 Agent 声明权限的交集。
- 子 Agent 不得切换自身权限或替用户批准请求。

### 7.3 路径与文件

- 所有项目文件路径继续经过 WorkspacePolicy 和目录绑定核验。
- 扩展、Skill、Hook 配置和 Worktree 注册表拒绝符号链接或 junction 越界。
- 大工具结果只能写入 TriCoder 管理的运行目录，文件名由系统生成。
- Hcode 的文件状态缓存不能替代 TriCoder 的路径身份核验。

### 7.4 进程

- 只传 argv，禁止 shell=True。
- 可执行文件解析到可信绝对路径。
- 子进程环境继续剔除敏感变量；允许传给 MCP 的凭据必须通过显式环境变量引用逐项授权。
- 输出、运行时间和进程树必须受限。
- 启动 MCP 服务器本身视为代码执行，不因其“只是工具服务器”而自动放行。

### 7.5 网络

- 远程 MCP 默认关闭。
- 启用时仅允许经过验证的 HTTPS 端点。
- URL 禁止 userinfo；认证值不得写入项目配置、日志或会话数据库。
- 审计只记录服务器标识、端点主机、工具名、耗时和结果类别，不记录认证头与任意正文。

### 7.6 Hooks

- pre_tool_use Hook 必须在任何副作用之前完成。
- pre_tool_use Hook 失败或超时采用 fail-closed。
- post_tool_use 和纯通知 Hook 失败采用告警并继续，但必须审计。
- Hook 的命令动作必须重新进入 Tool Gateway，不能从 Hook Engine 直接启动进程。
- Hook 递归触发有深度上限，同一事件链不得无限自触发。

### 7.7 Skills

- Skill 是数据和提示，不是可导入的 Python 插件。
- 目录发现、文件数、单文件大小、总大小和 include 深度必须有上限。
- include 只能位于已授权 Skill 根目录中。
- Skill 声明的工具集只能缩小当前可用工具，不能扩大。
- 参数替换不执行模板表达式和 shell 展开。

### 7.8 Worktree

- Worktree 是显式特权操作。
- 管理目录位于 TriCoder 应用状态目录下的专用根，不放入凭据目录。
- 创建前核验 Git 仓库根；进入 Worktree 后生成新的工作区策略实例。
- 默认不迁移 Hcode 的 node_modules、.venv、vendor 符号链接功能。
- 删除只作用于 TriCoder 注册且 Git 身份匹配的 Worktree；不得按模糊名称递归删除目录。
- Worktree 中的文件变更继续进入变更账本。

### 7.9 子 Agent

- 第一版本只允许单层、前台、只读子 Agent。
- 后续写能力必须加入单写者或 Worktree 隔离。
- 每个子 Agent 有独立 agent_id、轮次、token、时间、工具和工作区预算。
- 取消父任务必须取消所有子任务并清理资源。
- 子 Agent 结果以有界结构化摘要返回，不自动持久化完整对话。
- 并发上限必须配置且有安全默认值。

## 8. 功能迁移映射

| 能力 | Hcode 参考 | TriCoder 落点 | 复用方式 |
| --- | --- | --- | --- |
| 流式 Provider | hcode/client.py | providers.py、core/events.py | 参考协议解析，按 TriCoder Transport 重写 |
| 流式 Agent 事件 | hcode/agent.py | agent.py、core/events.py | 参考事件分类，不复制执行循环 |
| token 预算与压缩 | hcode/context/manager.py | context/manager.py | 适配纯算法，保留完整工具回合语义 |
| 大结果落盘 | hcode/context/manager.py | context/spill.py | 重写路径和权限边界 |
| 指令加载 | hcode/memory/instructions.py | skills/loader.py 或 instructions.py | 适配 include 算法并收紧根目录 |
| Skills | hcode/skills/ | skills/ | 可参考解析和目录设计，不执行安装功能 |
| MCP | hcode/mcp/ | mcp/ | 参考生命周期；工具包装、进程和审计重写 |
| Hooks | hcode/hooks/ | hooks/ | 参考模型与条件；执行器重写 |
| Worktree | hcode/worktree/ | worktree/ | 参考状态机；Git、路径和清理重写 |
| 子 Agent | hcode/agents/、hcode/teams/ | agents/ | 参考任务状态；权限与调度重写 |
| TUI 状态 | hcode/app.py、hcode/tui.py | tui.py | 参考交互，不复制 Agent 状态管理 |
| Session | hcode/memory/session.py | sessions.py | 不迁移自由文本 JSONL |
| Remote | hcode/remote.py | 无 | 本轮排除 |

## 9. 配置设计

继续使用项目 .tricoder.toml，不读取 Hcode 的 .hcode/config.yaml。

建议的新配置命名空间：

~~~toml
[extensions]
enabled = true

[[mcp.servers]]
id = "example"
transport = "stdio"
command = "python"
args = ["-m", "example_mcp"]
enabled = false

[skills]
enabled = true
project_dir = ".tricoder/skills"

[hooks]
enabled = true

[worktree]
enabled = false

[agents]
enabled = false
max_depth = 1
max_concurrency = 1
default_read_only = true
~~~

配置实现必须：

- 对未知字段给出明确错误或警告，不能悄悄忽略关键安全字段。
- 敏感值仅允许环境变量引用，不接受明文 token。
- 功能默认关闭或采用最小权限安全默认值。
- 提供 doctor 输出，但只显示掩码或是否存在。
- 配置优先级继续遵守 TriCoder 已有规则。

最终字段和语法在各阶段计划中通过配置测试锁定。

## 10. 依赖与许可

### 10.1 Hcode 源码

Hcode 仓库当前包含 MIT License，版权标识为 PooooLish。TriCoder 当前未发现根 LICENSE。迁移任何实质性 Hcode 源码前必须完成以下许可门：

1. 确认两个仓库的源码所有权与可迁移范围。
2. 决定 TriCoder 的发布许可证。
3. 如果复制或改编 Hcode 的实质性代码，保留 MIT 版权和许可文本，并在 NOTICE 或等价文件记录来源、文件范围和提交标识。
4. 如果只依据行为重新实现，也在 open-source-assessment 中记录 reference 边界。

许可未明确时可以写独立实现和测试设计，但不得复制 Hcode 实质性代码。

### 10.2 第三方依赖

预期至少评估：

- 官方 Python MCP SDK：MCP 客户端与协议。
- PyYAML：兼容 Hcode Skill front matter 时可能需要。
- 现有 Rich 与 Textual：继续用于 CLI/TUI。

每次新增依赖必须单独完成身份、许可证、安全公告、维护状态、安装副作用、传递依赖和锁文件审查。不得直接沿用 Hcode 的版本范围，也不得在没有用户批准时安装或更新锁文件。

## 11. 故障模型

| 故障 | 处理 |
| --- | --- |
| Provider 流中断 | 保留已显示文本但不把不完整工具调用交给执行器；返回可重试安全错误 |
| 取消 | 停止读取 Provider；终止受管工具/MCP/子 Agent；密封或回滚当前变更账本 |
| MCP 启动失败 | 标记服务器不可用，不注册其工具；内置工具仍可工作 |
| MCP 运行中崩溃 | 当前调用返回规范化错误；禁用该连接；是否重连由有界策略决定 |
| Skill 解析失败 | 不加载该 Skill，向 doctor 和审计报告路径与错误类别，不输出正文 |
| pre Hook 失败 | 拒绝对应动作 |
| post Hook 失败 | 工具结果保持有效，记录告警 |
| Worktree 创建失败 | 运行受控 Git 清理；只清理已确认由本次创建的状态 |
| Worktree 删除冲突 | 停止并要求人工处理，不强制删除 |
| 子 Agent 失败 | 将有界错误结果返回父 Agent，不扩大预算重试 |
| 持久化失败 | 延续当前 unsaved_memory 行为，禁止以空状态覆盖已有状态 |

## 12. 测试策略

### 12.1 测试层级

1. 纯单元测试：事件、配置、解析、权限交集、预算和状态机。
2. 契约测试：Provider 流、ToolHandler、Extension Host、Hook、MCP 和子 Agent 接口。
3. 安全集成测试：路径越界、符号链接、环境变量泄漏、审批绕过、取消和超时。
4. 本地协议集成测试：使用仓库内 fake MCP server 和 fake Provider，不联网、不使用真实 Key。
5. TUI pilot 测试：流式文本、审批、取消、子 Agent 状态和扩展错误。
6. Eval：迁移前后运行固定 smoke suite，比对成功率、工具调用数、轮次、验证状态和失败类别。
7. CI：Windows/Linux，Python 3.11/3.12；不启动真实 Provider。
8. 人工真实测试：仅在用户显式执行时使用真实 API，凭据只来自进程环境。

### 12.2 每阶段门槛

- 新测试先失败，再写最小实现。
- 聚焦测试通过。
- 完整 unittest 通过。
- compileall 通过。
- 不出现新增明文凭据、自由文本持久化或未受控 subprocess。
- Git diff 只包含本阶段文件和已声明文档。
- 现有 Eval smoke 不退化。

### 12.3 测试移植规则

Hcode 测试是场景来源，不应机械复制：

- 先写 TriCoder 行为契约。
- 再挑选 Hcode 中对应边界场景。
- 将 fixture 改为 TriCoder 的 Policy、ToolRegistry、Audit 和 SessionRuntime。
- 保留 Hcode 已暴露过的回归场景，例如流式工具在 pre_tool_use 之前执行。
- 测试不得读取真实用户配置、密钥目录或真实 API。

## 13. 迁移阶段与依赖

~~~text
Phase 0  基线、许可与迁移清单
   |
Phase 1  类型化事件、取消与异步兼容层
   |
Phase 2  Provider 流式输出与 Agent 异步循环
   |
Phase 3  token 上下文管理与大结果落盘
   |
Phase 4  Extension Host 和配置信任模型
   |
Phase 5  MCP
   |
Phase 6  Skills 与项目指令
   |
Phase 7  Hooks
   |
Phase 8  Worktree
   |
Phase 9  子 Agent：只读单层
   |
Phase 10 子 Agent：受控写入、并发与任务树
   |
Phase 11 TUI、Eval、CI、文档和发布门
~~~

Phase 5 至 Phase 8 在代码上依赖同一 Extension Host，不能在没有明确文件所有权的情况下并行修改共享模块。Phase 9 必须等待 MCP、Hooks、Worktree 的权限契约稳定。

## 14. 阶段完成定义

每个阶段只有同时满足以下条件才算完成：

- 该阶段规范中的公开接口有测试。
- 现有行为兼容或变更被明确记录。
- 安全不变量有对应回归测试。
- 聚焦与完整测试结果已写入 project.md。
- 依赖和锁文件变更已审查。
- 文档与 CLI help 已同步。
- 没有未解释的测试跳过。
- Git diff 已自审查。
- 用户明确批准进入下一阶段。

## 15. 整体完成定义

- TriCoder 可发现并调用受控 MCP 工具。
- TriCoder 可加载受限 Skills 和项目指令。
- Hooks 可在模型与工具生命周期中安全运行。
- TriCoder 可创建、进入和清理受管 Worktree。
- 父 Agent 可创建受预算、权限和工作区约束的子 Agent。
- CLI 与 TUI 均能显示流式输出、扩展状态、子任务状态和取消结果。
- 所有动态能力共享 Tool Gateway、AuditLogger 和安全路径边界。
- 会话数据库仍不持久化自由文本、源码、工具输出或认证信息。
- Windows/Linux 和 Python 3.11/3.12 CI 通过。
- 离线 Eval 通过；真实 Provider 测试由用户选择执行。
- SECURITY、README、架构文档和迁移来源说明准确。

## 16. 回滚原则

- 每个 Phase 使用独立分支或执行时创建的安全 Git worktree。
- 每个 Phase 只产生一个可独立审查的能力增量。
- 新能力使用默认关闭的 feature flag，直至该阶段验收通过。
- 不通过时回滚该阶段提交，不修改之前稳定阶段。
- 数据库 schema 变更必须向前兼容；未启用新功能时旧数据库仍可读取。
- 依赖变更与功能实现分开审查，必要时可移除依赖并关闭对应模块。

## 17. 暂缓事项

以下能力不属于此次完成标准：

- Hcode Remote/WebSocket 服务。
- OS 级 sandbox 后端。
- 任意第三方 Python 插件。
- 无限制 Agent 递归。
- 无人审批的跨工作区写入。
- 自动安装 Skills 或 MCP 服务器。
- 完整会话原文持久化。

它们必须在平台迁移完成后分别进行新设计和安全评审。
