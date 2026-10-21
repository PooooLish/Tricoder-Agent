# 交给 coding session 的提示词

你是 TriCoder 本轮 coding 实施会话。工作区：`D:\MaHong\AGENT_WORKSPACE_V2\projects\tricoder-cli`。请用中文，单会话完成本包，不派生子 Agent。

先读 `../../AGENTS.md`、项目 `AGENTS.md`、README.md、pyproject.toml、project.md 和 `docs/reviews/2026-10-19-reviewer-handoff.md` 的最新条目，再完整阅读：

1. `docs/reviews/2026-10-20-verification-delivery-code-investigation.md`
2. `docs/superpowers/plans/2026-10-20-verification-delivery-state.md`
3. `runtime/verification-delivery-design-20261020/probe.py`、`results.json`、`verification.json`

本次目标：消除历史非失败检查造成的永久 stale 阻塞，提供可定位的失败检查反馈，并对能锚定当前可信快照的普通验证阻塞提供一次 finish 纠正。按计划 Task 1→2→3→4 实施。

重要：上轮真实16轮组是产物19/19、完整4/19。本轮尚无修复后成绩，不得声称此包必然解决全部真实失败。原 F1/F2/F3、搜索P2、R1/R2 已关闭，本轮 V1/V2/V3 是新增范围。

实施前保存实际 before 与 hash，HEAD 不代表修改前状态。保留全部现有修改/未跟踪文件及旧日志，新证据用计划指定的唯一目录。先补真实 Runtime RED 测试，再做最小实现，保存中途失败和最终 GREEN；生产代码完成后全量回归、自我审查，并交独立 reviewer。

必须保证：

- 当前有效检查与历史过期记录分开；旧记录不复活，未解决失败不删除。不同成功命令可满足当前覆盖，但不能消除其他失败。
- Engine、反馈和 Runtime 跨任务义务一致；只改 `completion.py` 的 stale 条件不够。新字段保持默认兼容，不能让 Runtime 从历史记录恢复能力。
- 反馈有界且脱敏，提供具体检查 ID/类型/受控目标。不得直接记录任意命令参数、输出、密钥或绝对路径，不新增自动重放工具。
- 只对计划定义的当前可信锚点开放一次纠正；所有原因共用一次计数且不增加轮数。保留外部变化、取消、cleanup_failed、UNKNOWN、authority、扫描、审批、审计与 legacy_unknown 的停止边界。
- 保留真实失败同签名有效复验规则；不通过合并不同测试过滤器/范围、清空旧失败、修改评分来提高完成率。
- 纯确认/只读审查仍不强制无关测试。恢复不提前闭合任务、释放锁/owner、生成终态记忆或启动5秒终止期限。

本轮不改默认 strict/relaxed/fullaccess、默认轮数、quality-v2.2 题库/隐藏verifier/AND评分，不推进 Replan。搜索截断题面修订是后续独立包，不在本次混入。不得读 .env.local/真实凭据/用户数据库，不安装、不调用真实模型、不提交/推送，不 reset/clean。

完成后提供实现报告、actual before/after/delta/hash、逐项 RED/GREEN、全量测试结果与 skips、风险和未验证范围；更新交接与 project.md，写“实现完成，待独立复审”，不自行关闭发现。然后停止，交还 reviewer。
