# 交给 Coding Session 的提示词

将下面内容整段发送给在本项目工作的 coding session；主方案是执行依据，无需依赖 reviewer 聊天历史。

```text
你是 TriCoder 项目的 coding 实施 session。请实施以下方案，不只做审查或建议：

工作区：D:\MaHong\AGENT_WORKSPACE_V2\projects\tricoder-cli
主方案：docs/superpowers/plans/2026-10-19-eval-contract-and-agent-tools.md

先读工作区 ../../AGENTS.md、项目 AGENTS.md、README.md、pyproject.toml、project.md，再读：
- docs/reviews/2026-10-19-quality-v1-real-baseline.md
- docs/reviews/2026-10-19-quality-v1-failure-analysis.md
- 上述主方案全文

本轮范围已确定，按主方案小步实施，不要重新开启泛化设计讨论。先核对当前代码和现有修改，记录精确 before；只有遇到与方案不兼容的实际状态或不可安全执行的阻塞时才暂停相应部分，并说明具体原因。

按顺序完成：
A1：新增 quality-v2，公开允许修改范围，补足明确的业务需求；保留原 quality-v1 和全部旧成绩。
A2：保留逐轮失败事实，明确模拟故障的证据类型；完成后先运行当前 Agent 在修订题库上的 19 题基线并冻结。
B1：补丁格式说明、可脱敏错误子类型及正反例；不扩展补丁语法、不泄漏补丁正文。
B2：固定 git_diff 在已确认父仓库场景返回无副作用的不可用结果，使任务可以继续；不运行父仓库 diff，不改变普通命令/全局策略拒绝、取消、清理失败和 UNKNOWN 门禁。完成后在同一冻结题库运行另外 19 题。

使用 superpowers:executing-plans 逐任务实施，不启动并行子 Agent。各任务先建立有行为意义的失败测试，再最小实现、验证和自审，记录 RED/GREEN。新字段必须兼容旧调用和旧结果；遵守主方案的接口、默认值、错误白名单、隐私和测试要求。

重要限制：
- 工作树有多批未提交/未跟踪内容，HEAD 不是本次 before。保留所有现有修改，不 reset/clean/覆盖，不从 HEAD 重建，不为 worktree 迁移丢失未提交内容。
- 本轮日志和源码快照放在 runtime/eval-contract-tools-20261019/ 下独占的新目录，不覆盖旧记录；不要复制整个工作目录、凭据或用户数据库。
- 不读取或打印 .env.local/真实密钥正文，不访问用户数据库，不安装依赖，不提交或推送项目。
- 用户已允许真实模型测试，所有配置可用。本轮按主方案仅做 DeepSeek deepseek-v4-flash A/B 各 19 次，密钥只由现有配置加载器在程序内使用。不要重新索要密钥，不输出配置对象，不扩展成三家模型/多次重复测试。加载或网络失败如实记录，不伪造完整基线。
- Replan 项目继续搁置；使用现有 RecoveryAction.REPLAN 不等于启动新规划框架。不得推进长期记忆、上下文压缩、提高 max_rounds 或其他历史待办。
- 不通过放宽隐藏测试、把失败清零、忽略失败轮、给所有评测副本初始化 Git 或题目 ID 特判来提分。
- A/B 的题库、模型、种子、预算、记忆配置必须一致；A 必须在 B 的 Agent 修改之前执行。不能把题面修订带来的涨分算作 Agent 改进。

最终交付：
1. 四项任务的具体改动、精确 before/增量证据位置。
2. focused/full regression 的实际命令、退出码、失败与 skips；原搜索超时 P2 的回归结果。
3. A/B 原生 run 路径、逐题对照、分项分数、token/耗时、故障触发与逐轮停止原因；单次结果不宣称统计显著。
4. 尚未解决的问题、风险和未验证范围；无法执行的项明确标出。
5. 更新 project.md 和 docs/reviews/2026-10-19-reviewer-handoff.md 的最新状态为“实现完成，待独立复审”，保留旧记录。

请用中文汇报。不要替 reviewer 宣布独立验收通过，不自动继续计划外工作。
```
