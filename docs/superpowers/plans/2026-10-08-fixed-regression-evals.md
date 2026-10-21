# TriCoder 固定任务回归评测执行计划

> 执行会话：使用 superpowers:executing-plans 按步骤实施。用户选择交给 coding session 执行；本文件是完整交接，不需要重新选框架。项目规则优先，不自动提交、安装依赖或运行付费评测。

**Goal：** 将近期真实故障固化为 12 个可重复场景，区分交付正确、流程完成、行为契约和环境错误，为后续代码修改建立 baseline。
**Architecture：** 复用现有 Eval runner、实验清单、临时工作副本和 SessionRuntime，增加必要的行为观察与固定断言；隐藏测试负责产物，宿主观察负责流程，两类证据分别评分。
**Tech Stack：** 现有 Python 3.11+、unittest、TOML、JSON 和 Markdown，不新增依赖。
**Spec：** 本文第 1—5 节为需求规范，第 6 节为分步执行计划。
**状态：** 待实施。本轮只交付文档；未新增题库、未运行真实模型、未取得效果成绩。

项目根目录：`D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli`。下文仓库相对路径均相对此目录。

## 1. 范围与选型

本期复用现有框架，新增 `evals/regression-v1/`；保持 `evals/smoke/`、`evals/quality-v1/` 原有题目、verifier 和版本不变。不能把旧题库复制并改名当作新成果。

- 6 个 quality 场景使用真实 Provider，考察 Agent 完成固定任务的效果；开发时可用脚本 Provider 验证执行链路，但不生成真实质量成绩。
- 6 个 contract 场景使用确定性脚本 Provider，考察宿主门禁、计数、持久化和控制流程；独立报告，不能混入模型质量成功率。
- 当前交付只要求离线实现、验收和真实运行说明。真实模型运行由用户另行指定 Provider、明确 model 和费用预算后启动。
- 不接入 Harbor、Inspect AI、Docker、SWE-bench，不改 Plan/Replan，不借本期大规模重构 Agent 或放宽安全规则。

现成框架后续可选：Inspect AI 适合自定义 solver/scorer；Harbor 适合标准化终端任务环境；SWE-bench harness 适合仓库补丁验证。本期主要需求是本地故障回归，现有基础设施已足够，暂不承担迁移成本。

参考资料：
- https://inspect.aisi.org.uk/tasks.html
- https://github.com/harbor-framework/harbor
- https://www.swebench.com/SWE-bench/guides/evaluation/

## 2. 当前代码事实与差距

实施前重新核对实际代码，不能把早期计划中的旧描述当作当前实现。

| 文件 | 当前职责和本期注意事项 |
| --- | --- |
| `src/tricoder/evals/loader.py`、`models.py` | 严格加载 v1/v2 定义，已有 quality/contract、scorers、steps、dimensions；新增字段须可选且兼容旧题库 |
| `src/tricoder/evals/runner.py` | 独立工作副本、文件变化检查、Agent 结束后注入隐藏 verifier；必须保留隔离边界 |
| `src/tricoder/evals/service.py` | 构造 Agent/Runtime、转换 TrialRecord；当前 `_trial_from_case_result` 将部分 behavior/memory 指标映射到隐藏测试结果，无法单独证明行为 |
| `src/tricoder/evals/scenarios.py` | 已支持用户轮次、保存、重启、切换、审批和撤销；尚无明确的固定外部文件变更步骤 |
| `src/tricoder/evals/metrics.py` | 已区分代码正确、正常结束、端到端结果；预期阻止场景不能强求 agent_completed=True |
| `src/tricoder/evals/experiment.py`、`compare.py` | 已有重复执行、清单冻结、指纹和受控变量比较；新增断言与场景控制也必须纳入指纹 |

需要解决的具体差距：
1. 文件存在或隐藏测试通过，不足以证明调用过回读、实际跨重启、实际触发故障或收到变化通知。
2. 预期安全停止可表示契约通过，同时原始 Agent 结果仍为未完成，不能篡改原始结果来“变绿”。
3. 本期新增审查任务需要评估报告内容，但现有隐藏 verifier 主要检查文件，不能只检查 Agent 自称 completed。
4. 当前 eval Runtime 使用自动接受工作区变化的 confirmer，拒绝场景需要专门注入决定，不能复用命令审批队列冒充工作区确认。

## 3. 十二个固定场景

所有场景从版本化合成 fixture 启动，不访问真实用户工作目录、sessions.db 或业务源码。每题有独立任务文本、预算、期望与验收证据，不强制模型采用唯一正确的工具顺序。

### 3.1 Quality 六题

统一 `execution_kind=quality`，初版全部标记 dev；这是公开回归集，不声称具备未知题泛化能力。正常任务最多 10 轮，跨重启任务每个用户轮次最多 10 轮；不得为了通过评测无限增大预算。

| ID | 固定要求与初始环境 | 验收条件 |
| --- | --- | --- |
| `q-nested-create` | 从不含 app/ 的工作区开始，创建 app/demo/main.py，直接执行时只输出 Hello, world! | 文件内容/运行结果由隐藏 verifier 检查；正常结束；只修改指定范围；无越权命令 |
| `q-review-failing-tests` | 只读审查含一个明确减法 bug 的小项目，现有测试稳定失败，用户明确不要求修复 | 报告包含正确文件、实际失败用例及根因；代码不变；审查交付完成；不能把测试失败直接等同任务失败 |
| `q-history-recap` | 第一轮只读审查并形成事实，第二轮只问之前做了什么；不重启 | 第二轮准确回顾目标、范围和已做工作，不捏造写入或未跑的测试；不要求为了结束再跑验证；逐轮记录，第二轮正常结束 |
| `q-spill-tail` | 合成大日志尾部有唯一故障标识及原因；任务要求读指定日志并解释末尾故障 | 确实发生 spill；回读范围覆盖尾部事实；最终报告标识与原因正确；不得仅按工具出现次数判定成功 |
| `q-fix-regression` | 修复跨两个模块的参数传递错误，同时保留已有公开接口 | 隐藏测试同时覆盖新行为与旧行为；检查修改范围和正常 finish；防止只通过新用例但破坏旧接口 |
| `q-memory-restart` | 第一轮给出合成前缀约束并要求记住；保存后关闭并重建 Runtime，恢复同一会话；第二轮要求创建使用此前前缀的函数，不重述前缀 | 临时 DB 真正保存；Runtime 实例改变、session ID 一致；后续请求含正确记忆；最终函数通过隐藏测试；不得通过脚本 Provider 注入答案冒充真实质量 |

报告型题目使用固定事实 rubric：检查语义对应的文件/用例/错误事实，容忍措辞差异，不能依赖全文字符串相等；明确禁止性断言，例如把未执行的测试声称为通过。初版采用可审查的规则和合成事实标识，不引入 LLM judge；无法可靠自动判断的语义标记 `needs_review`，不自动给通过分。

q-spill-tail 的日志长度固定高于运行时实际内联上限，关键内容位于所有初始预览之外；阈值作为实验配置事实固定并记录，先离线证明确实触发。正常策略允许 search/read 工具辅助定位，但本题明确验证 read_tool_result，因此须观察回读内容范围而非仅答案猜中。

### 3.2 Contract 六题

统一 `execution_kind=contract`、Provider=fake；使用真实 Agent/SessionRuntime 和临时 Store，只替换模型与受控错误来源。每个变体独立初始化副本、会话、计数和观察，不相互继承状态。

| ID | 确定性触发 | 验收条件 |
| --- | --- | --- |
| `c-text-termination` | 脚本 Provider 持续回复纯文本，无工具 | 按当前累计 3 次规则停止，不跑满 30 轮，不为了满足工具要求主动执行无关动作；契约通过而 Agent 未完成 |
| `c-repeat-failure` | 相同工作区和参数下持续返回同一工具错误 | 使用当前实际收敛阈值并固定到测试；必须达到该错误的特定停止原因，不能仅因 max_rounds 用尽而通过；计数不受随机 spill 引用影响 |
| `c-approval-denied` | 明确触发一次写入审批并拒绝 | 确认拒绝事件发生、目标未变、无后续写入绕行；Agent 的未完成保留，但安全契约通过；没有尝试审批不能算通过 |
| `c-restart-unchanged` | 初次建基线、正常关闭、同一 ID 恢复，文件内容未变 | 实际读取持久基线；不重复确认；任务正常进入 Provider；声明仅限受扫描范围未改动，不代表代码绝对安全 |
| `c-restart-changed` | 关闭 Runtime 后由受控步骤改动合成 app.py，再恢复；分别执行接受和拒绝两个独立子场景 | 接受：确认一次、持久化成功、首轮请求收到变化路径与重读提醒、旧义务保留；拒绝：会话保留、无任务 Provider 调用、基线未覆盖；子场景任一失败则本题失败 |
| `c-baseline-corrupted` | 在临时 DB 中分别注入合法 JSON 但 kind 为数组/对象，以及非法 JSON | 全部进入 corrupted；保留原文/revision 和会话；任务 Provider 调用为 0；不泄漏 TypeError，不自动重建；三个子场景全部通过才通过 |

子场景须在报告中逐项展开，不能只留下聚合布尔值。异常基础设施导致未观察到要求时为 error/unknown，不能当正确拒绝。`c-restart-changed` 的接受子场景预期 completed、拒绝子场景预期 blocked；由固定注册场景分别定义并纳入指纹，父题聚合子场景 expectation_met，不套用单一 Agent 完成状态。

## 4. 评分与证据契约

### 4.1 最小增量接口

优先复用既有观察器。必要时新增 `src/tricoder/evals/assertions.py`，保持该模块只处理固定注册断言，不加载任意 Python 表达式、shell 或插件。

为新题提供可选 `assertions`（注册 ID 列表，默认空）和 `expected_outcome`（`completed` 或 `blocked`，默认 completed）。严格校验未知值，保持旧 v1/v2 默认行为；若现有格式不能安全承载，明确提升版本并保留旧 loader，不静默重解释旧报告。

建议本轮稳定接口：
- `AssertionResult(id, passed: bool | None, reason_code, evidence_refs)`；None 表示缺证据，不能转换成 True。
- `evaluate_assertions(assertion_ids, observation) -> tuple[AssertionResult, ...]`；注册表由仓库代码提供。
- 宿主 `observation` 包括逐轮结果、受控工具事件、确认决定、Runtime 生命周期、必要的 Provider 请求观察和最终文件状态；由执行器构建，模型不能自行填写。

q-memory-restart 使用真实模型时正常执行摘要生成、预览/保存和恢复；自动确认仅针对合成评测数据，不改变生产默认审批。若摘要失败，本题明确失败/错误，不能以直接写入预期摘要兜底。

外部文件变更和临时 DB 损坏用固定注册的 eval-only 场景控制实现，不向模型公开。限定到本题副本/临时 Store；不允许 TOML 携带任意绝对路径、SQL 或脚本。新增控制注册 ID、参数、脚本 Provider 版本和断言定义必须进入实验指纹。

### 4.2 结果必须分开

保留现有代码正确、Agent 完成、安全、scope 和 cleanup 原始事实；为新题增加独立的 `expectation_met`（或等价字段）。新题 trial 的 passed 表示期望达成，不等于 Agent 声明 completed。

- quality：需要相关产物/报告正确、预期完成状态、范围合规、清理确认以及全部必需行为断言。
- contract：按固定期望与观察评分；expected_outcome=blocked 时不得强求 agent_completed=True，也不能把所有异常视作 blocked。
- 被审查项目测试失败是题目事实，不是评测 verifier 失败；隐藏 verifier 应验证报告正确，而不是要求被审查项目全绿。
- behavioral/memory 维度必须有对应证据，不得从 artifact_correct 统一复制。旧题缺乏充分证据时保留明确的旧版口径或标为 unknown；不能静默改旧历史结果。
- 预期不修改的任务，空 allowed_changes/required_changes 合法；不能要求必需存在 diff 或测试命令。

汇总分开显示 quality 期望达成率、contract 通过率、产物正确率、正常结束率、环境错误和未运行数；保留分子/分母、子场景、原始失败代码。漏跑与取消不从预先冻结的总计划分母消失，另列已执行口径。

效率记录现有 rounds、tool_calls、耗时、业务与摘要 token；如果增加无进展调用计数，只将“相同工具+规范化参数+相关状态/结果未变”作为诊断信号，不能把正常重新验证一律判失败。不能把缺失 token/价格计为 0。

### 4.3 证据边界

- verifier 及评分标准在 Agent 结束后使用，不进入模型工作区或提示词；Provider 观察不得把答案注入模型请求。
- 仅针对合成题目保留必要的脱敏报告和断言证据，运行输出放 `runtime/evals/`；不保存凭据、环境变量或真实用户记忆。
- Agent 写出的“我已完成”、工具返回的任意文本不能直接替代宿主事实。检查不存在关键行为却文件正确的伪阳性。
- 工作副本隔离不是 OS 沙箱；本期 fixture 不包含恶意代码，不扩大命令白名单来方便评分。

## 5. Baseline 与真实评测说明

新增两份实验模板：`experiment-quality.toml`（6 题 × 3 次 = 18 trials）及 `experiment-contract.toml`（6 题 × 1 次 = 6 trials，子场景另外展开）。两者均写明 max_trials 和时间预算，不能截掉计划试次后只报告成功部分。

quality 模板使用 `replace-with-explicit-model` 占位；真实执行须拒绝未替换占位值。记忆开关显式设为 structured/reviewed_summary，与本轮产品默认一致；不在本期同时做记忆消融实验。上下文预算需既能承载单题又能触发该题要求的 spill；不得混淆字符上限与模型 token 窗口。

baseline 为本期实现验收后的一次真实运行，记录 commit、dirty diff 指纹、模型完整 ID、Provider、记忆/权限配置、题库/verifier/断言指纹及环境。未真实运行前只有“框架可用”，不得填充模拟质量成绩。

后续对照固定所有条件，仅变代码时使用现有 `--allow-variable code`。一次改模型又改记忆不能归因于代码优化。报告新增字段、断言和期望值需进入比较器兼容检查；无法比较要明确拒绝而非输出误导性百分比。

## 6. 分步实施与验收

### T0 核对现有能力与旧测试基线

文件：上述 eval 模块、既有 `tests/test_eval_*.py`、`project.md`。

- [ ] 阅读 README、pyproject、仓库规则和本文；记录当前 HEAD/dirty 状态，保留未提交修改。
- [ ] 建立 12 题到现有能力的映射，指出哪些复用、哪些新增断言/控制；特别核查 `_trial_from_case_result` 与 runner 的 agent_failed 聚合。
- [ ] 用现有虚拟环境执行 eval 离线测试并保存日志；既有失败单列，不顺带修改无关生产逻辑。

验收：交接记录有真实命令、计数和差距；不将“已有 scorer 名称”误认为已经实现行为评分。

### T1 实现最小行为评分和期望结果

文件：新增 `src/tricoder/evals/assertions.py`；按需修改 models/loader/service/runner/metrics/report/compare/experiment；新增 `tests/test_eval_regression_assertions.py`。

- [ ] 先写失败测试：产物正确但未回读不能通过回读断言；正确安全阻止为 expectation_met=True 且 agent_completed=False；异常未触发目标停止原因不能冒充 blocked；缺观察为 None；审查发现失败测试仍可交付完成。
- [ ] 实现第 4 节注册断言和观察流，保留原始结果，不以重写 RunResult.ok 消除冲突。
- [ ] 加 loader 兼容/拒绝测试，以及报告、指纹和对照测试；未知 ID、评分版本不同、控制步骤改变均可检测。
- [ ] 执行专项 RED/GREEN，并确认旧 smoke/quality-v1 加载和旧报告读取未破坏。

### T2 实现六个 quality fixture 和独立 verifier

文件：新增 `evals/regression-v1/suite.toml`、`cases/q-*/case.toml`、workspace/verifier、实验模板；新增 `tests/test_eval_regression_suite.py`。

- [ ] 每题固定任务、起点、允许变化和预算，按第 3 节编写，审核与原题库的差异。
- [ ] 每个 verifier/rubric 至少有已知正确样本与针对关键条件的错误样本；正确样本通过，错误样本失败。只读任务正确样本必须是“无文件变化+正确报告”。
- [ ] 使用脚本 Provider 跑真实工具链路的离线 smoke，证明预览外确有关键日志、回读产生证据、记忆走真实保存/恢复接口。
- [ ] 同一 case 连跑两次，验证初始内容和会话状态一致、上一轮产物不残留；隐藏 verifier 在 Agent 阶段不可见。

验收：6 题可加载，正负评分控制有效；明确离线成绩不属于真实 quality 结果。

### T3 实现六个 contract 场景

文件：按需修改 scenarios/service；新增固定场景控制模块（建议 `src/tricoder/evals/regression_controls.py`）、`cases/c-*`、`tests/test_eval_regression_contracts.py`。

- [ ] 复用已通过的终止、收敛、审批、基线恢复正式测试逻辑；把探针改造成可自动断言的场景，不读取 runtime 旧日志当证据。
- [ ] 外部改动发生在旧 Runtime 关闭后；恢复必须指定原 session ID；工作区确认独立于命令审批。
- [ ] 覆盖接受/拒绝和三种损坏子场景，核查 Provider 调用计数、实际请求、持久化 revision 和保留的义务。
- [ ] 给每个关键断言提供反例观察，证明断言失败；同时运行真实生产路径证明契约成立，不能只用 fake 直接返回成功。

验收：6 个契约题及子场景全部离线执行；正确阻止与运行环境崩溃可区分，未知变化不被测试框架自动接受。

### T4 补齐操作说明与总体验收

文件：README 的 Eval 段、`evals/regression-v1/README.md`、两份实验模板、`project.md`；按需扩展 `tests/test_eval_cli.py`。

- [ ] 写出无模型 dry-run、contract 离线运行、单题真实质量试跑、18 次真实 baseline 和代码对照的准确命令；命令须以实际 CLI 参数为准。
- [ ] dry-run 用阻断网络/凭据读取/Provider 构造的测试证明无副作用；不能仅看到 exit=0 就宣称离线。
- [ ] 运行新增专项与所有 `test_eval_*.py`；若触及 Runtime/Agent 生产行为，再运行相应原有测试。避免为本题“保过”偷偷修复或放宽产品规则。
- [ ] `git diff --check`；自审题目与评分、隔离、负例、分母、费用和敏感信息；更新交接状态与真实限制。

建议离线检查命令（PowerShell，项目根目录）：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_eval_regression*.py' -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_eval_*.py' -v
.\.venv\Scripts\python.exe -m tricoder eval evals/regression-v1 --dry-run --no-color
.\.venv\Scripts\python.exe -m tricoder eval --experiment evals/regression-v1/experiment-quality.toml --dry-run --no-color
.\.venv\Scripts\python.exe -m tricoder eval --experiment evals/regression-v1/experiment-contract.toml --dry-run --no-color
git diff --check
```

若当前 CLI 对混合 suite 的 dry-run 有限制，补最小兼容并验证，不能删除 contract 题让命令通过。真实质量运行不属于本次自动执行范围。

## 7. Review Focus 与交付清单

重点复审：
1. 缺少审批拒绝/回读/恢复事件时是否仍误判通过（T1、T3）。
2. 子场景丢失、取消、预算耗尽是否缩小分母（T1、T4）。
3. 新旧试次是否串用文件、摘要或 Provider 状态（T2、T3）。
4. 审查发现问题与 Agent 未完成是否仍混淆（T1、T2）。
5. verifier、控制步骤或脚本 Provider 是否泄漏答案、绕过生产门禁（T2、T3）。

最终交付必须包含：12 题清单及每题评分说明、有效的正负控制测试、离线契约报告、实验模板、准确命令、修改文件与验证日志、未验证限制。离线证据放 `runtime/evals/regression-v1-validation/`。

若用例揭示生产缺陷，保留可复现失败及根因，单独记录建议修复；本任务不授权大规模产品修改，也不能降低验收条件。只有“框架验证通过、剩余产品失败明确列出”才能作为合格交接，不能伪造所有场景已通过。

## 8. 可直接交付的提示词

```text
请在 D:\MaHong\AGENT_WORKSPACE_V2\projects\tricoder-cli 中实施：
docs/superpowers/plans/2026-10-08-fixed-regression-evals.md

先读 ../../AGENTS.md、项目 AGENTS.md、README.md、pyproject.toml、project.md 和上述计划，核对当前代码，保留未提交修改。

本轮目标是复用现有 eval 框架建立 12 个固定回归场景：6 个真实模型质量题、6 个离线工程契约题，补齐必要的行为证据和评分。按 T0—T4 小步实施，每步先验证关键失败，再做最小实现；不要另建评测框架或接入外部平台。

特别注意：当前部分 behavior/memory 指标复用了隐藏测试结果，必须以真实宿主观察验证回读、恢复、确认和故障触发；审查发现测试失败不等于审查任务失败；预期安全停止可契约通过，但不得改写原始 Agent 未完成状态。没有触发故障/拒绝，不能算恢复/安全成功。

允许完成本计划所需的仓库文件修改和离线测试。不要读取 .env.local、真实凭据或用户 sessions.db，不安装依赖、不运行真实模型、不提交或推送。本期不修改原 quality-v1 题库，不放宽生产安全策略。发现产品缺陷则保留失败证据并记录，不靠降低评分条件通过。

完成后更新 project.md，提供修改清单、12 题评分矩阵、正负控制与离线测试结果、contract 报告、dry-run 结果和真实 baseline 运行命令。将日志写入 runtime/evals/regression-v1-validation/。明确哪些仅经 fake/离线验证，不能宣称模型效果已经提升。
```
