# Coding session 提示词：交付效率与验证指引修复

将以下内容整段交给独立 coding session：

---

你是 TriCoder 本轮实施者。工作区：
`D:\MaHong\AGENT_WORKSPACE_V2\projects\tricoder-cli`

请实施 `docs/superpowers/plans/2026-10-27-quality-delivery-guidance-fixes.md`，按任务 A→B→C→D 小步开发、验证、自审并交付 reviewer。用户将此提示词交给你即授权本方案范围内的本地源码、测试、题库与文档修改；不需要为每次可逆编辑重复询问。若发现需要改变本文明确保留的安全/评分/预算边界，先完成可独立部分，再报告原因，不自行扩范围。

先读 `../../AGENTS.md`、项目 `AGENTS.md`、`README.md`、`pyproject.toml`、`project.md`、`docs/reviews/2026-10-19-reviewer-handoff.md`，以及上述方案和 `docs/reviews/2026-10-27-quality-v3-repeat3-real-rerun.md`。

本轮基线：DeepSeek deepseek-v4-flash，30题×3，artifact90/90、delivery87/90。失败是 sf-aware-time-order 第2/3次和 rc-search-truncated 第3次，全部16轮耗尽；不是权限拒绝或 cleanup_failed。已有提示、补丁计数诊断、验证反馈都存在，不能把“补充已有同义提示”当作全部修复。

具体实施：

1. **工具反馈**：补丁结构/计数错误给出精确 read/edit 替代建议；create_file 已有文件时不要强迫无意义重读；保持解析、审批、恢复和无副作用边界。native/legacy 一致，不引入自动覆盖或自动补丁修复。
2. **轮次提示**：新增有界纯函数，在首轮及剩余≤3轮时追加宿主预算消息，16轮对应1/14/15/16；含当前轮/后续轮数，最多4条/user_turn。JSON type=execution_budget，但 message.kind 必须复用 protocol_feedback，避免被 ContextManager 删除，并沿用摘要不可提炼为用户要求的规则。追加正常历史以保持 usage 前缀；测试实际 prepare→Provider、协议错误计数不受影响和跨turn摘要隔离。不重写 system prompt、不瞬时注入后删除、不增加模型调用或硬预算，不自动完成。
3. **验证反馈**：在检查成功但覆盖不足时及时解释 pending 与下一步；读文件不补覆盖。旧失败同检查复验、零测试、过期/伪造证据、unknown、清理失败优先级不变。命令仅为条件示例，不拼路径、不自动执行、不把提示作为证据。
4. **题库**：新建 `evals/quality-v3.1/`，suite ID `quality-v3-1`，benchmark `quality-v3.1`。30份模型可见 README 首选根目录 `python -m unittest discover -v`；先离线证明实际发现测试且通过真实Runtime收尾。旧V3全字节保留；新包case业务定义、public/hidden测试、split、任务/后续steps、修改范围、故障、16/16000及四维AND不变。四清单身份按方案命名，仍30/90/22/8。

工作树已有多批未提交/未跟踪内容，HEAD不是本次before。先在 `runtime/quality-delivery-guidance-fixes-20261027/<unique>/` 保存实际安全文件before/hash和Git状态；生成after/delta和测试证据，不覆盖已有日志，不reset/clean/stash或替换整棵树。保全 `runtime/quality-v3-repeat3-real-20261027/` 与 `runtime/evals/20261027t055018.826595z/`，不要改原分数或把独立oracle通过追认为交付成功。

验收必须包括 fake Provider + 真实工具/Runtime 的检查与finish链路，不能仅测文案或手工制造RunResult成功。新增行为先RED再GREEN；验证两种协议、memory off/structured、小预算、取消、消息配对、usage锚、旧失败优先、零测试、跨turn隔离、有界脱敏以及最后一轮没有finish仍失败。按方案运行专项、全部Eval和完整Python测试，记录实际数量/skip/exit。参考当前27项专项通过和旧2060项全量，但不得复制为本轮结果。

不读取真实凭据或 `.env.local`、不读取用户数据库、不安装依赖、不提交/推送、不调用真实模型。本轮只用合成临时数据库和fake Provider；前次90次真实调用授权已履行。不要修改默认strict、切换relaxed/fullaccess、增加轮数、放宽覆盖/完成门禁，或自动推进Replan和历史待办。

最终交付：

- 本方案任务完成矩阵、实际修改清单及before/after差异。
- RED/GREEN、27项旧行为相关回归、新版全库实际测试发现/Runtime链路、变体评分、全部Eval/全量测试、八个dry-run与 `git diff --check` 证据。
- 旧源码非本轮范围、旧V3、hidden业务定义、历史真实结果hash保全证明。
- `docs/reviews/2026-10-27-quality-delivery-guidance-fixes-implementation.md`；追加更新 project.md 和 reviewer handoff，保留旧正文。说明未验证平台、真实模型收益及任何限制。

结束状态写“实现完成，待独立复审”；不要自行宣称真实交付已提升或替reviewer关闭问题，不自动复测真实模型。

---
