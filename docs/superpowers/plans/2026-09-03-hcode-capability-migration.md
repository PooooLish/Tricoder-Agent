# Hcode 能力迁移到 TriCoder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** 在不削弱 TriCoder 既有安全与隐私边界的前提下，分阶段迁移 Hcode 的 MCP、Skills、Hooks、Worktree 和子 Agent 能力，并补齐流式运行时、上下文预算、TUI、Eval 与跨平台验证。

**Architecture:** TriCoder 保持主体地位。新增异步事件层、Context Manager 与 Extension Host；所有动态能力通过 ToolRegistry、WorkspacePolicy、审批、AuditLogger、Subprocess Control 和 ChangeJournal 的统一执行边界接入。Hcode 只作为本地只读的设计、算法和测试场景来源。

**Tech Stack:** Python 3.11+、标准库 asyncio、现有 Rich/Textual、现有 unittest、待审查并批准后引入的官方 Python MCP SDK，以及 Skills front matter 确有必要时引入的 YAML 解析器。

**Spec:** docs/superpowers/specs/2026-09-03-hcode-capability-migration-design.md

## Global Constraints

- 目标仓库固定为 projects/tricoder-cli；来源仓库 projects/hcode 在迁移中保持只读。
- 先读工作区与两个项目的 AGENTS.md、目标仓库 project.md、设计规范和本计划。
- 不读取 .env.local、.local/secrets、.local/envs 或任何凭据目录。
- 不打印、复制、记录或提交真实 API Key、token、密码和私有数据。
- 文件删除、依赖安装、Git worktree 创建、commit 和 push 均需当时取得用户明确批准。
- 不使用 git reset --hard、git clean、破坏性 checkout、sudo、未知安装器或 curl 管道执行。
- 不覆盖目标仓库进入任务前已有的修改和未跟踪文件。
- 每个阶段只修改该阶段声明的文件；共享文件冲突时停止并重新规划。
- 新能力默认关闭或采用最小权限默认值。
- ToolRegistry 是所有有副作用操作的唯一执行入口。
- 任务启动时固定权限快照；子 Agent 权限只能缩小。
- SQLite 不持久化任务原文、模型自由文本、源码、工具输出或认证信息。
- Remote 与 OS 级沙箱不属于本计划。
- 不复制 Hcode 的安全缺陷、自由文本 Session JSONL、Remote 或裸 subprocess 路径。
- 每阶段执行前编写该阶段的细化实现计划；一次只执行一个阶段。
- 每阶段采用测试先行、聚焦验证、完整回归、diff 自审查和用户检查点。

---

## 0. 使用方式与当前仓库保护

本文件是跨会话主计划，不应在一个会话内连续完成全部阶段。推荐每个 Phase 使用一个新 Codex 会话，并在阶段末更新本文件的复选框和 project.md 证据。

每次开始一个 Phase，执行者必须先运行：

~~~powershell
git -C D:\MaHong\AGENT_WORKSPACE_V2\projects\tricoder-cli status --short --branch
git -C D:\MaHong\AGENT_WORKSPACE_V2\projects\hcode status --short --branch
Get-Content D:\MaHong\AGENT_WORKSPACE_V2\AGENTS.md
Get-Content D:\MaHong\AGENT_WORKSPACE_V2\projects\tricoder-cli\AGENTS.md
Get-Content D:\MaHong\AGENT_WORKSPACE_V2\projects\tricoder-cli\project.md
Get-Content D:\MaHong\AGENT_WORKSPACE_V2\projects\tricoder-cli\docs\superpowers\specs\2026-09-03-hcode-capability-migration-design.md
Get-Content D:\MaHong\AGENT_WORKSPACE_V2\projects\tricoder-cli\docs\superpowers\plans\2026-09-03-hcode-capability-migration.md
~~~

已知的计划编写时工作区状态：

- TriCoder main 比 origin/main 超前 19 个提交。
- TriCoder 有既存修改：test/README.md、test/smoke_demo.py。
- TriCoder 有既存未跟踪文件：docs/project-deep-dive.md、run_tests.ps1 以及若干 test 下示例和测试。
- 目标仓库 .pytest_cache 读取时可能报告 Permission denied。
- Hcode main 与 origin/main 对齐，但有此前 Eval 规划文档相关的本地改动。

这些状态仅供识别既存工作，执行者必须以当时的 git status 为准，不能清理或吸收无关改动。

通用阶段验证：

~~~powershell
Set-Location D:\MaHong\AGENT_WORKSPACE_V2\projects\tricoder-cli
.\.venv\Scripts\python.exe -B -m unittest discover -s tests
.\.venv\Scripts\python.exe -B -m compileall -q src tests
.\.venv\Scripts\python.exe -B -m tricoder eval evals\smoke --dry-run --no-color
~~~

如 .venv 不存在或不可运行，停止并报告；不得自行安装或修改环境。

---

## Phase 0：基线、来源、许可和迁移清单

### Task 0.1：冻结可复现基线

**Files:**

- Modify: project.md
- Create: docs/migration/hcode-capability-map.md

**Interfaces:**

- Consumes: 当前 TriCoder 和 Hcode Git 状态、测试结果、设计规范
- Produces: 可恢复的基线提交标识、测试证据和逐模块迁移清单

- [x] **Step 1: 记录只读基线**

记录两个仓库的 HEAD、分支、remote、git status、Python 版本和 TriCoder 当前依赖列表。不得记录环境变量值。

- [x] **Step 2: 运行现有完整测试**

Run:

~~~powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests
.\.venv\Scripts\python.exe -B -m compileall -q src tests
.\.venv\Scripts\python.exe -B -m tricoder eval evals\smoke --dry-run --no-color
~~~

Expected: unittest 与 compileall 退出码为 0；dry-run 成功枚举 smoke cases。若数量与 project.md 不同，记录实际数量，不把旧数字当作新证据。

2026-09-03 初次结果：`compileall` 退出 0，unittest 与 dry-run 因项目 `.venv`
为 Python 3.10.16、缺少 Python 3.11 标准库 `tomllib` 而退出 1。根因通过只读
诊断确认。经用户随后明确授权，旧环境备份到 `runtime/env-backups/` 并用 Python
3.11.6 重建 `.venv`；仅安装项目既有声明依赖。重跑结果：unittest 545 tests
全部通过（4 skipped），`compileall` 退出 0，dry-run 验证 3 个 smoke cases。

- [x] **Step 3: 建立能力映射**

docs/migration/hcode-capability-map.md 必须逐项记录：

| 字段 | 内容 |
| --- | --- |
| Capability | streaming、context、MCP、Skills、Hooks、Worktree、sub-agent、TUI |
| Hcode source | 精确文件和基线提交 |
| TriCoder target | 精确目标文件 |
| Reuse mode | reference、adapt 或 rewrite |
| Security delta | 必须收紧或保留的边界 |
| Test source | Hcode 场景与 TriCoder 新测试位置 |
| Status | not-started、in-progress、verified |

- [x] **Step 4: 更新 project.md**

将当前里程碑改为本迁移，记录 Phase 0 的测试证据、既存脏工作树边界和下一步 Phase 1。

### Task 0.2：许可与依赖门

**Files:**

- Modify: docs/open-source-assessment.md
- Create after user license decision: NOTICE
- Modify after user license decision: pyproject.toml
- Create or modify after user license decision: LICENSE

**Interfaces:**

- Consumes: Hcode MIT License、两个仓库的所有权信息、用户的 TriCoder 许可证决定
- Produces: 允许 reference、adapt 或 copy 的书面边界

- [x] **Step 1: 核对 Hcode 来源**

记录 Hcode 基线提交、MIT License 文本和版权标识。检查待迁移文件是否存在额外文件头或第三方来源说明。

- [x] **Step 2: 请求 TriCoder 发布许可证决定**

推荐 MIT，以便与 Hcode 一致。用户未明确决定前，不创建或修改 LICENSE，不复制 Hcode 实质性实现。

2026-09-03：用户已明确选择 MIT。

- [x] **Step 3: 写迁移许可记录**

NOTICE 至少记录 Hcode 仓库、基线提交、MIT、版权标识和发生 adapt/copy 的文件清单。若全部采用独立重写，则在 open-source-assessment 中记录 reference 边界并说明没有复制实现。

- [x] **Step 4: 建立依赖审批表**

对官方 Python MCP SDK 和可能的 YAML 解析器记录 need、identity、license、security、maintenance、runtime、manifest impact、decision 和 rollback。必须使用执行当日的权威来源；没有完整证据时结论为 defer。

**Phase 0 exit gate:**

- 基线测试证据真实可复现。
- 迁移清单覆盖全部目标能力。
- 实质性源码复用前已解决许可证和 NOTICE。
- 未安装依赖，除非用户另行明确批准。

当前状态：许可证、文档和绿色基线门均已满足。用户明确授权删除进入任务前
`test/README.md` 末尾的一个多余空行后，全仓库 `git diff --check` 通过。
Phase 0 完成，Phase 1 尚未开始。

---

## Phase 1：类型化事件、取消和预算基础

### Task 1.1：建立 core 契约

**Files:**

- Create: src/tricoder/core/__init__.py
- Create: src/tricoder/core/events.py
- Create: src/tricoder/core/cancellation.py
- Create: src/tricoder/core/budgets.py
- Create: tests/test_core_events.py
- Create: tests/test_cancellation.py
- Create: tests/test_budgets.py

**Interfaces:**

- Produces: AgentEvent、ProviderEvent、EventSink、CancellationToken、CancellationError、ExecutionBudget、BudgetExceeded

- [x] **Step 1: 写事件不可变性与纯数据测试**

覆盖文本、thinking、工具开始/完成、usage、压缩、子 Agent、失败和完成事件；断言事件不可变、无 Provider SDK 类型、动态正文不进入 repr 中的秘密字段。

- [x] **Step 2: 写取消传播测试**

覆盖初始未取消、幂等 cancel、父 token 取消子 token、子 token 不反向取消父 token，以及 raise_if_cancelled。

- [x] **Step 3: 写预算测试**

覆盖轮次、token、时间和子任务计数的消费、边界值与拒绝；预算对象不得因并发消费出现负数。

- [x] **Step 4: 运行 RED**

Run:

~~~powershell
.\.venv\Scripts\python.exe -B -m unittest tests.test_core_events tests.test_cancellation tests.test_budgets -v
~~~

Expected: 因模块尚不存在而失败。

- [x] **Step 5: 实现最小契约**

只使用标准库 dataclasses、enum、threading 和 time。CancellationToken 必须可从 TUI 线程安全触发；预算消费必须在锁内完成。

- [x] **Step 6: 运行 GREEN 与完整回归**

Run focused tests, then the common stage verification.

- [x] **Step 7: 审查导入边界**

core 不得导入 tui、sessions、tools 的具体实现、任何 Provider SDK 或 Hcode 包。

**Phase 1 exit gate:** 新 core API 稳定；现有 CodingAgent 尚未改变行为。

当前状态：2026-09-03 已完成。RED 因 `tricoder.core` 尚不存在产生 3 个预期
导入错误；实现后新增 12 个契约测试通过，完整测试 557 项通过（4 skipped）。
`compileall`、3 个 smoke Eval dry-run case 和 core 导入边界检查均通过。详细证据
见 `docs/migration/phase-1-verification.md`。本阶段未接入 `CodingAgent`，未安装
依赖，也未进入 Phase 2。

---

## Phase 2：Provider 流式输出与异步 Agent

### Task 2.1：流式 Transport 与 Provider 协议

**Files:**

- Modify: src/tricoder/providers.py
- Modify: src/tricoder/models.py
- Create: tests/test_provider_streaming.py
- Modify: tests/test_providers.py

**Interfaces:**

- Consumes: ProviderEvent、CancellationToken
- Produces: ModelProvider.stream(...) -> AsyncIterator[ProviderEvent]

- [x] **Step 1: 写 Provider 流契约测试**

使用 fake byte stream 覆盖：UTF-8 跨 chunk、SSE 多行 data、结束标记、文本增量、分片 tool arguments、usage、未知事件、超大事件、连接中断和取消。

- [x] **Step 2: 写“不完整工具不执行”测试**

流在 ToolCallCompleted 前中断时，最终响应不得包含可执行 ToolCall。

- [x] **Step 3: 运行 RED**

Run:

~~~powershell
.\.venv\Scripts\python.exe -B -m unittest tests.test_provider_streaming tests.test_providers -v
~~~

- [x] **Step 4: 实现 OpenAI-compatible 流转换**

保持现有 urllib 与 Provider profile 设计；不为流式功能引入 OpenAI SDK。设置响应字节、单事件和累计输出上限。每次等待或读取前检查 CancellationToken。

- [x] **Step 5: 保留 complete 兼容**

complete 的现有契约和测试必须继续通过。可以内部聚合 stream，但 Provider 错误类别、重试和大小限制不能改变。

### Task 2.2：异步 Agent Runtime

**Files:**

- Modify: src/tricoder/agent.py
- Modify: src/tricoder/session_runtime.py
- Modify: src/tricoder/cli.py
- Modify: src/tricoder/tui.py
- Modify: tests/test_agent.py
- Modify: tests/test_session_runtime.py
- Modify: tests/test_cli.py
- Modify: tests/test_tui.py

**Interfaces:**

- Consumes: ModelProvider.stream、AgentEvent、CancellationToken
- Produces: CodingAgent.run_with_context_async、同步兼容包装、SessionRuntime.cancel_current

- [x] **Step 1: 写 async Agent 行为测试**

同一 fake Provider 响应下，同步入口和异步入口必须产生等价 RunResult、SessionContext、工具次数、usage 和审计类别。

- [x] **Step 2: 写事件顺序测试**

覆盖 round start、text delta、完整 tool call、审批、tool result、usage 和 completion。断言工具执行只发生在 ToolCallCompleted 与 pre-tool 检查之后。

- [x] **Step 3: 写取消测试**

覆盖 Provider 读取中取消、命令执行中取消、工具调用之间取消和 TUI 退出取消。取消不得留下半个工具回合或把任务标记为验证通过。

- [x] **Step 4: 运行 RED**

Run focused agent/runtime/UI tests.

- [x] **Step 5: 提取共享单轮逻辑**

避免复制现有规划、协议、审计、变更账本和完成判定。run_with_context_async 是规范实现；同步包装只负责建立事件循环并拒绝在已运行事件循环中错误嵌套。

- [x] **Step 6: 接入 CLI 与 TUI**

CLI 可批量显示增量；TUI 使用线程安全事件桥更新 RichLog。所有动态文本继续使用 Text，不启用 markup。

- [x] **Step 7: 回归 native 与 legacy_json**

两种协议的完整工具回合、invalid action 和 finish 顺序必须继续通过。

**Phase 2 exit gate:** TriCoder 有真实流式路径、可取消 Agent、兼容同步入口；MCP 尚未接入。

---

## Phase 3：token 上下文预算和大工具结果落盘

### Task 3.1：Context Manager

**Files:**

- Create: src/tricoder/context/__init__.py
- Create: src/tricoder/context/manager.py
- Create: tests/test_context_manager.py
- Modify: src/tricoder/agent.py
- Modify: tests/test_agent.py

**Interfaces:**

- Consumes: Message、TokenUsage、ActionProtocol.complete_round
- Produces: ContextBudget、ContextSnapshot、ContextManager.prepare、ContextManager.record_usage

- [x] **Step 1: 写预算与回合边界测试**

覆盖固定 system/task 消息、多个工具调用的完整回合、孤立 call/result、最后一轮超预算、usage 可用和 usage 缺失时的保守估算。

- [x] **Step 2: 写与旧 compact_session_messages 的兼容测试**

在字符预算模式下，现有测试样本必须得到相同的保留顺序和孤立回合处理。

- [x] **Step 3: 运行 RED 并实现最小 ContextManager**

优先使用 Provider usage；没有 tokenizer 时采用明确记录的保守估算，不新增 tokenizer 依赖。

- [x] **Step 4: 将 agent.py 的压缩职责委托给 ContextManager**

先保留原函数作为兼容代理，待完整回归后再由后续独立清理任务决定是否删除。

### Task 3.2：工具结果 spill

**Files:**

- Create: src/tricoder/context/spill.py
- Create: tests/test_context_spill.py
- Modify: src/tricoder/tools/__init__.py
- Modify: src/tricoder/session_runtime.py

**Interfaces:**

- Produces: ToolResultSpillStore.persist、ToolResultSpillStore.preview、ToolResultSpillStore.cleanup

- [x] **Step 1: 写受控目录与清理测试**

覆盖系统生成文件名、session 隔离、权限失败、符号链接目标拒绝、重复 call id、大小上限、读取引用和只清理当前 session。

- [x] **Step 2: 实现运行目录**

目录位于 TriCoder 管理的状态或 runtime 根，不位于目标源码目录。审计只记录大小、哈希和相对状态标识，不记录正文。

- [x] **Step 3: 接入 ToolResult 预算**

大结果在返回模型前替换为有界预览和读取说明；不能向模型暴露本机绝对路径。

- [x] **Step 4: 验证会话数据库**

查询测试数据库 schema 与内容，断言 spill 正文和路径未写入 SQLite。

**Phase 3 exit gate:** 长上下文按 token/完整回合管理，大结果有安全生命周期。

---

## Phase 4：Extension Host 与配置信任模型

### Task 4.1：扩展描述与宿主

**Files:**

- Create: src/tricoder/extensions/__init__.py
- Create: src/tricoder/extensions/models.py
- Create: src/tricoder/extensions/host.py
- Create: tests/test_extension_host.py

**Interfaces:**

- Produces: ExtensionKind、ExtensionTrust、ExtensionDescriptor、ExtensionFailure、ExtensionProvider、ExtensionHost
- Produces through ToolRegistry: ToolOrigin、ToolRegistry.register、ToolRegistry.execute_async

- [x] **Step 1: 写生命周期测试**

覆盖 discover、start、部分启动失败、工具名冲突、stop 逆序、重复 stop、取消和失败隔离。

- [x] **Step 2: 写冲突与来源测试**

内置工具名优先；两个扩展生成相同规范名时两者都不注册并报告冲突。descriptor source 必须是安全标识，不得含认证信息。

- [x] **Step 3: 实现宿主**

宿主聚合动态 ToolHandler 和 prompt fragments，不导入 UI，不执行权限判断，不直接写 Session。

- [x] **Step 4: 增加 ToolRegistry 动态注册和异步执行**

内置与动态工具共用 register；重复名称、非法 origin 和缺少风险声明时拒绝。execute 与 execute_async 共享参数校验、审批、审计和 ChangeJournal 路径，不维护两套执行逻辑。

### Task 4.2：配置模型与 doctor

**Files:**

- Modify: src/tricoder/models.py
- Modify: src/tricoder/config.py
- Modify: src/tricoder/cli.py
- Modify: tests/test_config.py
- Modify: tests/test_cli.py
- Modify: .env.example
- Modify: README.md

**Interfaces:**

- Consumes: .tricoder.toml 与现有 CLI/env 优先级
- Produces: ExtensionsConfig、MCPConfig、SkillsConfig、HooksConfig、WorktreeConfig、AgentsConfig

- [x] **Step 1: 写默认关闭和严格解析测试**

覆盖未知 transport、重复 id、明文 secret 字段、非法路径、非布尔 enabled、负预算和未知关键安全字段。

- [x] **Step 2: 写敏感引用测试**

配置只保存 env 名称；解析时不得把值放入 dataclass repr、错误、doctor 或审计。

- [x] **Step 3: 实现配置并扩展 doctor**

doctor 只显示 extension id、类型、启用状态、信任等级和凭据是否存在。

**Phase 4 exit gate:** 扩展宿主可用 fake providers 测试；所有真实扩展仍默认关闭。

---

## Phase 5：MCP

本阶段采用已批准的任务级生命周期设计；实施时以细化计划
`docs/superpowers/plans/2026-09-03-tricoder-mcp-integration.md` 为准。细化计划补充了
SessionRuntime/一次性 CLI 接线、强制启动审批、递归 Schema 子集、最小环境和
确定性清理要求；与下方早期摘要冲突时，以细化计划和对应设计为准。

### Task 5.1：依赖审查与可选导入边界

**Files:**

- Modify: docs/open-source-assessment.md
- Create: docs/framework/mcp-integration.md
- Modify after explicit approval: pyproject.toml
- Modify after explicit approval: dependency lock file
- Create: tests/test_mcp_dependency_boundary.py

**Interfaces:**

- Produces: 明确 pin 的 MCP SDK 决策、可选导入错误和移除路径

- [x] **Step 1: 完成当日依赖审查**

核对官方仓库、包发布者、最新兼容版本、许可证、安全公告、Python 版本、传递依赖、安装脚本和网络行为。Hcode 的 mcp 版本范围只作参考。

- [x] **Step 2: 请求安装与锁文件变更批准**

列出将变化的 manifest、lock 和环境。用户未批准时停止，不运行 pip、uv 或其他安装命令。

- [x] **Step 3: 写缺少 MCP 依赖时的测试**

未安装或不可导入时，TriCoder 非 MCP 功能必须正常；启用 MCP 时返回明确配置错误。

- [x] **Step 4: 在批准后固定版本并核对 lock diff**

只加入已批准依赖，不顺带升级 Rich、Textual 或其他包。

### Task 5.2：MCP 客户端和生命周期

**Files:**

- Create: src/tricoder/mcp/__init__.py
- Create: src/tricoder/mcp/client.py
- Create: src/tricoder/mcp/manager.py
- Create: tests/fixtures/fake_mcp_server.py
- Create: tests/test_mcp_manager.py

**Interfaces:**

- Produces: MCPServerState、MCPClient、MCPManager.start_all、MCPManager.call_tool、MCPManager.stop_all

- [x] **Step 1: 写 fake stdio MCP server**

只使用固定工具和固定响应；不访问网络、用户目录或环境秘密；支持正常、慢响应、崩溃、超大输出和非法 schema 模式。

- [x] **Step 2: 写生命周期测试**

覆盖初始化、工具列表、调用、超时、取消、进程退出、重复关闭、stderr 限制和部分服务器失败。

- [x] **Step 3: 写环境过滤测试**

默认不得向服务器传递 Provider Key 或名称匹配 token/password/secret/credential 的变量；显式 env 引用只传指定项。

- [x] **Step 4: 实现受控 stdio transport**

服务器启动必须经过 MCP 专用受控进程策略：shell=False、固定 argv、可信可执行文件、受限 cwd、受限 env、输出和进程树终止。

- [x] **Step 5: 暂不启用远程 transport**

配置解析可以识别并拒绝未启用的远程 transport；远程 MCP 需另一个安全评审任务。

### Task 5.3：MCP Tool Adapter

**Files:**

- Create: src/tricoder/mcp/tool_adapter.py
- Modify: src/tricoder/tools/__init__.py
- Modify: src/tricoder/extensions/host.py
- Create: tests/test_mcp_tool_adapter.py
- Modify: tests/test_tools.py
- Modify: tests/test_audit.py

**Interfaces:**

- Consumes: MCP tool schema、MCPManager.call_tool
- Produces: MCPToolHandler(ToolHandler)

- [x] **Step 1: 写名称与 schema 测试**

使用 mcp__server__tool 形式的稳定名称；覆盖非法字符、碰撞、嵌套 schema、未知类型和超大 schema。

- [x] **Step 2: 写审批与审计测试**

MCP 调用必须触发现有审批；fullaccess 是否自动允许由工具风险声明决定，不能由 MCP server 决定。审计不得包含参数正文或认证头。

- [x] **Step 3: 写故障规范化测试**

服务器错误、取消、超时和非法返回转换为有界 ToolResult；不能抛出 SDK 对象到 Agent/UI。

- [x] **Step 4: 实现并注册适配器**

动态工具注册使用 ToolRegistry 公共扩展方法，不直接修改私有 handler 字典。

**Phase 5 exit gate:** fake MCP 完整链路通过；真实服务器默认不启动；非 MCP 启动无额外失败。

2026-09-06 交接证据：Windows / Python 3.11.6 上 `test_mcp*.py` 110 项通过（1 项
当前账户无创建 symlink 权限而跳过），完整 `tests` 762 项通过（5 项同类平台能力
跳过），并完成 compileall、离线 doctor、3 个 smoke eval dry-run 用例及 workspace
doctor。测试覆盖 task-local 生命周期、启动/工具审批、Schema 拒绝、凭据过滤、
取消/超时清理、禁用路径和仓库内 fake stdio；不把本机结果延伸为跨平台或真实
Provider/MCP server 结论。

Task 9 fix round 1 追加 CP936 doctor 退出 0 的回归：严格 CP936 内存流测试先复现
U+2022 Key 掩码的 `UnicodeEncodeError`，再以 ASCII 掩码修复。固定合成 Key、显式
离线 env 文件和 `--no-color` 的 CP936 与 UTF-8 doctor 均退出 0，且不发送网络请求。

---

## Phase 6：Skills 与项目指令

### Task 6.1：Skill 数据模型和解析

**Files:**

- Create: src/tricoder/skills/__init__.py
- Create: src/tricoder/skills/models.py
- Create: src/tricoder/skills/parser.py
- Create: tests/test_skill_parser.py
- Modify after approval if required: pyproject.toml
- Modify after approval if required: dependency lock file

**Interfaces:**

- Produces: SkillDefinition、SkillSource、SkillParseError、parse_skill_file、substitute_skill_arguments

- [ ] **Step 1: 决定 front matter 格式**

优先评估标准库可实现的受限格式。若兼容 Hcode YAML 是明确需求，完成 PyYAML 依赖审查并取得安装批准。

- [ ] **Step 2: 写解析边界测试**

覆盖必需字段、重复字段、未知安全字段、UTF-8、文件大小、参数长度、字面替换、拒绝表达式执行和恶意 YAML 标签。

- [ ] **Step 3: 实现纯数据解析器**

解析模块不得导入、执行或 eval Skill 中的任何代码。

### Task 6.2：发现、include 与渐进加载

**Files:**

- Create: src/tricoder/skills/loader.py
- Create: src/tricoder/instructions.py
- Create: tests/test_skill_loader.py
- Create: tests/test_instructions.py
- Modify: src/tricoder/extensions/host.py
- Modify: src/tricoder/agent.py

**Interfaces:**

- Produces: SkillLoader.discover、SkillLoader.catalog、SkillLoader.load、InstructionLoader.load

- [ ] **Step 1: 写目录安全测试**

覆盖项目根、嵌套目录、符号链接/junction、大小和数量上限、include 循环、include 深度和工作区外路径。

- [ ] **Step 2: 写渐进加载测试**

初始 prompt 只含名称和有界摘要；正文仅在明确调用后注入；同一 Skill 重复加载行为稳定。

- [ ] **Step 3: 写工具收缩测试**

Skill 指定工具列表时只能取得当前 ToolRegistry 与声明列表的交集。

- [ ] **Step 4: 实现 loader 与 prompt fragment**

不迁移 Hcode 的 InstallSkill 工具。项目 Skill 只能由用户放置或受控仓库变更产生。

- [ ] **Step 5: 扩展 CLI/TUI 命令**

新增只读命令用于列出、查看来源和激活 Skill。动态正文按纯文本显示，不写入 Session 数据库。

**Phase 6 exit gate:** Skills 可安全发现和按需加载；无自动安装、无权限扩大。

---

## Phase 7：Hooks

### Task 7.1：Hook 模型、条件和生命周期

**Files:**

- Create: src/tricoder/hooks/__init__.py
- Create: src/tricoder/hooks/models.py
- Create: src/tricoder/hooks/engine.py
- Create: tests/test_hooks.py
- Modify: src/tricoder/extensions/host.py

**Interfaces:**

- Produces: HookEvent、HookDecision、HookContext、HookDefinition、HookEngine.run

- [ ] **Step 1: 锁定事件集合**

第一版包括 session_start、before_model、after_model、pre_tool_use、post_tool_use、task_complete 和 task_failed。

- [ ] **Step 2: 写条件与模板安全测试**

条件只允许等值、前缀和已枚举字段匹配；模板只允许已知上下文字段的字面替换。拒绝属性遍历、表达式和 shell 展开。

- [ ] **Step 3: 写 fail-closed 测试**

pre_tool_use Hook 超时、异常、未知 decision 或取消时，工具不得执行。

- [ ] **Step 4: 写 post failure 测试**

post_tool_use 失败不回滚已完成工具，但产生安全告警和审计事件。

- [ ] **Step 5: 实现纯决策 Hook**

首个增量只允许 allow、reject、add_context 和 notify；不直接执行命令。

### Task 7.2：受控 Hook 命令动作

**Files:**

- Modify: src/tricoder/hooks/models.py
- Modify: src/tricoder/hooks/engine.py
- Modify: src/tricoder/tools/__init__.py
- Modify: src/tricoder/audit.py
- Modify: tests/test_hooks.py
- Modify: tests/test_audit.py

**Interfaces:**

- Produces: HookToolRequest，经 ToolRegistry 执行

- [ ] **Step 1: 写旁路防护测试**

patch subprocess.Popen、os.system 和常见执行入口，断言 Hook Engine 从不直接调用它们。

- [ ] **Step 2: 写递归上限测试**

Hook 产生的工具请求带 event_chain_id 和 depth；达到上限时拒绝且不继续触发 Hook。

- [ ] **Step 3: 接入 ToolRegistry**

Hook 命令转换为 run_command 工具请求，带 origin=hook:<id>，接受正常策略、审批、环境过滤和审计。

- [ ] **Step 4: 移植 Hcode 回归场景**

加入“流式工具必须先经过 pre_tool_use 才执行”的平台无关测试。

**Phase 7 exit gate:** Hooks 能拒绝、补充上下文、通知和受控请求命令，但无直接副作用路径。

---

## Phase 8：Worktree

### Task 8.1：Git 服务与 Worktree 状态机

**Files:**

- Create: src/tricoder/worktree/__init__.py
- Create: src/tricoder/worktree/models.py
- Create: src/tricoder/worktree/service.py
- Create: tests/test_worktree_service.py
- Modify: src/tricoder/subprocess_control.py
- Modify: src/tricoder/policy.py

**Interfaces:**

- Produces: WorktreeRecord、WorktreeState、WorktreeService.create、enter、leave、remove、list

- [ ] **Step 1: 写真实临时 Git 仓库测试**

使用临时目录和本机 git，覆盖创建、进入、列表、脏状态、分支冲突、非仓库、路径含空格和取消。

- [ ] **Step 2: 写路径与身份测试**

覆盖管理根越界、符号链接/junction、同名替换、外部删除、HEAD 不匹配和伪造注册表。

- [ ] **Step 3: 写删除拒绝测试**

未注册、身份不匹配、存在未提交变更或 Git 不认可的 Worktree 一律拒绝删除。测试不使用模糊递归删除。

- [ ] **Step 4: 实现专用 Git argv**

不把任意 git 参数暴露给模型。每个操作使用固定子命令和固定允许参数，经受控进程执行。

- [ ] **Step 5: 禁用依赖目录 symlink**

不迁移 Hcode 的 node_modules、.venv、vendor 链接功能。若未来需要，单独设计复制或缓存策略。

### Task 8.2：Session、命令与变更账本集成

**Files:**

- Modify: src/tricoder/session_runtime.py
- Modify: src/tricoder/sessions.py
- Modify: src/tricoder/commands.py
- Modify: src/tricoder/shell.py
- Modify: src/tricoder/tui.py
- Create: tests/test_worktree_integration.py
- Modify: tests/test_sessions.py
- Modify: tests/test_tui.py

**Interfaces:**

- Consumes: WorktreeService
- Produces: /worktree list|create|enter|leave|remove，Worktree 元数据持久化

- [ ] **Step 1: 写显式审批测试**

create、enter 跨工作区和 remove 均要求明确用户确认；只读模式拒绝 create/remove。

- [ ] **Step 2: 写工作区切换事务测试**

新工作区的配置、Policy、ToolRegistry、ExtensionHost 和 Agent 全部构造成功后才切换；失败保留原 Session。

- [ ] **Step 3: 写数据库最小化测试**

只保存 worktree id、受管路径标识、仓库标识、branch、HEAD 和状态；不保存 diff 或文件正文。

- [ ] **Step 4: 实现命令和 TUI 状态**

动态路径按纯文本显示。删除前显示精确目标、branch、HEAD 和 dirty 状态。

**Phase 8 exit gate:** Worktree 生命周期安全可恢复，且与 Session/Policy/Journal 一致。

---

## Phase 9：子 Agent，只读单层版本

### Task 9.1：子 Agent 权限、预算和结果模型

**Files:**

- Create: src/tricoder/agents/__init__.py
- Create: src/tricoder/agents/models.py
- Create: src/tricoder/agents/coordinator.py
- Create: tests/test_agent_permissions.py
- Create: tests/test_agent_coordinator.py

**Interfaces:**

- Produces: AgentId、AgentTask、AgentCapabilitySet、AgentBudget、AgentStatus、AgentResult、effective_child_capabilities

- [ ] **Step 1: 写权限交集表测试**

覆盖 strict、relaxed、fullaccess、read_only、工具白名单、工作区范围和父子嵌套。任何输入组合都不能让子权限超过父权限。

- [ ] **Step 2: 写默认限制测试**

max_depth=1、max_concurrency=1、default_read_only=true；子 Agent 创建子 Agent 必须拒绝。

- [ ] **Step 3: 写结果边界测试**

结果包含状态、摘要、修改文件标识、验证状态、usage 和安全错误；摘要有长度上限，不携带完整消息历史。

- [ ] **Step 4: 实现纯模型与 coordinator**

Coordinator 通过 factory 创建 CodingAgent，不直接构造 Provider、ToolRegistry 或审批器。

### Task 9.2：只读 Agent 工具

**Files:**

- Create: src/tricoder/agents/task_manager.py
- Create: src/tricoder/tools/agents.py
- Modify: src/tricoder/tools/__init__.py
- Modify: src/tricoder/agent.py
- Modify: src/tricoder/audit.py
- Create: tests/test_subagent_tools.py
- Modify: tests/test_agent.py

**Interfaces:**

- Produces: spawn_agent、get_agent、list_agents、cancel_agent 工具

- [ ] **Step 1: 写端到端 fake Provider 测试**

父 Agent 创建只读子 Agent，子 Agent 读取文件并返回摘要；尝试 write_file、run_command、MCP 写工具和 spawn_agent 均拒绝。

- [ ] **Step 2: 写取消和资源清理测试**

父取消、显式 cancel_agent、Provider 失败和超时都必须结束子任务并释放 ExtensionHost/MCP 借用。

- [ ] **Step 3: 写审计关联测试**

每条事件带 root_task_id、agent_id、parent_agent_id 和安全类别，不记录任务正文。

- [ ] **Step 4: 实现前台单任务管理器**

第一版不使用后台线程池或并发；spawn_agent 等待子 Agent 完成后返回。

**Phase 9 exit gate:** 单层只读子 Agent 全链路可用，权限无法扩大。

---

## Phase 10：子 Agent 受控写入、并发和任务树

### Task 10.1：单写者与 Worktree 隔离

**Files:**

- Modify: src/tricoder/agents/models.py
- Modify: src/tricoder/agents/coordinator.py
- Modify: src/tricoder/agents/task_manager.py
- Modify: src/tricoder/changes.py
- Create: tests/test_subagent_writes.py

**Interfaces:**

- Produces: WorkspaceLease、WriteIsolationMode、AgentCoordinator.acquire_workspace

- [ ] **Step 1: 写同工作区单写者测试**

两个子 Agent 请求同一工作区写权限时只能一个持有 lease；等待者取消后不得获得幽灵 lease。

- [ ] **Step 2: 写 Worktree 隔离测试**

并行写子 Agent 默认各自使用受管 Worktree；结果返回父 Agent 前列出精确变更和验证状态。

- [ ] **Step 3: 写审批归属测试**

子 Agent 的审批请求必须显示 agent_id、工具、目标和来源；只有人类 Approver 可以批准，父 Agent 输出文本不能作为批准。

- [ ] **Step 4: 实现 write lease**

lease 与 CancellationToken 绑定并在 finally 中释放；进程崩溃后的状态由下次 doctor 报告，不自动删除 Worktree。

### Task 10.2：有界并发和消息

**Files:**

- Modify: src/tricoder/agents/task_manager.py
- Modify: src/tricoder/agents/coordinator.py
- Create: src/tricoder/agents/messages.py
- Create: tests/test_agent_concurrency.py
- Create: tests/test_agent_messages.py

**Interfaces:**

- Produces: AgentMessage、send_message、poll_completed、max_concurrency enforcement

- [ ] **Step 1: 写并发上限测试**

使用 barriers 和 fake Agents 证明运行数不超过配置；完成、失败和取消都释放 slot。

- [ ] **Step 2: 写消息边界测试**

只允许父子或同一 root_task 下已登记 Agent 通信；长度、数量和队列有上限；未知收件人拒绝。

- [ ] **Step 3: 写停滞与预算测试**

重复相同工具调用、轮次耗尽、token 耗尽和超时必须停止对应 Agent，不自动增加预算。

- [ ] **Step 4: 实现后台调度**

采用标准库 asyncio TaskGroup 或等价的显式任务集合；所有 task 都被等待、取消或回收，不遗留后台异常。

**Phase 10 exit gate:** 并发和写入具有明确隔离、审批、预算、取消和审计语义。

---

## Phase 11：TUI、Eval、CI、文档与发布门

### Task 11.1：平台 TUI

**Files:**

- Modify: src/tricoder/tui.py
- Modify: src/tricoder/ui.py
- Modify: src/tricoder/commands.py
- Modify: src/tricoder/shell.py
- Modify: tests/test_tui.py
- Modify: tests/test_ui.py
- Modify: tests/test_commands.py

**Interfaces:**

- Consumes: AgentEvent、ExtensionHost 状态、AgentCoordinator 状态
- Produces: /mcp、/skills、/hooks、/worktree、/agents、/cancel

- [ ] **Step 1: 写 UI 状态测试**

覆盖流式增量合并、MCP 断开、Skill 激活、Hook 拒绝、Worktree 切换、子任务树、取消和 80 列布局。

- [ ] **Step 2: 写不可信文本渲染测试**

工具名、Skill 名、MCP 描述、Hook 消息、Agent 摘要和路径均按纯文本渲染，Rich markup 不得执行。

- [ ] **Step 3: 写退出清理测试**

退出时取消当前任务、等待受管子 Agent、关闭 MCP、尝试持久化安全记忆；失败时返回非零或明确警告。

- [ ] **Step 4: 实现最小平台面板**

保持现有 Header、RichLog、Input、sidebar 和审批 Modal；不在 UI 中复制 Agent 状态机。

### Task 11.2：Eval 扩充

**Files:**

- Create: evals/platform/manifest.toml
- Create: evals/platform/cases/mcp-tool/case.toml
- Create: evals/platform/cases/skill-loading/case.toml
- Create: evals/platform/cases/hook-rejection/case.toml
- Create: evals/platform/cases/worktree-task/case.toml
- Create: evals/platform/cases/subagent-readonly/case.toml
- Create: tests/test_eval_platform_suite.py
- Modify: README.md

**Interfaces:**

- Consumes: 现有 Eval runner 与隐藏 verifier
- Produces: 离线确定性平台能力套件

- [ ] **Step 1: 为每项能力建立失败 fixture**

初始 fixture 不应预先满足 verifier；不得包含真实网络、Key 或用户目录依赖。

- [ ] **Step 2: 写 hidden verifier**

分别验证 MCP 规范结果、Skill 按需加载、Hook 在副作用前拒绝、Worktree 隔离和子 Agent 只读边界。

- [ ] **Step 3: 写恶意边界 case**

覆盖 MCP 输出洪泛、Skill include 越界、Hook 递归、Worktree 注册表伪造和子 Agent 权限扩大。

- [ ] **Step 4: 运行 dry-run 和 fake Agent 集成**

不把 fake Agent 成功当作真实模型质量结论。

### Task 11.3：CI、文档和最终安全审查

**Files:**

- Modify: .github/workflows/ci.yml
- Modify: README.md
- Modify: SECURITY.md
- Modify: CONTRIBUTING.md if present
- Modify: docs/project-deep-dive.md if it is accepted into the repository
- Modify: docs/open-source-assessment.md
- Modify: project.md

**Interfaces:**

- Produces: Windows/Linux、Python 3.11/3.12 的可重复发布证据

- [ ] **Step 1: 扩展 CI**

CI 安装锁定依赖，运行 unittest、compileall、smoke dry-run 和 platform dry-run。CI 不访问真实 Provider 或公共 MCP 网络。

- [ ] **Step 2: 更新使用文档**

README 必须包含安全默认配置、MCP server 显式启用、Skill 来源、Hook 行为、Worktree 删除规则、子 Agent 权限和取消方法。

- [ ] **Step 3: 更新 SECURITY**

明确没有 OS 沙箱、MCP server 是本地代码执行、远程 MCP 未启用、子 Agent 不构成隔离边界，以及凭据环境引用规则。

- [ ] **Step 4: 运行凭据与持久化检查**

扫描跟踪候选 diff 中的私钥块、常见 token 形式、Authorization 和明文 secret 配置；检查测试 SQLite 不含任务原文、源码和工具输出。

- [ ] **Step 5: 运行完整跨平台验证**

本地执行通用阶段验证；推送后确认全部 CI matrix。不能用本地 Windows 结果替代 Linux 证据。

- [ ] **Step 6: 人工真实 Provider smoke**

仅在用户明确选择时执行。使用进程环境注入 Key；只运行最小、低成本任务；报告 Provider、模型、时间、token usage 和结果，不输出 Key。

- [ ] **Step 7: 最终 diff 与来源审查**

逐文件核对 Hcode adapt/copy 与 NOTICE；确认没有导入 hcode 包、没有 Remote、没有自由文本 Session 持久化、没有未受控 subprocess。

**Phase 11 exit gate:** 整体完成定义全部满足，文档与现实一致。

---

## 12. 每阶段提交与评审协议

每个 Phase 完成聚焦和完整验证后：

1. 运行 git status 和 git diff --check。
2. 只审查该 Phase 声明文件。
3. 记录测试命令、退出码、测试数量、跳过及平台。
4. 更新 project.md 的 Progress、Next Action、Blockers 和 Verification。
5. 用户明确批准后才 git add 和 commit。
6. commit 不包含进入 Phase 前的既存修改。
7. 用户明确批准后才 push。
8. CI 失败时先复现和诊断，不叠加下一 Phase。

推荐提交边界：

| Phase | Commit intent |
| --- | --- |
| 0 | docs: establish Hcode migration baseline |
| 1 | feat: add typed runtime events and cancellation |
| 2 | feat: stream provider and agent events |
| 3 | feat: add token-aware context management |
| 4 | feat: add extension host |
| 5 | feat: integrate MCP through tool gateway |
| 6 | feat: add bounded skills and instructions |
| 7 | feat: add policy-bound hooks |
| 8 | feat: add managed git worktrees |
| 9 | feat: add read-only child agents |
| 10 | feat: isolate concurrent child agents |
| 11 | docs: complete platform migration gates |

提交信息仅为建议，不能替代用户批准。

## 13. 强制停止条件

遇到以下情况时停止当前 Phase并报告：

- 需要读取真实密钥或用户级敏感目录。
- Hcode 待迁移文件来源或许可证不明确。
- 需要安装、升级或移除依赖但未获批准。
- 必须删除目录、Worktree、数据库或用户文件。
- 当前工作树存在与本 Phase 同文件的未知修改。
- 新实现要求绕过 ToolRegistry、WorkspacePolicy、审批或审计。
- 会话数据库测试发现自由文本、源码、工具输出或凭据。
- 子 Agent 能获得高于父 Agent 的权限。
- pre_tool_use Hook 可能在副作用之后运行。
- Worktree 删除目标无法通过注册表、Git 与文件身份三方核验。
- 完整回归失败且原因未定位。

## 14. 新会话启动提示词

将下面内容发送给新的 Codex 会话；一次只替换“本次只执行”后的 Phase 编号：

~~~text
请在 D:\MaHong\AGENT_WORKSPACE_V2 中继续 TriCoder 平台化迁移。

先完整读取：
1. 工作区 AGENTS.md；
2. projects/tricoder-cli/AGENTS.md；
3. projects/tricoder-cli/project.md；
4. projects/tricoder-cli/docs/superpowers/specs/2026-09-03-hcode-capability-migration-design.md；
5. projects/tricoder-cli/docs/superpowers/plans/2026-09-03-hcode-capability-migration.md。

来源仓库 projects/hcode 只读。本次只执行主计划中的下一个未完成 Phase；先检查两个仓库 git status，识别并保护既存修改，再为该 Phase 写细化实施计划。按测试先行、小步实现、聚焦测试、完整回归、diff 自审查和 project.md 交接执行。

不得读取 .env.local、.local/secrets、.local/envs；不得保存或输出真实凭据。依赖安装、文件删除、Git worktree、commit 和 push 必须先获得我的明确批准。所有 MCP、Hooks、Worktree 和子 Agent 有副作用操作必须经过 TriCoder ToolRegistry、权限策略、审批、审计和资源限制。

完成一个 Phase 后停止，给出修改文件、测试证据、限制、风险和下一 Phase，不要自动继续。
~~~

## 15. 总迁移检查表

- [x] Phase 0：基线、来源、许可和迁移清单
- [x] Phase 1：类型化事件、取消和预算基础
- [x] Phase 2：Provider 流式输出与异步 Agent
- [x] Phase 3：token 上下文预算和大工具结果落盘
- [x] Phase 4：Extension Host 与配置信任模型
- [x] Phase 5：MCP
- [ ] Phase 6：Skills 与项目指令
- [ ] Phase 7：Hooks
- [ ] Phase 8：Worktree
- [ ] Phase 9：子 Agent，只读单层版本
- [ ] Phase 10：子 Agent 受控写入、并发和任务树
- [ ] Phase 11：TUI、Eval、CI、文档与发布门
