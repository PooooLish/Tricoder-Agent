# 2026-10-29 Windows清理三项P2独立复审

结论：**不通过，暂不合并**。原 R1/R2/R3 的具体触发条件已修复、可以关闭；本轮新增 **R4/P2：观察线程启动/等待被中断后被错误判为已退出**，仍破坏资源所有权及清理成功契约。关闭旧发现不代表整体清理实现通过。

这是单会话独立复审，没有使用子 Agent；没有修改生产代码或项目测试、安装依赖、读取真实凭据/用户库、调用真实模型、提交、推送或合并。测试使用现有 Windows Python 3.11.6 和隔离合成数据。

## 1. 对象、基线与证据

- 分支 `codex/eval-metrics-delivery-guidance`，HEAD `d6d6c4c8245bdffc19cba03c37b2bfe316f7b448`；本轮候选是未提交工作树，不是该 SHA 对应的旧 CI 产物。核查时本地 main 为 `a81ec2457ece970756b00424393e5f2df9253858`；本轮没有重新查询远端 main。
- 已读工作区/项目 AGENTS、README、pyproject、project/handoff、原三项修复计划及本轮实施报告。实际 before 使用 `runtime/ec29-p2fix-5e0b4ed2/before/`，未用 HEAD 代替。
- 新独立证据根：`runtime/ec29-review-p2-7c19e4ab/`。每个测试命令使用独立的 `<label>-v3` 目录，编译缓存单独保存在 `compile-cache/`。旧记录完整保留。
- 264 份最终源码/测试/辅助脚本与 `candidate-frozen`、源码索引全部一致，树摘要 `50f5890b77e607d468f94c5b31cbf491b08335235e5b706987edf1f53e979a8f`。本轮验证结束后再次核对未漂移。

## 2. 新发现 R4 / P2：观察线程不能以 ident/is_alive 作为中断后的退出证明

**位置**：`src/tricoder/process/windows_job.py:126-135`；关联启动点 `:63-64`、observer `:96-114`、完成发布 `:173-198`；`src/tricoder/process/control.py:339` 在 Job 已关闭后直接返回成功，进一步导致迟到资源无法经原入口回收。

### 触发与实际后果

1. **启动确认窗口**：Python `Thread.start()` 先创建原生线程，再等待 Python 启动确认。原生线程已经启动但还未设置 ident 时，主线程的等待可以被 KeyboardInterrupt 中断。当前 `worker.ident is None → True` 把“启动状态未确认”当作“没有活线程”。
2. **join 中断窗口**：本机 Python 3.11.6 的 `_wait_for_tstate_lock` 异常分支可能释放仍由工作线程持有的结束锁并调用 `_stop()`。因此 `is_alive() == False` 并不总能证明被中断 join 对应的线程已经退出。当前重试只检查这个状态，会继续通过完整性核对、关闭完成端口和 Job、发布 complete。
3. 观察线程此时可能仍在 `_capture` 的 native 调用中。恢复后，它把新取得的精确句柄写进 `extras`。Job 已为 0，调用方先前又得到了清理成功，资源没有进入 pending。即使再次调用 `_ProcessResources.cleanup`，Job.close 的已关闭分支也不再调用 tracker.cleanup，迟到句柄仍未回收。

**公开调用链的真实复现**：`test_public_join_boundary.py` 直接调用 `run_bounded_process`，保留真实 Popen、Job、完成端口、OpenProcess、IsProcessInJob、WaitForSingleObject、CloseHandle。通过事件让 observer 暂停在真实 OpenProcess 已取得句柄、尚未返回登记的交错点；用局部 Python tracing 在实际 `Thread.join` 的等待语句注入 KeyboardInterrupt。没有伪造 native 退出/清理成功返回值，没有 sleep，没有向用户控制台发送 Ctrl+C。

结果见 `reviewer-public-join-v3/public-join-interruption.json`：

```text
primary_preserved=true
exception_cleanup_failed=false, exception_has_resource=false
scope_pending=false, scope_failed=false
observer_exited=false, python_is_alive=false
job_handle=0, port=null, complete=true
extra_handle_count_after_observer_exit=1
extra_handles_valid=[true]
extra_handle_count_after_resource_retry=1
```

主中断本身没有被覆盖，问题是清理被误确认、owner 门禁失去依据。测试自有 finally 单独等 observer 完成并关闭残留句柄；这部分不算产品清理成功。父子进程已经退出，不能把“进程已退出”当作“观察线程及其句柄都已回收”。

另有两个层次的交叉证据：`reviewer-startup` 使用真实原生线程、延后 bootstrap 的受控调度，证明启动确认未完成时也会提前关闭；`reviewer-real-join` 经真实 `_ProcessResources.finish` 两次调用证明同一缺口；`reviewer-join-v2` 使用真实 Python observer 和受控 native 模型复现同一状态变化。它们是同一条 R4 的不同证据，不计为四个独立产品缺陷。

这是确定性中断注入下的产品失败，不代表测得了用户自然按 Ctrl+C 的发生频率；本轮没有对其他 Python 补丁版本外推。已保存实际解释器 `Thread.start/_bootstrap_inner/join/_wait_for_tstate_lock` 源码到 `python-thread-lifecycle.txt`；CPython 官方问题也记录了这种 join 中断后的误停止状态：[CPython #90882](https://github.com/python/cpython/issues/90882)。该链接说明机制，本地新探针才是本轮验收依据。

### 为什么原新增测试没有捕获

`test_primary_interrupt_during_observer_start_keeps_native_owner` 在原 `WindowsJobTracker.start()` 完整返回后才抛异常，届时 worker 已完成启动确认。现有 blocked-observer 测试检查 join 超时，没有打断 join/线程状态等待本身。两个测试都保留且本轮通过，但覆盖不到上述窗口。

### 修复约束

- 在尝试启动前登记明确的启动生命周期和 owner；未启动、启动结果未知、已运行、已停止必须区分。不能由 ident=None 推导从未创建原生线程。
- 增加由 observer 自己发布、位于所有 native 调用和账本写入之后的完成/静止确认；不能仅信任中断后的 Python is_alive/join 状态。需要严格 OS 线程退出证明时，使用受持有的原生线程句柄及有界等待，不猜测 PID/TID。
- 未确认时沿用原期限保留 Job、端口、tracker、已取得/待登记资源，并报告 cleanup_failed/pending；主中断保持不变。不能提前 complete 或先关闭 Job 再寄望下次重试处理迟到句柄。
- 补入启动确认被中断、join 被中断、迟到 capture、原期限已耗尽、公开 run_bounded_process 到持久 owner 的回归；同时保留普通启动、启动前明确失败、正常停止成功路径。
- 本轮仅提出修复要求，没有实现修改，也不建议通过升级/恢复 Python 3.12、放宽预算或屏蔽中断绕开。

## 3. 原三项发现与其他要求逐项结论

| 核查项 | 独立结论及证据 |
| --- | --- |
| R1：查询后出生的成员没有精确等待 | **原条件关闭**。累计 TotalProcesses 对账结合全生命周期唯一成员表和逐句柄 Wait，在 observer 正常确认停止时没有复现旧问题。聚焦中 query/terminate 两个真实交错点各 10 轮均捕获子句柄，返回即时 Wait=0，测试兜底前严格删除目录。R4 是新增 observer 生命周期缺口，整体成功契约仍未通过。 |
| R2：捕获失败后空列表洗掉未知 | **原条件关闭**。OpenProcess error5、Query error5、4096 容量不足的首次失败和空列表重试均保持 pending；返回值/异常两条真实上层 owner 集成路径均阻断下一 verifier/case，原期限不增长、failed 不清空。部分句柄、Wait 错误/超时、Close 失败、丢消息、错误身份与计数倒退负例通过。 |
| R3：恢复前可见取消仍执行脚本 | **关闭**。入口、Snapshot 后、枚举循环和 OpenThread 登记后均复检原 token；本/父令牌四组真实事件探针无 ResumeThread，脚本未 SetEvent，资源清理成功。恢复后才到达的取消仍不承诺撤销已发生效果。 |
| 旧搜索 5 秒 P2 | **维持关闭**。本轮实际运行至少 5.2 秒模型等待、随后搜索/compileall/finish；取消、已有期限、未确认清理、默认普通命令及 Session owner 回归通过。没有调用真实模型。 |
| 冷启动、输出超限与正常行为 | 冷启动探针、输出超限真实树 10 轮及立即目录删除、普通/快速退出/孙进程成功路径通过；没有以目录错误忽略、额外 sleep 或基础设施错误当成功。 |
| 资源与评分 | Eval cleanup/runner/TaskCleanup/Agent/Session 均未改；资源不序列化、失败验证不因清理成功变通过、环境 pending 不被新 host owner 隐藏的既有检查通过。R4 仍能在新底层 observer 上破坏清理成功语义。 |
| 诊断 | 本次新生产代码没有增加 argv/env/输出/异常正文的诊断发布；既有受控诊断断言保留。没有真实敏感数据泄露试验。 |
| TUI、fake Popen、历史 CI 测试 | 本轮未改这些旧测试；原断言和目录清理失败抛出语义保留。仅既有 owner 取消 fixture 透传 resources 参数，另新增 owner 集成测试。 |
| 依赖闭包 | evals/workspace.py 增补复制 windows_job.py 和空 process 包标记，复用既有路径/文件安全核查。中途全量真实暴露的 ImportError 已在最终候选修正；这是必要接线，没有改题目或评分。 |

完整性论证以标准 Job 继承、无 breakaway、匿名不继承 Job、只有宿主分配挂起 root 为边界。通知只发现候选；未捕获成员使累计数与唯一表不符，不能靠空列表、ActiveProcesses=0 或父退出替代逐句柄退出。累计数包括已退出及失败关联，因此缺失时允许保守 pending，不声称所有 native 故障都能自动恢复。

核对依据：[Microsoft TotalProcesses 定义](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_basic_accounting_information)、[完成端口通知及 PID 限制](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_associate_completion_port)、[进程退出信号语义](https://learn.microsoft.com/en-us/windows/win32/procthread/terminating-a-process)。这三者不能代替观察线程自己的完成证明。

## 4. 本轮实际验证

| label | unittest 方法数 | 结果（skip 单列） | exit | unittest 秒 / 墙钟秒 | 备注 |
| --- | ---: | --- | ---: | --- | --- |
| focused | 207 | OK | 0 | 126.481 / 128.023 |  |
| full | 2299 | OK (skipped=15) | 0 | 1587.788 / 1600.656 |  |
| compile | — | 编译完成 | 0 | — / 1.671 |  |
| reviewer-startup | 1 | FAILED (failures=1) | 1 | 0.044 / 0.398 |  |
| reviewer-join | 1 | FAILED (failures=1) | 1 | 3.013 / 3.563 | 首版探针收尾超时，不作产品 RED |
| reviewer-join-v2 | 1 | FAILED (failures=1) | 1 | 0.005 / 0.508 |  |
| reviewer-real-join | 1 | FAILED (failures=1) | 1 | 0.306 / 0.808 |  |
| reviewer-public-join | 1 | FAILED (failures=1) | 1 | 0.577 / 1.087 |  |

本地全量 **2299 项，2284 通过、15 skip、0 失败/错误，exit0，1587.788 秒**。这是当前冻结字节的 reviewer 独立运行，不是转述实施方的全量。新增 reviewer 探针位于 runtime，未混入项目 discover；因此全量绿色与 R4 探针失败同时成立，不能将全量绿色当作 R4 通过。

有效补充失败为 startup、join-v2 模型、real-join、public-join 各 1 项，均 0 error/skip、exit1。首版 `reviewer-join` 在测试自有收尾中等待了一个产品没有发出的停止消息，导致 3 秒断言失败；该次归为探针收尾错误，保留日志，不作产品 RED。v2 增加测试自有停止/完成握手后才产生有效契约失败。初始保全脚本曾使用无效编码名 utf8-sig，修正为 utf-8-sig 后重做全部保全，记录于 setup-first-error.txt。

真实轮次、子场景与 unittest 方法数不相加。`native-review-summary.json` 保存聚焦两个 10 轮收据及四组取消摘要；全量各自的真实收据留在 full-v3。skip 逐条列于 `full-skips.json`，不得计为通过。

### 复跑命令

原始完整参数、退出码、耗时在各 label.json/log；以下是这次已执行的命令。复跑必须复制 runner 到新独占目录或使用新 label，不能覆盖旧证据。

```text
D:\MaHong\AGENT_WORKSPACE_V2\projects\tricoder-cli\.venv\Scripts\python.exe -X utf8 -B -m unittest test_windows_cleanup_boundaries test_eval_cleanup_ownership test_subprocess_control test_search_timeout_recovery test_session_ownership test_eval_runner test_eval_timing test_eval_quality_v3_review_fixes test_agent_async test_mcp_transport test_tui -v
D:\MaHong\AGENT_WORKSPACE_V2\projects\tricoder-cli\.venv\Scripts\python.exe -X utf8 -B -m unittest discover -s tests -v
D:\MaHong\AGENT_WORKSPACE_V2\projects\tricoder-cli\.venv\Scripts\python.exe -X utf8 -B -m compileall -q src tests
D:\MaHong\AGENT_WORKSPACE_V2\projects\tricoder-cli\.venv\Scripts\python.exe -X utf8 -B runtime/ec29-review-p2-7c19e4ab/test_startup_boundary.py
D:\MaHong\AGENT_WORKSPACE_V2\projects\tricoder-cli\.venv\Scripts\python.exe -X utf8 -B runtime/ec29-review-p2-7c19e4ab/test_join_boundary.py
D:\MaHong\AGENT_WORKSPACE_V2\projects\tricoder-cli\.venv\Scripts\python.exe -X utf8 -B runtime/ec29-review-p2-7c19e4ab/test_join_boundary_v2.py
D:\MaHong\AGENT_WORKSPACE_V2\projects\tricoder-cli\.venv\Scripts\python.exe -X utf8 -B runtime/ec29-review-p2-7c19e4ab/test_real_join_boundary.py
D:\MaHong\AGENT_WORKSPACE_V2\projects\tricoder-cli\.venv\Scripts\python.exe -X utf8 -B runtime/ec29-review-p2-7c19e4ab/test_public_join_boundary.py
```

统一环境由本轮 run.py 设置：`PYTHONPATH=src;tests`、`PYTHONDONTWRITEBYTECODE=1`、`PYTHONUTF8=1`、独占 `TRICODER_V3_EVIDENCE`；compileall 指定独占 PYTHONPYCACHEPREFIX。没有安装依赖。

## 5. 保全、增量和中途证据

- actual before 382 份、after 386 份各自与其 hash 索引完全匹配；实际增量 9 项（5 修改、4 新增、0 删除），包括未跟踪新增模块/测试/报告。与 delta-index 对应，不以 HEAD 的不精确基线代替。
- 原 5723 份安全保护索引全部未变，包含旧 evals 766 + mg28 2739 = 3505 份原保护文件，以及旧实施、独立复审、规划证据。
- 本轮实施证据 2720 份 safe hash 全部一致，seal 对 index/hash/final-verification/after/delta 的五项核对全真；531 项 metadata-only 未读取内容，也不宣称已验证内容哈希。
- 现有生产文件 124 份未变；原有生产修改只有 control.py、evals/workspace.py，另新增 windows_job.py。评分 `_score` AST 相同，默认权限、预算、题库、TaskCleanup、无关 Agent/Session 源码未改；CI 仍仅 Windows Python 3.11。
- 已独立检查实施 RED/GREEN 日志及终止门闩、辅助脚本、依赖闭包的中间失败。red-native/R1/R3 等是旧产品失败；real-helper-cancel 的 IndentationError 和 red-termination-gate 的错误交错点是 fixture 问题；full-frozen 两个失败是新增模块漏复制导致的真实集成回归。最终候选已修该接线；中间记录均保留，不要求旧错误探针在新实现下仍通过。
- 本次只新增独立报告、runtime 证据，并在 project/handoff 顶部追加本轮记录。两份文档的原正文逐字节保留；原阻塞报告、旧复审和实施报告不覆盖。最终 hash 校验见 final-review.json。

## 6. 限制与下一步

- **R4 修复并独立复审前禁止合并。** 原 R1/R2/R3 可按上述具体触发条件关闭，新的 blocker 单独跟踪；不要把状态简写为“全部通过”。
- 最终候选仍未提交；本轮没有取得对应新 SHA 的干净远端 Windows3.11 CI，也未推送。旧 d6d6c4c 的绿色 CI 不能覆盖本轮工作树。后续获得明确授权后再做最终 SHA CI/合并，本次不自动推进。
- 没有真实模型复测、其他 Windows 版本/Python 补丁版本、POSIX 运行、实际 4097 进程压力、真实权限拒绝/全部 native 故障组合或自然 Ctrl+C 频率测试。4096/API 故障组合使用受控模型；中断使用确定性注入，native 成功/失败结果未伪造。外部特权注入、WMI/Job 外代理不在本次受管进程树契约中。
- 同步 native API 仍没有可抢占的 OS 硬超时保证；本轮没有把这个既有边界描述成已解决。观察线程生命周期是新增资源责任，R4 不能以该限制豁免。
- 历史 CI 40721403072 两次失败的底层诊断不足，**仍不能证明本轮缺口或修复就是历史 verification_error / WinError32/5 的精确根因**。新本地/未来 CI 成功也不能追溯证明该因果。
