# 2026-10-20 验证交付修复后：全量回归与真实模型重跑

用户授权“调用真实模型，重跑全量测试”。由独立 reviewer 执行；没有修改生产代码、测试、题库、默认权限或默认轮数。

## 结论

**全量本地回归通过；16轮真实质量完整交付由4/19提高到16/19，产物保持19/19正确，但尚未全通过。8轮仍明显受交付预算限制。**

本地执行 `.venv/Scripts/python.exe -X utf8 -B -m unittest discover -s tests -v`，1838 tests：1823通过、15跳过、0 failures/errors，456.019秒，exit0。跳过包括11项符号链接权限和4项OS/文件系统限制，不能算作通过；详细名称见 analysis.json。原搜索P2及本轮验证交付相关回归包含在本次全量中。

| 指标 | 修复前8轮 | 修复后8轮 | 修复前16轮 | 修复后16轮 |
| --- | ---: | ---: | ---: | ---: |
| 产物正确 | 18/19 | 17/19 | 19/19 | 19/19 |
| 完整交付/整题通过 | 1/19 | 3/19 | 4/19 | 16/19 |
| 范围合规/清理确认 | 19/19、19/19 | 19/19、19/19 | 19/19、19/19 | 19/19、19/19 |
| user_turn成功 | 4/22 | 6/22 | 7/22 | 19/22 |
| 预算耗尽终止 | 16 | 16 | 2 | 2 |
| 验证阻塞终止 | 2 | 0 | 13 | 0 |
| 模型声明未完成 | 0 | 0 | 0 | 1 |
| 原生评测耗时/秒 | 240.031 | 258.704 | 373.014 | 497.670 |
| 输入token | 511,763 | 541,799 | 926,117 | 971,823 |
| 输出token | 20,169 | 20,108 | 33,279 | 33,103 |
| 缓存命中token | 454,912 | 479,744 | 851,328 | 880,384 |
| 缓存未命中token | 56,851 | 62,055 | 74,789 | 91,439 |

16轮完整率21.1%→84.2%，净增12题：新增13题通过、失去rc-search-truncated这1题。8轮完整率5.3%→15.8%，产物正确率94.7%→89.5%。单次结果不能证明稳定收益或单项改动的因果贡献。

两组各19道quality、各1次，共38个真实trial、44个user_turn；不等于只调用模型38次。两组complete=true、exit1，均无基础设施error或漏跑；exit1表示存在质量失败。11道contract由本地相关回归覆盖，没有作为真实模型质量题加入分母。

## 配置、对照与证据完整性

- DeepSeek `deepseek-v4-flash`，quality-v2.2，memory off/off，seed23，每user_turn分别8/16轮，调度预算3600秒。先完成本地全量回归，再串行执行真实8轮、16轮。凭据仅由程序内部加载，未查看或输出凭据正文，也未读取用户数据库。
- 595份源码/测试/脚本/题库与独立复审版本一致，执行前后hash未变。新冻结代码实际被导入；frozen/code未变。冻结题库与上一轮完全一致；8/16两份suite仅19个case.toml的max_rounds存在8→16差异。
- 同预算的新旧报告通过未修改的严格比较器 `allowed_variables={'code'}`；实际fingerprint变化仅code，题面、评分、预算、模型等其他指纹相同。比较文件：comparison-code-budget8.json、comparison-code-budget16.json。
- 上述是同预算的代码版本对照；8轮与16轮之间另有预算变量。每题只有1次、单模型单平台，没有重复基线或统计显著性检验；seed控制调度，不保证远端生成确定性。
- 逐题文件与汇总完全一致，19个唯一quality身份、repetition、指标及多turn AND独立核对通过；4453份受保护历史文件hash不变。旧日志未覆盖。

## 剩余失败与实际恢复路径

16轮剩余3题的隐藏verifier均通过，故产物正确；失败集中在Agent交付。审计显示以下具体过程：

| 题目 | 实际过程 | 结论边界 |
| --- | --- | --- |
| rc-read-once | 第15轮finish时observed、未解除检查0、pending parser.py；触发一次missing_relevant_verification纠正。第16轮run_command成功，随后预算耗尽，没有下一次finish。前面另耗费读取故障恢复、工具参数纠正和测试发现/包初始化步骤。 | 是纠正发生太晚后的预算终止；不是历史stale或未解决失败导致的终止。审计未保留完整命令和输出，不能仅由成功退出推断最后一步已取得所有认可覆盖。 |
| sf-empty-input | 第15轮finish时observed、未解除检查0、pending stats_ops.py；第16轮纠正命令成功，仍耗尽预算。第6/8/9轮重复patch_count_mismatch，之后改用edit_file；第12轮命令失败，补tests/__init__.py后第14轮成功。 | 轮数被补丁与验证步骤消耗，收尾时间不足；没有自动增加预算或绕过门禁。 |
| rc-search-truncated | 第3轮成功读取searcher.py，第7轮修改，第8轮compileall成功；第9轮模型主动finish(incomplete)。此时observed、pending0、未解除检查0；终止原因为model_incomplete。 | 不是验证门禁拒绝完成声明。脱敏审计只保留summary长度，无法据此还原模型为何选择incomplete；没有为此读取会话数据库。 |

注意：失败turn最终validation_status=stale不能倒推finish当时已stale。这里引用的是finish_assessment瞬间的状态，两项预算失败均为observed，未解决检查均为0。

16轮共触发10次一次性finish纠正：9次missing_relevant_verification、1次unresolved_verification。其中8题最终完整通过，另2题在第16轮耗尽预算。mm-todo-once第13轮触发unresolved纠正后最终通过，这是新恢复分支在真实调用中实际走通的证据；不代表可以忽略真实验证失败。两组没有verification_blocked、needs_input、policy_denied最终终止，也没有scope/cleanup失败。

8轮有16题预算耗尽，只有cf-price-discount、cf-update-caller、sf-empty-input完整通过。产物失败为：

- cf-module-interface：formatter.py增加新helper及局部class api，但api.py仍调用旧单参数format_name(first,last)，实际模块调用方未修复；不能因出现同名class就算跨文件接口修复完成。
- rc-search-truncated：searcher.py未发生要求的改动，仍为大小写敏感startsWith语义（Python startswith）；required_change_missing。不是上一轮“把模拟截断实现进业务”的同一产物错误。

这些结果支持下一步优先调查工具调用/补丁反复纠错、测试发现与认可覆盖反馈，以及主动incomplete的成因。本轮没有显示需要relaxed/fullaccess的证据；修改默认权限不能解释或解决已观察的预算与模型完成声明问题。未自动实施任何后续修复或推进Replan。

## 逐题结果

表中“产物/交付”分别对应artifact_correct和整题passed；多轮题rounds为user_turn合计，因此可能超过单turn的8/16上限。

| 题目 | 8轮产物/交付 | 8轮合计rounds | 16轮产物/交付 | 16轮合计rounds |
| --- | --- | ---: | --- | ---: |
| mm-todo-once | 正确/失败 | 9 | 正确/通过 | 16 |
| rc-read-once | 正确/失败 | 8 | 正确/失败 | 16 |
| cf-price-discount | 正确/通过 | 5 | 正确/通过 | 9 |
| mm-early-constraint | 正确/失败 | 9 | 正确/通过 | 12 |
| sf-tail-boundary | 正确/失败 | 8 | 正确/通过 | 12 |
| rc-patch-conflict | 正确/失败 | 8 | 正确/通过 | 15 |
| sf-unique-stable | 正确/失败 | 8 | 正确/通过 | 11 |
| cf-module-interface | 失败/失败 | 8 | 正确/通过 | 12 |
| cf-update-caller | 正确/通过 | 5 | 正确/通过 | 5 |
| cf-parameter-validation | 正确/失败 | 8 | 正确/通过 | 12 |
| sf-negative-boundary | 正确/失败 | 8 | 正确/通过 | 11 |
| rc-invalid-args | 正确/失败 | 8 | 正确/通过 | 9 |
| mm-latest-correction | 正确/失败 | 9 | 正确/通过 | 13 |
| rc-provider-transient | 正确/失败 | 8 | 正确/通过 | 13 |
| rc-search-truncated | 失败/失败 | 8 | 正确/失败 | 9 |
| dl-regression | 正确/失败 | 8 | 正确/通过 | 6 |
| sf-subtract | 正确/失败 | 8 | 正确/通过 | 11 |
| sf-empty-input | 正确/通过 | 8 | 正确/失败 | 16 |
| cf-backward-api | 正确/失败 | 8 | 正确/通过 | 15 |

## 留档与未验证范围

- 新证据目录：`runtime/verification-delivery-real-20261020/`。入口run_sequence.py；独立核对analyze.py；full-regression.log、real-8.log、real-16.log及completion.json保留命令、退出码和耗时。analysis.json为最终核对结果，partial-budget8.json仅为中途诊断，不用于最终工具错误统计。
- 原生8轮：`runtime/evals/20261020t075021.335002z/`；16轮：`runtime/evals/20261020t075443.294504z/`。剩余3题分别为16轮trials/0002、0015、0018的audit；8轮产物失败分别为trials/0008、0015的workspaces。
- initial-contract.json、source-before/after.json、frozen/source-sha256.json、verification.json、native-evidence-sha256.json及final-verification.json记录版本、导入、数据复核与保全检查。
- 本轮未跑其他模型/Provider、每题3次重复、其他OS/Python组合或真实网络故障场景；故障题由套件注入，不能外推为真实服务中断验证。15项平台/权限skip保持未验证；评分仍是现有有限文件快照范围。
- 独立复审通过及V1/V2关闭、V3验收结论维持，旧搜索P2与F1/F2/F3、R1/R2维持关闭；本次真实质量未全通过，不等于重开已修复的门禁缺陷。无生产修改、依赖安装、提交/推送或子Agent。
