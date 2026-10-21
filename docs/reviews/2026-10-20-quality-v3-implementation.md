# Quality V3 离线建设实施记录

**状态：实现完成，待独立复审。** 本记录由 coding 实施者编写，不代表独立验收；本轮未调用真实模型，不自行关闭新发现。

执行依据为 `docs/superpowers/plans/2026-10-20-quality-v3-expansion.md`，
Task 1→2→3→4，单会话使用 executing-plans/TDD 实施。
用户明确禁止派生、迁移 dirty worktree、提交和删除证据，故覆盖技能中对应默认步骤。
没有生产框架变更，无依赖安装、凭据/用户数据库读取或真实 Provider 调用。

所有本轮证据均在：
`runtime/quality-v3-expansion-20261020/implementation-b29568ac93/`（下称 E）。
HEAD 未作为 before；实施前复制 696 份白名单实际文件并保存 SHA256、git status，
另保存旧 quality-v2.2 全部 102 个普通文件哈希和 7,880 份受保护历史证据哈希。
旧日志未重跑覆盖。

## 具体交付

| 任务 | 实际文件与行为 | 主要证据 |
| --- | --- | --- |
| Task 1 | 新 `evals/quality-v3/suite.toml`、四份实验清单、matrix、migration、来源hash；30 quality/0 contract，22 dev/8 holdout，类别8/11/4/4/3，每turn16/16000 | task1-red、task1-green、task1-green-2；suite测试 |
| Task 2 | 新套件内原19题的公开契约、README、tests包、公开与隐藏验证；按公开输入域补边界；原19全部dev；module-interface允许定义/调用方二选一修复；provider计数题转single_file无故障 | task2-red、task2-green；old-verifier-coverage.json |
| Task 3 | 新增11题固定接口及真实CLI、包导入、缓存嵌套隔离、CSV流水线；前3dev、后8holdout；partial-revision两轮契约 | task3-batch1/2/3-red与green日志；focused-final逐项JSON |
| Task 4 | 四个新测试文件；真实隔离评分、Runtime顺序/AND、scope、无finish、首轮消息隔离、故障层次；README和交接 | task4-red系列、task4-green、focused-final；完整日志及索引 |

四个新增测试文件：

- `tests/quality_v3_support.py`：正确/错误/替代实现只供测试；真实复制、隐藏验证、评分及进程收据捕获。包装生产进程函数仅记录结果，未替换执行结果。
- `tests/test_eval_quality_v3_suite.py`：数量/版本/预算/分组/类别/实验计划、来源哈希、资源边界和需求映射。
- `tests/test_eval_quality_v3_verifiers.py`：动态创建独立顶层测试，每个变体独立副本和子进程，125项。
- `tests/test_eval_quality_v3_contract.py`：真实 Runtime、多轮 AND、首轮可见内容、无finish、范围及故障边界；另补缓存service绕过get的真实评分反例。

矩阵 business_contract 对应隐藏 normal/boundary/regression 三组，
public_baseline 对应公开基础例子，不把基础例子称为全契约覆盖。
memory 的首轮文件仅包含当时已知或两轮共同成立的行为；最终修正只在第二轮题面和隐藏验证出现。
实际 Runtime 的 Provider 消息与 read_file 结果确认隔离，不能只靠静态题面字符串断言。

## 逐题变体验收

30×4=120 项基础变体：initial、correct、wrong_a、wrong_b。
另外 CSV/dialect、稳定去重/scan、旧API/wrapper、module-interface/caller 加上缓存/slots，共5项合法替代实现。
各项都走真实 `load_suite`、隔离复制、`install_verifier`、`_run_verification` 和评分入口；
并检查实际 `modified_files`、子进程退出码、测试数、超时/清理/输出上限。

最终逐项结果见 E/variant-acceptance-index.json 及其指向的 focused-final-2 JSON：
初始与两类错误变体共90项按预期被隐藏测试拒绝，30项正确解和5项替代解均通过公开与隐藏测试。
每项隐藏验证有3个顶层行为组；35项可接受实现另实际发现并通过1项公开测试。
这125项是离线变体验收，不是模型trial，不计入真实质量成绩。
新增缓存service绕过get反例另外统计，不混入基础120项。

CLI题启动实际 Python 子进程检查退出码、stdout、stderr；JSONL还校验输入字节未改；
包迁移导入真实包/调用方，并观察新导出被调用；cache真实存取嵌套dict/list，get spy在类上记录并执行真实调用，兼容slots，不替换业务返回值。

范围负例逐项核对实际修改文件：

- 只改 tests：隐藏业务失败且 required_change_missing。
- 正确源码加越界文件/嵌套同名 tests：隐藏通过但 change_out_of_scope。
- 正确源码加允许的 tests 直接子文件：通过。
- 正确产物不finish：真实Runtime停止为 progress_stopped（重复读取保护），保留 agent_failed、agent_unverified，隐藏仍通过；不是伪造16轮预算成功。
- partial-revision 首轮实际输出 `P:MiXeD`，随后真实工具修为strip+lower且忽略prefix；正常两轮均成功。
- 同一序列首轮主动incomplete、第二轮成功：最终隐藏通过，整体仍 agent_failed，保留第一轮事实。

## RED/GREEN 与中途问题

命令前缀统一为 `.venv/Scripts/python.exe -X utf8 -B`；
证据 runner 只给子进程设置 `PYTHONPATH=src;tests`、禁止pyc、指定独占日志目录。
每次完整 argv、cwd、墙钟耗时和退出码见 E/commands-index.json 及同名 `.json/.log`。

| 阶段 | 实际结果 |
| --- | --- |
| task1-red | 3项，3失败：新suite不存在；exit1 |
| task1-green | 3项，1错误：测试将dimensions tuple误当对象；不是产品缺陷 |
| task1-green-2 | 3项通过，7.868秒，exit0 |
| task2-red | 79项，40失败：22个正确/替代解缺公开发现，18个错误变体被旧隐藏放过；另20个错误变体已有覆盖，不虚构RED |
| task2-green | 79项通过，30.224秒，exit0 |
| task3三批RED | 13/16/16项，每批4失败：正确实现仍被待实施的行为verifier拒绝，不是导入错误 |
| task3批1/2 GREEN | 13/16项通过，5.633/8.162秒，exit0 |
| task3批3首次GREEN | 16项中1失败：Windows JSONL CLI输出编码与读取编码不一致；保留日志 |
| task3批3 GREEN-2 | 隐藏CLI测试显式以UTF8运行子进程，16项通过，8.978秒，exit0 |
| task4-red系列 | 缺suite文档是有效RED；其余工具错误字段、宿主覆盖、无finish验证状态等为测试脚本假设错误，未当作生产修复依据 |
| task4-green | 11项通过，39.990秒，exit0；原scope/AND/故障等门禁无需修改即可通过 |
| focused首次 | 151次发现，16错误：生成器类别名被unittest再次发现，独占日志拒绝覆盖；移除别名，未删行为测试 |
| self-review-cache-red/green | 新真实评分反例先被放过（1失败，exit1），补公开get调用契约断言后1通过、exit0 |
| focused-final（首次） | 136项通过，0失败/错误/skip，58.614秒；墙钟59.250秒，exit0；之后追加slots合法变体 |
| self-review-slots-red/green | 真实隔离脚本exit1复现合法slots被实例spy误拒；改类级真实调用观察后exit0，公开1项/隐藏3项通过；并保留为第五项合法替代实现 |

旧隐藏覆盖探针针对新套件中尚未修改的旧verifier副本执行，其内容与旧版本一致；
未改旧suite或回写旧成绩。Task3骨架RED只证明尚未实现验证契约，
不能冒充旧生产功能缺陷；最终90项真实错误变体拒绝证据另列。

Runtime脚本最初只有tests定向发现，当前宿主不会据此认可根源码覆盖；
已按既有规则追加目标compileall，业务仍由公开/隐藏测试验证。
没有扩大权限、修改Agent、清空失败或改变AND以通过。

另外尝试了slots缓存配合`cache.get(key=key)`的合法调用反例：
`self-review-keyword-red`只是探针命名，实际exit0，公开1项/隐藏3项均通过；
现有autospec已规范化位置/关键字参数。这是已有覆盖，不计RED、不触发代码修改，
也不并入125项正式变体分母。

## 最终命令与回归


全部使用本轮现有 `.venv/Scripts/python.exe -X utf8 -B`，环境仅当前子进程：
`PYTHONPATH=src;tests`、`PYTHONDONTWRITEBYTECODE=1`。

| 日志标签 | 实际命令尾部 | 总数 | 成功 | skip | 失败/错误 | 测试/墙钟秒 | exit |
| --- | --- | ---: | ---: | ---: | --- | --- | --- |
| `focused-final-2` | `-m unittest discover -s tests -p 'test_eval_quality_v3*.py' -v` | 137 | 137 | 0 | 0/0 | 60.080 / 60.672 | 0 |
| `eval-final` | `-m unittest discover -s tests -p 'test_eval*.py' -v` | 309 | 307 | 2 | 0/0 | 162.835 / 163.766 | 0 |
| `full-final` | `-m unittest discover -s tests  -v` | 1975 | 1960 | 15 | 0/0 | 493.431 / 498.656 | 0 |

四次最终CLI命令为 `-m tricoder eval --experiment evals/quality-v3/experiment-quality-<name>.toml --dry-run --no-color`：

| name | trials | 墙钟秒 | exit |
| --- | ---: | ---: | ---: |
| baseline | 30 | 1.828 | 0 |
| repeat3 | 90 | 1.859 | 0 |
| dev | 22 | 1.828 | 0 |
| holdout | 8 | 1.859 | 0 |

首次全量 `full` 为1974项/1959通过/15skip/943.358秒、exit0；追加slots变体后的最终全量以上表为准。两轮结果不相加宣称独立覆盖。

最终全量的skip逐项如下（未验证，不计通过）：

- `test_simple_unittest_target_rejects_symlink_escape (test_command_compatibility.CommandCompatibilityPolicyTests.test_simple_unittest_target_rejects_symlink_escape)`：'当前账户不能创建符号链接'
- `test_internal_symlink_remains_strict (test_create_file_recovery.CreateFileRecoveryTests.test_internal_symlink_remains_strict)`：'当前账户不能创建符号链接'
- `test_link_source_refused (test_delivery_budget_experiment.BudgetExperimentTests.test_link_source_refused)`：'当前账户不能创建符号链接'
- `test_binding_directory_identity_rejects_link (test_directory_tools.DirectoryProtocolFlowTests.test_binding_directory_identity_rejects_link)`："当前账户不能创建目录链接：[WinError 1314] 客户端没有所需的特权。: 'C:\\\\Users\\\\MaHong\\\\AppData\\\\Local\\\\Temp\\\\tmp_f4tnr6e\\\\real' -> 'C:\\\\Users\\\\MaHong\\\\AppData\\\\Local\\\\Temp\\\\tmp_f4tnr6e\\\\link'"
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

聚焦与全部Eval的详细进程退出码、每题modified_files、公开/隐藏测试数见逐项JSON；全量保留原搜索P2至少5.2秒等待、清理失败停止等既有回归结果，不能将这些旧功能归为本包改进。


## 实际增量与历史保全

E/before/ 和 before-sha256.json 保存实际before；
after/、after-sha256.json、file-index.json 覆盖每个实际文件，包括全部未跟踪新文件；
delta.patch 从实际before生成，不是HEAD diff。
status-before/status-after记录已有dirty状态；preservation.json核对旧源码/旧测试/旧suite/脚本及历史证据；
evidence-sha256.json索引本轮证据（不自引用）。

只授权修改旧 README.md、project.md、reviewer-handoff.md，并新增quality-v3、
上述四个测试文件和本报告。原计划、提示词、真实报告、旧suite/hidden verifier、
生产src、pyproject、旧测试和默认配置保持字节不变。
历史记录在project/handoff中保留；当前实现在独立复审前不关闭任何发现。

## 自审、风险和未验证范围

- 自审补齐了service必须调用get的漏检，并复现/修复实例spy误拒合法slots实现；保留真实RED/GREEN。未发现必须修改生产框架的阻塞。
- 单会话作者自审弱于独立复审；本记录不能替reviewer验收。按用户要求保留dirty工作树、不派生、不提交、不删除证据。
- 仅标准库合成仓库与离线Provider/Runtime路径。未运行真实模型，不宣称质量提升、30题真实完成率或统计显著性。
- 有限公开域样例与五种替代实现不构成穷举、模糊测试或所有等价实现的证明；CSV替代解仍使用标准库reader的不同写法，未构建第二套CSV解析算法。
- 旧quality-v2.2最近16轮产物19/19、完整16/19为历史背景，不是本包成绩。新旧题面/边界/数量/划分/预算变化，不可归因比较。
- 新8题holdout已由作者和离线脚本接触，尚未真实试跑；不能称为作者盲测或训练无污染。
- 文件级评分没有扩展为目录级审计；嵌套文件负例不证明空目录审计。模拟故障不证明真实网络或真实大输出截断，memory不证明长期持久化。
- 除本机Windows外未验证其他平台；完整测试中的skip在最终结果逐项保留，不算通过。
- 不改产品默认轮数/权限、MAX_EVAL_CASES=32、旧实验脚本、finish/验证/取消/清理/owner/AND；不推进Replan或其他历史任务。

最终保全：actual before 696份，after 905份；209份新增、3份授权旧文档修改，212份实际增量。122份src、125份旧测试、357份旧eval及7,880份受保护历史证据保持；208份冻结新题库/测试文件在最终验证期间未变。具体路径与SHA256以file-index/preservation/evidence-sha256为准。
