# 2026-10-20 交付修复实施记录

状态：**实现完成，待独立复审**。这是 coding 作者的实施与自审记录，不是独立验收；原 F1/F2/F3 发现和 reviewer 历史结论保留，不由本记录关闭。

证据根目录：`runtime/delivery-fixes-20261020/implementation-334076fa24/`（下文证据路径均相对此目录）。按 Agent 包→固定源码快照→Eval 包顺序执行，单会话无子 Agent；没有读取 .env.local/真实凭据/用户数据库，没有安装、提交、推送或新增真实模型调用。

## 实际改动

| 任务 | 位置 | 变化与保持的边界 |
| --- | --- | --- |
| A1/F1 | `src/tricoder/engine/verification_feedback.py:49,162`、`state.py:85`、`loop.py` 初始化 | 反馈读取现有 scope、快照、检查 authority、失败/过期与覆盖并集。旧失败尚在时给 pending 和 stale_checks/unresolved_checks，新增固定 validation_status/count；pending_paths 可以为空。不执行第二次完成判定、不清空失败、不创建 scope，路径/长度上限保留。 |
| A2/F2 | `src/tricoder/session/runtime.py:1568` | attributable=true 但未完成时准确提示“任务尚未完整交付”；外部未归属、未知影响、清理问题保留各自诊断。失败仍撤销证据、保留 pending/下次确认，不推进基线。 |
| A3 | `src/tricoder/tools/write.py:291`、`execution_state.py` | 仅审批前通过策略检查的已存在普通文件返回 invalid_argument/replan/file_already_exists/NONE，0审批、原字节不变。原生及 legacy 同任务改用 edit→检查→finish 能完成。目录/链接/敏感/越界/只读及审批后竞态保持严格分支。 |
| A4 | `patches.py:36,228`、`tools/write.py:521`、`protocols.py` | 仅已确定的 EOF hunk 计数不匹配给白名单整数，最多5项、0..1000000；未确定时省略。不含路径/源码/异常正文、不拓展语法。说明新建/编辑选择、零测试、定向发现和修改后重验。 |
| B1/F3 | `execution_state.py:8`、`models.py:379`、`engine/completion.py`、`loop.py:560`、`finalization.py`、`telemetry.py`、`audit.py`、`agent.py`、`session/runtime.py` | RunResult 末尾追加可选 TaskStopReason；真实分支赋值，旧调用 None。finish outcome 仅白名单或省略默认 completed；finish_assessment 读取唯一判定及计数。cancel/cleanup/UNKNOWN 优先，原成功后被 Runtime 否决才记 runtime_rejected。不凭摘要推断，审计失败仍停止。 |
| B2 | `evals/models.py:159`、`service.py:117,198,209`、`report.py:291,766` | 5个可选逐turn字段，严格类型和枚举；旧缺失unknown。observer仅取本turn成功执行的finish声明，每turn重置。独立诊断表逐行完整身份，声明与最终ok分列；AND、评分事实及JSON schema版本不变。 |
| B3 | 新 `evals/quality-v2.2/` | 从实际before的quality-v2复制：id=quality-v2-2、benchmark=quality-v2.2、19quality/11contract，19个workspace_root不变。仅quality公开turn加非交互说明，3个memory首轮加只确认契约。全部workspace/verifier及11contract逐字保留，无未来turn泄漏。 |
| B4 | 新 `scripts/prepare_delivery_budget_experiment.py:38` | 只生成项目内全新runtime目录，拒绝已存在/越界/链接/非白名单文件，不读配置、不执行模型。两组同源码、题库、模型、seed、记忆与调度预算；仅19题max_rounds从8变16及必要实验id不同。严格比较器仍拒绝跨预算/benchmark。 |

新增/扩展回归：`tests/delivery_helpers.py`、`test_verification_feedback_consistency.py`、`test_runtime_delivery_diagnostics.py`、`test_create_file_recovery.py`、`test_patch_feedback.py`、`test_delivery_stop_diagnostics.py`、`test_eval_diagnostics.py`、`test_eval_quality_v22_suite.py`、`test_delivery_budget_experiment.py`，以及 `test_agent.py:2279` 的已有普通文件恢复契约。核心路径使用真实 SessionRuntime/工具和合成 Provider，新核心测试数据库仅建在隔离 runtime；没有用 mock 最终 RunResult 代替门禁验收。

## 前态、分包与完整索引

- `before/`、`before-sha256.json`、`status-before.txt`：572份当时实际安全文件。HEAD不是before，未重建或覆盖既有未提交内容。
- `package-a-source/`、`package-a-sha256.json`：第一包13场景和48项安全回归完成后、第二包开始前固定的源码/测试。
- `package-a-delta.patch`：实际before→第一包；`package-b-delta.patch`：第一包→最终（其他文件从实际before）；`delta.patch`：整体实际增量。
- `after/`、`after-sha256.json`、`changed-files.json`、`file-index.json`：最终文件内容、哈希及完整相关文件索引，含新未跟踪文件，新增文件before=null。`status-after.txt`记录最终Git状态。
- `evidence-index.json`列出本轮证据文件哈希（不索引自身）。原reviewer目录、冻结A/B与旧真实run未覆盖。`compatibility.json`核对2057份保护证据、255份旧题库文件不变。

## 验证说明

逐任务RED、校准过程、中间失败与GREEN全部保留。组间重叠，不求和为独立测试总数；失败/跳过不计通过。完整命令、退出码、墙钟耗时在各 `*-command.json`，原始输出在同名 `.log`；汇总见文末和 `test-results.json`。旧reviewer的78项不计入本轮。

夹具/命令错误未隐去：A1 legacy缺reason在校准RED前修正；A2改为断言既有WorkspaceOperationCancelled；B1校准cancel_current、ToolAction参数及避免重复读取守卫的操作序列。B1首次GREEN发现固定reason被审计器变成chars，补固定枚举白名单后93项通过。B3首次GREEN用了不存在的模块名，改为实际public_contract/path_matching后18项通过，无生产修补掩盖此失败。

首次完整回归为1815项/544.044秒/1失败/15跳过：旧 `test_agent.py` 要求create-existing终止，与本轮A3的明确恢复契约冲突。只更新此公开用例的预期，并补错误分类检查，保留原文件字节与既有验证状态断言；没有改生产逻辑、删除测试或改隐藏断言。该用例首次更新漏取JSON的tool_result外层，日志保留；修正后6项/1.393秒/1跳过通过，随后重新执行完整回归。

原搜索P2在 `a-safety`、`final-focused`及完整回归中实际执行：同一CodingAgent正则超时后模型等待至少5.2秒→再搜索→compileall→finish成功；scope未更换、无遗失资源。清理未确认则停止并保留原owner，禁止复用；取消、已有终止deadline语义保留。额外16轮不能消除真实旧失败或清理问题。

语法检查实际命令为 `.venv/Scripts/python.exe -X utf8 -X pycache_prefix=runtime/delivery-fixes-20261020/implementation-334076fa24/compile-cache -m compileall -q src tests scripts`，exit0，缓存仅落本轮runtime。Git及未跟踪文件空白检查另有独立日志。

## 历史兼容、题库与实验

旧原生run由当前解析器只读加载、渲染到本轮目录，原JSON字节和成绩不改：quality-v1 `20261019t091216.781907z`=6 passed；A `20261019t113749.503913z`=1；B `20261019t115744.613637z`=1；quality-v2.1 `20261020t014325.739761z`=2。缺新诊断字段保留unknown，未回填猜测值。

最新真实成绩仍为产物15/19、完整2/19、范围/清理各19/19。新quality-v2.2尚无真实成绩；原quality-v2目录仍为benchmark quality-v2.1。新版本11contract及全部夹具/verifier字节一致，精确允许的题面差异由新测试及来源hash验证。

`prepare-budget-command.json`记录生成命令，输出为 `budget-experiment/`。共用 `code/` 的119份源码快照和102份题库文件；`source-sha256.json`、`allowed-delta.json`及 `budget-validation.json`核对只有19个case预算差异。两组都是DeepSeek deepseek-v4-flash、seed23、memory off/off、每题一次、调度预算3600秒。

新主manifest、budget8、budget16的CLI dry-run均exit0、计划19trials；suite包含30cases，但11contract不冒充真实质量题。命令见 `dry-run-v22-command.json`、`dry-run-budget8-command.json`、`dry-run-budget16-command.json`。本轮不执行38次真实调用。严格比较器拒绝预算指纹不同是预期，没有放宽比较器。

## 自审与限制

作者针对实际增量做了分包自审，没有提高默认权限/轮数，未改AND、隐藏verifier、评分事实、Git helper、普通命令策略、资源释放顺序或失败清理逻辑。现有scope引用与audit枚举白名单属于必要接线，不新增authority；未知诊断None和原aggregate逻辑保留。

Windows不支持的符号链接/POSIX边界测试按日志跳过，其跨平台真实行为未在本机验证。评分快照仍为文件级，未扩展目录级审计。生成脚本失败可能留下本次新目录的部分文件，重试需新目录；不保证源题库被并发修改时的快照原子性，未来执行前应核对冻结源码hash。

同一合成9轮序列8轮停止/16轮成功只证明预算机制，真实收益未知，不宣称提高完整率或统计显著。历史mm-latest-correction的完整命令/outcome未保存，重建序列不是精确历史还原；历史policy_denied具体触发仍未知。缺乏可信事实的审计/记忆失败保留stop_reason=None，不制造原因。

保持原发现和历史记录，等待用户交回独立reviewer；不自行关闭问题、不继续Replan、记忆或预算优化等其他事项。

## 本轮实际测试清单

| 日志 | tests | unittest秒 | 实际结果 | exit |
| --- | ---: | ---: | --- | ---: |
| a-safety | 48 | 14.62 | OK | 0 |
| a1-green | 71 | 30.555 | OK | 0 |
| a1-red-calibrated | 6 | 6.069 | FAILED (failures=3, errors=1) | 1 |
| a1-red | 6 | 4.832 | FAILED (failures=4, errors=1) | 1 |
| a2-green | 76 | 30.628 | FAILED (errors=1) | 1 |
| a2-green-final | 76 | 30.942 | OK | 0 |
| a2-red | 4 | 2.128 | FAILED (failures=2) | 1 |
| a3-green | 164 | 13.455 | OK (skipped=2) | 0 |
| a3-red | 5 | 0.565 | FAILED (failures=3, skipped=1) | 1 |
| a4-green | 25 | 0.723 | OK | 0 |
| a4-red | 8 | 0.629 | FAILED (failures=2) | 1 |
| b1-green | 87 | 27.349 | FAILED (errors=2) | 1 |
| b1-green-final | 93 | 28.276 | OK | 0 |
| b1-red-calibrated | 7 | 2.337 | FAILED (failures=8, errors=1) | 1 |
| b1-red | 7 | 2.587 | FAILED (failures=6, errors=3) | 1 |
| b2-green | 52 | 37.441 | OK (skipped=1) | 0 |
| b2-red | 10 | 17.278 | FAILED (errors=6) | 1 |
| b3-green | 7 | 23.639 | FAILED (errors=1) | 1 |
| b3-green-final | 18 | 52.181 | OK | 0 |
| b3-red | 2 | 0.79 | FAILED (errors=2) | 1 |
| b4-green | 7 | 13.714 | OK (skipped=1) | 0 |
| b4-red | 4 | 0.002 | FAILED (errors=4) | 1 |
| create-legacy-contract-green | 6 | 1.5 | FAILED (errors=1, skipped=1) | 1 |
| create-legacy-contract-green-final | 6 | 1.393 | OK (skipped=1) | 0 |
| final-focused | 115 | 99.511 | OK (skipped=3) | 0 |
| full-regression | 1815 | 544.044 | FAILED (failures=1, skipped=15) | 1 |
| full-regression-final | 1815 | 511.405 | OK (skipped=15) | 0 |

完整回归实际命令：`.venv/Scripts/python.exe -X utf8 -B -m unittest discover -s tests -v`。全部逐组原始命令如下（runner设置PYTHONDONTWRITEBYTECODE=1）：

```text
a-safety: ["D:\\MaHong\\AGENT_WORKSPACE_V2\\projects\\tricoder-cli\\.venv\\Scripts\\python.exe", "-X", "utf8", "-B", "-c", "import sys,unittest; sys.path.insert(0,'tests'); result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:])); sys.exit(not result.wasSuccessful())", "test_search_timeout_recovery", "test_subprocess_control", "test_batch_failure", "test_cancellation"]
a1-green: ["D:\\MaHong\\AGENT_WORKSPACE_V2\\projects\\tricoder-cli\\.venv\\Scripts\\python.exe", "-X", "utf8", "-B", "-c", "import sys,unittest; sys.path.insert(0,'tests'); result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:])); sys.exit(not result.wasSuccessful())", "test_verification_feedback_consistency", "test_task_validation", "test_finish_coverage", "test_finish_verification_recovery"]
a1-red-calibrated: ["D:\\MaHong\\AGENT_WORKSPACE_V2\\projects\\tricoder-cli\\.venv\\Scripts\\python.exe", "-X", "utf8", "-B", "-c", "import sys,unittest; sys.path.insert(0,'tests'); result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:])); sys.exit(not result.wasSuccessful())", "test_verification_feedback_consistency"]
a1-red: ["D:\\MaHong\\AGENT_WORKSPACE_V2\\projects\\tricoder-cli\\.venv\\Scripts\\python.exe", "-X", "utf8", "-B", "-c", "import sys,unittest; sys.path.insert(0,'tests'); result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:])); sys.exit(not result.wasSuccessful())", "test_verification_feedback_consistency"]
a2-green: ["D:\\MaHong\\AGENT_WORKSPACE_V2\\projects\\tricoder-cli\\.venv\\Scripts\\python.exe", "-X", "utf8", "-B", "-c", "import sys,unittest; sys.path.insert(0,'tests'); result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:])); sys.exit(not result.wasSuccessful())", "test_runtime_delivery_diagnostics", "test_session_runtime", "test_workspace_gate"]
a2-green-final: ["D:\\MaHong\\AGENT_WORKSPACE_V2\\projects\\tricoder-cli\\.venv\\Scripts\\python.exe", "-X", "utf8", "-B", "-c", "import sys,unittest; sys.path.insert(0,'tests'); result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:])); sys.exit(not result.wasSuccessful())", "test_runtime_delivery_diagnostics", "test_session_runtime", "test_workspace_gate"]
a2-red: ["D:\\MaHong\\AGENT_WORKSPACE_V2\\projects\\tricoder-cli\\.venv\\Scripts\\python.exe", "-X", "utf8", "-B", "-c", "import sys,unittest; sys.path.insert(0,'tests'); result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:])); sys.exit(not result.wasSuccessful())", "test_runtime_delivery_diagnostics"]
a3-green: ["D:\\MaHong\\AGENT_WORKSPACE_V2\\projects\\tricoder-cli\\.venv\\Scripts\\python.exe", "-X", "utf8", "-B", "-c", "import sys,unittest; sys.path.insert(0,'tests'); result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:])); sys.exit(not result.wasSuccessful())", "test_create_file_recovery", "test_tools", "test_tool_errors", "test_directory_tools"]
a3-red: ["D:\\MaHong\\AGENT_WORKSPACE_V2\\projects\\tricoder-cli\\.venv\\Scripts\\python.exe", "-X", "utf8", "-B", "-c", "import sys,unittest; sys.path.insert(0,'tests'); result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:])); sys.exit(not result.wasSuccessful())", "test_create_file_recovery"]
a4-green: ["D:\\MaHong\\AGENT_WORKSPACE_V2\\projects\\tricoder-cli\\.venv\\Scripts\\python.exe", "-X", "utf8", "-B", "-c", "import sys,unittest; sys.path.insert(0,'tests'); result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:])); sys.exit(not result.wasSuccessful())", "test_patch_feedback", "test_patches", "test_protocols"]
a4-red: ["D:\\MaHong\\AGENT_WORKSPACE_V2\\projects\\tricoder-cli\\.venv\\Scripts\\python.exe", "-X", "utf8", "-B", "-c", "import sys,unittest; sys.path.insert(0,'tests'); result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:])); sys.exit(not result.wasSuccessful())", "test_patch_feedback"]
b1-green: ["D:\\MaHong\\AGENT_WORKSPACE_V2\\projects\\tricoder-cli\\.venv\\Scripts\\python.exe", "-X", "utf8", "-B", "-c", "import sys,unittest; sys.path.insert(0,'tests'); result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:])); sys.exit(not result.wasSuccessful())", "test_delivery_stop_diagnostics", "test_finish_verification_recovery", "test_finish_coverage", "test_runtime_delivery_diagnostics", "test_cancellation", "test_batch_failure"]
b1-green-final: ["D:\\MaHong\\AGENT_WORKSPACE_V2\\projects\\tricoder-cli\\.venv\\Scripts\\python.exe", "-X", "utf8", "-B", "-c", "import sys,unittest; sys.path.insert(0,'tests'); result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:])); sys.exit(not result.wasSuccessful())", "test_delivery_stop_diagnostics", "test_finish_verification_recovery", "test_finish_coverage", "test_runtime_delivery_diagnostics", "test_cancellation", "test_batch_failure", "test_audit"]
b1-red-calibrated: ["D:\\MaHong\\AGENT_WORKSPACE_V2\\projects\\tricoder-cli\\.venv\\Scripts\\python.exe", "-X", "utf8", "-B", "-c", "import sys,unittest; sys.path.insert(0,'tests'); result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:])); sys.exit(not result.wasSuccessful())", "test_delivery_stop_diagnostics"]
b1-red: ["D:\\MaHong\\AGENT_WORKSPACE_V2\\projects\\tricoder-cli\\.venv\\Scripts\\python.exe", "-X", "utf8", "-B", "-c", "import sys,unittest; sys.path.insert(0,'tests'); result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:])); sys.exit(not result.wasSuccessful())", "test_delivery_stop_diagnostics"]
b2-green: ["D:\\MaHong\\AGENT_WORKSPACE_V2\\projects\\tricoder-cli\\.venv\\Scripts\\python.exe", "-X", "utf8", "-B", "-c", "import sys,unittest; sys.path.insert(0,'tests'); result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:])); sys.exit(not result.wasSuccessful())", "test_eval_diagnostics", "test_eval_report", "test_eval_scenarios", "test_eval_loader", "test_eval_compare"]
b2-red: ["D:\\MaHong\\AGENT_WORKSPACE_V2\\projects\\tricoder-cli\\.venv\\Scripts\\python.exe", "-X", "utf8", "-B", "-c", "import sys,unittest; sys.path.insert(0,'tests'); result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:])); sys.exit(not result.wasSuccessful())", "test_eval_diagnostics"]
b3-green: ["D:\\MaHong\\AGENT_WORKSPACE_V2\\projects\\tricoder-cli\\.venv\\Scripts\\python.exe", "-X", "utf8", "-B", "-c", "import sys,unittest; sys.path.insert(0,'tests'); result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:])); sys.exit(not result.wasSuccessful())", "test_eval_quality_v22_suite", "test_eval_quality_v2_suite", "test_eval_scope_contract"]
b3-green-final: ["D:\\MaHong\\AGENT_WORKSPACE_V2\\projects\\tricoder-cli\\.venv\\Scripts\\python.exe", "-X", "utf8", "-B", "-c", "import sys,unittest; sys.path.insert(0,'tests'); result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:])); sys.exit(not result.wasSuccessful())", "test_eval_quality_v22_suite", "test_eval_quality_v2_suite", "test_eval_public_contract", "test_eval_path_matching"]
b3-red: ["D:\\MaHong\\AGENT_WORKSPACE_V2\\projects\\tricoder-cli\\.venv\\Scripts\\python.exe", "-X", "utf8", "-B", "-c", "import sys,unittest; sys.path.insert(0,'tests'); result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:])); sys.exit(not result.wasSuccessful())", "test_eval_quality_v22_suite"]
b4-green: ["D:\\MaHong\\AGENT_WORKSPACE_V2\\projects\\tricoder-cli\\.venv\\Scripts\\python.exe", "-X", "utf8", "-B", "-c", "import sys,unittest; sys.path.insert(0,'tests'); result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:])); sys.exit(not result.wasSuccessful())", "test_delivery_budget_experiment", "test_eval_compare"]
b4-red: ["D:\\MaHong\\AGENT_WORKSPACE_V2\\projects\\tricoder-cli\\.venv\\Scripts\\python.exe", "-X", "utf8", "-B", "-c", "import sys,unittest; sys.path.insert(0,'tests'); result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:])); sys.exit(not result.wasSuccessful())", "test_delivery_budget_experiment"]
create-legacy-contract-green: ["D:\\MaHong\\AGENT_WORKSPACE_V2\\projects\\tricoder-cli\\.venv\\Scripts\\python.exe", "-X", "utf8", "-B", "-c", "import sys,unittest; sys.path.insert(0,'tests'); result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:])); sys.exit(not result.wasSuccessful())", "test_agent.AgentTests.test_failed_create_does_not_invalidate_previous_successful_verification", "test_create_file_recovery"]
create-legacy-contract-green-final: ["D:\\MaHong\\AGENT_WORKSPACE_V2\\projects\\tricoder-cli\\.venv\\Scripts\\python.exe", "-X", "utf8", "-B", "-c", "import sys,unittest; sys.path.insert(0,'tests'); result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:])); sys.exit(not result.wasSuccessful())", "test_agent.AgentTests.test_failed_create_does_not_invalidate_previous_successful_verification", "test_create_file_recovery"]
final-focused: ["D:\\MaHong\\AGENT_WORKSPACE_V2\\projects\\tricoder-cli\\.venv\\Scripts\\python.exe", "-X", "utf8", "-B", "-c", "import sys,unittest; sys.path.insert(0,'tests'); result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:])); sys.exit(not result.wasSuccessful())", "test_delivery_stop_diagnostics", "test_verification_feedback_consistency", "test_runtime_delivery_diagnostics", "test_create_file_recovery", "test_patch_feedback", "test_eval_diagnostics", "test_eval_report", "test_eval_scenarios", "test_eval_loader", "test_eval_compare", "test_eval_quality_v22_suite", "test_delivery_budget_experiment", "test_search_timeout_recovery", "test_batch_failure"]
full-regression: [".venv/Scripts/python.exe", "-X", "utf8", "-B", "-m", "unittest", "discover", "-s", "tests", "-v"]
full-regression-final: [".venv/Scripts/python.exe", "-X", "utf8", "-B", "-m", "unittest", "discover", "-s", "tests", "-v"]
```

完整回归跳过名称/原因（原日志逐行）：

```text
本地文件简写不能把指向工作区外的链接转换成可执行测试目标。 ... skipped '当前账户不能创建符号链接'
test_internal_symlink_remains_strict (test_create_file_recovery.CreateFileRecoveryTests.test_internal_symlink_remains_strict) ... skipped '当前账户不能创建符号链接'
test_link_source_refused (test_delivery_budget_experiment.BudgetExperimentTests.test_link_source_refused) ... skipped '当前账户不能创建符号链接'
目录身份读取不得跟随符号链接或 Windows reparse point。 ... skipped "当前账户不能创建目录链接：[WinError 1314] 客户端没有所需的特权。: 'C:\\\\Users\\\\MaHong\\\\AppData\\\\Local\\\\Temp\\\\tmpv111n43n\\\\real' -> 'C:\\\\Users\\\\MaHong\\\\AppData\\\\Local\\\\Temp\\\\tmpv111n43n\\\\link'"
防止 workspace fixture 通过符号链接指向 suite 外部。 ... skipped '当前账户不能创建符号链接'
防止递归复制跟随 fixture 内的符号链接离开评测目录。 ... skipped '当前账户不能创建符号链接'
PATH 中的符号链接/reparse 可把纯名称重定向到未审批程序。 ... skipped '当前账户无权创建符号链接'
符号链接指向工作区外时，脚本路径必须被拒绝。 ... skipped '当前账户不能创建符号链接'
防止符号链接绕过工作区边界。 ... skipped '当前账户不能创建符号链接'
test_symlink_is_not_followed (test_verification_evidence.SnapshotTests.test_symlink_is_not_followed) ... skipped 'Windows 未授权符号链接'
外部守卫的名称若改指向新 inode，当前持有者必须停止继续执行。 ... skipped 'Windows 打开锁文件时不能替换守卫文件'
若锁路径跟随链接，两个拼写可落到不同锁文件并绕过互斥。 ... skipped '当前 Windows 权限不允许创建目录链接'
即使两个文件系统锁都被移出名称空间，第二个合规进程仍不能进入。 ... skipped '需要 Linux 抽象 socket 锁'
POSIX 内部锁 inode 被移走后，外部守卫仍阻止第二个合规进程。 ... skipped 'Windows 打开锁文件时控制目录不能被重命名'
若原始控制字符进入终端，diff 可伪造界面或隐藏确认内容。 ... skipped '当前文件系统不允许控制字符文件名'
```

## 13场景实施后结果

两包分别复制探针到本轮目录、更新ROOT/OUT再执行，原reviewer文件不改。以下为第二包逐turn事实。

| 场景 | ok | rounds | 逐turn停止原因 |
| --- | --- | ---: | --- |
| ack_only | True | 1 | completed |
| ack_then_work | True | 4 | completed, completed |
| ask_then_work | False | 4 | needs_input, completed |
| reconstructed_completed | False | 7 | verification_blocked |
| reconstructed_incomplete | False | 7 | model_incomplete |
| reconstructed_compile | False | 8 | verification_blocked |
| targeted_tests_then_compile | True | 6 | completed |
| budget_8 | False | 8 | verification_blocked |
| retry_exact_failed_check | True | 8 | completed |
| same_sequence_budget8 | False | 8 | round_budget_exhausted |
| same_sequence_budget16 | True | 9 | completed |
| budget_attribution | False | 2 | round_budget_exhausted |
| create-existing | tool=False | - | invalid_argument/replan/file_already_exists，0审批、原字节不变 |

13场景断言均符合预期（包含应失败的负例），不等于13个任务都成功。`budget_8`沿用旧探针名称，实际是第8轮finish验证被拒，不能仅按rounds猜预算。F1保持pending/stale_checks、unresolved=1、pending_paths=[]，同命令重试才解除；ask_then_work前轮失败仍整体False；budget_attribution捕获attributable=true且不误报未归属。

证据：`package-a-probes/verification.json`、`package-b-probes/verification.json`及各自attempt3；`scenarios-summary.json`；`multi-trial-example.json/.md`逐行身份及旧unknown检查。
