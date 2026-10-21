# Quality V3真实模型基线（2026-10-21）

在本轮独立复审通过、Q3-R1/R2/R3关闭后，按此前条件授权运行冻结版本的30道真实quality题，每题一次。**产物正确29/30，完整通过27/30**；范围合规30/30，清理确认30/30。实验complete=true，30题均记录，状态分布{'passed': 27, 'failed': 3}，CLI退出1。

Provider为DeepSeek，模型deepseek-v4-flash，quality-v3，seed23，memory off/off，每user_turn16轮/16000上下文，实验预算7200秒，repetitions1。原生run：`runtime/evals/20261021t062203.210557z/`；新验证证据：`runtime/quality-v3-real-20261021/`，无覆写旧日志。

本轮先独立运行2001项全量回归：1986通过、15平台/权限skip、0失败/错误、837.646秒、exit0；before/current16次对照及额外6次评分均符合预期。详见`docs/reviews/2026-10-21-quality-v3-review-fixes-rereview.md`。

开发集与保留集分项：{"dev": {"total": 22, "artifact": 21, "completed": 19}, "holdout": {"total": 8, "artifact": 8, "completed": 8}}。失败turn停止原因：{"verification_blocked": 1, "model_incomplete": 1}。运行耗时724.797秒，实际usage：`{"input_tokens": 1343808, "output_tokens": 70940, "cached_tokens": 1166208, "cache_miss_tokens": 177600}`。这些为服务商返回的用量数据，未估算费用。逐turn诊断、工具错误分类及有界纠正记录见analysis.json。

| 题目 | 划分 | 产物正确 | 完整通过 | 失败／停止原因 |
| --- | --- | --- | --- | --- |
| mm-todo-once | dev | 是 | 是 | — |
| rc-read-once | dev | 是 | 是 | — |
| cf-package-api-migration | holdout | 是 | 是 | — |
| cf-record-schema-compat | holdout | 是 | 是 | — |
| sf-csv-records | dev | 否 | 否 | verification_failed |
| sf-aware-time-order | dev | 是 | 是 | — |
| cf-price-discount | dev | 是 | 是 | — |
| mm-early-constraint | dev | 是 | 是 | — |
| sf-tail-boundary | dev | 是 | 是 | — |
| cf-config-precedence | dev | 是 | 是 | — |
| rc-patch-conflict | dev | 是 | 是 | — |
| cf-cli-result-contract | holdout | 是 | 是 | — |
| sf-unique-stable | dev | 是 | 是 | — |
| cf-cache-copy-isolation | holdout | 是 | 是 | — |
| cf-module-interface | dev | 是 | 是 | — |
| cf-update-caller | dev | 是 | 否 | agent_failed,agent_unverified; verification_blocked |
| cf-parameter-validation | dev | 是 | 是 | — |
| sf-negative-boundary | dev | 是 | 否 | agent_failed,agent_unverified; model_incomplete |
| rc-invalid-args | dev | 是 | 是 | — |
| mm-latest-correction | dev | 是 | 是 | — |
| rc-provider-transient | dev | 是 | 是 | — |
| rc-search-truncated | dev | 是 | 是 | — |
| mm-partial-revision | holdout | 是 | 是 | — |
| dl-regression | dev | 是 | 是 | — |
| sf-subtract | dev | 是 | 是 | — |
| sf-empty-input | dev | 是 | 是 | — |
| dl-jsonl-line-diagnostics | holdout | 是 | 是 | — |
| cf-backward-api | dev | 是 | 是 | — |
| cf-page-merge | holdout | 是 | 是 | — |
| dl-order-summary-pipeline | holdout | 是 | 是 | — |

已独立核查CSV失败：生成的csv_records.py:14通过`if row`过滤空记录，使输入换行返回[]，而公开契约要求与csv.reader一致，应为[[]]；普通和带引号换行的对照保持。4个业务oracle结果见csv-oracle.json，因此这是该次生成代码的行为偏差。cf-update-caller则隐藏验证通过，但第7轮finish时report.py仍pending；纠正后第9轮依旧未覆盖，因一次纠正额度耗尽而停止。它是交付验证失败，不能混同产物错误或预算耗尽。其他失败按上表及原始turn/audit事实记录，本轮未对每一种停止展开新生产修复。

逐题文件与总报告、四维AND及跨turn完成条件已独立核对。815份源码/测试/脚本/题库文件、321份冻结文件、11,088份受保护历史文件hash保持。没有修改生产/题库/测试、权限/默认预算，未安装依赖、提交或推送。密钥只由既有程序加载，未人工读取或输出凭据正文，未读取用户数据库。

本次是新quality-v3的单次基线，不是3次重复实验；新增题库与v2.2契约/题数均有变化，不能直接把分数差归因Agent提升。保留集已由作者和审查离线接触，不声称作者盲测。Windows15项skip、其他平台/模型、长期记忆、真实故障恢复均未验证。评分器有限变体覆盖不证明任意数据流或目录级安全。本轮不自动修改Agent、提高权限或推进Replan。
