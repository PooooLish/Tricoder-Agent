# Agent 交付反馈与工具恢复 Implementation Plan

> 实施状态（2026-10-20）：实现完成，待独立复审。勾选是coding记录，不代替独立验收；证据和限制见 `../../reviews/2026-10-20-delivery-fixes-implementation.md`。

> **For agentic workers:** 使用 superpowers:executing-plans 按任务执行。用户将本计划交付给独立 coding session；本 reviewer 不实施。不要派生子 Agent、创建新会话或自动提交。

**Goal:** 消除验证“已满足”与 finish 拒绝的矛盾、错误的工作区归因，以及可安全恢复的文件工具误用停止。

**Architecture:** 由现有宿主验证状态生成一致反馈，不改变完成门禁或检查替代规则；只调整已确认无副作用的工具前置错误。保留默认权限、资源所有权和终止语义。

**Tech Stack:** Python 3.11+、现有 unittest、真实 SessionRuntime/工具、合成 Provider；不新增依赖。

**Spec:** `docs/reviews/2026-10-20-delivery-failure-investigation.md`，尤其 F1–F4；先读完整核查报告及其 13 个场景。

## Global Constraints

- 读取 workspace/project AGENTS.md、README.md、pyproject.toml；全部工作在本项目内。
- 脏工作树含多批未提交内容；HEAD 非精确基线。只读保存实际 before 文件、hash、git status，禁止 reset/clean/覆盖旧记录。
- 本计划不改 strict/relaxed/fullaccess 默认值、不放宽命令/敏感路径、不改评分通过条件、不实施 Replan、不自动推进历史待办。
- 不读 .env.local、真实凭据或用户数据库；不安装依赖、不调用真实模型、不提交/推送。合成测试使用新建的隔离 runtime 目录与数据库。
- 实施证据用 `runtime/delivery-fixes-20261020/implementation-<唯一后缀>/`；保留 RED/GREEN 和中途失败日志。
- 旧搜索 timeout P2、取消/cleanup_failed/UNKNOWN 和现有审批门禁必须保持。

## Review Focus

1. 当前检查成功但另一个失败/过期检查仍存在：不能提示 satisfied（Task 1）。
2. 普通脚本、0 tests、过滤检查、跨快照或跨 authority 证据不能解除义务（Task 1）。
3. 任务失败但文件可归属：不误报外部修改，也不自动接受失败基线（Task 2）。
4. create_file 已存在文件与审批后竞态、目录、符号链接、敏感路径必须分开（Task 3）。
5. 补丁诊断不得回显路径/源码/任意异常，解析失败仍无写入（Task 4）。

## Task 1：对齐验证反馈与完成事实（最高优先）

**Files:** 修改 `src/tricoder/engine/verification_feedback.py`；按需从 `engine/completion.py` 提取无副作用共享判定，但不改变 assess_finish 的拒绝条件。新增 `tests/test_verification_feedback_consistency.py`；回归 `tests/test_task_validation.py`、`test_finish_coverage.py`、`test_finish_verification_recovery.py`。

**Interfaces:** 保持 `checkpoint(state)`、`build_batch_feedback(state, before) -> str | None`、`finish_recovery_feedback(...)` 调用签名。输出增加有界 `validation_status`、`unresolved_check_count`，以及固定 reason_code `unresolved_checks` / `stale_checks` / `zero_tests_reported`；旧键保留。路径继续沿用 controlled_pending_paths 的敏感路径过滤、20 项/2000 字符上限。

- [x] 保存实际 before；将核查 `reconstructed_completed` 的事件序列改写为回归测试：最后成功命令之后 `build_batch_feedback` 不得返回 satisfied/verification_accepted，且必须报告尚有旧失败/过期记录。在当前代码上断言失败，保留 RED。
- [x] 加入正例：重跑先前失败的同一命令后 feedback satisfied、finish 成功；定向 tests + 源码 compileall 的联合覆盖仍成功。
- [x] 实现无副作用状态检查：本轮有验证义务时，只有 authority/快照有效、修改覆盖完整、无 failed_snapshot、无 unresolved、验证报告非 stale/failed，才能宣布 satisfied。先检查未知/未解决/过期等阻塞事实，再判断最近检查是否通过。不得调用会清空 evidence 或改变 state 的 assess_finish 来生成反馈。
- [x] 区分“路径覆盖齐全但旧失败未解决”与“覆盖不足”：前者可 pending_paths=[]，但 obligation 必须 pending，并明确 unresolved_check_count 和 reason，不能伪造缺失路径。
- [x] 无本轮修改义务的纯审查/确认仍允许正常完成；不能仅因审查发现失败就建立修改义务。保持 no-op 反馈抑制，避免每次读取重复灌入反馈。
- [x] 补负例：zero tests、scope_filtered、失败后仅 compileall、同路径改写后旧快照、伪造 authority、cancel/cleanup_failed/UNKNOWN；确保不能因新反馈恢复完成能力。对已有测试覆盖的安全路径复用既有测试，不抄实现写同义断言。
- [x] 运行新测试与上述三组；全部 GREEN，真实工具回放既能重现阻塞、又能在补对检查后成功。不要修改 TaskValidationTracker 的同命令替代规则来“修复”提示。

## Task 2：收尾原因准确化

**Files:** 修改 `src/tricoder/session/runtime.py:_finalize_workspace_task`；新增 `tests/test_runtime_delivery_diagnostics.py`，回归 `test_session_runtime.py`、`test_workspace_gate.py`。

**Interfaces:** 保持 RunResult.ok、verification、基线推进/确认逻辑不变。仅将诊断文案按已计算的 attributable/tainted/result 状态拆分：有归属但未交付；无法归属；未知影响/清理问题沿用其根因。

- [x] RED：真实 Runtime 执行 edit→compileall，max_rounds=2，在下一轮 finish 前停止。捕获 task_changes_match_baselines 返回 true，断言摘要保留预算原因且不包含“未归属”。核查已有 boundaries.py 可直接参考。
- [x] 实现明确文案，例如“任务尚未完整交付；已记录本轮修改，下次任务前需确认工作区”；只在 attributable=false 等真实归属失败分支称“未归属”。不要因为可归属而推进失败任务的 baseline。
- [x] 验证正负对照：真实外部变化仍提示无法归属并需确认；正常成功任务无需多余确认；预算/取消/模型 incomplete 的原始原因不被通用文案覆盖；pending/证据撤销行为与修复前一致。
- [x] 运行新测试、workspace gate 与 runtime 聚焦测试；保存结果。

## Task 3：create_file 前置错误精细恢复

**Files:** 修改 `src/tricoder/tools/write.py:CreateFileTool`、`src/tricoder/execution_state.py` 的 detail_code 白名单、必要工具描述；新增 `tests/test_create_file_recovery.py`，回归 `test_tools.py`、`test_tool_errors.py`、`test_directory_tools.py`。

**Interfaces:** 新增固定 `file_already_exists` detail_code。仅已通过 workspace/path/read_only 检查、审批前确认的正常普通文件已存在分支，返回 `INVALID_ARGUMENT / REPLAN / EffectState.NONE`；原文件字节不变、无审批、无新目录或文件副作用。固定提示引导先 read_file 再 edit_file/apply_patch。

- [x] RED：对已存在普通文件 create_file，当前 policy_denied/stop_task；期望新分类和无效果。真实 Agent 下一轮改用 edit_file、可信检查、finish 应完成，无需新建任务或重置状态。
- [x] 在最小前置分支实施，不能把 PolicyError 全局转换为 recoverable。目标是目录/链接或安全状态不确定时保留严格拒绝；read_only、越界、敏感路径、审批拒绝、审批后文件出现/变化、父目录身份变化、取消和 cleanup_failed 均保持原处理。
- [x] 保留原子发布及最后一刻 no-overwrite；模型错误不意味着允许覆盖。不要新增“fullaccess 下覆盖已有文件”例外。
- [x] 增加拒绝原因的固定 detail_code 仅在能明确辨认的分支；日志不得保存被拒绝的原始路径/内容来定位历史请求。历史两次 policy_denied 的精确原因仍 unknown，不回写旧记录。
- [x] GREEN 后在真实 Runtime 回放恢复路径及上述竞态负例，核对工具/任务两层效果与资源归属。

## Task 4：减少补丁和测试调用的可避免错误

**Files:** 修改 `src/tricoder/protocols.py`、`src/tricoder/tools/write.py` 的描述、按需 `src/tricoder/patches.py:PatchError`；测试 `tests/test_patch_feedback.py`、`test_patches.py`、`test_protocols.py`。

**Interfaces:** 不变更 unified diff 语法、宽松修补行数、批准/原子性。PatchError 可追加 `diagnostics: tuple[tuple[str, int], ...] = ()`，键仅 `hunk_index`、`declared_old`、`declared_new`、`observed_old`、`observed_new`，值为有界非负整数。只在 parser 已确定计算值时填入，未确定时省略；ToolError.detail_code 保持现有分类，数字可用于固定工具反馈，不能混入异常正文或源码。

- [x] 测试 count mismatch 的确定数字反馈、无法确定时不编造；无路径/源码泄漏；多文件原子预检、审批前零副作用不变。现有有效补丁全部仍可解析。
- [x] 描述小范围已读文件优先 edit_file，复杂/多文件修改用 apply_patch；明确 create_file 仅新文件、缺父目录使用 create_parents 或 create_directory。
- [x] 在通用提示中增加简短测试指导：零测试不等于验证通过；tests 目录定向发现不自动证明根源码覆盖；按宿主反馈补相关检查；检查之后再改文件需要重新检查。不得引导用语法检查替代必要业务测试或强制所有任务添加测试。
- [x] 不注入题目 ID、隐藏答案或这 19 题特例。不扩张命令白名单，不加入 python -c/pip/任意 Shell。
- [x] 跑 parser、工具反馈、协议测试，保存失败计数变化只作后续真实实验指标，不保证修改后模型一定少犯错。

## 集成验收与交接

- [x] 新增测试按 RED→实现→GREEN 逐项执行；上述真实 Runtime 场景同时覆盖 legacy/native 协议适用路径，不能只 mock 最终 RunResult。
- [x] 核查报告中的 13 个场景按修复后的合理期望独立重放：F1 反馈不再虚报；F2 原因准确；F4 新文件仍安全创建、已存在普通文件可恢复。
- [x] 回归原搜索 P2：`python -B -m unittest discover -s tests -p test_search_timeout_recovery.py -v`，必须覆盖同任务 >=5.2 秒延迟与清理失败停止；执行相关 cancellation/工具错误测试。
- [x] `git diff --check`，对新未跟踪文件另查空白；输出精确 before/after hash、文件清单、测试命令/退出码/跳过原因。不将 reviewer 的 78 tests 写成 coding 自己运行的结果。
- [x] 更新 project.md 和 reviewer-handoff 为“实现完成，待独立复审”，保留本次发现原文，不能代 reviewer 关闭 F1/F2。后续按第二份计划补诊断与新评测契约。

**完成边界：** 生产行为按以上最小范围实现，未改权限默认、评分主指标、轮数默认或 Replan；没有新增真实调用。提交用户审查，不自动 merge/commit/push。
