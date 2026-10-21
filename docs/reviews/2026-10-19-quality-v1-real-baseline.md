# quality-v1 真实模型单次基线

日期：2026-10-19。用户明确授权使用真实密钥测试，并确认所有 Provider 配置均可用。本轮选择已说明的 DeepSeek `deepseek-v4-flash`，19 道 quality 题各 1 次；没有执行 11 道 contract 题，也没有扩展为三家模型比较或 57 次重复实验。

## 结果

- 全部 19 个 trial 完成，6 passed、13 failed、0 error、0 未运行；CLI 退出 1，表示存在失败题，不是运行器崩溃。
- 隐藏产物测试正确：14/19（73.7%）。Agent 正常完成：11/19（57.9%）。完整通过：6/19（31.6%）。
- 修改范围合规：13/19；报告清理确认：19/19。单次结果没有统计稳定性保证。
- 运行器耗时 234.078 秒。输入 334,184 tokens（其中缓存命中 289,280，未命中 44,904），输出 15,788 tokens。缓存命中已包含在输入中，不再次相加。未配置价格表，金额未知。

| 类别 | 实际题数 | 产物正确 | 完整通过 |
| --- | ---: | ---: | ---: |
| 单文件 | 5 | 5 | 2 |
| 跨文件 | 5 | 5 | 2 |
| 恢复 | 5 | 2 | 2 |
| 多轮记忆 | 3 | 2 | 0 |
| 交付 | 1 | 0 | 0 |

## 逐题结果

| 题目 | 产物正确 | 完整通过 | 本次观察 |
| --- | --- | --- | --- |
| sf-subtract | 是 | 是 | 正常完成 |
| sf-tail-boundary | 是 | 否 | 新增 test_sequence_ops.py，评分判 change_out_of_scope |
| sf-empty-input | 是 | 否 | 新增 test_stats_ops.py，评分判 change_out_of_scope |
| sf-unique-stable | 是 | 否 | 新增 test_collection_ops.py，评分判 change_out_of_scope |
| sf-negative-boundary | 是 | 是 | 正常完成 |
| cf-price-discount | 是 | 是 | 正常完成 |
| cf-parameter-validation | 是 | 是 | 正常完成 |
| cf-module-interface | 是 | 否 | 新增 test_api.py，评分判 change_out_of_scope |
| cf-update-caller | 是 | 否 | git_diff 被 policy_denied / stop_task 阻断；记录 agent_failed、agent_unverified |
| cf-backward-api | 是 | 否 | 新增 test_converter.py，评分判 change_out_of_scope |
| rc-read-once | 是 | 是 | 读取失败注入实际触发，随后恢复并完成 |
| rc-invalid-args | 否 | 否 | 调用 ask_user 得到 needs_input / stop_task；目标未改，参数故障尚未触发 |
| rc-provider-transient | 是 | 是 | 注册的透明重试模拟被记录，任务完成；不是实际网络故障重试实验 |
| rc-search-truncated | 否 | 否 | 注入 output_limit 后继续定位，随后 git_diff 被拒绝，未修改目标 |
| rc-patch-conflict | 否 | 否 | 注入后重读并修改，但仍只替换首个匹配；第 8 轮结束时尚无 finish |
| mm-early-constraint | 是 | 否 | 两轮最终代码符合颜色约束，第二轮 git_diff 被拒绝；整体合并为失败 |
| mm-latest-correction | 否 | 否 | 首轮 git_diff 被拒绝；第二轮错误地把空 latest 回退到 initial |
| mm-todo-once | 是 | 否 | 首轮 git_diff 被拒绝；第二轮代码和 finish 正常，但不能抹去首轮失败 |
| dl-regression | 否 | 否 | 把除零改为抛 ZeroDivisionError，隐藏测试要求 None；新增测试越界，第 8 轮仍未 finish |

分组可读为：6 题完整通过；5 题代码和 Agent 收尾正确但仅因额外测试文件越界失败；3 题产物正确但任务失败；5 题产物不满足隐藏测试。没有重算或覆盖原始分数。

## 解释分数时的边界

1. **修改范围没有完整传给模型。** 上述 5 道“只因新增测试越界失败”的题，case.toml 的 allowed_changes 只允许指定源文件；实际 user_turn 没有明确禁止新增测试。当前 service.py 传递 case.steps / case.task，不自动把 allowed_changes 加入提示。这里反映出任务描述与评分规则之间的差距，不能只依据最终 failed 认定函数修复能力不足。
2. **部分预期行为只存在于隐藏测试。** rc-invalid-args 没有明确描述要 trim；rc-patch-conflict 没有明确描述要替换全部匹配；dl-regression 没有明确描述除零应返回 None。本次分别出现请求澄清、保持首个替换、选择抛异常。保留原成绩，但需在解释中计入题目歧义。
3. **5 道题受到 git_diff 的停止行为影响。** 合成工作副本位于项目仓库内部，Git 根不等于任务工作区；当前工具的边界检查返回 policy_denied / stop_task。本次没有放宽策略或初始化新 Git 仓库来改变成绩。5 题中 3 题最终产物正确，另 2 题还存在未修改或代码错误。
4. **7 道题出现 apply_patch invalid_argument。** 审计仅保存参数统计，不能凭此还原模型补丁正文或断言具体格式根因；多道题随后改用 edit_file 成功。它表明工具调用摩擦，仍需另行诊断。
5. 多轮题使用原始上下文，结构化压缩和持久化均关闭；本次不构成持久记忆质量测量。单次运行也不代表跨重复稳定性或模型排名。

## 实验条件与证据

- Windows / Python 3.11.6，使用现有 .venv。每次 user_turn 按题目最多 8 轮，多轮题的合并 rounds 可超过 8；保留既有规划阶段和原题故障注入。
- 原题库完整复制到 `runtime/quality-v1-real-20261019/suite/`，实验文件 `experiment.toml` 指定 repetitions=1、seed=23、memory=off/off、quality 条件及 3600 秒调度预算。未修改题面、隐藏测试、评分或生产源码。
- 实际命令：`.venv/Scripts/python.exe -B -m tricoder eval --experiment runtime/quality-v1-real-20261019/experiment.toml --no-color`。
- 原生结果：`runtime/evals/20261019t091216.781907z/result.json`；原生报告：同目录 `report.md`；分题审计和合成工作副本保留在 `trials/`。
- Reviewer 汇总：`runtime/quality-v1-real-20261019/review-summary.json`、`audit-evidence.json`、`command-result.json`、`run.log`。源代码/正式测试/原题库相关 324 个 .py/.toml 文件前后哈希一致。
- 密钥由程序在授权范围内加载，不输出密钥正文，不将密钥写入实验清单或记录。未访问用户 Session 数据库；Eval 使用自己的临时 SQLite。未安装依赖、提交或推送。

## 历史与下一步

核对现存 runtime/evals 下结构化结果后，未找到 quality-v1 的既有真实完整基线。2026-10-08 的三家真实记录是 regression-v1 的 q-nested-create 单题冒烟：产物均正确，但都因缺少受信验证而未完成；它们早于后续修复，且题目/条件不同，不能用来直接计算本次提升幅度。

目前最有价值的后续是由用户决定是否先审查并对齐题面、允许修改范围和宿主工具可用性，再进行可比较的重复实验或三家模型对比。本轮只保存发现，不修改生产代码或评分规则；Replan 仍按用户要求搁置。
