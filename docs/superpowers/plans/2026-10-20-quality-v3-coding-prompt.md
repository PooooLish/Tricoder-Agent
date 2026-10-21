# 交给 coding session：Quality V3 扩充与优化

你是 TriCoder 本轮 coding 实施会话。工作区：`D:\MaHong\AGENT_WORKSPACE_V2\projects\tricoder-cli`。请用中文，单会话按计划执行，不派生子 Agent。

先读工作区`../../AGENTS.md`、项目`AGENTS.md`、README.md、pyproject.toml、project.md和`docs/reviews/2026-10-19-reviewer-handoff.md`最新条目，再完整阅读：

1. `docs/superpowers/plans/2026-10-20-quality-v3-expansion.md`——第1—6节为需求设计，第7—9节为实施和验收。
2. `docs/reviews/2026-10-20-verification-delivery-real-full-rerun.md`——最近真实证据。
3. `evals/quality-v2.2/README.md`、suite/实验清单及19道quality定义；`src/tricoder/evals/loader.py`、`faults.py`、workspace/runner/experiment和现有题库测试。

用户已经明确：**真实模型质量题达到30题**，不是新增30题，也不是将contract凑入30。交付新`evals/quality-v3/`：优化原19题、增加计划中的11题，全部execution_kind=quality；原11道contract继续单独保留在旧套件。旧版本和历史成绩不得覆盖。

直接按计划Task1→2→3→4实施；使用`superpowers:executing-plans`。本提示词是该修改包的执行授权，不需要再次询问数量、实施方式或是否创建计划。若出现计划无法覆盖的生产框架变更，提供具体阻塞和最小方案，交回reviewer，不自行扩大范围。

先保存actual before与SHA256，HEAD不是当前修改前基线。工作树已有多批未提交/未跟踪内容，保留全部现有修改。新证据放到`runtime/quality-v3-expansion-20261020/implementation-<唯一后缀>/`，保留每次失败和最终成功日志。

必须落实：

- 新suite为30quality、0contract；22dev/8holdout。原19全部dev；新增前3题dev，后8题holdout。已用于诊断的旧holdout不能继续宣传为未接触题。
- 每user_turn固定16轮、16000上下文上限；四份实验清单的trial数30/90/22/8。只改新benchmark预算，不改产品默认轮数或旧8/16实验脚本。
- 新题按计划规定的真实接口与业务契约实现fixture/隐藏verifier，不把正确答案放进workspace。标准库即可，不安装依赖、不接外部服务。
- 每题公开需求、公开测试和隐藏测试一致；加入可发现的公开测试及tests/__init__.py。memory首轮可见内容不能泄露后轮修正。
- 截断故障可发生在read/search/glob，不能要求Agent在业务代码中模拟截断。旧Provider暂时故障只是计数；v3保留该题ID用于追溯，但改为纯increment题、single_file、faults=["none"]、不计恢复能力。旧机制不改。
- 每题至少实际验收初始错误、正确实现、两个不同错误变体，共120项基础变体；CSV、稳定去重、旧API兼容至少再验证一种不同合法实现。逐项记录，不只看汇总。
- 用真实隔离复制、隐藏验证与评分入口；增加只改tests、越界/嵌套路径、正确但不finish、前轮失败后轮成功等负例。CLI检查真实子进程，包迁移检查实际导入链。
- `MAX_EVAL_CASES=32`无需放宽。保持src、默认权限、命令策略、finish/验证/清理/取消/资源所有权及AND规则；不能为通过新题修改Agent。
- 所有旧suite、旧测试、真实结果和审查日志保持，actual before/after/delta可核对。新旧benchmark不同，不能把分数变化归因为Agent改进。

本轮**不运行真实模型**，不读取`.env.local`、真实密钥或用户数据库；不安装、不提交/推送、不reset/clean，不推进Replan。此前真实模型授权属于之前的评测任务，本实施包只做离线建设与验证。

最终跑新题库专项、全部eval测试、四份CLI dry-run和完整unittest discovery。新测试用例必须先有有效RED再有GREEN；已经覆盖的条件如实记录，不虚构失败证据。记录实际测试总数、成功/失败/skip、耗时及未验证范围。

交付：

1. `evals/quality-v3/`及README、case-matrix.json、migration.md、来源hash、四份实验清单；
2. 新题库测试与测试侧正确/错误变体，至少120项逐题验收结果；
3. actual before/after/delta、历史保全、RED/GREEN、dry-run、全量日志和证据hash索引；
4. `docs/reviews/2026-10-20-quality-v3-implementation.md`，更新README、project.md及reviewer交接。

状态写“实现完成，待独立复审”，不宣称真实模型成功率提升，不自行关闭新发现。完成后停止，交回独立reviewer。
