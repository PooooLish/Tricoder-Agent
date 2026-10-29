# 给 coding session：Eval量化指标实施提示词

你是 TriCoder coding 实施会话，请按以下文档直接完成离线修改，完成后交独立 reviewer，不在本会话运行真实模型。

工作区：`D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli`。

先读：
1. 工作区 `../../AGENTS.md` 与项目 `AGENTS.md`。
2. README.md、pyproject.toml、project.md、`docs/reviews/2026-10-19-reviewer-handoff.md`。
3. **实施与验收契约：`docs/superpowers/plans/2026-10-27-eval-quantitative-metrics.md`**。第1—7节是spec，第8—10节是任务/验收。
4. `docs/reviews/2026-10-21-quality-v3-review-fixes-rereview.md`、`docs/reviews/2026-10-21-quality-v3-real-baseline.md`，理解历史结论，不重解释或覆盖旧成绩。

目标：在现有Eval框架增加产物/完整交付pass@k、完整交付pass^k、逐题重复成功分布、token效率和阶段耗时；增加`eval-analyze`离线入口以及受严格配对约束的新比较区块。默认k=1/2/3。

执行要求：

- 用户已选择由本coding session实施本包，按Task1→5单会话小步TDD；不重新要求用户选择执行方式，不派生实施子会话，不提交。
- 开始前保存actual before、全文件索引/hash和脏工作树状态。HEAD不是本包基线，保留全部未提交/未跟踪内容。
- 核心规则以文档为准：一次整题执行才算sample；按condition/case/repetition隔离；contract不计quality；不完整计划、缺plan、未知结果和n<k不可通过删除样本得到分数。
- 公式先做手算正反例，确保n=3,c=2得到@1=2/3、@2/@3=1、^2=1/3、^3=0。两个条件同题不可合并；原stable_cases不是pass^k。
- Token保持main/summary分离，cache是input子集；部分字段未知不能变零，失败消耗计入每次成功摊销。标provider_reported/billing_completeness unknown，不改cost_per_success为token或虚构金额。
- 新timings只在Eval层观测，保留旧duration与清理/取消/异常语义；不得改变owner、终止期限、权限、默认预算或finish门禁。
- 新schema字段默认兼容；新/旧报告离线分析同一公式，缺历史split为unknown，缺阶段时间为null。不要读当前题库补历史信息。compare继续严格指纹与计划配对，不信任输入预计算分数。
- 实现新CLI与报告后，只读分析2026-10-21原生result.json+plan.json。必须得到产物29/30、完整27/30、main token总量1,414,748；@2/@3不可用，不能把原30次当90次。
- 本包不修CSV、交付验证遗留问题、Replan，不改evals里的任何fixture、题面、verifier、清单或split，不自动扩范围。
- 不读取.env.local/真实凭据/用户数据库，不安装依赖，不提交/推送，不执行真实模型。以前的30题真实授权已消费完；90次真实重复实验另待用户授权。

验收：新专项、全部Eval、全量unittest、四CLI dry-run(30/90/22/8)、合成90行统计、旧报告离线分析、严格比较兼容、原搜索超时恢复/取消/清理/owner回归。测试总数按实际报告；平台skip单列，不算通过。每轮使用新的日志及TRICODER_V3_EVIDENCE目录，不覆盖历史。

交付：

- 最终代码与有意义的正反例测试。
- `docs/eval-quantitative-metrics.md`：指标口径、字段与兼容、使用示例、不可用原因和计量限制。
- `docs/reviews/2026-10-27-eval-quantitative-metrics-implementation.md`：实际增量、RED/GREEN、最终命令/计数/exit/耗时、逐项要求映射、未验证范围。
- `runtime/eval-metrics-20261027/implementation-<unique>/`：实际before/after、hash/delta、新日志、历史保全、离线派生分析路径。
- 更新project.md与reviewer交接，保留旧正文，标明“实现完成，待独立复审”；随后停止。不要用实现自测代替独立验收，不自动付费跑分。
