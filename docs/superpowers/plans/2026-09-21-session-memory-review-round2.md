# TriCoder 会话记忆第二轮审查：修复交接文档

日期：2026-09-21。状态：S1—S3 已实施并通过本地回归；证据见 `runtime/session-memory-review-round2/`。

目标目录：`D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli`。下文相对路径均以此为根。

## 1. 任务范围与证据

先读工作区及项目 AGENTS.md、README.md、project.md，再读本文件。前置设计见同目录 `2026-09-18-tricoder-session-memory.md`，上一轮修复见 `2026-09-20-session-memory-review-fixes.md`。

本轮是在当前 R1—R4 修复基础上修正边界问题，不重做整个记忆系统，不进行 LangGraph 迁移。

| 编号 | 优先级 | 问题 | 审查复现 |
| --- | --- | --- | --- |
| S1 | P2 | 最新候选生成失败后仍能保存旧候选 | A 任务候选覆盖到消息 3；B 完成但摘要失败，历史到消息 6；真实保存入口调用假存储仍接受 covered_through=3、next_message_seq=7 |
| S2 | P2 | 决策替代操作重复合并失败 | new 替代 old 后，old 已归档；下一候选重复 new 的 replaces_id=old，merge_review_candidate 抛 MemoryValidationError |
| S3 | P2 | 归档没有逐项删除入口 | 对已归档条目调用真实 _edited_memory，返回找不到记忆条目；归档上限为 40，此外还有总字符上限 |

审查时 62 项记忆专项测试通过，git diff --check 通过；新增复现使用假 Provider、内存对象或假存储，无真实模型/真实数据库访问。没有重跑完整项目测试，没有手工验证 TUI 操作。实施会话必须补成持久回归测试，不能把历史通过记录视为修复证据。

## 2. 全局约束

- 保留已有用户改动，先检查最新代码和 git status；不用 reset、clean、覆盖目录回退。
- 仅修改本项目必要源代码、测试和文档。测试证据放 `runtime/session-memory-review-round2/`。
- 维持新记忆功能默认 off、保存前确切预览确认、Session/generation/原始快照绑定及数据库版本比较。
- 摘要不改变审批、权限、文件影响、unknown_effects、验证证据和清理结果。
- 不新增依赖、向量库、后台 Agent 或完整对话持久化；不读取密钥和真实会话数据库，不调用真实模型，不自动提交发布。
- 网络总结不得在 SQLite 事务中进行；CLI/TUI 共享 runtime 规则，UI 不得绕过覆盖检查和预览绑定。
- 每项先补失败复现，再最小实现、聚焦测试和自我审查。完成后直接进入下一项，不设置重复人工批准关卡。

## 3. S1：保存候选覆盖不足时拒绝保存，并支持重试

### 3.1 代码入口

- `src/tricoder/agent.py`：prepare_review_memory，候选失败后旧 review_memory_candidate 保持不变。
- `src/tricoder/session_runtime.py`：preview_memory_save、save_memory_preview。
- `src/tricoder/context/manager.py`：plan_save_candidate，完整任务选择和覆盖判断。
- `src/tricoder/models.py`：SessionContext 及需要的保存状态。
- `src/tricoder/shell.py`、`tui.py`、`commands.py`：本地记忆命令。

### 3.2 行为决定

首版不提供“部分保存也算成功”的隐式降级。普通 `/memory save` 只有在候选覆盖当前可保存的全部已完成任务时才允许预览和保存；覆盖不足时显示具体范围并引导 `/memory refresh` 重试整理。

`/memory refresh` 是本轮拟新增本地命令：只生成待审候选，不执行业务工具，不直接写数据库。成功后用户仍通过 `/memory save` 确认。若旧候选仍有价值，可以留在内存供查看，但必须标注不是最新候选。

### 3.3 实施步骤

- [x] 给当前场景加失败测试：第二任务成功、收尾总结失败，保存预览必须拒绝，而非写入旧候选。
- [x] 提取统一的覆盖检查，预览和提交都调用；不能仅依赖 UI 一次性警告或 memory_warning。
- [x] 覆盖目标基于已结束任务及合法 call/result 组，不能简单等于 next_message_seq-1。本地 memory_edit、临时系统消息不等于遗漏任务。
- [x] 未闭合任务、非法覆盖边界、无法判定完整性必须明确拒绝，不当作“没有新消息”。避免仅使用 plan.needs_summary=False 认定已经完整。
- [x] 覆盖检查不能因旧历史被压缩而漏掉任务；核对运行时摘要 covered_through、保存候选覆盖及当前保留历史。需要新增可靠水位时，由程序更新，并在所有异常/取消出口传播。
- [x] 增加 runtime 的候选刷新入口，复用现有无工具 summarizer、预算、最多两批、超时和错误分类；不得调用 run_task 来触发刷新。
- [x] 刷新期间串行化任务/会话变更，或使用已验证的快照提交方式；模型等待不阻塞 UI 事件循环。取消必须阻止迟到结果提交。
- [x] 刷新失败保留旧候选与历史，继续禁止普通保存；成功只发布完整候选，不发布第一批成功、第二批失败的部分结果。
- [x] 预览展示已覆盖范围、目标范围和状态。确认期间任务/编辑/clear/Session 变化，重新预览；数据库 CAS 规则保留。
- [x] `render_memory` 区分“曾保存的版本”“当前候选”“是否覆盖最新任务”，不要只显示已保存 revision 造成误解。

### 3.4 验收用例

1. A 候选成功→B 摘要失败→save：拒绝，数据库无变化。
2. 接上一步 refresh 成功→预览确认→重启：能恢复 B 的目标/约束。
3. refresh 超时、取消、第二批失败：原候选与历史不变，无部分保存。
4. refresh 不调用业务工具、不增加业务轮数；新增用量归入记忆请求。
5. 本地编辑产生新序号：不被误判为遗漏任务，也不绕过确切候选检查。
6. 预览后状态变化、会话切换或 clear：拒绝旧预览。
7. 全部历史已覆盖：save 不额外调用模型；persistence=off 拒绝语义保存和刷新。
8. 旧候选落后于已经压缩的历史：不能因看不到原消息而判定覆盖完整；不能恢复来源时明确说明，需要用户处理，禁止伪造完整性。

建议扩展 `tests/test_memory_save_coverage.py`，新增 `tests/test_memory_refresh.py`。

## 4. S2：决策替代的重复合并应幂等

### 4.1 代码入口与原因

`src/tricoder/context/memory.py` 的 merge_review_candidate：replaces_id 每次都被当作一条新替代操作；首次合并已将旧决策移入 archived，但新决策继续携带该字段。下一次总结仍可能返回同样条目。

幂等在这里的含义：重复提交同一替代结果，不重复归档、不报不存在的旧条目、不意外改变现有决策。覆盖水位因确有新历史而推进，允许 revision 随状态变化增加；不要求整个对象完全不变。

### 4.2 实施步骤

- [x] 先写 old→new→重复 new 的失败测试，使用真实 merge_review_candidate。
- [x] 在处理新替代前，识别当前是否已经存在同一 ID、同一替代关系和相同语义的结果，且归档中存在对应旧条目。
- [x] 对一致重复执行无副作用合并；不重复追加 archived，不把旧决策再次移除。
- [x] 新 ID 引用从未存在的旧决策仍然拒绝；同一 new ID 改变 replaces_id 或语义时不能冒充重复操作，按合法候选更新规则检查并保留人工确认。
- [x] 检查模型输入、序列化及重启恢复路径，保证已保存的替代关系再次出现仍能处理。不要只在内存第一次合并后临时删字段绕过旧数据问题。
- [x] 明确归档被用户删除后的替代重复策略：已存在且内容一致的新决策可以凭其已保存替代元数据识别；新的未知替代仍需有效来源。不得因为清理归档又把合法活跃决策变成无法维护。
- [x] 将重复 done/cancelled/superseded 状态一并检查；只修复同类幂等问题，不扩大为模型自动批准执行成功。

### 4.3 验收用例

1. 首次替代：old 归档一次，new 活跃一次。
2. 连续重复替代：无异常，归档数量不变。
3. 保存、重启后再重复：同样通过。
4. 无效旧 ID、冲突的新 ID/替代关系：拒绝且旧对象不变。
5. 删除已归档 old 后，重复既有 new：按明确幂等规则处理；不同的新替代仍不能绕过检查。
6. 同一条目重复终结不生成多个归档项。

建议扩展 `tests/test_memory_updates.py`、`tests/test_memory_persistence.py`。

## 5. S3：增加有确认的归档删除入口

### 5.1 代码入口与目标

`src/tricoder/session_runtime.py` 的 _edited_memory 只查活跃字段；`context/memory.py` 的 archived 上限 40。当前“请选择保留内容”的容量提示缺少实际操作入口。

新增本地命令：

- `/memory archive`：列出当前目标记忆中的归档条目，显示 ID、类别、状态和容量。
- `/memory archive delete <条目ID>`：生成删除预览，确认后删除该归档条目。只处理归档，不删除同名活跃条目或工作区文件。

优先作用于当前 review_memory_candidate；没有候选时作用于 conversation_memory。预览明确标注目标。确认只更新内存及 revision，不自动保存；要更新数据库仍用 `/memory save`，同时满足 S1 覆盖要求。

### 5.2 实施步骤

- [x] 给“归档存在但删除时报找不到”补失败测试。
- [x] 增加独立归档查找及删除逻辑，不把普通 edit 的空文本悄悄扩展为删除不同区域的内容。
- [x] 复用预览绑定机制：session_id、generation、原始运行时/候选快照、revision、消息序号。提交前重新校验。
- [x] 预览显示删除条目、删除后容量和对重启恢复的影响；用户取消则无变化。
- [x] 删除只释放归档容量，保留全部活跃约束/目标/待办及可信执行状态；不为归档删除再创建一个占用同一归档池的条目。
- [x] 不自动删除最早归档，不提高条目上限来替代清理能力。字符预算和条目预算都显示；达到任一上限有可操作提示。
- [x] 明确引用处理：清除归档不导致已有活跃决策重复替代报错，按 S2 规则执行；不复活被删除的终结项。
- [x] CLI/TUI 都接入命令与确认；日志仅记录必要元信息，不写归档正文。
- [x] 当前磁盘保存行不立即删除。用户需再次确认保存才能使本次归档清理跨重启生效，界面必须明确提示。

### 5.3 验收用例

1. 达到 40 条归档，新增终结失败；确认删除一条后，原操作可以成功。
2. 未达 40 条但总字符超限：删除归档释放容量后可继续。
3. 取消、重复提交、切 Session、clear 后确认：不误删、不串会话。
4. 活跃条目、权限、unknown_effects、验证证据不变。
5. 清理候选不提前修改运行时正式记忆；清理正式记忆不错误写入另一候选。
6. 确认保存并重启后归档保持删除；未保存退出则明确不承诺删除已持久化记录。

建议新增 `tests/test_memory_archive.py`，扩展 shell/TUI 命令与确认测试。

## 6. 实施顺序与连续验收

顺序：读取最新基线→S1→S2→S3→连续流程和全量回归。每项保存失败复现和修复结果，不一次性重写整个 Agent。

连续流程必须覆盖：

```text
任务 A 生成候选
→ 任务 B 成功但摘要失败
→ 保存被拒绝
→ refresh 重试成功
→ 预览确认保存
→ 决策替代后重复合并
→ 归档达到容量限制
→ 用户确认清理
→ 再次保存
→ 重启核对
```

断言真实内存/数据库状态、来源和覆盖位置、工具调用次数，不只检查提示文字。全程假 Provider、合成数据；不能把假模型测试解释为真实摘要质量已验证。

从目标项目目录逐条运行：

```powershell
& ./.venv/Scripts/python.exe -B -m unittest discover -s tests -p 'test_memory*.py' -v
& ./.venv/Scripts/python.exe -B -m unittest discover -s tests -p 'test_conversation_memory.py' -v
& ./.venv/Scripts/python.exe -B -m unittest discover -s tests -p 'test_session_runtime.py' -v
& ./.venv/Scripts/python.exe -B -m unittest discover -s tests -v
& ./.venv/Scripts/python.exe -m compileall -q src tests
git diff --check
```

每条检查退出码与测试数量，记录跳过原因。新测试尚未存在时“发现零项”不算通过。环境缺失不得自行安装依赖。

## 7. 文档、兼容性和交付

- [x] 更新 README：普通保存的完整性要求、refresh 的额外请求、归档清理确认和再次保存要求。
- [x] 更新 project.md，记录阶段、实际测试结果、剩余风险，证据放 runtime/session-memory-review-round2。
- [x] 优先兼容当前 schema v2；若确需增加持久字段，采用明确版本/兼容默认值，并在合成旧库上测试，不后台改写真实数据库。
- [x] off 模式保持原行为，无新增模型请求。
- [x] 汇报每个问题的复现与修复证据；区分 runtime 验证、UI 测试及未做的手工测试。
- [x] 不承诺完整历史回放、无损还原压缩前消息或崩溃续跑。

回退优先关闭新记忆功能并新开会话，不删除数据表、不覆盖用户工作区。关闭不等于删除已保存语义数据，也不能恢复已压缩原文。

## 8. 交给实施会话的要求

用户明确授权实施后，按本文件在唯一目标项目中修改源代码、测试和文档。正常阶段无需再次询问；发现重大行为改变、需要依赖安装/真实凭据/真实模型调用或 Git 发布时，再说明具体动作申请授权。交付前完成聚焦测试、连续场景、全量回归与自我审查，不把本计划写成已完成。
