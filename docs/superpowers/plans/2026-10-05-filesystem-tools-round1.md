# TriCoder 常用文件工具补齐：第一轮执行方案

> **For agentic workers:** 使用 `superpowers:executing-plans` 按阶段实施，每阶段先补失败回归，再小步修改、验证和自审。不要自动提交或推送。

**Goal:** 用户要求“创建 game 目录并编写程序”时，Agent 能通过专用工具完成，不再反复调用无法创建父目录的 create_file，最后尝试被拒绝的 Shell 命令。

**Architecture:** 新增受控目录创建能力，与文件创建共用目录操作实现；把目录身份、变更、副作用、撤销和工作区对账纳入本地可信状态。保持命令白名单与现有权限语义，不靠放行 mkdir/PowerShell/Python 任意代码弥补工具缺口。

**Tech Stack:** Python 3.11+，现有 unittest、目录绑定、ChangeJournal、ToolRegistry、CLI/TUI；不新增第三方依赖。

**Spec:** 本文第 1—6 节为本轮设计与验收约束，第 7 节为执行步骤。

**状态：** S0—S5 已实施并完成离线验收；实际接口与证据见第 7 节及
`runtime/filesystem-tools-round1/`。未验证项保留在项目交接记录中。

## 1. 当前事实与问题根因

分析基线为 `de0af48`，实施前仍须核对 HEAD 和工作树，保留之后出现的改动。

- `src/tricoder/tools/__init__.py` 的内置工具列表没有目录创建工具。
- `src/tricoder/tools/write.py::CreateFileTool.run` 明确要求父目录存在，否则返回 INVALID_ARGUMENT。
- 同文件的 ApplyPatchTool 也拒绝父目录不存在的补丁目标。
- `tests/test_tools.py::test_create_file_rejects_missing_parent_before_approval` 固定了旧行为，不能无意破坏兼容性。
- `run_command` 是受限执行器，不接受任意 mkdir、Shell 或 PowerShell 命令。截图没有展示实际命令参数，不能编造它究竟尝试了哪条命令。
- `changes.py` 的 FileSnapshot/FileChange 目前仅表示文件；目录绑定提供文件发布操作，没有 mkdir/rmdir 接口。
- 工作区扫描可以记录目录，但 `workspace/snapshot.py::task_changes_match_baselines` 主要按文件账本解释变化。目录变化仅在特定条件下作为已登记文件的祖先被接受，不能因此认为已经支持独立空目录变更。
- 当前 CONFIRMED 文件副作用会触发待验证并可能清除旧失败。不能把 mkdir 直接伪装成普通文件写入。

因此，补齐工具注册只是入口；还必须补齐可信状态和撤销。此次问题不是应该继续放宽命令白名单。

## 2. 工具补齐范围与优先级

| 能力 | 当前情况 | 本轮决策 |
| --- | --- | --- |
| 列目录、读文件、glob、grep | 已有专用工具 | 保留，说明如何组合使用 |
| 创建目录/空目录/多层目录 | 缺失 | 新增 create_directory |
| 创建文件并补父目录 | create_file 拒绝缺失父目录 | 新增显式 create_parents 选项 |
| 多文件补丁的父目录 | 必须预先存在 | 本轮仍先 create_directory，再 apply_patch；完善提示 |
| 查询路径类型/是否存在 | 可用现有列目录/搜索组合，但不够直接 | 后续考虑 path_info，不混入第一轮 |
| 查询当前工作区/解释器 | CLI 状态和固定版本查询已有部分能力 | 后续考虑只读 workspace_info，不靠 pwd 等 Shell 猜测 |
| 移动、重命名、复制文件 | 缺少专用工具 | 第二轮规划，先设计双路径审批、覆盖冲突和撤销 |
| 删除文件/目录 | 无通用删除工具；撤销有受控删除 | 后续单独设计，首轮不新增递归删除 |
| 包安装、任意 Shell、联网下载、启动长期服务 | 当前受限 | 不属于本轮补齐范围 |

本轮不修改 Provider、提示词注入架构、Docker、评测平台、工作区扫描限额和数据库 schema。上次审查的 unittest 混合参数错误分类问题，应单独跟踪，不得在此轮宣称已修复。

## 3. 对模型公开的工具契约

### 3.1 create_directory

建议调用：

```json
{"path":"game/src","parents":true,"exist_ok":true}
```

- `path` 必填，工作区内相对路径；拒绝绝对路径、`..`、敏感/控制目录及链接逃逸。本轮目录链不接受符号链接或 Windows reparse point。
- `parents` 默认 true；允许补齐有限的缺失祖先目录。false 时父目录必须存在。
- `exist_ok` 默认 true；同名普通目录已存在时返回成功且 no-op，不修改它、不记录本任务所有权、不请求写入审批。
- `exist_ok=false` 且目录已存在，或同名目标是普通文件：返回可纠正 INVALID_ARGUMENT，不覆盖。
- read_only 仍拒绝该写工具，包括目标已经存在的情况，保持写工具入口规则一致。
- strict/relaxed 请求一次明确目录清单审批；fullaccess 沿用内置写工具批准策略。审批后必须重新确认祖先身份和缺失目标，不能把竞态出现的目录据为己有。
- `.`/空白路径不作为创建目标；空白由参数校验拒绝，`.` 返回固定可纠正错误。
- 每次最多创建 16 层、单任务最多新增 128 个目录；上限必须由本地控制，不能让模型指定。超限在写入和审批前拒绝。
- 正常输出区分“实际新增的目录清单”与“已存在、未修改”。它不产生测试通过证据。

### 3.2 create_file 的 create_parents

```json
{"path":"game/main.py","content":"...","create_parents":true}
```

- 新参数为可选 boolean，默认 false，保持旧调用及旧测试语义。
- false 且父目录缺失：仍不产生副作用，返回 INVALID_ARGUMENT / REPLAN，固定提示“先调用 create_directory，或显式设置 create_parents=true”。
- true 时，审批前计算缺失目录清单和目标文件 diff；一次审批确认二者，避免多次无意义弹窗。
- 通过共享目录操作实现创建父目录，不通过 ToolRegistry 嵌套调用 create_directory，不额外创建独立任务或绕过一次操作账本。
- 目标文件仍使用已有不可覆盖的原子发布方式，不能降低为普通 write_text 覆盖。
- 已有父目录不归本次所有；目标已存在、路径冲突、预算不足或用户拒绝时，必须在创建任何目录前退出。
- 如果计划跨多个文件，优先先 create_directory，再 create_file/apply_patch，避免每个文件重复创建和审批相同目录。

### 3.3 apply_patch 与模型说明

本轮不为 apply_patch 增加自动创建父目录，以免同时改动多文件补偿事务。父目录缺失时，固定提示先调用 create_directory；重试补丁仍走正常审批。

工具描述及 native/legacy 共用提示须明确：

1. 先按任务需要检查目标位置；需要目录时用 create_directory。
2. 创建嵌套文件可显式使用 create_parents=true。
3. create_file 不能被当作创建目录的替代工具；不要以占位文件模拟空目录。
4. 不要为 mkdir/pwd 等常见文件操作反复试探 run_command。
5. 已存在目录是幂等成功，同名文件冲突需要调整路径；用户拒绝/越界/未知副作用仍停止。

不得通过增加“工具成功即任务成功”的提示绕开真实完成条件。

## 4. 本地可信实现设计

### 4.1 模块分工

| 文件 | 修改职责 |
| --- | --- |
| 新 `src/tricoder/tools/directory.py` | 目录创建工具、有限目录链预检与操作辅助逻辑；供 create_file 共用 |
| `src/tricoder/tools/binding.py` | Windows/POSIX 的安全目录创建、身份读取、仅空目录删除原语 |
| `src/tricoder/tools/write.py` | create_parents 协调、联合审批、错误提示；文件发布机制保持 |
| `src/tricoder/changes.py` | 目录快照和变更账本；变更预算、修订游标、预览与封存 |
| `src/tricoder/execution_state.py`、`models.py` | 可区分文件与目录的最小状态扩展，默认值兼容旧构造 |
| `src/tricoder/tools/__init__.py`、`handlers.py` | 注册 write 风险、仅本地可信副作用归一化、异常收尾、审计 |
| `src/tricoder/task_observation.py` | 接收目录变化；不丢已提交状态，不误清旧验证失败 |
| `src/tricoder/workspace/snapshot.py` | 目录账本与开始/结束扫描的精确对账 |
| `src/tricoder/tools/undo.py`、`session/runtime.py` | 目录撤销、冲突预检、撤销后基线核验与任务完成状态 |
| `src/tricoder/engine/loop.py`、`engine/finalization.py` | 只在需要时更新纯目录任务的完成判断和结果传递 |
| `protocols.py`、`presentation/console.py`、`presentation/tui.py`、`engine/telemetry.py` | 工具说明、审批标题、目录计数与脱敏日志 |

不要把全部目录实现塞进 agent.py，也不趁机重构上述模块。

### 4.2 目录账本和副作用

在 changes.py 增加独立 `DirectorySnapshot(path, mode, identity)` 与 `DirectoryChange(path, before, after)`，不要用 content="" 的 FileSnapshot 冒充目录。本轮正常前向操作仅创建目录；before=None，after 为实际身份。

- TaskChangeSet 末尾增加 `directory_changes=()`，保留旧位置参数构造；ChangeJournal 新增目录记录、补偿记录和数量预算。
- 目录创建后即刻记录实际身份；父子目录逐级登记，不能等文件创建成功才一次性记录。
- 目录操作和补偿必须推动同一个任务修订游标，异常观察通道能消费净零操作，不能重复发布旧事实。
- 推荐 FileEffects 末尾增加 `directory_paths=()`，原 paths 保持普通文件语义；CONFIRMED 允许至少一个集合非空，NONE 两者必须为空。路径校验、去重和上限必须覆盖新字段。
- ExecutionState、SessionContext 及任务结果增加兼容默认值的 modified_directories，或提供等价的显式目录变更表示；不能为了少改接口把目录混进 modified_files 并触发原文件验证逻辑。
- 所有新增可信字段仅从精确内置处理器及本地账本取得。外部扩展/MCP 自报目录效果不能绕开现有来源校验。
- 本轮目录撤销能力与文件撤销一样仅服务现有内存生命周期；不承诺进程重启后恢复撤销账本，不通过顺带改 SQLite 实现持久化。

### 4.3 创建原语与竞态约束

为目录绑定实现最小接口，例如：

```python
create_directory(name: str) -> FileIdentity
directory_identity(name: str) -> FileIdentity
remove_empty_directory(name: str, expected: FileIdentity) -> None
```

name 只接受一个路径组件；高级逻辑负责目录链和审批，平台原语负责相对已绑定父目录操作。

- 从现存且已绑定的安全祖先开始，按层创建并绑定新目录；复用现有 Windows 句柄/祖先绑定和 POSIX dir_fd 设计。
- mkdir 后取到的对象必须为普通目录；不能跟随链接、junction 或 reparse point。
- Windows 上尤其不能退化为“检查一次路径，然后对任意完整字符串 mkdir”。创建/删除过程中要维持父链绑定并验证身份。
- 创建失败且无法确认是否已经留下目录时，停止并记录 UNKNOWN；不能凭异常类型断言零副作用。
- 实施安全原语遇到平台能力不足时安全拒绝，记录准确限制；不能以静默降级换取 Windows 用例通过。
- 本轮不能把目录绑定宣称为整个子进程的 OS 沙箱。

### 4.4 失败、取消与补偿

- 对目录链和 create_file(create_parents=true)，先完成全部路径/冲突/预算预检，再审批和修改。
- 取消发生在创建前，零副作用；发生在创建中，记录已创建目录后，按深度逆序补偿。
- 只能删除本次确认创建、身份仍匹配且为空的目录，不递归删除，不删既有父目录。
- 文件已发布时先按已有身份核验与补偿规则处理文件，再处理父目录；若文件实际已提交但后处理失败，不能遗忘该提交。
- 完整补偿后报告失败且净变化已撤回，保留正确修订/失效状态；补偿不完整时准确保留剩余账本，存在无法证明的影响则 UNKNOWN / STOP_TASK。
- 用户或外部进程在新目录放入额外文件时，必须保留该文件及目录，报告冲突；不得为了“回滚完整”递归删除。
- 审批期间目录/目标变化、用户拒绝、越界、敏感路径和不确定状态继续停止；不要把它们泛化成可重规划参数错误。

### 4.5 完成判断与验证证据

这是本轮必须单独测试的集成点：

- 单纯创建空目录：本地核验目录存在、身份和任务末扫描一致后，可完成该任务；显示“目录已创建”，测试状态仍为未运行。不能要求模型为了 mkdir 专门写一个测试，也不能生成虚假的 VerificationEvidence。
- 创建/修改文件的任务：继续走原有文件验证规则。目录创建成功不能替代测试，目录变化不能清除先前失败或 UNKNOWN。
- 已有通过证据后新增目录：旧工作区证据不能继续冒充当前完整状态的证明；按当前快照绑定规则失效，但不能把目录操作当作修复了失败的代码。
- “只创建目录”是否可以完成，由实际文件/目录账本、已有待验证状态和任务末核验决定，不能仅根据最后一次工具名称判断。
- 空目录也须被工作区对账接受为本工具真实修改；不能扩大祖先目录豁免到任意目录替换。
- 最终界面分别显示新增目录与修改文件数量；不要出现“0 修改，所以没做事”或“创建目录后测试通过”的误导。

## 5. 撤销契约

本轮增加的是“撤销本任务创建的目录”，不是通用删除工具。

1. 预览同时列文件反向 diff 与待移除目录，目录按深度逆序展示。
2. 撤销前全量检查文件 after 状态、目录身份、目录中是否存在不属于本任务可撤销集合的内容；有冲突则先拒绝整组撤销，避免明知无法撤销却先删一部分。
3. 用户同意后，先撤销文件，再从最深层开始移除本任务创建的空目录；既有父目录永不移除。
4. 执行中仍核验身份和空目录条件。中途竞争或失败时按现有撤销事务要求补偿，并报告实际状态。
5. 如果补偿必须重建已移除目录，新 identity 必须进入可信恢复证据，不能假装恢复了旧 inode；沿用或扩展当前撤销后状态核验机制。
6. create_directory 连续重复调用不会把之前就存在的目录加入新任务撤销集合。
7. 单独目录任务也必须可以进入最近一次可撤销变更，不能只检查 change_set.changes 是否非空。

## 6. 审查重点

1. **权限和路径**：read_only、用户拒绝、越界/敏感目录、链接/junction、审批期换目录均不产生未授权写入。
2. **失败后实际状态**：创建第 N 层失败、文件发布失败、取消、账本/输出异常，真实新增目录不能消失于记录。
3. **撤销不误删**：额外文件、替换后的同名目录、既有祖先目录必须保留；禁止递归删除。
4. **验证语义**：目录成功不清除旧失败、不伪造测试通过，纯目录任务能正常完成。
5. **下一任务体验**：工具合法创建目录后，下一任务不应无故弹出外部修改确认；真实外部修改仍会被发现。

每一项必须有行为测试，不只检查工具注册或 mock 调用次数。

## 7. 分阶段实施

### S0：固定基线和失败场景

**读取：** AGENTS.md、../../AGENTS.md、README.md、pyproject.toml、project.md，以及第 4.1 节相关实现。

- [x] 核对工作树；在 `runtime/filesystem-tools-round1/` 记录基线、实际测试结果与涉及接口，不复制真实密钥/Session 数据。
- [x] 新建 `tests/test_directory_tools.py`：fake Provider 请求 create_directory 后创建 game/main.py，证明当前工具缺失；另测 create_parents=true 的目标行为迁移前失败。
- [x] 记录当前 `create_file` 默认缺失父目录的拒绝行为，明确后续保留该兼容用例。

### S1：目录身份、账本和受控原语

**修改：** changes.py、tools/binding.py、新 tools/directory.py；必要的纯状态类型扩展。
**测试：** tests/test_changes.py、新 tests/test_directory_tools.py。

- [x] 先补 DirectorySnapshot/DirectoryChange、净变化、幂等、目录数量预算和修订游标测试。
- [x] 实现逐层绑定创建和仅空目录删除，覆盖 Windows 实际目录行为；受平台权限限制的链接测试明确跳过原因。
- [x] 故障注入：第 2 层创建失败、创建后身份核验失败、补偿遇到新文件、目录被替换；验证已确认变化与 UNKNOWN 分类。
- [x] 不注册未完成的工具；局部原语和账本测试通过并自审后进入 S2。

### S2：create_directory 完整注册与状态闭环

**修改：** tools/directory.py、tools/__init__.py、handlers.py、execution_state.py、models.py、task_observation.py、workspace/snapshot.py、必要的 engine/session 完成判断。
**测试：** test_directory_tools.py、test_effect_state.py、test_workspace_snapshot.py、test_workspace_consistency.py、test_verification_evidence.py。

- [x] 注册内置 write 工具及 schema；覆盖 parents/exist_ok 默认值、非法类型、已存在、同名文件、层级/数量上限。
- [x] 补 strict/relaxed/fullaccess/read_only 权限矩阵和审批后身份变化测试。
- [x] 接入目录副作用、异常观察、任务末对账；证明普通扩展不能自报可信目录修改。
- [x] fake Provider 执行仅创建空目录并 finish，确认可完成、目录计数正确、没有测试通过证据。
- [x] 覆盖已有 UNKNOWN/失败/旧通过证据的状态保持与失效，不降低真实文件验证要求。

### S3：create_file 显式补齐父目录

**修改：** tools/write.py 与 S1 共用辅助逻辑。
**测试：** test_tools.py、test_directory_tools.py。

- [x] 保留旧的默认缺失父目录测试；增加 create_parents=true、false、非法类型和多层目标测试。
- [x] 联合审批包含完整目录清单和文件 diff；拒绝时不创建任何目录。
- [x] 预存同名文件、禁止路径、预算不足均在修改前拒绝；文件仍不可覆盖原子发布。
- [x] 模拟创建目录后文件发布失败、发布后账本失败和取消；按第 4.4 节核验补偿与剩余状态。
- [x] 对 apply_patch 只修改缺失父目录反馈，保留既有多文件事务规则。

### S4：撤销、用户说明和审计

**修改：** tools/undo.py、session/runtime.py、changes.py、protocols.py、presentation/console.py、presentation/tui.py、engine/telemetry.py。
**测试：** test_changes.py、test_tools.py、test_session_runtime.py、test_ui.py、test_tui.py、test_protocols.py、test_directory_tools.py。

- [x] 创建目录＋文件后撤销；只创建空目录后撤销；已有父目录保留；二次幂等创建不会被错误撤销。
- [x] 注入外部文件、身份变化、撤销中途失败，验证全量预检和逆序补偿，不误删用户内容。
- [x] 审批以字面文本展示目录清单；Console/TUI 明确目录操作，路径控制字符不能伪造界面。
- [x] native/legacy 都能看到目录工具、父目录参数和正确恢复建议；脱敏审计记录动作类别、数量和既有允许范围内的相对路径，不记录源码正文。

### S5：端到端验收和文档

**测试：** 新 test_directory_tools.py 加已有 CLI/Agent/Session 全量回归。

- [x] 使用 fake Provider、合成工作区和现有 Python：创建 game、写 main.py/test_main.py、执行真实 unittest、得到验证证据、finish、撤销整组任务。
- [x] 覆盖 create_directory→create_file、create_file(create_parents=true) 和 create_directory→apply_patch 三条路径，以及 native/legacy 和 sync/async 入口。
- [x] 纯空目录任务完成后启动下一任务，不产生虚假的外部修改告警；外部再添文件则仍要求确认。
- [x] 运行受影响测试、完整 unittest、内存语法编译、CLI --help、git diff --check，记录实际结果和跳过条件。
- [x] 更新 README.md、project.md 和本计划状态；说明默认 create_parents=false、目录撤销边界以及未验证平台，不宣称图形界面游戏已通过交互验收。

## 8. 验证命令与完成标准

新测试模块创建后，按阶段选择：

```powershell
.\.venv\Scripts\python.exe -B -m unittest tests.test_directory_tools tests.test_changes tests.test_tools -q
.\.venv\Scripts\python.exe -B -m unittest tests.test_effect_state tests.test_workspace_snapshot tests.test_workspace_consistency tests.test_verification_evidence -q
.\.venv\Scripts\python.exe -B -m unittest tests.test_protocols tests.test_ui tests.test_tui tests.test_session_runtime tests.test_cli -q
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -q
.\.venv\Scripts\python.exe -B -m tricoder --help
git diff --check
```

不安装依赖，不调用真实 Provider；避免通过 compileall 生成大量 pycache，优先内存 compile。已有测试失败需要归因，不能删测试或放宽生产安全条件求通过。

完成必须同时满足：模型可发现并实际使用目录工具；普通工程任务不再因缺少目录能力停住；目录与文件各自记录真实副作用；权限、任务末对账、撤销和验证语义全部闭环。只实现 mkdir 并返回成功不算完成。

## 9. 可交给 coding session 的提示词

```text
请在 D:\MaHong\AGENT_WORKSPACE_V2\projects\tricoder-cli 实施以下方案：
docs/superpowers/plans/2026-10-05-filesystem-tools-round1.md

先读 ../../AGENTS.md、AGENTS.md、README.md、pyproject.toml、project.md，再按 S0—S5 小步实施。本次授权修改计划范围内的源码、测试和文档，请实际完成实现。

目标是新增 create_directory，并为 create_file 增加显式 create_parents=true；旧调用默认 false，apply_patch 本轮先调用目录工具再使用。保留命令白名单，不靠放行 Shell、mkdir 命令或 python -c 绕过缺口。

必须连同目录身份、账本、副作用、审批、任务末快照对账和撤销一起实现。只创建空目录也要能完成和撤销；目录操作不产生测试通过证据，不清除已有失败或 UNKNOWN。撤销只能移除本任务创建、身份匹配且为空的目录，不能递归删除或误删用户新增文件。

每阶段先补失败回归，再最小修改、聚焦验证和自审。保留已有未提交改动和独立仓库边界；不安装依赖，不读取 .env.local、凭据或真实 Session 数据库，不调用真实 Provider，不启动 Docker，不提交或推送。

使用 fake Provider、合成工作区和现有 Python 验证目录创建、文件创建、真实测试、finish、撤销、下一任务扫描的完整流程。记录实际测试结果和平台限制，更新 README.md、project.md 及 runtime/filesystem-tools-round1/ 的执行证据。

不要混入移动、复制、通用删除、提示词注入架构或其他历史问题。若现有平台安全原语不足，明确指出具体缺口，不以降低安全边界或省略撤销来冒充完成。
```
