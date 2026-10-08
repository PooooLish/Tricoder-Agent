# 工作区快照自动建立与跨重启恢复执行计划

> **For agentic workers:** 使用 superpowers:executing-plans，在单个 coding session 中按阶段执行；先回归测试，再最小实现。本轮不自动提交或推送。

**Goal:** 会话激活时自动扫描，保存每个 Session 最后认可的工作区基线；恢复时无变化直接继续，有变化才询问，拒绝只取消当前操作并保留会话。

**Architecture:** 保留现有进程内 WorkspaceBaseline、扫描器和工作区锁，增加不含源码正文的持久化基线。跨重启比较使用独立的稳定内容清单，不能直接复用包含时间/文件身份的 snapshot_id；通过比较后以新鲜扫描构建运行期基线，原有验证证据仍不得跨重启恢复。

**Tech Stack:** Python 3.11+、现有 SQLite/unittest；不新增依赖。

**Spec:** 本文第 1—5 节是本轮已确认方向的具体执行规格；第 6—8 节为任务及验收。用户将本文交付给 coding session 即按本文实施，不另启动其他会话。

## 0. 全局约束与当前状态

- 先读 ../../AGENTS.md、AGENTS.md、README.md、pyproject.toml、project.md 和当前 diff。保留未提交修改，不 reset/checkout 覆盖，不自动提交或推送。
- 仅在本项目实施。禁止读取 .env.local、真实凭据或真实用户 sessions.db；用临时 SQLite、合成工作区、fake Provider；不安装依赖、不调用真实模型。
- 当前 project.md 已记录 compileall -r 范围修复完成待复审；保留其代码及回归，不把它的实现方结果冒称为本轮独立复审。此次不捎带修改 git_diff、Plan/Replan、摘要策略或沙箱。
- 工作区锁、Session 锁、完整扫描限制、确认后复扫、UNKNOWN、撤销冲突、取消/清理/审计失败和历史验证义务继续生效。
- 对只读回顾，历史 pending/legacy_unknown 不成为本轮修改门禁。接受新基线不能清除这两类义务，也不能授予通过 evidence。
- 哈希一致只说明扫描范围内内容及约定元数据一致，不表示代码正确、测试通过或操作绝对安全。

## 1. 原因与代码入口

| 位置 | 当前行为 | 本轮职责 |
|---|---|---|
| src/tricoder/workspace/snapshot.py | 完整扫描、WorkspaceBaseline、文本 diff；snapshot_id 包含 identity 等字段 | 保留运行期扫描安全语义，提供稳定持久化投影 |
| src/tricoder/workspace/gate.py | baseline 缺失且要求初始化时弹确认 | 支持无记录自动初始化、持久化清单比较和元数据变化预览 |
| src/tricoder/session/runtime.py | _workspace_baselines 仅内存；初始化/切换设置确认；切换丢旧基线 | 编排激活扫描、恢复、任务前复查、认可基线持久化 |
| src/tricoder/session/store.py | Session 与记忆 SQLite 存取 | 增量迁移和基线独立读写，不混入语义记忆 |
| src/tricoder/presentation/shell.py | SessionRuntimeError 被展示为任务运行失败，随后返回输入循环 | 拒绝正常取消文案，保持活动 Session |
| src/tricoder/presentation/console.py、tui.py | 初始化确认和变化确认展示 | 显示一致/变化/取消/扫描失败，避免重复初始化打扰 |

代码中 Shell 拒绝后的现有路径并非必然退出会话；不要以截图猜测改退出逻辑，应通过真实 Shell 测试确认活动 ID 和下一条输入仍可用。

## 2. 用户可见行为

| 情况 | 行为和文案 |
|---|---|
| 全新 Session 首次激活/首次任务 | 完整扫描后自动建立基线，无 y/N |
| 旧版 Session 从未保存过基线 | 一次自动建立；提示“已建立当前工作区基线，无法核对此前变化” |
| 切换或重启恢复，基线一致 | 非交互通知“扫描范围内未检测到变化，可以继续任务”；每次激活最多一次，无 y/N |
| 同一次激活下后续任务且无变化 | 正常继续，不每轮重复一致通知 |
| 和原基线不同 | 展示完整分页路径差异并询问“工作区存在相对于本会话上次基线的变化，是否以当前状态继续工作？[y/N]” |
| 不同意或取消变化确认 | “已取消本次操作，会话保留”；不调用模型、不写任务文件，不覆盖原基线；仍可输入命令/切换/退出 |
| 扫描失败或扫描不完整 | 明确原因，停止此次工作区操作，保留会话/旧基线；不得用部分结果放行 |
| 持久化记录损坏/版本不支持/绑定不匹配 | 明确“历史基线不可用”，保留记录；不得走无历史自动初始化 |

扫描期间工作区锁被其他任务占用：不接管、不长时间卡住；会话仍可选中，标记“工作区检查待执行”，告知当前不能启动文件任务。后续任务取得锁后完成同一检查，不能把 pending 状态视为已检查。

确认只接受特定候选：确认后必须复扫，若变化则旧确认失效，沿用有界确认次数；拒绝时不退出整个会话。激活时已确认的状态若保持一致，第一条任务不得重复弹同一确认。

## 3. 持久化数据与比较边界

### 3.1 稳定清单

建议新增 src/tricoder/workspace/baseline_record.py，集中处理纯数据结构、序列化校验与比较，避免把所有细节堆入 runtime.py。

建议接口（允许根据现有类型做局部命名调整，需同步文档）：

```python
make_baseline_record(baseline: WorkspaceBaseline) -> BaselineRecord
compare_baseline_records(before: BaselineRecord, after: BaselineRecord) -> BaselineComparison
```

BaselineRecord 包含 format_version、scope_version、workspace_key、稳定根身份标识、complete、排序去重 entries、content_digest；每个 entry 包含安全相对路径、文件/目录类型、大小、内容摘要、约定权限模式。时间戳作为展示字段可另存，不参与内容摘要。revision 由存储层维护。

- 使用规范序列化和 SHA-256。目录清单也纳入，确保空目录增删可见；类型、路径、权限变化不得被仅比较文本内容掩盖。
- 文件 mtime/ctime、inode、扫描时间不进入稳定内容摘要；编辑器原子保存相同内容应判一致。根身份单独校验，根目录被替换不能被误当原工作区；不要把目录 mtime 当根稳定身份。
- 当前 snapshot_id 和运行期 identity 校验保留，仍用于扫描内稳定性、竞态与证据绑定，不能为了消除提示删掉它们。
- 全量扫描后才能投影；保留当前排除项和扫描资源上限，scope_version 必须绑定这些比较语义。路径规则遵循现有 WorkspacePolicy/规范化方式，不新增另一套路径放行规则。
- 数据不含 text、文件正文、原始工具输出、完整聊天、审批许可或验证 evidence。禁止直接 asdict(WorkspaceBaseline) 入库，因为 entries 中有 text。
- 版本/扫描范围/工作区根不匹配不可报告“一致”；需要显式重建确认并说明原比较不可用。记录损坏先报错，不能自动覆盖；第一版无需新增修复命令。
- 此能力不是防本地数据库篡改的密码学认证，也无法推断“改过后恢复原内容”的历史事件；只比较受扫描范围的当前状态。

### 3.2 存储

在现有 SessionStore 增加独立表 session_workspace_baselines，按 session_id 主键绑定 sessions；保存版本化 payload、revision、updated_at。不要放进 conversation_memory 或让 LLM 填写。

建议接口：

```python
load_workspace_baseline(session_id: str) -> StoredBaseline | None
save_workspace_baseline(session_id: str, record: BaselineRecord,
                        *, expected_revision: int | None) -> StoredBaseline
```

StoredBaseline 包含 record 与 revision；expected_revision=None 仅允许首次插入，已有记录用 revision 对照更新，防止旧缓存覆盖新版本。迁移幂等、旧行不捏造历史基线，读写使用既有事务。

- None 只表示旧版/新会话从未建立记录；JSON 损坏、未知版本、无效路径、重复/越界路径、非法摘要/枚举、complete=false 等必须抛固定类别错误。
- 建议在 sessions 加 baseline_initialized 标记，与首次基线插入同事务完成。标记为真但记录丢失属于异常，不是首次初始化。已有表记录存在但标记不符同样不能静默重建。
- 校验 payload/entry 数量及总序列化尺寸上限；不可无界读取数据库 JSON。与现有 SnapshotLimits 协调设置并写明固定值；沿用项目参数化 SQL、权限和事务做法。
- 会话摘要或 /memory save 不管理这些宿主元数据，不增加用户保存确认。Session 记忆清空不能顺便删除有效基线；/clear 的显式文件状态确认需核对现有契约，在原已确认流程中更新基线。
- 保存失败：保留旧记录，不报告已跨重启保存；任务启动前无法保存必要的接受基线则阻止启动，留在会话中；收尾保存失败要展示独立持久化失败事实并留旧基线供后续重新核对，不能伪称业务代码失败或成功落盘。

### 3.3 比较与展示

恢复的历史清单没有旧源码，只能可靠展示“新增/删除/内容修改/类型或权限变化”的路径列表。明确“跨重启仅提供文件级差异”；不能生成伪造逐行 diff。当前进程有旧 text 时仍沿用现有完整文本 diff。

## 4. 生命周期与并发规则

1. **激活目标 Session：**取得其 Session 所有权、恢复元数据；取得短时工作区锁后完整扫描。会话选择和工作区可执行状态分别表示；扫描失败/拒绝后保留目标会话，但未通过门禁不得执行文件任务。激活结束释放工作区锁，空闲不持有。
2. **有旧记录：**以旧记录比较当前扫描。稳定摘要一致且绑定合法时，把当前新鲜扫描安装为进程内基线；不恢复旧 evidence。不同则走确认、复扫、原子保存。变化路径仍通知现有 _observe_workspace_change，失效旧文件证据；确认后的变化通知必须保留到下一任务装配，不能在切换时被吞掉。
3. **没有旧记录：**完整扫描成功后自动初建并持久化；旧会话告知无法核对此前历史。该动作不清除已有 unknown_effects、legacy_unknown、pending 或未确认的任务清理状态。
4. **任务开始：**仍取得工作区锁、重新完整扫描，和最后认可基线比较。不能用激活时快照作为可跨空闲期免检许可。激活拒绝/扫描失败/锁忙时，任务必须重做门禁。
5. **任务收尾：**复用 _finalize_workspace_task 的可归属判断，只在现有合法推进条件满足时，把认可的内存基线和持久化记录一起推进。失败、取消、UNKNOWN、taint、清理失败或未归属外部变化不能自动吸收；无变化或框架自身路径特例保留既有精确规则。
6. **切换离开/关闭：**持久化或确认已持久化的是最后认可基线。不得重新扫描后直接接受当前磁盘状态；失败任务留下的变化必须在下次仍然可见。持久化失败不得无提示丢弃待处理状态。
7. **撤销与显式清理：**核对 _finalize_undo_workspace 及 clear 相关入口；合法完成后的基线推进同样入库；冲突/拒绝不更新。不要仅修正常任务路径。
8. **A/B 两会话同工作区：**各自保存基线。B 接受新状态不更新 A；A 再激活时必须看到相对 A 的变化。沿用现有锁顺序，不引入反向锁等待。

稳定摘要一致后必须以当前扫描重新建立严格运行期基线，不用持久化数据拼造含旧 identity 的 WorkspaceBaseline 供进程内验证。外部变更确认也不意味着语义记忆里的旧文件判断全部正确；继续要求 Agent 读取当前文件。

## 5. 五个复审重点

1. 切换/关闭/失败任务收尾不能偷偷接受外部或未归属变化；对应 T3、T4。
2. 损坏记录、缺失记录、未知版本和扫描范围变化不能全部折叠成无基线；对应 T1、T2、T3。
3. 内容相同的原子保存无需询问，但根替换、目录变化和权限变化仍需识别；对应 T1、T3。
4. 确认期间/激活后再变更必须重新检查，旧确认不可重复使用；对应 T3。
5. 拒绝/锁忙/扫描失败不得结束交互会话，也不得意外发起模型调用；对应 T3、T4。

## 6. 分阶段实施

### T0：锁定现状及回归基线

**Files:** project.md、tests/test_session_runtime.py、tests/test_workspace_gate.py、tests/test_shell.py。

- [ ] 保存 git status/diff 和当前相关测试结果到 runtime/workspace-baseline/；禁止覆盖已有验证义务证据。
- [ ] 用真实 SessionRuntime + 临时 SQLite + fake Provider 建立“恢复缺基线重复确认”的现状测试；为目标行为编写失败断言，失败必须落在行为断言，不是导入/配置错误。
- [ ] 验证拒绝后 Shell.execute 返回 None、current.record.id 不变、下一条 /status 可执行；将正确现状保留为回归，不虚构必然退出缺陷。

### T1：稳定内容清单与纯比较

**Create:** src/tricoder/workspace/baseline_record.py、tests/test_workspace_baseline_record.py。
**Modify only if needed:** src/tricoder/workspace/snapshot.py。
**Interfaces:** 实现第 3.1 节 BaselineRecord、BaselineComparison、make_baseline_record、compare_baseline_records。

- [ ] 先测：相同内容 touch/原子保存摘要一致；内容/路径/文件目录类型/空目录/权限变化被发现；根替换不相等；正文不出现在序列化 payload；不完整扫描拒绝。
- [ ] 实现独立稳定摘要和文件级差异，不修改原 snapshot_id 及运行期扫描稳定性。
- [ ] 运行新模块和 test_workspace_snapshot.py，确认上述正反例通过。

### T2：增量存储及损坏区分

**Modify:** src/tricoder/session/store.py。
**Create:** tests/test_workspace_baseline_store.py。
**Interfaces:** 第 3.2 节 load/save，StoredBaseline、CAS revision；只消费 T1 的已校验纯数据。

- [ ] 测试旧库重复迁移、初次插入、往返恢复、CAS 冲突、事务回滚、A/B 行隔离；原 session/conversation_memory 数据保持。
- [ ] 测试 initialized=true 但记录缺失、JSON/路径/摘要损坏、unsupported format、complete=false、尺寸超限明确报错；不能返回 None。
- [ ] 实现表/标记迁移和存储接口，保持 schema 初始化幂等，不连接真实用户数据库。
- [ ] 运行新模块和 test_sessions.py；扫描 payload 断言不存在源码与工具原文哨兵。

### T3：接入激活、门禁和收尾

**Modify:** src/tricoder/workspace/gate.py、src/tricoder/session/runtime.py。
**Create:** tests/test_workspace_baseline_recovery.py。
**Related tests:** test_workspace_gate.py、test_workspace_consistency.py、test_session_ownership.py。
**Interfaces:** Runtime 使用 T2 读写、T1 比较，Gate 保留确认后的严格复扫；持久化错误/确认拒绝/扫描失败使用可区分的结果或异常类别，UI 不解析中文字符串判断状态。

- [ ] 用真实关闭/重新构造 Runtime 测试相同文件不询问；变化一次确认；拒绝留原基线和 Session；接受复扫后不重复询问，且重启恢复正确。
- [ ] 测试 A→B→A 同工作区，B 修改后 A 看到差异；同内容原子保存不确认；同名工作区不同根不串行；新会话初建不继承其他 Session 的认可状态。
- [ ] 测试激活后空闲修改，首次任务重新确认；确认期间变化再次确认；锁忙后重试；扫描失败不调用 Provider、不覆盖记录。
- [ ] 测试失败任务、取消、外部变化、UNKNOWN 后退出重启仍有门禁/历史义务；pending/legacy_unknown 不因接受基线消失；也不恢复 evidence。
- [ ] 测试任务收尾存储失败、切换保存失败、根替换/范围版本不兼容均不宣称已恢复一致；撤销成功推进、撤销冲突不推进。
- [ ] 小步接入第 4 节所有生命周期入口，基线持久化集中在少量助手中；不要为此大拆 runtime.py 或复制新扫描器。
- [ ] 运行恢复与门禁/一致性/所有权专项，保留日志及 fake Provider 调用计数。

### T4：交互及文档

**Modify:** src/tricoder/presentation/shell.py、console.py、tui.py、README.md、project.md。
**Tests:** tests/test_shell.py、tests/test_ui.py、tests/test_tui.py。

- [ ] 测试一致只提示一次且无确认；差异分页完整，拒绝显示正常取消；Session ID 不变、下条输入可执行；新/旧首次基线文案不声称历史未变。
- [ ] 测试 Console 与 TUI 的扫描失败/记录损坏/锁忙和拒绝分支；任务开始前不显示“任务完成/失败”来冒充运行结果。
- [ ] 更新 README 快照章节：自动持久化的宿主元数据、旧会话首次初始化、扫描边界、跨重启只有文件级差异、拒绝不退出、不会恢复通过能力。
- [ ] 更新 project.md，记录迁移兼容性、实施阶段、命令/数量/跳过原因和待复审事项；第一版无需提供数据损坏自动修复功能。

### T5：最终验证与交接

- [ ] 各阶段先运行所在测试模块；全部实现后运行下面相关组合，再完整回归一次。若失败，记录修复前结果和根因，不用全量通过替代边界探针。
- [ ] 自查没有持久化正文/工具输出/审批/evidence，没有清空历史义务或修改旧命令白名单，没有自动 git init。
- [ ] git diff --check，人工检查迁移、确认绑定、所有基线更新点和 UI 文案。
- [ ] 交付“实现完成，待复审”，附证据目录、未验证平台和已知限制；不自行声称独立复审通过。

## 7. 验证命令

在项目根 PowerShell 使用现有虚拟环境，不安装依赖：

```powershell
$baselineTestPatterns = @('test_workspace*.py','test_session*.py','test_shell.py','test_ui.py','test_tui.py','test_verification*.py','test_task_outcome_memory.py','test_agent_termination.py')
foreach ($baselineTestPattern in $baselineTestPatterns) {
    & .\.venv\Scripts\python.exe -B -m unittest discover -s tests -p $baselineTestPattern -v
    if ($LASTEXITCODE -ne 0) { throw "测试失败：$baselineTestPattern" }
}
.\.venv\Scripts\python.exe -B -m unittest discover -s tests
git diff --check
```

日志保存 runtime/workspace-baseline/，记录每个命令退出码、实际数量及跳过原因。至少一条跨重启恢复测试必须经过真实 SessionStore、真实扫描器和真实 Runtime 激活/任务入口，不能只测哈希助手或全 mock 的比较结果。

## 8. 最终验收清单

- [ ] 重启/切换后代码一致：不问 y/N。
- [ ] A 的旧基线在 B 改动后仍保留，A 返回能看到差异。
- [ ] 哈希变化：完整文件级差异→确认→复扫→才接受；拒绝不退出会话。
- [ ] 无历史自动初建只发生一次；损坏历史不静默重建。
- [ ] 当前扫描不完整时不放行，不保存半份基线。
- [ ] 切换与关闭不吸收失败任务的未知变化。
- [ ] 同内容重存不会因 mtime/inode 变化误询问；运行期安全检查没有削弱。
- [ ] 基线记录没有正文；接受基线不等于验证通过；旧义务/UNKNOWN 不被清除。
- [ ] 新功能、既有恢复回顾和 compileall -r/零测试/范围义务回归均通过。

## 9. 可直接交给 coding session 的提示词

```text
请在 D:\MaHong\AGENT_WORKSPACE_V2\projects\tricoder-cli 实施：
docs/superpowers/plans/2026-10-07-persistent-workspace-baseline.md

先读根/项目 AGENTS.md、README.md、pyproject.toml、project.md、完整计划及当前 diff，再按 T0—T5 小步实施和验证。本文第 1—5 节是行为规格，第 6—8 节是任务和验收；按单会话 executing-plans 执行。

目标：激活/切换时自动扫描，持久化每个 Session 最后认可的无正文工作区基线。新会话及从未有基线的旧会话自动初建；切换或重启恢复后相同则无交互继续，不同则展示差异并确认。拒绝仅取消当前操作，保持 Session 可用。扫描失败/锁忙不放行文件任务。真正执行任务前仍持锁复查。

关键边界：自动扫描不等于接受新基线；不能切换/退出时把当前磁盘状态直接覆盖旧记录。损坏/缺失已初始化记录和不支持版本不能伪装成首次使用。不要直接持久化现有含正文的 WorkspaceBaseline，也不要用含 identity/时间的 snapshot_id 充当稳定内容哈希。保留运行期完整扫描与竞态校验、确认后复扫、Session/工作区锁和所有历史验证义务。接受基线不能恢复 evidence 或解除 UNKNOWN/pending/legacy_unknown。

使用临时 SQLite、合成工作区和 fake Provider，先失败回归后实现。必须覆盖真实关闭重启、A→B→A、同内容原子保存、确认期间变化、激活后空闲修改、拒绝后继续输入、失败/取消任务退出后恢复、迁移损坏和存储失败。Console/TUI 均覆盖。保留已经完成的 compileall -r 等修复；不处理 git_diff、Plan/Replan 或摘要功能。

保留用户未提交修改，不读取 .env.local/真实凭据/真实用户库，不调用真实模型、不安装依赖、不提交推送。证据保存 runtime/workspace-baseline/，更新 README 和 project.md，执行专项及最终完整回归。交付实现摘要、测试实际数量/跳过/退出码、剩余限制，标明“实现完成，待复审”。
```

## 10. 实施状态（2026-10-07）

- T0—T5 已在当前保留用户改动的工作树完成，状态为“实现完成，待复审”。
- 实际接口按现有命名采用 `StoredWorkspaceBaseline`；行为和安全边界保持本文规格。
- 新鲜聚焦回归 408 项通过（6 项跳过），最终完整回归 1620 项通过（13 项跳过）。
- 实施账本和最终命令证据位于 `runtime/workspace-baseline/`；详细兼容性与限制见 `project.md`。
