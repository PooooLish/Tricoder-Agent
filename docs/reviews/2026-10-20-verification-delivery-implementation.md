# 验证状态与交付纠正：coding 实施报告

状态：**实现完成，待独立复审**。V1/V2原发现与V3设计核查记录保留；本文件不是独立验收，不自行关闭发现。

依据 `docs/superpowers/plans/2026-10-20-verification-delivery-state.md`，按 Task 1→2→3→4 单会话实施。V1/V2/V3 为本包范围，不重新打开已关闭的 F1/F2/F3、搜索 P2、R1/R2。未运行真实模型；最近真实16轮成绩仍是产物19/19、完整4/19，不能推断本包修复后的真实收益或全部失败原因。

## 精确基线与增量

证据目录：`runtime/verification-delivery-fix-20261020/implementation-4a468f3275/`（下文路径均相对此目录）。

- `before/`、`before-sha256.json`：实际修改前678份选定文本文件，包含当时的未提交/未跟踪版本；不是 HEAD。`status-before.txt` 保存逐文件Git状态，原有 `.pytest_cache` 无访问权限提示也保留。
- `after/`、`after-sha256.json`、`file-index.json`、`delta.patch`：最终实际文件、hash、完整选定文件索引及增量，新增文件以空 before 表示；含未跟踪的新测试和本报告。
- `protected-sha256.json`、`preservation.json`：旧证据与题库保留检查。`normalized-code-before.json`/`normalized-code-after.json`固定最终测试前后的595份生产代码、测试、脚本和题库；`final-test-code-sha256.json`、`final-code-sha256.json`均为自审中间快照，不是最终源码证据。
- `commands.json`、各命令同名 `.json/.log`：实际命令、环境、退出码、墙钟时间及完整输出。`implementation-ledger.md` 记录中途失败和修正，`evidence-index.json` 列出本包文件hash（不含索引自身）。

## 实际修改

| 任务 | 文件/接口 | 行为与保护 |
| --- | --- | --- |
| 1 | `core/validation.py:TaskValidationReport` | 末尾追加默认空 `stale_check_ids`，保持旧构造调用兼容。只描述失效身份，不带快照/authority，不持久化能力。 |
| 1 | `engine/validation.py:TaskValidationTracker.report` | 未解决失败优先 failed；有当前认可的完整、稳定、有效检查时，旧非失败 stale 不再否决 observed。保留 `_records/_latest/_stale/_unresolved` 和同签名有效复验规则。 |
| 1 | `engine/completion.py:current_verification_satisfied/assess_finish`、`engine/verification_feedback.py:_obligation` | 共享只读 authority/当前快照/联合覆盖/失败判断；finish仍重新扫描。显式检查 unresolved。补足 failed 汇总下的 stale-only 外部变化拒绝，纯确认/当前只读失败审查不凭空要求无关测试。 |
| 1 | `session/runtime.py:_merge_verification_obligation`、收尾可信通过判定、`_with_stale_task_validation` | 逐条检查最新签名、失效ID、owns_check、当前digest、类型、有效执行、目标；未解除失败不得清空义务。旧整体stale但无失效身份的报告保守拒绝。收尾失效标出全部相关ID，真实失败继续优先failed，保留基线确认逻辑。 |
| 2 | `engine/verification_feedback.py`、`protocols.py` | 最多5条blocking_checks，提供ID、类型、固定failed/incomplete/record_unavailable和受控相对目标；区分历史过期数、未解除数、省略数和pending。被淘汰失败保留ID，不编造参数；先复验失败，再补覆盖。 |
| 3 | `engine/completion.py`、`engine/loop.py` | 新增unresolved_verification窄恢复：当前完整扫描、当前任务owned稳定完整检查锚点、无停止事实。与原missing恢复共用一次计数和原max_rounds，不清空失败，不自动执行。审计返回后再次检查取消/清理/UNKNOWN/已启动期限，反馈发布前停止。 |
| 3 | `audit.py` | 仅将missing_relevant_verification、unresolved_verification、recovery_limit_reached加入固定reason白名单，审计保留reason/count/limit，不包含命令正文。 |
| 4 | README、本报告、计划实施状态、project.md、reviewer-handoff | 说明当前契约、实际证据及限制；历史发现和旧记录保留。 |

测试增量包括新 `test_verification_delivery_state.py`、`test_blocking_check_feedback.py`、`test_unresolved_finish_recovery.py`，及既有 tracker、Runtime覆盖、反馈、finish和终止诊断测试。原零测试的诊断与定向范围断言保留，仅将后来真实tests+syntax联合覆盖的最终期望改为可完成。旧“失败不能进入missing恢复”改为检查它进入独立的unresolved恢复，未改外部变化拒绝测试。
`test_verification_evidence.py` 的未解决失败负例补第二个finish，防止新增的一次纠正耗尽旧测试Provider队列；仍断言第5轮失败、义务和失败快照保留，不改变生产Provider。
最终全量暴露的另外五个旧队列也作同样适配：`test_agent.py`的三个旧失败负例、`test_delivery_budget_experiment.py`的旧失败预算负例，以及`test_eval_diagnostics.py`的逐turn失败场景。只补第二次finish并加强轮数/未解决失败断言；原8/16九轮合成序列、逐turn真假值、AND及终止原因断言均保留，未修改Eval生产代码或评分。

反馈总上限仍2000字符，pending最多20路径，每阻塞条目最多3个受控目标；过长、敏感、绝对、越界、含参数式字符或不能安全表示的目标不输出。本实现不提供任何命令正文重放提示，只用ID定位原工具回合。路径脱敏后为空不会被误认成覆盖已齐全。反馈生成不改变evidence、failed_snapshot或tracker。
纯只读失败审查保留失败事实和unresolved数量，但blocking_checks为空，并明确可按审查结果交付；不向此类任务提出修代码或补测试义务。

## RED/GREEN 与新鲜验证

所有测试用现有 `.venv/Scripts/python.exe -X utf8 -B`；运行器设置 `PYTHONPATH=src;tests`、`PYTHONDONTWRITEBYTECODE=1`。核心用真实SessionRuntime、文件工具、子进程、锁及`runtime/delivery-test-*`中新建合成数据库，Provider为离线队列。故障注入只控制宿主边界，不用mock结果替代真实门禁。

各次结果详见 `commands.json`，包括初始错误与修正，不能只看最后成功：

- Task1 `task1-red`：5项、4个失败断言、exit1；`task1-green-initial`：81项、4 failures、exit1；`task1-green`：85项、0失败/skip、exit0。
- Task2 `task2-red`：3项、3 failures/1 error（缺字段直接取值）；改为显式字段断言后 `task2-red-assertions`：3项、4 failures、0errors、exit1；`task2-green`：9项通过、exit0。
- Task3 `task3-red`：6项、8个失败断言、exit1；初始GREEN尝试45项、23 failures/1 error，错误将组合scope ID与原token相等比较；下一次45项、5 failures（旧响应队列/状态断言及幂等收尾调用次数假设）；修正后 `task3-green`：51项通过、exit0。
- 自审额外RED：脱敏空路径的覆盖提示、只读失败检查后的外部变化、Runtime最终failed优先级、恢复审计返回期间的cleanup/deadline/UNKNOWN。对应日志分别以`task2-boundary`、`task1-external-failure`、`task1-runtime-status`、`task3-audit-boundary`命名。均保留初始失败，不隐藏中途错误。
- `focused-final`：133项、0失败/skip、76.580秒测试时间、exit0。之后补Runtime状态和审计窗口保护；`focused-final-v2`：133项、1 failure、exit1，原因是Windows单次sleep(5.2)实测5.187秒。保持>=5.2断言，改为循环等待实测达到阈值。
- 最终边界 `task3-final-boundaries-green`：10项、0失败/skip、35.953秒测试时间、exit0；旧队列负例 `legacy-negative-green`：1项、0失败/skip、0.986秒、exit0。
- `task2-read-only-red`：1项失败（只读失败审查被错误列为blocking）；`task2-read-only-green`：11项、0失败/skip、6.206秒、exit0。名为`full-final`的第二轮全量启动后发现此反馈问题，因此它也作为中间版结果保留，最终完整证据使用后续日志。
- `full-final`：1837项、2 failures/3 errors/15 skips、428.549秒，exit1（墙钟436.203秒）。五个失败均来自新增纠正后旧Provider队列耗尽；单独保存 `legacy-fixture-red`：5项、2 failures/3 errors、8.772秒、exit1。适配队列且保留失败门禁断言后，`legacy-fixture-green`：5项、0失败/skip、exit0（墙钟10.046秒）。
- `focused-verified`：最终源码139项、0失败/错误/skip、80.198秒测试时间、81.812秒墙钟、exit0。包含上列五个既有负例，以及原搜索P2和新增至少5.2秒恢复等待。
- `full-verified`：LF格式恢复前的完整回归1838项，其中1823通过、15跳过、0 failures/errors；测试417.163秒，墙钟422.156秒，exit0，本轮通过记录保留。
- `full-normalized`：最终字节快照的完整回归1838项，其中1823通过、15跳过、0 failures/errors；测试421.653秒，墙钟426.843秒，exit0。跳过不计通过：11项当前Windows账户无符号/目录链接权限，2项Windows打开锁文件时不支持对应替换/重命名，1项需要Linux抽象socket，1项当前文件系统不允许控制字符文件名。逐条身份和原因为`skips.json`。
- `syntax-verified.json`为最终242份src/tests的内存语法编译（不写pyc）；`git-diff-check-verified`与`changed-whitespace.json`检查tracked及本次新增/替换行，均exit0。CLI `--help`/`run --help`本轮各exit0，未加载真实模型。所有命令完整参数、退出码、数量和时间见`verification.md`/`commands.json`。
- 最后打包曾因Windows交接脚本把4份文档LF转为CRLF而失败：`packaging-attempt1/`保留当时after、脚本及git diff --check的exit2输出（打包脚本exit1）。只恢复这4份文档的原LF格式并修正脚本换行选项，未改变代码语义；之后还发现`test_agent.py`适配旧队列时引入CRLF，第二次打包也拒绝（exit2/打包exit1，记录在`packaging-attempt2/`）。仅将该文件恢复为原LF，`line-ending-normalization.json`证明AST一致；重新冻结595份文件并运行`full-normalized`。生产代码未再变化，最终打包及空白检查重新执行。

聚焦命令：

```powershell
$env:PYTHONPATH='src;tests'
$env:PYTHONDONTWRITEBYTECODE='1'
.venv/Scripts/python.exe -X utf8 -B -m unittest test_verification_delivery_state test_blocking_check_feedback test_unresolved_finish_recovery test_task_validation test_finish_coverage test_finish_verification_recovery test_verification_feedback_consistency test_verification_obligation test_runtime_coverage test_delivery_stop_diagnostics test_search_timeout_recovery test_agent.AgentTests.test_finish_after_failed_verification_fails test_agent.AgentTests.test_successful_verification_does_not_override_prior_failure test_agent.AgentTests.test_unrelated_success_does_not_override_failed_verification test_delivery_budget_experiment.BudgetExperimentTests.test_same_nine_round_sequence_and_extra_budget_cannot_clear_old_failure test_eval_diagnostics.EvalDiagnosticsTests.test_real_runtime_delivery_turns_keep_and_and_reset_declaration -v
.venv/Scripts/python.exe -X utf8 -B -m unittest discover -s tests -v
```

第一轮全量运行期间，自审继续修正了Runtime状态与恢复审计边界；该轮仅为中间回归。它在 `test_real_modification_with_unresolved_failure_remains_blocked_after_pass` 的旧Provider队列耗尽后挂起（StopIteration不能传入asyncio Future），核对命令和父PID后仅停止本轮该测试子进程；墙钟702.438秒，子进程exit4294967295、wrapper exit1，没有完整统计，不能计作通过。身份和异常保存在 `interrupted-test-process.json`/`full-regression.log`。最终全量另行运行。旧reviewer的73/78/228项结果不计入本次通过证据。

## 真实Runtime场景与安全边界

`probe.py`/`probe-final.py`/`probe-verified.py`是复制到本包的新探针，ROOT/OUT已修正，旧reviewer目录未执行或写入。生产代码最终结果在`probe-results-verified/results.json`，9场景通过、exit0、墙钟8.375秒，自身前后595份源码/测试/脚本/题库hash一致；探针后只修正交接文档及`test_agent.py`的LF格式，生产代码未变；探针不是旧真实模型历史命令的逐字重建。

| 场景 | 实现后期望/结果 |
| --- | --- |
| 旧成功检查→修改→不同命令当前全覆盖 | 完成；0未解决，会话pending清空 |
| 同上再补原命令 | 完成，保持原成功路径 |
| 旧成功普通脚本→修改→当前全覆盖 | 完成，无额外脚本重跑义务 |
| 旧零测试→创建测试→tests+syntax当前联合覆盖 | 完成；零测试自身仍无验证能力 |
| 同一联合覆盖，无旧零测试 | 完成 |
| 真实失败→修改→只syntax→两次finish | 保持失败，不能靠syntax清除失败；只纠正一次 |
| 真实失败→修改→同签名有效复验→syntax | 完成，未解决集合按真实复验清除 |
| 失败→过早finish→修复→同检查→补覆盖→finish | 第3轮纠正，第7轮完成；同一任务/owner/锁 |
| 纯确认 | 第1轮完成，无无关检查 |

此外测试覆盖：当前失败不修复、无关/零测试/过滤测试、第二次过早finish、两种原因共享计数、记录32条淘汰和摘要回返不复活、旧报告默认字段、跨任务/authority、外部变化、完整扫描失败、审批拒绝、明确incomplete、最后一轮、记忆/历史未提前闭合、批次剩余动作仍skipped。新纠正期间实测等待至少5.2秒后正常复验，原搜索P2也独立重跑；清理未确认继续停止并保留精确owner。

## 自审、保留结果与限制

- 没有重置deadline、清空failed/pending或换scope隐藏资源。当前失败集合与旧失效ID分别保存；Runtime不从旧报告或持久化记录恢复通过能力。
- 对只剩stale、缺少当前可信锚点的任务仍保守停止。本包没有承诺覆盖全部真实失败，也不将syntax等同业务需求正确。
- 反馈的ID只能定位原工具回合；超过窗口的记录不保留完整参数，某些非ASCII/复杂路径也会被保守省略。这是当前脱敏策略限制，未新增自动重放工具。
- 检查和Runtime沿用既有有限快照及TOCTOU边界；没有扩展文件系统覆盖、目录级Eval审计、权限策略、评分或预算。
- 未运行真实模型、Linux/POSIX、网络文件系统或真实外部MCP；旧真实结果、冻结实验、quality-v2.2题面/隐藏verifier和AND均不改。搜索截断题面与Replan等后续事项不在本包。
- 保护核验：678份before副本自校验；4453份已记录历史证据未变；按旧已保护manifest回查最新8/16轮原生run的82份结果/报告/计划/审计未变（只读计算hash，不输出正文）。352份选定eval文件未变，其中包含quality-v2.2全部102份题库文件；最初快照未纳入旧regression的5份txt/log fixture，本包未修改这些文件，但不声称对其作了before hash证明。最终测试前后595份代码/测试/脚本/题库hash一致。
- 最终after有682份选定文件，实际25份增量（8份生产代码、12份测试、5份文档）；逐文件完整hash见`file-index.json`，增量列表见`modification-index.md`。未跟踪新测试及本报告都纳入after/delta。
- 自审核对旧失败负例与AND断言保留、Runtime不复活旧记录、一次计数/预算/owner不变、受控反馈不含命令正文。独立reviewer应以实际before/delta及新鲜日志复核，不以本实施记录代替验收。完成后停止，等待用户交回reviewer。
