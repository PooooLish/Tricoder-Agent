# R4：Windows观察线程中断与资源所有权修复计划

> **For agentic workers:** 使用 `superpowers:executing-plans` 按任务实施。本任务单会话执行，不派生子 Agent。本文交给独立 coding session；本规划会话不实施代码。用户的“不提交、不推送、不合并”优先于通用技能中的提交步骤。

**Goal:** 修复观察线程启动确认或等待被中断后，错误确认清理成功、关闭 Job/端口并漏掉迟到句柄的 R4/P2。

**Architecture:** 保留现有进程成员账本，在 tracker 内补齐观察线程的显式启动状态、资源归属和可靠退出证明。线程自己发布“不会再操作 native 资源/账本”的完成确认；清理同时取得真实退出证据，不能用中断后的 `ident/is_alive/join` 推断退出。只有观察者已停止、成员完整且精确进程句柄均已退出时，才允许完成发布与句柄释放。

**Tech Stack:** 现有 Python 3.11、stdlib threading/ctypes、Windows Job/IOCP/线程句柄、unittest；不增加依赖。

**Spec:** `docs/reviews/2026-10-29-windows-cleanup-p2-independent-review.md` 第2节；独立证据 `runtime/ec29-review-p2-7c19e4ab/`。旧三项计划 `docs/superpowers/plans/2026-10-29-windows-cleanup-p2-fixes.md` 的安全、期限、成员完整性契约仍适用。

## 1. 基线、授权与范围

- 规划核查时分支 `codex/eval-metrics-delivery-guidance`，HEAD `d6d6c4c8245bdffc19cba03c37b2bfe316f7b448`。R1/R2/R3 的实现仍在未提交/未跟踪工作树，HEAD 不是 R4 的 actual before。接手时重新核对，不能 reset/clean/stash 或从 HEAD 重建覆盖工作区。
- 原 R1/R2/R3 的具体触发条件已由 reviewer 关闭；整体验收因新 R4/P2 不通过。本计划不重新打开已关闭条件，也不能删掉其回归来解决 R4。
- 保留原提交、旧交接全文、原阻塞结论及所有中间失败。保护旧 evals/mg28 的3505份索引、后续5723份保护索引、本轮实施安全证据2720份，以及 R4 reviewer 的证据索引。重叠索引不得相加虚报独立文件数。
- 新实施证据使用 `runtime/ec29-r4fix-<唯一后缀>/`；每条测试命令用全新的 `TRICODER_V3_EVIDENCE`，不得覆盖旧 reviewer 脚本输出或旧日志。
- 不读 `.env.local`、真实凭据或用户数据库；只用合成数据。不安装本机依赖，不调用真实模型，不扩大权限/预算，不修改评分/题库，不推进 Replan 或 Skills，不恢复 Python3.12 CI。
- 不提交、不推送、不自动合并。通过实施自测不等于独立复审通过；最终候选的远端 CI 需要后续明确推送授权，旧 SHA 的绿色 CI 不能覆盖新工作树。

## 2. 当前代码与确定性证据

以下行号对应规划时工作树，实施后应以函数名重新定位。

| 位置 | 当前行为与本轮责任 |
| --- | --- |
| `src/tricoder/process/windows_job.py:30-44` | tracker 已由 Job 持有，但没有区分未尝试启动、启动结果未知、已运行及可靠退出；需补线程生命周期字段/私有状态对象。 |
| `windows_job.py:63-64` | `Thread(...).start()` 的调用返回/异常被当作主要启动事实；原生线程已创建、Python启动确认尚未到达时可能中断。 |
| `windows_job.py:66-95` | `_capture` 在 native 调用后登记句柄；观察线程仍运行时，迟到句柄能进入 `extras/members`，必须持续有 owner。 |
| `windows_job.py:97-114` | `_monitor` 有异常隔离，但没有独立的最终完成确认；所有返回与异常都必须经过受控退出协议。 |
| `windows_job.py:126-135` | `_stop_monitor` 用 `ident is None` 直接成功，再用 `join/is_alive` 作退出证据；这是两个 R4 窗口的主要错误。 |
| `windows_job.py:163-198` | `cleanup` 在错误停止判断后继续证明完整性、`complete=True`、关闭资源；需对 observer 的可靠退出设置强前置条件。 |
| `src/tricoder/process/control.py:142-149, 160-181` | 工厂已经提前把 Job 放到资源对象；主异常后的自动二次清理会再次触发上述错误成功，从而不给异常附加 `cleanup_resource/cleanup_failed`。保留主异常身份及原期限。 |
| `control.py:337-349` | `_WindowsJob.close` 在 `_handle==0` 后直接成功；正确修复首先必须禁止 observer 存活时关闭 Job，不能仅在这个分支临时扫一次 extras。 |
| `src/tricoder/evals/cleanup.py`、`evals/runner.py`、`task_cleanup.py` | 已有 exact resource 保留、持久 owner 与执行阻断路径。默认不修改这些生产模块，以集成测试证明新 tracker 正确接入。 |
| `src/tricoder/evals/workspace.py:109` | 既有隔离 helper 已复制 windows_job.py。若新增生产模块，必须同步最小依赖闭包；优先不新增模块。 |

### 两个必须覆盖的窗口

**A：启动确认中断。** `Thread.start()` 已调用底层原生线程创建，但目标尚未设置 ident；主线程的启动等待被 KeyboardInterrupt 中断。当前 `_stop_monitor` 提前成功并关闭 Job/端口，而原生线程稍后仍会执行目标。

**B：join 中断。** 本机 Python3.11.6 的 `_wait_for_tstate_lock` 在异常路径可能错误释放结束锁、标记 `_is_stopped`。目标仍阻塞在真实 OpenProcess 边界时，`is_alive()` 已变成 False；自动重试关闭资源后，目标继续登记句柄。该 CPython 行为的官方记录：[issue #90882](https://github.com/python/cpython/issues/90882)。本轮依据是实际解释器源码和独立探针，不把其他补丁版本视为已测。

公开调用链收据 `reviewer-public-join-v3/public-join-interruption.json` 的关键事实：主中断身份保留；`exception_cleanup_failed=false`、异常不携带资源、scope无pending；observer未退出而Job为0/port为空/complete为真；observer退出后存在1个有效精确句柄，原资源再次cleanup仍未回收。测试自有finally关闭该句柄不算产品清理成功。

### 可复用的独立探针

位于 `runtime/ec29-review-p2-7c19e4ab/`：

- `test_startup_boundary.py`：真实线程，延后 bootstrap 并中断启动确认。
- `test_join_boundary_v2.py`：真实 Python observer + native 模型，复现中断后的失真状态。
- `test_real_join_boundary.py`：真实 Job/IOCP/进程句柄，经 `_ProcessResources.finish` 重试。
- `test_public_join_boundary.py`：真实 `run_bounded_process` 的主中断及自动重试，最重要的验收入口。
- `python-thread-lifecycle.txt`：本机解释器相关源码；相应 `.log/.json` 是旧失败事实。

不要以 `test_join_boundary.py` 首版收尾超时作为产品 RED，它是已记录的探针错误。使用上述 v2 或真实探针。新机制若不再走 Python join，要把中断门闩移到真实等待/确认边界并记录原因；不能因原注入点没有被调用就宣布 GREEN，也不能要求旧机制专用断言原样通过。

## 3. 推荐实现方向与不可破坏的契约

### 3.1 生命周期事实必须由 owner 和 worker 共同闭合

建议在现有模块内增加私有 `_ObserverLifecycle`，或同等职责的 tracker 字段；不要扩展成通用线程框架。至少需要表达这些事实（命名可调整，但必须在实现报告给出映射）：

| 状态/事实 | 可以据此推导什么 |
| --- | --- |
| 尚未跨越启动尝试边界 | 可证明没有该观察线程；例如端口/关联失败，尚未调用线程启动。 |
| 已尝试启动、结果未确认 | 即使 `ident is None`、start抛异常，也可能有线程将来运行；保持 owner/pending。 |
| worker 已进入并登记精确线程身份 | 线程存在，native句柄、事件及后续采集均归当前tracker；不能清空启动未知后立即关闭资源。 |
| worker 发布 quiescent/done | 发布后不再调用native API、不再写成员表或extras；这是停止操作的确认，不自动等于OS线程已退出。 |
| 精确线程退出确认 | 该真实线程对象已退出；结合静止确认和成员证明后才能完成资源释放。 |
| 句柄关闭未确认 | 仍保留对应句柄；下一次沿用原期限重试，不能清空状态。 |

关键顺序：

1. Job持有tracker，tracker持有线程生命周期对象；**先登记“启动可能发生”及owner，再调用start**。
2. worker进入后先登记自身可等待的精确线程身份，再发布 ready；用户root不能因“start调用返回了”就跳过新增观察设施的初始化失败检查。
3. worker所有退出路径进入finally，先完成最后的native操作、句柄登记及账本更新，再发布静止确认。该确认后不可再产生上述副作用；发布后的包装器、回调或异常处理也必须满足这一点。
4. 停止请求根据明确生命周期发送，不能以 `worker.is_alive()==False` 省略停止消息。已发送、发送失败、worker已可靠退出分开处理；Post失败不能假装成功或靠关闭port强制清理正在运行的worker。
5. 清理在原剩余期限内取得静止及真实退出证据，然后重新检查最后加入的所有精确进程句柄和累计完整性；之后才允许 `complete=True` 和关闭进程/候选/port/Job句柄。
6. 任何一步未确认则返回False/保留资源；如果主线程再被中断，仍传播原异常，状态只能保守保留，不能在finally设置假完成。

### 3.2 退出证明：推荐精确 Windows 线程句柄，先做小范围验证

保留 Python Thread 执行 Python 目标函数，但不要把它的中断后状态作为唯一退出证明。推荐在 **worker自身** 用 `GetCurrentThread` 的伪句柄，经 `DuplicateHandle` 转成不可继承、可等待的真实线程句柄，立即登记到tracker；清理用 `WaitForSingleObject` 和原剩余期限确认退出，成功后 CloseHandle，关闭失败继续持有。

Microsoft 文档明确说明 DuplicateHandle 可把当前线程伪句柄转成真实句柄；线程对象在退出后变为 signaled：[DuplicateHandle](https://learn.microsoft.com/en-us/windows/win32/api/handleapi/nf-handleapi-duplicatehandle)、[Thread termination](https://learn.microsoft.com/en-us/windows/win32/procthread/terminating-a-thread)。这是候选机制的依据，不是“已证明完整实现正确”的背书。

实施要求：

- 配齐 ctypes argtypes/restype，使用正确 HANDLE 宽度；申请必要等待权限，不设置可继承，不误用 `DUPLICATE_CLOSE_SOURCE`，不关闭伪句柄，不重复关闭借用的root Popen句柄。
- 不能在主线程调用 GetCurrentThread 然后等待自己的句柄；不能等线程结束后再按可能复用的TID OpenThread补证据。
- 获取句柄失败、准备阶段异常、真实 Wait 的 TIMEOUT/FAILED、Close失败均须有负例。没有可靠线程身份时不能把done事件当作完整退出证明，必须保留未知，或提供另一种同样可靠的证明。
- 若选择其他机制，先在独占证据根证明两个中断窗口、资源不再产生及真实退出；写清依据，再集成。不要用未验证的CreateThread/ctypes回调方案替换现有线程模型，不增加强杀线程或异步注入异常的生产逻辑。
- **仅添加done Event的方案不足**：done在函数返回前发布，外层finally/包装器可能仍运行。至少必须证明无后续资源副作用，并满足真实退出契约；不能悄悄把“线程已退出”降格成“设置了标志”。

### 3.3 期限与接口

保持公开 `run_bounded_process(...)` 的参数和结果契约、`_stop_monitor(self, deadline: float) -> bool`、`cleanup(self, handle, deadline: float) -> bool`。私有启动接线可以按需要扩展，例如 `start(self, *, deadline: float, cancellation=None) -> None`，但所有调用点/fixture都需一致。

- 新增ready/身份/静止等待必须有界。启动等待如需deadline，由control从现有命令执行预算推导并传入，后续收集只消费剩余执行时间；不能新增配置预算或在每次检查时重新 `now + timeout`。
- 正常启动握手不得调用任务 `begin_termination`，避免复活“搜索超时后等待模型5.2秒导致下一普通命令失败”的旧P2。取消、未确认清理及已有终止期限继续按原规则终止/收紧。
- 若新增握手响应CancellationToken，检查 `control.py` 工厂中包围 `tracker.start()` 的 `except Exception: return job`：不得吞掉取消并让外层改抛普通OSError/UNKNOWN。取消应携带已登记owner按原边界传播，父令牌取消同样适用；KeyboardInterrupt及二次清理中断的主异常身份保持。
- 清理重试只接受原资源期限或更早期限。所有Event/原生等待/可选join共享剩余时间；超时后可做零等待确认，不能加sleep、宽限期、无限join、忙等或反复重发预算。
- 不在锁内做可能阻塞的native调用/等待；worker仍可能取得句柄时不能释放其owner。同步native调用和Thread.start内部等待的不可抢占边界如实报告，不通过另建无owner后台线程“实现超时”。

### 3.4 启动前失败与未知启动不能混淆

现有 `test_observer_startup_failures_keep_exact_job_until_cleanup` 的 `thread` 子场景把整个 `Thread.start` mock成 RuntimeError，并期待立即清理成功。测试作者知道这个fake没启动线程，**生产代码不能仅凭同样异常类型推导该事实**。

必须拆开：

- 可证明从未尝试启动（例如构造/准备在调用start之前失败）：可以清理已取得的port/Job，并保留原关闭断言。
- 已跨过启动尝试边界，随后Exception或BaseException但结果不明：保持pending，直到得到可靠退出证据。即便最终证明没有启动，也必须有可信的机制依据；不能识别mock对象或靠异常文本走捷径。
- 若重构此旧fixture，保留端口、关联、明确启动前失败的原成功/关闭次数语义，并新增启动未知的保守断言；记录适配原因，不把“启动未知立即清理成功”保留为错误的兼容要求。这属于纠正fixture表达，不允许删除测试类别或以skip消除冲突。

## 4. 文件范围

| 文件 | 允许工作 |
| --- | --- |
| `src/tricoder/process/windows_job.py` | 主要修复：生命周期、精确线程句柄、完成握手、停止/退出与关闭前置条件。优先在此文件内收敛。 |
| `src/tricoder/process/control.py` | 仅必要的启动预算/取消/ready和防御性一致性接线；不重写通用进程执行、异常映射或Popen行为。 |
| `tests/test_windows_cleanup_boundaries.py` | 加生命周期/中断/native失败/关闭重试方法，明确适配旧启动fixture。 |
| `tests/windows_cleanup_boundary_probe.py` | 加真实R4场景，或新增职责单一的 `tests/windows_observer_lifecycle_probe.py`；不要把runtime旧日志作为自动测试依赖。 |
| `tests/test_eval_cleanup_ownership.py` | 从真实进程资源到Eval持久owner的取消/中断、重试及下一verifier/case阻断。 |
| `src/tricoder/evals/workspace.py` | 仅当确实新增模块时更新隔离复制闭包；保留现有安全检查。 |
| project/handoff、新实施报告 | 追加本轮决策、全部测试和限制；保留历史。 |

默认不修改TaskCleanup、Agent、Session、评分、题库、权限、其他线程框架。发现必须跨范围才能正确实现时，先记录具体调用链和理由；不能以一般性重构扩大本任务。

## 5. 实施任务

### Task 1：保全与能说明问题的RED

- [ ] 读取项目规则、README/pyproject、最新project/handoff、本计划、R4独立报告及旧三项计划。记录实际分支、HEAD、main和完整dirty状态，不覆盖工作树。
- [ ] 建立actual before，包含新增未跟踪生产文件/测试/文档；保存安全hash、旧证据保护索引和新证据根。不要读取metadata-only数据库/缓存内容。
- [ ] 把上述四个有效reviewer探针复制/适配到新证据或正式测试。新label和证据目录必须独占；旧脚本/日志不改。
- [ ] 至少把以下断言写成正式测试：

```python
# 启动结果未知，或中断后Python线程状态失真，但观察者仍可能登记资源。
assert scope.has_pending
assert scope.failed
assert error.cleanup_failed
assert error.cleanup_resource is exact_resources
assert job_handle_is_retained
assert not tracker.complete
assert primary_exception_is_preserved
```

- [ ] 在当前候选上取得RED，失败应是清理误确认/owner丢失，不能是ImportError、注入点未触发或探针收尾超时。另保存真实进程探针的触发收据；测试自有收尾必须有界并确认完成，不能掩盖首次产品结果。

### Task 2：启动归属与可靠退出证明

**接口**：原tracker和Job持有关系不变；`_stop_monitor(deadline)`只在已证明无观察线程，或该精确线程已停止并退出时返回True。

- [ ] 添加 `test_start_attempt_interrupted_before_python_ack_stays_pending`：真实原生线程已创建、ident尚为空，中断后保留owner；释放门闩后可按原期限处理最终退出。
- [ ] 添加 `test_interrupted_thread_state_cannot_certify_observer_exit`：Python is_alive失真时，精确退出证据未到达不能关闭port/Job。
- [ ] 添加 `test_observer_done_before_native_exit_is_not_completion`：故意在done发布后、线程真正退出前保持门闩，清理必须保持未确认。
- [ ] 在新证据根验证线程精确句柄获取、等待、关闭及失败所有权；正常线程必须能成功停止，不得所有路径永久pending。
- [ ] 实现生命周期和ready/静止/退出协议，确保获取句柄后先登记再处理其他失败；启动失败不能恢复用户root。
- [ ] 添加 `test_cancel_during_observer_ready_keeps_cancellation_mapping`：本令牌/父令牌在新ready等待中取消，root不执行，工厂不能把取消吞成普通初始化错误；已取得的线程/port/Job资源清理成功或仍由exact owner持有。
- [ ] 覆盖启动前明确失败、启动后抛Exception/BaseException、身份获取失败、Wait TIMEOUT/FAILED、线程句柄Close失败、port/Job Close失败；运行新测试GREEN。
- [ ] 若机制不再使用Python join，移动注入到新的真实等待边界，并增加“Python状态被污染也不影响native证明”的测试；保留主异常与资源断言，不要求固定旧内部调用发生。

### Task 3：阻断迟到资源与贯通持久owner

**接口**：继续由 `_ProcessResources.finish`、异常cleanup_resource、Eval retain/retry交接；不把资源放进可序列化结果。

- [ ] 添加 `test_public_interrupt_keeps_owner_until_late_capture_and_exit`：直接执行run_bounded_process；真实OpenProcess取得句柄后阻塞、主清理中断、自动重试。门闩释放前必须pending，不能返回清理成功或关闭Job/port。
- [ ] 门闩释放后让worker完成最后登记并退出，再沿用原期限重试。核对成员、重复/未知extras、port、observer线程句柄、Job关闭次数；最终没有遗漏或重复关闭。即使后续物理清理成功，原failed/失败验证事实不能改判通过。
- [ ] 添加 `test_pending_observer_blocks_next_verifier_and_case`：至少覆盖已有环境scope及无环境scope的显式持久owner；丢弃异常/结果后仍被owner持有，下一verifier和case executor调用次数为0。新的host owner不能掩盖旧环境pending；被阻断case仍有原阶段耗时结构。
- [ ] 添加 `test_second_interrupt_keeps_primary_and_consumed_deadline`：主中断和清理中的二次中断对象不同，最终保留主中断身份，native/owner仍在；全部重试期限不晚于原期限，资源对象不进入JSON/诊断。
- [ ] 检查 `_WindowsJob.close` 的已关闭分支：生产顺序必须保证没有活observer/待登记句柄。若增加防御检查，应拒绝不一致状态，不能只扫一次extras或把未知状态改成成功。

### Task 4：回归、冻结及可复审交付

- [ ] 正式测试覆盖真实startup确认中断、等待中断、公开调用链和不受中断的对照；真实句柄等待不可mock为成功。日志明确区分中断注入与自然Ctrl+C频率（后者不作已测声明）。
- [ ] 重跑R1 query/terminate各10轮、R2三类捕获失败→空列表/owner路径、R3本/父token×snapshot/OpenThread四组真实取消；保持立即删除断言及精确退出确认。
- [ ] 重跑冷启动、输出超限10轮、普通/快速退出/孙进程、原搜索至少5.2秒恢复、默认普通命令、已有期限、Session owner；不改原评分、权限和预算。
- [ ] 运行聚焦、最终冻结后的全量和compileall；新代码/辅助脚本变动后，旧全量不能覆盖最终字节。记录所有失败/error/skip及退出码；skip不算通过。
- [ ] 生成actual after、含新增文件的delta及hash、完整命令和结果、native逐轮收据、原证据保护核对。旧日志不覆盖，包括无效中间探针。
- [ ] 更新project/handoff并写新实施报告，状态为“R4实现完成，待独立复审”；不自行关闭reviewer发现、不提交推送合并。

## 6. 验收矩阵与命令

| 输入/场景 | 必须满足 |
| --- | --- |
| 原生线程已创建、启动确认中断 | 无ident不能代表无线程；Job/port/生命周期有owner，root不执行，原异常保留。 |
| join/原生等待中断、Python状态失真 | 在真实退出前pending；自动二次清理不能发布complete/释放资源。 |
| done已到达但线程尚未退出 | 不得完成；后续包装器/回调不能在成功后再登记资源。 |
| native获取句柄后尚未登记 | owner覆盖这一在途工作；Job/port不先关闭；最后取得的句柄也被回收。 |
| 超期、二次中断、Wait/Close失败 | 不续时、不丢句柄、不覆盖主异常；有必要的固定诊断，不泄露正文/环境/路径。 |
| worker确实停止、完整性及逐进程等待均通过 | 正常成功，所有新增句柄恰当关闭；不能靠永久pending掩盖问题。 |
| Eval返回值/异常及持久owner | 未确认状态阻断下一执行；失败评分/failed黏着；资源不序列化，原时序结构保持。 |
| 既有R1/R2/R3及搜索P2 | 原成功和负例全部保留，搜索模型等待5.2秒不污染下一命令清理期限。 |

使用已有 `.venv/Scripts/python.exe -X utf8 -B`，`PYTHONPATH=src;tests`、`PYTHONDONTWRITEBYTECODE=1`、`PYTHONUTF8=1`。以下是交给实施方的新运行要求，不是本规划会话的已执行结果。每条都经新runner保存完整日志/实际exit，指定新TRICODER_V3_EVIDENCE；compileall另设独占缓存目录。

```powershell
.venv/Scripts/python.exe -X utf8 -B -m unittest test_windows_cleanup_boundaries test_eval_cleanup_ownership -v
.venv/Scripts/python.exe -X utf8 -B -m unittest test_windows_cleanup_boundaries test_eval_cleanup_ownership test_subprocess_control test_search_timeout_recovery test_session_ownership test_eval_runner test_eval_timing test_eval_quality_v3_review_fixes test_agent_async test_mcp_transport test_tui -v
.venv/Scripts/python.exe -X utf8 -B -m unittest discover -s tests -v
.venv/Scripts/python.exe -X utf8 -B -m compileall -q src tests
```

最后一次reviewer基线为聚焦207通过/0skip；全量2299项、2284通过/15skip；compileall通过，但runtime四项有效R4探针失败。新增正式方法会改变数量，以实际发现/运行数为准，不得降低旧覆盖或把真实重复轮次累计成额外unittest方法。

## 7. Review Focus 与禁止的替代方案

1. 启动调用抛异常但原生线程仍会迟到运行——Task2必须保留状态未知，不能凭异常类猜“没启动”。
2. 完成事件已置位但外层finally/目标尾部仍在运行——Task2必须取得真实退出证据。
3. 清理主线程被第二次中断，native句柄却已取得——Task3必须保留主异常及exact owner。
4. Python状态失真导致停止消息未发，worker继续等IOCP——Task2/3必须依生命周期停止而非is_alive分支。
5. 原期限已过，随后worker结束且最后句柄才登记——Task3允许零等待确认/回收，不能重新发5秒预算或无依据清failed。

禁止：忽略KeyboardInterrupt；修改CPython私有锁/标志作为生产修复；强杀线程；sleep/宽限期；全局禁用取消；扩大默认权限；删断言/skip；仅调整 `_handle==0` 分支扫extras；只设置done而不证明退出；把原已通过全量当新实现验收。

本计划的推荐原生句柄方案仍需按Task2实测和自审，不是已完成修复。历史CI40721403072两次失败的精确根因仍未知；未来新CI不能追溯证明旧因果。即使本轮实现/复审通过，也需最终候选对应的CI及后续明确合并指令。
