# Eval 量化指标

本模块只统计既有 Eval 事实，不改变评分、Agent、权限、预算或完成门禁。一次 sample
是从独立工作区/Session 开始的一整题执行；多 user_turn、模型轮次和 Provider retry
均不增加 sample 数。真实重复实验需要另行授权。

## 成功口径与分母

唯一身份为 `(condition_id, case_id, repetition)`；重复身份、未知条件、计划外行和
元数据冲突拒绝解析。只有 `execution_kind=quality` 进入新成功指标，contract 继续保留
原有 expectation_met。每个 condition 独立输出 all_quality、by_split、by_category。

- 产物成功：`artifact_correct is True`。
- 完整交付：artifact_correct、agent_completed、scope_compliant、cleanup_confirmed
  的三值 AND；任一 False 即失败，全 True 才成功，否则未知。
- 已启动的 error 行按保守端到端口径计失败，不能删掉后提高成功率；已有 True 产物
  证据仍计产物成功。这不是仅针对已验证代码的正确率。
- quality 的 status=passed 却有四维 False 是矛盾输入；status=passed 且事实不足
  保持未知。status=failed 但四维全 True 可来自额外 assertion，不覆盖原始 status。
- cancelled、not_run、缺计划行会使所属 condition 的成功指标不可用。顶层
  complete=False 且无法归属具体缺项时，保守阻塞全部条件。

每题有 n 个样本、c 次成功时：

```text
pass@k = 1 - C(n-c,k) / C(n,k)
pass^k = C(c,k) / C(n,k)
```

上式约定上标数小于 k 时组合数为 0。默认 k 为 1/2/3，可指定 1..100 的整数，排序
去重。n<k 时不可估计。先逐题估计，再对题等权宏平均，不能按 trial 加权。
组内任一计划题样本不足，该 k 整组不可用；不得只选择 eligible 子集。

手算例 n=3,c=2：@1=2/3、@2=@3=1；^1=2/3、^2=1/3、^3=0。
其中 pass^k 是 k 次全成功的有限样本估计，不能用总体 pass@1 的 k 次方替代。
原 `stable_cases` 不变，展示为 legacy observed-all-success，不代表固定 k 的可靠性。

`case_results` 保留每题 n_planned、n_started、成功次数、原始未知维度计数，以及
排除 error 保守失败后仍未确定的 unresolved 计数。`success_histograms` 按 n 分桶，
保留 0/n 到 n/n 及 unknown；不完整题不会伪装成 0/n 或稳定成功。

## 字段、兼容与不可用原因

原实验 JSON schema_version=2 和旧指标键不变；追加：

| 字段 | 含义与旧默认 |
| --- | --- |
| TrialRecord.split | dev / holdout / unknown，旧行 unknown |
| TrialRecord.timings | 阶段观测，旧行 null |
| ExperimentRunReport.trial_plan | 全计划身份及 execution_kind/category/split，含未跑行；旧报告为空 |
| quantification | schema_version=1，method=tricoder-eval-quantification-v1 |
| coverage | planned、recorded、started、missing、infrastructure_errors、incomplete |
| artifact_pass_at_k / delivery_pass_at_k / delivery_pass_power_k | 每 k 含 value、tasks_total、tasks_eligible、reason |
| efficiency | all_started、successful、unsuccessful、unknown_outcome 的分布和 token 摊销 |

成功指标的 reason 为 null（可用）、not_applicable（无 quality 题）、missing_plan、
incomplete_plan、unknown_outcome 或 insufficient_repetitions。null 不显示成 0%。
每个分组沿用 condition 完整性，不挑选完成了的简单题或 split 形成 headline。

离线旧 plan 仅有身份时，kind/category 从同一历史 case 的一致行读取；split 保持
unknown，不读取当前题库。若旧计划中某题完全没有结果且缺 kind，保留其身份至
`unresolved_plan_identities`，所属 condition 不可用，无法确认的 quality 分母为 null；
不会猜该题类型或删除该计划题。其他已完整记录的条件不受其影响。
schema1 报告可识别，但不将旧 pass 位变造成四维或重复身份，新量化组为空。

## Token 与资源分布

main 与 summary 两通道独立，不新增合并总量。每个字段的 observed_sum、strict_sum、
observed、missing、mean、median、p90、p95、max 分别报告。空组数值摘要均为 null。
分布标注 observed_subset；P90/P95 使用 nearest-rank，median 偶数取两个中值的平均。

main reported_total_tokens 仅在 input/output 均已知时等于二者之和，cached 不再相加。
缓存是 input 子集；cache_miss 单独观测，不推导缺失值。缓存率为 sum(cached)/sum(input)，
不是逐行比率平均。缺字段保留 observed_ratio 和 coverage，严格比率不可用；零 input
比率 null。cached>input 标 invalid，不 clamp，阻塞相关严格摊销与新比较 token 差值。

summary_count=0、summary_triggered=False、summary_usage 全缺失三条件同时成立时，
仅新派生统计把该行摘要消耗计为 0；原行不改。其他摘要缺失保持未知。

`main_reported_tokens_per_delivery_success` 是 all_started main 严格总量除以完整成功
运行次数，包含失败消耗，不是只对成功子集求均值。计划不完整、未知交付结果、非法缓存、
用量缺失、无成功分别给 incomplete_plan、unknown_outcome、invalid_usage、missing_usage、
no_success。缺 plan 时仅描述已观察行的资源，不把该描述当作完整实验覆盖证明。

所有 token 标记 `token_accounting=provider_reported`、`billing_completeness=unknown`。
字段完整不等于覆盖了所有失败请求，上游累计器可能忽略 None。多轮重复上下文如实计入；
不同模型 tokenizer 和缓存条件不同，token 不等于跨模型费用。cost_per_success 仍为 null，
没有价格、金额、账单完整性或显著性推断。

## 时间口径

新 TrialTimings 四字段是非负整数毫秒，恒有 total=agent+verifier+harness：

- total：进入 Eval `_run_case` 至最后 verifier 清理结束，不含外层 trial 目录创建与报告写盘。
- agent：executor callback 整段，包含 Session 初始化、多 turn、规划、工具、Provider 等待和自身清理；不是纯模型延迟。
- verifier：install_verifier 与隐藏验证阶段；Agent 自己运行的测试属于 agent。
- harness：其余准备、快照、评分及最终 verifier 清理，加上毫秒取整余量。

使用单调纳秒时钟，只做观察，不参与 timeout/deadline、取消、owner 或清理判断；原异常
传播与 finally 顺序保留。确定未执行阶段为 0，整个未启动 trial 的 timings 为 null。
旧 duration_ms 的 JSON 键及计算位置不变，展示名 legacy_eval_duration_ms；不拿它补
历史阶段值。Experiment duration_ms 仍是批次墙钟，不能用 trial 耗时之和替代。
rounds、tool_calls、retries 按整题累计，16 是每 user_turn 预算，不是整题累计上限。

## 离线使用与严格比较

```powershell
$env:PYTHONPATH='src;tests'
.venv/Scripts/python.exe -X utf8 -B -m tricoder eval-analyze --result runtime/evals/<run>/result.json --plan runtime/evals/<run>/plan.json --ks 1 2 3 --no-color
.venv/Scripts/python.exe -X utf8 -B -m tricoder eval-compare runtime/evals/<base>/result.json runtime/evals/<candidate>/result.json --allow-variable model --no-color
```

新结果有内嵌 trial_plan 时可省略 --plan；同时提供的计划必须身份完全相同，已声明
共同字段一致。输入只读，不加载配置、Provider、Session 数据库、当前题库或审计日志。
result 上限 32 MiB，plan 8 MiB，行数上限沿用 MAX_EXPERIMENT_TRIALS=1000；拒绝目录、
符号链接/reparse、重复 JSON 键及非法计数。输入错误固定输出
`eval_analyze_error=invalid_input`，exit2；有效失败实验或部分指标 null 分析仍 exit0。
CLI 参数本身缺失等语法错误仍由 argparse 处理。

输出独占 `runtime/evals/analyze-<unique>/analysis.json` 与 analysis.md，原子写入，记录
显式输入 SHA256；不复制源码、对话或未知自由字段。新报告与旧报告分析共用纯统计实现，
永远重算原始 trial，不信任输入预计算的 quantification。

compare 保留旧输出及严格指纹检查，仅单条件可比较，condition 名称可以不同。新
quantification_comparison 附两侧独立描述；只有 case/repetition 集合全配对、计划
kind/category/split 相容、对应指标可用才给差值。否则为 missing_plan、unpaired_plan
或相应不可用原因；资源缺失为 missing_observation。成功差是百分点，消耗差是候选减
基线的严格总量，新阶段时间与 legacy 时间分列。不自动加载当前清单补历史计划。

2026-10-21 的既有 30 题结果离线重算应为产物29/30、完整27/30、main总量1,414,748；
单题 n=1，@2/@3/^2/^3 不可用。历史缺 split 与阶段时间分别为 unknown/null。
这不增加真实样本，不构成新的真实重复基线或 Agent 质量提升证据。
