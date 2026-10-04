# 工作区锁与任务前快照确认 Implementation Plan

> 执行会话使用 superpowers:executing-plans 逐项实施。未经授权不提交 Git、不安装依赖。

**Goal：** 防止两个 TriCoder 会话同时操作同一工作区，并在代码发生变化时先展示差异、取得用户确认，再执行任务。
**Architecture：** 工作区任务锁 → 完整快照 → 与本 Session 基线比较 → 无变化放行 / 有变化确认并复查 / 扫描失败阻止 → 执行与清理 → 更新可信基线 → 释放工作区锁。
**Tech Stack：** Python 3.11+、现有 SessionRuntime、标准库文件锁与 difflib、现有 CLI/Shell/TUI、unittest。
**Spec：** 本文第 1—5 节。依据用户 2026-10-04 已确认的三分支规则。
**状态：** W1—W6 已于 2026-10-04 实施；实际接口按现有代码小幅调整，安全边界未放宽。
实现与验证证据见 `runtime/workspace-consistency-implementation/verification.md`，长期交接见
`project.md`。本文后续的未勾选清单保留为原始验收规格，不代表当前实现状态。

## 1. 用户已经确定的规则

| 扫描结果 | 强制行为 |
| --- | --- |
| 快照完整且无变化 | 不增加确认，正常执行任务 |
| 快照完整且有变化 | 展示新增、修改、删除差异；用户明确同意后再执行；拒绝或取消则不执行 |
| 扫描失败、超限、不完整或不能确认稳定性 | 阻止任务，显示原因；只能修复问题后重试，不能选择忽略并继续 |

确认期间保持工作区锁。用户同意后再完整扫描；变化则旧确认失效，重新展示；扫描失败则阻止任务。
fullaccess、relaxed、命令自动审批、无交互入口均不得自动跳过此确认。没有确认能力且需要确认时返回明确错误。
用户确认只是接受当前代码作为任务起点，不批准后续写入、命令或其他会话的改动，也不代表旧记忆仍正确。

首版不做 worktree、Docker、文件监控服务、跨会话记忆共享、逐条记忆文件绑定或完整源码持久化。

## 2. 已核对的代码入口

- session_lock.py：现有文件锁按“数据库规范路径 + Session ID”区分，不能充当工作区锁。
- session_runtime.py：run_task、_run_task_locked、_ensure_session_for_task_locked、_finish_task_ownership、prepare_undo、关闭/清理流程。
- cli.py：单次任务直接构建工具并运行 Agent，不可只改 Runtime。
- evals/service.py：存在多条工具装配路径；评测副本也需要一致的锁/门禁或显式测试适配。
- verification.py：已有有界工作区快照，但 scope_id 与本地验证能力绑定，不应改成跨 Session 的共享验证授权。
- tools/write.py、tools/undo.py：已有审批后、提交前冲突检查，仍须保留。
- context/memory.py：语义记忆描述目标、约束等，不能让模型生成工作区信任状态。
- tests/test_session_ownership.py、test_session_landing.py、test_verification_evidence.py 是重要回归入口。
- 当前入口延迟创建 Session；空闲落地页不占锁、不创建 Session。新增工作区机制不能破坏这个行为。

实施前重新核对 README、project.md、pyproject.toml 和 git status；保留现有用户修改。这里是代码结构观察，不是已复现并发测试的结论。

## 3. 锁与快照的精确定义

### 3.1 工作区锁

新增 WorkspaceLock，与 SessionLock 各负其责。
- SessionLock 保证同一会话只有一个所有者；WorkspaceLock 保证同一工作目录同时只有一个活动任务。
- 工作区身份使用严格解析后的目录规范路径，并核验根目录文件身份；处理 Windows 大小写及已允许的路径别名。无法证明身份一致时拒绝，不建立可能冲突的第二套锁。
- 锁的位置不能依赖 Session ID、会话数据库路径、当前目录或用户可随意指定的每次运行目录。
- 首版建议固定为工作区内 runtime/tricoder-control/workspace.lock；所有数据库的会话都访问同一位置。该精确控制目录不进入快照且模型工具不可访问，防止锁文件/快照元数据成为代码差异或被工具删除。目录/文件需拒绝链接和 reparse 重定向。
- 父子工作区重叠不是“同一规范路径”；首版不承诺处理此类并发。README 必须明确同一项目各会话需选择同一工作区根，不得声称覆盖任意重叠目录。
- 操作系统锁非阻塞获取；占用时立即报告“工作区正在执行其他任务”，不自动重试、不抢占、不无限等候。
- 锁文件不随解锁删除，不用 PID 或过期时间强制夺锁。
- 锁文件必须有可写权限；若只读文件系统不能建立锁，明确拒绝任务，不降级为无锁执行。
- 这是同机协作锁，不防编辑器、不防旧版进程或恶意绕过，不作为安全沙箱。

持有期包括：扫描、确认、模型请求、工具执行、验证、结束扫描和受管资源清理。空闲会话不持有工作区锁。
并发顺序统一为 Runtime 任务互斥 → 已有 Session 所有权（存在时）→ 工作区锁；新 Session 在拿到工作区锁并完成初次扫描后才创建。所有锁均非阻塞，禁止反向等待。
清理不确定时保留工作区资源所有权，禁止另一任务接手；关闭和取消不得提前释放。进程崩溃时 OS 会释放文件锁，但不能据此推断子进程已经退出，见第 5 节。

### 3.2 首版快照与基线

新增独立 WorkspaceBaseline，包含规范工作区身份、范围版本、快照 ID、完整标记、相对路径清单及每文件内容摘要/身份；支持有界 UTF-8 文本旧内容用于 diff。
复用 verification.py 的安全扫描机制或提取小型共享扫描器，但保留验证能力 token 的原语义。
- 必须覆盖未提交修改、新增、删除、改名（首版按删除+新增显示）与文件类型变化。
- 不能只看 HEAD、mtime 或大小；文本/普通二进制内容使用哈希。目录/文件前后身份和清单稳定性需复查。
- 明确排除敏感路径、.git、.venv、固定缓存和程序自有控制/审计文件。不要按 .gitignore 跳过全部未跟踪源码，也不要仅因为目录名为 runtime 就跳过用户的运行时代码目录。
- 首版建议硬上限：5000 个普通文件、单文件 2 MiB、总读取 64 MiB、文本旧内容总量 16 MiB、扫描时间 10 秒、目录深度 32。任何上限使覆盖不完整时阻止任务；不能只扫描前 N 个文件。
- 普通二进制记录哈希/大小/类型，不存正文；超大小限制仍阻止任务。符号链接、junction、reparse、特殊文件拒绝扫描，不跟随到工作区外。
- 失败原因使用安全枚举：permission_denied、limit_exceeded、unstable、unsupported_entry、cancelled、io_error；不直接输出可能含敏感信息的异常原文。
- 敏感文件在打开前排除；不读取 .env.local、.local/secrets 等，也不为检查过滤规则访问真实秘密。内容过滤命中可能凭据时不缓存/展示原文，不用该内容做演示。

每个 Session 单独持有基线。B 完成任务只能更新 B 的基线，不能让 A 自动接收 B 的新基线。
首版旧内容仅驻内存，不写 SQLite、日志或长期源码缓存；关闭、切换释放 Session 时清理。单次 CLI 为新任务建立临时基线。
新 Session 完整初扫后建立初始基线，不谎称已经与历史版本比较过。
恢复的旧 Session 没有基线时：完整扫描成功后展示“没有旧快照，无法比较历史修改”，要求明确确认初始化当前基线，并使旧验证与代码观察失效；拒绝则不执行。
已有基线扫描失败时禁止走“初始化基线”快捷路径。基线范围版本不兼容按历史基线不可用处理，不能判为无变化。
如未来要跨重启展示完整 diff，需另立源码快照持久化方案和隐私/容量设计，本期不暗中增加。

### 3.3 差异与用户确认

文本显示标准 unified diff，新增/删除显示对应全文；二进制或无可安全展示正文的文件显示相对路径、类型、前后大小和变化事实，不伪造文本差异。
完整文件清单不可静默截断。长文本差异可分页/展开；明确告诉用户还有未展示部分。首版无法完整呈现应拒绝放行，不把截断视为已确认。
终端控制字符和恶意文件名须安全转义；diff 来自不可信代码内容，不得作为高权限模型指令。
确认对象绑定 Session ID（或新任务候选标识）、工作区身份、基线 ID、候选快照 ID、任务请求 ID 与生命周期代次。
布尔“同意”必须返回给对应预览，过期回调、会话关闭、任务取消和预览失效均不得复用批准。
同意后重新扫描必须与候选在同一完整范围中一致，否则重新确认；不允许先调用 Provider 再补确认。
拒绝/取消保持旧基线，不写入待执行用户消息，不调用 Provider、不产生工具效果；新 Session 在初扫失败/工作区占用时不创建。
扫描本身和新的同步检查支持取消；TUI 不在 UI 线程中阻塞扫描。

## 4. 对旧记忆与验证的最小处理

检测到变化，立即使旧验证证据失效；即使用户拒绝，本 Session 也不能继续展示该旧代码版本“测试通过”。
用户同意后，在真实上下文装配中加入本地生成的提示：工作区已变化，列出安全路径和变化类型，历史源码及实现状态需要重新读取核实，用户目标/约束仍保留。
保留原始对话，不拆坏 tool call/result 配对。旧工具源码输出在模型视图中需标识为历史观察，不能仅修改摘要却把旧源码当当前事实；不把差异原文放进系统指令。
不自动清空或改写已审批保存的语义记忆，不自动 /memory refresh，更不能自动把“决定用 YAML”改为“决定用 JSON”。
本期不承诺强制模型逐个重读所有变更文件；能保证的是：明确提示、旧证明失效、原有写入前冲突检查继续生效。实现需测试模型实际收到这一状态。
接受变化不清空已有 unknown_effects，不解除撤销冲突，不恢复旧审批能力。

## 5. 任务收尾与非协作修改

任务结束且受管资源已清理后再次扫描：
- 完整且可确认是当前任务观察/产生的文件版本，更新本 Session 基线。
- 出现无法解释的变化（例如结束前编辑器修改未触及的文件），不能静默吞进新基线。保留上次接受版本及“变化待确认”状态，下次任务走 diff 确认。
- 扫描失败，保留旧基线并标记 baseline_unresolved；任务报告应区分业务结果与收尾失败，不声称当前代码已验证。下一任务仍须完整重扫和门禁。
- 取消/失败但产生部分效果时，同样不把所有结束状态自动认为已接受；保留待确认差异。读写来源不明时按未知处理。
- 不能仅凭“task 成功”就把结束快照全部视为本任务写入。可结合现有 ChangeJournal 和命令前后受控快照；无法证明归属的命令副作用保留未知状态。
- 结束扫描不稳定不能自动无限重扫。

门禁通过、即将开始实际执行时，在控制目录建立最小活动标记（不含任务正文），正常清理完成才移除/标记完成。仅等待确认或扫描失败未开始执行时不制造残留执行标记。另一进程取得 OS 锁但发现遗留标记时，阻止任务并报告需确认旧资源清理，不能只清标记继续。
首版不实现自动抢救残留子进程或强制解锁命令；复用已有 pending-cleanup 能力，提供手工恢复步骤，明确未知时保持阻塞。
如果扫描收尾失败但已确认没有活动进程，可以释放 OS 锁；不完整基线状态仍保留，不阻塞其他会话通过自身完整检查。
任务前后扫描不保证发现“任务中改动后又改回”的所有过程；不将其描述为文件系统事务隔离。

## 6. 实施任务

文件位置可按最新代码微调，职责和验收不变。每项先写失败测试、运行确认失败，再最小实现、跑专项和自审。证据放 runtime/workspace-consistency-implementation/。

### W1 工作区互斥与资源生命周期

涉及新 workspace_lock.py、session_runtime.py、task_cleanup.py 及新 test_workspace_lock.py。
- [ ] 实现目录身份和固定锁路径，保护控制目录不受内置工具读写影响。
- [ ] 真实子进程测试：不同 Session/不同 DB 同根竞争失败；不同工作区同时成功；同进程两个实例仍互斥。
- [ ] 覆盖 Windows 大小写路径、目录替换、链接拒绝、占用失败和锁文件保留。
- [ ] 接入 run_task 全生命周期；占用时 Provider/工具调用数为 0，空闲落地页无占锁和 Session 行。
- [ ] 测试取消、异常、清理未确认不提前释放；强制退出后 OS 锁可再取得，但遗留标记使业务执行被阻止。

接口建议：
```python
WorkspaceLock.acquire(root: Path) -> WorkspaceLock
WorkspaceLock.close() -> None  # 幂等；只释放资源，不删锁文件
```

### W2 有界快照和差异对象

涉及新 workspace_snapshot.py、verification.py 的必要共享逻辑、新 test_workspace_snapshot.py。
- [ ] 定义不可变 WorkspaceBaseline、FileSnapshotEntry、WorkspaceChangePreview，字段满足第 3 节。
- [ ] 合成项目测试同大小同 mtime 内容变化、新增/删除、改名、空文件、二进制和大小写冲突。
- [ ] 测试权限错误、超限、扫描期间修改、根目录替换、特殊文件；任何 incomplete 均不得返回 unchanged。
- [ ] 检查敏感路径在读取前排除、旧内容不落盘、控制/审计文件不引发无穷差异。
- [ ] diff 分页与安全转义测试，确认覆盖的候选 ID 与显示对象一致。

接口建议：
```python
capture_workspace_baseline(root: Path, limits: SnapshotLimits) -> WorkspaceBaseline
compare_baselines(before: WorkspaceBaseline, after: WorkspaceBaseline) -> WorkspaceChangePreview
# capture 失败抛固定异常；compare 拒绝不同身份、不同范围及不完整对象。
```

### W3 任务开始门禁和精确确认

涉及新 workspace_gate.py、session_runtime.py、models.py 必要状态、新 test_workspace_gate.py。
- [ ] 实现三分支：unchanged 执行；changed 等确认；扫描失败阻止。
- [ ] 测试拒绝、取消、无确认器、fullaccess、旧预览重放时 Provider/工具调用数均为 0。
- [ ] 同意后复查：期间变更→重新确认；期间扫描失败→阻止；未变→仅执行一次。
- [ ] 恢复 Session 缺基线需确认；新 Session 完整初扫正常建立；已有基线不能用缺基线分支绕过扫描失败。
- [ ] 保留延迟 Session 创建行为和现有 Session 锁；阻塞/取消时不写待执行消息。

门禁判定手算用例：
```text
A 基线：f.py = "one"
B 修改为 "two" 并结束
A 提交任务 → 显示 one→two → 用户拒绝 → 执行次数 0、A 基线仍 one
A 重试并同意，确认期间改成 "three" → 旧确认无效，必须显示新差异
第二次确认后复查仍 three → 执行次数 1
任意扫描返回 incomplete → 执行次数 0
```

### W4 记忆标记、验证失效和结束基线

涉及 session_runtime.py、context/manager.py、agent.py 必要装配点、verification.py、新 test_workspace_consistency.py。
- [ ] 验证变化后旧通过状态立即撤销，接受后 Agent 收到安全变更提示，旧用户约束仍在。
- [ ] 验证旧 tool call/result 完整，历史观察不会被包装为当前代码事实。
- [ ] A/B 会话交替操作测试：B 更新自己的基线不会使 A 自动看不到变化。
- [ ] 任务成功、失败、取消、结尾扫描失败、结尾不明外部写入分别覆盖；未知变化不得自动被基线吸收。
- [ ] 切换/关闭清理内存旧内容；恢复旧会话走无基线确认，不从语义摘要重建可信代码快照。

### W5 CLI Shell TUI 撤销与 Eval 接入

涉及 cli.py、shell.py、tui.py、ui.py、evals/service.py、tools/undo.py 的相关入口。
- [ ] 单次 CLI、Shell、TUI 共用同一 gate；TUI 异步确认可取消、关闭后迟到结果不可执行。
- [ ] 撤销提交也取得工作区锁并走快照门禁；准备预览与提交分离时绑定版本，不能拿旧预览直接提交；原 Undo 冲突检查不变。
- [ ] memory refresh 不得被当作重新读取代码或清除 workspace stale 状态。
- [ ] Eval 对每个独立副本遵守互斥，使用显式注入的测试确认器；不在生产 fullaccess 增加自动放行。
- [ ] 扫描失败显示原因与重试方法；长 diff 分页，未审阅完整差异不能确认；二进制以类型/大小清单确认。
- [ ] 对 shell/TUI/单次 CLI/只读任务/Eval 形成入口覆盖测试；只读任务同样需要可信视图，不绕开门禁。

### W6 回归和交付

- [ ] 连续流程：A 首次建基线→B 修改→A 拒绝→A 同意期间再变化→重新确认→执行→A 自身修改后下轮无外部差异。
- [ ] 注入扫描异常、取消和进程清理异常，验证失败时没有模型/工具调用及锁泄漏。
- [ ] README 说明共同根目录、协作锁边界、内容快照只驻内存、恢复需确认、扫描上限、非文本 diff 和异常恢复。
- [ ] project.md 更新进度、证据、下一步；不写未经验证的效果结论。
- [ ] 执行专项、完整回归、只读语法检查和 git diff --check。

建议验证：
```powershell
& ./.venv/Scripts/python.exe -B -m unittest discover -s tests -p 'test_workspace*.py' -v
& ./.venv/Scripts/python.exe -B -m unittest discover -s tests -p 'test_session*.py' -v
& ./.venv/Scripts/python.exe -B -m unittest discover -s tests -p 'test_verification*.py' -v
& ./.venv/Scripts/python.exe -B -m unittest discover -s tests -p 'test_eval*.py' -v
& ./.venv/Scripts/python.exe -B -m unittest discover -s tests -v
git diff --check
```

## 7. 自审与交付边界

重点检查：不同数据库是否锁同一位置；确认后是否再扫描；失败是否被当作无变化；其他会话是否覆盖本 Session 基线；取消/close 是否提前解锁；fullaccess 是否越过确认；旧验证是否仍展示通过；结尾外部变化是否被静默吞掉。

测试用临时目录、合成代码、假 Provider、临时 SQLite；不得读取真实秘密/用户会话库、调用真实模型、安装依赖、恢复 Docker或自动提交推送。POSIX 无法实测时明确列为未验证，不能用 Windows 通过替代。

完工必须分别说明：W1—W6 完成度、测试数量与跳过、锁与快照覆盖范围、内容保存位置和生命周期、恢复步骤、未解决风险。
本方案没有新增自动覆盖代码的能力；外部改动确认不是回滚，不自动恢复旧快照。不通过删除锁文件、重置基线或清除 unknown_effects 来“解决”冲突。
