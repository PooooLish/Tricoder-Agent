# Coding session 提示词：交付反馈、工具恢复与评测契约

将以下内容完整交给 coding session：

---

你是 TriCoder 项目的 coding session。用户要求你实施独立 reviewer 核查后给出的修复；不要重复真实模型基线运行，也不要把实现记录当作独立验收。

工作区：`D:\MaHong\AGENT_WORKSPACE_V2\projects\tricoder-cli`。

先读：

1. `D:\MaHong\AGENT_WORKSPACE_V2\AGENTS.md` 和项目 `AGENTS.md`。
2. `README.md`、`pyproject.toml`、`project.md` 最新条目、`docs/reviews/2026-10-19-reviewer-handoff.md` 最新条目。
3. `docs/reviews/2026-10-20-delivery-failure-investigation.md`。
4. `docs/superpowers/plans/2026-10-20-agent-delivery-feedback.md`。
5. `docs/superpowers/plans/2026-10-20-eval-delivery-contract.md`。

这两份计划是本次实施范围；按第一份→第二份顺序小步执行。用户已指定 coding session 实施，不需要再询问执行方式。默认单会话执行，不派生子 Agent或新任务。

## 已核查事实，避免走错方向

- 当前真实评测为 DeepSeek deepseek-v4-flash、quality-v2.1、19题各一次：产物15/19、完整2/19、范围/清理各19/19。不要重写历史成绩。
- Eval 已注入自动批准器。将默认权限改成 relaxed/fullaccess 不能解决本次问题；不修改默认权限或安全边界。
- 确认的 F1：旧失败/过期检查还存在时，另一条成功检查可触发 satisfied/verification_accepted，随后 finish 因旧检查拒绝。修反馈一致性，不能清空旧失败或绕过门禁。
- 确认的 F2：即使 task_changes_match_baselines 返回true，任务仅预算耗尽也会提示“未归属变化”。修诊断归因，保留失败任务确认/基线逻辑。
- 确认的 F3：finish 审计不含 outcome，Eval 不含足够验证/结束诊断。tool.ok不等于Agent完成，旧未知值不能猜测。
- create_file 的审批前普通文件已存在分支，无副作用却返回policy_denied/stop_task。只精细恢复这个前置使用错误；审批后竞态、敏感路径/目录/链接/只读/取消等严格处理。
- 前置确认无需修改/检查，只finish即可成功；AND本身不是已确认bug。不要跳过前置turn、忽略needs_input或只用最后一轮评分。
- 同一合成9轮序列：8轮停止、16轮成功，仅证明预算机制。真实模型收益未知，不改默认轮数、不宣称已提升。
- 上一次真实mm-latest-correction的完整命令/outcome未保存，核查重建序列只是机制复现，不能写成精确还原历史原因。

## 必须交付

1. 一致且有界的验证反馈及回归测试。
2. 准确的runtime失败归因，保持原门禁行为。
3. create_file前置文件已存在的安全恢复、补丁/工具使用反馈改进。
4. 可信stop_reason、finish声明与逐轮验证诊断，兼容旧数据且不泄漏正文。
5. 新quality-v2.2公开非交互及前置确认契约；旧quality-v2.1/旧结果完全保留，隐藏verifier不改。
6. 只生成并dry-run 8/16轮冻结实验的脚本和测试，本轮不发起真实模型调用。
7. 实施前后快照、独立日志、精确增量索引、自审与交接更新。

## 实施规则

- 工作树有多批未提交/未跟踪修改，HEAD不是精确before。开始时为实际要改的src/tests/evals/docs文件及README/pyproject保存before与hash、git status；不要复制本地秘密配置、.env.local或用户数据库。
- 新证据目录：`runtime/delivery-fixes-20261020/implementation-<唯一后缀>/`。不得覆盖 reviewer 的 `runtime/delivery-diagnosis-20261020/`、旧真实run及既有日志。
- 参考探针：probe.py、followup.py、boundaries.py、attempt3/、verification.json。若重跑，复制脚本到你的新目录并修正ROOT/OUT，不在reviewer目录直接重跑撞名。
- 用真实SessionRuntime/工具和合成Provider做核心验收；合成测试数据库只能新建在隔离runtime中。普通mock验证不足以替代资源/门禁/多轮路径。
- 每个任务先保存RED，再最小实施、GREEN；已有反例变成测试，成功/负例都要验证。不要为全绿删测试、改隐藏断言、改旧得分或放宽全局策略。
- 默认不安装依赖、不读取真实凭据、不调用模型、不提交/推送，不做Replan框架或无关重构。
- 第一包完成后固定源码快照，再做第二包；两包结果独立可审查。若发现新阻塞，记录具体代码与复现，不以扩大权限或放宽评分规避。

## 交付给 reviewer 的报告

用中文报告：

- 每项需求实际修改的文件与位置、解决的现象、未解决/不确定部分。
- before快照、增量diff、after hash和完整文件索引（包括未跟踪新文件）。
- 实际执行的命令、测试数、耗时、失败/跳过及退出码；不要把reviewer旧78项测试计入你的执行。
- 13个核查场景的实现后结果，新增边界测试、原搜索超时P2至少5.2秒延迟回归和清理失败停止。
- 旧真实结果6/1/1/2 passed兼容性、旧题库/隐藏verifier保留校验，新版本19quality/11contract和两组dry-run。
- 明确“实现完成，待独立复审”；新模型质量未经真实重跑。更新project.md与reviewer-handoff，保留原发现和历史记录。

完成后停止，等待用户将结果交回 reviewer；不要自行关闭独立审查问题、提高默认权限或启动额外实验。

---
