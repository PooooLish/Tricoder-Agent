# Eval R1/R2 独立复审（通过）

日期：2026-10-19。范围：`docs/superpowers/plans/2026-10-19-eval-review-r1-r2-fixes.md` 对应实现；沿用 reviewer 角色，不代为修改生产代码。

## 结论

**本次范围通过，R1/P2、R2/P3 均关闭，未发现新的阻塞问题。** 原搜索超时 P2 回归仍通过，维持关闭。此结论确认确定性评分/报告修复，不代表真实模型完成率提升或整个项目无缺陷。

## R1：根匹配与旧规则兼容

- `src/tricoder/evals/models.py:74` 添加默认 legacy 的 path_match_mode；loader 仅在 schema 2 接受新字段，省略时保持旧行为。
- `src/tricoder/evals/runner.py:333`、`:336` 分别将模式传给 allowed 和 required 判定。workspace_root 使用规范相对路径、同段数、逐段 fnmatchcase；legacy 保留原 PurePosixPath.match。模式非法时不回退，保留目录仍被拒绝。
- reviewer 使用本轮实际 before 中的 runner/题库与当前 runner/题库对照，每种模式都走真实 `run_isolated_case` 的快照、隐藏验证和评分；不使用实施方评分汇总替代复现。

| 同样正确修复 subtract 后新增的文件 | before | 当前 |
| --- | --- | --- |
| tests/test_review.py | passed | passed |
| tests/tests/test_review.py | passed（原漏判） | change_out_of_scope |
| other/tests/test_review.py | passed（原漏判） | change_out_of_scope |
| tests/nested/test_review.py | change_out_of_scope | change_out_of_scope |

8 个真实隔离评分场景均核对了实际 modified_files，隐藏验证全部通过，因此上述负例失败确实来自范围判定。另有 2 个合成 required 场景：只修改 extra/math_ops.py 不能满足根模式的 math_ops.py 必需修改，返回 required_change_missing；legacy 仍保留旧后缀行为。

新题库 19 quality 题的 case.toml 唯一语义增量为 path_match_mode=workspace_root；11 contract 定义保持字节不变。benchmark_version 更新为 quality-v2.1，suite id/目录仍是 quality-v2，旧冻结题库保持 quality-v2。

为支持方案指定的点分版本，loader 增加独立 benchmark 版本校验，保留 64 字符上限，suite/case ID 校验不放宽。这是必要的接口修正；格式正反例、非法新路径/模式、大小写、旧 globstar、旧模式及跨版本比较拒绝均有本轮实际运行的回归覆盖。

## R2：诊断身份与数据不变

`src/tricoder/evals/report.py:757-770` 的 User turns、Fault evidence 两表均从已验证 TrialKey 读取 Condition、Case、Repetition；旧 unknown 行也携带身份。

reviewer 独立构造同题的 review-a/第2次（两轮）、review-b/第1次（一轮且无故障）、review-a/第3次（旧诊断缺失、有故障 ID）三条记录，对照 before/current renderer：

- 当前 4 条逐轮/unknown 行均带正确完整身份；before 只有题目名。
- 故障区仅有 2 条实际已触发记录，均能归属；无故障 trial 不造行，旧记录保留“证据类型未知”。
- before/current 的 `experiment_report_as_dict` 结果完全相等，修复只改变可读报告，不改 JSON、指标或分数。

## 独立验证与证据

新证据单独保存于 `runtime/eval-review-r1-r2-20261019/review-20261019/`，未覆盖旧日志。

1. **147 项相关 unittest，91.820 秒，退出 0：146 通过、1 skipped，0 failures/errors。** 跳过为当前 Windows 账户不能创建符号链接，不计通过。完整命令含 19 个模块，见 `test-command.json`、`tests.log`。覆盖路径/loader/评分/public contract/v2、诊断/报告/比较、相关 Eval 链路及既有补丁、Git、搜索超时回归。
2. `probes.py`：上述 8 个 before/current 实际评分、2 个 required 评分及多 trial 报告对照，退出 0。详见 `probes.json`、`report-before.md`、`report-current.md`。
3. `audit.py`：独立核对 531 份 before 内容与其 hash、34 项实际增量及 after hash。生产代码仅变更 loader/models/prompts/report/runner 五个 Eval 文件，未改 Agent/Provider/工具或清理机制。
4. 核对 before 清单中的 149 份旧 quality-v1/regression/smoke 文件、66 份 v2 workspace/verifier 文件和 11 份 contract 定义不变；19 quality 定义仅增加模式字段。核对 1,086 份已记录历史证据 hash 一致。旧 v1/A/B 原生结果各 19 条仍可读取，通过数保持 6/1/1。见 `integrity-audit.json`。
5. reviewer 开始/结束的 479 个源码、正式测试、题库 .py/.toml 文件 hash 一致，未改生产代码/题库；`git diff --check` 退出 0。见 `reviewer-integrity.json`、`diff-check.log`。

## 未验证范围与下一步

本轮没有新增真实模型调用，没有重跑项目全量、其他平台/Python 或长期记忆/网络故障实验。评分快照仍为普通文件级，没有新增目录/空目录审计；该限制已明确写入方案和文档，不作为本轮已实现能力。

未读取真实凭据或用户数据库，未安装依赖、提交/推送或启动子 Agent。当前 R1/R2 无剩余阻塞，等待用户指定后续范围；不自动推进 Replan、预算优化或历史待办。
