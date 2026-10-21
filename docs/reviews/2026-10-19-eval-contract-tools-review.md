# 评测契约与工具反馈独立复审

> 后续状态：本报告的 R1/P2、R2/P3 已在独立复审中关闭，见 `2026-10-19-eval-r1-r2-rereview.md`。下文保留原发现、复现与当时“暂不通过”结论，不改写历史证据。

日期：2026-10-19。对象：`docs/superpowers/plans/2026-10-19-eval-contract-and-agent-tools.md` 的 A1/A2/B1/B2 实现。

## 结论

**暂不通过：1 个 P2 阻塞发现，另有 1 个 P3 报告问题。** 补丁错误反馈和固定 Git helper 的改动在本次核查范围内通过；v2 的范围评分还不能完整兑现公开契约。旧搜索超时 P2 的延迟恢复和清理失败停止回归仍通过，维持关闭。

当前结果不是“实施方 1770 测试通过，所以验收通过”。本 reviewer 独立运行相关测试并新增探针，测试结果和发现分别记录如下。

## 发现

### R1 / P2：嵌套同名 tests 目录绕过 v2 的直接子文件限制

位置：`evals/quality-v2/cases/sf-subtract/case.toml:5`（19 道 quality 题均添加相同的 `tests`、`tests/*.py` 模式）；`src/tricoder/evals/prompts.py:14-15` 明确禁止嵌套目录。实际匹配依赖未修改的 `src/tricoder/evals/runner.py:349` 的 `PurePosixPath.match`。

`PurePosixPath('tests/tests/test_probe.py').match('tests/*.py')` 返回 True：这个模式是后缀匹配，不是从工作区根锚定的两段路径。独立通过真实 `run_isolated_case` 的快照、hidden verifier 和评分流程验证：

| 同样正确修复 subtract 后添加的文件 | 实际评分 |
| --- | --- |
| tests/test_probe.py | passed，正确正例 |
| tests/nested/test_probe.py | failed / change_out_of_scope，普通嵌套负例 |
| tests/tests/test_probe.py | **passed，违反公开的禁止嵌套契约** |

第三组的 modified_files 明确含 `tests/tests/test_probe.py`，没有依赖模型自报路径或省略变更。没有真实模型调用；使用受控 executor 创建合成产物，让实际评分器决定结果。

影响：违反题面范围的产物仍可能被计为 scope_compliant/完整通过。新测试覆盖了普通 nested 子目录，却漏掉尾部仍能匹配 tests 的目录。原方案也低估了既有 matcher 的后缀语义；不能简单归因于实施方未遵守方案。

建议：为新公开契约明确工作区根锚定的路径语义，保证 tests 下只允许一个文件名分量；增加 `tests/tests/test_x.py`、`other/tests/test_x.py` 和正常直接子文件的真实评分回归。必须保留旧 v1/历史规则兼容，不全局悄悄改变已有 benchmark，不通过删除题面限制或放宽允许范围解决。此项修复后需复审。

### R2 / P3：新诊断表缺失 condition/repetition，重复实验无法明确归属

位置：`src/tricoder/evals/report.py:757-770`。

新增 User turns 和 Fault evidence 两个 Markdown 表只写 case_id。相同题目在不同 condition 或不同 repetition 下会产生同名行；一条 trial 可有多个 turn，且只有触发故障的 trial 才生成故障行，因此不能可靠地通过行位置与上方 Trials 表对应。

独立构造 `(alpha, sf-subtract, 1)`、`(alpha, sf-subtract, 2)`、`(beta, sf-subtract, 1)` 三条记录，分别带不同终止原因。实际报告的诊断区只出现三行 sf-subtract，完全没有 alpha/beta 或 repetition 身份，故障表也一样。JSON 的完整 TrialKey 仍在，不属于数据丢失；问题在新增的人读诊断视图。

建议：两个表及旧记录的 unknown 行都展示 Condition、Case、Repetition（或等价完整 TrialKey），补多条件/多重复/多轮/未触发故障的渲染测试。不是本次单条件、单重复 A/B 结果数字的错误。

## 通过与保留边界

- A1：字段真实 bool 校验、默认旧提示不变、实际 Eval→SessionRuntime 接线、steps 优先、后轮 latest 不提前泄漏、v2 四题需求补足通过相关验证；范围判定存在 R1。
- A2：逐轮失败保留、首失败轮、整体 AND、故障未触发 unknown、counter_only/synthetic_tool_result 区分、旧结果读写通过相关验证；Markdown 诊断存在 R2。
- B1：错误子类型白名单、原构造兼容、有效示例、补丁原子预检/审批及脱敏反馈通过相关测试；修改未扩大解析语法。真实 B 审计仍出现 9 次补丁参数错误（6 次 count_mismatch、2 次 invalid_hunk、1 次 invalid_header），不能称为模型补丁错误已全部解决。
- B2：独立在项目仓库下的新合成工作区做真实 Git 根探测，并用 before 文件加载旧 GitDiffTool 对照。before 为 POLICY_DENIED/STOP_TASK；当前为 EXECUTION_FAILED/REPLAN/git_workspace_unavailable/NONE，diff 子进程均未启动。当前同一真实 CodingAgent 使用离线队列 Provider，依次 git_diff→edit_file→compileall→finish，4 次响应后成功；普通 `run_command git status` 在同样父仓库条件下仍首轮停止，后续编辑未执行。没有修改生产源码来完成对照。
- 取消、UNKNOWN、普通命令门禁和清理失败仍由相关负例验证。Git 根探测的既有同步子进程/None 回退未在本轮重构；这不属于新增修复保证。

## 独立验证

证据根：`runtime/eval-contract-tools-20261019/review-20261019/`，所有新文件单独保存，不覆盖实施或旧审查日志。

1. `run_checks.py` 实际运行 16 个相关 unittest 模块，**Ran 154 tests in 85.796s，退出 0，0 failures/errors，1 skipped**。跳过为当前 Windows 账户不能创建符号链接；跳过不算通过。完整命令/模块见 `focused-command.json`，日志见 `focused-tests.log`。
2. `independent_probes.py` 经过实际隔离评分器的 3 个路径场景与多 trial 报告渲染，退出 0，确认 R1/R2。结果在 `independent-probes.json`，可读报告在 `multi-trial-report.md`。探针退出 0 表示观察符合所写复现断言，不表示产品不存在缺陷。
3. `tool_probe.py` 在真实父仓库环境核对 before/current 工具结果及同任务恢复/普通命令停止，退出 0，见 `tool-probe.json`。
4. `evidence_audit.py` 独立验证保存的 before 内容哈希、A/B 源码冻结哈希、A/B 题库与清单逐字一致，当前 117 个生产源码文件与 B 冻结一致。A→B 仅 5 个计划内工具相关源码变化；154 个旧题库文件保持原 hash；旧原生 19 条结果可读取，仍保留 6 passed。结果见 `evidence-audit.json`。
5. reviewer 开始/结束的 478 个源码、正式测试和题库 .py/.toml 文件哈希一致；`git diff --check` 退出 0。未修改生产代码或题库。

## A/B 记录核验及其含义

本次未重新调用真实模型；下面是对已有原生 results 和审计的独立重读，不是新一轮质量实验。

| 指标 | A | B |
| --- | ---: | ---: |
| trial 数 | 19 | 19 |
| 完整通过 | 1 | 1 |
| 产物正确 | 14 | 18 |
| 现有评分报告范围合规 | 19 | 19 |
| 报告清理确认 | 19 | 19 |
| user_turn 数 | 22 | 22 |
| 失败且达到第 8 轮的 turn | 13 | 17 |
| 终止工具错误 policy_denied 的 turn | 7 | 2 |
| git_diff 调用数 | 1 | 0 |

B 的完整交付能力仍未通过大多数题；其中 17 个失败 turn 达到轮次上限，另 2 个被 policy_denied 停止。此表描述观测，不断言所有达到第 8 轮的失败仅由预算不足引起。B 未调用 git_diff，不能用 B 成绩证明该修复在真实模型运行中的收益；目前该分支的修复证据来自独立离线 Agent 探针。A/B 单次、单模型，不做显著性推断。

R1 并不自动推翻已有 A/B 的每个 scope_compliant 数字，也不允许回写旧分数；它证明判分规则存在可复现的漏判输入，需要修复并补测试。

## 未验证与下一步

没有重新运行实施方全量 1770 测试，没有新增真实模型调用、跨平台/Python/Provider 验证，没有检验真实网络故障、持久记忆/压缩、统计稳定性或预算优化。不读取真实凭据/用户数据库，不安装依赖、提交或推送。

下一步只处理 R1、R2 并交独立复审；不因完整通过率低自动推进 Replan 或扩大范围。本报告不实施修复。
