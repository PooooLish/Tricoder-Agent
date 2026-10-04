# TriCoder Agent 第一轮职责拆分 Implementation Plan

> 执行会话：使用 superpowers:executing-plans 按 R0—R6 实施。用户已明确选择交给另一个 coding session 执行。本轮保持功能行为不变，不自动提交、不安装依赖。无需重新选择执行方式。

**Goal：** 将 agent.py 拆成职责明确、状态可追踪的组件，让主循环可读，同时保留现有工具、记忆、取消和安全行为。
**Architecture：** agent.py 保留 CodingAgent 门面；engine/ 承接执行编排、任务状态、工具批次及结果收尾；context/ 增加历史与记忆编排组件。底层规则继续复用，不创建第二套验证/副作用状态机。
**Tech Stack：** Python 3.11+、现有 dataclass/Protocol、asyncio、unittest；无新增依赖。
**Spec：** 本文第 1—4 节保存用户已认可的第一轮设计，第 5—7 节规定执行任务与验证。
**状态：** 待实施；2026-10-05 本轮仅编写执行文档，没有修改功能代码、运行回归或真实 Provider。

项目根：D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli。以下路径均相对此根。

## 1. 当前观察与任务范围

分析时 agent.py 为 1857 行，_run_with_context_owned 为 994 行（533—1526），内含 8 个闭包；行号和计数只作定位，实施前核对最新代码。
src/tricoder 共 73 个 Python 文件，根目录 29 个；session_runtime.py 为 2816 行。
上层 cli.py、session_runtime.py、tui.py、evals/service.py 依赖 tricoder.agent 的入口；测试还导入若干常量、历史辅助函数并 patch 模块属性。
当前工作树包含未提交的 Eval、Session 和工作区门禁修改。不能用 HEAD 内容覆盖这些改动；行为基线必须取当前工作树。

本轮必须完成：
- 拆分 Agent 的历史处理、流式响应收集、观测/审计、任务状态、记忆编排、工具批次和结果收尾。
- 保留 CodingAgent 的外部入口及实际使用的兼容导入。
- 保留单次 CLI、SessionRuntime、TUI、Eval 的运行方式。
- 更新结构说明、补充必要的行为回归和导入边界测试。

本轮不做：
- 不整体移动 src 文件，不重构 session_runtime.py，不移动 task_cleanup/task_observation/execution_state 的定义。
- 不拆 providers.py，不接 Claude/Gemini，不添加断流重试或非流式降级。
- 不改记忆算法、默认配置、审批规则、工作区锁/扫描门禁、SQLite schema、CLI 参数或审计格式。
- 不并行执行工具，不修改模型调用顺序，不改变失败重试次数。
- 不迁移 LangGraph，不增加 mixin 继承树、依赖注入容器或通用工作流框架。

目标是职责可读，不是减少全项目文件数。agent.py 约 150—300 行、核心 loop 约 200—350 行只作参考，不能为达标挤压代码或删除检查。

## 2. 模块结构与旧代码迁移表

```text
src/tricoder/
  agent.py                       CodingAgent 门面、装配和兼容导出
  engine/
    __init__.py                  轻量包标识，避免全量 eager import
    state.py                     单次任务状态与阶段结果类型
    loop.py                      AgentRunner：主循环、规划、阶段调度
    provider_request.py          Provider 请求消费与流式结果组装
    tool_batch.py                工具批次执行、skipped 配对和停止原因
    finalization.py              会话结果收尾、历史保留/回退
    telemetry.py                 Observer、事件、审计字段及失败处理
  context/
    history.py                   完整工具回合、序号与历史窗口辅助函数
    coordinator.py               压缩/保存候选的编排
    manager.py                   原有预算/压缩计划，保持职责
    memory.py                    原有记忆模型/校验/合并
    summarizer.py                原有摘要请求
```

| 原 agent.py 代码 | 目标职责 |
| --- | --- |
| _is_complete_tool_round、_complete_round_tail、compact_messages、compact_session_messages | context/history.py |
| AgentObserver、ProviderUsageObserver、NullObserver、_notify_provider_usage | engine/telemetry.py |
| _request_provider_async、_response_events | engine/provider_request.py |
| _log、_audit_usage、_audit_arguments、固定审计失败处理 | engine/telemetry.py，保留失败传播顺序 |
| _build_review_memory_candidate、prepare_structured_memory、prepare_review_memory | context/coordinator.py |
| _run_with_context_owned 的局部可变状态 | engine/state.py |
| 工具 action 循环、_skipped_result、_fill_remaining_results | engine/tool_batch.py |
| turn_result、历史回退、收尾结果组装 | engine/finalization.py |
| 初始化请求、规划、round 循环、finish 调度 | engine/loop.py |
| __init__、run、run_with_context、run_with_context_async、refresh_review_memory 及异步版本、资源所有权委托 | agent.py 保留公开入口；复杂实现可委托 |

规划相关 PLANNING_PROMPT、_parse_plan 和 _planning_round_async 可先与 loop 同处，避免只为几十行新增 planning 模块。
固定消息/异常常量必须只有一个定义位置；agent.py 用别名保留旧导入，不复制类型或异常。
协议序列化继续交给 protocols.py，审批和真实工具执行继续交给 ToolRegistry。

## 3. 依赖和状态所有权

依赖方向：
```text
CLI / SessionRuntime / TUI / Eval
              ↓
        agent.CodingAgent
              ↓
          engine.loop
       ↙       ↓       ↘
 context    tool_batch   provider_request
              ↓
     现有 ToolRegistry / Provider / 状态转换
```

- engine 与 context 不反向导入 tricoder.agent、session_runtime 或 UI。
- context/coordinator 不依赖 engine/state；接收 SessionContext、明确配置/依赖并返回结果，由 loop 合并。
- 不向每个组件传完整 CodingAgent/self，不用通用 kwargs 字典模拟全部状态。
- 不复制 current_cleanup/current_task_observation 的 ContextVar、VerificationEvidence、异常类或权威对象。身份判断、线程局部资源和取消令牌必须维持同一来源。
- 新包 __init__.py 不主动导入 Agent/工具/MCP，防止冷启动循环依赖和可选依赖变必需。

### 3.1 每任务状态与长期对象

AgentRunState 为一次 run 调用新建，仅在当前任务存活；不要挂成下一任务可复用的 self.current_state。

| 状态域 | 内容 | 唯一修改责任 |
| --- | --- | --- |
| history | 原始 context、messages、前缀边界、序号、current_task_id | 历史辅助函数和 loop 的消息追加 |
| execution | modified_files、verification/evidence/failure、unknown_effects、清理/效果是否已消费 | 通过现有 apply_tool_transition 及受信验证/收尾规则 |
| memory | conversation_memory、review_candidate、完成水位、摘要统计/告警、compacted 标志 | loop 提交 coordinator 的结果 |
| progress | round、工具次数、业务 usage | loop / tool batch 的明确计数点 |

可用有类型的嵌套 dataclass，但不把同一字段存两份；SessionContext 是跨阶段传递快照，不能让部分模块改快照、另一部分改自建镜像。
所有可变列表用 default_factory；ContextManager、Provider、ToolRegistry、Observer、审计器、cleanup owner 保持现有生命周期。
尤其不能在每个 round 重建 ContextManager 清掉 usage 锚点，也不能把多个任务的 summary_failure 标志共享。

### 3.2 拟议组件契约

允许根据当前类型微调名称，但需在实施记录同步；禁止含糊的任意 dict 返回值。

```python
async def collect_provider_response(
    provider: ModelProvider,
    messages: list[Message],
    tools: tuple[ToolDefinition, ...] | list[ToolDefinition],
    cancellation: CancellationToken,
    event_sink: EventSink | None,
) -> ProviderResponse: ...

class AgentRunner:
    async def run(self, task: str, context: SessionContext, *,
                  cancellation: CancellationToken,
                  event_sink: EventSink | None) -> SessionTurnResult: ...

class MemoryCoordinator:
    async def compact(self, context: SessionContext, *,
                      fixed_messages: tuple[Message, ...],
                      tools: tuple[ToolDefinition, ...],
                      cancellation: CancellationToken) -> MemoryStepResult: ...
    async def build_review_candidate(self, context: SessionContext, *,
                                     cancellation: CancellationToken) -> MemoryRefreshResult: ...
```

MemoryStepResult 在 coordinator 内定义：返回 context、memory_usage、memory_calls、summary_failed、compacted、warning。统计明确为“本次调用增量”；loop 只加一次。审计失败与取消用现有语义异常传播，不转换成普通 warning。原有两批上限、失败保留、覆盖水位规则不变。

ToolBatchOutcome 在 engine/state 定义：停止原因使用有限枚举（continue/replan/finish/cancelled/fatal），以及必要的 finish 结果；副作用与消息由约定的 state 单一提交。finish 仅表示收到 finish 工具结果，最终成功仍由验证和收尾判定。
finalization 接收 state、RunResult、rollback_task 标志及显式依赖；不做数据库保存，不自行释放工作区锁。

## 4. 不允许改变的行为

### 4.1 工具与异常顺序

必须保留：
1. 执行工具；取消时先从 TaskObservation 恢复已经发生的事实。
2. 归一化副作用，核验本地 verification authority。
3. apply_tool_transition 更新状态，publish_state 发布可信事实。
4. 构造并写入当前工具结果消息。
5. Observer、完成事件与审计通知。
6. 首个失败/取消/finish/审计失败时，剩余调用只补 skipped，不再执行。

协议消息构造失败时不能伪造完整回合或重试构造；通知失败不能撤销已经发生的文件事实。
_fill_remaining_results 先构造全部剩余协议消息，再进行可抛异常的通知；skipped 不增加真实工具执行计数。
异常身份和优先级保留，不用 finally 中的新异常覆盖原异常。原生 asyncio.CancelledError 继续传播，子令牌不能取消调用方父令牌。

### 4.2 Provider 与记忆

- 流式文本可以及时转发，但整个响应完成前不执行工具。
- Provider 不具备 stream 时保留 complete 兼容；流失败不新增自动降级。
- 保留 planning 的无工具调用、失败降级、审计失败中止、round 0 用量和事件顺序。
- usage 缺失仍是 None；业务、规划、记忆用量不能重复或漏记。
- 原始历史与临时请求视图分开；摘要不反复追加到原始历史。
- 消息序号、task_id、完整工具回合、完成水位、候选覆盖范围保持原规则。
- 运行时摘要与待保存候选分开；摘要失败不删除原历史；审计成功前不发布候选。
- 失败回退只回退适用的消息历史，不是文件撤销；有完整工具回合时保留已发生的中间事实。
- workspace_change_notice 仍进入正确请求视图，不能因拆分漏掉；不改它的清除/保留时机。

### 4.3 取消、清理与验证

- TaskCleanup 的上下文覆盖原有操作范围；pending 资源继续阻止任务复用。
- 工具落盘后取消、Observer 抛错、审计失败时，修改路径与 unknown_effects 不丢失。
- 原验证证据必须由原 authority 校验，修改后旧证据不能恢复通过。
- finish 成功条件、失败快照黏性、取消后撤销验证和失败时保留事实不变。
- Session/工作区锁生命周期由现有宿主继续负责；本轮不得重新获取第二把锁或提前 close。
- 无全局可变 Agent 状态；两个实例交错运行不共享消息、候选或计数。

## 5. 执行任务

### R0 基线与行为契约

文件：新增 tests/test_agent_refactor_contract.py，复用现有相关测试，记录 runtime/agent-refactor-round1/progress.md。
- [ ] 阅读规则及最新 docs，检查状态；记录当前行数/模块依赖与已有修改，禁止输出私密 diff。
- [ ] 在修改实现前跑 Agent、异步、批次、记忆、验证专项，必要时全量基线；既有失败单独记录，不能通过改断言掩盖。
- [ ] 以假 Provider、合成临时文件、假工具构建表征测试，固定返回结果、消息 role/kind/配对、执行次数、文件结果与事件顺序。
- [ ] 覆盖成功、Provider 错误、协议反馈、批次失败、取消后副作用、审计失败、finish 验证失败、摘要失败。
- [ ] 表征测试应在旧实现通过；不为“先红后绿”故意改旧行为。新接口边界测试在接口未实现时失败是正常的。

确定性比较去除真实耗时、随机 ID 的字面差异，但保留 ID 引用关系、事件次序及次数；不要以大段录制文本作脆弱快照。

### R1 提取历史和观测辅助职责

文件：context/history.py、engine/telemetry.py、轻量 engine/__init__.py、agent.py、test_agent.py、test_protocols.py。
- [ ] 移出完整回合判断与压缩兼容函数，保持函数签名和结果顺序；避免 context/__init__.py 引入重导出环。
- [ ] 移出 Observer 类型及用量通知；agent.py 保留同一类型对象的导出。
- [ ] 封装日志安全字段与 fail-closed 行为，保持失败事件无源码/原始参数泄漏。
- [ ] 运行历史/协议/审计拒绝/观察者异常测试；此阶段仍由旧主循环调用新辅助组件。
- [ ] 测试旧导入能使用；不用占位函数或重复类假装兼容。

### R2 提取 Provider 消费与规划委托

文件：engine/provider_request.py、engine/loop.py 中规划相关组件、agent.py、test_agent_async.py、新 test_agent_provider_request.py。
- [ ] 移出 _request_provider_async 与 _response_events，保持模型调用次数、完整结束判断、用量 merge 和事件转发。
- [ ] 验证只实现 complete 的 fake、完整 stream、中途断流、无完成事件、取消、event sink 抛错。
- [ ] 明确测试 ToolCallCompleted 后再断流不会执行工具；当前完整响应后执行的时机不变。
- [ ] 规划逻辑可以先由旧循环委托新函数，不提前改变主循环；测试 round 0 用量和失败分支。
- [ ] 组件只依赖 ModelProvider 及显式事件/取消接口，不读取 CodingAgent 或 SessionRuntime。

### R3 显式任务状态与记忆协调

文件：engine/state.py、context/coordinator.py、agent.py、test_memory*.py、test_conversation_memory.py。
- [ ] 先将闭包 nonlocal 状态转换为单次 AgentRunState，逐段替换并保持调用顺序；先不同时搬整个循环。
- [ ] 验证同一个 Agent 连续两次 run 及两个实例交错运行不共享任务计数/候选；ContextManager 生命周期保持。
- [ ] 移出压缩和 review candidate 编排，返回 MemoryStepResult/MemoryRefreshResult，沿用现有 manager/summarizer 校验。
- [ ] 测试两批上限、失败不删历史、记忆 off 不请求摘要、最新完成水位、候选失败保留和 memory usage 增量只加一次。
- [ ] 保留 refresh_review_memory 同步/异步入口，手动刷新与正常收尾共用候选构造，不能产生两套规则。
- [ ] 审计失败前后的状态发布边界与异常类型保持，用针对性失败注入验证。

### R4 提取工具批次

文件：engine/tool_batch.py、engine/state.py、agent.py、test_batch_failure.py、test_tool_errors.py、test_effect_state.py、test_verification_evidence.py。
- [ ] 将 action 循环、skipped 结果补齐集中到批次组件，首个失败后的剩余动作不执行。
- [ ] 用显式 ToolBatchOutcome 通知 loop 下一步；工具层仍负责审批、实际 I/O 和权限，不复制审批策略。
- [ ] 测试三个工具“首个成功、第二个失败、第三个 skipped”，真实执行次数为 2，三个 call 均有匹配结果。
- [ ] 注入工具已写入后取消、Observer 抛错、协议结果构造失败、审计失败，核对第 4 节顺序和事实保留。
- [ ] 验证 native 与 legacy_json 两种协议，未知工具、MCP 来源与验证 authority 不能因抽取被绕过。

### R5 提取收尾并缩减主循环

文件：engine/finalization.py、engine/loop.py、agent.py、test_agent_refactor_contract.py、test_reliability_integration.py。
- [ ] 移出 turn_result、history rollback 和状态发布辅助逻辑，保留关键状态所有者和 pending cleanup 交接。
- [ ] AgentRunner 主循环只编排准备、压缩、规划、请求、协议解析、批次执行、finish/错误收尾；不要把 994 行原样搬入 loop。
- [ ] CodingAgent 保留构造参数、同步/异步返回类型、原生取消行为、context_manager 与必要注入属性的兼容。
- [ ] 核对 R0 所有确定性场景的返回、历史、事件及工具次数一致；检查审计失败不改变首异常身份。
- [ ] 实测工作区变化提示仍进入 Provider 请求；finish 后无后续工具执行，未验证修改不能显示通过。

### R6 兼容清理、结构说明与最终验证

文件：agent.py、README.md、project.md、tests/test_mcp_dependency_boundary.py、新 tests/test_agent_import_boundaries.py。
- [ ] 清理不再使用的旧实现，保留小型有用途的兼容别名；不留两份算法。
- [ ] 逐项检查 tricoder.agent 导入清单：CodingAgent、AgentObserver、NullObserver、PLANNING_PROMPT、既有协议/上下文常量、compact_messages、compact_session_messages、parse_action 及现有测试使用的回合辅助函数。
- [ ] 明确 monkeypatch 查找位置：纯重导出不能保证 patch 继续拦截内部引用。内部测试可改到真实定义点或注入依赖，但必须保持原行为断言，不保留失效 patch 冒充覆盖。公开函数调用兼容与内部 patch 路径兼容分开记录。
- [ ] 用独立子进程导入 tricoder.agent、tricoder.tools、tricoder.mcp.tool_adapter、tricoder.session_runtime 并测试不同导入顺序；MCP 依赖可选边界仍成立。
- [ ] 加最小静态依赖检查：engine/context 不反向导入 agent/session_runtime/UI。不要用任意“每文件必须小于 N 行”的测试。
- [ ] README 增加读代码顺序：agent 门面→loop→state→tool_batch→finalization，记忆/Provider 分别查对应组件。
- [ ] 更新 project.md、实施记录和迁移表，运行最终门禁；说明未验证平台及真实模型未测。

## 6. 验证入口与交付

以下命令可作专项入口，按变更阶段选取；新模块测试遵循 test_agent*.py 命名便于发现。
```powershell
& ./.venv/Scripts/python.exe -B -m unittest discover -s tests -p 'test_agent*.py' -v
& ./.venv/Scripts/python.exe -B -m unittest discover -s tests -p 'test_batch_failure.py' -v
& ./.venv/Scripts/python.exe -B -m unittest discover -s tests -p 'test_memory*.py' -v
& ./.venv/Scripts/python.exe -B -m unittest discover -s tests -p 'test_verification_evidence.py' -v
& ./.venv/Scripts/python.exe -B -m unittest discover -s tests -p 'test_workspace*.py' -v
& ./.venv/Scripts/python.exe -B -m unittest discover -s tests -p 'test_eval*.py' -v
& ./.venv/Scripts/python.exe -B -m unittest discover -s tests -v
git diff --check
```

完整回归前跑语法检查；可用 compile(source, filename, 'exec') 避免写入源码目录缓存。
全量日志保存在 runtime/agent-refactor-round1/，不包含真实源码/凭据/会话消息；所有测试数据合成。
不运行真实 Provider，不读取 .env.local、真实会话数据库或凭据目录，不安装依赖、不自动提交推送。
最终交付：旧职责→新位置表、入口兼容说明、状态生命周期说明、各阶段测试证据、最终全量结果、未完成项及风险。

## 7. 自审重点与停止条件

重点风险已有对应任务：
- R3：状态复制两份或跨任务共享，导致记忆/验证相互覆盖。
- R4：先通知再发布事实，落盘后异常导致丢失副作用；skipped 配对被异常截断。
- R5：取消/审计失败回退整段历史，误删已完成工具回合。
- R2/R6：流式调用次数变化、patch 失效、导入环或可选 MCP 强制加载。
- R3/R5：usage 锚点重置、记忆水位/告警失真、工作区变更提示遗漏。

发现行为差异先判定是否由本轮引入。既有缺陷单独记录，不顺手修复并宣称“纯重构”；确需改变可观察行为时应单独提出具体差异。
不通过删测试、改成功条件、吞异常、关闭锁/审批或增加重试来让回归通过。
无法完成全部拆分时报告停在何阶段、哪些模块仍承担过多职责；不能仅移动长函数就宣布解耦完成。
