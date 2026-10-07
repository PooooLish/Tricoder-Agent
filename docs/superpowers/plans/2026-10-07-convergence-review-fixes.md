# 第三批失败收敛复审补齐执行计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. 用户另行交给 coding session 实施，不要求启动子 Agent。

**Goal:** 修复第三批 C1/D 复审发现的三处问题：重复回填工具结果、大型输出绕过重复检测、实际修改后读取计数未分段。

**Architecture:** 保持现有 Runner、工具注册表、ProgressGuard 与统一收尾边界；在原有分支补齐单次回填、稳定结果摘要和独立读取阶段。重复失败与振荡仍复用原有证据，不重写 Agent 循环。

**Tech Stack:** Python 3.11+、asyncio、unittest、现有临时工作区及 fake Provider；不增加依赖。

**Spec:** `2026-10-06-task-verification-clarification-convergence.md` 第 10—12 节；`project.md` 中“2026-10-07 第三批 C1/D 复审”；本文件的 F1—F3 验收要求。

**状态：** R0—R4 已按当前工作树实施并完成本地验证，现停在待复审交付点；这表示修复候选已交付，不表示复审已经通过。实施证据见 `runtime/task-quality-round1/convergence-review-fixes.md`。

**实施摘要（2026-10-07）：**

- R0：基线 HEAD `43f721a`；诊断脚本确认 F1 为 `call-2=2 / closed=false`、F2 消费第 5 次 Provider 并成功、F3 在第 6 次请求误停。三个场景均先转为正式失败断言。
- R1：进展扫描取消分支复用 `remaining_filled`，已回填的失败批次不再重复发布结果；尚未回填的成功批次仍只补一次。覆盖 skipped 审计失败优先级、原生取消清理失败、事件/审计单次性和后续记忆候选。
- R2：`ToolRegistry` 在输出预算与 spill 前生成宿主 `progress_output_digest`；Runner 不再把随机引用或展示包装加入指纹。文件正文按完整内容摘要，命令耗时仅在命令检查路径规范化；扩展自报摘要被覆盖，原 `spill_sha256` 完整性字段保留。
- R3：`ProgressGuard.note_workspace_change()` 只推进读取阶段；仅成功、非中断且由账本/宿主确认的实际文件或目录变化触发。no-op、UNKNOWN 和自报路径不触发；失败签名与振荡序列不携带阶段号。
- R4：修复后诊断为 F1 每调用一条结果且闭合、F2 第 4 次停止、F3 可继续到 finish 后按真实“未验证”事实结束。聚焦组合 `Ran 355`，OK；完整回归 `Ran 1541 tests in 328.642s`，OK（13 skipped）；compileall、CLI help、19 项导入边界与 `git diff --check` 均通过。

## 1. 范围与交付原则

- 只补 F1/F2/F3，不实施流看门狗、统一任务预算、任务计划系统、沙箱或 Provider 扩展。
- 先读 `../../AGENTS.md`、项目 `AGENTS.md`、`README.md`、`pyproject.toml`、`project.md` 和原执行方案。
- 以执行时最新工作树为准，保留前三批已有修改；禁止从 HEAD 覆盖文件或重置工作区。
- 不读 `.env.local`、真实凭据或用户会话库，不调用真实 Provider，不安装依赖，不自动提交或推送。
- 不提高停止阈值，不关闭 ProgressGuard，不清空历史，不自动撤销文件，不通过把失败改成成功来消除报错。
- 保持取消、审计失败、UNKNOWN、清理失败的优先级及既有权限、审批、锁、记忆水位语义。
- 新测试使用项目现有虚拟环境、合成临时工程；不要用用户桌面工程验证。
- 阶段证据放 `runtime/task-quality-round1/`。最终更新 `project.md` 和本计划完成状态，旧失败记录保留为历史。

## 2. 已有复现证据

复审运行的 8 组聚焦回归共 144 项通过；额外探针发现下列缺陷。通过现有测试不能替代新场景验收。

从项目根执行：

```powershell
.\.venv\Scripts\python.exe -B runtime/task-quality-round1/review-c1-repro.py
```

| 问题 | 当前实际结果 | 修复后要求 |
|---|---|---|
| F1：首工具失败后进展扫描取消 | call-1 一条结果，call-2 两条结果；`task_block_closed=false` | 每个调用恰好一条结果；任务历史可闭合 |
| F2：相同大型文件连续读取四次 | 未停止，继续第 5 次 Provider 请求并 finish 成功 | 第 2 次提醒、第 4 次停止，不发起第 5 次请求 |
| F3：读 A 三次，修改 A→B→A 后回读 | 第 6 次请求中的首次回读被当作第 4 次重复读取而停止 | 修改后首次回读不触发旧读取预算；随后可正常请求 finish |

该脚本是诊断探针，exit 0 只表示脚本运行完成。必须将这些场景转为正式断言测试；修复后不能仍用旧缺陷输出作为通过条件。

## 3. 文件与接口边界

| 文件 | 本轮职责 |
|---|---|
| `src/tricoder/engine/tool_batch.py` | F1 单次回填；F2 消费稳定结果摘要；F3 在可信变更后通知读取阶段变化 |
| `src/tricoder/engine/progress.py` | F3 独立读取阶段；不得清除失败/振荡记录 |
| `src/tricoder/tools/__init__.py` | F2 在截断或 spill 包装前产生宿主结果摘要 |
| `src/tricoder/models.py` | 仅在需要传递宿主摘要时追加带默认值的字段，保持旧构造兼容 |
| `src/tricoder/changes.py`、`workspace/verification.py` | 优先复用真实内容变化依据；不改严格验证与冲突检测的 digest 语义 |
| `tests/test_agent_convergence.py`、`tests/test_progress_guard.py` | 三项核心回归及反向场景 |
| `tests/test_context_spill.py`、`tests/test_memory_compaction.py`、`tests/test_memory_save_coverage.py` | 大输出一致性、停止后的历史和记忆兼容 |

建议的窄接口：

- F2：在工具结果进入输出预算处理前计算 `progress_output_digest`，若增加到 ToolResult 则默认 None。由宿主注册表计算/覆盖，不信任扩展自报；Runner 使用可信值，兼容路径缺失时由宿主计算有界回退值。实际命名可调整，交接记录映射。
- F3：`ProgressGuard.note_workspace_change() -> None` 只递增读取阶段。保留 `note_user_answer()`，两者都不得清除失败或振荡证据。阶段号只参与读取指纹，不参与失败或检查指纹。
- F1：不新增公开接口，先复用 `remaining_filled`；若抽取辅助函数，必须保留错误优先级与事件/审计的单次性。

## 4. R0：复现与固定基线

- [x] 记录 HEAD、status 和相关 diff；核对三个问题是否仍存在，已修复的部分只补验证，不重复改写。
- [x] 阅读现有复现脚本、进展守卫、工具批次、spill、变更证据及相关测试。
- [x] 执行诊断探针，记录每项实际结果和环境。
- [x] 将 F1/F2/F3 分别补入正式测试，运行并确认因预期缺陷失败；记录 RED 证据，不能仅因 fixture/import 出错就算复现成功。

统一测试命令形式：

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p 'test_agent_convergence.py' -v
```

## 5. R1 / F1：取消时保持工具结果一对一

**根因：** 工具可恢复失败时，批次已补齐后续 skipped；进入进展观察后若扫描抛取消异常，异常分支再次无条件调用 `fill_remaining_results()`。

**主要位置：** `engine/tool_batch.py` 复审时第 284—296 行；行号可能随其他修改变化，以符号和逻辑定位。

- [x] 新增 `test_failed_batch_progress_cancel_fills_remaining_once`：同一 native 响应含非法参数 read_file 和另一个合法调用；仅在进展扫描注入 CancellationError。
- [x] 断言两个 call ID 各有一条 tool result；第二个为 skipped；后续工具未执行；Provider 不再请求；取消结果通过统一 finalizer 返回。
- [x] 使用当前 ContextManager 检查该任务块闭合，再验证它不会因重复结果阻塞后续任务的压缩/保存；不伪造成功 finish 或推进成功水位。
- [x] 最小修复异常分支：已经回填则不再回填；尚未回填则补齐一次，并处理回填审计失败。不要用简单吞异常掩盖问题。
- [x] 增加 NativeCancellationError 等价路径；取消时携带 cleanup_failed 的情况仍保留清理失败事实。
- [x] 反向测试：首工具成功后扫描取消，后续调用仍被补一次；首次 skipped 审计失败仍优先按审计失败收尾；tool 完成事件和 skipped 审计不重复发布。
- [x] 运行 `test_agent_convergence.py`、`test_agent_termination.py`、`test_memory_compaction.py`、`test_memory_save_coverage.py`、`test_audit.py`，记录 GREEN。

**完成标准：** 可恢复失败＋多调用批次＋进展扫描取消不再破坏一对一结果配对。保留已有调用和真实错误，不删除历史来凑配对。

## 6. R2 / F2：指纹基于结果内容，不基于暂存说明

**根因：** spill 后 result.output 变为“引用＋大小＋预览”等展示文本，每次调用的 reference 不同。当前指纹对这段文本取 hash，导致相同内容也被识别为新结果；原始 spill_sha256 又可能抵消命令耗时归一化。

**主要位置：** `engine/tool_batch.py::_result_fingerprint`、`tools/__init__.py::_apply_output_budget`。

- [x] 新增等价正式回归 `test_spilled_identical_reads_stop_on_fourth_result_content`：真实 ToolRegistry、SpillStore 和 fake Provider；在 workspace 之外的独立临时 runtime 保存 spill。把内联上限调小，确保大文件读取实际进入 spill 分支。
- [x] 验证四次内容相同且引用不同；第 2 次有提醒、第 4 次停止；同批后续写入被 skipped；第 5 个排队 Provider 响应不被消费。
- [x] 在宿主输出预算处理前产生稳定结果摘要，并传给进展检测。不要通过读取任意 spill 路径、把完整输出重新塞回模型或修改引用唯一性来修复。
- [x] 完整原始内容进入摘要，不只取头尾；仅中间内容改变也必须改变摘要。hash 可以增量处理，不新增保存整份明文的副本。
- [x] 区分输出用途：文件内容读取按内容计算，不能把源码中的 `1s`/`2s` 一概当作运行耗时删除；命令诊断可沿用有界、明确的耗时规范化。指纹中的长度和其他字段不得重新引入已排除的随机引用或耗时差异。
- [x] 规范化命令结果之后，不再把原始 spill_sha256 混入同一比较而抵消规范化。原 sha 继续服务于暂存完整性检查，不能删除或更改这项安全校验。
- [x] 新字段如由 ToolResult 承载，工具注册表必须覆盖/剥离扩展伪造值；不要把摘要来源信任交给 Provider 或扩展。旧 ToolResult 构造与无 spill 路径保持兼容。
- [x] 补反向场景：相同内容不同 call ID；不同内容相同长度；仅中间内容不同；命令结果仅耗时变化；真正错误信息变化；无 spill 与 spill 失败回退；扩展自报假摘要；文件正文时间字符串不同。
- [x] 运行 `test_progress_guard.py`、`test_agent_convergence.py`、`test_context_spill.py`、`test_tools.py` 及改动涉及的扩展结果校验测试，记录 GREEN。

**完成标准：** 暂存位置、随机引用和展示包装不会制造进展；真实内容差异仍可区分；原有暂存隔离、容量、完整性校验不变。

## 7. R3 / F3：真实变更开启新的读取阶段

**根因：** 当前读取指纹含工作区摘要和 answer_epoch，却没有变更阶段。A→B→A 恢复到旧摘要时，会再次累计修改前的读取；而成功编辑不会进入 `_observe_progress()`，仅在下一次读时比较摘要也看不到中间的 B。

- [x] 新增等价正式回归 `test_real_a_b_a_change_starts_new_read_interval`，先固定读取 A 三次→edit A→B→edit B→A→读取 A 的真实 Agent 场景，最后排入 finish。
- [x] 断言修改后首次回读未停止，Provider 可继续请求 finish；不要要求未重新检查的代码获得验证通过。可单独断言“未因 repeated_observation 停止”，避免把业务验收和循环判断混在一起。
- [x] 为 ProgressGuard 增加独立读取阶段号及 `note_workspace_change()`；读取比较限定阶段，失败签名和振荡状态序列不携带此阶段号。
- [x] 在成功且可信的实际变更发布后通知 guard。优先使用当前工具变更账本中前后内容/路径/目录存在性的真实差异；必要时使用完整受覆盖快照。每次 A→B、B→A 都必须观察到，不能只比较任务最初与最终净 Diff。
- [x] 无内容变化的重写、重复 create_directory(exist_ok=true)、只读工具成功、版本查询或工具自报 modified_paths，均不能自行开启新阶段。对象 identity 或 mtime 改变不等于内容进展。
- [x] 不完整扫描、UNKNOWN、取消或清理失败不视为有效进展；不得清除已有失败或恢复安全执行资格。使用快照时沿用现有取消与安全收尾规则。
- [x] 反向测试：同阶段四次读仍停止；真实变更后可重新读，但该新阶段四次重复仍停止；no-op 不重置；新用户回答只重启读取区间；失败→读/写→回到相同失败仍累计；A→B→A→B→A 同检查失败仍触发振荡。
- [x] 维持最多 32 条观察、新任务隔离和既有 native 无工具预算；不能每次写入就重建整个 guard。
- [x] 运行 `test_progress_guard.py`、`test_agent_convergence.py`、`test_agent_termination.py`、`test_session_runtime.py`，记录 GREEN。

**完成标准：** 正常修改后回读不被旧读取次数误伤，但失败与振荡检测不被写操作绕过。

## 8. R4：整体回归和交付

- [x] 复跑诊断脚本，记录修复后的输出。F1 每个 call ID 一条且 closed=true；F2 在第 4 次读取停止；F3 不再以 repeated_observation 在第 6 次请求停止。用正式测试断言作为验收主证据。
- [x] 合并运行 `test_progress_guard.py`、`test_agent_convergence.py`、`test_agent_termination.py`、`test_memory_compaction.py`、`test_memory_save_coverage.py`、`test_session_runtime.py`、`test_audit.py`、`test_clarification.py`，再覆盖 R2 新触及的 spill/扩展/模型兼容测试。
- [x] 核对取消、审计失败、UNKNOWN、清理失败、锁释放、后续任务及失败记忆未回归；记录具体测试名，不只写“安全不变”。
- [x] 最终代码运行一次完整回归及 CLI 帮助、导入边界检查、`git diff --check`。若出现失败，区分基线问题与本轮引入；只在新改动或未解决问题需要时重跑。
- [x] 更新本计划与 `project.md`：逐项 F1/F2/F3 的改动、RED/GREEN、全量结果、剩余限制。原主计划顶部仍存在“C 未开始”的历史状态文字，交付时同步成准确状态，保留历史证据。
- [x] 完成后停在待复审交付点；不自行把复审结论写成“审查通过”，不自动提交/推送。

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -q
.\.venv\Scripts\python.exe -B -m tricoder --help
git diff --check
```

## 9. 自审重点

1. 失败批次已经补结果后再取消：结果、事件和审计都只能一次，R1 覆盖。
2. 大输出只变引用或耗时：不能视为进展；正文中部变化仍须识别，R2 覆盖。
3. 扩展伪造摘要：不得影响可信进展计数，R2 覆盖。
4. A→B→A 有真实写入但最终净零：重启读取阶段，保留振荡证据；no-op 不重启，R3 覆盖。
5. 停止后再次运行及记忆处理：不残留重复 ToolResult，不继承任务计数，不伪造成功，R4 覆盖。

## 10. 交付给 coding session 的提示词

```text
请在 D:\MaHong\AGENT_WORKSPACE_V2\projects\tricoder-cli 中执行第三批 C1/D 复审补齐。

先读工作区/项目 AGENTS.md、README.md、pyproject.toml、project.md，再完整阅读：
docs/superpowers/plans/2026-10-07-convergence-review-fixes.md
并参考原计划：
docs/superpowers/plans/2026-10-06-task-verification-clarification-convergence.md

本轮仅修复已复现的 F1/F2/F3：
F1：失败批次已经补 skipped 后，进展扫描取消再次补结果，破坏历史配对。
F2：大型输出的暂存引用进入结果指纹，使相同读取绕过重复检测。
F3：真实修改 A→B→A 后，首次回读错误沿用修改前读取次数。

按 R0—R4 顺序实施。先运行 runtime/task-quality-round1/review-c1-repro.py，
再把三个场景变成正式失败回归，确认根因后做最小修复。
诊断脚本 exit 0 不是验收通过，必须核对断言与行为。

保留已有工作树修改。不得提高阈值、关掉守卫、清空历史或自动撤销。
F1 保持单次结果回填及审计/取消优先级；F2 保持暂存完整性并拒绝伪造摘要；
F3 仅重启读取阶段，不能清空失败与振荡记录，no-op 不能制造进展。

不扩展新功能、不安装依赖、不读 .env.local/真实凭据/用户会话数据库，
不调用真实 Provider、不自动提交或推送。用 fake Provider 和合成临时工程验证。

完成聚焦和最终完整回归，更新 project.md、两个计划的当前状态，
将 RED/GREEN 与回归证据放 runtime/task-quality-round1/。
最终用中文报告逐项修复、实际测试结果、未验证边界和剩余风险，停在待复审交付点。
```
