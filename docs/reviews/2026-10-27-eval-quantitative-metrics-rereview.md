# Eval 量化指标独立复审（2026-10-27）

**结论：本轮约定范围通过，没有新增阻塞发现。** 这份结论来自 reviewer 本轮代码核查、独立回放与新鲜离线验证，不以实施方测试记录替代验收。没有修改生产代码、测试或题库。

## 实际增量与审查范围

- 以 `runtime/eval-metrics-20261027/implementation-1cefa5ed1d/before/` 为实际基线，不使用 HEAD 代替 before。
- 独立校验 before 916份、after 924份及当前924份，hash全部一致；实际18文件增量（10修改、8新增）符合计划范围。
- 原 Agent/Provider/Engine/Session/process、评分器、全部题库、预算与默认权限未变；本轮核查的 821 份源码/测试/脚本/题库和 19,739 份既有历史证据在审查前后未变。
- 本轮新证据独占目录：`runtime/eval-metrics-20261027/review-20261027/`。旧日志与实施方首轮失败记录保留。

## 代码检查与结论

| 范围及位置 | 独立验收结果 |
| --- | --- |
| `src/tricoder/evals/quantification.py:27`、`:32`、`:209` | 组合估计正确；按condition/case/repetition隔离，quality逐题宏平均；不把多轮当sample。计划缺失/不完整、未知结果、n<k均不凭剩余样本伪造headline |
| `quantification.py:37`、`:101`、`:119` | 失败用量纳入摊销；未知交付不误入失败组或形成确定分母；main/summary独立，缓存不双计，字段缺失与非法值不伪装0；保留provider_reported及billing unknown |
| `models.py:140`、`:153`；`experiment.py:79`、`:202`；`report.py:192`、`:353` | 计划包括error/not_run，可信分组补齐与显式冲突拒绝；可选split/timings兼容旧行；新字段不改旧schema及已有指标口径 |
| `runner.py:129`、`:140`、`:163`、`:217`；`service.py:897` | 新计时仅观察；最终清理计入total，agent包含callback生命周期；原duration位置、错误分类、异常传播和cleanup顺序保持；无新增终止期限或owner行为 |
| `analysis.py:55`、`:132`、`:168`、`:190`；`cli.py:254` | 仅显式输入离线重算；不信任预计算quantification；计划身份/分组冲突拒绝，旧split/阶段时间不猜测；输入边界、固定错误、独占输出及来源hash符合约定 |
| `compare.py:149` | 不同condition名称按case/repetition配对；只有完整相容计划才给新差值；suite/verifier/budget等严格指纹仍拒绝；25项旧比较字段与实际before完全一致 |

未发现需coding session继续修复的本包P1/P2。这里的通过只针对量化指标实现，不表示真实模型质量提升或此前CSV/交付遗留问题已修复。Q3-R1/R2/R3及原搜索超时P2保持关闭；Replan继续搁置。

## 本轮实际验证

1. reviewer独立全量：**2060项，2045通过、15跳过、0失败、0错误，exit0**；unittest 799.824秒，进程墙钟 808.156秒。全部Eval 394项包含在此次全量中，没有重复相加为更多独立测试；skip名单见`skips.json`。
2. reviewer自编9项探针全部通过：n=1..12各c/k枚举所有k子集，共1,456次公式对照；81种三值维度组合×3状态判定；条件/quality隔离、宏平均、取消/缺项、token摊销、summary三条件、非法JSON/计数、严格比较以及历史逐行oracle。见`independent_probes.py`、`independent-probes.log`。
3. 实际before/current各7场景、共14次生命周期回放：正常、准备错误、executor错误、verifier安装错误、最终清理错误、KeyboardInterrupt、async cancellation；除新增timings外原结果、legacy duration、事件顺序、异常类型完全一致。使用亚毫秒余量时，正常total94=agent31+verifier18+harness45。见`timing_differential.py`与两侧JSON。
4. 历史报告在实际before/current自比较，25个旧字段完全一致；新pass/token差值在无计划时为null/missing_plan。见`legacy_compare_differential.py`。
5. 独立CLI dry-run：baseline/repeat3/dev/holdout为30/90/22/8，全部exit0。没有执行90次真实采样。
6. 独立CLI分析2026-10-21旧result+plan：产物29/30、完整27/30，main token1,414,748，每完整成功摊销52,398.0741；@2/@3/^2/^3为insufficient_repetitions，split unknown、阶段时间null。逐行oracle与CLI一致，来源hash未变。新派生输出：`runtime/evals/analyze-77fa714053604710bbe76565eae00b3a/analysis.json`。
7. 原搜索P2至少5.2秒等待后搜索/compile/finish恢复、清理未确认保留exact owner、取消/已有期限/普通命令及验证门禁相关测试随全量通过。新增计时专项也包含在全量中。
8. 变更Python AST、`git diff --check`、实际增量及前后hash保全均通过；精确证据见`verification.json`和`baseline-verification.json`。

## 保留的风险与未验证范围

- 实施方首轮全量在未修改的 `tests/test_verification_evidence.py:1181`（前置检查失败，随后Windows临时锁清理错误）有一次非稳定失败。本轮独立全量中该测试通过；**根因仍未定位，不能把通过重跑写成已修复或证明与本包无关**。当前没有可稳定复现且能归因到此增量的阻塞证据；若再次发生，应记录实际RunResult/stop_reason和锁所有权后另行诊断。
- 15项平台/符号链接权限skip未验证；只验证当前Windows/Python环境。没有做其他平台、真实模型、真实90次重复、货币费用、请求级账单完整性或统计显著性验证。
- 历史30题只是只读重算，不新增模型样本；不能推断pass@3，holdout也不重新变成作者盲测。Provider用量仍可能缺少失败请求，字段齐全不代表账单齐全。
- 未读取真实凭据、`.env.local`或用户数据库，未安装、提交/推送。测试使用既有离线fake/隔离合成资源。

## 交接

量化指标本包独立复审完成。可使用新离线分析入口；若要真实30×3基线，按既有清单冻结源码/题库/模型/预算后另获用户授权执行。本轮不自动跑真实模型、不推进历史Agent修复。`project.md`和reviewer交接已新增本结论，旧正文完整保留。
