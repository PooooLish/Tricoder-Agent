# Eval 复审 R1/R2 最小修复实施方案

> **For agentic workers:** 使用 `superpowers:executing-plans` 在用户交付的 coding session 逐项实施，不启动并行子 Agent。遵守保留脏工作树、不提交/推送的当前约束，不执行技能中的默认提交步骤。

**Goal:** 关闭 R1/P2 的公开文件范围漏判与 R2/P3 的诊断 trial 身份缺失，同时保持旧 benchmark 语义和历史结果不变。

**Architecture:** 为 EvalCase 添加显式、默认兼容的路径匹配模式，只让修订后的 quality-v2 质量题选择工作区根匹配。允许路径与必需路径采用同一模式；Markdown 诊断直接展示已有 TrialKey，不修改结果数据结构或评分。两个任务分别验收，不重新执行上轮 A1/A2/B1/B2。

**Tech Stack:** Python 3.11+、标准库 fnmatch、unittest、现有 TOML/报告链路；无新依赖。

**Spec:** `docs/reviews/2026-10-19-eval-contract-tools-review.md` 的 R1/R2，加本文第 1～3 节的确定设计。原方案 `2026-10-19-eval-contract-and-agent-tools.md` 仅作背景；冲突处以本修复方案为准，尤其不能继续假定 PurePosixPath.match 是根锚定匹配。

## 实施状态（2026-10-19）

**R1/R2实现完成，待独立复审。** 下列勾选表示 coding 实施与本轮离线验证完成，不代表 reviewer 关闭 R1/P2 或 R2/P3；原“暂不通过”报告保留。

证据：`runtime/eval-review-r1-r2-20261019/implementation-bfbab00786/`，见 verification.md、self-review.md、final-delta.diff、final-audit.json。R1 最终 16、R2 18、相关 Eval 97、工具/P2 16 项，均 exit0；合计 147 项，1 个 Windows 符号链接权限 skip、0 failures/errors。真实评分与多 trial 报告另存独占目录，旧 A/B 未运行/未改写。

实施裁定：实际 benchmark_version 校验复用 dot-free ID 正则，不能承载方案指定 quality-v2.1。已先保存加载失败及新增版本正反例 RED，再增加 benchmark 专用点分段校验（64 字符上限）；suite/case ID、比较器、旧版本合法值不变。首次 GREEN 的 6 errors 保留在 r1-green.log，不冒充通过结果。

执行技能中的默认提交、工作树迁移、子 Agent 和删除过程证据步骤按用户明确限制不执行；本轮按实际 dirty tree before、自审与独立日志交付。目录级快照/真实模型/全量回归仍不在本轮验证范围。

## 1. 已确认问题与范围

R1 已经过真实 `run_isolated_case` 复现：同样正确修复 subtract 后，`tests/test_probe.py` 通过、`tests/nested/test_probe.py` 被拒绝，但公开禁止的 `tests/tests/test_probe.py` 仍完整 passed。原因是 `PurePosixPath.match('tests/*.py')` 可以匹配路径后缀。原方案也低估了此语义，本轮应修正契约设计，不仅添加某个目录名的黑名单。

R2 已经过实际报告渲染复现：同一题在 alpha/第1次、alpha/第2次、beta/第1次下的逐轮及故障信息仅显示题目名，不能明确归属。JSON 已保存完整 TrialKey，问题仅在 Markdown 展示。

本轮仅修这两项。不得改 Git helper、补丁解析、Agent 执行、恢复/取消/清理、Provider、长期记忆、轮次预算或 Replan；不追求通过率提升。不新增真实模型调用，旧授权不需要重新询问，但这两个确定性缺陷没有重跑模型的必要。

## 2. Global Constraints

- 工作区：`D:\MaHong\AGENT_WORKSPACE_V2\projects\tricoder-cli`。先读 `../../AGENTS.md`、项目 `AGENTS.md`、README、pyproject、project.md、复审报告及本文。
- 当前工作树包含多轮未提交/未跟踪内容，HEAD 不是 before。保存本轮涉及文件的实际内容和 SHA-256、tracked/untracked 状态到独占的 `runtime/eval-review-r1-r2-20261019/<新标识>/before/`；不要复制整个项目、凭据或数据库。
- 保留全部现有修改。不 reset/clean/覆盖，不从 HEAD 重建，不强制切换到缺少未提交内容的 worktree；不提交/推送项目，不安装依赖，不创建会话或子 Agent。
- 不读取 `.env.local`、真实密钥正文或用户数据库。本轮仅使用合成 fixture、离线 Provider 和已脱敏的历史评测结果。
- `evals/quality-v1/`、`evals/regression-v1/`、smoke、旧 runtime 证据与冻结 A/B 副本保持字节不变。尤其保留 `runtime/eval-contract-tools-20261019/implementation-f36328bab5/` 和 `review-20261019/`。
- 不放宽测试、不删除公开范围限制、不回写历史分数，不以题目 ID 或目录名黑名单硬编码绕过问题。
- 每项先 RED 再最小实现/GREEN；RED 应是行为断言失败，不只是导入错误。实施结论只写“已修复实现，待独立复审”，不替 reviewer 关闭发现。

## 3. 确定设计

### 3.1 显式路径语义与版本

在 `src/tricoder/evals/models.py` 定义：

```python
PathMatchMode = Literal["legacy", "workspace_root"]
# 添加到 EvalCase 末尾，保留既有位置参数兼容：
path_match_mode: PathMatchMode = "legacy"
```

loader 仅在 schema 2 接受可选 `path_match_mode`，只接受精确字符串 legacy/workspace_root；缺字段为 legacy。schema 1 仍拒绝未知字段。不得根据 expose_edit_scope、suite ID、case ID 或当前目录隐式切换匹配规则。

当前 `evals/quality-v2/` 中 **19 道 quality** 题明确添加 `path_match_mode = "workspace_root"`；11 道 contract 的 case.toml 保持原字节。保留全部 ID、split、task/steps、allowed_changes、required_changes、预算、workspace/verifier；不再复制出另一整套 30 题。

将该工作目录下 suite.toml 的 `benchmark_version` 更新为 **quality-v2.1**，suite `id` 仍为 quality-v2，schema_version 仍为 2。在 README 解释：目录/ID 保持稳定，评分修订由 benchmark_version 区分；旧冻结副本仍为 quality-v2。更新当前相关说明和版本断言，不修改旧记录。比较器应沿既有逻辑拒绝 quality-v2 与 quality-v2.1 的严格配对比较，不为本修复放宽该检查。

### 3.2 匹配规则

保持现有 helper 名称和旧调用默认行为，增加 keyword-only 模式：

```python
def _path_matches(path: str, pattern: str, *, mode: PathMatchMode = "legacy") -> bool: ...
def _path_is_allowed(path: str, patterns: tuple[str, ...], *, mode: PathMatchMode = "legacy") -> bool: ...
```

在 `_score` 中，allowed_changes **和 required_changes** 两处都传 `case.path_match_mode`。保留保留目录检查及其他评分项；诊断、mode 不改变 Agent 权限。

- legacy：逐字保留原 `PurePosixPath(path).match(pattern)` 行为，不加全局新限制，不改变旧题或直接调用 helper 的默认语义。
- workspace_root：按 `/` 分段，路径和模式段数必须相同；对应段使用标准库 `fnmatch.fnmatchcase` 比较，通配符不得跨 `/`。大小写敏感，在 Windows 上也不得使用会隐式 normcase 的 fnmatch.fnmatch。不使用字符串 endswith 或不分段的 fnmatchcase(path, pattern)。
- 新模式接受每段内的 `*`、`?`、字符组；本次不支持递归 globstar。仅对 workspace_root，loader 对 allowed/required 中任何包含 `**` 的模式报 EvalDefinitionError，不退回 legacy。旧模式的 `**` 行为不变。
- 新模式要求规范的相对 POSIX 路径/模式：拒绝绝对路径、反斜杠、驱动器/冒号、空段、`.`、`..`，不先规范化消掉问题再放行。loader 对新模式非法模式报定义错误；matcher 对非法输入路径返回 False；未知 mode 拒绝，不能静默 fallback。
- `_path_is_allowed` 继续优先拒绝框架保留路径，即使模式看似能匹配。不得令通配符匹配隐藏 verifier 或控制目录。

关键预期：

| path | pattern | workspace_root | legacy |
| --- | --- | --- | --- |
| tests/test_x.py | tests/*.py | True | 保持原 True |
| tests/tests/test_x.py | tests/*.py | False | 保持原 True |
| other/tests/test_x.py | tests/*.py | False | 保持原 True |
| tests/nested/test_x.py | tests/*.py | False | 保持原 False |
| tests/tests | tests | False | 保持原 True |
| math_ops.py | math_ops.py | True | 保持原 True |
| other/math_ops.py | math_ops.py | False | 保持原 True |

`build_case_steps` 在 expose_edit_scope=true 且 mode=workspace_root 时补充固定句：“上述路径模式均从工作区根匹配；通配符只匹配单层名称，不包含任意父目录前缀。”已有业务需求、每轮范围注入、latest 后轮修正保持。旧模式提示保持原样，不能为根匹配补一句话却仍走 legacy 评分。

**边界：** 目前 capture_snapshot/changed_paths 记录普通文件，不是完整目录审计。本轮修复文件路径判分，不扩展 FileFingerprint 或把空目录行为变成新的验收项；不得拿 `_path_is_allowed('tests')` 的单元测试声称已经验证目录快照。README/交付报告如实说明此边界，实际漏判回归必须包含落盘文件。

### 3.3 诊断表身份

只修改 `_render_experiment_markdown` 的 User turns、Fault evidence 两节。新增列顺序：

```text
User turns: Condition | Case | Repetition | Turn | OK | Verification | Rounds | Tools | Terminal tool error
Fault evidence: Condition | Case | Repetition | Triggered fault | Evidence kind
```

每行的前三列直接来自经 `trial_record_from_dict` 验证后的 `safe.key.condition_id/case_id/repetition`；不从顺序推测、不拼接自由文本。

- 有 turn 的每行、旧记录缺 turn_results 的 unknown 行都携带完整身份。
- 有故障时每行携带身份；旧记录有 triggered_faults、无 fault_evidence 时继续显示“证据类型未知”，同时携带身份。
- 无故障仍保持原行为：不伪造故障行。不要为了表格齐全把无故障写成故障证据未知。
- JSON schema、TrialKey、原始事实、指标与分数不改。保留脱敏/白名单，不输出模型内容或异常正文。

## 4. Review Focus

1. 目录重名及任意父前缀不能穿透根匹配；正常直接测试文件仍通过。（任务一：真实隔离评分）
2. allowed 与 required 都必须传入同一模式，不能“范围严格了，但必需修改仍可由同名后缀满足”。（任务一：required 负例）
3. 缺字段、schema 1、旧 `**`、旧冻结 suite 仍按旧规则执行；不借修复改旧成绩。（任务一：兼容与版本）
4. Windows 大小写和反斜杠不能导致匹配行为随平台变化；非法新模式 fail closed。（任务一：匹配/加载器）
5. 多 condition×repetition×turn、旧 unknown、未触发故障必须能归属且不造假。（任务二：表格行断言）

## 5. 任务一：R1 根匹配与兼容验收

**Files:** 修改 `src/tricoder/evals/models.py`、`loader.py`、`runner.py`、`prompts.py`；修改 `evals/quality-v2/suite.toml`、19 quality case.toml 和 README。新增 `tests/test_eval_path_matching.py`；更新 `tests/test_eval_quality_v2_suite.py`、`test_eval_public_contract.py` 中相关断言，必要时补现有 loader/compare 测试。

**Interfaces:** 第 3.1/3.2 节的字段和 helper 签名；不改既有数据结构位置参数顺序。既有 v2 范围测试调用 helper 时要明确传 case.path_match_mode，避免继续测默认 legacy。

- [x] 保存本轮 before/hash；在未修改源码前，用复审探针等价场景确认 tests/tests/test_x.py 的旧 false-pass，并保存独立 RED 日志。原 reviewer 脚本/证据不覆盖。
- [x] 编写 matcher/loader RED：覆盖上述表、`test_x.py` 根目录拒绝、大小写差异、非法路径、新模式 `**` 拒绝、legacy `**` 保持、未知 mode/非字符串拒绝、schema 1 不接受新增字段。
- [x] 编写真实评分 RED：通过 `run_isolated_case` 创建正确 subtract 产物，并分别落盘 tests/test_x.py、tests/tests/test_x.py、other/tests/test_x.py、tests/nested/test_x.py。前者通过，后三者必须包含 change_out_of_scope；验证 modified_files 确实包含目标文件，不仅测 helper 或模型自报变更。
- [x] 编写 required RED：用合成 case 的 required_changes=("math_ops.py",)，只修改 other/math_ops.py；在 workspace_root 模式中应有 required_change_missing。允许范围可在该合成测试专门允许 other/math_ops.py，以隔离 required 判定；旧模式保持既有后缀行为。不要修改真实题目的 hidden verifier 来造此测试。
- [x] 实施模式、loader 校验、评分双处接线、提示与 benchmark 修订；不扩大真实 allowed_changes。
- [x] 更新/运行根匹配、public contract、quality-v2 suite 测试 GREEN；复核 19 quality 显式 opt-in、11 contract 不变、所有 workspace/verifier 字节不变。
- [x] 运行 legacy 回归：读取旧 v1 和冻结 quality-v2，mode=legacy；旧报告仍可读取且旧分数不变；quality-v2/quality-v2.1 严格比较被拒绝。同版本正常比较仍工作。

## 6. 任务二：R2 可归属诊断表

**Files:** 修改 `src/tricoder/evals/report.py`；扩展 `tests/test_eval_diagnostics.py`，必要时 `tests/test_eval_report.py`。无需改 service、scenarios 或 TrialRecord。

**Interfaces:** 第 3.3 节的两个 Markdown 表列；只消费现有 TrialKey，JSON 输出内容不变。

- [x] 编写 RED：同题至少三个 trial：alpha/1 有两轮、alpha/2 一轮、beta/1 无 turn_results；逐条检查诊断行的 Condition/Case/Repetition，而不是仅断言文档某处存在 alpha。
- [x] 故障 RED：alpha/1 有 counter_only，alpha/2 没有触发故障，beta/1 是旧记录（有 triggered_faults、无 fault_evidence）。只应有两条故障行，身份分别正确，旧记录显示未知；alpha/2 不凭空出现。
- [x] 断言新渲染前后的 JSON 结果、TrialKey、metrics/failure_codes 不变；unknown 行也含完整身份，Markdown 列数一致，固定字段仍经安全读取器校验。
- [x] 最小修改两个表，运行 GREEN 和旧报告/比较测试；保存示例报告到本轮新 evidence 目录，不覆盖旧原生报告。

## 7. 验证与交付

使用已有解释器；按任务先聚焦、后相关回归，不安装依赖：

```powershell
.venv/Scripts/python.exe -B -m unittest tests.test_eval_path_matching tests.test_eval_public_contract tests.test_eval_quality_v2_suite -v
.venv/Scripts/python.exe -B -m unittest tests.test_eval_diagnostics tests.test_eval_report -v
.venv/Scripts/python.exe -B -m unittest tests.test_eval_loader tests.test_eval_runner tests.test_eval_service tests.test_eval_scenarios tests.test_eval_faults tests.test_eval_compare tests.test_eval_metrics tests.test_eval_experiment tests.test_eval_smoke_suite tests.test_eval_regression_suite tests.test_eval_regression_contracts -v
.venv/Scripts/python.exe -B -m unittest tests.test_patch_feedback tests.test_git_diff_availability tests.test_search_timeout_recovery -v
git diff --check
```

以上以实际运行结果计数，记录 exit code、failures/errors/skips；因 Windows 权限跳过不能算通过。检查 untracked 新文件格式，git diff --check 不覆盖它们。改动限定于 Eval，无需默认再跑全部 1770+ 测试或真实 A/B；若发现更广的调用影响，再有针对性扩展测试，并说明原因。

交付要求：

- [x] R1/R2 分别提供精确 before、增量 diff、RED/GREEN、实际命令与新报告；新日志保存在 `runtime/eval-review-r1-r2-20261019/<新标识>/`。
- [x] 列出当前源码/题库允许变更，证明旧题库、历史 runtime/冻结结果和 v2 workspace/verifier 未改；旧代码缺 mode 的语义不变。
- [x] 明确 R1 的三类嵌套/前缀输入都被拒绝，正常直接文件通过；required 接线被独立负例覆盖。
- [x] R2 的完整身份覆盖每种诊断行，未触发不造假，旧记录兼容；不改旧分数或宣称模型质量提升。
- [x] 更新 project.md 和 reviewer handoff 最新条目，状态写“R1/R2 实现完成，待独立复审”；保留本次未通过的复审记录，不自行宣布关闭。
- [x] 中文汇报变更、验证、限制和未验证范围；完成后停止，不提交/推送，不继续其他历史任务。

## 8. 计划自审

R1 通过显式 opt-in、根锚定逐段匹配、双处评分接线和 benchmark 修订解决，不用新规则改旧成绩。R2 使用已有 TrialKey，不另造数据字段。五个 Review Focus 均有具体验证；目录级快照、Agent 工具与真实模型重跑明确不在本轮范围。本文接口均为拟新增接口，不表示当前实现已具备。
