# Quality V3 真实模型30×3复测（2026-10-27）

**已完成90次真实试次，产物正确90/90，四维完整交付87/90。** complete=true，30题每题恰好repetition1/2/3，没有取消、漏跑或选择性补跑。CLI exit=1；非零质量退出不等于实验未完成。

## 配置和执行边界

- 用户本轮明确授权30题各3次。使用独立复审通过且hash一致的代码；DeepSeek `deepseek-v4-flash`，quality-v3，单condition quality-baseline，16轮/user_turn、16000上下文、memory off/off、seed23、实验预算21600秒。seed仅控制调度顺序，不是模型采样seed。
- 冻结323份源码/题库文件，使用冻结代码实际导入；每trial独立工作区和Session。没有更换模型、放大预算、修改权限/隐藏评分/四维AND、修复生产代码或选择性重跑。condition的none不取消case既定受控故障；rc-search-truncated的tool_output_truncated属于合成工具输出，不是真实正则超时。
- 原生结果：`runtime/evals/20261027t055018.826595z/result.json`；新证据：`runtime/quality-v3-repeat3-real-20261027/`。原始日志、失败与旧实验保留。
- 本轮没有重跑2060项离线全量；前一轮独立复审的通过版本在真实测试前后均校验hash。新增离线oracle只用于解释当前失败，不加入90次样本或追认完整交付。

## 成功指标

| 成功口径 | k=1 | k=2 | k=3 |
| --- | ---: | ---: | ---: |
| 产物 pass@k | 100.0000% | 100.0000% | 100.0000% |
| 完整交付 pass@k | 96.6667% | 98.8889% | 100.0000% |
| 完整交付 pass^k | 96.6667% | 94.4444% | 93.3333% |

以题等权宏平均；每题n=3，一个sample是整题隔离执行，多turn/工具轮次不算新sample。pass@k为从已记录重复中选k次至少一次成功的组合估计；pass^k为k次全成功估计。完整交付要求产物、Agent完成、范围和清理四维AND；artifact通过不能替代finish。

- 每次重复的计数：`{"1": {"artifact": 30, "delivery": 30}, "2": {"artifact": 30, "delivery": 29}, "3": {"artifact": 30, "delivery": 28}}`。
- 产物成功次数分布：`{"3": 30}`；完整交付分布：`{"1": 1, "2": 1, "3": 28}`。键表示每题3次中的成功次数，值表示题数；没有出现的桶为0题。
- split计数：`{"dev": {"trials": 66, "artifact": 66, "delivery": 63}, "holdout": {"trials": 24, "artifact": 24, "delivery": 24}}`；完整原始维度计数：`{"artifact_correct": {"True": 90}, "agent_completed": {"True": 87, "False": 3}, "scope_compliant": {"True": 90}, "cleanup_confirmed": {"True": 90}}`。
- 结果状态：`{"passed": 87, "failed": 3}`；失败turn停止原因：`{"round_budget_exhausted": 3}`。

## Token用量

| 通道/字段 | 严格总量 | 均值 | 中位数 | P90 | P95 | 最大 | 已知/缺失 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| main/input_tokens | 4,356,678.00 | 48,407.53 | 43,241.50 | 74,174.00 | 104,924.00 | 166,149.00 | 90/0 |
| main/output_tokens | 221,098.00 | 2,456.64 | 2,045.00 | 4,155.00 | 5,200.00 | 9,604.00 | 90/0 |
| main/reported_total_tokens | 4,577,776.00 | 50,864.18 | 45,305.00 | 78,551.00 | 109,276.00 | 175,753.00 | 90/0 |
| main/cached_tokens | 3,851,776.00 | 42,797.51 | 39,424.00 | 64,000.00 | 91,904.00 | 149,248.00 | 90/0 |
| main/cache_miss_tokens | 504,902.00 | 5,610.02 | 3,902.00 | 10,650.00 | 11,988.00 | 16,901.00 | 90/0 |
| summary/input_tokens | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 90/0 |
| summary/output_tokens | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 90/0 |
| summary/reported_total_tokens | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 90/0 |
| summary/cached_tokens | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 90/0 |
| summary/cache_miss_tokens | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 90/0 |

每次完整成功的main已报告token摊销：**52,618.11**，reason=None。分子包括失败试次消耗，缓存token是input子集，不再次相加。缓存加权命中率：88.4108%。

summary原始usage为空，但90行summary_count=0、summary_triggered=False且字段全缺失时按既定派生规则记0，原始行不改。main/summary保持独立通道。本表标记provider_reported、billing_completeness=unknown；累计字段不证明覆盖了每次失败API请求，不计算货币费用。

最高main token的5个试次（token、case、repetition、ordinal）：`[[175753, "sf-aware-time-order", 3, 66], [139558, "sf-aware-time-order", 2, 36], [113492, "dl-jsonl-line-diagnostics", 3, 87], [110124, "cf-page-merge", 3, 89], [109276, "mm-partial-revision", 3, 83]]`。

## 任务耗时与执行开销

整批实验墙钟 **2881.702秒（48.03分钟）**；外层CLI进程墙钟2890.688秒。下表是90个任务观测值的分布，不把trial时间总和当成整批墙钟。

| 时间口径 | 总和(s) | 均值(s) | 中位数(s) | P90(s) | P95(s) | 最大(s) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 整题total | 2,881.232 | 32.014 | 30.638 | 44.036 | 53.889 | 65.572 |
| Agent callback | 2,850.406 | 31.671 | 30.337 | 43.733 | 53.549 | 65.253 |
| 隐藏验收 | 22.097 | 0.246 | 0.230 | 0.299 | 0.389 | 0.556 |
| 框架其他开销 | 8.729 | 0.097 | 0.097 | 0.117 | 0.125 | 0.160 |

Agent callback包含Session初始化、多turn、工具、模型等待及自身清理，不能当作纯推理时间；total还包括隐藏验收、框架准备和最终verifier清理。每行total=agent+verifier+harness已核对。P90/P95为nearest-rank，中位数偶数取两者平均。

legacy duration均值31.991秒、中位数30.611秒，保持旧口径，不冒充新阶段。整题rounds总量908、均值10.09、最大19；tool_calls总量1290；记录的retries总量12。多turn题整题轮数可能超过16，预算是每user_turn。

## 失败原因与后续方向

### ordinal 36 / sf-aware-time-order / repetition 2

- 产物True、agent_completed False、scope/cleanup True；16轮、19工具，stop_reason=round_budget_exhausted，finish_outcome=null，没有finish_assessment或纠正记录。
- 总耗时59926ms，agent59632、verifier208、harness86；input131713+output7845=139558 reported tokens。
- round1～3读取文件/说明/公开测试；round4 create_file/file_already_exists；round5/7/8 apply_patch/patch_count_mismatch，round6 apply_patch/invalid_argument（无detail，不能猜具体格式错误）。
- round9 edit_file写time_order.py（new_text_chars2986）；round10创建tests/test_sort_events.py（3147字符）。round11、13 unittest执行失败；round12、15编辑该测试文件，round14读取该测试文件。round16 unittest成功，然后预算耗尽，没有finish。
- 原始审计对命令参数和旧文本脱敏，未记录完整失败stdout，不能断言round11/13究竟是哪条断言失败；可以确定两次失败之后修改的是自建测试文件，最后测试成功。
- 独立副本离线验证：oracles/0036/tests.log，9 tests全部通过、exit0；原生5份文件hash未变。只验证最终产物，不增加真实trial、不追认完整交付。
- 相同题第一次ordinal6完整通过，但也耗14轮/15工具、47269ms，input90390+output5806=96196。两次均自行实现较大的时间解析器；可观察的主要浪费是写入协议错误及测试反复纠正，不把“代码较长”单独当成失败根因。
- 对应宿主路径：src/tricoder/patches.py:227/243/251为hunk声明/实际行数不符；tools/write.py:300为普通已有文件的create_file拒绝，需read/edit；并非提高审批权限能改变的工具契约。
- 当前判断：模型/Agent工具使用与预算分配问题，产物本轮符合隐藏验收；没有证据指向该题评分误拒绝或取消/清理/权限缺陷。建议优先让重复补丁错误后更早切换已有edit_file、控制额外实现/测试范围并为验证和finish预留轮次。这里仅建议，不实施，不通过增加预算重跑选择性提高成绩。

### ordinal 66 / sf-aware-time-order / repetition 3

- 同样产物True、完整False、scope/cleanup True；16轮/19工具，stop_reason=round_budget_exhausted。total65572ms=agent65253+verifier207+harness112；input166149+output9604=175753 tokens。
- rounds1～4读取；round5 create_file/file_already_exists；round6～7 patch_count_mismatch；round8 edit_file实现time_order.py；round9添加tests/test_sort_events.py；round10 unittest失败，round11改实现，round12 unittest失败，round13改测试，round14 unittest成功。
- round15 finish(outcome=completed)被拒绝，pending_paths=[time_order.py]、unresolved_check_count=0；触发唯一一次missing_relevant_verification纠正。round16仅read_file(time_order.py)，随后耗尽预算；没有补充检查或再finish。
- 与36不同，66不是从未尝试finish，而是过晚尝试且当前检查覆盖不满足宿主口径。两次都经历工具协议错误和测试纠正，预算压力明显。

### 已复现的文档/验证覆盖衔接问题（不能混同隐藏评分误判）

- 冻结题目workspace/README.md推荐 `python -m unittest discover -s tests -v`，并要求按当前验证要求后finish；此命令仅被tools/command.py:53-94分类为targets=(tests,)。workspace/check_coverage.py:24-36只按路径覆盖，不追踪Python import执行依赖；engine/completion.py:215后的覆盖并集仍保留根目录time_order.py。
- `oracle_0066.py`在最终快照独立副本执行上述README命令以及根目录`python -m unittest discover -v`，两者都测试通过；前者生产分类/覆盖函数得到pending time_order.py，后者targets=(.,)、pending空。原生文件不变。证据：oracles/0066/{readme.log,root-discovery.log,result.json}。
- 该探针是“最终产物运行 + 生产分类/路径覆盖纯函数”，不是完整Runtime重放；真实round14的完整argv已脱敏，不能声称已从日志证明其准确命令。但README、记录的module/cwd/参数数、finish pending以及独立机制复现相互一致。
- 因此可确认评测操作说明与宿主覆盖规则衔接不足，是额外finish纠正开销的可复现来源；具体历史因果保留上述限制。建议独立后续修订公开指引，选用经全库离线验证、宿主可认可的根目录发现命令，或明确补足修改文件检查；保持隐藏验收和四维AND不变。若要改变宿主覆盖算法，应另行设计可信证据机制，不能把任意测试成功直接视为全工作区验证。
- 两例不需要提高默认relaxed/fullaccess来解决：记录的是invalid_argument/测试失败/未满足验证覆盖，未出现policy_denied或清理失败。优先改善工具策略、预算分配、覆盖反馈和评测指引；不自动增加预算或放宽完成门禁。

### ordinal 82 / rc-search-truncated / repetition 3

- 产物True、完整False、scope/cleanup True，16轮/17工具、round_budget_exhausted。total44036ms，input73014+output2871=75885 tokens。
- 本题配置的tool_output_truncated是既定合成工具结果故障（fault_evidence=synthetic_tool_result），不是本次真实正则搜索超时/cleanup故障。round2 read_file返回OUTPUT_LIMIT；round3 search_text及round6 read_file为invalid_argument；round8 create_file/file_already_exists；round10/11 patch_invalid_hunk。
- round12 edit_file实现searcher.py，round13新增tests/test_find_prefix.py，round14 unittest成功，round15 finish因pending searcher.py触发missing_relevant_verification纠正；round16 compileall成功后预算耗尽，未能再次finish。
- 最终产物在独立副本以README命令和根目录discover分别跑11项测试，均通过；生产命令分类/路径覆盖函数分别给pending searcher.py和空pending。与66相同，只是最终快照和纯函数机制验证，不冒充原始完整argv重放。见oracles/0082/。
- 失败由工具纠错消耗及较晚满足验证覆盖后无交付轮次共同造成，属于Agent执行效率与公开验证流程衔接问题。已配置合成截断消耗属于既定测评条件，不能直接作为服务商API重试或真实search timeout复发证据。原搜索P2关闭结论不变。


中途工具错误总计142次，分类：`{"apply_patch/invalid_argument/patch_count_mismatch": 51, "read_file/execution_failed/": 3, "create_file/invalid_argument/file_already_exists": 43, "run_command/execution_failed/": 18, "run_command/invalid_argument/": 6, "edit_file/execution_failed/": 3, "edit_file/invalid_argument/": 3, "read_file/output_limit/": 3, "search_text/invalid_argument/": 3, "apply_patch/invalid_argument/patch_invalid_hunk": 5, "apply_patch/invalid_argument/patch_invalid_header": 1, "apply_patch/invalid_argument/": 1, "create_file/invalid_argument/": 1, "read_file/invalid_argument/": 1}`。finish纠正共47次，分类：`{"missing_relevant_verification": 47}`；47个触发纠正的试次中，45个最终完整通过。以上中途错误与最终失败分别计数，不能相加当成失败trial数。

本轮证据支持两条后续工作：一是改进Agent的工具选择/同类补丁失败切换/预算分配；二是完善评测公开指引与宿主验证覆盖规则的衔接。没有证据要求提高默认relaxed/fullaccess或放宽finish四维门禁。本轮只分析，不实施修复。

## 逐题结果

| 题目 | split | 产物成功/3 | 完整成功/3 | 三次完整交付（按repetition） |
| --- | --- | ---: | ---: | --- |
| cf-backward-api | dev | 3/3 | 3/3 | 通过，通过，通过 |
| cf-cache-copy-isolation | holdout | 3/3 | 3/3 | 通过，通过，通过 |
| cf-cli-result-contract | holdout | 3/3 | 3/3 | 通过，通过，通过 |
| cf-config-precedence | dev | 3/3 | 3/3 | 通过，通过，通过 |
| cf-module-interface | dev | 3/3 | 3/3 | 通过，通过，通过 |
| cf-package-api-migration | holdout | 3/3 | 3/3 | 通过，通过，通过 |
| cf-page-merge | holdout | 3/3 | 3/3 | 通过，通过，通过 |
| cf-parameter-validation | dev | 3/3 | 3/3 | 通过，通过，通过 |
| cf-price-discount | dev | 3/3 | 3/3 | 通过，通过，通过 |
| cf-record-schema-compat | holdout | 3/3 | 3/3 | 通过，通过，通过 |
| cf-update-caller | dev | 3/3 | 3/3 | 通过，通过，通过 |
| dl-jsonl-line-diagnostics | holdout | 3/3 | 3/3 | 通过，通过，通过 |
| dl-order-summary-pipeline | holdout | 3/3 | 3/3 | 通过，通过，通过 |
| dl-regression | dev | 3/3 | 3/3 | 通过，通过，通过 |
| mm-early-constraint | dev | 3/3 | 3/3 | 通过，通过，通过 |
| mm-latest-correction | dev | 3/3 | 3/3 | 通过，通过，通过 |
| mm-partial-revision | holdout | 3/3 | 3/3 | 通过，通过，通过 |
| mm-todo-once | dev | 3/3 | 3/3 | 通过，通过，通过 |
| rc-invalid-args | dev | 3/3 | 3/3 | 通过，通过，通过 |
| rc-patch-conflict | dev | 3/3 | 3/3 | 通过，通过，通过 |
| rc-provider-transient | dev | 3/3 | 3/3 | 通过，通过，通过 |
| rc-read-once | dev | 3/3 | 3/3 | 通过，通过，通过 |
| rc-search-truncated | dev | 3/3 | 2/3 | 通过，通过，round_budget_exhausted |
| sf-aware-time-order | dev | 3/3 | 1/3 | 通过，round_budget_exhausted，round_budget_exhausted |
| sf-csv-records | dev | 3/3 | 3/3 | 通过，通过，通过 |
| sf-empty-input | dev | 3/3 | 3/3 | 通过，通过，通过 |
| sf-negative-boundary | dev | 3/3 | 3/3 | 通过，通过，通过 |
| sf-subtract | dev | 3/3 | 3/3 | 通过，通过，通过 |
| sf-tail-boundary | dev | 3/3 | 3/3 | 通过，通过，通过 |
| sf-unique-stable | dev | 3/3 | 3/3 | 通过，通过，通过 |

## 独立核对、保全和限制

- 90个唯一身份、每题3次、原生逐题文件与总报告完全一致；90个独立workspace，审计Session标识数90，没有跨trial复用。未读取Session数据库。
- 独立按原始行枚举k子集重算pass@k/pass^k，逐字段核对token分布、时间分布及阶段守恒，与原生quantification一致。见`analysis.json`、`verification.json`及`summarize_run.py`。
- 另以冻结代码运行真实`eval-analyze --result ... --plan ... --ks 1 2 3`离线CLI，exit0，派生quantification与原生结果完全相同，输入hash不变；见`offline-analysis-verification.json`及`offline-analyze.log`。该步骤没有模型调用。
- 821份源码/测试/脚本/题库、323份冻结文件、19,961份受保护历史文件hash保持。新增结果和报告另存，不覆盖旧证据；完整索引见native-evidence-sha256.json和最终seal。
- 这是30题×3、单模型/单平台/有限隐藏verifier的观测，不能称统计显著、全场景可靠或作者盲测。旧2026-10-21单次基线为产物29/30、完整27/30；本轮与旧单次差异不能归因于仅新增观测指标的代码修改，也不直接混并为每题4次样本。
- 其他模型/平台、记忆开启、长期任务和真实故障注入未验证；此前Windows离线15项skip仍不在本轮覆盖内。失败命令的完整argv/旧文本未保存在脱敏审计中，相关根因推断已与可复现机制分开说明。
- 密钥只由既有程序加载，未人工读取/显示凭据或.env.local，未读取用户数据库、安装依赖、提交/推送。Q3-R1/R2/R3、量化指标复审及原搜索P2既有结论保持；不自动推进Replan。
