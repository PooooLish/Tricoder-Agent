# 2026-10-29 Eval 清理独立复审

**结论：不通过，新增3项P2阻塞；不建议合并。** 本次单会话执行，无子Agent，无生产/项目测试修改，无提交、推送或合并。远端CI通过是核实的事实，不能覆盖下面新增的独立失败场景。

审查源码：`d6d6c4c8245bdffc19cba03c37b2bfe316f7b448`；分支 `codex/eval-metrics-delivery-guidance`；PR https://github.com/PooooLish/Tricoder-Agent/pull/1 。main仍为 `a81ec2457ece970756b00424393e5f2df9253858`，PR OPEN、mergedAt=null。新证据根：`runtime/ec29-review-6e1b2ea2/`。

旧Eval P2“直接丢弃cleanup_resource引用”的上层缺口已验证修复，可关闭该具体发现；这不代表底层退出确认已可靠。新增R1/R2仍可使未确认资源被过早判清理成功，整体验收及合并阻塞不关闭。原搜索共享终止期限P2独立回归通过，维持关闭。

## 新发现

### R1 / P2：枚举到终止之间新加入Job的后代未被确认退出

位置：`src/tricoder/process/control.py:438`（查询列表）、449行（终止Job）、455行（依据已捕获句柄判成功）。

`terminate_and_wait`只在终止前查询一次进程列表；现有成员在该查询返回后仍能创建继承Job的后代。终止请求会包含这些后代，但`pending_processes`没有它们的句柄，等待旧列表后即可返回True并关闭Job。父进程退出及旧列表句柄退出不能证明新增成员退出。

独立验证：

- native API确定性模型：查询返回PID11，终止前新增PID12；只等待11后返回True，12仍未确认。
- 真实受控交错：实际QueryInformationJobObject返回后，用stdin/队列握手让真实父进程创建新子进程；确认新子进程属于同一精确Job，并由reviewer预先持有其进程句柄。没有虚构query/terminate/wait的返回值。
- 普通子进程10轮：实现均未捕获新增句柄，但返回时子进程已signaled，未观察到提前返回；该结果仅是对照，不能据此排除竞态。
- 64个等待线程的合成子进程5轮：**5/5在真实`_ProcessResources.finish(None)`返回True后，精确子句柄的即时WaitForSingleObject(h,0)仍为258**。子进程stderr独立重定向，避免继承管道的reader join偶然替代退出等待。最终断言exit1。
- 完成失败观察后，测试才用自己持有的句柄有界等待退出、关闭句柄并删除自己的目录；没有sleep、忽略rmtree或把后续等待算成产品的成功确认。

证据：`real-threaded-v3.log/json`、`real-late-threaded-member-v3.json`、`threaded-v3-observations.jsonl`、`real_late_threaded_member_v3.py`。另保存直接Job.close层的v2（5/5同类失败）和普通子进程对照。

修复要求：形成可证明完整的成员/退出确认边界，覆盖查询与终止间的新成员；无法证明时保留精确owner并阻断。只在终止后再读一次空列表仍不足以证明退出，不能以Job计数归零替代句柄确认。

### R2 / P2：首次无法捕获完整句柄集，重试空列表会忘记不确定性

位置：`src/tricoder/process/control.py:439`、447行及455行。

`captured`每次回调重新初始化；失败只保存在本次局部变量。第一次查询失败（包括4096容量不足），或OpenProcess因非87错误失败时，仍执行TerminateJobObject。下一次重试可能只读到空Job列表；此时captured=True且pending_processes为空，就关闭Job并返回True，虽从未持有或等待那些进程对象。

独立故障注入执行真实生产closure，分别模拟OpenProcess失败(error5)、查询容量不足(error234/4097成员)、查询失败(error5)，然后空列表但进程对象尚未signaled。三个场景都得到：first=False，second=True，waited=[]，Job已关闭。恢复既有期限/owner不能补回已经丢失的“未捕获成员”事实。它会令上层scope.retry移除原本应保留的资源。

证据：`test_native_review.py`、`native-boundaries.log`、`capture-open_error.json`、`capture-overflow.json`、`capture-query_error.json`。这些是确定性native故障模型，不宣称真实触发了系统4097进程或访问拒绝。

修复要求：跨重试保留完整性/不确定状态；仅在获得真正退出证据后解除。应同时覆盖已获得句柄的等待/关闭失败与未获得句柄的失败；当前正向对照证明“已获得句柄WAIT_TIMEOUT后保留至signaled”的路径有效，不能外推到未捕获路径。

### R3 / P2：线程枚举期间取消后仍调用ResumeThread执行用户代码

位置：`src/tricoder/process/control.py:148`的取消检查，及`_resume_windows_process`第525行的ResumeThread。

取消仅在进入恢复helper之前检查。helper内部创建快照、遍历线程、打开线程句柄后直接恢复线程，不再观察取消；其间到达的取消无法阻止脚本开始执行。

独立验证先以模型证明Snapshot阶段置位取消后仍调用ResumeThread，再用真实CREATE_SUSPENDED/Job/Toolhelp/ResumeThread执行合成脚本：快照成功返回时设置CancellationToken，原生ResumeThread前确认取消已置位，脚本随后成功SetEvent通知父测试。OS事件握手提供确定的执行证据，不使用sleep。最后正常抛出CancellationError且清理成功，不能撤销已经执行的脚本。

证据：`real_cancel_during_snapshot.py`、`real-cancellation.log/json`、`real-cancel-during-snapshot.json`；1项、1失败、0skip、exit1。记录为cancelled_before_resume=true、previous_count=1、user_script_event=0。

修复要求：恢复helper应在实际恢复前检查/协调取消，特别是经过可能耗时的native枚举与OpenThread之后；取消路径继续保留/清理快照和线程句柄。现有测试在Job分配后、helper调用前取消，覆盖不了本次交错。

## 逐项验收

| 用户关注项 | 独立结论 |
| --- | --- |
| 返回值/异常先retain精确资源再retry | 通过；代码顺序和两路径回归符合，清理成功仍保留失败判分 |
| 无环境scope的显式/进程寿命owner，丢弃结果 | 通过现有边界；弱引用/结果丢弃/精确释放回归通过；资源不进入结果序列化 |
| 环境旧scope不被新host隐藏，下一verifier/case阻断 | 通过；入口检查两种pending；在executor前阻断，timings仍为结构化结果 |
| failed/pending不可无据清空 | 上层未重置failed；底层R1/R2误确认可过早移除资源，因此整体不通过 |
| 已有期限、原资源期限、主/二次中断 | 聚焦回归通过；未改TaskCleanup；子scope retry取原期限与调用者期限较早者 |
| 原搜索P2 | 真实工具链超时后合成Provider等待至少5.2秒，再搜索/compileall/finish通过；未确认清理、普通命令、取消及Session owner负例通过 |
| 新Windows启动取消边界 | Job分配前不执行用户代码的既有冷启动/恢复失败用例通过；新增Toolhelp期间取消R3失败 |
| Windows后代退出确认 | 已有10次输出超限/预持有句柄/立即删目录回归通过；新增成员及捕获失败R1/R2不通过 |
| Toolhelp/线程句柄失败保留 | 当前路径先登记native_handles，关闭失败保留，相关回归通过；不宣称全部native错误组合穷尽 |
| 诊断与评分 | 受控类型/整数errno、winerror/布尔状态，无argv/env/输出/异常正文；_score AST与actual before相同；资源不序列化 |
| 历史测试、TUI、fake fixture | 原断言保留；tearDown异常原样抛出并加诊断；TUI只改测试等待并新增Event门闩；三处fixture只mock新增恢复边界 |
| Python3.12与范围 | 仅3.11 CI，未恢复3.12；默认权限/预算/题库/TaskCleanup及118份无关生产文件保持 |

## 本次实际测试与命令

所有命令均由reviewer执行，使用已有.venv，不安装依赖。`run.py`给每次调用设置全新 `TRICODER_V3_EVIDENCE=<证据根>/<label>-v3`，保存子进程原生退出码、时间和完整输出。命令前缀为：

```powershell
.venv/Scripts/python.exe -X utf8 -B runtime/ec29-review-6e1b2ea2/run.py
```

| label及后续参数 | 数量与结果 | exit |
| --- | --- | --- |
| `focused -m unittest test_eval_cleanup_ownership test_subprocess_control test_search_timeout_recovery test_session_ownership test_eval_runner test_eval_timing test_eval_quality_v3_review_fixes test_agent_async test_mcp_transport test_tui -v` | 187项，187通过、0失败/错误、0skip；118.846s | 0 |
| `native-boundaries runtime/ec29-review-6e1b2ea2/test_native_review.py` | 4方法：1通过、3失败；因subTest共5条失败断言，0错误、0skip | 1 |
| `real-enumeration runtime/ec29-review-6e1b2ea2/real_late_member.py` | 10轮真实普通子进程对照；均未捕获新增成员，未观察到退出延迟 | 0 |
| `real-threaded-v2 runtime/ec29-review-6e1b2ea2/real_late_threaded_member_v2.py` | 5/5轮Job.close返回成功但子句柄未signaled，最终断言失败 | 1 |
| `real-threaded-v3 runtime/ec29-review-6e1b2ea2/real_late_threaded_member_v3.py` | 5/5轮ProcessResources.finish返回成功但子句柄未signaled，最终断言失败 | 1 |
| `real-cancellation runtime/ec29-review-6e1b2ea2/real_cancel_during_snapshot.py` | 1项，1失败、0错误、0skip；真实脚本在取消后执行 | 1 |
| `compileall -m compileall -q src tests` | 编译完成；缓存定向本轮证据目录 | 0 |

真实轮次不是新增unittest方法数，不能与187项相加宣传总通过率。复现脚本采用独占输出，重跑需复制runner及相关probe到新的同级独占证据目录并替换路径，不能覆盖当前文件。

首次`real-threaded-enumeration`（未带v2/v3）的退出码也是1，但来自reviewer清理代码对已终止中的进程再次TerminateProcess的返回值断言，不是有效完整产品RED。该中间脚本/日志保留；后续版本在失败观察之后以精确句柄有界等待确认退出，完成所有轮次、清理并保存逐轮证据，最后才因产品契约断言exit1。不得用前一次harness错误冒充本报告R1证据。

没有另跑本地2279项全量：已用新探针确定阻塞，重复不覆盖新增边界的原全量不能消除这些发现。本地全量实施日志只作参考，未当独立验收。

## CI与实施证据的独立核查

从GitHub重新读取并保存了最终run原生metadata/full log，匹配源码d6d6c4c：[Windows3.11 CI 40810040358](https://github.com/PooooLish/Tricoder-Agent/actions/runs/40810040358)确实2279项、2275通过、4skip，577.536s，compile成功。四项skip分别是Windows锁文件替换、Linux抽象socket、Windows锁目录重命名、控制字符文件名；未计为通过。原始skip行见`ci-current-summary.json`。

本地实施全量为2279项/15skip/1147.780s；核对e8f3b1e到d6d6c4c确实只改`tests/eval_cold_process_probe.py`，故该本地日志不覆盖最终helper字节。最终helper已在本次187项独立重跑中覆盖。CI的成功不能据此关闭本次新探针发现。

抽查RED原始日志并保存`red-log-audit.json`：ownership确为未retry/未持有的断言；主中断身份、blocked timings、环境旧owner、TUI发布门闩、冷启动精确Job归属均有行为失败记录。real-process-red-3和计数方案green-1均为实际WAIT_TIMEOUT，后者确为失败方案。focused-launch-1为旧fake抵达新native边界的fixture错误。focused-1日志是缺PID文件导致process_exception等，不能独立证明进程退出缺陷；其CRLF归因来自实施调查，不将该日志当产品RED。未要求旧错误行为探针在修复后保持通过。

**历史因果限制仍保留：** 40721403072两次尝试的public verification_error、tearDown WinError32/5缺底层诊断；本次新复现、新机制以及新CI成功均不能追溯证明它们的精确根因。

## 保全与未验证范围

- actual before369/after374均逐文件hash核对，当前374份与after一致；独立计算15项增量，10修改/5新增/0删除，与delta-index一致，含新增未跟踪文档及新增源码/测试；不将HEAD当actual before。
- 旧evals766份、runtime/mg28安全证据2739份，共3505份全部一致。实施证据2128份安全hash一致，seal的index/manifest hash一致；2130条索引中2条仅metadata，不读取其内容、不虚称全部内容校验。
- README、pyproject、AGENTS、CI、TaskCleanup、Session/Agent/权限策略无增量；118份非本轮生产文件保持，评分_score AST一致。旧project/handoff正文与原阻塞报告完整保留。
- 审查开始时三份实施文档状态单独留存；本次只新增本报告并向project/reviewer-handoff追加结论，保留实施方未提交内容。实施报告本身未改。
- 未验证POSIX、其他Windows版本、全部native API错误组合或无限并发进程树；未真实创建4097进程，容量及AccessDenied使用故障模型。未安装依赖、读取.env.local/真实凭据/用户数据库、调用真实模型或推进Replan。没有新模型质量结论。
- 仍须coding session修复R1/R2/R3后重新独立复审。**本次不合并；即使下一次复审通过也须用户明确指令。**

Windows语义参考：进程列表是查询返回的成员列表，容量不足需识别完整性；终止请求针对调用时的Job成员，不能替代对精确进程对象的退出确认。参见[进程列表定义](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_basic_process_id_list)、[TerminateJobObject](https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-terminatejobobject)。恢复线程会降低挂起计数并实际运行线程，参见[ResumeThread](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-resumethread)。本报告具体失败结论以代码与上述独立探针为依据。
