# 检查结果、任务收尾与失败记忆：最小修改执行计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. 用户将交给另一 coding session 实施；不要求启动子 Agent。

**Goal:** 审查发现测试失败时可以交付报告；真正未完成的任务保持未完成；失败历史也可以进入经过确认保存的会话记忆。

**Architecture:** 复用 CommandCheckRecord、TaskValidationTracker、现有文件副作用证据、finish 和结构化记忆。分开检查事实、交付声明及宿主安全约束，取消“执行过测试就必须修到通过”的隐含规则；记忆覆盖按已结束且历史闭合的任务推进，不再按任务成功推进。

**Tech Stack:** Python 3.11+、asyncio、unittest、既有 Provider/Console/TUI/SQLite；不新增依赖。

**Spec:** 本文第 1—5 节为本轮设计与验收约定。状态：R0—R3 已实施，R4 最终回归与记录已完成；实际证据见 `runtime/task-outcome-memory/verification.md`。

## 1. 已确认的问题与本轮边界

当前调研基线 HEAD：`cb617e4`，另有“记忆默认开启”的未提交修改。实施以最新工作树为准，不能覆盖这批修改。

已用真实 ToolRegistry 和临时 unittest 工程复现：审查任务没有修改文件，失败测试执行完整、前后快照稳定，finish 提交报告后仍得到 `ok=false`，并追加“文件修改后的验证失败”。另一个合成会话已有消息但最新成功任务位置为 0，保存允许预览空记忆，refresh 不调用摘要器。

代码位置（行号可能随实现变化）：

| 位置 | 当前问题/责任 |
|---|---|
| `tools/command.py:RunCommandTool` | 非零退出码映射 execution_failed/replan；保留真实工具结果即可，不在本轮重写错误体系 |
| `task_observation.py:apply_tool_transition` | 收到检查证据就 `required=True`，将检查事实变成交付门禁 |
| `engine/loop.py:_initialize_verification/_finish_success` | 从检查状态再次推导验证义务；只在成功后推进记忆位置和生成候选 |
| `session/runtime.py` 的状态合并、证据失效与恢复路径 | 存在相同反推逻辑，可能让修复后的 Agent 再次被旧状态阻塞 |
| `context/coordinator.py:build_review_candidate` | 目标位置为 0 时认为已经覆盖，不生成候选 |
| `session/runtime.py:preview_memory_save` | 两个位置均为 0 就展示完整，允许保存空对象 |

本轮不做任务类型分类器、任务计划系统、自动需求验收、多 Agent、Provider 扩展、数据库迁移、输出摘要重做或通用错误类型重构。报告严重级别、日志首尾截断、非 Git 目录提示等另行处理。

## 2. Global Constraints

- 先读 `../../AGENTS.md`、项目 `AGENTS.md`、README、pyproject.toml、project.md 和本文；工作仅限项目内。
- 保留当前工作树所有既有修改，尤其默认 `structured` / `reviewed_summary`、CLI 配置传递和相应测试；不 reset、不从 HEAD 覆盖文件。
- 不读取 `.env.local`、真实凭据、真实会话数据库或用户桌面工程；只使用合成临时工作区和模拟 Provider。
- 不安装依赖，不调用真实模型，不自动提交或推送；诊断与日志置于 `runtime/task-outcome-memory/`。
- 不将测试非零退出码改成零，不把工具失败统一改成成功，不关闭失败收敛，不放宽权限、审批、锁、UNKNOWN 或清理规则。
- 检查输出及模型声明不能成为可信执行证据。记忆不能恢复权限、审批、文件副作用或测试通过状态。
- 持久化继续要求 `/memory save` 预览确认；摘要失败不删除原历史，不自动保存完整对话。

## 3. 最小收尾设计

### 3.1 复用已有事实，修正验证义务的来源

保留 CommandCheckRecord 的 returncode、execution_complete、workspace_stable 和 TaskValidationReport 的失败记录。一次测试正常结束但失败，是检查发现，不自动要求本次任务修复。

`verification_required` 仅表示宿主已有的修改验证义务：受控文件修改、既有未解决的修改验证义务、未知影响等。不要仅因出现检查证据、失败快照或“通过/失败”的展示字符串，就建立新的修改验证义务。

同时检查 `apply_tool_transition`、`_initialize_verification` 和 SessionRuntime 的合并/切换/恢复路径；不能只删除一处 `required=True`。可抽取一个很小的纯辅助函数统一此语义，但不新增验证框架。

约束：

- 只观察到失败、未产生修改验证义务时，失败事实保留，但不阻塞 finish。
- 有实际文件修改或继承的未解决修改义务时，仍保留本项目现有的验证门禁。本轮不声称支持任意未验证修改的成功交付。
- 未知影响、取消、审计失败、清理失败继续按原优先级停止，不能因为模型声明已交付而绕过。
- 无关检查成功不能清除另一个失败记录；不得清空 TaskValidationTracker 来换取绿色状态。
- 旧进程/旧持久化状态若缺少足够来源证据，保持保守处理；不得仅凭“当前修改文件数为 0”清除既有义务。说明用户重启后新任务的适用范围。

### 3.2 给 finish 增加一个可选字段，不增加任务类型

接口：`finish(summary: str, outcome: "completed" | "incomplete" = "completed")`。

- summary 继续必填；旧调用不带 outcome 时保持请求完成的兼容语义。
- completed 是“模型声明请求已交付”，不是“全部测试通过”或“宿主证明需求满足”。
- incomplete 用于尚未完成、受阻等情况；finish 工具本身可执行成功，但最终 RunResult.ok 必须为 false。
- 非法 outcome 按参数错误处理。native schema、legacy_json、工具说明和统一提示词同时更新。
- 从真正执行成功的内置 finish 参数，经明确的批次结果字段传到收尾，不从 summary 文本猜测，不信任扩展伪造的结果元数据。
- 模型请求 completed 且宿主无硬阻塞、无未满足的修改验证义务时，允许 `RunResult.ok=True`，即使 TaskValidationReport.status 为 failed。
- 不能把“没有修改文件”作为用户目标已完成的证明；提示模型按原始请求判断，有未完成工作应提交 incomplete。宿主本轮不实现语义验收，因此模型误判仍是已知限制。

这是对“交付状态”的有限表达，不是按 review/modify/diagnose 写死分支，也不新增额外 LLM 审核请求。

### 3.3 展示只做必要调整

Console/TUI 分开显示任务结束结果、检查结果、文件变化；保留“需求覆盖未自动确认”。允许出现“已交付（存在检查失败）”，但不能把检查失败显示为通过。

“文件修改后的验证失败”只在确有修改验证义务时使用；观察性检查失败改为“检查发现失败，见检查记录”。本轮不重做 Markdown 渲染或日志布局。

## 4. 失败任务记忆设计

### 4.1 覆盖位置改为已结束的完整任务

本轮为最小兼容保留 `latest_completed_task_seq` 字段名，统一将其文档语义改为“最新可纳入记忆的已结束任务位置”，含成功和失败；UI 用“最新已结束任务”。它不再是成功凭据，禁止其他逻辑用它恢复执行成功状态。无需改 SQLite schema。

在统一收尾处推进此位置，而不是只在 `_finish_success` 的 completed 分支推进。依据是：当前任务确已结束，待纳入的任务块历史完整，消息序号有效。不能只因为存在一轮完整工具调用就忽略后面的残缺调用；也不能越过缺失的任务块推进连续覆盖。

复用 `TASK_TERMINATION_KIND`、可信终止标记白名单、`with_task_termination_facts` 和 ContextManager 任务块检查：

- 正常 finish 但宿主验证失败、显式 incomplete、轮数耗尽、重复失败/观察/振荡停止，都要留下确定的未完成事实。
- 失败事实由宿主根据最终结果生成，不照抄模型声称成功的 summary，也不靠字符串匹配决定最终状态。防止 finish 声称成功却被宿主否决后，摘要仍写成已完成。
- 取消、模型中断、UNKNOWN、审计/清理异常也保留真实中断事实；已有完整结果保留，缺失结果仅按既有配对规则记录“中断/未执行/未知”，不得补假成功。
- 完整性无法确认时保留原历史及受阻原因，不推进位置、不允许跳过这段保存较新的任务；不得宣称“失败记忆完整”。中断的未提交 Provider 片段不能直接拼进可恢复对话。
- 已有专用终止标记复用且只追加一次；通用未完成标记用于尚未覆盖的路径，不重复制造待办。
- 若故障发生在形成可用历史之前，保留安全失败事实并明确无可摘要任务内容；不制造凭空的消息来源。

### 4.2 摘要时机与预算

不要为了让失败进入记忆而在所有异常分支中强行请求模型。

- 正常 finish（含检查失败或 incomplete）：历史闭合后复用现有有界摘要流程，最多一次整理流程；保留原有最多两批限制。
- 收敛停止、轮数耗尽、取消、Provider 故障、审计/清理失败等异常停止：本轮不额外发起摘要请求，不新建 token 绕过取消，不扩大请求预算。保留已闭合历史/终止事实，稍后由 `/memory refresh` 整理；若运行资源仍未安全释放，继续拒绝刷新。
- 摘要失败只增加记忆告警，保留原历史、旧候选及任务结果；不得把任务失败改成成功。审计与取消的既有更高优先级不变。
- 统一收尾不得递归调用自身或重复发 RuntimeCompleted/RuntimeFailed；摘要工作置于现有异步编排边界，TaskFinalizer 保持同步状态转换职责。
- 持久化 off 时不自动生成保存候选；结构化压缩 off 时保留原有禁用行为。

### 4.3 空候选与刷新

- 当候选 revision=0、覆盖为 0 且无任何语义条目时，`/memory save` 拒绝并提示“暂无可保存的会话记忆”。不显示“覆盖完整”确认框。
- 有已结束历史但候选未覆盖时，先提示 `/memory refresh`；刷新必须消费失败任务来源，成功后仍需用户确认保存。
- 合法的全量删除/清空后候选可能没有条目，不能一律禁止；保留既有显式清除和归档编辑语义。
- 成功保存、重启恢复应保留未解决事项，不能恢复任何可信执行权限/证据；默认开启配置保持不变。

## 5. Review Focus

1. 只观察测试失败的会话再发下一任务/切换会话，不能又被 Runtime 推导成修改义务（R1）。
2. 有真实修改或继承修改义务时，completed 不得绕过验证；UNKNOWN 和清理失败仍阻塞（R1）。
3. finish 自称完成但宿主否决，失败事实必须进入摘要，不得洗成成功（R2）。
4. 中断、多工具批次与旧终止标记组合，不能重复回填、重复终止或跨缺口推进（R2）。
5. 空初始候选与合法清空候选区分；保存预览后发生新失败任务，旧预览必须失效（R3）。

## 6. 分步实施与验收

### R0：建立失败复现，不动用户工程

**文件：** 新建 `tests/test_task_outcome_memory.py`，按需要复用现有测试 fixture；证据放 `runtime/task-outcome-memory/`。

- [x] 用临时工作区创建一个真实失败的 unittest。模拟 Provider 调 run_command 后 finish，断言退出码 1、workspace_stable=true、修改文件为空、检查失败被保留，但任务可交付。先记录当前失败结果。
- [x] 复现失败任务位置不推进、refresh 无摘要、空候选允许保存。断言应针对真实行为，不依赖模型文本恰好出现某词。
- [x] 记录起始 HEAD、dirty 文件和测试命令；已有默认开启修改不是本轮可回滚内容。

### R1：拆开检查事实与交付结果

**修改：** `src/tricoder/task_observation.py`、`engine/loop.py`、`engine/state.py`、`engine/tool_batch.py`、`session/runtime.py`、`tools/command.py`、`protocols.py`；仅按需要修改 models、Console/TUI。

**接口：** finish 新增可选 outcome；RunResult.ok 和 TaskValidationReport 分别表达交付结果和检查事实；既有工具错误协议不变。

- [x] 先补 finish completed/incomplete/缺省/非法值测试，以及 legacy_json 兼容测试。
- [x] 修正检查事实到验证义务的转换和 Runtime 二次合并，接通内置 finish outcome；不新增任务类型字段。
- [x] 覆盖：审查失败检查后可交付；无修改但 incomplete 仍未完成；修改后失败/未验证仍未完成；A 检查失败不能被 B 通过抹除；取消/UNKNOWN/清理失败优先。
- [x] 覆盖同一 Session 下一任务、模型切换、Runtime 合并与重启恢复的保守行为；已继承的真实修改义务不被丢失。
- [x] 更新最小展示文案并跑聚焦测试。不要删除旧安全断言；因语义改变调整的测试须解释新旧区别。

### R2：统一结束记录，失败可进入记忆

**修改：** `engine/finalization.py`、`engine/loop.py`、`engine/state.py`、`models.py`、`context/memory.py`、`context/coordinator.py`、`context/manager.py`、`context/summarizer.py`；只修改确实需要接通的文件。

**接口：** 原字段 latest_completed_task_seq 保留名称、改为闭合终态覆盖含义；新增通用未完成终止常量时必须加入可信标记映射，复用确定性待办保留机制。

- [x] 先补正常 finish 被宿主判失败也能推进覆盖并产生未完成记忆的测试。
- [x] 将历史闭合、单次终止事实和位置推进集中到统一收尾，不逐个异常分支复制摘要代码。
- [x] 正常结束只调用一次现有候选整理；异常停止只保留事实，显式 refresh 后可生成候选。刷新不得重跑业务工具。
- [x] 覆盖显式 incomplete、验证失败、max_rounds、ProgressGuard、取消、Provider 中断、审计失败及清理失败；各路径证明消息配对、调用预算、结果不被改写和终态事件单次。
- [x] 摘要器返回“已解决”的候选时，宿主未完成事实仍被保留；摘要失败后历史和旧候选不丢失。
- [x] 覆盖成功→失败、失败→成功连续任务，保存范围不能漏掉中间失败，也不能跳过不完整历史。

### R3：保存、恢复和展示回归

**修改：** `session/runtime.py`、`presentation/console.py`、`presentation/tui.py`、README、project.md。

**测试：** `test_memory_save_coverage.py`、`test_memory_persistence.py`、`test_memory_summarizer.py`、`test_session_runtime.py`、`test_cli.py`、`test_tui.py` 及 R0 新增测试。

- [x] 初始空候选拒绝保存；存在未覆盖失败任务时提示 refresh；合法清除不被误挡。
- [x] 临时 SQLite 中完成“失败→refresh→确认保存→重启”，确认未解决事项恢复且无原始工具输出/可信执行状态持久化。
- [x] 保存预览后新任务结束或候选变化，旧预览失效；摘要/存储失败保留可恢复状态。
- [x] Console/TUI 均区分交付与检查，不再在无修改场景追加“修改后验证失败”；需求覆盖仍未自动确认。
- [x] 更新 README：失败记忆、延后 refresh、保存确认、任务交付与检查通过的边界；更新 project.md 和本文状态。

### R4：最终验证与交付

先按 R1/R2/R3 运行相关测试，新增核心文件可用：

```powershell
.venv\Scripts\python.exe -B -m unittest discover -s tests -p test_task_outcome_memory.py -v
.venv\Scripts\python.exe -B -m unittest discover -s tests -p test_memory_save_coverage.py -v
.venv\Scripts\python.exe -B -m unittest discover -s tests -p test_agent_convergence.py -v
.venv\Scripts\python.exe -B -m unittest discover -s tests -p test_agent_termination.py -v
.venv\Scripts\python.exe -B -m unittest discover -s tests -q
git diff --check
```

- [x] 全量通过后自查：没有靠关记忆、跳过新增测试、放宽修改验证或删除失败记录获得通过；不重复进行无变化的全量测试。
- [x] 记录实际测试数量、跳过原因、剩余限制及未验证平台，不引用上一批测试作为本批证据。
- [x] 最终汇报涉及文件、旧/新行为、记忆保存操作、现存会话兼容边界与未完成事项；保持未提交状态。

## 7. 可复制给 coding session 的提示词

```text
请实施 docs/superpowers/plans/2026-10-07-task-outcome-memory-minimal-fix.md。

这是一次最小修复，不是 Agent 架构重构。请先读根/项目 AGENTS.md、README.md、pyproject.toml、project.md 和执行计划，以当前工作树为准，保留“记忆默认开启”等已有未提交改动。

核心要求：
1. 保留测试退出码和检查失败事实，但不再把观察到的测试失败自动变成整个任务失败。实际修改后的验证义务、UNKNOWN、审批、取消、审计和清理约束保持有效。
2. finish 仅增加可选 outcome=completed|incomplete，默认兼容旧调用；它是模型的交付声明，不能伪造测试通过，也不能绕过宿主约束。不增加 review/modify/diagnose 分类器。
3. 任务成功与否不再决定能否进入记忆。按已结束、历史完整的任务推进覆盖位置，确定性保留未完成事实。正常结束生成候选；异常停止保留事实，后续 /memory refresh 整理，不因摘要重启已停止的模型循环。
4. 拒绝保存初始空候选；失败历史能经 refresh、确认保存后重启恢复。保留现有脱敏、配对、候选原子更新和审批保存边界。

按 R0—R4 小步执行：先失败复现，再最小修复、聚焦测试、完整回归、自查和文档更新。必须检查 SessionRuntime 是否重新把检查失败变成修改验证义务，不能只改 Agent 的一个 if。完整回合不等于整段任务历史完整，不能跨缺口推进记忆位置。

只用模拟 Provider、临时工程和临时 SQLite；不读 .env.local、真实会话库或桌面工程，不联网调用模型、不装依赖、不自动提交或推送。不得通过把测试失败改成功、清空检查记录、关闭默认记忆或关闭失败收敛来通过测试。

验收至少包括：审查发现失败但可交付；修改后验证失败仍未完成；显式 incomplete 保持未完成；同 Session 下一轮不被观察性失败污染；失败记忆可保存恢复；取消/UNKNOWN/清理失败不被降级；中断批次不重复回填；旧保存预览失效；空候选拒绝。

完成后汇报实际修改、测试证据、兼容限制和未完成事项，并更新 project.md 与执行计划状态。无需等待逐步确认；若发现必须改变本文安全边界或做大范围迁移，先报告具体原因，不自行扩张范围。
```
