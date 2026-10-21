# TriCoder 评测契约与工具反馈修订实施方案

> **For agentic workers:** 使用 `superpowers:executing-plans` 在用户指定的 coding session 中逐任务实施；不用并行子 Agent。本文不授权 reviewer 会话实施。用户要求保留脏工作树且不提交/推送，优先于技能中的 worktree/commit 默认步骤。

**Goal:** 修复已证实的评测契约缺口及 Git 辅助工具中断问题，改善补丁错误诊断，用版本化题库与分阶段实验验证效果。

**Architecture:** 工作分为两个独立验收的部分：A 只修改 Eval 契约与报告，B 修改 Agent 固定工具反馈。A 完成后先取得当前 Agent 的修订题库基线，再实施 B；保持题库与运行条件不变进行对照。保留旧题库、旧结果、现有安全门禁，不启动 Replan 重构。

**Tech Stack:** Python 3.11+、unittest、TOML、现有 CLI/Provider/SessionRuntime；不增加依赖。

**Spec:** 本文第 1～3 节为本轮确定设计；事实依据为 `docs/reviews/2026-10-19-quality-v1-failure-analysis.md`、`docs/reviews/2026-10-19-quality-v1-real-baseline.md`。先阅读两份报告，不把建议描述当成已完成实现。

## 1. 范围与优先级

本次必做四项，分别提交可独立检查的增量证据：

1. **A1：quality-v2 公开契约。** 解决允许修改范围未传给模型、关键业务语义只在隐藏测试出现的问题。
2. **A2：Eval 诊断。** 保留逐轮事实，明确故障证据类型，避免把模拟计数当成真实重试。
3. **B1：补丁反馈。** 为受限 unified diff 提供准确示例和有限、可脱敏的错误子类型。
4. **B2：Git 辅助工具不可用。** 固定无参数 git_diff 在已确认父仓库场景不执行 diff，并返回可继续的能力缺失；任意命令和真正策略违规保持原行为。

本轮不做：Replan 架构、长期记忆或压缩重构、自动扩大 max_rounds、兼容任意补丁语法、扩充复杂项目题、真实网络故障注入、自动遍历三家模型、安装依赖或修复历史待办。上述覆盖缺口在报告明确列出，不宣称本轮已解决。

`mm-latest-correction` 的错误通过公开空值契约及保留隐藏测试暴露；不要给 Agent 写题目 ID 特判、直接修评测产物或灌入标准答案。更清晰的工具反馈不保证模型不再犯语义错误。

## 2. Global Constraints

- 工作区：`D:\MaHong\AGENT_WORKSPACE_V2\projects\tricoder-cli`。先读 `../../AGENTS.md`、项目 `AGENTS.md`、README、pyproject、project.md、上述分析报告和本文。
- HEAD 不是精确 before。保留全部未提交、未跟踪内容；不得 reset/clean/checkout 覆盖、从 HEAD 重建实现或丢弃已有文件。不要为执行计划强制迁移到丢失未提交内容的新 worktree。
- 只在本项目修改。开始时将本轮涉及的源文件、测试和题库做内容快照与 SHA-256 清单，保存到独占的 `runtime/eval-contract-tools-20261019/<新运行标识>/before/`；记录 tracked/untracked 状态，但不复制凭据、数据库或整个工作目录。
- 不读取或打印 `.env.local`、真实密钥正文、用户数据库；不安装依赖、不提交或推送项目，不新建会话或跨会话发送消息，不使用子 Agent。
- 用户已允许真实模型测试且确认配置可用。仅由既有配置加载器在评测进程内加载密钥，不人工查看文件、输出配置对象或复制凭据。本文最多安排 DeepSeek `deepseek-v4-flash` 的 A/B 各 19 次；不默认扩展到三家模型或每题三次。配置加载失败则报告阻塞，不索要或输出密钥。
- 所有新日志使用独占目录；旧 `runtime/quality-v1-real-20261019/` 和 `runtime/evals/20261019t091216.781907z/` 只读。不把原报告重算为通过。
- 不改 `evals/quality-v1/`、其原始 verifier、smoke/regression 题库或历史证据。兼容已有 schema、默认行为和旧结果读取；新增可选字段必须默认不改变旧题提示。
- 不放宽审批、路径边界、UNKNOWN、取消、清理失败、验证义务和资源所有权；不把 compileall 成功表述为功能正确。

## 3. 确定设计

### 3.1 新题库和可见约束

新增 `evals/quality-v2/`，以 quality-v1 的 30 题为起点，保留题目 ID、dev/holdout、19 quality/11 contract、8 轮预算及原 verifier 内容。suite 的 id/benchmark_version 改为 `quality-v2`，schema_version 仍为 2。复制来源与逐题差异写入该目录 README。v1 保留原样。

新增 `EvalCase.expose_edit_scope: bool = False`，仅为 schema 2 的可选字段；只接受真实 bool，拒绝字符串/整数。v2 的 19 道 quality 题设为 true；11 道 contract 保持原有设定和脚本，不扩大允许范围。

新增 `src/tricoder/evals/prompts.py`：

```python
def build_case_steps(case: EvalCase) -> tuple[ScenarioStep, ...]: ...
```

输入优先级保持 `case.steps` 优先、否则以 `case.task` 创建 user_turn。expose_edit_scope=False 时内容逐字保持；为 true 时在每一个 user_turn 附加由 `allowed_changes` 生成的固定范围说明，控制步骤不变。不得将 task 再插入多轮步骤而提前泄露后续修正，不得载入 verifier、隐藏断言或标准答案。required_changes 仍由评分器执行，不额外制造一份范围真相源。

v2 的 19 quality 题保留原源文件 allowed_changes，统一追加 `tests` 和 `tests/*.py`，允许新建 tests 目录及其直接子 Python 测试文件；不允许任意源码路径、根目录 test 文件、任意深度目录。公开说明明确测试应放 `tests/`，不强制 Agent 写测试。继续按当前 matcher 评分，新增测试验证实际匹配语义，不能凭 glob 名称猜测递归行为或改写全局路径规则。

以下题面的 task 与对应 user_turn 同步修改：

| 题目 | 公开需求（保留故障说明） |
| --- | --- |
| rc-invalid-args | normalize 返回去掉首尾空白并转小写的字符串；保留内部空白；全空白输入返回空字符串。首次写工具会返回一次模拟参数错误。 |
| rc-patch-conflict | replace_tag 替换所有出现的 old 为 new，无匹配时保持原文；首次写操作会返回模拟上下文冲突，重读后继续。 |
| dl-regression | divide 在 b==0 时返回 None，其他情况返回 a/b；正常除法行为不变。 |
| mm-latest-correction | 只修改第二个修正 user_turn：以 latest 参数为准，包括空字符串；不得因假值退回 initial。保留第一轮旧决定，以测试最新修正。task 可同步概述，但不能作为额外首轮消息。 |

其余题逐项检查“可见需求能解释每条隐藏断言”；只补缺失规范，不把隐藏测试输入/源码放进 prompt。发现额外歧义时记录具体断言与题面差异，按同一原则修订 v2，不动 v1。若原题含“只修改 X.py”等措辞，改为“生产源码只修改 X.py；可按公开范围添加测试”，消除与新增 tests 路径的矛盾，不扩大可改的生产源码范围。

### 3.2 逐轮与故障证据报告

在 Eval 数据层新增 `EvalTurnRecord`（不用修改生产 RunResult）：`turn_index: int`（从 1 起）、`ok: bool`、`verification: str`（沿用安全枚举）、`rounds: int`、`tool_calls: int`、`terminal_tool_error: str | None`（仅 ErrorCode 白名单值）。TrialRecord 增加 `turn_results: tuple[EvalTurnRecord, ...] = ()`、`first_failed_turn: int | None = None`。

`run_runtime_scenario` 增加可选 `on_user_turn_completed: Callable[[int, RunResult], None] | None = None`，每次 user_turn 完成后调用。service 利用现有 observer 收集该轮工具错误，回调时保存结果并清空该轮观测。terminal_tool_error 只记录实际观察到的 STOP_TASK 根错误；不是所有失败都有工具错误，不知道则 None，不从自然语言猜。取消、UNKNOWN、清理失败仍以原结果为准，诊断字段不参与授权/执行。

整体 ok 仍为各 turn 的 AND，旧 failure_codes、最终产物评分及 failure_stage 保持语义。报告同时显示逐轮状态、首个失败轮和整体分项，不把后一轮成功覆盖前轮失败。

TrialRecord 再加 `fault_evidence: tuple[tuple[str, str], ...] = ()`，只列实际触发的固定 fault ID，对应类型：provider_transient=`counter_only`；tool_transient_read/tool_invalid_args/tool_output_truncated/patch_conflict/approval_denied=`synthetic_tool_result`。旧结果缺字段按“证据类型未知”展示，不推断真实机制。JSON 使用固定对象列表 `[{"fault_id": ..., "kind": ...}]`，严格白名单，不能存任意异常文本。

本轮不改注入机制和旧 retries 数值；报告明确 retries 在该路径包含模拟恢复计数，不能称为实际 HTTP 重试次数。没有触发时 recovered 仍为 None。README 分清“收到模拟错误后继续”与“实际传输/参数修复/截断机制验证”；memory off/off 明确只能测短多轮行为。新增字段读写往返、旧结果读取和比较器兼容都要测试。

### 3.3 固定工具错误子类型

在 `ToolError` 末尾添加可选 `detail_code: str | None = None`，保留现有位置参数兼容；`tool_failure` 增加同名 keyword-only 参数。只允许固定集合，public_fields 仅在非空时输出，不把异常正文装入 detail_code：

```text
git_workspace_unavailable
patch_invalid_header
patch_invalid_hunk
patch_count_mismatch
patch_unsupported_operation
patch_context_mismatch
patch_invalid_format
```

detail_code 仅供反馈/诊断，不决定 recovery、成功状态、文件副作用或验证结论。非法值必须拒绝或在外部输入边界被安全归一化，不透传到模型/日志；不要依据中文异常消息做字符串分类。复用现有 `error.public_fields()` 进入协议与审计的路径；检查实际经过路径，不为此记录完整补丁。

`PatchError` 添加 keyword-only 的 `detail_code`（默认 patch_invalid_format），解析器各抛错点按结构赋值；原文本不作为稳定 API。ApplyPatchTool 保留 INVALID_ARGUMENT/现有恢复语义，返回固定解释与错误子类型；实际上下文不匹配必须发生在任何写入/审批提交前，保留原子预检。

工具 description 给出一个完整、可被当前解析器接受的最小 unified diff 示例，明确 `--- a/...`、`+++ b/...`、带行号与计数的 `@@ ... @@`、上下文前缀、真实换行；不接受 Markdown 围栏或其他工具的 Begin/End Patch 语法。让测试解析这个公开示例，避免示例自己错误。不要扩展格式兼容范围。

### 3.4 Git 的最小行为修改

本轮选择**固定 helper 返回明确不可用结果**，暂不做动态工具注册或在 definitions 属性里执行 Git 探测，以免新增隐藏子进程、缓存失效与工作区切换复杂度。

只改变 `GitDiffTool.run` 中已明确发现 Git 根在工作区外、尚未启动 diff 的分支：

```text
ok=False
error.code=EXECUTION_FAILED
error.recovery=REPLAN（使用既有枚举，不新增规划/重试框架）
error.detail_code=git_workspace_unavailable
file_effects=NONE
verification_passed=None；不签发验证证据
```

固定反馈：“当前工作区不是独立仓库根，git_diff 不可用；未执行 diff。请使用当前工作区文件与已有变更记录继续验证，不要通过命令读取父仓库。”同步在工具描述写明适用条件。

仅根探测明确确认不相等时走该分支；探测超时/异常/不确定不伪装成已确认的能力缺失。没有 Git 或不是 Git 仓库不伪报“没有变更”；保留/明确原有安全失败行为。根与 cwd 相等时保留正常 diff 行为，包括 git worktree 的 `.git` 文件形态，不用“.git 必须为目录”的捷径。

`RunCommandTool` 的父仓库命令拒绝、其他 POLICY_DENIED 默认 STOP_TASK、取消、TIMEOUT、OUTPUT_LIMIT、CLEANUP_FAILED、RESULT_UNCERTAIN 和 UNKNOWN 全部保持。不得将 _git_command_escapes_workspace 全局返回 False，也不得改全局 tool_failure 映射。不得为掩盖该问题给全部 Eval 工作区初始化 Git 仓库。

## 4. Review Focus

1. 多轮最新修正不能提前进入第一轮提示；每轮可见范围与评分一致。（A1）
2. 测试文件可写不等于可改任意源码；保留 hidden verifier 隔离与根目录/深层目录越界负例。（A1）
3. 前轮失败后轮成功、故障未触发、旧记录缺字段：不能被展示成完整通过或真实恢复。（A2）
4. 非法/恶意补丁不泄漏源码或凭据；错误细分不改变原子写入、审批与副作用判断。（B1）
5. Git 根探测不确定、调用期间取消、cleanup_failed、普通 git 命令：不能借能力缺失分支降级。（B2）

## 5. 文件职责

| 单元 | 文件 |
| --- | --- |
| A1 数据与提示 | 修改 `src/tricoder/evals/models.py`、`loader.py`、`service.py`；新增 `src/tricoder/evals/prompts.py` |
| A1 题库 | 新增 `evals/quality-v2/`、其 README 和实验模板；原 v1 不改 |
| A2 诊断 | 修改 `src/tricoder/evals/scenarios.py`、`models.py`、`service.py`、`report.py`；必要时兼容 `compare.py`；faults.py 只允许新增证据类型映射，不改注入执行 |
| B1 错误与补丁 | 修改 `src/tricoder/execution_state.py`、`models.py`、`patches.py`、`tools/write.py`；仅在确需接线时改协议/审计调用点 |
| B2 Git | 修改 `src/tricoder/tools/command.py`，不重构整个 tools registry |
| 新测试 | `tests/test_eval_public_contract.py`、`tests/test_eval_quality_v2_suite.py`、`tests/test_eval_diagnostics.py`、`tests/test_patch_feedback.py`、`tests/test_git_diff_availability.py` |
| 既有回归 | 对应 loader/service/scenarios/report/faults/compare、patches/tools、Agent termination/cancellation、search timeout recovery 测试 |

不要求以上每个候选文件都修改；实施前确认当前符号及调用点。若并发变化使设计不再适用，报告具体冲突，不覆盖别人的修改。

## 6. A1：公开评测契约

**Interfaces:** `EvalCase.expose_edit_scope`、`build_case_steps(case)`；service 的实际运行入口必须使用该 helper，不能只有孤立单元测试。

- [x] 写失败测试：缺字段保持旧行为；bool 以外输入拒绝；有 steps 不额外插入 task；只有 task 正确包装；控制步骤不动；两轮都收到允许路径且第一轮无 latest 新要求；verifier 内容不进入消息。
- [x] 写集成失败测试：用离线捕获 Provider 走实际 Eval/SessionRuntime，检查收到的 user_turn；不要只检查 helper 字符串。
- [x] 写 v2 契约测试：30=19+11、split/预算/ID 不变、v1 原内容 hash 不变、verifier 与 v1 一致；修正四题的可见语义。19 题新增测试目录/直接 Python 文件通过范围检查，非法源文件、根目录 test 文件、tests/nested/test_x.py、保留 verifier 路径仍失败。
- [x] 运行上述测试确认预期 RED，再实现第 3.1 节；不以修改 hidden verifier 消除 RED。
- [x] 在合成副本运行每题正解/负例 hidden verifier，沿用现有 quality suite 的正反测试思路；新增题库测试应独立可运行，不依赖真实模型或标准答案进入 Agent。
- [x] focused tests GREEN，记录差异与命令。保留旧 quality-v1、regression、smoke loader/评分回归。

## 7. A2：可解释的失败报告

**Interfaces:** 第 3.2 节的 EvalTurnRecord、TrialRecord 附加字段和 `on_user_turn_completed`；旧调用方省略回调行为不变。

- [x] 写失败测试：两轮分别 false/true，整体仍 false，first_failed_turn=1，turn_results 保留两轮；单轮成功无 first_failed_turn；无法归因时 terminal_tool_error=None；可恢复错误后成功不得被记成终止错误。
- [x] 写故障证据测试：provider_transient 只标 counter_only，不能标真实 transport；模拟工具错误标 synthetic_tool_result；未触发时列表为空、recovered=None；混合故障分别标注。
- [x] 写 JSON/Markdown 往返和旧记录读取测试；输入未知 detail/fault ID、原始异常正文或密钥占位字符串不能透传。旧比较器读取缺字段记录不崩溃，不自动改旧得分。
- [x] 运行确认 RED，再接入 service/scenarios/report；保留原 AND、failure_codes 和评分门禁。报告清楚列出总通过、产物正确、完成、范围、清理、逐轮失败及故障证据。
- [x] 跑 focused tests 和旧 report/compare/faults/scenarios 回归 GREEN，保存验证记录。
- [x] **阶段 A 冻结并建立基线：** 保存 A 的源码/题库 hashes；此时禁止先改 Agent 工具。使用第 10 节条件执行 19 次 A 组，保留原生报告与日志。

## 8. B1：补丁说明与结构化错误

**Interfaces:** `ToolError.detail_code`、`tool_failure(..., detail_code=None)`、`PatchError(..., detail_code='patch_invalid_format')`；不改变现有主错误码与恢复决策。

- [x] 写失败测试：旧构造/public_fields 完全兼容；合法 detail 被保留；未知 detail 不透传；detail 不改变 should_stop_task。
- [x] 写补丁测试：公开说明中的合法示例能解析并在合成文本上应用；缺头、坏 hunk、行数不一致、不支持操作、上下文冲突分别产生固定子类型；无部分写入、无虚假 modified_paths。
- [x] 写真实 Agent 离线流程测试：错误补丁→模型可见带子类型的反馈→有效补丁或 edit_file→有效验证→finish。再测试非法路径、审批拒绝、UNKNOWN 不因诊断增强而继续写。
- [x] 审计隐私负例：补丁正文使用 `SYNTHETIC_PRIVATE_MARKER`，模型反馈/日志可以保留固定 detail，不得记录原补丁或异常正文；原有脱敏测试通过。
- [x] 先 RED 后按第 3.3 节最小实现，focused tests 与 patch/工具/执行状态/协议回归 GREEN。不得根据这一步宣称先前 7 题已全部修复。

## 9. B2：Git helper 不可用时继续

**Interfaces:** 仅固定 git_diff 的已确认父仓库分支返回第 3.4 节契约；采用 B1 的 detail_code。

- [x] 写 RED：根在父目录时 diff 执行器调用数为 0，返回 False/EXECUTION_FAILED/REPLAN/git_workspace_unavailable/NONE，无验证证据。
- [x] 写离线 Agent RED：同一任务先 git_diff 不可用，再编辑合成文件、有效检查、finish，最终完成；不要靠换 task/scope 抹掉失败。
- [x] 加正例：根相等且有 diff 正常报告；空 diff 正常报告；支持根身份相等的 worktree；无 Git/无仓库不伪报 clean。
- [x] 加负例：run_command 的父仓库 git 仍 POLICY_DENIED/STOP_TASK；审批拒绝、取消（包括探测返回时取消）、超时、清理失败、UNKNOWN 不进入可用性降级；后续写工具调用计数为 0。探测无法确认不能生成 git_workspace_unavailable。
- [x] 先 RED 后最小改动，focused tests GREEN；原 recoverable search 的延迟恢复和失败停止测试继续通过，确认未重开旧 P2。
- [x] 完成阶段 B 自审并冻结代码；确认 quality-v2 及实验条件与 A 完全一致，执行 19 次 B 组。

## 10. 验证命令与 A/B 实验

使用现有 `.venv/Scripts/python.exe -B`，不安装。以下新增模块由实施任务创建；应按任务逐组运行，首次 RED 不应只是导入/拼写错误：

```powershell
.venv/Scripts/python.exe -B -m unittest tests.test_eval_public_contract tests.test_eval_quality_v2_suite -v
.venv/Scripts/python.exe -B -m unittest tests.test_eval_diagnostics tests.test_eval_loader tests.test_eval_service tests.test_eval_scenarios tests.test_eval_report tests.test_eval_faults tests.test_eval_compare -v
.venv/Scripts/python.exe -B -m unittest tests.test_patch_feedback tests.test_patches -v
.venv/Scripts/python.exe -B -m unittest tests.test_git_diff_availability tests.test_tools tests.test_agent_termination tests.test_cancellation tests.test_search_timeout_recovery -v
.venv/Scripts/python.exe -B -m unittest discover -s tests -v
git diff --check
```

最后运行一次全量回归；有失败需区分本轮新增/既有问题，不能为全绿改不相关模块。若执行环境不支持某平台夹具，明确 skipped，不能当作通过。git diff --check 不覆盖 untracked，新文件另行检查。测试内部的合成夹具不得指向实际项目的 Git 索引或用户文件。

A/B 各自使用新 runtime 子目录，冻结完整 quality-v2 副本；实验清单的 suite 指向其同目录下 suite，符合 loader 路径约束。使用现有命令：

```powershell
.venv/Scripts/python.exe -B -m tricoder eval --experiment <本轮独占目录>/stage-a/experiment.toml --no-color
.venv/Scripts/python.exe -B -m tricoder eval --experiment <本轮独占目录>/stage-b/experiment.toml --no-color
```

两个命令分别在 A2、B2 结束时执行，不应等全部代码改完再连着跑。条件固定：DeepSeek/deepseek-v4-flash、quality only、19 题每题 1 次、seed=23、memory off/off、原 plan_enabled、每 user_turn 8 轮、调度预算 3600 秒、max_trials=19。每份 manifest 仅一个 condition，faults=[none] 由 case 自带故障生效；JSON/TOML 不出现密钥。

每组记录源码、题库和 manifest hashes、实际命令、exit code、19 题逐题结果、token/耗时、fault 是否实际触发、turn 状态和清理状态。预期不是强制 19/19 passed：CLI 退出 1 但 complete=true 且有失败题，可为有效的质量测量。若因配置/网络/预算不完整，保留部分结果，不能冒充完整 A/B。

比较只在同一 v2 题库的 A/B 之间进行；v1→v2 的变化单列为契约变化，不能全算 Agent 提升。单次随机结果只用于初筛，不宣称统计显著或跨模型有效。不能以必达分数指导放宽 verifier。

## 11. 交付与验收清单

- [x] 四项任务分别给出改动路径、RED/GREEN、负例及增量 diff；保存本轮 before，可由 reviewer 精确复核。
- [x] v1 与历史 evidence hashes 未变；v2 的 allowed_changes 和模型可见约束同源，关键语义公开，hidden verifier 不泄漏且未被削弱。
- [x] 保留全部原始失败事实；旧报告兼容，模拟故障不被解释成真实网络机制验证。
- [x] git_diff 不读取父仓库 diff、无副作用时可以继续；普通命令及清理/取消/UNKNOWN 门禁仍严格。
- [x] 补丁诊断无敏感正文，原子性、审批、越界和验证行为保持。
- [x] 提供 A/B 原生 run 路径及对照表；若未能执行明确原因，离线功能验证与模型质量结果分开报告。
- [x] 更新 project.md 与 reviewer handoff 的最新状态：只写“实现完成，待独立复审”，不得自行关闭新增审查项或声称 reviewer 已验收。
- [x] 最终中文报告包含范围、验证、风险、未验证项；不提交/推送，不自动开始后续覆盖扩展。

## 12. 计划自审

A1 对应可见契约和题面歧义；A2 对应多轮混淆与模拟故障证据；B1 对应补丁调用摩擦；B2 对应已复现的 Git helper 中断。五项 Review Focus 均有负例任务。新接口为本文拟定接口，不是当前代码已经具有的功能。长期记忆、真实传输故障及预算优化显式留待后续，避免与本次确定性修订混为一项验收。

## 13. 本轮实施记录（2026-10-19）

状态：**实现完成，待独立复审**；上述设计阶段文字和历史依据保留，新增接口已实现。

- RED/GREEN：A1 RED8个失败子例→51 tests/1 skip；A2 RED4→66 tests/1 skip；B1 RED9个失败子例→152 tests/0 skips；B2校准夹具后RED3→134 tests/0 skips，GREEN均exit0。最终全量1770 tests in 398.367s，exit0，0 failures/errors，13平台skips，12条asyncio慢回调诊断。原P2同任务>=5.2秒延迟恢复、搜索→compileall→finish及未确认清理停止回归均通过。最终git diff --check exit0；新文件另检无尾随空白。

- 真实A/B各19题，均complete=true、exit1（质量失败，非伪造通过）。A完整1/19、产物14/19；B完整1/19、产物18/19。同一冻结quality-v2/manifest，DeepSeek deepseek-v4-flash，seed23、memory off/off、原规划、每user_turn8轮、3600s、max_trials19。A原生run为runtime/evals/20261019t113749.503913z/，B为runtime/evals/20261019t115744.613637z/。逐题、分项、token/耗时、实际故障和逐轮诊断见本轮ab-results.md及JSON，单次不宣称统计显著；v1→v2契约变化不计Agent提升。

- 精确before与哈希、四项diff、冻结源码、实际命令/日志位于runtime/eval-contract-tools-20261019/implementation-f36328bab5/；入口为modification-index.md、verification.md、self-review.md。181份旧证据及154个旧题库文件哈希未变。A1/A2共享文件没有各自时间点快照，分项diff为实际before与A冻结后态之间的逻辑拆分；B1换行归一化的前后hash/AST一致证明单列，未冒充额外功能修复。

- 限制/后续：A/B完整率同为1/19，一题改善、一题退化；产物14→18不能视为统计显著提升。B未调用git_diff，该修复的质量收益本轮未测得，只能引用离线真实Agent与负例；不要据单次分数扩大范围。Git根探测仍沿用既有同步/None回退，本轮未重构不确定探测及资源管理。13 skips、其他平台/模型、真实网络故障、持久记忆/压缩和预算优化未验证；Replan框架继续搁置。配置仅由现有评测加载器在程序内使用，未人工读取凭据正文或用户数据库，未安装依赖。下一步独立复审，不自动继续历史待办。
