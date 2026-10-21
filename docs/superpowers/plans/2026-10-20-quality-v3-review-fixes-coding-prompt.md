# 交给 coding session：修复 Quality V3 复审发现

你是TriCoder本轮coding实施会话，工作区：
`D:\MaHong\AGENT_WORKSPACE_V2\projects\tricoder-cli`。
请使用中文、单会话执行，不派生子Agent，不提交/推送。

先读工作区`../../AGENTS.md`、项目`AGENTS.md`、README.md、pyproject.toml、project.md和reviewer交接最新条目，再完整阅读：

1. `docs/reviews/2026-10-20-quality-v3-rereview.md`
2. `docs/superpowers/plans/2026-10-20-quality-v3-review-fixes.md`
3. `runtime/quality-v3-expansion-20261020/review-20261020/probe_scoring.py`
4. 同目录`probe-results.json`、`final-verification.json`及相关`probes/*/reviewer-result.json`

本次只修Q3-R1/R2/R3三个P2，按计划Task1→2→3→4，用`superpowers:executing-plans`实施。不是再次扩题、不是修Agent，也不是立即真实跑分。

已确认的问题：

- **Q3-R1**：时间排序hidden只比较id，丢弃timestamp/其他字段的错误实现仍passed。改为验证完整记录及嵌套内容，保留稳定顺序和输入不变；允许等值副本，不强迫同一对象。
- **Q3-R2**：CLI只测缺参数/非法整数，忽略第三参数仍passed。直接main和实际CLI都要检查三/四参数、返回码或退出码2、stdout空、stderr非空。
- **Q3-R3**：缓存和包迁移hidden使用assert_called_once_with，误拒合法的多次调用。应验证目标接口实际使用和正确业务结果，接受多次、slots、关键字与合法预处理；绕过get/新导出的负例仍必须拒绝。

用户已授权这个修复包，请直接完成白名单修改，不再次询问是否实施或是否创建计划。若必须改生产框架、题面或其他未授权模块，先提供明确反例及最小变更理由，交回reviewer；本包按现有能力应无需这些改动。

实施前保存actual before与hash，HEAD不是本次精确基线。新证据目录：
`runtime/quality-v3-review-fixes-20261020/implementation-<唯一后缀>/`。
保留全部现有修改和未跟踪文件，所有RED/GREEN、逐项收据、完整日志均独立保存。

允许修改计划列出的四份hidden verifier，新增`tests/test_eval_quality_v3_review_fixes.py`，更新必要的V3方法说明/矩阵以及实施报告和交接。**不改src、case.toml、workspace、suite/实验配置、题数/split/预算、公开业务要求、默认权限或AND。** 不通过在题面补“只调用一次”来合理化评分器。

务必注意：

1. 原reviewer探针断言旧错误行为；不能覆盖它或要求它修复后仍exit0。在新测试/新脚本中重放同样8例，正确期望是2个错误failed、6个正确/合法实现passed。
2. 每项新增测试先使用正确期望获得有效RED，再修hidden至GREEN；保持原125项变体不删、不替换。不要只重跑旧309测试宣称修复。
3. 用真实隔离复制、公开/隐藏子进程和评分入口验证；导入错误、超时、基础设施error不能充当业务错误被拒的证据。
4. 新增“双入口不同步”的CLI负例、嵌套字段丢失负例、完整深拷贝合法解及目标调用绕过负例，按计划保证既能拒错又能容纳等价实现。
5. `TRICODER_V3_EVIDENCE`每次指定新目录，避免独占日志冲突；保留原审查和实施目录不变。
6. 完成新专项、全部V3、全部Eval、全量unittest及四个dry-run，记录真实测试数、失败/skip、耗时，不复制旧成绩。

本coding包不读`.env.local`、真实凭据或用户数据库，不安装、不运行真实模型、不reset/clean、不提交推送。用户真实模型授权以独立复审通过为前提，本轮尚未满足，不可提前执行。

最终交付：

- 四份verifier修复及新增正反回归；
- actual before/after/delta/hash、历史保全、RED/GREEN、原125项及新增变体收据、重放8例的新期望结果；
- 新专项/全部Eval/全量与30/90/22/8 dry-run日志；
- `docs/reviews/2026-10-20-quality-v3-review-fixes-implementation.md`；
- 更新project.md和`docs/reviews/2026-10-19-reviewer-handoff.md`，状态写“实现完成，待独立复审”。

保留原复审暂不通过结论，不自行关闭Q3-R1/R2/R3，不声称真实成功率提升。完成后停止，交回独立reviewer；通过后由reviewer继续已授权的30题DeepSeek flash真实复测。
