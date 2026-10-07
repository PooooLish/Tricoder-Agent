# 历史验证状态与本轮交付解耦：执行计划

> 执行方式：使用 writing-plans 组织本计划；实施会话按 executing-plans 逐项执行。采用单会话、小步修改、先复现再修复；不自动提交或推送。

**Goal：**恢复历史会话后，“之前做了什么”、解释代码、只读审查等已交付请求，不再仅因历史检查状态过期而被判为未完成；真实修改的验证义务仍保留。

**Architecture：**区分历史检查记录、会话遗留验证义务、本轮执行门禁。持久化保留义务元数据，但不恢复通过证据；Agent 与 Runtime 使用一致的本轮完成判定。语义记忆、模型文字和工具自报字段均不能解除宿主验证义务。

**Tech Stack：**现有 Python 3.11+、dataclass、SQLite、unittest；不增加第三方依赖。

**Spec：**本文件第 1—4 节是本轮设计约束。前置背景见 project.md，以及 runtime/task-outcome-memory/review2-findings.md；此前 F1/F2/F3 的正确行为必须保留。

## 1. 已确认的根因与范围

当前链路（行号仅供定位，实施时重新核实）：

| 位置 | 当前行为 | 问题 |
|---|---|---|
| session/runtime.py:1582，SessionStore | 保存 verification 字符串 | 未记录检查结果与修改验证义务的区别 |
| session/runtime.py:_build_active，约 2889 | passed/failed 统一降为待验证 | 降级证据合理，但丢失来源的状态不应产生新义务 |
| engine/state.py:AgentRunState.start | Session 验证状态及累计修改进入新任务 | 会话范围与本轮范围混用 |
| engine/loop.py:_initialize_verification，约 549 | 历史状态＋无证据，或累计 modified_files，建立 required | 没有本轮修改也可能强制验证 |
| engine/loop.py:_finish_success，约 677 | required 未满足则 completed=False | 回顾问题被历史验证义务否决 |
| session/runtime.py:_run_task_locked，约 1573 | required/trusted_pass 再次否决结果 | 只改内层不会完整修复 |
| task_observation.py、engine/tool_batch.py | 回填、合并 required 与 evidence | 旧义务可能经观察器重新流入本轮 |

已独立复现：临时真实 SessionRuntime/SQLite，fake Provider，首次只读审查发现语法检查失败，ok=true、required=false；关闭恢复后重复审查，ok=false、required=true、modified_files=0。恢复入口先把 failed 降为待验证；再次失败检查导致“文件修改后的验证失败”。用户最新截图是恢复后只读回顾、未执行检查，表现为“文件修改后尚未运行验证命令”。两者属于同一范围混淆。

本轮不处理：40,000 字符摘要输入上限、大任务分段摘要、Plan/Replan、沙箱、通用业务验收器、Git 工具 UX。不得把这些独立问题混入修复。

## 2. 目标行为与安全边界

| 场景 | 本轮交付 | 历史状态 |
|---|---|---|
| 恢复后回顾历史，无写入、finish completed | 可完成 | 旧检查未经重新确认，不称为当前通过 |
| 只读审查发现测试失败，无本轮修改 | 可完成报告 | 保留真实失败检查 |
| 历史真实修改待验证，本轮只回顾 | 可完成回顾 | 修改验证义务仍为 pending，不清除 |
| 本轮实际修改但未验证，finish completed | 不完成 | 保留本轮修改验证义务 |
| 本轮实际修改且有宿主有效通过证据、无阻断 | 可完成 | 仅解除证据确实覆盖的义务 |
| finish incomplete | 不完成 | 不得因“无写入”改为成功 |
| UNKNOWN、取消、清理失败、审计失败、相关终态扫描不完整 | 按现有安全规则阻断 | 不因是回顾/审查而豁免 |

不得从中文关键词判断 task_type，不从摘要或“我完成了”推断安全事实。无本轮修改不等于必然成功；必须同时满足 finish 声明、历史完整性和既有安全门禁。

保留限制：本轮不增加语义验收器，无法自动证明模型所有自然语言结论正确。历史义务不阻塞回顾，不表示允许 UI 把历史修改标为已验证。验证专用任务若缺少测试，仍应由既有任务验证信息如实显示，不伪造通过。

## 3. 最小数据设计

### 3.1 会话义务作为独立宿主元数据

建议追加以下字段到 SessionMemory，并在 SessionContext 中带入对应会话状态（名称可以按现有风格微调，语义必须保持）：

- `verification_obligation: Literal['none', 'pending', 'legacy_unknown']`。
- `pending_verification_paths: tuple[str, ...]`：工作区相对路径，独立于用于 diff/undo 的累计 modified_files；目录按项目已有规范处理。

新建会话默认 none、空路径。pending 由宿主确认的修改建立，不能由模型或结构化记忆设置。legacy_unknown 表示老数据来源不足，不能解释为 none 或已通过。

SQLite session_memory 增加对应状态列及 JSON 路径列即可，沿用已有幂等补列方式；旧行默认 legacy_unknown，新建行显式 none。允许旧行有已知修改路径时保守迁为 pending；没有已知修改但历史 passed/failed/pending 的，保留 legacy_unknown，不伪造确定结论。旧状态为空且无修改/UNKNOWN 的处理需由迁移测试明确固定。

字段追加默认值，兼容现有构造；枚举、JSON 类型、路径边界严格校验。unknown_effects 是独立硬阻断，不并入上述枚举。无需保存源码、测试输出、审批、可用 evidence、文件句柄或绝对私有路径。

### 3.2 本轮门禁独立计算

`AgentRunState.verification_required` 作为本轮门禁使用。新任务不能仅因历史 verification 字符串或累计 modified_files 非空把它设为 True。

本轮 required 的来源：宿主确认的本轮修改；本轮动作造成的验证状态失效；本轮相关检查记录失效等既有保守条件。UNKNOWN/取消等独立门禁继续保留。

所有初始化、观察器 publication/reconcile、工具批次回填、最终工作区核验必须保持这个范围。不能在 start 设 False 后又从 Session 原字段无条件 OR 回来。不要删除累计修改记录来获得 False；不要仅以净 diff 判断本轮是否有写入，A→B→A 仍有实际操作，需要沿用真实账本/观察事实。

本轮待验证义务在结束时合并到 Session pending；下一轮回顾不能清空它。后续验证只有在现有 VerificationScope authority、快照和覆盖范围均有效时才能解除对应义务。若当前证据不能证明覆盖全部历史路径，则保留剩余 pending；无关通过、版本查询、普通读取不能解除。legacy_unknown 不能通过模型文字或无关检查解除；沿用明确的检查/确认边界，不新增自动信任捷径。

## 4. 完成判定与展示

Agent 和 Runtime 必须遵守同一契约：本轮 completed 声明＋本轮验证要求满足＋当前安全状态允许。历史 pending/legacy_unknown 作为独立报告信息，不能单独否决本轮回顾。

可以提取一个很小的纯函数供两层共用，或复用现有结果契约；不要为此重构整个 Agent。Runtime 仍需独立验证当前 evidence 和文件状态，不能直接信任模型或 Agent 的布尔值。

同时修正 Runtime 的 `(reconciled.modified_files and verification == '待验证')` 条件：这里若仍使用累计文件列表，会再次把历史范围当本轮范围。必须基于本轮可信效果判断，不能单纯删掉保护。

RunResult/展示可追加带默认值的历史义务字段，以便 Console/TUI/状态命令一致展示，避免解析自由文本：

```text
本轮交付：已完成
本轮验证：未运行（本轮未产生修改验证义务）
历史修改验证：尚未确认 / 仍有待验证项
```

只有存在真实本轮修改时，才显示“本轮修改尚未验证”。历史证据失效显示历史说明；检查失败显示检查事实。不要把所有失败改成成功，不删除历史警告，不显示未经证据支持的“验证通过”。

## 5. 逐步实施

### R0：固定失败场景

**Files：**tests/test_task_outcome_memory.py、tests/test_verification_evidence.py、tests/test_session_runtime.py；可新建 tests/test_verification_obligation.py 集中新增组合。

- [x] 使用临时工作区/数据库、fake Provider，新增“只读检查失败→关闭 Runtime→恢复→只读回顾→finish completed”。预期两轮交付均可完成、失败事实仍保留；先确认当前代码失败。
- [x] 补“历史检查通过→恢复→回顾”对照，证明 passed 同样不应制造新义务。
- [x] 补“历史真实修改未验证→恢复→回顾”，预期回顾完成但 pending 不消失；这是防止清空安全状态的关键测试。
- [x] 通过实际 _build_active 恢复，不能只替换 SessionContext 跳过生产路径。记录失败断言及输出。

### R1：持久化与兼容

**Files：**src/tricoder/models.py、src/tricoder/session/store.py、src/tricoder/session/runtime.py；tests/test_sessions.py、tests/test_session_runtime.py。

- [x] 按第 3 节添加宿主义务字段、追加式数据库迁移、严格读写校验。先写测试再实现。
- [x] 测试旧表有 passed/failed/待验证、空修改/真实修改、UNKNOWN，重复 initialize，损坏枚举/JSON，保存恢复不丢义务。
- [x] 保留旧 verification 作为历史观测值或明确过期展示；不能恢复通过能力。新会话与老会话未知值必须可区分。
- [x] 在同一既有事务中保存互相关联的状态，数据库异常不留下部分行更新；仅临时 DB 测试迁移，不操作真实 sessions.db。

### R2：本轮范围与两层收尾

**Files：**engine/state.py、engine/loop.py、engine/tool_batch.py、task_observation.py、session/runtime.py；相关 tests。

- [x] 分离 Session 历史 pending 和本轮 required，追踪所有赋值及 OR 合并位置。
- [x] 保持真实本轮编辑、命令副作用、净零写入、目录变化建立义务；读取和 no-op 不建立。
- [x] 修改 Agent 完成判断、Runtime required/trusted_pass 与累计 modified_files 门禁，统一第 4 节契约。
- [x] 本轮通过只更新其证据覆盖的 pending，不能用一个无关检查清除整个会话义务；修改撤销和 /clear 保持其既有明确确认语义。
- [x] 运行 R0 测试转绿，并补 cancel/cleanup/UNKNOWN/外部修改/审计失败负向测试。

### R3：显示与记忆边界

**Files：**models.py、presentation/console.py、presentation/tui.py、presentation/commands.py、README.md；tests/test_tui.py 及现有展示测试。

- [x] 区分当前交付、当前检查事实、历史义务；不把历史待验证描述成本轮修改未验证。
- [x] 覆盖 finish incomplete、有历史 pending 的 completed、纯审查失败检查、无 Git 目录等场景。
- [x] 保持结构化记忆低信任：pending 文本不建立宿主门禁，模型把待办写成 done 也不能清除宿主 pending。
- [x] 回归 F1/F2/F3 的失败终止事实、覆盖水位、refresh/save/restart、旧预览失效及保存确认；修复后本轮确已完成时不凭空追加 task_incomplete。

### R4：整体验收与交接

- [x] 运行以下聚焦回归；新文件若采用建议名字，加上对应命令：

```powershell
.venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_verification_obligation.py" -v
.venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_verification_evidence.py" -v
.venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_task_outcome_memory.py" -v
.venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_session*.py" -v
.venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_memory*.py" -v
.venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_agent_termination.py" -v
.venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_tui.py" -v
```

- [x] 聚焦通过后完整回归一次：`.venv\Scripts\python.exe -B -m unittest discover -s tests`；记录数量与跳过理由，不把未运行写成通过。
- [x] 执行 `git diff --check`，自查新增持久化字段的所有构造/replace/SQL 路径，检查旧会话及新会话均不会漏默认值。
- [x] 将证据放在 runtime/verification-obligation/；更新 project.md、README.md，说明迁移及未覆盖边界。
- [x] 保留用户修改，不提交或推送。最终交付状态为“修复完成，待复审”。

## 6. 审查必须核对的边界

1. 新会话与恢复会话面对相同无副作用请求，不能因历史展示字符串不同而任意改变交付结论。
2. 历史真实修改待验证，不阻止回顾，但也不会被回顾、无关测试通过或模型摘要悄悄清除。
3. 当前写入产生义务，即使净 diff 为零、modified_files UI 仍显示累计范围，也不能漏判。
4. 两层收尾、TaskObservation 和恢复分支一致；只修一个 if 不算完成。
5. 老数据来源不足明确显示未知；UNKNOWN 仍硬阻断；损坏数据不能当新空会话。

## 7. 可直接交付给 coding session 的提示词

```text
请在 D:\MaHong\AGENT_WORKSPACE_V2\projects\tricoder-cli 中执行
docs/superpowers/plans/2026-10-07-verification-obligation-scope-fix.md。

先读取根/项目 AGENTS.md、README.md、pyproject.toml、project.md 和完整执行计划。
本轮目标是分离“历史检查结果、会话遗留修改验证义务、本轮完成门禁”，修复恢复会话后只读回顾也被判未完成的问题。

按 R0—R4 逐步完成，先用真实 SessionRuntime＋临时 SQLite＋fake Provider 复现，再做最小修改。必须走实际 _build_active 恢复路径，同时修改初始化、观察器状态合并、Agent 收尾和 Runtime 最终门禁；不能只改 UI 或某一个 completed 条件。

历史 passed/failed 不能因缺少旧 evidence 而凭空创建本轮修改义务；历史真实 pending 必须持久保留，回顾完成不能解除它。本轮实际修改仍须有效验证，取消、UNKNOWN、清理/审计失败和最终扫描等安全边界保持。旧库按文档追加迁移、来源未知明确标记，不伪造历史通过证明。

保留所有既有未提交修改，特别是记忆默认开启、finish.outcome、检查事实分离、失败记忆和 F1/F2/F3。不要关键词识别任务类型，不让模型/摘要决定可信状态，不删除历史来消除门禁，不把任意无关通过用来清空所有 pending。

本轮不处理摘要输入超限、大任务分段摘要、Plan/Replan 或沙箱。不得访问真实 .env.local/凭据/会话库、调用真实 Provider、安装依赖、提交或推送。

完成聚焦与完整回归，记录迁移、恢复、scope、安全负向和展示验证证据；更新 project.md 和 README.md。交付根因、改动、测试、剩余边界，停在“修复完成，待复审”。
```
