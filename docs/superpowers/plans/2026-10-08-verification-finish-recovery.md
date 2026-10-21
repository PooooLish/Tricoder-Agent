# TriCoder 验证反馈与收尾纠正执行计划

> 执行会话：使用 superpowers:executing-plans 按步骤实施。用户指定由 coding session 执行。项目规则优先，保留现有未提交修改，不自动安装依赖、提交、推送或调用真实模型。

**Goal：** 让 Agent 能理解脚本运行与可信验证的区别，并在首次过早请求完成时获得一次补充验证机会。
**Architecture：** 澄清系统与工具说明；由宿主在工具执行后提供验证状态；将现有 finish 判定提取为共用的收尾检查，在闭合任务前决定完成、有限纠正或按原规则停止。
**Tech Stack：** 当前 Python、unittest 和 Agent/ToolBatch/VerificationScope，不新增框架、依赖或持久化表。
**Spec：** 本文第 1—5 节为需求和状态规则，第 6 节为实施步骤。
**状态：** T0—T4 实现完成，待复审。最终串行全量 1678 项、13 skipped、0 failures/errors；只执行离线验证，无新真实模型成绩。

项目根目录：`D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli`。下文仓库相对路径均相对此目录。

## 1. 背景与边界

用户提供的三家 API 实测中，q-nested-create 均生成正确文件并成功执行脚本，隐藏 verifier 确认产物正确，但因没有可信验证证据而以 agent_unverified 结束。当前代码与该归因吻合：

- `src/tricoder/protocols.py` 的 COMMON_SYSTEM_PROMPT 将 script/syntax/static/tests 并列，未解释解除文件验证义务所需证据。
- `src/tricoder/tools/command.py` 的 RunCommandTool.description 未明确普通脚本不能清除修改后的义务。
- `src/tricoder/protocols.py` 的 `_tool_result_content` 主要回填 ok/output/error，模型难以看见当前宿主义务。
- `src/tricoder/engine/tool_batch.py` 执行 finish 后回填结果、跳过批次剩余动作，返回 FINISH。
- `src/tricoder/engine/loop.py` 收到 FINISH 后直接调用 `_finish_success`，检查证据，随后 close_task、生成记忆候选、finalizer.finish；缺少补救分支。

本期只解决提示可理解性、状态反馈及一次收尾纠正。三家各一次结果只是冒烟证据，不足以证明整个模型或 Agent 的质量水平。

必须保留：
1. 普通脚本退出码 0 不签发修改验证证据。
2. 测试/语法/静态检查必须经过当前命令策略、有效执行、覆盖范围和快照校验；不能仅靠命令名字或输出文本通过。
3. compileall 只证明语法检查，不证明需求满足、功能正确或执行安全。
4. 隐藏 verifier 只评分产物，不能回写生产 Agent 状态或补签证据。
5. 审查、问答没有适用修改义务时，不为了结束任务强制运行测试。
6. 原先继承的义务与 legacy_unknown 不能因当前任务只读而被清除。

## 2. 提示及工具说明

同时调整 COMMON_SYSTEM_PROMPT、RunCommandTool.description、FinishTool.description；原生与 legacy JSON 协议要求一致。

建议系统说明：

> 普通脚本执行只记录运行结果，不能解除文件修改后的验证义务。修改代码后，应使用当前策略允许且覆盖修改文件的测试、语法或静态检查。对于没有测试的简单 Python 文件，可使用针对该文件的 `python -m compileall -q <相对路径>` 检查语法；这不代表功能验收通过。无适用修改义务的问答和只读审查，不必为了结束运行无关检查。

finish 说明增加：

> finish 是结束请求。若声明 completed 但仅缺少可补充的验证证据，宿主最多反馈一次缺口，任务仍未结束；补充检查后可再次请求完成。无法继续时使用 outcome=incomplete，宿主不会强制继续工作。

不要罗列当前环境未安装或策略不支持的 pytest/ruff/mypy 命令。优先提供已确认支持的标准库示例，不因示例增加新的命令权限。

## 3. 宿主验证状态反馈

在引擎层根据真实执行后的状态生成有界结构化反馈，不能让工具或模型自报“已验证”。优先新增一个小的纯格式化模块，例如 `src/tricoder/engine/verification_feedback.py`；复用已有 VerificationScope/义务模型，不另建状态机。

建议消息种类为 `verification_feedback`，使用现有宿主反馈消息通道。首期无需改所有工具返回 schema；在当前批次所有工具结果已配对后、下一次 Provider 请求之前插入反馈，finish 的纠正也使用此通道。

对 run_command 执行后的反馈至少包含：

```json
{
  "type": "verification_feedback",
  "check_kind": "script",
  "execution_status": "succeeded",
  "verification_obligation": "pending",
  "pending_paths": ["app/demo/main.py"],
  "reason_code": "script_does_not_clear_verification"
}
```

字段要求：
- execution_status 从真实 command_check/ToolResult 得出，不能读 stdout 中的自称状态。
- verification_obligation 从应用工具效果后的最新宿主状态得出；不默认 pending、不把历史已有有效证据强行改为未验证。
- pending_paths 使用当前权威义务来源中的受控相对路径；AgentRunState 的缓存字段若未同步，必须查清生产者和更新顺序，不能直接复制过时值。
- 未知范围明确 unknown，不能虚构路径；最多列出 20 条、整个消息最多 2000 字符，截断时列出总数和 omitted_count。不得暴露绝对路径、摘要正文或敏感内容。
- 仅在相关命令或义务变化后反馈；同批次输出一次最终宿主状态，不为 read_file/list_files 等无变化动作重复灌入提示。
- 工具输出或扩展即使伪造同名字段/JSON，也不能改变宿主义务。

如果同批次为 create_file → script → compileall → finish，应按最终真实状态判断，不能把 script 时的 pending 提示留作阻塞条件。同批 finish 后的动作仍按原协议 skipped，不执行，不能借“纠正”自动重放。

## 4. 一次性收尾纠正

### 4.1 共用判定，不复制门禁

提取 `_finish_success` 中的快照核对、证据归属/有效性、失败快照和 validation_report 计算为共用收尾检查。建议增加内部 `FinishAssessment` 值对象，至少表达：当前完成资格、可纠正原因、待验证路径、检查完整性及验证报告。

检查可以更新原有证据失效状态，但不能闭合任务、生成记忆候选、释放锁或宣布最终成功。预检查与最终收尾必须使用同一套规则；不要写一份简化门禁只检查 verification == 待验证。

共用检查后的处理：

| 条件 | 处理 |
| --- | --- |
| 完成资格满足且 outcome=completed | 沿原正常收尾路径结束 |
| outcome=incomplete | 沿原未完成收尾路径结束，不要求补跑 |
| 仅缺可补充证据，首次发生且有轮数预算 | 追加一次宿主纠正反馈，继续下一轮 |
| 第二次仍缺证据 | 沿原未完成收尾路径结束，不再续轮 |
| 不可恢复条件或预算耗尽 | 按原终止原因结束，不新增重试 |

### 4.2 可纠正条件必须全部满足

1. finish 声明 completed，调用参数有效，所属工具批次已完整配对且审计成功。
2. 存在适用修改义务，变化范围已知，当前扫描完整且文件副作用可确认。
3. 唯一缺口是尚无覆盖该范围的有效认可证据，例如只跑了脚本、查版本、跑了无关测试或明确 0 个测试。
4. 没有当前未解决的验证失败、stale/外部未知变更、legacy_unknown、UNKNOWN/tainted 状态、取消、审批拒绝、审计错误、进程或清理异常。
5. 尚未用过本任务纠正机会，且至少剩余一次普通执行轮次。即使只剩一轮也不扩充预算，不承诺可以完成。

状态模型不能区分“尚无证据”和“失效/未知证据”时，补最小可信原因区分或保守不恢复，不一概把所有 invalid 变成可恢复。继承的已知 pending 义务保持现有语义；无法确认来源/范围时不能自动给命令建议或消除义务。

本期不自动恢复明确测试失败，也不处理策略拒绝后换命令绕行。模型在正常循环遇到测试失败如何重规划，继续走原规则。

### 4.3 计数、历史与审计

- 在 AgentRunState 新增任务级计数 `finish_verification_recoveries`，默认 0，最多 1。读文件、工具成功、压缩或改写文本都不能重置；新用户任务重新初始化，不跨任务继承。
- 首次纠正仍在原任务 ID 和原预算内；不重建 Agent/Runtime，不额外开隐藏 Provider 请求。
- finish 工具返回成功仅表示结束请求已收到；纠正反馈明确“任务尚未结束”，不能同时展示最终完成卡片。不要将先前工具结果改写成另一个结果或为同一 tool_call_id 追加第二条 tool result。
- 纠正发生前不得调用 close_task、推进最终任务覆盖位置、生成终结记忆候选、释放工作区/会话锁。正常运行时的既有压缩可继续，但必须保留本轮必要反馈且不重置计数。
- 最终终止才执行一次原收尾流程；后续新命令/写入后重新检查当前快照，不能复用第一次 FinishAssessment 当作新证据。
- 审计记录 `finish_verification_recovery`、固定原因、次数/上限和受控路径；第二次不满足记录最终拒绝原因。日志/反馈写入过程中取消或审计失败仍优先停止。

建议纠正文本：

> 当前尚不能确认完成：app/demo/main.py 修改后的验证义务未解除。此前脚本运行成功，但未提供认可的验证证据。请执行当前策略允许且覆盖该文件的检查；若无法继续，请使用 finish(outcome=incomplete) 说明原因。补充检查机会已使用 1/1。

反馈只是说明缺口，不批准或自动执行命令。动态路径若用于命令展示必须安全引用；首期可以只给通用命令模板，避免拼接不安全字符串。

## 5. 评测与兼容原则

- 本期不修改 q-nested-create 的用户任务、fixture、隐藏 verifier 和评分期望，不把“必须运行 compileall”写入题目来提高成绩。
- 保留此前三次 API 结果，不覆盖报告。当前只代表三家各一次冒烟，不标成完整 baseline。
- 默认只做离线实现和回归。新一轮真实调用需用户另行明确授权模型及预算；不得默认复用旧回合的付费测试授权。
- 建议真实对照分为提示/反馈修改后、加收尾纠正后两阶段；固定模型完整 ID、题库、记忆和权限参数，比较成功率、纠正触发/成功次数、轮数与 token。模型服务波动单列。
- 代码指纹变化使用现有 compare 的受控变量机制；提示与工具定义的变更应能通过代码/配置指纹识别。若比较器因证据版本变化拒绝比较，明确说明，不强行删字段。
- legacy_json 与原生协议都经过同一收尾资格检查；不得只修某一家 Provider。

## 6. 分步实施

### T0 固化失败与既有约束

文件：`tests/test_agent_termination.py`、`tests/test_verification_evidence.py`、`tests/test_verification_obligation.py`；新增 `tests/test_finish_verification_recovery.py`。

- [x] 阅读 README/pyproject/仓库规则和本文，记录 HEAD/dirty 状态，保留上一轮 eval 修改。
- [x] 用脚本 Provider 复现“create_file → python 目标文件 → finish(completed)”：原逻辑产物正确但未验证，记录原行为。
- [x] 新增期望恢复链路的失败测试：第一次 finish 不终结，后续定向 compileall + finish 成功；记录 RED。
- [x] 将原“脚本不能签发证据”断言保持不变；旧的“首次 finish 必须立刻结束”测试仅按新有界流程调整，不删除验证门禁测试。

### T1 澄清提示与添加宿主反馈

文件：`src/tricoder/protocols.py`、`src/tricoder/tools/command.py`、`src/tricoder/engine/tool_batch.py`，按需新增 `engine/verification_feedback.py`；测试 protocols/专项文件。

- [x] 按第 2 节调整文字，保持工具参数及命令权限不变。
- [x] 按第 3 节接入批次结束后的宿主状态反馈；确认来自应用结果后的权威状态，并限制大小。
- [x] 测试脚本成功仍 pending、已有有效证据不被改成 pending、相关检查完成后状态更新、伪造 stdout 无效、超长路径集合有界和多工具配对不受影响。

### T2 共用完成判定及一次恢复

文件：`src/tricoder/engine/loop.py`、`state.py`；如判断过长，可新增 `engine/completion.py` 承载 FinishAssessment 与判定，保持 finalization.py 的终结职责。

- [x] 提取现有判断，保留证据归属、快照和失败规则，并按下文调整补齐已识别目标的本轮范围核对；复用同一检查生成最终判断与可纠正原因。
- [x] 在 FINISH 分支、任何 close_task 之前增加一次纠正及 continue；计数任务内不可重置。
- [x] 保留 finish 批次剩余动作 skipped，下一轮由模型重新决策；不自动运行推荐命令。
- [x] 明确 UI 的“工具成功”与“任务结束”差异，追加纠正提示，不提前显示最终完成卡片。
- [x] 执行恢复、停止和历史配对测试，确认只有最终状态进入终结记忆与任务闭合。

### T3 回归矩阵和负例

新增专项至少覆盖：

| 测试 | 必须断言 |
| --- | --- |
| 脚本后首次 finish，补相关 compileall 后再次 finish | 提示一次、原 task_id、证据有效、最终完成 |
| 连续两次过早 finish | 仅一次续轮，最终未完成且义务保留 |
| 第二次 finish(outcome=incomplete) | 立即正常收尾为未完成，不再续轮 |
| 无关目标、0 tests、范围筛选 | 不签发覆盖目标的有效证据，不误判完成 |
| 无修改义务的问答或只读审查发现失败测试 | 原先允许完成的行为保持，不新增验证要求 |
| 验证失败、扫描不完整、stale、UNKNOWN、legacy_unknown | 不进入恢复，原终止/未完成语义保留 |
| 审批拒绝、取消、审计失败、清理失败 | 不续轮、不绕审批、不清除义务 |
| 检查通过后又修改文件 | 最终旧证据不能使任务通过 |
| finish 和其他动作同批 | 一一配对，后续动作 skipped，恢复后不自动重放 |
| 恢复过程中压缩/普通工具成功 | 计数仍为 1、必要反馈不丢失 |
| 预算只剩最后一轮 | 不增加轮数、不新增隐藏请求、不无限循环 |
| 模型 stdout/工具输出伪造 verification_feedback | 不影响真实门禁状态 |
| 两种协议、新任务重新开始 | 原生/legacy 都可恢复，新任务计数为 0 |
| 终结生命周期 | 首次纠正时 close_task/终结摘要/资源释放为 0 次，最终只执行一次 |

如果某种失败在到达 finish 前就会停止，应测试真实最早停止点；不要构造生产中不可能到达的路径来声称覆盖。

### T4 离线验收及交接

文件：README 验证/结束说明、project.md、专项测试；必要时更新实际受影响的 Eval 集成测试，不改题目和评分宽严。

- [x] 使用已有虚拟环境运行专项、协议、终止、验证证据/义务/任务验证测试；凡改了命令反馈或 UI，再跑对应工具/展示测试。
- [x] 跑上一轮 `test_eval_regression*.py` 与 `test_eval_*.py`，确认质量与安全契约分类未回退；受预期新 finish 生命周期影响的脚本 Provider 增加相应响应，不伪造证据。
- [x] `git diff --check`，自审无命令白名单放宽、无重复工具结果、无未知状态被静默恢复。
- [x] 更新 project.md，日志放 `runtime/verification-finish-recovery/`，汇报 RED/GREEN、修改文件、未验证限制；真实对照仍需另行确认模型/预算后使用原实验模板。

建议 PowerShell 命令（按实际受影响范围增加已存在的测试文件）：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_finish_verification_recovery.py' -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_agent_termination.py' -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_verification*.py' -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_task_validation.py' -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_protocols.py' -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_eval_*.py' -v
git diff --check
```

## 7. Review Focus 与完成标准

重点检查五项：证据缺失与失效混淆（T2/T3）；恢复计数被压缩重置（T3）；工具批次配对和 skipped 被破坏（T1/T3）；首次 finish 提前闭合记忆/释放锁（T2/T3）；只读任务和历史义务被一刀切重解释（T0/T3）。

完成标准：离线恢复链路可复现并通过，全部停止边界保持，原有效验证仍有效，普通脚本仍不能签发证据，旧评测题和产物评分未改。仅离线通过时应表述为“恢复机制验证通过，真实模型提升待测”。

### 2026-10-08 实施核对与设计调整

- `engine/completion.py` 承担统一 `FinishAssessment`，Runner 只在执行结果已配对、审计成功后调用，最终收尾直接消费同一评估。`verification_feedback.py` 与完成判断复用检查范围逻辑，避免无关检查被提示为 satisfied。
- 当前 `AgentRunState.pending_verification_paths` 是进入任务时的 Session 状态，不能代表本轮全部新增路径。因此新增只驻本任务的已确认文件/目录路径集合，由工具批次应用真实 `FileEffects` 时更新；计数也只存于该任务。
- 现有通过 capability 证明受覆盖文件快照稳定，并不能单独说明检查目标覆盖当前修改。统一判定同时核对本轮签发的检查记录、有效 cwd/targets、快照、零测试及 scope_filtered；针对测试文件的命令不自动推断 import 的源码覆盖。既有相关测试改用工作区级发现或明确 incomplete，保留失败/义务断言。
- 离线 Eval 发现既有 `python -m unittest -q` 的隐式发现被记录为空目标。先补真实子进程 RED 后，按该命令实际行为补记有效 cwd 的 `.`；不放宽命令策略、零测试或筛选规则。Eval 的多轮记忆计数测试给增长的固定提示前缀增加预算，仅保持原来的两次收尾摘要目标，未修改题库预算。
- `verification_feedback` 使用既有宿主 user 消息通道，历史分组允许独立反馈但仍拒绝未配对工具；摘要提示排除此类执行反馈。Console/TUI 复用纠错通知显示“本轮尚未结束”，不发布提前终态。
- 原 `q-nested-create` 的离线工具链对照：创建嵌套文件→运行脚本→首次 finish→定向目录 compileall→最终 finish，Agent 已通过且隐藏产物正确；compileall 产生的字节码缓存仍被原评分器判为 `change_out_of_scope`。正式测试保留该失败条件，题目、fixture、隐藏 verifier、评分均不变。这是后续需单独决策的 Eval 范围限制，不属于本轮放宽范围。
- 本轮无数据库或公开 CLI 参数变更。证据与最终数量以 `runtime/verification-finish-recovery/verification.md` 为准；未验证真实 Provider、真实用户库、Linux/macOS 或 Python 3.12。

## 8. 交付提示词

### 2026-10-08 F1/F2 覆盖补齐

- 独立探针复现后补正式 RED，修复创建父目录后的定向文件验证与多条检查覆盖并集。
- 共用覆盖计算排除本轮已确认文件的父目录检查要求，但保留目录账本、身份和最终快照；独立目录仍保守维持原要求。检查并集仅来自当前任务、当前完整快照、未失效的认可成功记录，不接受失败/零测试/筛选记录。
- 批次反馈、最终判定与收尾纠正反馈使用同一剩余目标；不放宽命令、安全停止、一次纠正和轮数预算。Runtime 的历史目录 pending 口径不在本轮变更范围，不能把当前源码验证通过称为已统一跨任务全部目录义务。
- 本轮证据单独保存在 `runtime/verification-finish-recovery/review-fixes/verification.md`，原独立复审记录保留。原 q-nested-create 缓存评分限制仍未解决。

```text
请在 D:\MaHong\AGENT_WORKSPACE_V2\projects\tricoder-cli 中实施：
docs/superpowers/plans/2026-10-08-verification-finish-recovery.md

先读仓库规则、README.md、pyproject.toml、project.md 和上述计划，核对当前代码，保留上一轮 Eval 及其他未提交修改。

本轮只做三件事：澄清普通脚本与可信验证的差异；提供来自宿主的验证义务反馈；在首次 finish(completed) 仅缺可补充验证证据时允许一次继续执行。按 T0—T4 小步实施，先固定失败测试，再最小修改与验证。

必须保留：普通脚本成功不签发验证证据；compileall 仅证明语法；隐藏 verifier 不回写 Agent 成功；审批拒绝、取消、未知副作用、扫描/审计/清理失败不恢复；无适用修改义务的只读任务不强制测试。finish(incomplete) 直接结束。

恢复必须发生在 close_task、终结记忆生成和资源释放之前，同一任务最多一次，仍受原预算约束。复用同一套收尾检查，不复制简化门禁；工具调用结果一一配对，finish 后本批动作保持 skipped，不自动执行或重放命令。

允许完成计划内代码、文档修改和离线测试。不读取 .env.local、真实凭据或用户库，不安装依赖、不调用真实模型、不提交或推送。不修改 q-nested-create 的任务、fixture、隐藏 verifier 或评分要求来提高通过率。

完成后更新 project.md，提供修改清单、RED/GREEN 与回归结果、一次恢复及正确停止的证据、未验证限制。日志放 runtime/verification-finish-recovery/。真实三家模型对照等待用户单独授权，不能宣称离线通过代表模型效果已经提升。
```
