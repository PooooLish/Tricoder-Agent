# TriCoder 下一阶段：任务验证、需求澄清与等待、失败收敛执行计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. 本次由用户另行交给 coding agent 实施；不要求启动子 Agent。

**Goal:** 让 TriCoder 能说清本次任务验证了什么、在需求不清时真正等待用户，并在重复失败或无效操作时及时停止。

**Architecture:** 沿用 Provider → Runner → 工具 → SessionRuntime 的现有边界。先把命令副作用观察与验证事实分开，再加入进程内澄清等待，最后由本地计数器控制失败收敛；不依靠模型自觉停止。

**Tech Stack:** 现有 Python 3.11+、unittest、Rich、Textual；不引入新框架或依赖。

**Spec:** `project.md` 中“2026-10-06 后续开发目标与阶段划分”，以及本文第 1—3 节的具体范围和验收约定。

**状态：** 第一批 S0/A1/A2、第二批 B1/B2 和第三批 C1/D 均已实施；第三批复审发现的 F1/F2/F3 修复候选也已完成本地验证，当前停在待复审交付点，不表示复审已经通过。2026-10-06 实施基线 HEAD 为 `43f721a`，始终以保留既有修改的工作树为准。证据依次见 `runtime/task-quality-round1/verification.md`、`second-batch-verification.md`、`third-batch-verification.md` 与 `convergence-review-fixes.md`。

## 1. 为什么要做，以及先做到什么程度

### 1.1 当前代码能确认的问题

| 现象 | 代码依据 / 现状 | 本轮处理 |
|---|---|---|
| `python main.py` 显示执行成功，任务却以“文件影响未确认”结束 | `src/tricoder/tools/command.py` 只对 `_is_verification_command(args)` 捕获执行前后快照；普通脚本没有快照，正常退出仍返回 UNKNOWN | A1：为普通允许命令补充副作用观察，不能通过忽略 UNKNOWN 解决 |
| 跑了其他目录的测试，不能证明本次文件正确 | `workspace/verification.py` 主要证明受覆盖文件状态和验证证据是否仍有效，不是需求覆盖分析器 | A2：分开显示验证种类、执行目标、证据时效与需求覆盖限制 |
| 不清楚用户意图时只能靠文本或结束任务 | 现有审批只回答允不允许操作，不能承载需求回答 | B1/B2：独立 `ask_user` 与可取消等待 |
| 重复读文件、测试失败、修改互相抵消 | 已有 native 无工具响应次数限制与总轮数限制，不是通用进展检测 | C1：增加有界、可解释的重复检测 |

普通脚本问题已经有小型复现实验：在临时目录执行只打印 Hello 的脚本，进程成功，但快照调用次数为 0、工具 effects 为 UNKNOWN。这里的 UNKNOWN 表示“程序没有获得足够的副作用观察证据”，不证明脚本修改了文件。

最新 `project.md` 记录：上次结束协议 P2（停止后的历史闭合与记忆兼容）已经修复。本次文档未独立复审该修复；S0 必须复核，不能把历史记录当作本轮测试结果。

### 1.2 交付顺序

1. **S0：确认当前基线和 P2。**
2. **A1：修复普通命令副作用观察。** 单独交付，优先解决当前使用阻塞。
3. **A2：任务验证记录与展示。**
4. **B1/B2：澄清协议、生命周期和界面。**
5. **C1：失败和无进展收敛。**
6. **D：串联验收与交接。**

每个阶段均可独立验收。推荐先交付 A1/A2，审查后再进入 B/C；不要在一个补丁中同时改所有状态机。

### 1.3 本轮不做

- 任务计划的设计、步骤状态维护和自动重规划：留到后续阶段。
- Docker/操作系统沙箱、任意命令放行、Provider 扩展、框架迁移。
- 跨重启恢复等待、释放工作区锁后后台等待、持久化终端会话。
- 完整测试覆盖率推断、自动判定任意业务需求正确、自动改低测试预期。
- 批量重构目录或修改会话记忆的默认开关。

## 2. 全局约束

- 先读 `../../AGENTS.md`、项目 `AGENTS.md`、`README.md`、`pyproject.toml`、`project.md`、`docs/framework/module-layout.md`。
- 只在当前项目操作；不读取 `.env.local`、真实凭据、真实会话数据库或用户桌面测试工程；用临时目录和 fake Provider 复现。
- 不安装依赖，不调用真实厂商 API，不自动提交或推送。保留执行开始时所有已有改动；禁止覆盖式恢复文件。
- 保持 strict/relaxed/fullaccess、read-only、路径绑定、审批、进程树清理、文件撤销和审计边界。用户回答不是执行审批。
- `UNKNOWN`、清理失败、取消不能被普通工具成功、`finish` 文案、摘要或用户一句“继续”消除。
- 同步/异步、native/legacy_json、Console/TUI 都要有明确行为；新增类型不得破坏现有导入方向和兼容构造。
- 证据保存到 `runtime/task-quality-round1/`，不得放源码、用户回答全文或其他敏感内容进脱敏审计。
- 对已启动命令的异常继续保守处理；不要把所有异常统一转为可重试。

## 3. 先统一三个概念

| 概念 | 能说明什么 | 不能说明什么 |
|---|---|---|
| 命令结果 | 某条实际命令在某 cwd 下退出码为 0/非 0，输出是什么 | 用户需求必然满足 |
| 文件状态观察 | 有限覆盖范围内，执行前后文件内容是否稳定 | 全操作系统无副作用、无网络行为、已建立沙箱 |
| 任务验证 | 本次做过哪些检查，检查对象和限制是什么 | 无关测试通过等于当前需求完成 |

新界面至少分开显示“执行结果”“文件状态检查”“任务验证”。保留 `RunResult.ok` 现有技术语义与兼容接口，但不得仅根据它把“需求已验证”显示为通过。即使任务正常收尾，也允许显示“已交付；功能未验证”。

首版任务验证做到**可追溯的事实记录和保守展示**。模型提供的用途、目标文件或相关性说明只作为“模型声明”；不变成宿主已证明的业务覆盖。全面自动验收不属于本轮。

## 4. 模块边界与新增接口约定

以下是计划新增接口，并非当前已有功能。实施时可按现有命名风格调整名称，但语义不变，并在交接中记录映射。

| 文件 | 职责 |
|---|---|
| `src/tricoder/tools/command.py` | 实际命令、cwd、退出结果和执行前后快照；不判断业务需求 |
| `src/tricoder/workspace/verification.py` | 复用受覆盖快照及证据 authority；不扩大为业务判断器 |
| 新建 `src/tricoder/core/validation.py` | 不可变验证记录类型；不导入 engine、tools 或界面 |
| 新建 `src/tricoder/engine/validation.py` | 单任务记录聚合、当前证据时效和保守结论 |
| 新建 `src/tricoder/core/clarification.py` | 请求、结果、宿主回调类型；不依赖 Textual |
| 新建 `src/tricoder/tools/clarification.py` | `ask_user` 参数校验和回调适配 |
| 新建 `src/tricoder/presentation/clarification_wait.py` | 有超时、可取消、首次完成生效的等待对象 |
| 新建 `src/tricoder/engine/progress.py` | 有界重复检测纯逻辑；不执行工具，不调用模型 |
| `engine/state.py`、`loop.py`、`tool_batch.py`、`finalization.py` | 任务状态、批次边界、停止优先级与统一收尾 |
| `models.py`、`tools/__init__.py`、`protocols.py`、`agent.py` | 最小字段与依赖注入接线、工具定义和协议说明 |
| `session/runtime.py`、`presentation/shell.py`、`presentation/tui.py`、`core/events.py` | 生命周期、锁、等待呈现及结果展示 |
| `context/memory.py`、`context/manager.py` | 新停止原因的历史闭合与失败事实保留；仅做必要扩展 |

**验证接口：** `CommandCheckRecord` 至少包含宿主生成的 `task_id/check_id`、规范化 argv、有效 cwd、kind、returncode、output 摘要或已有 spill 引用、受覆盖快照标识、证据限制。kind 固定为 `script/tests/syntax/static/information/other`；模型不能自行签发记录。`TaskValidationReport` 包含有界 records、`status`（`unverified/observed/failed/stale`）和 limitations。`observed` 表示有当前有效的检查事实，不等于业务通过。每任务最多保留 32 条，淘汰时展示“仅最近记录”，失败未解决状态不能随淘汰消失。

**澄清接口：** `ClarificationRequest(request_id, question, options)`；`ClarificationResult(status, answer)`，status 为 `answered/cancelled/timed_out/unavailable`；answer 只允许在 answered 时存在。宿主回调接收 request、取消令牌和超时秒数并返回 Result；Console 和 TUI 共享这些语义。新字段追加默认值，保持旧构造兼容。

**进展接口：** `ProgressObservation` 接收宿主规范化的工具名称、参数指纹、结果指纹、失败分类、受覆盖文件内容摘要和状态是否完整；`ProgressGuard.observe(observation) -> ProgressDecision` 返回 `continue/warn/stop`、固定 reason、count、limit。单任务实例，最多保留最近 32 条 observation；指纹不记录原始源码或答案。

## 5. S0：基线和前置复核

**文件：** 只读当前源码；更新本文件阶段勾选、`project.md` 和 runtime 验证记录。

- [x] 记录 HEAD、`git status --short`、相关已有 diff；执行中的真实基线优先于本文记录。不要读取被排除的秘密文件。
- [x] 读结束协议、P2 终止标记、工具批次、命令、验证、SessionRuntime 锁与快照代码。
- [x] 运行现有 `test_agent_termination.py`、`test_protocols.py`、`test_memory_compaction.py`、`test_memory_save_coverage.py`、`test_session_runtime.py`。
- [x] 复核 P2：零工具停止和“完整工具回合后停止”都能闭合；不配对的真实 ToolCall 仍不可压缩；失败待办不会被空摘要抹掉；失败不推进成功任务水位。
- [x] 在临时目录通过真实 ToolRegistry 运行只打印 Hello 的脚本，记录 exitcode、快照次数、effects、verification；把当前阻塞固定成 A1 的失败回归。
- [x] P2 与既有安全测试保持通过；A1 失败回归先红后绿，没有删除保护断言或清空历史。

测试命令格式（PowerShell，下文均沿用）：

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p 'test_agent_termination.py' -v
```

## 6. A1：普通命令的副作用观察

**修改：** `tools/command.py`；必要时调整 `workspace/verification.py` 的辅助接口。**测试：** `tests/test_command_compatibility.py`、`tests/test_verification_evidence.py`、`tests/test_effect_state.py`。

- [x] 写失败测试 `test_plain_script_stable_workspace_does_not_create_unknown`：真实临时 Hello 脚本，执行成功、前后均扫描、effects 为 NONE、普通脚本没有旧的测试验证证据。
- [x] 运行上述专项，确认新用例因当前快照缺失失败。
- [x] 把“是否捕获执行副作用快照”和“是否认可为旧验证命令”拆成两个判断。固定版本查询保留现有窄例外；其他允许命令在审批通过后、执行前和正常清理后捕获快照。
- [x] 执行前扫描不完整或失败时，不启动普通命令，返回明确的观察失败原因；不要伪造已执行或改为白名单绕过。执行后扫描失败、变化或清理不确定，继续 UNKNOWN 并停止。
- [x] 两份快照完整、同范围且内容稳定时，只恢复 `previous_unknown`。进入命令前就 UNKNOWN 的状态不得被清除。
- [x] 普通脚本 `verification_passed` 保持 None，不调用旧验证证据的成功签发路径。
- [x] 保留取消、超时、输出超限、清理失败的保守路径；无法确认进程未启动时，不按无副作用返回。
- [x] 补测：非零退出但文件稳定、脚本写文件、前后扫描失败、原 UNKNOWN、版本查询和异步路径。
- [x] 运行 A1、验证证据、Effect、子进程控制与取消组合；真实工具入口覆盖稳定脚本与写文件脚本。
- [x] README 已说明扫描排除、有限覆盖及“不是沙箱”；第一批与 A2 同批交付审查。

## 7. A2：任务验证事实、时效和展示

**新增：** `core/validation.py`、`engine/validation.py`、`tests/test_task_validation.py`。**修改：** 第 4 节相关模型、工具、Runner、Runtime 与展示模块，`README.md`。

- [x] 写失败测试：修改 `test1/main.py` 后只运行根目录 calculator 测试，报告可显示测试命令成功，但不能显示 `main.py` 功能验证通过；普通 Hello 脚本可以记录实际输出，不升级旧验证水位。
- [x] 实现第 4 节验证类型，由命令执行器产生记录，Runner 按 task_id 收集，RunResult 追加有默认值的报告；外部工具自报记录被剥离。
- [x] 记录实际 cwd 与规范化目标；script、tests、syntax、static 使用策略已验证 argv 并按工具选项语法分类，排除项、过滤表达式和静态检查选项值不冒充目标；无法识别时不猜测导入覆盖。
- [x] 输出截断复用现有 spill 引用并标注；`OK`、`Ran 0 tests` 仅生成诊断提示。
- [x] 验证记录绑定当前 task ID、每任务轮换的独立 check authority 与完整快照；即使历史 task 标签复用，跨任务记录仍被拒绝。
- [x] 失败替代按规范化 argv＋cwd 严格匹配，且要求宿主确认执行完整、非信息命令工作区稳定；清理失败等退出码 0 的不可信结果不能清除失败，容量淘汰不删除未解决失败。
- [x] 后续写入按检查签名使报告 stale，信息查询和无关稳定命令不会复活编辑前证据；SessionRuntime 收尾扫描失败或发现未归属变化时同步使已有检查 stale。
- [x] 文件状态检查单独保留，syntax、static、tests、script 分别呈现；Console/TUI 均展示记录限制和最近记录截断，并始终显示“需求覆盖未自动确认”。
- [x] 保留旧 `verification` 文件状态语义；无检查任务和正常 finish 都不会伪造功能验证。
- [x] 覆盖 0 tests、跨任务、写后过期、stdout 伪造、spill、失败替代、无检查、32 条、sync/async、Console/TUI。
- [x] 运行任务验证、证据、Agent、SessionRuntime、Shell、Console/TUI 聚焦回归；结果记录到 runtime 证据。
- [x] 工具与系统说明要求选择相关检查，不为 finish 循环运行无关测试；本地规则仍是唯一可信边界。

## 8. B1：需求澄清协议与等待生命周期

**新增：** `core/clarification.py`、`tools/clarification.py`、`tests/test_clarification.py`。**修改：** 工具注册、Runner/批次状态、Runtime、事件及必要的历史闭合逻辑。

- [x] 写失败测试：模型一次返回 `ask_user` 和写文件；用户回答后，该批写文件不得执行，必须由模型基于新回答重新提出操作。
- [x] 注册 `ask_user(question, options?)`：问题 1—1000 字符；options 可省略，提供时为 2—4 个互异的非空字符串，每项最多 120 字符；回答最多 4000 字符，空回答不提交。允许自由文本，不强制选项。
- [x] 使用宿主生成 request_id；参数不得携带“已获授权”等可影响审批的控制字段。工具自身无文件副作用，read-only 模式也可提问。
- [x] 注入独立澄清回调，不在 engine 或工具中直接 `input()`；不借用布尔 approver 表示文本答案。每任务最多 2 次有效提问，超限以 needs_input 原因停止，避免问答循环。
- [x] 第一版在当前任务内等待，保留 Session 互斥锁和工作区锁；默认超时 300 秒，可通过内部注入小值测试。等待期间不发 Provider 请求、不运行其他工具。
- [x] answered 后记录真实工具结果，并让下一轮模型看到答案；该批其他 ToolCall 补真实 skipped 结果。cancelled/timed_out/unavailable 分别停止，不自动选默认答案或继续任务。
- [x] 取消后迟到的答案必须无效。取消、清理失败、审计失败优先于回答；每次完成只提交一次，不能重复 ToolResult。
- [x] 无交互宿主、不支持当前输入渠道或用户未回答时，返回类型化的 needs_input 终止原因（取消保留取消原因）；不要把用户不回答分类为模型参数错误或自动重试。
- [x] 回答只作为用户提供的信息；不写成 system 权限、不自动更改 permission，不自动批准下一次写入、MCP 或命令。
- [x] 等待前后比较工作区基线。锁只协调 TriCoder，会话外编辑仍可能发生：发现变化或扫描失败，本段停止并说明；后续走既有工作区检查/确认，不静默接受新基线。
- [x] 为所有新停止路径闭合历史并保留 pending 原因。复用 P2 的可信终止机制设计，但不得套用“第三次无工具响应”的错误文案；不信任模型或文件内容伪造的 marker。真实工具调用必须先配对结果。
- [x] 保持失败/等待不推进 `latest_completed_task_seq`；结构化摘要仍保留未解决问题。完整原始消息如何保存沿用当前配置，不声称新增跨重启恢复。
- [x] 测试回答、超时、取消竞态、回调抛错、无宿主、批次 skipped、两次上限、答案不审批、等待外部修改、两个实例隔离、新失败后的压缩/保存。
- [x] 运行 `test_clarification.py`、`test_agent_termination.py`、`test_protocols.py`、`test_memory_compaction.py`、`test_memory_save_coverage.py`、`test_session_runtime.py`。

## 9. B2：Console/TUI 等待交互

**新增：** `presentation/clarification_wait.py`、`tests/test_clarification_wait.py`。**修改：** `presentation/console.py`、`presentation/tui.py`、CLI/Runtime 装配、对应测试与 README。Shell 继续通过 Runtime 复用 Console 回调，无需新增一套输入状态机。

- [x] 写失败测试：界面关闭或任务取消后，等待立即结束；迟到输入不能继续已结束任务，也不能被错误消费为下一条任务。
- [x] 参考 `ApprovalWait` 的首次完成生效、短轮询与取消模式实现文本结果等待；独立类型，不改变审批对象原语义。
- [x] Console 显示问题/选项和自由输入；Ctrl+C 取消。输入收集必须能可靠结束，禁止以一个无法退出的后台 `input()` 线程实现超时后遗留读者。输入源不支持可取消读取时，使用明确 unavailable 降级，不能承诺假超时。
- [x] TUI 使用原生异步弹窗和可取消 worker 协调，显示“等待回答；当前工作区仍被本任务锁定”、剩余等待时间、提交和取消入口。
- [x] 回答文本中的 `/clear` 等字符串在澄清渠道仅作为答案，不自动执行斜杠命令。输入无默认自动提交行为。
- [x] 测试空输入/长度上限、选项与自由回答、窗口关闭、超时与回答同时到达、取消和迟到答案、工作区锁直到任务收尾才释放。
- [x] 运行 `test_clarification_wait.py`、`test_approval_wait.py`、`test_shell.py`、`test_tui.py`、`test_cli.py`。手工交互若未实际验证，单独列为限制。

**第二批实现说明：** 同步与异步工具入口都复用调用方取消令牌；Console 只对真实终端启用可取消读取，自定义阻塞 `input_fn` 和非交互输入明确返回 unavailable。工作区等待前后使用现有受覆盖快照核对，SessionRuntime 的任务前门禁继续负责完整基线确认。TUI 保留两参数旧 runtime factory 兼容；这不等于跨重启恢复，C1/D 也未在本批实施。

## 10. C1：失败和无进展收敛

**新增：** `engine/progress.py`、`tests/test_progress_guard.py`、`tests/test_agent_convergence.py`。**修改：** `engine/state.py`、`loop.py`、`tool_batch.py`、`finalization.py`、审计字段与历史终止兼容。

### 10.1 首版规则与固定阈值

| 类型 | 识别条件 | 首版动作 |
|---|---|---|
| 相同状态重复失败 | 相同工具、规范化参数、错误码/退出码/有界结果指纹、完整且相同的受覆盖文件内容摘要 | 同一失败指纹累计第 2 次提醒，第 3 次停止；普通读操作不清零 |
| 无变化重复读取 | 相同只读工具、参数与结果指纹；其间没有可信文件变化或新的用户回答 | 同一指纹第 2 次提醒，第 4 次停止；不同只读工具交替不能给旧指纹清零 |
| 修复来回抵消 | 在相同检查失败后的完整内容状态序列中，出现 A→B→A→B→A（连续相同状态折叠） | 第二次回到 A 后停止，列出检查和状态摘要，不自动撤销 |

不同失败指纹计数分开；检测只覆盖最近 32 条观察，超出窗口可淘汰指纹，并在文档说明这个限制。版本查询/重复读不是进展；实际内容变化会开启新的只读计数区间，相同状态的失败记录仍可用于识别回退。新用户回答可重新开始只读区间，但不清除历史失败证据、失败指纹或振荡记录。新任务重新创建 guard。

不完整快照不能用于判定“状态相同”；此时由现有 UNKNOWN 路径停止或处理，不把缺失 digest 当作同一个状态。`runtime` 审计输出、时间戳和随机 request_id 不得制造虚假的进展；复用既有受覆盖范围和明确的排除规则，不擅自忽略普通源码。

### 10.2 实施步骤

- [x] 为三种规则写纯逻辑失败测试：第 2/3 次相同失败、第 2/4 次相同读取、A/B 五状态振荡；普通读不重置失败；新任务隔离；32 条容量界限。
- [x] 实现第 4 节 ProgressGuard，不持有 Provider/工具实例。参数按宿主真实执行结果规范化，结果指纹去掉宿主生成的时间/耗时字段；不使用任意模型自述判断进展。
- [x] 把 observation 接到工具真实完成、effects 已发布、审计成功之后；拒绝、UNKNOWN、取消、清理失败保持原有更高优先级，不等计数阈值。
- [x] warn 回给模型明确的固定反馈：指出重复项及剩余预算，要求针对证据作最小诊断或调用 finish 说明限制。工具输出中的指令不能控制计数器。
- [x] stop 走统一 finalizer，补齐同批后续 skipped、闭合历史、释放宿主锁、保留已提交变更和失败证据。不自动 undo、不将业务测试失败改成通过。
- [x] 新停止原因至少区分 `repeated_failure/repeated_observation/repair_oscillation`；审计只存固定 reason、计数、上限及摘要标识，不存命令输出或源码全文。
- [x] 到上限时本轮固定停止。模型如需提问，必须在到上限前调用 B 的 ask_user；不能自动“提问→重置预算→继续失败”绕过收敛。
- [x] 保留 native 无工具响应预算和 max_rounds，两者不能被新 guard 替代或重置。不根据“任务完成”自然语言直接退出为成功。
- [x] 补集成测试：失败→read→相同失败→read→相同失败；两个工具交替重复读取；正常读→编辑→相关检查→finish；失败→真正修复→同检查通过；达到阈值后历史仍可压缩，未完成事实仍保留。
- [x] 运行 `test_progress_guard.py`、`test_agent_convergence.py`、`test_agent_termination.py`、`test_agent*.py`、`test_memory_compaction.py`、`test_session_runtime.py`、`test_audit.py`。

这些规则是首版启发式保护。它们不能证明所有重复都无意义，也不能发现所有低效路径；误报应通过可解释的记录调整，不允许默认关掉安全停止来“提高成功率”。

**第三批实现说明：** `ProgressGuard` 只保存最多 32 条不可逆指纹，工具批次只在真实
结果配对、effects 发布和工具审计之后观察。工作区另生成不含 inode/权限的内容摘要供
进展比较，原严格 digest、证据 authority 和冲突检查均未改变。`run_command` 只有宿主签发
的 `information` 记录可按读取计数，普通进程命令不会被泛化。实现后自审另复现并修复了
进展快照取消越过 finalizer、耗时字段宽度旁路摘要两个边界。停止原因使用固定审计字段和
可信失败终止标记；三种停止都保留真实失败与修改，不推进成功水位，并可被后续压缩/保存
转换为 pending 事实。

## 11. D：端到端验收与自审

- [x] **场景 1：Hello 已存在。** fake Provider 读取→执行→finish；报告实际输出且不制造 UNKNOWN。
- [x] **场景 2：脚本确实写文件。** 创建额外文件后保留 UNKNOWN 并在 finish 前停止。
- [x] **场景 3：无关测试。** 测试事实可见，但 main.py 需求覆盖始终未自动确认。
- [x] **场景 4：需求不清。** ask_user→等待→回答→重新请求模型→必要审批→执行→finish；等待中 Provider 调用次数不增长。
- [x] **场景 5：等待取消/外部改动。** 不执行原批次后续写入；收尾释放锁；旧答案不复活任务；下次任务按正常工作区门禁运行。
- [x] **场景 6：失败收敛。** 重复失败/读取/修改振荡在各自阈值停止，不耗尽 30 轮；变更与失败证据保留，下一任务不继承计数器。
- [x] **场景 7：停止后记忆。** 各新停止路径后执行新正常任务，检查历史配对、压缩和保存；失败待办不丢失、成功水位不被失败推进。
- [x] 第一批最终代码运行完整 `unittest discover -s tests -q`、CLI `--help`、导入边界测试和 `git diff --check`；首次失败已分析修正，随后只在最终代码重跑一次全量。
- [x] 第一批已更新 README、模块说明、project.md、本计划与 runtime 证据；准确记录环境、数量、跳过和未验证边界，未声称真实 Provider 或用户工程已验证。
- [x] 第三批最终代码运行 C1/快照/审计 43 项（1 skipped）、Agent/Runtime/记忆/安全邻接 414 项（2 skipped）及 B/C/D 聚焦 200 项，全部通过。
- [x] 第三批完整回归 `Ran 1527 tests in 222.983s`，OK（13 skipped）；compileall 与 `git diff --check` 退出 0。结果记录于 `runtime/task-quality-round1/third-batch-verification.md`。

## 12. 自审重点

1. **扫描失败或部分覆盖：** A1 前扫描失败不能启动命令；后失败不能声称 NONE；A2 必须保留覆盖限制。
2. **看似成功的弱证据：** A2 对无关测试、0 tests、伪造 stdout、过期快照不作业务通过推断。
3. **等待并发：** B1/B2 对回答/取消/关闭竞态只能单次完成；同批写入不能抢跑，外部编辑必须被检测。
4. **停止后的历史：** B1/C1/D 对真实 ToolCall 配对、可信终止标记和 pending 事实逐项验证，不能重复上次 P2。
5. **收敛误判与绕过：** C1 区分真正的新内容与重复读取；无关工具和提问不抹掉失败计数，检测本身有容量上限。

## 13. 给 coding agent 的启动提示词

```text
请在 D:\MaHong\AGENT_WORKSPACE_V2\projects\tricoder-cli 中执行下一阶段改进。

先阅读 ../../AGENTS.md、项目 AGENTS.md、README.md、pyproject.toml、project.md、docs/framework/module-layout.md，再完整阅读：
docs/superpowers/plans/2026-10-06-task-verification-clarification-convergence.md

本轮第一批只实施 S0、A1、A2：复核结束协议 P2，修复普通 Python 脚本执行后因未捕获快照而产生 UNKNOWN 的问题，建立与实际任务关联的验证事实及清晰展示。完成后停在交付点，汇报结果，暂不执行 B/C/D 的新增功能；D 中与 A 相关的场景 1—3 本批必须验证。

请以开始时的最新工作树为准，先记录 HEAD/status/既有修改；文档写作时的 HEAD 仅供定位，不能覆盖当前文件。若项目已有同名能力，核对并补缺，避免重复实现。

每项先写能复现问题的失败测试，再做最小实现、聚焦回归和自审。不要把“退出码 0”“文件稳定”“需求已验证”混为一谈：普通脚本增加副作用观察，不能伪造旧测试证据；无关测试通过不能证明本次需求完成。保留 UNKNOWN、取消、审批、清理、工作区锁、撤销与记忆水位边界。

禁止读取 .env.local、真实密钥或用户会话数据库；不使用用户桌面工程作测试；不安装依赖、不调用真实 Provider、不自动提交或推送。不要放宽命令白名单、增加沙箱、迁移框架或顺带实现任务计划系统。

测试使用 fake Provider、临时工程和现有虚拟环境。验证中发现既有失败要区分本轮引入与基线问题，不能删除断言或伪造通过。将阶段证据放在 runtime/task-quality-round1/，更新 project.md 和执行文档的完成状态。

最终用中文说明：根因与修改、文件清单、实际测试命令和结果、保护边界、剩余限制，以及 B 阶段下一步。功能完成前不要仅以文档更新结束任务；遇到真实阻塞时说明证据与需要的信息。
```

**第一批通过审查后，继续 B 的提示词：**

```text
继续执行 docs/superpowers/plans/2026-10-06-task-verification-clarification-convergence.md 的 B1/B2。先检查 A1/A2 的当前代码与交接记录，保留已有修改。实现 ask_user 与 Console/TUI 的进程内等待：300 秒超时、可取消、保留 Session/工作区锁、回答不等于审批、回答后重新请求模型、同批后续动作跳过、等待前后检查外部修改。补齐新停止路径的历史闭合和记忆回归。实施边界与验证要求沿用文档，不做跨重启恢复或 C 阶段。完成后自审并交付。
```

**B 通过审查后，继续 C/D 的提示词：**

```text
继续执行 docs/superpowers/plans/2026-10-06-task-verification-clarification-convergence.md 的 C1/D。先复核当前 A/B 集成和阶段记录，按文档实现单任务有界 ProgressGuard：相同失败第 3 次停止、相同无进展读取第 4 次停止、A→B→A→B→A 修复振荡停止。不得重置原生协议预算，不得降低测试预期，不得自动撤销。补齐真实工具批次、取消、审计、历史闭合和记忆测试，再完成 D 的端到端与最终回归。更新交接文档，报告真实验证与限制，不自动提交或推送。
```
