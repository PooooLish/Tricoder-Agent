# TriCoder ReAct 结束协议修复执行计划

> **For agentic workers:** 使用 `superpowers:executing-plans`（如果可用）逐项实施。本文件是交给另一个 coding session 的任务书；编写本文件不代表已经修改或验证生产行为。

**Goal:** 模型完成工作后能明确调用 `finish`；忘记调用时收到准确纠错，反复不遵循协议时提前停止，避免重复读文件直到耗尽 30 轮。

**Architecture:** 保留显式 `finish` 和本地收尾验证。协议层提供准确结束说明，Runner 在单任务状态中累计原生响应缺少工具调用的次数；普通工具成功不重置计数。停止与成功分开判断，不根据模型自然语言推断任务完成。

**Tech Stack:** Python >= 3.11、现有 native/legacy 工具协议、异步 AgentRunner、unittest、Rich/Textual。无需新增依赖。

**Spec:** 本文第 1—4 节是本轮行为规格，第 5—7 节是实施与验收步骤。代码位置以 2026-10-05 当前工作树为依据，实施前按符号重新定位。

## 全局约束

- 先读 `../../AGENTS.md`、本仓库 `AGENTS.md`、`README.md`、`pyproject.toml` 和 `project.md`。
- 只在 tricoder-cli 仓库内工作。不读取 `.env.local`、真实密钥、真实会话库；不访问用户桌面测试项目，不调用真实 Provider，不安装依赖。
- 当前存在常用文件工具第一轮的大量未提交改动，尤其涉及本计划也要修改的 `protocols.py`、`engine/state.py`、`engine/loop.py`。以当前工作树为基线，不用 HEAD 覆盖，不撤销其他任务修改。
- 保留目录工具、审批、工作区锁、快照、修改账本、撤销、取消和验证证据的现有边界。
- 只实现本轮结束协议修复。不得顺带改记忆默认开关、Provider 厂商适配、命令白名单或测试覆盖判定。
- 不增加总轮数，不将 `tool_choice` 全局改为 required，不根据“完成”等关键词直接返回成功。
- 不自动提交或推送。阶段进展与验证结果写入项目文档，临时验证材料放 `runtime/react-termination-recovery/`。

## 1. 问题证据与根因

用户任务是创建 `test1/` 并写入简单 Python 文件。截图中第 19、21、23、25、27、29 轮反复出现：模型输出完成总结 → 框架提示“本轮没有工具调用” → 模型再次 read_file/list_files/glob_files。第 30 轮达到上限，任务显示未完成。

这轮是结束协议纠错循环，不是前一次游戏测试断言失败后的修复循环。输入累计 174,605 token 是跨轮相加，不是单请求窗口大小；开启语义摘要不能修复结束协议。

已核实代码：

| 位置 | 当前行为 | 本轮动作 |
|---|---|---|
| `src/tricoder/protocols.py::NativeToolProtocol.resolve_action` | 所有无 tool_calls 响应均产生 `ToolCallCountError` 纠错，包括正文结束且 finish_reason=stop | 保留显式结束约束，改善反馈 |
| `COMMON_SYSTEM_PROMPT` / `NATIVE_TOOL_PROMPT` | 提及 finish，但没有充分说明结束时必须调用它 | 明确完成、阻塞、需澄清时的结束路径 |
| `NATIVE_TEXT_FEEDBACK` | 仅要求再调用任意工具 | 明确引导 finish，禁止用无关动作充数 |
| `src/tricoder/tools/command.py::FinishTool` | 描述为“提交本轮任务的文字总结” | 强调结束本轮、进入本地验证、不能自证成功 |
| `src/tricoder/engine/loop.py::AgentRunner` | 无 actions 时反馈并 continue，直到 max_rounds | 单任务累计计数，达到上限后可靠停止 |
| `src/tricoder/engine/state.py::AgentRunState` | 当前无专项纠错次数状态 | 新增仅运行时字段 |
| `AgentRunner._finish_success` / `TaskFinalizer.finish` | 本地检查证据并收尾 | 保留；专项停止复用统一收尾但不得调用成功路径 |

只读协议复现已确认：纯文本、无 tool_calls、finish_reason=stop 返回空 actions；真正的 finish ToolCall 返回 finish action。该结果不代表修复已实施或真实模型已验收。

## 2. 行为规格：结束与成功分开

### 2.1 正常结束

1. 工作完成、无法继续、或必须等待用户补充信息时，模型应提交原生 `finish`，参数为 `{"summary":"结果、未完成项与下一步"}`。
2. `finish` 不需要额外审批，不执行用户代码；已有终止批次规则保留，finish 后同批动作仍须正确回填 skipped，不能执行。
3. 必须经过现有 `_finish_success` 和宿主收尾检查。模型说“测试通过”不能清除真实失败、待验证或 UNKNOWN。
4. 本轮不扩展 finish schema、不新增业务状态枚举；“阻塞/需用户信息”通过 summary 如实表达。不要把当前 `RunResult.ok` 宣传为已经完备表达业务验收。
5. 无工具调用的普通文本仍不直接结束；`finish_reason=stop` 仅表示厂商本次生成结束。长度截断、拒绝输出、流不完整也不能自动视为完成。

### 2.2 提示词与工具说明

系统提示词补充清晰规则，native 和 legacy 均使用各自协议调用 finish：

> 当工作已完成、无法继续或需要用户补充信息时，必须调用 finish 提交总结并结束本轮任务。不要仅以普通文本总结结束，也不要为满足工具调用要求重复读取文件或运行与当前任务无关的测试。finish 只请求结束，完成状态以本地验证结果为准。

FinishTool 建议描述：

> 结束本轮任务并提交总结；用于工作完成、无法继续或需要用户补充信息时。调用后由程序检查已有验证证据，不会因总结声称成功而标记验证通过。

`NATIVE_TEXT_FEEDBACK` 保持常量接口，内容改为：

> 本轮没有提交工具调用。如果工作已完成、无法继续或需要用户补充信息，请调用 finish，并在 summary 中说明结果与未完成事项；只有仍有必要工作时才调用其他工具。不要重复读取文件或运行无关测试来满足工具调用要求。

纠错内容是程序生成的提示，不得拼接模型正文、文件内容或工具输出为高权限指令。Provider 工具定义中必须继续包含唯一内置 finish；无需修改厂商 tool_choice 策略。

## 3. 单任务纠错预算

采用简单明确的累计预算，本轮不开发通用“智能无进展判断器”。

### 3.1 新字段与常量

- `AgentRunState.native_missing_tool_responses: int = 0`。
- `engine/loop.py` 模块常量 `MAX_NATIVE_MISSING_TOOL_RESPONSES = 3`。
- 每次创建新的 AgentRunState 初始化为 0；不要加入 SessionContext、SQLite 或跨任务记忆，不放在共享 Provider/Agent 实例上。
- 判断依据为 native 协议下现有解析结果 `audit_error_type == "ToolCallCountError"` 且无 actions；不解析自然语言，也不把 legacy JSON 错误或重复 call ID 混入这个计数。

### 3.2 精确时序

| 本任务累计次数 | 行为 |
|---|---|
| 第 1 次 | 记录 usage/历史，追加准确反馈，提示累计 1/3，允许下一轮纠正 |
| 第 2 次 | 同上，提示累计 2/3，并说明再次发生将停止 |
| 第 3 次 | 记录必要历史和审计，立即结束为未完成；不发送第 4 次纠错请求，不执行任何额外工具 |

普通工具成功、有效文件修改、测试通过均不清零：这是本任务的协议错误总预算，不是判断业务进展的计数器。连续和交替出现都受约束，例如 text → read_file → text → list_files → text 在第 5 个业务响应停止。

取舍必须写入 README：一次长任务中即便期间有有效进展，累计三次原生无工具响应仍会停止。此策略是有界纠错的第一版，不宣称能识别所有无进展行为。未来若要按进展重置，必须单独设计，不能本轮凭工具成功清零。

### 3.3 退出和审计

- 默认停止摘要：`结束协议纠正失败：本任务累计 3 次未提交工具调用，已停止继续请求；任务结果仍需确认。`
- 在已有 `invalid_action` 审计元数据中增加固定原因及计数，建议字段为 `reason="native_missing_tool_call"`、`correction_count`、`correction_limit`、`will_stop`；不记录模型正文或源码。
- 先保留该轮真实 usage，再处理停止；总轮数仍按现有轮号计算，工具次数不凭空增加。
- 审计失败沿用已有审计失败路径；取消、UNKNOWN、清理失败不能被新原因覆盖或降级。
- 使用统一 finalizer 返回 `ok=False`；专项超限不应调用 `_finish_success`，不触发“任务成功”的记忆保存候选流程。
- 保留已经提交的文件/目录效果、账本和验证状态；不自动撤销，不重放工具，不伪造 finish ToolCall 或孤立 tool result。
- 宿主仍执行原有收尾扫描/锁释放。即使宿主追加现有基线提示，也应保留专项停止原因。当前“未归属变化”措辞过宽的问题另案处理。
- `max_rounds` 仍作全局兜底。未达专项次数但先达到总轮数时，保持原有总轮数停止语义。

## 4. 范围之外

- 不自动把纯文本转换为成功，不通过关键词判断“任务完成”。
- 不强制额外 Provider 请求只生成 finish，不改变请求接口或厂商能力声明。
- 不实现一般测试失败指纹、修复振荡检测、任意工具参数错误总预算。
- 不修复用户的 game/test1，不放宽命令、不运行无关测试来制造验证通过。
- 不声称现有文件快照验证等于业务测试覆盖；截图中的计算器测试不能证明新脚本正确。
- 不顺带修复修改文件统计口径、工作区收尾误导文案；将其登记为后续事项。

## 5. 代码布局与实施步骤

预计修改 `protocols.py`、`tools/command.py`、`engine/state.py`、`engine/loop.py`、`README.md`、`project.md`；新增 `tests/test_agent_termination.py`；按需补 `tests/test_protocols.py`、`tests/test_tools.py` 和 `tests/test_session_runtime.py`。仅展示需要时才改 console/TUI，不新增通用框架。

### S0：确认当前基线并固化复现

- [x] 记录 git 状态、当前提交、重叠文件差异；确认目录工具第一轮已经存在且不被覆盖。
- [x] 使用现有虚拟环境，运行协议与相关 Agent 基线测试；区分既有失败和本轮引入失败。
- [x] 在 `tests/test_agent_termination.py` 创建确定性 FakeProvider，用独立 response 队列重放截图行为，禁用 planning，避免计划请求混入断言。
- [x] 先添加失败测试：原生纯文本和只读工具交替，目前会耗尽队列或达到 max_rounds，目标是第三次无工具响应提前停止。队列超取须立刻报错，异步测试设置有界超时。

### S1：补齐可理解的结束契约

- [x] 测试新 NATIVE_TEXT_FEEDBACK 明确包含 finish、summary 与避免无关动作的含义；不要为完整句子建立大量脆弱快照断言。
- [x] 修改系统提示词、native 指引和 FinishTool 描述；legacy 仍输出原有 JSON 动作，不能要求 legacy 使用原生 API。
- [x] 测试发给 FakeProvider 的工具定义实际包含 finish、必填 summary 和更新后的说明。
- [x] 测试纯文本 → 收到纠错 → finish 的真实解析和执行链，不能在测试中把纯文本当 finish。

### S2：实现累计纠错预算与可靠停止

- [x] 先为第 1/2 次继续、第 3 次停止、不因普通工具成功清零、新任务重置编写失败测试。
- [x] 在 AgentRunState 增加运行时计数，Runner 无 actions 分支中增加预算判断。protocol 层只解析，不保存跨轮状态。
- [x] 复用既有 usage、审计、反馈、finalizer 路径；计数停止发生在下一次 Provider 请求之前。
- [x] 原因通过现有 summary/审计传达，无需扩展公开 RunResult schema。UI 状态仍来自结构化结果，不能显示为成功。
- [x] 运行专项测试并检查无意的副作用：没有撤销、没有新工具执行、没有跨任务计数污染。

### S3：补齐边界回归和宿主收尾

- [x] 按第 6 节测试矩阵覆盖 finish 本身失败/验证未通过、取消、审计失败、native/legacy、同步/异步入口。
- [x] 至少一个集成用例使用临时工作区与真实内置文件工具：创建文件 → 三次无工具响应 → 提前停止；文件仍存在，修改状态保留，后续任务可取得同一工作区锁。不能访问真实 Session 数据。
- [x] 对同一 Session 的下一任务和两个独立 Agent 的纠错计数做隔离断言；不要求新增同一 Agent 实例并发支持。
- [x] 验证 finish 后同批工具不执行且配对完整。长度截断文本和空响应不能被当作成功；流不完整继续沿用 Provider 异常路径。

### S4：文档、回归和交付

- [x] README 解释显式 finish、三次累计预算、退出不代表验证成功，以及目前不会处理所有重复动作。
- [x] 更新 project.md：实现内容、未解决事项、测试结果与平台限制。
- [x] 按第 7 节执行聚焦与完整回归、自审 diff；只根据本次新鲜结果报告通过情况。
- [x] 交付代码改动摘要、复现修复前后对照、准确测试数字、遗留限制；不自动 commit/push。

## 6. 必须覆盖的验收矩阵

| 编号 | 场景 | 核心断言 |
|---|---|---|
| A1 | text → finish | 第二次业务响应结束；无多余读文件；无验证要求时沿用正常完成语义 |
| A2 | text → text → text → 未消费的响应 | 恰好 3 次业务请求；ok=False；专项原因；不是 max_rounds |
| A3 | text → read → text → list → text | 5 次业务请求、2 次工具执行；累计不清零；第 6 次请求不发生 |
| A4 | text → 有效写入 → text → 测试通过 → text | 有效工具也不重置累计预算；提前停止仍保留实际修改与验证事实 |
| A5 | 新任务与两个实例隔离 | 新任务从 0 起；A 的错误次数不影响 B；计数不落会话库 |
| A6 | finish 携带“测试已通过”，本地实际失败/待验证 | 不伪造证据、不清除失败、不把模型声明变成业务成功 |
| A7 | 只读正常结束、纯目录任务结束 | 不被新增规则强迫执行测试；保留既有目录任务语义 |
| A8 | finish 后同批写入 | 写入不执行；skipped 配对完整；无孤立 call/result |
| A9 | 专项停止前已经写文件/目录 | 文件不自动回滚，账本和效果保留，锁释放，无额外写入 |
| A10 | 取消、UNKNOWN、审计/清理失败 | 现有优先级和安全状态保留；不因专项计数报告成功 |
| A11 | 空响应、finish_reason=length、流未完成 | 不自动成功；已在 Provider 层拒绝的响应不冒充协议层纠错 |
| A12 | 重复 call ID、legacy JSON 错误、普通工具 execution_failed | 不误记为 native 缺工具调用；原有错误恢复仍有效 |
| A13 | native/legacy 正常 finish，同步/异步入口 | 正常结束均不回归；新预算只作用于指定 native 错误 |
| A14 | max_rounds=2，text 两次 | 保持原来的总轮数停止；没有第 3 次请求 |
| A15 | usage、审计与 UI | 专项停止轮 usage 不丢失；审计只含元数据；用户看到准确停止原因 |

重点自审：有工具调用的 assistant 附带文本不计错；读取任意文件不能清零；无动作且审计写入失败时优先报告审计失败；截断响应不能被转成完成；新任务不得继承旧计数。分别由 A3、A10—A13、A5 验证。

## 7. 验证命令

在仓库根目录执行，使用既有环境，不安装依赖。以下 test_agent*.py 范围同时覆盖新专项测试。

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_protocols.py" -v
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_agent_termination.py" -v
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_agent*.py" -q
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_session_runtime.py" -q
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_tools.py" -q
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -q
git diff --check
```

聚焦红绿过程须可说明；全量通过后没有新变更无需反复重跑。基线已有失败必须单独说明，不能偷偷放宽断言。真实模型是否更稳定调用 finish 需要后续人工实测，本轮 FakeProvider 测试只能证明框架的纠错和停止机制。

## 8. 可直接交付 coding session 的提示词

```text
请实施以下文档，不要只给建议：
D:\MaHong\AGENT_WORKSPACE_V2\projects\tricoder-cli\docs\superpowers\plans\2026-10-05-react-termination-recovery.md

目标：修复模型反复输出完成总结，却被“没有工具调用”反馈推回 read_file/list_files，直到耗尽 30 轮的问题。

先读工作区与项目 AGENTS.md、README.md、pyproject.toml、project.md 和整份计划，核对最新工作树。仓库有目录工具第一轮的未提交修改；保留它们，不能从 HEAD 覆盖，也不要重做目录工具。

按 S0—S4 小步实施并验证：
1. 明确 finish 是结束动作，完善提示词、工具描述和无工具响应反馈。
2. 原生无工具响应在单任务内累计计数，前两次纠错，第三次可靠停止为未完成。普通工具成功和写入都不清零；新任务重置。
3. 保留现有 finish 本地验证路径，不能根据“完成”文本或 finish_reason=stop 自动成功。
4. 保留真实文件修改、账本、验证状态、取消、审计与锁释放；不能为了止循环自动撤销或执行无关测试。
5. 用确定性 FakeProvider 和临时工作区完成第 6 节回归，运行聚焦及完整测试，更新 README 和 project.md。

范围外：记忆默认开关、通用无进展算法、命令放权、桌面游戏修复、工作区误导文案及 Provider tool_choice 重构。不要安装依赖、读取 .env.local/真实会话库、调用真实模型、自动提交或推送。

遇到当前代码与计划不一致，先核实符号和边界，记录必要的小幅适配；只有会改变范围或安全语义的问题才停下来说明。最终交付：改动文件与行为、修复前后复现、准确测试结果、遗留限制。不要声称 FakeProvider 回归证明了真实模型调用成功率。
```

## 9. 当前交接状态

- 2026-10-05：S0—S4 已按本文件实施；代码事实和 RED/GREEN 过程见
  `runtime/react-termination-recovery/progress.md`。
- 修复前新增复现证明连续第三次原生无工具响应后仍消费第 4 个 `finish` 并错误成功；
  修复后累计预算、明确反馈、固定审计原因、可信收尾与锁释放均有确定性测试。
- 新鲜完整回归：Windows / Python 3.11.6，1440 项通过，13 项条件跳过；没有调用
  真实 Provider、读取真实密钥/会话库、安装依赖、提交或推送。
- 已知后续事项：一般修复循环的无进展检测、收尾基线提示分类、文件修改统计口径、业务测试覆盖；本轮不顺带实施。
