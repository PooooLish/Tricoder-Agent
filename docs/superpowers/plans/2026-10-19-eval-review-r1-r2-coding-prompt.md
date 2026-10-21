# R1/R2 修复 Coding Session 提示词

将以下内容整段发送给 coding session：

```text
你是 TriCoder 项目的 coding 实施 session。请只修复本次独立复审发现的 R1/P2 和 R2/P3，完成代码、测试、验证和交接，不只提供建议。

工作区：D:\MaHong\AGENT_WORKSPACE_V2\projects\tricoder-cli
执行方案：docs/superpowers/plans/2026-10-19-eval-review-r1-r2-fixes.md
复审报告：docs/reviews/2026-10-19-eval-contract-tools-review.md
复现证据：runtime/eval-contract-tools-20261019/review-20261019/

先读 ../../AGENTS.md、项目 AGENTS.md、README.md、pyproject.toml、project.md，再读复审报告与执行方案全文。以本次方案为执行依据，上轮 eval-contract-and-agent-tools 方案仅作背景，不重新执行 A1/A2/B1/B2 或真实 A/B。

R1：当前 tests/*.py 按后缀匹配，tests/tests/test_probe.py 违反公开限制却通过评分。按方案新增显式 path_match_mode，默认 legacy 保持旧语义；仅当前 quality-v2 的19道质量题 opt-in workspace_root。按路径分段、同段数、大小写敏感匹配，通配符不跨目录；allowed 和 required 两处统一接线，拒绝新模式不支持的 ** 和非法路径。更新 benchmark_version 为 quality-v2.1，保持 suite id/目录不变，旧冻结副本与分数不改。不得用目录名黑名单或全局替换旧 matcher 解决。

R2：User turns/Fault evidence 的每条实际行和旧 unknown 行补 Condition、Case、Repetition，从已验证的 TrialKey 取值；没有触发故障不造行。不改 JSON schema、结果事实或评分。

实施方式：使用 superpowers:executing-plans，单 session 逐任务 RED→最小实现→GREEN→自审，不使用子 Agent。复现必须走真实隔离评分器并核对 modified_files；报告测试必须检查每行的身份，不能只检查全文含有模型/条件名称。required_changes、旧 mode、旧报告、跨 benchmark 比较拒绝也要有回归。

边界：
- 工作树有多批未提交/未跟踪内容，HEAD 不是精确 before。先保存实际涉及文件内容/hash和状态到 runtime/eval-review-r1-r2-20261019/ 下新的独占目录；保留全部现有修改，不 reset/clean/覆盖、不从 HEAD 重建、不因 worktree 丢失改动。
- 不修改旧 quality-v1/regression/smoke、历史 runtime证据、冻结 A/B、v2 workspace/hidden verifier 或11道 contract 的 case.toml；不回写旧成绩。
- 不修改已通过的 Git helper、补丁反馈、Agent/Provider、取消/清理/UNKNOWN门禁。不推进 Replan、记忆、预算优化或其他历史事项。
- 不安装依赖，不读取 .env.local/真实凭据/用户数据库，不提交或推送项目，不创建新会话或子Agent。本轮离线验证即可，不新增真实模型调用，不重新询问真实密钥授权。
- 当前评分快照是文件级，本轮不扩展目录快照；如实说明此限制，不用目录字符串匹配测试声称完成目录级审计。

请完成方案列出的聚焦与相关回归，保存独立日志、R1真实评分结果及R2多trial示例报告；不要覆盖reviewer旧探针和日志。发现实际代码与方案冲突时先报告具体证据，不扩大到计划外重构。

最后中文交付：逐项改动、精确before/增量diff、RED/GREEN命令与退出码/失败/skips、旧题库和历史证据保留结果、限制及未验证范围。更新 project.md 和 docs/reviews/2026-10-19-reviewer-handoff.md，状态为“R1/R2实现完成，待独立复审”，保留原未通过结论，不替reviewer关闭发现。完成后停止，不自动继续下一任务。
```
