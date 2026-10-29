# 2026-10-29 Eval 清理资源所有权与 Windows 进程退出实施报告

状态：**实现完成，待独立复审**。PR #1 未合并；原 main 集成阻塞报告及独立结论完整保留。本报告属于实现自测，不能替代 reviewer 验收。

## 基线、范围与提交

- actual before 为任务开始时工作树的369份安全文件字节及SHA256，不从HEAD重建；本地提交2807343，远端PR候选cca5535。初始项目工作树无修改/未跟踪文件，存在既有`.pytest_cache/`权限告警。
- 独占证据：`runtime/ec29-afafcaf4/`。`before/`、`before-sha256.json`、`status-before.txt`记录基线。全部evals的766份文件与runtime/mg28的2739份安全证据文件（合计3505）在`protected-sha256.json`中；最终逐字节hash核对0变化。原索引不包含数据库、锁及pyc，未复制任何用户数据库或秘密配置。
- 原有提交保留，新增候选提交依次为ed0bf24（资源交接/退出确认）、e8f3b1e（冷启动归属及TUI测试同步）、dc09c12（新探针LF规范化）、d6d6c4c（探针持有精确进程句柄）。当前源码候选`d6d6c4c8245bdffc19cba03c37b2bfe316f7b448`已推送原PR以执行干净CI，没有合并main。交接文档更新留在本地工作树，不另外触发无代码变更CI。
- 本地与远端main仍为`a81ec2457ece970756b00424393e5f2df9253858`。CI继续仅Windows/Python3.11；未恢复3.12，未修改工作流、默认权限、任务终止预算、评分或题库。

## 需求与实际改动

| 要求 | 文件/位置与实现 | 行为证据 |
|---|---|---|
| 返回值/异常资源不丢失 | 新增`src/tricoder/evals/cleanup.py`；`runner.py:325`两路径先retain精确cleanup_resource，再按已消费期限重试 | 返回值/异常注入、弱引用、释放、清理异常、中断身份、取消映射测试 |
| 上层交接及阻断 | `runner.py:80/120`可选cleanup_owner；默认持久进程owner；已有TaskCleanup继续作为owner；`runner.py:312`同时检查宿主与环境scope pending | 真实隔离runner评分链路；下一case在executor前被阻断；新host owner不能隐藏环境旧pending；阻断结果保留timings |
| 有界清理 | 子scope包装重试取原期限与调用者期限的较早者；不清空failed/pending，不因清理成功改写失败判分 | 已有更早deadline、过期deadline、精确资源释放及原搜索P2回归 |
| Windows退出确认 | `process/control.py:329/432`保留Job中进程的SYNCHRONIZE句柄，TerminateJobObject后按原期限确认退出，再关闭句柄/Job；失败保留所有权 | 真实输出超限：父/后代均在精确Job，10次预持有句柄均已signaled，随后立即rmtree |
| Windows启动归属 | `process/control.py:122/479`挂起创建→加入Job→检查取消→恢复初始线程；Toolhelp快照及线程句柄由原ProcessResources持有 | 新解释器冷启动RED/GREEN；恢复失败和取消不执行脚本；句柄关闭失败保留重试 |
| 两个历史CI失败点安全诊断 | `runner.py:402`只输出固定阶段、白名单类别、整数errno/winerror及布尔事实；新增`tests/eval_process_diagnostics.py`；`test_eval_runner.py`与`quality_v3_support.py`接入 | 无argv/env/输出/异常正文/绝对路径；目录删除失败记录后原样抛出；资源不进入可序列化结果 |
| 新CI的TUI竞态 | `tests/test_tui.py:406/594`只修测试等待：结果出现后等待worker完成，保留侧栏所有断言 | Event精确控制发布时点，RED失败；36项TUI GREEN；不改生产TUI |

辅助新增：`tests/test_eval_cleanup_ownership.py`共19项，`tests/eval_cold_process_probe.py`使用全新解释器/独占临时目录；探针在进程仍活着时取得并持有精确句柄到最后，不在退出后按可能复用的PID重开句柄。`test_subprocess_control.py`、`test_agent_async.py`、`test_mcp_transport.py`各给原有fake Popen fixture补新OS恢复边界的mock，旧异常/deadline断言保留。共12个代码/测试文件发生增量；另3份实施/交接文档更新。完整文件列表见`delta-index.json`。

## 根因证据与不能推断的部分

1. **Eval所有权缺口已确定性复现**：旧返回值与异常两条路径丢弃cleanup_resource，cleanup调用0；新路径持有并有界重试，未确认资源上交宿主，不让下一case继续。原普通异常、取消映射及BaseException传播保持；二次清理中断不会覆盖主中断。
2. **Windows异步退出确认缺口已真实复现**：CloseHandle(KILL_ON_JOB_CLOSE)+父进程wait不足以证明后代已退出；第一次使用Job ActiveProcesses计数归零的方案也失败，已弃用。预持有进程句柄是最终探针的判据。
3. **独立冷启动归属竞态已真实复现**：新解释器中父程序在加入Job前创建后代，两个后代均不在精确Job、清理却成功返回。采用挂起创建与先分配后恢复，保持取消/期限。它与第2项是不同场景，不能混为同一份证据。
4. **两次历史CI根因仍未证实**：40721403072 attempt1为2259项/1失败/4skip（public verification_error）；attempt2为2259项/1错误/4skip（输出超限测试tearDown WinError32/5）。原日志缺底层失败类别或持锁进程，无法把新机制探针反推为两次历史事件的精确原因。新候选CI已覆盖这两个测试且通过，仅说明当前复测未出现。
5. ed0候选的新CI40805494315为2274项/1失败/4skip，失败是TUI在worker侧栏发布前观察last_result；新增确定性测试证明并修正测试同步。不是忽略原断言或修改生产功能。

## RED、GREEN与中间失败

全部命令、退出码、耗时在`commands-and-results.md`、`test-index-final.json`及同名`.log/.json`；各次TRICODER_V3_EVIDENCE独占，不覆盖旧日志。表内失败数量可包含subTest断言，不等于独立test方法数。

| 日志 | 实际结果 | 说明 |
|---|---|---|
| red-ownership → green-ownership | 4项/5失败/exit1 → 4通过/exit0 | 返回值、异常及原期限RED |
| green-handoff | 10通过/exit0 | 扩展边界覆盖，不虚构每项单独RED |
| focused-1 | 59项/10失败/exit1 | 新测试把Windows CRLF当LF，属于测试编写错误，不作生产RED |
| real-process-2、real-process-red-3 | 各1项/10子例失败/exit1 | cleanup成功但精确句柄WAIT_TIMEOUT |
| real-process-green-1 | 1项/10子例失败/exit1 | Job计数方案无效，已弃用 |
| real-process-membership | 1项/10子例失败/exit1 | 确认本场景父子均在Job，仍未退出 |
| real-process-green-2 | 1项/10次真实进程树通过/exit0 | 句柄退出+立即删目录 |
| red-secondary-interrupt、red-blocked-timing、red-inherited-owner | 各1项，分别1/2/1失败，均exit1 | 自审补主中断保留、timings、环境旧owner负例；后续聚焦与全量通过 |
| focused-2 / focused-final | 107 / 109通过，exit0 | 中间版聚焦，非最终版全量替代 |
| red-ci-sidebar → green-ci-sidebar | 1失败/exit1 → TUI36通过/exit0 | 精确发布门闩，不加sleep |
| red-cold-start → green-cold-start | 1失败/exit1 → 1通过/exit0 | 新解释器真实Job归属 |
| focused-launch-1 | 77项/1错误/exit1 | 原fake Popen测试撞到新native恢复边界；仅适配fixture |
| focused-launch-final | **187通过，0skip，exit0** | unittest98.973s，墙钟100.088s |
| cold-lf-green / cold-exact-handles | 各1通过，exit0 | 最新探针规范化及精确句柄保留，墙钟0.700/1.054s |
| full-1 / full-final | 2272 / 2274项，各15skip、0失败/错误、exit0 | 中间提交全量，分别787.712/948.852s |
| full-launch-final | **2279项：2264通过、15skip、0失败/错误、exit0** | unittest1147.780s，墙钟1154.644s |
| compileall-launch-final | exit0，0.503s | src/tests编译检查 |

最终聚焦命令：

```powershell
$env:PYTHONPATH='src;tests'
$env:PYTHONDONTWRITEBYTECODE='1'
$env:PYTHONUTF8='1'
.venv/Scripts/python.exe -X utf8 -B runtime/ec29-afafcaf4/run.py focused-launch-final -m unittest test_eval_cleanup_ownership test_subprocess_control test_search_timeout_recovery test_session_ownership test_eval_runner test_eval_timing test_eval_quality_v3_review_fixes test_agent_async test_mcp_transport test_tui -v
.venv/Scripts/python.exe -X utf8 -B runtime/ec29-afafcaf4/run.py full-launch-final -m unittest discover -s tests -v
.venv/Scripts/python.exe -X utf8 -B runtime/ec29-afafcaf4/run.py compileall-launch-final -m compileall -q src tests
```

以上为已执行命令；若再次执行须换新label，避免独占日志冲突。原搜索P2的真实Agent搜索超时后5.2秒模型延迟、再搜索/compileall/finish，以及取消、未确认清理与owner负例包含在本轮聚焦和全量中。

本地最后一轮全量启动于e8f3b1e；运行中仅冷启动测试辅助脚本改为LF、并改为保留原句柄，所有生产文件不变。**不宣称这份本地全量覆盖了最新辅助脚本字节**；最新辅助脚本已单独GREEN，且最终d6d6c4c干净CI完整执行。具体哈希差异见`full-run-source-delta.json`。

本地15skip逐条身份/原因在`test-index-final.json`：11项本机链接权限限制，另4项为Windows锁文件替换/重命名、Linux抽象socket、控制字符文件名限制。CI具备链接权限，仅余后4项skip。skip不计为通过，未新增跳过条件。

## 干净CI

| 候选 / 原生run | 全量结果 | 编译 |
|---|---|---|
| ed0bf24 / 40805494315 | 2274项、1失败、4skip，496.856s | skipped；原失败保留 |
| e8f3b1e / 40809359141 | 2279项、2275通过、4skip，552.836s | success |
| dc09c12 / 40809631121 | 2279项、2275通过、4skip，526.083s | success |
| **d6d6c4c / 40810040358** | **2279项、2275通过、4skip、0失败/错误，577.536s** | **success** |

最终链接：https://github.com/PooooLish/Tricoder-Agent/actions/runs/40810040358 。每次对应不同提交，没有取消失败运行、无修改刷CI或修改workflow隐藏问题。各run原生metadata/full.log保存在证据目录。CI沿用原工作流的依赖安装步骤；本机没有安装依赖。

## 保全、增量与自审

- `before/`与`after/`、`before-sha256.json`与`after-sha256.json`、`delta-index.json`及`delta.patch`按实际文件字节生成；新增未跟踪文件也入索引。前后369/374文件，15项增量（10修改、5新增），无删除。`status-after.txt`记录交接文档尚未提交。
- `preservation-final.json`：3505份旧题库/指定历史证据hash全同。README、pyproject、AGENTS、CI、TaskCleanup、Session/Agent/策略及评分函数未修改。`source-preservation.json`单列无关源码/评分函数与旧交接正文保全。
- `aggregate-diff-check`对2807343..d6d6c4c为exit0；最终文档`git diff --check`另有独立日志。中间e8新测试辅助脚本CRLF导致check exit2但shell未阻止提交，已如实记录；dc09c12仅规范化该文件，AST相同，最终增量检查通过。
- 一次早期临时探针产生截断的控制台错误，完整日志缺失，未作为验收证据。它遗留的两个合成进程通过精确命令及创建时间确认归属后清理；空目录即时删除成功，详见`exploratory-owned-processes.json`和`exploratory-cleanup.json`。冷启动正式回归使用独占目录、精确句柄并保存完整结果。
- `self-review.md`为实施自审，`evidence-sha256.json`和`seal.json`为最终安全证据索引；不以此宣布独立验收。

## 限制与下一步

- 两次历史Windows3.11 CI的精确根因仍未知；新增诊断只能帮助以后发生时定位。没有改写旧成绩或以新成功消除旧失败事实。
- Windows Job进程列表捕获上限4096，查询/打开/等待/关闭失败保守保留资源；不是OS沙箱或任意恶意并发fork树的穷尽保证。全部native API错误组合、其他Windows版本和POSIX环境未穷尽验证。原期限耗尽时不会续发清理预算，未确认资源仍由owner阻断后续工作。
- 未调用真实模型、读取.env.local/凭据/用户库、安装本机依赖、修改权限/轮数/评分或推进Replan。未恢复Python3.12。无新增模型质量结论。
- 下一步只交独立reviewer核查源码、RED/GREEN与新CI；通过后再由reviewer/用户决定合并。当前main未合并，原阻塞报告与旧交接记录保留。
