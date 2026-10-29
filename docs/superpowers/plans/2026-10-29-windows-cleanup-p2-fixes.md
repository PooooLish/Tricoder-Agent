# Windows 清理与启动取消三项 P2 修复计划

> **For agentic workers:** 使用 `superpowers:executing-plans` 分步实施；本任务要求单会话，不派生子Agent。本文交给独立coding session，不在reviewer会话实施。用户的“不提交、推送、合并”约束优先于技能中的默认提交步骤。

**Goal:** 关闭独立复审R1/R2/R3，使未确认退出的资源不能被判清理成功，并阻止恢复线程前已取消的脚本执行。

**Architecture:** 保留 `_ProcessResources → _WindowsJob` 与 `TaskCleanup → Eval owner` 的所有权链。将Job退出确认从一次性局部快照改为跨调用保留的状态及完整性判据；恢复线程显式接收取消令牌，在native准备完成、实际恢复前复检。R1须先验证底层成员完整性机制，不能把建议当作已验证实现。

**Tech Stack:** 现有Python 3.11、ctypes、Windows Job/进程/线程API、unittest；不新增依赖。

**Spec:** `docs/reviews/2026-10-29-eval-cleanup-independent-review.md`，尤其R1/R2/R3及证据分级。本文给出修复约束、推荐技术路线与实施顺序，不改变复审结论。

## 1. 接手状态与硬约束

- 工作区：`D:\MaHong\AGENT_WORKSPACE_V2\projects\tricoder-cli`。
- 分支：`codex/eval-metrics-delivery-guidance`；源码审查基线：`d6d6c4c8245bdffc19cba03c37b2bfe316f7b448`；PR #1：https://github.com/PooooLish/Tricoder-Agent/pull/1 。
- main尚未合并，最后核对本地/远端均为 `a81ec2457ece970756b00424393e5f2df9253858`。开始时重新读取实际状态，不能假定聊天中的SHA仍是当前值。
- 工作树已有未提交的project、handoff、实施报告和复审报告；本计划及对应提示词也未提交。必须保存actual before，覆盖安全范围内未跟踪新增文件，不能仅用HEAD作基线。
- 保留 `runtime/mg28/`、`runtime/ec29-afafcaf4/`、`runtime/ec29-review-6e1b2ea2/` 和本次规划记录。旧evals766份+mg28安全文件2739份共3505份按既有索引复核；其余旧实施/复审安全证据也须保全。不要读取metadata-only数据库、.env.local、真实凭据或用户库。
- 不安装本机依赖、不调用真实模型、不推进Replan、不修改题库/评分/默认权限/模型轮数预算，不恢复Python3.12 CI。
- 不reset/clean/stash，不提交、推送或合并。若之后需要远端CI，等待用户单独授权推送最终候选；旧绿色CI不能充当新修改的CI。
- 保留当前正常命令、取消、UNKNOWN、cleanup_failed、主异常及Session owner语义。资源不进入可序列化结果；失败验证不能因后续清理成功而改判通过。
- 一律沿用/收紧原资源及任务终止期限。新增枚举、等待、监控器关闭也消费同一期限，不能每轮/每句柄重新领取5秒。

## 2. 代码定位与已确认事实

以下行号基于d6d6c4c，改动后以函数名定位。

| 发现 | 当前代码 | 失败机制 | 独立证据 |
| --- | --- | --- | --- |
| R1 / P2 | `src/tricoder/process/control.py`，`_create_windows_job`内`terminate_and_wait`，438/449/455行 | 只查询一次列表；查询后新增的后代被终止却没有进入精确句柄等待集 | `real_late_threaded_member_v3.py`：真实5/5轮`_ProcessResources.finish(None)`返回True时，预持有子句柄仍WAIT_TIMEOUT=258 |
| R2 / P2 | 同文件439、447、455行 | `captured`每次重算，首次捕获失败事实在下一次空列表重试中消失 | `test_native_review.py`及capture三份JSON：OpenProcess失败、查询失败、4096容量不足均first=False、second=True、waited=[] |
| R3 / P2 | 同文件148行及`_resume_windows_process`525行 | helper之前检查取消，Toolhelp/OpenThread期间抵达的取消不再检查，仍ResumeThread | `real_cancel_during_snapshot.py`：取消已置位，真实脚本仍SetEvent，1项失败 |

上述文件均位于 `runtime/ec29-review-6e1b2ea2/`。复审原187项聚焦全过、最终旧CI2279项通过并不覆盖这些新失败。

上层 `src/tricoder/evals/cleanup.py` 的retain/原期限retry/持久owner交接已经验证；`runner.py`在下一verifier/case前检查pending也已验证。优先修底层确认事实，不通过修改这些上层门禁来消除阻断。

## 3. R1技术路线：先证明成员完整性，再声称清理完成

### 推荐路线与取舍

**优先验证“从Job建立起追踪成员生命周期，并在清理时核对完整性”的路线。** 在root仍挂起、尚未执行用户代码时建立追踪，将root的既有精确句柄纳入；捕获后代身份/句柄，直至确认退出。相比当前临终一次性枚举，必须能发现最后一次快照之后的成员及可能漏记的成员。

Job完成端口可以作为发现事件的来源，但**不能单独构成完整性证明**。Microsoft明确普通通知不保证送达，通知携带PID也有退出/复用竞态。因此如果采用它，必须有独立完整性核对、丢消息/迟到消息/身份不明的负例，以及明确保留owner的失败结果。累计成员数等数据只能用于发现缺口，不能替代进程对象退出确认。[官方完成端口说明](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_associate_completion_port)

**备选路线是可验证的成员新增阻断机制，再捕获完整句柄集。** 只有证明该机制覆盖在途创建、仍存活父进程、后代线程与失败恢复，才能采用。反复SuspendThread/枚举直到两次相同，或猜测ActiveProcessLimit=0的含义，都不能默认满足这一条件。优先受支持API；不得直接引入未验证、未记录版本支持的undocumented API作为唯一保证。

这两条路线是给coding session做小型原型验证的技术选择，不是用户已选择或reviewer已验证的Windows实现。无需为普通内部命名反复询问；如果必须扩大平台要求、改变公开命令语义或使用额外依赖，应报告具体取舍。

### 必须交付的证明

- 每个成员由何种可靠证据进入账本，何时取得精确句柄，PID复用如何区分。
- 最后一次枚举/事件处理后再新增成员为何不能漏掉；在途创建如何计入。
- 查询失败、通知丢失、容量不足、OpenProcess失败或身份无法判定时为何不会误确认。
- 哪个判据说明“所有需确认的成员均已覆盖”，哪个判据说明“各自已退出”。两者必须分开；空列表、计数归零、终止调用成功、父进程退出不是后者的替代品。
- 监控线程、事件/端口/快照/Job/进程句柄的owner及退出顺序；失败后不能丢弃其唯一引用，也不能让监控者在资源判清理后继续写入账本。
- 原期限耗尽、API不支持、成员数超过受支持上限时的行为：返回未确认，保留资源并阻断；同时正常有界场景必须继续成功，不能将所有Windows命令永久判cleanup_failed充当修复。

若原型不能证明以上条件，R1仍未完成。应保留已完成的R2/R3及原型失败证据，明确剩余阻塞，而不是靠重跑绿色或延时提交“完成”。

## 4. 拟修改边界与接口

| 文件 | 修改范围 |
| --- | --- |
| `src/tricoder/process/control.py` | `_WindowsJob`及其native退出状态；`_create_windows_job`；`_resume_windows_process`及唯一生产调用点；必要的`_ProcessResources`精确资源持有 |
| `tests/test_windows_cleanup_boundaries.py`（新增） | R1/R2/R3确定性native故障与生命周期状态回归 |
| `tests/test_eval_cleanup_ownership.py` | 下层未确认到Eval owner、下一verifier/case阻断的集成断言 |
| `tests/windows_cleanup_boundary_probe.py`（新增） | 独立真实Windows进程/取消握手探针；只输出受控事实 |
| `tests/test_subprocess_control.py`等现有fixture | 仅在接口变化实际影响时适配新增OS边界；保留原断言 |
| project/handoff及新实施报告 | 追加进度、证据、限制，不覆盖旧正文 |

不默认拆分进程模块或修改TaskCleanup/Session/Agent。如果R1确实需要独立native辅助模块，限制为该机制并在delta解释职责，避免借机重构。

保持外部接口：

```python
run_bounded_process(..., cancellation: CancellationToken | None = None, ...) -> BoundedProcessResult
_WindowsJob.close(*, deadline: float | None = None) -> bool
_ProcessResources.cleanup(deadline: float) -> bool
```

R3推荐内部签名：

```python
def _resume_windows_process(
    process,
    resources: _ProcessResources,
    *,
    cancellation: CancellationToken | None = None,
) -> None: ...
```

`run_bounded_process`传入原令牌，包括父令牌取消语义；不要在此创建新的取消状态或访问令牌私有锁。

R1/R2状态由每个Job拥有，至少区分：完整性尚未证明、仍缺成员/身份信息、已持有但未signaled的句柄、已signaled但关闭失败的句柄、监控资源未关闭、全部确认完成。字段具体命名由实施方确定；不要把所有状态塞进单个每次重置的`captured`。

## 5. 分步实施

### Task 0：actual before、复现与证据准备

- [ ] 读取根/项目AGENTS、README、pyproject、最新handoff、独立复审报告及本文；核对HEAD/分支/dirty/untracked状态。
- [ ] 创建 `runtime/ec29-p2fix-<唯一后缀>/`。保存安全文件的actual before、SHA、git status及完整受保护索引。证据/源码白名单不得包含凭据或真实数据库。
- [ ] 将reviewer runner/probe复制到新目录后执行RED；不要原地运行会写独占文件的旧脚本。每轮设置新的TRICODER_V3_EVIDENCE目录。
- [ ] 保留R1真实v3、R2三种native失败、R3真实事件握手的RED。旧`real-threaded-enumeration`的测试清理错误不是有效产品RED，不能拿它代替v3。

交付：三项发现与新RED日志一一对应；新API未mock、ImportError、目录配置错误等必须列为fixture错误，不能计为产品RED。

### Task 1：先修R2，建立跨重试的保守状态

- [ ] 新增 `test_capture_failure_then_empty_retry_stays_pending`，分别覆盖OpenProcess error5、Query error5、容量不足error234。第一次失败后即使下一次列表为空，未获得可靠终结证据也必须继续False、保留Job和原资源。
- [ ] 新增 `test_partial_handles_survive_capture_failure`：部分句柄已取得、另一部分失败；既有句柄不能漏关、重复关或失去owner。
- [ ] 新增 `test_confirmed_handles_close_failure_stays_pending` 与 `test_missing_member_only_clears_after_valid_evidence`：前者在CloseHandle失败时保留；后者仅有真正补齐成员且退出的证据才可解除缺口，不能写一个无条件清flag入口。
- [ ] 将捕获缺口从回调局部变量提升到Job持久状态，所有失败路径先记录已取得的精确资源，再处理错误。R1机制未提供完整证明时，不因下一次query成功而清除过去缺口。
- [ ] 经 `_ProcessResources.finish → retain_failure → retry_verification_cleanup` 验证：首次失败后的立即retry仍不能误确认；原期限不增加，下一case executor及下一verifier均未启动，诊断/结果不含资源。

核心断言（测试夹具按最终机制适配，但含义不得改变）：

```python
assert first_cleanup is False
assert retry_after_empty_list is False
assert owner.has_pending
assert all(observed <= original_deadline for observed in retry_deadlines)
assert next_executor_calls == 0
```

Task 1完成不等于R1完成；不能把所有路径永久置为pending以宣称三项修完。

### Task 2：修R3，恢复前取消检查

- [ ] 新增 `test_cancel_during_snapshot_never_calls_resume`、`test_cancel_during_open_thread_never_calls_resume` 及父令牌取消变体。在这些native调用成功返回前置位取消；预期ResumeThread调用次数0。
- [ ] 修改上述helper签名并透传令牌。进入准备阶段及耗时枚举循环应响应取消；快照/线程句柄取得后先登记owner，在实际ResumeThread前再检查取消。
- [ ] 保留资源清理和主CancellationError身份；二次清理中断不能覆盖主中断，native CloseHandle失败仍有owner。不要改“已经恢复后发生取消”的现有运行中取消/效果未知语义。
- [ ] 将真实事件探针纳入回归：在Toolhelp阶段设置取消，合成用户脚本不得SetEvent；不需要调用ResumeThread，也不得依靠迅速kill掩盖先运行后取消。
- [ ] 覆盖无取消正常恢复、Job分配失败、ResumeThread失败/返回异常挂起计数、快照及线程句柄关闭失败。旧冷启动归属/Session owner测试仍须通过。

取消边界解释：恢复决策前已经可观察到的取消应阻止恢复；恢复之后抵达的取消仍须终止并确认清理，不能承诺撤销已执行脚本。不要用长时间持有CancellationToken私有锁来隐藏取消或阻塞UI。

### Task 3：R1原型验证及最小集成

- [ ] 在独占证据目录验证第3节选定机制，记录API、OS支持、资源owner及完整性论证；不先把未经验证的机制并入通用执行路径。
- [ ] 添加 `test_late_member_before_closure_is_awaited`：通过确定握手在查询后创建同Job子进程，独立持有其句柄；成功返回时该句柄必须signaled。
- [ ] 添加 `test_creation_at_closure_boundary_is_accounted_or_prevented`：覆盖进入关闭阶段与创建操作交错。若方案阻止了创建，须提供明确的拒绝/未执行证据；不能把旧探针等不到child-ready的超时当GREEN。
- [ ] 添加 `test_missing_notification_or_generation_mismatch_stays_pending`、`test_membership_limit_or_native_failure_stays_owned`：如果使用事件追踪必须覆盖丢失/迟到/重复通知、快速退出、PID复用/身份不确定；不用事件的方案覆盖等价的完整性丢失情形。4096边界必须有负例，不能扩大数组后删掉故障路径。
- [ ] 真实场景至少包括普通后代、64线程后代、快速退出后代、后代再创建后代、关闭边界受控交错。保持所有native调用真实返回，不能通过mock Wait返回0造成功。
- [ ] 原型通过后最小集成到Job创建和清理生命周期；如有监控worker/完成端口，必须在用户代码恢复前具有owner，并在未确认时继续被持有。保持正常无后代命令、正常验证器、compileall等成功路径。
- [ ] 每次等待使用剩余原期限。失败后先返回不确定并保持owner，再由宿主按既有规则处理；不能放宽UNKNOWN/cleanup_failed、抹去pending或授权下一case继续。

真实探针成功标准：成功返回的那个时刻，所有独立预持有的测试后代句柄立即检查均为signaled；随后立即删除工作目录成功。失败观察后的测试自有清理必须单独记录，不可算成产品清理成功。

若新机制替换了原查询时点，可把reviewer探针的门闩移到等价边界并记录适配理由；不能只保留“cleanup_failed=False”而删掉精确句柄、是否执行脚本、目录删除等业务断言。

### Task 4：集成回归、最终快照与交接

- [ ] 新边界模块全部GREEN，R1真实受控交错至少10轮，原输出超限真实进程树10轮、冷启动及R3真实事件回归通过；0skip的要求针对本机应能执行的新增Windows场景。
- [ ] 运行下述聚焦集合；数量会随新增测试增加，必须按实际记录，通过/失败/error/skip分列。
- [ ] 在最终源码/辅助脚本冻结后运行完整unittest及compileall。全量期间若任何相关文件变化，则原日志不能称覆盖最终字节；重新完成相应最终验证。
- [ ] 生成actual after、含新增文件的delta/hash、保护文件核对、完整命令/退出码/skip列表。按R1/R2/R3分别写修复前后证据，记录全部中途失败。
- [ ] 更新project/handoff，保留旧正文；交给reviewer独立复审。本轮无提交/推送授权时只提交工作成果给用户，不运行git commit/push/merge。若之后获准推送，再用最终SHA的干净Windows3.11 CI作为远端证据。

## 6. 回归命令与验收矩阵

使用既有 `.venv/Scripts/python.exe -X utf8 -B`，设置 `PYTHONPATH=src;tests`、`PYTHONDONTWRITEBYTECODE=1`、`PYTHONUTF8=1`；每个命令均配独占TRICODER_V3_EVIDENCE目录。compileall的PYTHONPYCACHEPREFIX指向本轮新目录。

```powershell
# 通过本轮独占runner保存完整log和实际exit code，不重用旧runner输出位置。
.venv/Scripts/python.exe -X utf8 -B -m unittest test_windows_cleanup_boundaries test_eval_cleanup_ownership -v
.venv/Scripts/python.exe -X utf8 -B -m unittest test_subprocess_control test_search_timeout_recovery test_session_ownership test_eval_runner test_eval_timing test_eval_quality_v3_review_fixes test_agent_async test_mcp_transport test_tui -v
.venv/Scripts/python.exe -X utf8 -B -m unittest discover -s tests -v
.venv/Scripts/python.exe -X utf8 -B -m compileall -q src tests
```

这些命令只使用已有.venv解释器，不创建或安装新环境。

| 验收项 | 必需结果 |
| --- | --- |
| R1晚到成员/在途创建 | 不遗漏；成功返回前精确退出确认；无法确认则pending且阻断 |
| R2捕获失败→空列表 | 不清除历史缺口；已持有句柄和Job继续有owner |
| R3准备阶段取消 | ResumeThread不调用、脚本事件未触发；已取得native句柄被确认关闭或仍有owner |
| 原期限/取消/异常 | 期限不延长；主异常身份保持；UNKNOWN、cleanup_failed及失败判分不放宽 |
| 原搜索P2 | 超时后至少5.2秒模型等待，再搜索、compileall、finish；取消、已有期限、未确认清理负例仍通过 |
| 正常路径 | 无后代、正常子进程、验证器、恢复未取消路径继续成功；不能统一返回清理失败 |
| 原测试/评分/诊断 | 保留原断言、TUI测试同步；目录清理失败抛出；诊断不含正文/argv/env/绝对路径/凭据；资源不序列化 |
| 全量与平台 | 最终2279项基数加新增测试；skip单列；仅Windows3.11为CI要求 |
| 保全 | 旧3505保护索引及旧实施/复审安全证据不变，actual before/after可复核 |

## 7. Review Focus：必须落实到测试的易漏情形

1. **先拿到部分句柄，再查询/打开失败**：Task 1验证不会丢资源，也不会空列表洗掉缺口。
2. **成员在关闭边界创建、快速退出或再派生**：Task 3验证完整性，不能把稳定两次列表当证明。
3. **取消来自父令牌且发生在OpenThread返回前**：Task 2验证不恢复，并保留线程/快照owner。
4. **原期限已过、Wait/Close/监控退出失败**：Task 1/3验证不续时、不提前释放，下一case不能启动。
5. **成功测试依赖继承管道或测试finally等待**：Task 3独立句柄即时检查、分离后代stdio；后续测试清理不能掩盖产品提前成功。

## 8. 禁止的“修复”与完成口径

- 不通过sleep、宽限期、无限轮询、忽略rmtree、增大验证/终止预算或跳过测试消除失败。
- 不以Job计数归零、终止调用成功、第二次空列表、收到部分完成端口消息替代完整性与退出确认。
- 不按可能复用的旧PID强杀外部进程；仅清理归属明确且精确持有的测试/产品资源。
- 不把API不支持、probe握手超时、ImportError或fixture适配错误当成功或产品RED。
- 不能声称新修复就是历史CI 40721403072两次失败的精确根因；旧日志不足以支持该因果结论。
- 最终报告区分“已修R2/R3”“R1原型通过”“R1集成通过”“本地全量”“最终SHA干净CI”“独立复审”；没有完成的项保持未完成。

本文已按三个发现逐项自审：R2/R3接口和断言明确；R1的机制选择保留为有明确失败判据的原型任务，没有给未经证实的native方案背书。所有实施、测试和提交权限以用户指令及项目规则为准。
