# 验证状态与交付纠正修复计划

> 2026-10-20 coding实施状态：**实现完成，待独立复审**。Task1/2/3已按RED→最小实现→GREEN完成，Task4最终focused139项通过；全量1838项，其中1823通过、15跳过、0 failures/errors；测试421.653秒，墙钟426.843秒，exit0。9组真实Runtime探针符合预期，保护/增量/skip明细见实施报告与独占证据目录。下方原“待实施”和原始检查表保留为计划历史，不代表独立验收；V1/V2/V3由reviewer复核，本会话停止。

实施报告：`docs/reviews/2026-10-20-verification-delivery-implementation.md`。

> 状态：待实施。由用户交 coding session 单会话执行；reviewer 不实施。使用 executing-plans 按本计划小步执行，不派生 Agent、不提交或推送。

**Goal:** 当前版本具备可信覆盖且没有未解决失败时，不再被旧的非失败检查永久阻止；真实失败仍可被准确定位、复验，并在安全条件下有一次纠正过早 finish 的机会。

**Architecture:** 保留命令事实与历史；把“旧记录过期”与“当前完成阻塞”分开。复用现有 tracker、覆盖计算、authority、最终快照与一次恢复计数器。同步 Engine/反馈/Runtime 的状态含义，不增加自动执行命令或通过能力。

**Tech Stack:** Python 3.11+、现有 unittest、真实 SessionRuntime + 合成 Provider，无新增依赖。

**Spec:** `docs/reviews/2026-10-20-verification-delivery-code-investigation.md` 及其9组离线证据；上轮真实结果见 `docs/reviews/2026-10-20-delivery-real-full-rerun.md`。

## 范围与约束

- 先读 workspace/project AGENTS、README、pyproject、最新交接。保留所有脏工作树及未跟踪内容，HEAD 不是 before。
- 实施前保存实际涉及文件的 before、hash、git status，新证据放 `runtime/verification-delivery-fix-20261020/implementation-<唯一后缀>/`；不覆盖旧日志或旧题库，不复制配置凭据。
- 不读取 .env.local/真实凭据/用户数据库，不安装、不调用真实模型、不提交/推送。不改默认权限、默认预算、AND 评分、隐藏验收、Replan、命令策略与审批。
- 不全局规范化或合并 argv，不把 `-k`、`-p`、不同 cwd/目标当同一次测试；本包保留真实失败的同签名有效复验规则。
- 不因隐藏 verifier 通过就认定 Agent 完成；不把 syntax 解释为业务正确性证明。保持当前 tests/syntax/static 的已认可覆盖契约。

## Task 1：历史过期不再一票否决当前验证

**Files:** `src/tricoder/engine/validation.py`、`core/validation.py`、`engine/completion.py`、`engine/verification_feedback.py`、`session/runtime.py`；相应测试 `test_task_validation.py`、`test_finish_coverage.py`、`test_verification_feedback_consistency.py`、`test_verification_obligation.py`、`test_runtime_coverage.py`。

### 设计要求

1. 保留 `_records` 历史、`_stale` 失效事实及 `_unresolved` 真实失败；新版本成功检查不能把旧记录“变新”。`current_records(snapshot_id)` 仍排除 stale、跨任务和跨快照。
2. `report().status` 用于当前检查状态汇总：**存在当前认可的有效检查时，仅有历史 stale 不再强制 status=stale**；真实 unresolved 仍必须报告 failed。没有当前认可检查时，保留 stale/未验证等保守结论。不能用普通脚本、information、零测试、scope_filtered、不完整或不稳定执行充当“当前认可检查”。report 的 observed 仍只是检查事实，不表示覆盖已满足。
3. 建议在 `TaskValidationReport` 末尾追加默认空的 `stale_check_ids: tuple[str, ...]`，暴露失效记录身份而不是快照或 authority。保留历史限制文案；新报告可分别显示“当前检查已观察，另有历史检查过期”。审计、Console/TUI 与旧记录读取保持兼容。也可采用等价的显式活动检查视图，但不能只删除 stale 判断而让 Runtime 复活旧证据。
4. 完成门禁仍要求：当前 authority/完整快照有效、认可检查联合覆盖所有应验证修改、无 failed_snapshot、无 unresolved、无取消/cleanup_failed/UNKNOWN。对**有本轮修改验证义务**的任务显式检查 unresolved，不能依赖旧的 stale 总开关间接保护。纯确认或只读审查报告检查失败，不凭空建立修改义务。
5. 反馈与 finish 共用相同的无副作用“当前证据/覆盖/失败”计算，避免一边 satisfied、一边拒绝。反馈使用已知宿主快照，finish 仍必须重新扫描；反馈不能替代最终扫描，也不能为了生成消息清空 evidence 或改状态。
6. 同步 Runtime 的 `_merge_verification_obligation`（当前约407行）：不再以“历史存在 stale”排除所有当前检查；逐条校验 `owns_check`、当前 digest、stable、effective success、认可类型、目标范围，排除明确 stale 的 check ID。未解除失败不得清除修改义务。不要仅删除 `status != stale` 后直接遍历 records；记录数截断、同签名最新记录、代码回到旧内容时均不能复活旧通过。
7. 保留 failed_snapshot 的安全含义。文件版本变化只能撤销旧版本失败快照对新版本的直接绑定，不能清空 `_unresolved`。失败检查只有同签名的完整、稳定、有效成功才能解除；零测试和 filtered 成功不能解除。

### RED / GREEN

- [ ] 把 reviewer 的三组障碍转为期望成功的真实 Runtime 测试，先保留当前 RED：旧成功不同命令替代、成功普通脚本历史、旧零测试后真实 tests+syntax 联合覆盖。
- [ ] 保留反例：没有新检查、仅信息/普通脚本/零测试/过滤测试、覆盖无关路径、证据跨版本/任务/authority、不完整扫描、检查后外部变化、真实测试失败后只有 compileall。
- [ ] 真实失败同签名复验成功恢复；不同签名的成功不删除失败；32条淘汰不遗忘失败；失效记录回到相同摘要不自动复活。
- [ ] 更新 `test_zero_tests_and_targeted_tests_do_not_claim_root_coverage` 的最终期望：零测试本身仍无效，但后来 tests 对测试目录与 compileall 对源码的联合覆盖可完成。保留该测试对零测试和范围的断言，不能整个删除。
- [ ] 真正运行 SessionRuntime，断言 result、反馈、会话待验证路径、下一任务前工作区确认需求一致；不能只测 CodingAgent.ok。

## Task 2：明确告诉模型哪项检查仍未解决

**Files:** `engine/verification_feedback.py`、必要的 tracker 只读接口、`protocols.py`，测试 `test_verification_feedback_consistency.py` 和新增有界反馈测试。

- [ ] 增加有界 `blocking_checks`：宿主 check_id、kind、固定 reason（例如 failed/incomplete）、受控相对目标；区分 `historical_stale_count`、`unresolved_check_count` 和 pending_paths。历史过期本身不应要求逐条重跑。
- [ ] 对能完整安全渲染的已允许检查，可给出重跑提示；仅白名单命令结构、已批准的参数形式和受控目标，任一部分不安全就不提供命令正文。不要拼接任意 argv、stdout/stderr、异常、绝对路径或环境变量。check_id 只是定位信息，不赋予自动重放权限。
- [ ] 保留2000字符/20路径边界；阻塞条目另设小上限（建议5），附总数和 omitted 数。老失败超过32条窗口后不能消失，也不能编造丢失的参数；保留 ID 并说明需查看原工具回合。
- [ ] `pending_paths=[]` 但 unresolved>0 时，明确“覆盖已齐全，仍须修复并复验列出的失败检查”；不建议无意义地再运行 compileall。
- [ ] 同步协议提示：先处理宿主阻塞检查，再补路径覆盖，最后 finish；无法继续用 incomplete。不能把提示词当成安全门禁的替代。
- [ ] native/legacy 两协议、有敏感目标/极长参数/32条淘汰的反馈均验证；断言反馈只读不改变状态。

## Task 3：已知验证阻塞允许一次有界纠正

**Files:** `engine/completion.py:FinishAssessment/assess_finish`、`engine/loop.py`、`engine/verification_feedback.py:finish_recovery_feedback`、`protocols.py`，测试 `test_finish_verification_recovery.py`、`test_delivery_stop_diagnostics.py`。

### 最小安全扩展

现有 `missing_relevant_verification` 纠正保留。新增固定 reason 如 `unresolved_verification`，但不能笼统写成 `status in {failed, stale}` 即恢复。

- 必须是 completed 声明、有本轮验证义务、确实存在未解决检查，且预算未尽、该任务的一次纠正尚未使用。
- 当前扫描完整、scope/检查记录属于当前任务，且能以**当前完整快照**锚定阻塞：例如当前稳定失败检查的 snapshot 与 finish 快照一致，或已有当前 owned evidence/有效检查但尚有旧失败未解除。失效的 passed evidence、跨 authority、缺少可核对事实一律不恢复。
- 不清除 failed_snapshot/unresolved，不接受 completed；将具体阻塞反馈交给模型，仍由后续正常工具调用和真实复验解除。
- 没有当前可信锚点、仅剩 stale 的场景，本包仍保守终止。不要为了扩大恢复范围另外引入“假设所有变化均由 Agent 导致”的状态。本包不保证覆盖所有真实失败。
- 取消、cleanup_failed、UNKNOWN、legacy_unknown、扫描不完整、外部变化、审计失败、明确 incomplete、审批拒绝和已终止任务不进入该恢复。当前失败检查之后发生外部变更的负例必须保留。
- 所有恢复原因共享现有 `finish_verification_recoveries < 1`；不能每个 reason 各给一次，也不能重置轮数。第二次过早 finish 直接失败，最终可通过正常检查后成功 finish。
- 复用现有任务、锁、cleanup owner、历史和记忆边界。纠正不调用 finalizer、不发布终态、不启动共享终止期限；已有期限不延长/清除，不启动新任务。审计记录 reason/count/limit，但不写命令正文。

### 回归

- [ ] RED：真实 Runtime“创建失败测试→失败→finish→修复→同检查成功→覆盖补齐→finish”，第一次 finish 不结束，最终成功；本次证据 `failed_finish_has_no_correction` 是现状对照。
- [ ] 当前失败不修复、仅无关命令、只做 compileall、零测试、第二次过早 finish 均不能完成。
- [ ] 修改既有 `test_failed_verification_is_not_a_missing_evidence_recovery`：它不应归到 missing evidence，但可在新窄条件下进入 unresolved recovery；新增未满足新条件仍终止的反例。`test_stale_check_before_finish_never_enters_recovery` 注入的是外部写入，必须保持拒绝。
- [ ] 重跑取消/清理/审计/最后一轮/记忆闭合/工作区锁/批次剩余 skipped/native+legacy 测试。纠正期间等待至少5.2秒后检查成功；已有终止期限与精确 owner 不因恢复发生变化。

## Task 4：验证、交接与评测顺序

- [ ] 逐项运行 RED/GREEN 并保存命令、退出码、测试数量、skip 原因；可从以下 focused 开始，再按实际触及文件扩充：

```powershell
$env:PYTHONPATH='src;tests'
.venv/Scripts/python.exe -X utf8 -B -m unittest test_task_validation test_finish_coverage test_finish_verification_recovery test_verification_feedback_consistency test_verification_obligation test_runtime_coverage test_delivery_stop_diagnostics test_search_timeout_recovery -v
```

- [ ] 全部完成后运行全量 `.venv/Scripts/python.exe -X utf8 -B -m unittest discover -s tests -v`；不把平台 skips 算通过。检查实际 before→after 增量、tracked/untracked 空白问题；保护旧证据和质量题 hash。
- [ ] 报告每项验收是否完成、实际改动文件和理由、遗留范围；更新 reviewer handoff 和 project.md，标“实现完成，待独立复审”，不自行关闭 V1/V2。
- [ ] coding 到此停止并交独立 reviewer。真实模型重跑由后续 reviewer 在本轮授权范围内另行安排；本 coding 提示词不授权调用真实模型。

评测顺序必须分开：先保持 quality-v2.2 题面/verifier、模型配置/预算相同，对照 Agent 修复；再独立考虑新 benchmark 修订。`rc-search-truncated` 题面建议明确：“首次截断由宿主 search_text 工具注入，不属于 find_prefix 业务要求；函数每次调用都应返回全部大小写不敏感的匹配项。”同时更新 task/step，保留旧版与旧成绩，不改隐藏断言；这是后续独立包，本包不实施。

## Self-review / 验收重点

1. 不把历史过期当当前失败，也不把真正失败当历史噪声。
2. 不只修 Engine：Runtime 的待验证路径、authority 和最终快照结果必须一致。
3. 不凭 report.status 或模型声明授予完成能力；必须使用宿主事实。
4. 不以完整率目标取消可信验证；修复后单次真实成绩仍需实测，不承诺19/19完整交付。
5. 本计划有意调整上一包“tracker/finish语义不改”的范围；只更新与新契约矛盾的测试，不削弱外部变化、权限、取消和资源清理的负例。
