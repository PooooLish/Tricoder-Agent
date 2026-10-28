# quality-v3.1 真实模型 30×3 复测（2026-10-28）

90 次全部执行完成，**产物正确 88/90，完整交付 86/90（95.5556%），未全部通过**。四次失败为两次真实修改范围违规、两次 Provider 请求阶段中断。原先发生轮次耗尽的两道题本轮合计 6/6 通过，但端到端交付率没有提高。

用户本轮授权真实复测；采用已独立复审通过的冻结版本。模型为 DeepSeek `deepseek-v4-flash`，memory off/off；每个 user_turn 最多16轮、上下文16000，seed23。30题（22 dev、8 holdout）各3次，90个独立工作区及90个独立审计 Session。condition faults=none，但保留题目自身规定的故障注入，不能称整套无故障注入。没有选择性补跑、删除失败或修改判题规则。

- run：`20261028t131121.505118z`；benchmark：`quality-v3.1`；experiment：`quality-v3-1-repeat3`。
- 原生 CLI exit=1（有失败样本）；complete=true、planned=recorded=90。包装脚本 exit0仅表示完整执行得到确认。
- 复审报告：[2026-10-28-quality-delivery-guidance-fixes-rereview.md](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/docs/reviews/2026-10-28-quality-delivery-guidance-fixes-rereview.md)。
- 原生结果：[result.json](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/runtime/evals/20261028t131121.505118z/result.json)；独立分析：[analysis.json](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/runtime/quality-v31-repeat3-real-20261028/analysis.json)。

## 指标口径与结果

完整交付为 artifact_correct、agent_completed、scope_compliant、cleanup_confirmed 四维 AND。每题 n=3：pass@k=1−C(3−c,k)/C(3,k)，pass^k=C(c,k)/C(3,k)，再对30题等权平均。pass^k表示k次全部成功，不是把总体通过率直接取k次方；本次通过枚举所有k元素子集独立复算。

| 指标 | k=1 | k=2 | k=3 |
|---|---:|---:|---:|
| 产物 pass@k | 97.7778% | 100.0000% | 100.0000% |
| 完整交付 pass@k | 95.5556% | 98.8889% | 100.0000% |
| 完整交付 pass^k | 95.5556% | 92.2222% | 90.0000% |

四维分别为：产物88/90、Agent完成88/90、范围合规88/90、清理确认90/90。27题3/3完整通过；cf-price-discount和dl-regression为2/3；sf-unique-stable为1/3。dev完整62/66、holdout完整24/24。标签不代表未经历史诊断使用的全新盲测。

## Token 与时间

Provider已报告输入 **3,006,833**、输出 **162,055**，合计 **3,168,888**。缓存命中输入 2,604,642，未命中输入 402,191，输入缓存比例 86.6241%；缓存是输入的子集，不重复加到总量。摘要未触发，已报告摘要用量为0。

每次平均 35,209.87 token，中位数 30,560.5，P95 65,829，最大 78,715。含失败成本的每次成功交付摊销为 36,847.53 token。

逐 trial 的90组已报告用量均有值；独立对796条 provider_usage 求和（其中101条 round0规划用量），全部与trial统计一致。这里核对的是保留下来的 Provider 报告，不是账单；失败请求的未报告用量及服务端重试计费是否完整仍未知，不能据此声称实际账单精确等于该总量。

整批原生耗时 **62.6977分钟**，包装进程 62.8305分钟。单次 total 平均 **41.793秒**、中位数 41.439秒、P95 **73.232秒**、最大 100.845秒。每条均满足 total=agent+verifier+harness；三者平均分别 41.379/0.322/0.092秒。

| 重复轮次 | 完整交付 | 产物正确 | trial平均秒数 | 总Agent轮数 | 已报告token |
|---|---:|---:|---:|---:|---:|
| 1 | 30/30 | 30/30 | 27.178 | 233 | 1,054,703 |
| 2 | 28/30 | 29/30 | 50.373 | 230 | 1,037,432 |
| 3 | 28/30 | 29/30 | 47.828 | 234 | 1,076,753 |

## 与上轮结果的描述性对照

旧版本为quality-v3，新版本为quality-v3.1，同时变化了Agent和公开README指引。因此下面仅作描述性对照，不合并样本、不绕过原生compare的兼容校验，也不能作代码改动的因果实验。题目case.toml、业务fixture、公开测试和隐藏verifier未放宽；只有README指引等套件元数据按复审范围变化。

| 项目 | 上轮V3 | 本轮V3.1 |
|---|---:|---:|
| 产物正确 | 90/90 | 88/90 |
| 完整交付 | 87/90 | 86/90 |
| 完整pass@1 | 96.6667% | 95.5556% |
| 完整pass^3 | 93.3333% | 90.0000% |
| 已报告token | 4,577,776 | 3,168,888 |
| 批次分钟 | 48.0284 | 62.6977 |
| Agent轮数 | 908 | 697 |
| 工具调用 | 1290 | 1016 |
| finish验证恢复事件 | 47 | 0 |

已报告token变化 -30.78%，轮数变化 -23.24%，实际整批时间变化 +30.54%。轮数和token减少，但真实耗时增加；没有独立请求时延和服务端负载证据，不能把时间变化全部归因于代码，也不能用token下降推断速度提升。两次请求中断还造成提前结束，因此token下降也不等同于同质量下等比例节省。

上轮失败题：sf-aware-time-order本轮11/9/11轮均通过；rc-search-truncated本轮7/7/10轮均通过。没有round_limit_exceeded或finish验证恢复事件，但这只说明本轮未复现，不能证明一般场景根治。

## 四次失败的独立核查

1. **#43 sf-unique-stable / rep2：Agent修改范围违规。** 第4轮主动create_file创建`collection_ops.py.new`，内容为`placeholder`，最终留在交付中。允许范围仅collection_ops.py及tests/*.py；模型第2轮已读取明确说明其他文件只读的README。第8轮finish被完成门禁接受，但事后范围评分正确拒绝。独立hash差分仅三项，唯一越界是该.new；新副本公开7项、隐藏3项全部通过。不是产物错误、预算耗尽或工具内部临时文件泄漏。证据：[scope-failure-verification.json](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/runtime/quality-v31-repeat3-real-20261028/scope-failure-verification.json)。
2. **#54 dl-regression / rep2：Provider请求阶段失败。** 第3轮ProviderError，前面仅4次读取/列表工具；工作区hash与初始fixture完全相同，无故障注入。原有除零逻辑仍错误，独立副本公开1项、隐藏3项各有1项失败。原生failure_stage=verifier不能替代根因分析：上游实际停止原因为provider_failed。证据：[provider-failure-verification.json](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/runtime/quality-v31-repeat3-real-20261028/provider-failure-verification.json)。
3. **#67 cf-price-discount / rep3：Agent修改范围违规。** 第4轮给只读checkout.py增加函数文档字符串；允许范围为pricing.py及tests/*.py。AST核查表明该文件仅新增docstring，业务逻辑未改；原交付副本与仅还原checkout.py的反事实副本，各自公开12项、隐藏3项全部通过。证明这项越界编辑不是实现任务的必要条件。原成绩保持失败。证据：[checkout-scope-verification.json](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/runtime/quality-v31-repeat3-real-20261028/checkout-scope-verification.json)。
4. **#73 sf-unique-stable / rep3：可恢复写入错误后Provider失败。** 第4轮create_file返回file_already_exists，后续同批工具跳过；第5轮ProviderError，未成功写入。工作区与初始fixture完全相同；独立副本公开/隐藏检查仍失败。不能把前一工具错误直接当作后续Provider异常的原因。证据：[provider-failure-0073-verification.json](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/runtime/quality-v31-repeat3-real-20261028/provider-failure-0073-verification.json)。

两次Provider错误均只保留error_type=ProviderError、error_chars=10，未保留code、retryable、请求阶段或安全的请求标识。[loop.py](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/src/tricoder/engine/loop.py) 第308—316行捕获时未记录这些字段；[providers.py](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/src/tricoder/providers.py) 第45—51行已有code/retryable，第443—448行可区分总预算、首进展和空闲超时。不能凭消息长度或间隔断言本次一定是网络、上游宕机、idle_timeout或cleanup_failed；真实根因仍有观测缺口。未读取Session数据库补查异常，也未进行选择性真实补跑。

两项范围判定可分别在case.toml第6行核对；[runner.py](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/src/tricoder/evals/runner.py) 第374—375行按公开allowed_changes判定。独立逐层路径匹配与原生匹配结果一致，未发现这四条属于判题误报。范围违规属于任务约束遵守问题；请求异常需要补足安全诊断信息。四次均非权限拒绝所致，不支持放宽默认权限或放宽allowed_changes来消除失败。

## 仍存在的过程成本与后续方向

本轮工具错误 104 次，其中file_already_exists40次、patch_count_mismatch34次；上轮相应为 142/43/51次。新反馈没有消除错误工具选择。原生retries字段为12，包含题目故障恢复，不等于完整的上游HTTP尝试次数。工具错误包含预设故障，不应全部视为新的生产缺陷。

建议下一轮优先处理：任务范围遵守及交付前变更检查；保存可脱敏的Provider错误分类与请求阶段，再决定是否调整特定恢复策略。不要先放宽权限、扩大题目允许范围、自动吞掉请求错误或修改本轮得分。本轮未改生产代码、测试或题库；后续具体修复仍交coding session，未自动推进Replan或历史待办。

## 复核与未验证范围

- 30题各有且仅有rep1/2/3；90个原始trial逐行与汇总相等，无取消、预算跳过或补样；90工作区/90审计Session无跨trial复用。
- 组合枚举、资源统计、耗时守恒与原生quantification一致；离线eval-analyze退出0且结果完全一致，原result/plan哈希未改变。
- 1030份复审源码/测试/脚本/题库、325份冻结文件、21,436份历史证据哈希保持。旧日志未覆盖。prepare首轮仅有旧目录引用错误，在模型调用前修正并保留记录：[preparation-repair.md](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/runtime/quality-v31-repeat3-real-20261028/preparation-repair.md)。
- 四个失败均在新副本/只读审计中独立核查，反事实副本不会回写原成绩或原工作区。凭据只由程序正常加载，未显示或复制；未安装、提交、推送。
- 本轮只验证这个模型、Windows环境及该30题的小样本重复；未新增V3 Agent-only对照、其他Provider/模型/平台或外部基准。未重跑此前2259项离线全量；本轮源码hash与已通过复审版本一致。Provider错误细类、失败请求账单完整性以及时延变化的因果归属未验证。
- 核对证据：[verification.json](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/runtime/quality-v31-repeat3-real-20261028/verification.json)、[provider-usage-verification.json](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/runtime/quality-v31-repeat3-real-20261028/provider-usage-verification.json)、[offline-analysis-verification.json](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/runtime/quality-v31-repeat3-real-20261028/offline-analysis-verification.json)。

## 逐题结果

| 题目 | 产物成功/3 | 完整交付/3 | 三次Agent轮数 |
|---|---:|---:|---|
| cf-backward-api | 3 | 3 | 7 / 7 / 6 |
| cf-cache-copy-isolation | 3 | 3 | 9 / 10 / 8 |
| cf-cli-result-contract | 3 | 3 | 8 / 9 / 9 |
| cf-config-precedence | 3 | 3 | 10 / 9 / 8 |
| cf-module-interface | 3 | 3 | 8 / 7 / 9 |
| cf-package-api-migration | 3 | 3 | 7 / 7 / 7 |
| cf-page-merge | 3 | 3 | 7 / 12 / 9 |
| cf-parameter-validation | 3 | 3 | 7 / 6 / 8 |
| cf-price-discount | 3 | 2 | 8 / 6 / 7 |
| cf-record-schema-compat | 3 | 3 | 9 / 7 / 8 |
| cf-update-caller | 3 | 3 | 5 / 6 / 7 |
| dl-jsonl-line-diagnostics | 3 | 3 | 11 / 8 / 8 |
| dl-order-summary-pipeline | 3 | 3 | 9 / 8 / 9 |
| dl-regression | 2 | 2 | 6 / 3 / 5 |
| mm-early-constraint | 3 | 3 | 7 / 6 / 7 |
| mm-latest-correction | 3 | 3 | 7 / 6 / 8 |
| mm-partial-revision | 3 | 3 | 13 / 15 / 15 |
| mm-todo-once | 3 | 3 | 7 / 11 / 6 |
| rc-invalid-args | 3 | 3 | 8 / 8 / 10 |
| rc-patch-conflict | 3 | 3 | 7 / 7 / 6 |
| rc-provider-transient | 3 | 3 | 6 / 5 / 6 |
| rc-read-once | 3 | 3 | 7 / 8 / 7 |
| rc-search-truncated | 3 | 3 | 7 / 7 / 10 |
| sf-aware-time-order | 3 | 3 | 11 / 9 / 11 |
| sf-csv-records | 3 | 3 | 9 / 8 / 10 |
| sf-empty-input | 3 | 3 | 7 / 6 / 7 |
| sf-negative-boundary | 3 | 3 | 7 / 6 / 5 |
| sf-subtract | 3 | 3 | 6 / 7 / 6 |
| sf-tail-boundary | 3 | 3 | 6 / 8 / 7 |
| sf-unique-stable | 2 | 1 | 7 / 8 / 5 |
