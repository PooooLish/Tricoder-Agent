# Eval 量化指标 Implementation Plan

> **For agentic workers:** 使用 `superpowers:executing-plans`，单会话按 Task 1→5 实施；不提交、不派生实施子会话。用户选择交给 coding session，本 reviewer 仅交付文档。用户把本文及提示词交给 coding session 即表示执行本文范围，不重复询问实施方式。

**Goal:** 在不改变题目、评分和 Agent 行为的前提下，为 TriCoder 提供可靠的 pass@k、pass^k、token 消耗和耗时统计，以及旧实验的离线分析入口。
**Architecture:** 沿用现有 Eval 调度、逐题记录和 JSON/Markdown 报告；新增纯统计模块，补充可信计划元数据和 Eval 边界计时。新旧报告兼容，派生分析写入新的 runtime 目录。
**Tech Stack:** Python 3.11+、标准库 dataclasses/math/statistics/unittest；复用既有 CLI、输出路径验证与原子写入。不加依赖。
**Spec:** 本文第 1—7 节是修改契约，第 8—10 节是实施、验收及交接。
**状态:** 方案已交付，尚未实施；不代表新指标已可用或 90 次真实实验已获授权。

## Global Constraints

- 仅在 `D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli` 工作，先读工作区/项目 AGENTS.md、README.md、pyproject.toml 与 reviewer 交接。
- 保留所有未提交和未跟踪文件；HEAD 不是准确 before。保存本轮实际 before、hash、delta；新证据用 `runtime/eval-metrics-20261027/implementation-<unique>/`。
- 不读 `.env.local`、真实凭据、用户数据库；不安装、提交/推送、reset/clean，不运行真实模型。历史 30 题授权已执行完，不自动延伸到 repeat3 的 90 次。
- 不修改任何题库 fixture、case/suite/experiment、hidden verifier、评分 AND、题数/split/预算；不调整默认权限、Agent 提示、工具恢复、finish、取消/期限/owner/锁或模型采样行为。
- 本包不修 CSV 或交付未闭合问题，不推进 Replan。Q3-R1/R2/R3、旧搜索 P2 等关闭结论保持。
- 所有旧结果、日志和冻结副本只读；不能回写旧 result.json 或把新统计当作当时就记录的事实。新增文档保持 LF，保留旧交接正文。

## Review Focus

1. 不同 condition、quality/contract、case、repetition 混算，或重复身份被当作额外采样。
2. 少跑、取消、未知结果被删掉后，成功率虚高；n<k 时伪造 pass@k。
3. 缓存 token 重复相加，摘要/失败消耗遗漏，未知 usage 被记为零。
4. 新计时改变异常/取消传播、清理顺序或资源所有权；旧 duration 被改名冒充 Agent 时间。
5. 旧报告缺 split/计划/计时仍猜数据，或比较器信任可被篡改的预计算分数、跨指纹比较。

## 1. 当前实现与设计选择

2026-10-27 读取当前文件确认：

| 位置/符号 | 已有能力 | 本包要补的内容 |
| --- | --- | --- |
| `src/tricoder/evals/metrics.py:aggregate_trial_metrics` | 四维完成率、严格字段级 usage 总计、unknown/missing | 独立的逐题重复估计与 token 分布；旧指标原样保留 |
| `src/tricoder/evals/report.py:experiment_report_as_dict`、`_efficiency_subset` | schema 2、类别分组、all_started/successful 的 median/P95 | 按 condition 隔离的量化区块、split、任务级数据、可读表格 |
| `report.py:_stable_cases` | 按 case_id 判断已记录行是否全成功 | 没有按 condition 隔离、没有固定 k/计划完整性，不得直接改称 pass^k |
| `src/tricoder/evals/experiment.py:plan_trials/run_experiment` | 固定调度、独立 trial 目录、每次结果落盘、plan.json | 将计划身份和题目分组固化到报告；seed 是调度种子 |
| `src/tricoder/evals/models.py:TrialRecord/ExperimentRunReport` | repetition、四维、usage、duration、category | 可选 split、计划元数据、可选阶段计时；缺失保持 unknown |
| `src/tricoder/evals/runner.py:_run_case/_run_prepared_case` | duration 从准备工作区前开始，含 Agent 和隐藏验证 | 保留旧 duration；新增含最终 verifier 清理的分阶段时间 |
| `src/tricoder/evals/service.py:_trial_from_case_result` | 将可信执行结果转 TrialRecord | 传递计时和可信 split，不更改评分 |
| `src/tricoder/evals/compare.py:compare_report_payloads` | 严格指纹、单条件、case/repetition 配对 | 新统计只在同一配对集合上比较；旧比较字段不变 |
| `evals/quality-v3/experiment-quality-repeat3.toml` | 30题×3=90 trials，21600秒，seed23 | 保持配置，只 dry-run；以后经用户授权才真实执行 |

考虑过：A 仅写临时分析脚本（快，但新实验仍缺元数据/阶段时间）；B 扩展既有框架（本方案）；C 引入外部评测平台（扩大依赖、迁移与维护成本）。B 为 reviewer 根据用户目标选择的实现建议，不声称用户逐项指定了内部 API。

本期包含：两种成功口径的 pass@1/2/3，完整交付 pass^1/2/3，成功次数直方图，token 分布/摊销，阶段时间，离线重算及严格比较。
本期不包含：货币价格表/费用结算、置信区间/显著性检验、网页仪表盘、Provider 首 token/纯推理时间、请求级完整账单追踪、跨 run 合并样本、自动重试实验。`cost_per_success` 继续 null，不能塞入 token 冒充货币。

## 2. 重复成功率契约

### 2.1 样本及成功判定

- 一个 sample 是整道题从隔离工作区和新 Session 开始的一次完整执行；多 user_turn、16 轮工具交互、Provider 内部 retry 都不是新 sample。
- 唯一身份 `(condition_id, case_id, repetition)`。重复身份、未知条件、与计划不匹配的身份必须拒绝，不能去重后继续评分。
- 只对 `execution_kind=quality` 输出本包 pass 指标；contract 保留原 expectation_met，不混入 quality 分母。
- 产物成功：`artifact_correct is True`，即使 finish 失败也可成功。
- 完整交付成功：沿用现有四维 AND。不能直接把 finish_outcome、工具退出0、最后一个 turn.ok 或 status=passed 当作唯一依据。此处称“四维完整交付”；原始status、expectation_met与assertions结果另存，不能互相覆盖。
- 状态一致性：仅对quality，status=passed却有四维False视为矛盾；status=passed但只有未知维度时保留unknown_outcome，兼容旧事实不足。status!=passed且四维全True可由额外assertion失败引起，合法保留，四维指标仍True。contract的blocked期望可能status=passed而agent_completed=False，不套quality校验；不机械要求status与四维AND等价。
- 已启动的 `error` 行作为一次失败纳入本包“保守端到端运行口径”，保留 infrastructure/unknown 数量；产物为未知时记 0 成功而非删行，并在方法元数据写明这不是“已验证代码正确率”。有真实 artifact=True 的失败行仍计产物成功。
- 对非 error 的未知维度，按三值 AND：任一 False 可确定失败；否则有 None 就不能确定完整结果。未知产物不能推为 False。受影响指标返回 null/reason=`unknown_outcome`，不只计算已知题。
- `not_run_*`、`cancelled`、缺少计划行或 report.complete=False：该受影响 condition 的 pass 指标为 null/reason=`incomplete_plan`。保留已观察计数和效率，不用零假装完成采样，也不选取已跑容易题计算 headline。
- condition 完整性独立判断：不要因其他 condition 未完成而污染本 condition；但顶层 complete=False 且原因无法按计划归属时保守标全局未完成。新报告须通过计划逐行状态判定，而不是仅信任 complete 布尔值。

### 2.2 公式与分组

每题实际完整采样数 n、成功数 c、k∈{1,2,3}：

- pass@k = `1 - C(n-c,k)/C(n,k)`，定义 C(a,k)=0 当 a<k。
- pass^k = `C(c,k)/C(n,k)`，表示 k 次全成功，不能写成 `(总体pass@1)^k`。
- 以题为单位等权平均，不能直接汇总 trial 成功数代替宏平均；计划允许各题 n 不同时仍逐题计算并输出 n 分布。任一计划题 n<k，整个分组该 k 为 null/reason=`insufficient_repetitions`，不只保留 n 足够的题。
- k 必须为正整数，bool、0、负数、非整数拒绝；实现提供 1..100 的有界范围，默认 (1,2,3)，排序去重。只在 n≥k 时计算。
- 输出必须按 condition 分开，且每个 condition 具有 all_quality、by_split(dev/holdout/unknown)、by_category 分组。category/split 不属于另一条独立 sample。
- 每题输出 n_planned、n_started、c_artifact、c_delivery、unknown counts；成功分布按 n 分桶，例如 n=3 时 `0/3、1/3、2/3、3/3`。结果未知的题另进unknown桶，不能误放0/3。n=1 的全成功不能叫“稳定3次”。
- 保留现有 `_stable_cases`/旧 JSON 字段以兼容，Markdown 中标为 legacy observed-all-success，不放在新稳定性 headline；不沿用它实现 pass^k。

方法参考：[HumanEval 官方估计器](https://github.com/openai/human-eval/blob/master/human_eval/evaluation.py)、[τ-bench 可靠性指标](https://arxiv.org/abs/2406.12045)。只参考公开方法，标准库独立实现，不引入或拷贝第三方项目。

## 3. Token、效率和缺失值契约

### 3.1 统计输出

对每个 condition 的 all_started、successful、unsuccessful、unknown_outcome 子集分别计算；后三者使用第2节四维成功判定（含error保守失败），不能凭status归类。unknown仍留在all_started，但不混入unsuccessful；未启动行不进入资源分母。每个子集先列 trials、observed、missing。旧efficiency字段分组方式原样保留，新统计单列。

- main usage 四字段：input_tokens、output_tokens、cached_tokens、cache_miss_tokens。
- summary usage 同样单列，不能静默丢失；本期不新建跨通道合计，以避免改动上游计量或双计。
- main `reported_total_tokens=input+output`，仅两个字段都已观测时存在；缓存命中是 input 的子集，绝不再加 cached/miss。
- 每个字段输出 `observed_sum`（有已知样本才有值）、`strict_sum`（该子集每行都有字段才有值）、observed、missing、mean、median、p90、p95、max；空组的所有数值摘要为 null，不是0。均值/分位数仅对已知值，显式标 observed_subset。
- 分位数采用 nearest-rank：升序第 `ceil(q*N)` 个，1-based；median 仍用 statistics.median，偶数时取中间两者平均。
- `main_reported_tokens_per_delivery_success = all_started.main.reported_total_tokens.strict_sum / 完整成功次数`；包含失败消耗。计划不完整、存在未知交付结果、usage缺失、无成功时依此次序返回 null，reason分别为 `incomplete_plan`/`unknown_outcome`/`missing_usage`/`no_success`；不能拿已知成功子集充当确定分母。这叫“每次成功运行的已报告 token 摊销”，不是仅成功样本平均，也不是每个独立任务解决成本。
- 缓存率采用 `sum(cached)/sum(input)`，不能平均逐题比率。参与行必须 input/cached 均已知、0≤cached≤input。总input=0返回null；缺失时可给 observed_ratio，同时 strict_ratio=null并给coverage。cache_miss是独立观测，不强行推导缺失值。
- summary_count=0、summary_triggered=False 且 summary_usage 全缺失时，可在**新派生摘要统计**认定该行无摘要消耗=0；原记录仍null。不满足这三个证据条件的缺失摘要一律unknown，不得只凭配置off猜零。
- 输入计数拒绝bool、负值、NaN/Infinity；字段内矛盾的缓存记录不产生比率，报告invalid并阻止相关严格摊销；不得偷偷clamp。CLI对结构非法输入退出2，保留来源文件。

### 3.2 完整性限制

现有 `TrialRecord.usage` 是已归一化、累计的 Provider reported usage，不保证覆盖所有失败请求。`engine/state.py:merge_usage` 会忽略 None，Eval 行完整不等于每次 Provider 请求都完整。

因此必须标 `token_accounting="provider_reported"`、`billing_completeness="unknown"`。本包的 observed/missing 只表示 trial 字段完整度，不能声称账单金额或实际完整用量。本期不改 Provider、Engine 的累计逻辑；不计算货币费用，避免虚假精确。

多轮重复上下文如实计入输入；不同模型 tokenizer/token定义及缓存条件不同，跨模型不能把 token 数直接等同货币成本。保持 usage 与 summary_usage 的现有含义不变。

## 4. 阶段耗时契约

新增 `TrialTimings(total_ms, agent_ms, verifier_ms, harness_ms)`，字段为非负整数。整体对象缺失为 None，不伪造旧报告计时。

- `total_ms`：进入 `_run_case`，直到其返回前最后的 verifier 清理结束；不含调度器在外面创建 trial 根目录及最终报告写盘。
- `agent_ms`：调用 agent_executor 前到返回/抛出后，包括该 callback 的 Session 初始化、多 turn、规划、工具、验证命令、Provider等待及自身资源清理。明确不是纯模型时延。
- `verifier_ms`：Eval 隐藏评分阶段的 install_verifier 和各 `_run_verification`，不包括 Agent 自己运行的测试；最后 remove_verifier 属于 harness。
- `harness_ms`：total减上述两个互不重叠阶段，包含准备、副本快照、差异、评分、verifier清理。高精度时钟测量后统一毫秒取整，确保 `total_ms=agent_ms+verifier_ms+harness_ms`，非负。
- 某阶段确定未执行，记0；整行根本未启动则 timings=None。旧报告缺计时=None；不能从旧 duration 或审计时间戳反推。
- 保留旧 `duration_ms` 的既有计算位置、数值语义和效率字段，标作 `legacy_eval_duration_ms` 的展示名称，不改变其 JSON 键。新 phase 字段单列，不能用旧 duration 补新 total。
- 使用单调时钟。不要把时间预算、cancel/deadline与观测时钟混用；不能新增 sleep、超时、后台任务或吞异常。计时 finally 不能替代原 cleanup finally，也不能让清理未确认时继续执行。
- 正常失败/可返回错误也记录已观测阶段时间；KeyboardInterrupt/取消等原本传播的异常仍原样传播，不为生成统计强行转成成功/普通错误。
- 时间、rounds、tool_calls、retries均输出all_started和successful/unsuccessful的上述分布。multi-turn rounds 是整题累计，可大于16；预算16是每user_turn，不是整题总上限。
- 调度器 experiment.duration_ms 仍为整批墙钟；不能拿并发任务时间总和冒充墙钟。本包不改变串行调度。

## 5. 数据模型与报告接口

以下名字固定，避免跨任务接口漂移；内部辅助函数由 coding 自行合理实现。

### 5.1 新增类型/字段

在 `evals/models.py` 新增 frozen dataclass：

- `TrialTimings(total_ms:int, agent_ms:int, verifier_ms:int, harness_ms:int)`，严格类型/非负/相加校验。
- `TrialPlanEntry(key:TrialKey, execution_kind:ExecutionKind, category:str, split:str)`；split 仅 dev/holdout/unknown，category沿用受限identifier。
- 在 `TrialRecord` 尾部追加 `split: str="unknown"`、`timings: TrialTimings|None=None`。
- 在 `ExperimentRunReport` 尾部追加 `trial_plan: tuple[TrialPlanEntry,...]=()`。
- 在 `runner.EvalCaseResult` 尾部追加 `timings: TrialTimings|None=None`。

在 run_experiment 固化全部计划行（包括将来not_run）；plan.json 的原 planned_trials 行追加 execution_kind/category/split，新 result.json 追加同内容 `trial_plan`。异常产生的error、not_run行也携带可信split；正常行通过 `_trial_from_case_result` 传递。来自执行器的明确冲突 metadata拒绝，不覆盖后假装一致；旧默认unknown可用可信 PlannedTrial补齐。

### 5.2 纯统计模块 `src/tricoder/evals/quantification.py`

接口：

```python
estimate_pass_at_k(n: int, c: int, k: int) -> float
estimate_pass_power_k(n: int, c: int, k: int) -> float
summarize_counts(values: tuple[int | None, ...]) -> dict[str, object]
quantify_trials(
    trials: tuple[TrialRecord, ...], *,
    plan: tuple[TrialPlanEntry, ...] | None,
    ks: tuple[int, ...] = (1, 2, 3),
    report_complete: bool | None = None,
) -> dict[str, object]
```

估计器对非法n/c/k和n<k抛ValueError，汇总层将正常“样本不足”转为可用性状态。report_complete由原始报告传入；为False但计划内找不到未完成condition时按全局未完成处理，否则由计划逐条件归属；为None时仍可从完整计划与逐行状态判断。纯函数不得读取文件、当前suite、配置、环境、时钟或调用Provider。

新顶层 `quantification`：`schema_version=1`、`method="tricoder-eval-quantification-v1"`、`ks`、`token_accounting`、`billing_completeness`、`by_condition`。

每个 condition 下含 all_quality/by_split/by_category；组包含 coverage、case_results、success_histograms、artifact_pass_at_k、delivery_pass_at_k、delivery_pass_power_k、efficiency。每个k估计包含 `value`、`tasks_total`、`tasks_eligible`、`reason`，未知reason固定枚举，不接受任意异常正文。完整组reason=null；无quality题为not_applicable。

缺plan时保留资源分布及已观测计数，但新pass指标null/reason=`missing_plan`。不能仅因行数等于planned_trials就认定没有漏题。未知split只进unknown，不能读当前suite猜历史划分。

原 schema_version=2 及旧指标键保持。新字段可选、缺失有明确默认；JSON不写NaN/Infinity。报告 Markdown 中明确新旧时间/稳定性口径，不用0%表示null。

## 6. 离线分析与比较

### 6.1 新命令

`tricoder eval-analyze --result <result.json> [--plan <plan.json>] [--ks 1 2 3] --no-color`

- 在 `cli.py` 增加参数和分发，独立模块 `evals/analysis.py` 提供 `analyze_report_payload(payload: object, *, plan_payload: object|None=None, ks:tuple[int,...]=(1,2,3))->dict[str,object]` 与 `run_analyze_command(args:object, *, output:TextIO)->int`。
- 不调用load_config/create_provider，不加载.env或Session数据库。只读显式输入，不自动遍历日志/审计，不执行候选源码，不扫描当前题库。
- 新报告直接使用嵌入trial_plan。旧v2可显式提供原生plan.json；旧plan只有身份时，kind/category仅从同一case已存在且一致的历史trial取，split=unknown，不从当前fixture补。完全缺席的旧plan题无法推断kind时，统计保守不可用，不删题。
- 如果嵌入plan与显式plan同时存在，两者已声明的共同字段/全部身份必须一致；冲突退出2。plan的experiment_id/suite_id若有则与result核对；计划行数、重复身份、条件、正整数repetition均校验。
- schema1 legacy可走现有normalize_report_payload，但不得凭原pass位补造四维或重复身份；新pass/阶段时间不可用，基础已有字段可展示。不可解析/不支持的schema拒绝，不静默猜。
- result输入建议上限32 MiB，plan 8 MiB，输入trials/plan行数不超过现有MAX_EXPERIMENT_TRIALS；读文本前检查文件类型/大小，拒绝目录、符号链接/reparse，错误输出固定 `eval_analyze_error=invalid_input`，exit2，无凭据/异常原文。成功分析即exit0，即使质量分数<100%或部分指标null。
- 使用已有output.py的受控目录预留与原子写入，在新 `runtime/evals/analyze-<unique>/` 写 `analysis.json`、`analysis.md`；打印result/report路径。无覆写选项，来源文件hash前后相同。派生报告记录输入SHA256，不复制源码/对话/任意unknown字段。
- 自动生成新实验报告与离线分析必须调用同一quantify_trials；不能维护两套公式。离线读取时重算，不信任输入里已有的quantification分数。

### 6.2 比较规则

- 继续调用现有 `_check_compatibility`，不增加“允许suite/verifier/budget变化”逃生口，不修改现有指纹集合。量化版本写在独立块，不能随意向fingerprints加入键导致旧严格reader失效。
- 保留旧compare输出，新增 `quantification_comparison`，由两侧原始trials重算。各侧先独立校验(condition_id,case_id,repetition)身份与本报告唯一condition；跨侧沿用(case_id,repetition)配对，condition_id名称可不同，不把名称差异当模型比较不兼容。逐对校验execution_kind/category/split，条件实验属性仍交既有兼容性检查。
- 只有跨侧配对集合完全匹配、计划元数据兼容、统计方法相同且各指标可用时，输出pass百分点差和消耗差；否则新差值null，reason=`unpaired_plan`/`missing_plan`/对应不可用原因，列两侧独立描述。不得各自挑最优/成功子集冒充配对改善。
- 旧单条件严格比较边界保持，不支持多条件报告直接compare；量化report自身可按condition展示。
- 新时间与旧时间分开比较。旧报告timings缺失时阶段差为null。token比较保留main/summary分离及missing；跨模型说明token不可直接当费用。
- 不自动加载当前实验定义补历史plan；如要比较旧报告，旧报告缺plan则新pass差null，旧比较仍可工作。无需为本包增加compare CLI的历史plan补充参数。

## 7. 固定验收数值与反例

| 验收 | 输入 | 预期 |
| --- | --- | --- |
| 估计器 | n=3,c=2 | pass@1=2/3，@2=1，@3=1；pass^1=2/3，^2=1/3，^3=0 |
| 边界 | c=0 / c=n，n=3 | 所有合法k分别0 / 1 |
| 宏平均 | A:3次成功2，B:3次成功0 | @1=1/3，@2=@3=1/2；^2=1/6，^3=0 |
| 不等n | A:n=3,c=3，B:n=1,c=0，计划完整 | @1=1/2而非3/4；@2/@3=null，不只报告A |
| 分组隔离 | 同case同repetition，condition A成功、B失败 | A=1/B=0；跨条件不构造n=2；contract不得进入quality |
| 计划缺失 | 30题计划只有29题结果，或3次计划只有2行 | incomplete_plan；不能缩分母；not_run/cancelled同样不可用 |
| 基础设施错误 | n=3，2完整成功+1error/未知artifact | 保守@1=2/3，error/unknown计数各正确；不能删除error变成100% |
| 四维 | artifact=True、agent_completed=False | 产物成功，完整失败；未知结果按第2节处理 |
| token分布 | 两行input10/20，output2/4，cache8/10 | total12/24、strict_sum36、mean/median18、P90=P95=max24；缓存率18/30=0.6 |
| 失败摊销 | 上两行仅第一行完整成功 | 每次成功摊销36，不是12；零成功=null |
| 部分usage | 第二行output=None | total已知12，missing1，strict_sum/严格摊销=null，不能记0 |
| 未知分母 | 两行usage完整，一行交付True、一行非error且交付None | all_started含两行，unknown子集含一行；严格摊销null/unknown_outcome，不能除以已知成功1 |
| 摘要 | summary_count=0/False/全null，另一行count>0但null | 前者新派生0，后者unknown；原行不变；main不额外加cached |
| 时间 | 假单调时钟安排agent30ms、verifier20ms、其余50ms | total100=30+20+50；分阶段边界包括最后清理；legacy duration原语义保持 |
| 兼容 | 老schema2无新增字段 | 原字段可读可比较；split unknown、timings None；不猜历史计划 |
| 不信任摘要 | 修改输入quantification为100% | 离线重算仍由原trial得到正确值 |

另需覆盖：空组、重复/非法repetition、bool计数、非法/超限k、负数/非有限数、缓存大于input、零input、plan身份/分组冲突、类别元数据不一致、所有任务缺结果、Provider错误usage缺失、异常清理计时、没有Agent执行的准备失败，以及旧schema1不能伪造新指标。

现存2026-10-21基线可只读离线验收：`runtime/evals/20261021t062203.210557z/result.json` + 同目录plan.json。

- 30quality，产物29/30，完整27/30，@1分别29/30和27/30；@2/@3/^2/^3=null（n=1）。
- input=1,343,808，output=70,940，reported total=1,414,748，均值47,158.2667，每次成功摊销52,398.0741。
- legacy时间均值24,140.4ms，median22,380.5ms，P95=38,253ms；旧timings缺失仍null。
- 旧trial无split、旧plan也无split，所以该离线分析split=unknown；不能引用本报告人为知道的22/8替代历史结构化证据。
- 这是已有结果重算，不是真实重跑；不得覆盖原日志，不能声称产生新的pass@3证据。

## 8. 文件范围与任务

默认允许修改：`src/tricoder/evals/{models,metrics,report,experiment,runner,service,compare}.py`、`src/tricoder/cli.py`，新增`src/tricoder/evals/{quantification,analysis}.py`；必要README/指标说明文档、相关既有Eval/CLI测试及下面新增测试。其他Agent/Provider/进程模块只读。metrics.py仅在需要复用三值AND时小幅抽取，不能改旧聚合结果。

### Task 1：纯统计与明确分母

**Files:** Create `quantification.py`、`tests/test_eval_quantification.py`；Modify `models.py`。
**Interfaces:** 第5节全部新dataclass与纯函数；本任务先用合成TrialRecord/TrialPlanEntry独立验收。

- [ ] 保存实际before、源码/题库/旧证据hash与未跟踪清单，不能把HEAD diff当本次增量。
- [ ] 为第7节公式、宏平均、不等n、condition/quality隔离、未知/error/not_run、重复身份、plan冲突写明确期望，运行新测试得到有效RED。
- [ ] 实现新增类型与统计函数，标准库math.comb/整数运算，不用概率总体公式近似。
- [ ] 加入字段级usage完整度、分位数、失败摊销和summary/缓存反例；运行至GREEN。
- [ ] 保留逐组/逐题输出及方法版本，确认旧aggregate_trial_metrics用例不变。

### Task 2：可逆兼容的计划元数据和报告

**Files:** Modify `models.py`、`experiment.py`、`service.py`、`report.py`；Test `tests/test_eval_quantification_report.py`、既有`test_eval_experiment.py/test_eval_report.py`。
**Interfaces:** 新`trial_plan`来自`plan_trials`的PlannedTrial；trial_record_as_dict/from_dict对split/timings可选往返；experiment_report_as_dict追加quantification。

- [ ] 先测新/旧行往返、unknown默认、计划含not_run/error、条件隔离、split冻结和既有构造器兼容，留RED。
- [ ] 传播可信元数据并生成新增JSON区块；已有字段、schema、指纹键和结果含义不变。
- [ ] Markdown展示三类成功指标及n/coverage、token/时间表；null带原因，legacy stable与阶段时间不可冒名。
- [ ] 再测两条件×两题×三次离线实验，核对共12行、每条件6行，防止条件维度混并为同一题的6次尝试。
- [ ] 全部本任务回归GREEN，检查新增JSON无自由文本泄漏、无NaN，旧报告兼容。

### Task 3：只观察、不干预的计时

**Files:** Modify `runner.py`、`service.py`；Test `tests/test_eval_timing.py` 与`test_eval_runner.py/test_eval_service.py`。
**Interfaces:** `EvalCaseResult.timings`→`TrialRecord.timings`；统计复用summarize_counts。

- [ ] 用可控单调时钟测试第7节100=30+20+50、未执行阶段0、旧对象None、最后清理计入total；不得靠真实sleep测试算术。
- [ ] 在现有Eval边界采集，保留旧duration和exception/finally顺序；准备失败、executor异常、隐藏超时、清理失败、取消传播均不能被计时吞掉。
- [ ] 验证结果/错误分类/资源owner与修改前一致，时钟仅提供观测，不成为终止预算。
- [ ] 跑针对性回归GREEN；源码范围不扩到Provider、Engine、Session、process。

### Task 4：离线分析和严格比较

**Files:** Create `analysis.py`、`tests/test_eval_analysis.py`；Modify `cli.py`、`compare.py`，Test `test_eval_compare.py/test_cli.py`。
**Interfaces:** 第6节新函数/命令；与report调用同一统计实现，compare新增区块重算两侧原始事实。

- [ ] 测试分析命令绝不load_config/create_provider、不读.env/数据库；有效失败实验分析也exit0，非法/超限输入exit2。
- [ ] 测新嵌入plan、旧显式plan、无plan、两plan冲突、schema1兼容及输入quantification被伪造的情况，留RED后实现。
- [ ] 输出到新runtime目录，核对来源hash不变；拒绝reparse/目录，保留既有output边界。
- [ ] 测旧compare仍可用，新统计差必须配对相同计划；只有部分身份重合时差值null，不改变旧输出；condition_id不同但声明模型变量且case/repetition一致可比较；不同suite/verifier/budget继续拒绝。
- [ ] 实际只读分析2026-10-21报告，核对第7节数值与null范围，将命令/输出/hash保存本轮证据；不重写原文件。

### Task 5：完整离线验收与交付

**Files:** README.md，新增`docs/eval-quantitative-metrics.md`、`docs/reviews/2026-10-27-eval-quantitative-metrics-implementation.md`，project.md和reviewer交接。

- [ ] 新指标、新报告/CLI/计时测试分别运行并保留独占日志；再运行全部Eval与完整unittest，报告实际数目、失败及skip，不沿用旧2001硬编码。
- [ ] 四份V3清单dry-run仍30/90/22/8。构造90行离线重复结果验证pass@3，不用真实模型凑样本。
- [ ] 验证回归不改变评分、权限、轮数、取消、cleanup、已有终止期限及owner；原搜索P2的延迟恢复回归保持。
- [ ] 对所有旧题库、真实结果、历史日志hash保全；明确before→after实际文件列表，包含新增未跟踪文件。
- [ ] AST/空白/类型边界自审，`git diff --check`；不自动修无关历史修改。
- [ ] 交付方法口径/数据字典、实际验证结果、旧数据限制和下一步；标“实现完成，待独立复审”，不自行宣称reviewer已验收或开始付费90次。

## 9. 验证命令

当前测试子进程使用 `PYTHONPATH=src;tests`、`PYTHONDONTWRITEBYTECODE=1`；新目录独占记录命令、stdout/stderr、exit、耗时与版本hash。已有V3测试的TRICODER_V3_EVIDENCE必须指向本轮独占子目录。

```powershell
.venv/Scripts/python.exe -X utf8 -B -m unittest discover -s tests -p 'test_eval_quantification*.py' -v
.venv/Scripts/python.exe -X utf8 -B -m unittest discover -s tests -p test_eval_timing.py -v
.venv/Scripts/python.exe -X utf8 -B -m unittest discover -s tests -p test_eval_analysis.py -v
.venv/Scripts/python.exe -X utf8 -B -m unittest discover -s tests -p 'test_eval*.py' -v
.venv/Scripts/python.exe -X utf8 -B -m unittest discover -s tests -v
.venv/Scripts/python.exe -X utf8 -B -m tricoder eval-analyze --result runtime/evals/20261021t062203.210557z/result.json --plan runtime/evals/20261021t062203.210557z/plan.json --ks 1 2 3 --no-color
.venv/Scripts/python.exe -X utf8 -B -m tricoder eval --experiment evals/quality-v3/experiment-quality-repeat3.toml --dry-run --no-color
git diff --check
```

另以baseline/dev/holdout清单各dry-run一次。不要运行不带 `--dry-run` 的真实eval；上述90行验收使用合成/现有fake机制。来源路径若丢失，记录缺失，不能下载或伪造历史成绩。

## 10. 完成标准与后续

用户能够在新实验报告查看可解释的重复质量、token/时间分布，并仅凭旧result+plan离线重算可证明的指标。样本不足/缺测/未跑/无plan都明确不可用，不能提升表面分数；单条件与多条件不混，旧报告仍能读取比较，生产行为与旧题库不变。

真实重复基线留给后续独立reviewer通过后、用户另行授权执行；建议仍用现有30×3清单，但本包不消费调用额度。多次尝试必须独立workspace/Session，保持源码、题面、verifier、预算和模型配置冻结；调度seed不冒充模型seed。一次或三次采样都不能宣称统计显著，也不能把新版V3与旧v2.2直接归因比较。
