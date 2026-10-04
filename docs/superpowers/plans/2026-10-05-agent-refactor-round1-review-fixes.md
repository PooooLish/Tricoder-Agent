# TriCoder 第一轮重构审查补齐 Implementation Plan

> 执行会话：使用 superpowers:executing-plans 按 C0—C4 实施。用户已选择交给 coding session；本次只交付执行文档。实施时按阶段修改、验证和自审，不自动提交、不安装依赖，不同时启动第二轮目录迁移。

**Goal：** 补齐记忆模块与执行引擎之间的职责边界，并将两个独立 Agent 异步交错运行的隔离要求落实为长期回归测试。
**Architecture：** MemoryCoordinator 接收会话快照和必要配置，返回明确的记忆处理结果；AgentRunner 负责将允许更新的字段合入 AgentRunState。异常时保留已经发生的局部进度，不改变原异常传播和审计提交顺序。
**Tech Stack：** Python 3.11+、现有 dataclass、asyncio、unittest、fake Provider/摘要器；无新增依赖。
**Spec：** 本文第 1—4 节，以及 [第一轮计划](2026-10-05-agent-refactor-round1.md)第 3 节、R3、R6。本文是第一轮审查补充，不是第二轮重构。
**状态：** C0—C4 已实施，2026-10-05 再次复审通过：本次新跑全量 1337 tests，OK，11 skipped；F1/F2 已关闭。原计划创建时仅写文档，后续实施与本次复审记录见 project.md；下一步按第二轮计划整理目录，本文件保留原执行要求供追溯。

项目根：D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli。下文路径均相对此根。

## 1. 审查发现与本轮范围

| 编号 | 已确认事实 | 要达到的结果 |
| --- | --- | --- |
| F1 / P2 | context/coordinator.py 导入 AgentRunState，prepare_compaction、prepare_review_candidate 直接修改任务消息、候选和统计；偏离第一轮的结果返回契约 | 记忆协调器不依赖 engine；执行引擎拥有任务状态的提交权 |
| F2 / P3 | 新契约测试覆盖同一 Agent 顺序执行，但没有固定两个独立 Agent 在 await 边界交错时的隔离 | 自动化验证消息、候选、业务/记忆用量和取消互不污染 |

审查未发现可复现的新增功能错误。不要把 F1 描述为已经造成记忆丢失，也不要把 F2 描述为已经证明存在串会话。

上一轮审查实际跑过三组测试：155 项（1 跳过）、93 项、84 项，均通过，组间有重复；还做过纯内存流异常及两实例交错探针。此记录不是本轮修改后的验收证据。

**允许修改：** coordinator.py、loop.py、agent.py 中协调器的装配点；必要的窄类型/合并辅助；相关测试；README.md、project.md 及实施记录。
**不做：** 第二轮搬目录、SessionRuntime 拆分、同一个 Agent 实例并发支持、锁设计调整、摘要算法/计费语义优化、持久化 schema 变化、Provider 重试或新厂商适配。

## 2. 全局约束

- 保留 CodingAgent 构造参数、同步/异步任务入口、refresh_review_memory 两种入口、公开返回类型及异常兼容别名。
- 不改调用次数、两批上限、候选覆盖水位、任务完成水位、原始历史与临时请求视图的分离方式。
- 运行时压缩与保存候选不是同一套提交语义，不能合并成一个“大事务”或统一处理失败。
- 业务用量与记忆用量分开；缺失 usage 保持 None；本轮保持现有失败分支的统计时点，不顺手“补齐实际账单”。
- 审计确认前不能发布新候选或删除被压缩历史；原生 asyncio.CancelledError、KeyboardInterrupt、SystemExit 及既有首异常不得被替换、吞掉或降级成普通 warning。
- 不重新定义 ContextVar、VerificationScope、权威对象、取消令牌或执行状态机；不动工作区锁、门禁和资源清理生命周期。
- 保留现有未提交与未跟踪修改。不得读取 .env.local、凭据目录或真实会话库；不得调用真实模型、安装依赖、自动提交/推送或执行破坏性 Git 回退。

## 3. 接口与状态所有权设计

### 3.1 正常路径：传入快照，返回结果

在 src/tricoder/context/coordinator.py 定义下列窄类型，使用 frozen dataclass（进度载体除外），不新增通用框架：

| 类型 | 字段与含义 |
| --- | --- |
| MemoryStepInput | context: SessionContext；fixed_messages: tuple[Message, ...]；provider_tools: tuple[ToolDefinition, ...]；summary_failed: bool；warning: str |
| MemoryStepResult | context: SessionContext；memory_usage: TokenUsage 或 None；memory_calls: int；summary_failed: bool；compacted: bool；warning: str |
| MemoryStepProgress | result: MemoryStepResult；仅为一次调用保存最新不可变结果快照，无其他可变状态 |

MemoryStepResult.memory_calls、memory_usage 只表示**这次协调器调用的增量**，不是整个任务累计值。summary_failed 和 warning 表示本次处理后的值，初始继承输入；compacted 表示本次是否已有压缩成功提交，初始 False。context 初始为输入 context。

接口固定为：

```python
async def prepare_compaction(
    self, request: MemoryStepInput, cancellation: CancellationToken, *,
    progress: MemoryStepProgress,
) -> MemoryStepResult: ...

async def prepare_review_candidate(
    self, request: MemoryStepInput, cancellation: CancellationToken, *,
    progress: MemoryStepProgress,
) -> MemoryStepResult: ...
```

build_review_candidate(context, cancellation) -> MemoryRefreshResult 保持现有职责和接口，仍供手动刷新与正常候选生成复用；不额外调用摘要器。

协调器不接收 AgentRunState、完整 Agent、Runner，也不接收把这些对象藏起来的代理、宽泛 Protocol、任意 kwargs 或 state 修改回调。
取消对 engine.telemetry.AgentObserver 的依赖：构造时只注入 on_error: Callable[[str], None]，由 Agent/Runner 传 observer.on_error；log 保持明确函数依赖。context/coordinator.py 不导入任何 engine 模块，包括 TYPE_CHECKING 和字符串导入。

**输入装配：** loop 从任务状态构造 SessionContext 快照，包含原始历史、序号、记忆与完成水位；fixed_messages 是现有 messages[:history_start]，不能漏掉系统提示、持久化摘要和 workspace_change_notice。工具定义是现有集合。原有历史规范化在哪个分支发生，就在局部快照的相同位置发生；不能因新接口在 memory=off 或跳过候选时增加无意义的序号更新。

**唯一合并点：** 在 AgentRunner 中实现窄辅助 _apply_memory_step(state, result)。只允许更新：

- 原始历史 messages[history_start:]、next_message_seq；不得覆盖固定前缀、history_start、current_task_id。
- conversation_memory、review_memory_candidate。
- memory_calls、memory_usage 按增量合并一次；memory_summary_failed、memory_warning 按结果更新；memory_compacted 与结果 compacted 取 OR。

不得从结果整体替换任务状态。modified_files、verification/evidence、unknown_effects、cleanup、业务 usage、tool_calls、latest_completed_task_seq 等继续由现有引擎规则维护；其中完成水位是协调器的只读输入。
SessionContext 虽为快照，也不要原地修改其内部消息参数字典等嵌套对象。

### 3.2 异常路径：不能等到正常 return 才保留进度

仅把最后一次 state 写入改成 return 会引入回归：摘要已经请求，随后失败，可能丢计数；第一批压缩提交、第二批失败，可能错误撤销第一批。

采用上述**每次调用独享的窄进度载体**解决：协调器在原本更新对应字段的时点，将新的 MemoryStepResult 放入 progress.result；正常结束也返回这个结果。载体不引用 AgentRunState，不放在 coordinator/self 或全局上，不跨调用复用，不允许携带执行引擎字段。

Runner 使用一个统一的 finally 合并进度，正常路径不要再合并 return 一次。保留外层已有异常分类处理：先合并，再进入 finalizer 或让原异常继续传播。不能在 except 分支先 return finalizer、再由 finally 更新状态，否则返回值已经用到了旧状态。

顺序示意：

```text
Runner 装配输入和本次 progress
    try:
        try: await coordinator.prepare_*(..., progress=progress)
        finally: 合并 progress.result（恰好一次）
    except 既有可处理异常: 维持原分类收尾
    原生取消/其他未处理异常：继续传播同一个异常
```

合并必须是本地、同步、无 I/O、无 Observer/审计/模型调用的操作；不能产生新的异步取消点或用新的合并错误覆盖首异常。输入和结果类型在进入调用前/构造时保证有效，禁止在 finally 调用可能失败的重新摘要或业务校验。

### 3.3 必须固定的提交时点

| 场景 | 当前语义与补齐后要求 |
| --- | --- |
| compaction 发起 summarize | 调用前增加 memory_calls；摘要失败或取消也保留已经增加的次数 |
| compaction 收到摘要 usage | 在后续 commit/audit 前累计 usage；后续审计失败不能把这部分用量丢掉 |
| compaction 校验或审计失败 | 未通过该批提交的摘要和历史不发布；不得删该批原历史 |
| 第一批 compaction 已提交，第二批失败/取消 | 保留第一批提交、已有序号和统计；原历史回退仍遵循 finalizer 的 memory_compacted 规则 |
| prepare_review_candidate 两批构建 | build_review_candidate 内保留局部候选；全批成功且候选审计成功后才发布 |
| review 构建失败或候选审计失败 | 保留原候选；沿用当前失败分支的 warning 与调用/usage 合并时点，不把中间局部候选和统计擅自发布 |
| on_error/log 回调本身抛异常 | 回调前已经生效的次数、失败标志、warning 等仍按旧行为保留；未提交候选不发布；原异常继续传播 |

这张表应先在当前实现上以表征测试确认。如发现表与最新工作树冲突，先记录实际分支并明确差异，不以文档为借口改变原有业务语义。

## 4. 重点审查与测试责任

- 两批中途失败不能造成“已审计压缩被回滚”或“未审计候选被发布”——C1/C2。
- finally 与正常 return 重复合并，导致摘要次数和用量翻倍——C2。
- 返回完整 SessionContext 时意外覆盖验证、文件副作用或完成水位——C2。
- A 暂停在摘要 await，B 先完成，A 的候选/统计/取消影响 B——C3。
- 只检查 import 语法，漏掉 TYPE_CHECKING、相对导入或字符串形式的反向依赖——C2/C4。

## 5. 执行任务

### C0：保存可追溯基线

**文件：** 只读当前源码、测试及第一轮计划；创建 runtime/agent-refactor-round1-review-fixes/progress.md 和 baseline 清单。

- [ ] 阅读规则、README.md、pyproject.toml、project.md；确认当前仍是第一轮拆分后的结构。若第二轮已经开始，不覆盖其改动，先记录路径映射和写入冲突。
- [ ] 记录 Git HEAD、状态以及本任务源码/测试文件的哈希；将相关文件的当前版本保存到本任务 runtime 子目录作为有限源码基线，包含未跟踪的 engine/coordinator 文件，不复制整个项目、配置密钥或用户数据。
- [ ] 运行 Agent、memory、conversation_memory、导入专项作为基线，保存命令、退出码、数量与跳过原因。当前工作树行为优先于 HEAD，不能恢复成旧提交来“建立基线”。
- [ ] 记录 F1/F2 为待完成，下一步 C1；已存在失败单独归因，与本轮关键行为相关的未解释失败不能直接忽略。

### C1：先固定现有异常进度行为

**创建：** tests/test_agent_memory_boundary.py。
**复用：** tests/test_memory_compaction.py、tests/test_memory_refresh.py、tests/test_memory_save_coverage.py、tests/test_agent_refactor_contract.py 的 fake 与数据构造方式。

- [ ] 用合成历史与可控摘要器覆盖第 3.3 节各提交时点；为当前调用结果/状态、消息序号、候选、调用数和 usage 建立表征断言。旧实现本来就应通过，不为先红后绿故意破坏业务行为。
- [ ] 两批压缩用不同 usage 值区分批次；分别注入第二批摘要失败、审计失败和取消，比较已提交上下文和计数，不能只检查 ok=False。
- [ ] 复用候选两批失败测试，明确候选保持原值，并固定成功/审计失败时的统计差异。
- [ ] 注入同一个异常对象到 on_error/log，确认既有传播路径保留异常身份；原生取消单独断言仍为取消，不能只接受任意 Exception。
- [ ] 执行新专项及受影响记忆测试，记录当前行为基线。新接口实现后更新装配方式，不弱化这些行为断言。

### C2：实现窄输入/结果与单点合并

**修改：** src/tricoder/context/coordinator.py、src/tricoder/engine/loop.py、src/tricoder/agent.py；tests/test_agent_memory_boundary.py、tests/test_agent_import_boundaries.py。
**接口：** 使用第 3 节类型、prepare_* 签名及 _apply_memory_step；MemoryAuditFailure 保持单一定义，agent._MemoryAuditFailure 仍指向同一类型。

- [ ] 先增加“协调器不导入 engine”“输入快照不变”“独立构造并调用协调器”的新边界测试，确认迁移前失败原因确为目标边界缺失。
- [ ] 添加三个窄类型；将协调器内部状态改为局部 SessionContext/MemoryStepResult，每次调用创建自己的 progress。不得只是给原 state 改类型名。
- [ ] 按旧提交时点更新 progress.result，保留 build_review_candidate 的原算法与异常边界；将 observer 依赖改为 on_error 回调。
- [ ] 更新 Agent/Runner 装配点及两处 prepare_* 调用。用嵌套 try/finally 确保正常与异常路径都恰好合并一次，随后才收尾或传播。
- [ ] 增加 sentinel 测试：结果合并前后，文件副作用、验证证据身份、unknown_effects、工具数、业务 usage、完成水位不变；记忆字段按结果更新，fixed_messages 不被覆盖。
- [ ] 直接测试一次正常合并、一次异常合并和多轮调用：memory_calls/usage 不丢失、不重复，缺失 usage 仍为 None；第一批成功第二批失败的结果与 C1 一致。
- [ ] 加静态边界检查，覆盖绝对导入、可解析的相对导入、from tricoder import engine、TYPE_CHECKING 以及已使用的 importlib/__import__ 字符串模式；不建设通用静态分析器，不允许用动态导入规避检查。
- [ ] 跑 C1 专项、memory_*、conversation_memory、Agent/批次、导入边界测试。冷导入 coordinator/agent/session_runtime 的不同顺序不得新增循环依赖。

### C3：固化两实例交错隔离与流异常探针

**创建：** tests/test_agent_instance_isolation.py、tests/test_agent_provider_request.py。
**复用：** tests/test_agent_async.py 的 fake 接口；使用 unittest.IsolatedAsyncioTestCase。

- [ ] 使用两份 Provider、Registry、Observer、摘要器和 ContextManager，分别构造两个 CodingAgent。以 asyncio.Event 控制进入/释放，用 wait_for 设外层超时；禁止依赖固定 sleep、真实网络或执行时间先后碰运气。
- [ ] 用不同任务文本、call ID、记忆目标和明确不相等的 usage 构造场景：A 在摘要处理中暂停，B 先完成，再释放 A。分别断言请求历史不混入另一方内容，候选只覆盖本会话来源，工具数、业务与记忆 usage 各自准确；原始输入快照不变。
- [ ] 增加取消场景：A 暂停后取消 A，B 仍完成；验证 B 的令牌、记忆、结果不受影响。分别覆盖显式任务令牌取消与 asyncio.Task.cancel；后者额外验证 A 的外部父令牌不被内部子令牌反向取消，前者不能错误要求被主动取消的父令牌仍未取消。
- [ ] 所有并发测试在 finally 释放等待门、取消/await 未完成任务，清理本场景资源；失败路径也不能留下悬挂任务影响后续测试。
- [ ] 将审查中的三类流边界探针固定到 Provider 专项：已收到 ToolCallCompleted 后断流；缺少 ProviderCompleted；event sink 抛出指定异常。业务工具调用数均为 0；异常身份/协议失败分类保持，不能新增流降级或重试。
- [ ] 执行新专项及 test_agent_async.py。新增隔离测试在健康旧实现上通过是合理的：这是补覆盖，不是在制造“旧实现有串会话”的结论。

### C4：回归、自审与交接

**修改：** README.md、project.md；实施证据仍放 runtime/agent-refactor-round1-review-fixes/。

- [ ] 自审：无 coordinator→engine 依赖、无隐蔽全状态代理、无跨调用 progress、无两份提交算法；正常和异常只合并一次；C1 的状态/顺序断言未被削弱。
- [ ] 最终运行一次完整 unittest、语法检查和 git diff --check，记录真实结果。若之后改代码，补跑受影响测试；既有 Windows 锁非稳定失败若重现，保留首次日志并归因，不能只不断重跑直到通过。
- [ ] README 简述快照输入、结果返回及异常进度合并；project.md 记录 F1/F2 的完成证据、未验证项及下一步。仅在本补齐验收通过后，才将第二轮前置记录更新为可继续。
- [ ] 最终汇报变更文件、接口变化、异常保留依据、隔离测试及全量结果；明确同实例并发、真实厂商、未测试平台仍不在本轮证明范围。

## 6. 验证命令

在项目根使用已有环境。按阶段执行对应集合；不要把下面所有命令在每个小改动后全跑一遍。

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p 'test_agent*.py' -q
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p 'test_memory*.py' -q
.\.venv\Scripts\python.exe -B -m unittest tests.test_conversation_memory tests.test_batch_failure tests.test_verification_evidence tests.test_mcp_dependency_boundary -q

# C1 / C2 新专项
.\.venv\Scripts\python.exe -B -m unittest tests.test_agent_memory_boundary tests.test_agent_import_boundaries -v

# C3 新专项
.\.venv\Scripts\python.exe -B -m unittest tests.test_agent_instance_isolation tests.test_agent_provider_request -v

# C4 最终门禁
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -q
.\.venv\Scripts\python.exe -X pycache_prefix=runtime/agent-refactor-round1-review-fixes/pycache -m compileall -q src tests
git diff --check
```

预期为测试通过，平台跳过逐项说明；新增接口专项在实现前的失败单独记录。通过数量以当时实际发现为准，不硬编码 1318。

## 7. 回滚与完成标准

使用 C0 的任务相关源码基线逐 hunk 撤销本轮改动，先核对文件是否又被其他会话修改；不得回退整个工作树或删除其他人的未跟踪文件。受影响测试与代码一同回退，保留失败证据。发现额外业务缺陷先登记，不把它混入本次行为不变的重构。

- [ ] F1：MemoryCoordinator 可脱离 AgentRunState 独立运行；引擎只合并允许字段，边界测试通过。
- [ ] 正常与异常结果均保留原调用/用量/审计时点；第一批提交与第二批失败、候选整体失败、原生取消测试通过。
- [ ] F2：两个独立 Agent 的确定性交错、候选/用量隔离和取消隔离测试通过。
- [ ] Provider 三类异常探针已固化，无业务行为扩展。
- [ ] 聚焦与全量结果可追溯；没有未解释的新失败，文档与实现一致。

## 8. 可交给 coding session 的提示词

```text
请在 D:\MaHong\AGENT_WORKSPACE_V2\projects\tricoder-cli 完成第一轮 Agent 重构的审查补齐。

先读 ../../AGENTS.md、AGENTS.md、README.md、pyproject.toml、project.md，以及：
docs/superpowers/plans/2026-10-05-agent-refactor-round1.md
docs/superpowers/plans/2026-10-05-agent-refactor-round1-review-fixes.md

本次授权修改补齐文档范围内的源码、测试和项目文档，请按 C0—C4 实施，不要只给建议，也不要同时启动第二轮目录迁移。每阶段验证和自审通过后继续，无需逐阶段再次询问。

两个目标：
1. MemoryCoordinator 脱离 engine/AgentRunState，使用明确的快照输入、结果返回和每次调用独立的窄异常进度载体；由 Runner 恰好合并一次允许字段。
2. 用 asyncio.Event 固化两个独立 Agent 异步交错时的消息、候选、业务/记忆用量和取消隔离；并固化文档中的流异常探针。

先建立当前工作树的相关源码基线和异常行为表征测试。重点保留两批压缩的部分提交、审计前后发布顺序、既有失败分支统计、原生取消和首异常身份。不要因改成 return 而丢失异常前进度，也不要重复合并。不得用代理或宽泛 Protocol 继续传完整任务状态，不得改变摘要、候选、持久化、锁或工具行为。

保留当前未提交和未跟踪改动。不读取真实密钥/会话库，不调用真实模型，不安装依赖，不自动提交或推送，不执行破坏性 Git 回退。证据保存到 runtime/agent-refactor-round1-review-fixes/。

完成后更新 README.md、project.md，报告实际修改、专项与全量测试、跳过项和剩余限制。基线失败或新增失败须先归因，不能削弱断言或不断重跑掩盖。验收通过后再将第二轮前置状态标为可继续；本次不执行第二轮。
```
