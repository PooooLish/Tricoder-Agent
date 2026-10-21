# 2026-10-20 交付修复后：全量回归与真实模型重跑

用户授权“调用真实模型，重跑全量测试”。独立 reviewer 执行，生产代码、题库和默认配置均未修改。

## 结果

**完整本地回归通过；真实模型的产物质量较好，但完整交付仍未解决。**

本地命令 `.venv/Scripts/python.exe -X utf8 -B -m unittest discover -s tests -v`：**1815 tests，1800 通过、15 跳过、0 failures/errors，418.320 秒，exit 0**。15 个跳过涉及本机符号链接权限、Windows 文件锁/重命名、Linux 抽象 socket 和控制字符文件名；不计为通过。原搜索超时 P2 等回归包含在此次全量执行中。

| 指标 | 上轮 quality-v2.1，8轮 | 本轮 quality-v2.2，8轮 | 本轮 quality-v2.2，16轮 |
| --- | ---: | ---: | ---: |
| 真实质量题 | 19 | 19 | 19 |
| 产物正确 | 15/19（78.9%） | 18/19（94.7%） | 19/19（100%） |
| Agent 完成 / 完整通过 | 2/19（10.5%） | 1/19（5.3%） | 4/19（21.1%） |
| 范围合规 / 清理确认 | 各19/19 | 各19/19 | 各19/19 |
| user_turn 成功 | 2/22 | 4/22 | 7/22 |
| 原生评测耗时 | 230.280秒 | 240.031秒 | 373.014秒 |
| 输入 token | 513,372 | 511,763 | 926,117 |
| 输出 token | 22,727 | 20,169 | 33,279 |
| 缓存命中 token | 451,072 | 454,912 | 851,328 |
| 缓存未命中 token | 62,300 | 56,851 | 74,789 |

两组均 complete=true、无基础设施 error 或漏跑、exit 1；此退出码表示质量题存在失败，不是运行中断。共执行 **38 个真实质量 trial、44 个 user_turn**，不是只调用模型 38 次。11 道 contract 没有冒充真实质量题或加入上述分母；“全量本地回归”指完整 unittest discovery。

8 轮仅 `cf-update-caller` 完整通过。16 轮通过 `mm-todo-once`、`mm-early-constraint`、`rc-search-truncated`、`cf-backward-api`；相对 8 轮新增4题、失去1题，净增3题。输入 token 增加约81%、耗时增加约55%，完整率仍只有21.1%。

## 配置与可比性

- DeepSeek `deepseek-v4-flash`，每组每题1次、seed23、memory off/off、每个 user_turn 的 max_rounds 分别8/16；两组调度预算均3600秒，沿用项目的规划配置。全量本地回归先结束，随后串行执行8轮和16轮组，避免本地回归负载叠加。
- 由 `scripts/prepare_delivery_budget_experiment.py` 新生成 `runtime/delivery-real-20261020/frozen/`。当前592份源码/测试/脚本/题库文件与复审版本一致，运行前后 hash 一致。
- 两组用相同 `code/` 冻结源码执行；独立子进程验证 `tricoder.__file__` 位于冻结目录。模型、记忆、顺序、题面、夹具、verifier相同，只有19道quality的轮数及必要的实验id/路径不同。精确差异、源码hash和执行入口见 `execution-contract.json`、`frozen/source-sha256.json`、`frozen/allowed-delta.json`、`verification.json`。
- 严格比较器未修改，budgets/suite指纹不同；本报告明确以预算作为实验变量，不伪称严格比较器已接受配对。
- 与上轮v2.1相比同时改了Agent反馈/恢复及公开题面，因此只能作历史描述性对照，不能把分数变化单独归因于Agent修复。8/16也都是独立随机模型运行、每题单次；`cf-update-caller` 在16轮组反而失败，表明不能把每个差异都解释为预算效果。不宣称统计显著性。

## 失败原因：现在能区分预算与验证阻塞

| 最终失败原因（按失败user_turn计） | 8轮 | 16轮 |
| --- | ---: | ---: |
| round_budget_exhausted | 16 | 2 |
| verification_blocked | 2 | 13 |
| needs_input / policy_denied 终止 | 0 | 0 |

1. **增加轮数减少了预算耗尽，但大部分任务仍未完成验证闭环。** 16轮组的13个验证阻塞，在真实 `finish_assessment` 时3个为failed、10个为stale；10个仍有未解除检查，2个仍有未覆盖路径（这些条件可重叠）。2个预算耗尽题为 `cf-module-interface`、`sf-negative-boundary`。不能用隐藏verifier通过替代Agent自身证据，也不能仅凭这些汇总断言门禁误判。
2. **8轮的2个验证阻塞均有1条未覆盖路径。** `cf-price-discount` 在第8轮请求完成，assessment为observed、未解除检查0、pending路径1；`sf-unique-stable` 第7轮请求完成时stale、pending路径1。实际完成声明与门禁拒绝现在明确分开记录。
3. **前置交互契约生效，但不能代替后续编码交付。** 两组3道memory首轮全部在第1轮成功确认、未要求运行检查；两组没有ask_user或policy_denied终止。已有普通文件分别触发2/3次 `file_already_exists` 可恢复错误，不再因此直接终止。这些观察支持保留当前权限边界，本轮没有证据支持改默认relaxed/fullaccess。
4. **工具使用错误仍消耗轮数。** 8/16组分别记录：create_file无细分类invalid_argument 12/11次，list_files invalid_argument 7/9次，run_command execution_failed 8/13次，patch_count_mismatch 2/4次。事件数不等于失败题数，其中也包含评测注入的合成故障；未持久化的完整参数不能凭主错误码猜测。
5. **唯一产物错误涉及题面和工具故障的语义混淆。** 8轮 `rc-search-truncated` 的生成产物新增 `_first_search_done`、首次只返回一条结果，把截断写进了业务函数，违反返回全部匹配项的隐藏断言。题面“首次搜索结果会模拟截断”可能被理解为业务要求，这是基于产物的推断，不能声称知道模型内部原因。16轮本题实现正确且完成。后续可核查是否应明确“截断由宿主搜索工具注入，不属于find_prefix业务行为”；本轮没有修改题面或重解释旧分数。

诊断限制：Runtime失败收尾会把检查报告标记为stale，所以不能把最终turn的stale直接当作初始根因。上面的验证阻塞分类使用收尾前的 `finish_assessment`。旧日志仍缺少完整命令/outcome，历史未知值不回填；本轮同样未保存完整命令正文，若进一步定位到命令签名/快照层，需要独立受控回放。

## 逐题结果

下表“原因/轮数”取最后一个编码turn；3道memory的首轮均另有1轮成功确认。“完整”按全部user_turn及原有评分条件计算。

| 题目 | 8轮产物 | 8轮完整 | 8轮原因/轮数 | 16轮产物 | 16轮完整 | 16轮原因/轮数 |
| --- | --- | --- | --- | --- | --- | --- |
| mm-todo-once | 通过 | 失败 | 预算耗尽/8 | 通过 | 通过 | 完成/14 |
| rc-read-once | 通过 | 失败 | 预算耗尽/8 | 通过 | 失败 | 验证阻塞/10 |
| cf-price-discount | 通过 | 失败 | 验证阻塞/8 | 通过 | 失败 | 验证阻塞/11 |
| mm-early-constraint | 通过 | 失败 | 预算耗尽/8 | 通过 | 通过 | 完成/10 |
| sf-tail-boundary | 通过 | 失败 | 预算耗尽/8 | 通过 | 失败 | 验证阻塞/11 |
| rc-patch-conflict | 通过 | 失败 | 预算耗尽/8 | 通过 | 失败 | 验证阻塞/13 |
| sf-unique-stable | 通过 | 失败 | 验证阻塞/7 | 通过 | 失败 | 验证阻塞/13 |
| cf-module-interface | 通过 | 失败 | 预算耗尽/8 | 通过 | 失败 | 预算耗尽/16 |
| cf-update-caller | 通过 | 通过 | 完成/6 | 通过 | 失败 | 验证阻塞/12 |
| cf-parameter-validation | 通过 | 失败 | 预算耗尽/8 | 通过 | 失败 | 验证阻塞/11 |
| sf-negative-boundary | 通过 | 失败 | 预算耗尽/8 | 通过 | 失败 | 预算耗尽/16 |
| rc-invalid-args | 通过 | 失败 | 预算耗尽/8 | 通过 | 失败 | 验证阻塞/11 |
| mm-latest-correction | 通过 | 失败 | 预算耗尽/8 | 通过 | 失败 | 验证阻塞/10 |
| rc-provider-transient | 通过 | 失败 | 预算耗尽/8 | 通过 | 失败 | 验证阻塞/10 |
| rc-search-truncated | 失败 | 失败 | 预算耗尽/8 | 通过 | 通过 | 完成/10 |
| dl-regression | 通过 | 失败 | 预算耗尽/8 | 通过 | 失败 | 验证阻塞/10 |
| sf-subtract | 通过 | 失败 | 预算耗尽/8 | 通过 | 失败 | 验证阻塞/14 |
| sf-empty-input | 通过 | 失败 | 预算耗尽/8 | 通过 | 失败 | 验证阻塞/12 |
| cf-backward-api | 通过 | 失败 | 预算耗尽/8 | 通过 | 通过 | 完成/12 |

## 证据与交接

- 新证据目录：`runtime/delivery-real-20261020/`。运行器 `run_sequence.py`、分析器 `analyze.py`、逐组原始日志、completion JSON、冻结两组、`analysis.json`、`verification.json`、`additional-verification.json` 均保留。
- 8轮原生run：`runtime/evals/20261020t050936.402635z/`；16轮：`runtime/evals/20261020t051338.584537z/`。两组各19个唯一quality trial，逐题JSON与总报告逐项一致，重新计数指标一致，turn AND一致；82份新原生JSON/JSONL/Markdown建立hash索引。
- 2057份已保护的历史证据hash未变；旧结果未回写。592份源码/测试/脚本/题库和冻结代码/题库保持一致。凭据仅由原程序在进程内加载，未人工读取、展示或复制；没有读取用户数据库、安装、提交或推送。
- 上轮复审F1/F2/F3及原搜索P2、R1/R2的关闭结论维持；本轮完整率低不等于已通过的具体修复失效。此次发现的剩余瓶颈和题面疑点属于后续核查材料，不自动修改生产实现或推进Replan。
- 未验证其他Provider/模型、重复57次基线、其他OS/Python或真实网络故障；恢复题仍包含synthetic_tool_result/counter_only，memory off/off不能证明压缩或持久记忆效果，文件级范围快照不覆盖完整目录审计。下一步等待用户指定。
