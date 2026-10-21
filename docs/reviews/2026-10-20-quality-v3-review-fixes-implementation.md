# Quality V3 Q3-R1/R2/R3 修复实施记录

**实现完成，待独立复审。** 本记录是 coding 实施证据，不是独立验收。保留
`2026-10-20-quality-v3-rereview.md` 的暂不通过结论，Q3-R1/R2/R3 由 reviewer 继续核查。
没有运行真实模型、安装依赖、读取真实凭据/用户数据库或提交/推送。

## 范围与实际修改

按修复计划 Task1→2→3→4 单会话执行，使用 executing-plans/TDD；无子 Agent。
HEAD 只记录为参考，不作为 before；现有未提交/未跟踪内容全部保留。

| 需求 | 文件与位置 | 修复及独立正反例 |
| --- | --- | --- |
| Q3-R1 | `evals/quality-v3/cases/sf-aware-time-order/verifier/test_hidden.py`，三个 Behavior 方法 | 调用前独立 deepcopy expected，比较完整记录及嵌套 dict/list；稳定顺序、输入不变、无 id 输入。原记录、浅/深副本接受；id-only 与嵌套字段丢失拒绝。 |
| Q3-R2 | `evals/quality-v3/cases/cf-cli-result-contract/verifier/test_hidden.py`，test_boundary/test_regression | 直接 main 与实际 CLI 分别测 0/1/非法整数/3/4 参数；每次独立 stdout/stderr，错误返回整数2/退出2，空 stdout/非空 stderr。保留正常与负数。双入口不同步、忽略四参数均拒绝；整数枚举返回码接受。 |
| Q3-R3 cache | `evals/quality-v3/cases/cf-cache-copy-isolation/verifier/test_hidden.py:27` | 类级 autospec + 真实 get；assert_any_call 校验目标实例/key，允许多次/keyword/slots；绕过、错误实例和错误 key 拒绝。 |
| Q3-R3 package | `evals/quality-v3/cases/cf-package-api-migration/verifier/test_hidden.py:20` | 包新导出 wraps 真实函数、业务结果和至少一次调用；finally reload 恢复 report 绑定。接受多次/keyword/strip/空串快捷分支，core/旧别名绕过拒绝。 |
| 新回归 | `tests/test_eval_quality_v3_review_fixes.py` | 26 个独立真实隔离评分用例，逐项核对 modified_files、公开/隐藏子进程数量、退出码、失败分类；不接受导入错误、超时、清理失败、输出超限作为业务拒绝证据。 |
| 方法与交接 | V3 README/migration、本报告、project.md、reviewer-handoff | 原记录保留，追加本包方法、结果和限制。矩阵三组方法名映射仍成立，因此未改矩阵。 |

没有修改 src、任何 case.toml/workspace、suite/实验清单、题数/split/预算、公开需求、
默认权限、Agent 或 AND。原125变体及 helper 没有改动。

## actual before / after / delta 与保全

独占证据根：`runtime/quality-v3-review-fixes-20261020/implementation-ff3c0f67b3/`（下文 E）。

- `before/`：8份实际涉及的既有文件原始字节；`before-absence.json`记录两个新增文件原先不存在。
- `before-sha256.json`：907份源码/测试/文档/配置实际before；`after/`为10份交付文件完整内容。
- `file-index.json`、`after-sha256.json`：包含新增未跟踪文件；`delta.patch`从实际before生成，非HEAD diff。
- `protected-sha256.json`：11,088份历史文件，包括原实施、reviewer探针/评分/日志和受保护的旧真实证据。
- `supplemental-fixture-preservation.json`：初始源码扩展名过滤未收录的一份旧 `.log` fixture，与受保护历史before字节hash比对一致；它是保全检查，不是重跑成绩。
- `final-code-freeze-2.json`：最终四份verifier及新增测试冻结；`verification.json`核对非白名单零变化、历史保全、AST及空白检查。
- `git-status-before.txt` / `git-status-after.txt`；Git对已有`.pytest_cache`目录权限提示原样保存，不修改该目录。
- `evidence-sha256.json`：最终证据文件索引，不含真实凭据/用户数据库。旧历史成绩和原暂不通过报告不回写。

## RED / GREEN

每个标签对应 E 下同名 `.log`（完整输出）、`.json`（命令/退出码/墙钟时间）及独占收据目录。
没有把原reviewer的通过日志当作本次通过证据；既有已覆盖正例如实保持GREEN。

| 标签 | 测试 | 通过 | 失败/错误 | skip | unittest秒 / 墙钟秒 | exit |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| task1-red | 5 | 3 | 2/0 | 0 | 3.002 / 3.438 | 1 |
| task1-green | 5 | 5 | 0/0 | 0 | 2.954 / 3.234 | 0 |
| task2-red | 5 | 1 | 4/0 | 0 | 4.578 / 4.844 | 1 |
| task2-green | 5 | 5 | 0/0 | 0 | 4.930 / 5.218 | 0 |
| task3-red | 14 | 9 | 5/0 | 0 | 7.906 / 8.188 | 1 |
| task3-green | 14 | 14 | 0/0 | 0 | 7.838 / 8.110 | 0 |
| task3-selfreview-red | 1 | 0 | 1/0 | 0 | 1.538 / 1.828 | 1 |
| task2-selfreview-red | 1 | 0 | 1/0 | 0 | 1.894 / 2.203 | 1 |
| focused-final-2 | 26 | 26 | 0/0 | 0 | 17.517 / 17.828 | 0 |

Task1 RED是两种错误被误评分passed；Task2 RED是四种参数错误被误评分passed；
Task3 RED是五种合法实现被误拒。原一次调用、slots及缓存keyword已有支持，不虚构RED。
自审保留两次新增RED：逐输入强制调用误拒空串快捷分支；type身份比较误拒IntEnum。
前者改为跨业务样例观察目标使用，后者改为isinstance(int)，仍检查返回值2。
`focused-final-2`覆盖上述所有修正。工具脚本准备曾因`utf8-sig`编码名拼写退出1，
已更正为`utf-8-sig`并记ledger；不计业务测试RED。
首次封存脚本将列表型附加收据当作字典读取，退出1；原脚本、部分索引、快照与错误
保存在`audit-attempt-1/`，随后按字典收据过滤再封存。此修正只涉及runtime索引脚本，
不改变已经冻结并通过最终回归的验证器/测试。

首次 `focused-final`/`v3-final`/`eval-final` 是25个新增测试阶段，分别25/162/334项；
`full-final`启动后发生自审修正，实际2000项、exit0，仅保留中间日志，不作为最终冻结版本验收。
所有最终结果取 `*-final-2`，没有合并重复执行数冒充新增覆盖。

## 最终离线验收

运行环境为本机Windows、既有 `.venv/Scripts/python.exe -X utf8 -B`，仅测试子进程设置
`PYTHONPATH=src;tests`、`PYTHONDONTWRITEBYTECODE=1`、`TRICODER_V3_EVIDENCE=E/<独占标签>`。
`command-index.json`保留展开后的完整argv、cwd、环境、exit和耗时。

```powershell
# 每行用 E/run_checks.py LABEL 包装以独占记录；实际子命令如下
.venv/Scripts/python.exe -X utf8 -B -m unittest discover -s tests -p test_eval_quality_v3_review_fixes.py -v
.venv/Scripts/python.exe -X utf8 -B -m unittest discover -s tests -p 'test_eval_quality_v3*.py' -v
.venv/Scripts/python.exe -X utf8 -B -m unittest discover -s tests -p 'test_eval*.py' -v
.venv/Scripts/python.exe -X utf8 -B -m unittest discover -s tests -v
# NAME 分别 baseline / repeat3 / dev / holdout；四次实际执行
.venv/Scripts/python.exe -X utf8 -B -m tricoder eval --experiment evals/quality-v3/experiment-quality-NAME.toml --dry-run --no-color
git diff --check
```

| 最终标签 | 测试 | 通过 | 失败/错误 | skip | unittest秒 / 墙钟秒 | exit |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| focused-final-2 | 26 | 26 | 0/0 | 0 | 17.517 / 17.828 | 0 |
| v3-final-2 | 163 | 163 | 0/0 | 0 | 75.861 / 76.469 | 0 |
| eval-final-2 | 335 | 333 | 0/0 | 2 | 173.187 / 174.110 | 0 |
| full-final-2 | 2001 | 1986 | 0/0 | 15 | 503.593 / 508.625 | 0 |

| CLI dry-run | cases / trials | 墙钟秒 | exit |
| --- | ---: | ---: | ---: |
| baseline | 30 / 30 | 1.984 | 0 |
| repeat3 | 30 / 90 | 1.984 | 0 |
| dev | 22 / 22 | 1.921 | 0 |
| holdout | 8 / 8 | 1.921 | 0 |

没有真实Provider调用。dry-run的题库/实验配置始终未变。
原搜索P2的同任务5.2秒等待→search→compileall→finish，以及清理未确认停止/保留owner，
均在最终全量中实际通过；不据此重开或替reviewer关闭历史发现。

全量skip逐项如下（Eval中的两项已包含在内，不计通过）：

- `test_simple_unittest_target_rejects_symlink_escape (test_command_compatibility.CommandCompatibilityPolicyTests.test_simple_unittest_target_rejects_symlink_escape)`：'当前账户不能创建符号链接'
- `test_internal_symlink_remains_strict (test_create_file_recovery.CreateFileRecoveryTests.test_internal_symlink_remains_strict)`：'当前账户不能创建符号链接'
- `test_link_source_refused (test_delivery_budget_experiment.BudgetExperimentTests.test_link_source_refused)`：'当前账户不能创建符号链接'
- `test_binding_directory_identity_rejects_link (test_directory_tools.DirectoryProtocolFlowTests.test_binding_directory_identity_rejects_link)`："当前账户不能创建目录链接：[WinError 1314] 客户端没有所需的特权。: 'C:\\\\Users\\\\MaHong\\\\AppData\\\\Local\\\\Temp\\\\tmp20vyfnpy\\\\real' -> 'C:\\\\Users\\\\MaHong\\\\AppData\\\\Local\\\\Temp\\\\tmp20vyfnpy\\\\link'"
- `test_load_suite_rejects_symlinked_workspace_directory (test_eval_loader.EvalLoaderTests.test_load_suite_rejects_symlinked_workspace_directory)`：'当前账户不能创建符号链接'
- `test_prepare_workspace_rejects_symlinked_fixture_entry (test_eval_workspace.EvalWorkspaceTests.test_prepare_workspace_rejects_symlinked_fixture_entry)`：'当前账户不能创建符号链接'
- `test_rejects_symlink_or_reparse_executable (test_mcp_security.MCPLaunchSecurityTests.test_rejects_symlink_or_reparse_executable)`：'当前账户无权创建符号链接'
- `test_rejects_script_through_symlink_escape (test_policy.CommandPolicyWorkspaceTests.test_rejects_script_through_symlink_escape)`：'当前账户不能创建符号链接'
- `test_rejects_symlink_that_points_outside (test_policy.WorkspacePolicyTests.test_rejects_symlink_that_points_outside)`：'当前账户不能创建符号链接'
- `test_symlink_is_not_followed (test_verification_evidence.SnapshotTests.test_symlink_is_not_followed)`：'Windows 未授权符号链接'
- `test_guard_path_replacement_is_detected_before_next_lifecycle_boundary (test_workspace_lock.WorkspaceLockTests.test_guard_path_replacement_is_detected_before_next_lifecycle_boundary)`：'Windows 打开锁文件时不能替换守卫文件'
- `test_reparse_workspace_or_control_directory_is_rejected (test_workspace_lock.WorkspaceLockTests.test_reparse_workspace_or_control_directory_is_rejected)`：'当前 Windows 权限不允许创建目录链接'
- `test_replacing_both_lock_names_cannot_split_kernel_lock_domain (test_workspace_lock.WorkspaceLockTests.test_replacing_both_lock_names_cannot_split_kernel_lock_domain)`：'需要 Linux 抽象 socket 锁'
- `test_replacing_internal_control_directory_cannot_create_second_lock (test_workspace_lock.WorkspaceLockTests.test_replacing_internal_control_directory_cannot_create_second_lock)`：'Windows 打开锁文件时控制目录不能被重命名'
- `test_untrusted_filename_and_content_controls_are_escaped (test_workspace_snapshot.WorkspaceSnapshotTests.test_untrusted_filename_and_content_controls_are_escaped)`：'当前文件系统不允许控制字符文件名'

## 逐项变体与八例重放

原125项：35正确/合法接受，90初始/错误拒绝。新增26项：15合法接受，11错误拒绝。
`variant-index.json`逐项列出三个最终测试运行的case/variant/status/modified_files和收据hash；
原125项未删除或替换。新回归的公开测试均通过；业务拒绝须hidden exit1、
verification_failed且无基础设施error。这里仅合成Agent完成声明以测评分器，不代表模型交付率。

`replay_eight_final.py`是在新目录保留原候选实现与业务oracle、替换评分期望的探针副本；
没有修改原reviewer脚本。`reviewer-eight-replay-final/probes/*/reviewer-result.json`保存全部8例；
`cli-observations.json`另保存14组main/CLI真实返回值、stdout/stderr；错误候选独立oracle
继续exit1是预期业务错误，整个重放命令exit0。

| 原reviewer标签 | 修复后评分 | 独立业务oracle exit | 实际modified_files |
| --- | --- | ---: | --- |
| time-control | passed | 0 | time_order.py |
| time-drops-record-fields | failed | 1 | time_order.py |
| cli-control | passed | 0 | cli.py |
| cli-ignores-extra-arguments | failed | 1 | cli.py |
| cache-control | passed | 0 | cache.py, service.py |
| cache-valid-two-get-calls | passed | 0 | cache.py, service.py |
| package-control | passed | 0 | report.py, textkit/__init__.py |
| package-valid-two-export-calls | passed | 0 | report.py, textkit/__init__.py |

`verification.json`另核对新测试中8个原始候选字符串SHA与原探针副本相同。

## 自审、限制与下一步

- 实际增量限定8既有文件修改+2新增文件；旧src、所有case/workspace和配置、原125项及历史证据hash保全。
- 只验证有限行为变体；调用spy证明观察到目标使用和正确结果，不证明任意程序的数据流或抵抗恶意验证器绕过。
- 仍为文件级评分，无目录级审计；只在当前Windows/Python环境验证，链接权限skip不算通过，未做其他平台测试。
- 未重新评估真实模型、网络故障、长期记忆或新质量成功率。新旧benchmark不适合直接归因Agent收益。
- 本包不替独立reviewer关闭Q3-R1/R2/R3。停止实施，交回独立复审；通过后由reviewer按既有条件授权继续30题DeepSeek flash真实复测。
