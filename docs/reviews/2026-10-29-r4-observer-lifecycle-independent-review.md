# R4/P2 观察线程生命周期独立复审（2026-10-29）

## 结论

**本轮复审通过，R4/P2 关闭；未发现新的 P1/P2 阻塞项。** 原 R1/R2/R3 已关闭的具体触发条件保持关闭。结论依据当前源码审查、reviewer 新执行的聚焦回归、额外编写的真实 Windows 原生线程探针及保全核对，不以实施报告自述代替验收。

审查对象是当前未提交候选：分支 `codex/eval-metrics-delivery-guidance`，HEAD 仍为 `d6d6c4c8245bdffc19cba03c37b2bfe316f7b448`；本地 main 为 `a81ec2457ece970756b00424393e5f2df9253858`。HEAD 不是 actual before。本轮未访问远端确认 main，没有该未提交候选的新远端 CI。没有提交、推送或合并；后续仍需明确指令。

独立证据：`runtime/ec29-r4review-c5d819a3/`。实施证据只读：`runtime/ec29-r4fix-93fd44da/`。

## 逐项核查

| 核查项 | 代码与验证依据 | 结论 |
| --- | --- | --- |
| 启动结果未知 | `src/tricoder/process/windows_job.py:78` 在调用 `Thread.start()` 前记录尝试；`_stop_monitor:169` 不再根据 `ident/is_alive/join` 宣称退出。启动确认前中断的真实探针观察到 ident 为空、原生线程已进入、Job/port/pending 均保留。 | 通过 |
| 精确线程身份与获取期间所有权 | `windows_job.py:42,92` 由观察线程自身 DuplicateHandle，将不可继承、仅 SYNCHRONIZE 权限的真实句柄直接写入 owner 的输出槽。新增独立探针阻塞在 native 调用已经写槽但尚未返回的位置，再中断主调用，验证精确资源仍可达。 | 通过 |
| 实际退出证明 | `windows_job.py:176-187` 要求 ready、有效原生句柄、WaitForSingleObject 返回 0、done；done 单独成立不能释放。独立探针让 done=true、Python is_alive=false、真实 native wait=258，清理仍返回未确认并保留 Job/port。 | 通过 |
| 迟到句柄与最终关闭 | `windows_job.py:191-219,221-255` 只有观察线程实际退出后才关闭账本及端口。公开调用和直接 finish 探针中，迟到 OpenProcess 句柄先被保留，随后同一资源重试收敛；线程及取得的句柄仅关闭一次。 | 通过 |
| 主异常与二次中断 | `src/tricoder/process/control.py:163-183` 保留 primary，清理二次中断不覆盖它。新重放记录两次 native wait 中断，primary 身份、cleanup_failed、精确 cleanup_resource、scope.failed/pending 均正确。 | 通过 |
| 执行期限与清理期限 | `control.py:141-155,456` 把 ready 等待计入已有执行预算，剩余预算传入采集；cleanup 使用原绝对期限。期限过后重试只做零等待确认，不续发等待预算；失败标志保持。正常命令独立探针确认 task termination_deadline=None。 | 通过 |
| ready 取消及恢复线程顺序 | `control.py:457` 单独重抛 CancellationError；后续恢复前再次检查取消。真实子令牌/父令牌 ready 取消均为 resume_calls=0、脚本 marker 不存在、取消语义保留。原 R3 的 snapshot/open × 本/父取消四场景也通过。 | 通过 |
| Eval owner 与下一阶段阻断 | `tests/test_eval_cleanup_ownership.py:293` 的四子场景覆盖返回值/异常 × 有/无环境 scope：丢弃结果后持久 owner 仍持有精确资源；下一 verifier、下一 case executor/prepare 被阻断，阶段耗时结构保留；新 host 不隐藏旧 pending。现有 ownership、序列化及评分回归全部通过。 | 通过 |
| 原搜索 P2 与默认行为 | 新跑 `test_search_timeout_recovery` 中实际等待至少 5.2 秒，再搜索、compileall、finish 成功；取消与未确认清理负例通过。未改变 TaskCleanup、Agent、Session、默认权限、题库、预算配置或评分。 | 通过 |
| Windows 进程退出及既有 R1/R2 | 本轮重放 query/terminate 两组各 10 次真实迟到成员：均进入正确 Job、被实现打开精确句柄、返回时 wait=0，并在测试兜底清理前立即删除目录。4096 上限、native 失败、捕获缺口与空快照重试继续保守失败。 | 通过 |
| 冷启动、输出超限、诊断及测试范围 | 聚焦回归重新执行冷启动、10 次真实输出超限树清理、严格目录删除，以及诊断字段、TUI、Session/Agent/MCP 契约。actual delta 中没有 TUI、原三个 fake Popen 文件或旧失败断言的进一步削弱。 | 通过 |

这里关闭的是已明确复现的 R4 所有权和退出确认缺陷，不是承诺覆盖全部异步中断或操作系统故障组合。

## Reviewer 独立执行结果

以下命令均使用已有 `.venv/Scripts/python.exe -X utf8 -B`，未安装依赖。`run.py` 将每条命令的 `TRICODER_V3_EVIDENCE` 分别设为独占的 `focused-v3`、`independent-observer-v3`、`isolation-v3`、`compile-v3`，日志以独占创建模式写入。编译缓存也独立存放。

| 运行 | 总项 | 通过 | 失败/错误 | skip | exit | unittest 秒 / wall 秒 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| focused | 220 | 220 | 0/0 | 0 | 0 | 122.867 / 125.078 |
| independent-observer | 3 | 3 | 0/0 | 0 | 0 | 0.365 / 0.775 |
| isolation | 14 | 13 | 0/0 | 1 | 0 | 6.326 / 6.717 |
| compileall src tests | — | — | — | — | 0 | — / 1.736 |

合计执行 237 个测试方法，236 通过、1 skip。skip 原因是当前账户不能创建符号链接，不能计为通过。native 子场景/10 次循环包含在方法计数内，不另外加到总数。

实际命令（以下为已经执行的记录，不应复用同一 label 覆盖日志）：

```powershell
.venv/Scripts/python.exe -X utf8 -B runtime/ec29-r4review-c5d819a3/run.py focused -m unittest test_windows_cleanup_boundaries test_eval_cleanup_ownership test_subprocess_control test_search_timeout_recovery test_session_ownership test_eval_runner test_eval_timing test_eval_quality_v3_review_fixes test_agent_async test_mcp_transport test_tui -v
.venv/Scripts/python.exe -X utf8 -B runtime/ec29-r4review-c5d819a3/run.py independent-observer runtime/ec29-r4review-c5d819a3/test_independent_observer.py
.venv/Scripts/python.exe -X utf8 -B runtime/ec29-r4review-c5d819a3/run.py isolation -m unittest test_eval_workspace test_eval_smoke_suite -v
.venv/Scripts/python.exe -X utf8 -B runtime/ec29-r4review-c5d819a3/run.py compile -m compileall -q src tests
.venv/Scripts/python.exe -X utf8 -B runtime/ec29-r4review-c5d819a3/audit.py
.venv/Scripts/python.exe -X utf8 -B runtime/ec29-r4review-c5d819a3/native_audit.py
git diff --check
```

额外三个测试保存在独立 runtime 中，没有改项目测试或生产代码：

1. `test_duplicate_output_slot_survives_main_interrupt_before_native_return`：真实 DuplicateHandle 写 owner 槽后暂停返回；主中断时真实 wait=258、资源及 Job/port 保留、脚本未运行；放行并由测试确认原生退出后，使用原期限重试成功，failed 保持，句柄仅关闭一次。
2. `test_false_python_liveness_and_done_do_not_replace_native_exit`：真实线程目标返回并置 done 后仍阻塞在线程尾部；用 tracing 在实际 Python join 状态路径注入中断，不直接篡改私有字段。Python is_alive=false、done=true 时 native wait=258，产品清理仍未确认；实际退出后原期限重试收敛。
3. `test_normal_command_does_not_start_task_termination_deadline`：运行合成普通命令，returncode=0，无 cleanup_resource/cleanup_failed/pending，任务终止期限为空。

前两个探针使用测试控制的事件和原生退出等待；没有用 sleep、忽略目录错误或伪造 native wait 成功来完成验收。测试拥有的最终释放只用于收尾，验收明确区分放行前产品失败结果与放行后重试结果。

## 实施方日志与 RED 有效性审计

独立重算了日志结果、文件哈希、冻结源码及 seal；**本轮没有再次独立执行全量 unittest discover**。实施方最终全量可确认为该冻结候选的留存证据：2312 项、2297 通过、15 skip、0 failure/error、exit0，unittest 1296.721 秒，wall 1307.468 秒。15 skip 原因逐项保存在本轮 `audit.json`，包括链接权限、Windows 锁文件语义、Linux 抽象 socket 及文件名限制，未计为通过。

当前 265 个 Python 源码/测试文件与 `candidate-frozen`、`candidate-source-sha256.json` 全部匹配，没有最终字节晚于全量日志的漂移。实施方聚焦 220 通过及编译 exit0 也与原始日志一致。这些记录是补充覆盖，R4 关闭的直接依据仍是上面的新独立执行和代码检查。

- 原启动确认、Python join 状态、真实 finish、公开调用 RED 均为行为断言失败，exit1；`red-formal` 的两个外层测试失败与子进程失败日志需区分，不能重复累计。
- `red-owner-actual-before` 是把新 owner 测试放到 actual before 上运行的追溯验证：1 方法、4 子场景失败。它不代表最早执行的 RED 时间顺序。
- `green-native-lifecycle` 虽然名字有 green，实际是 exit1、2 个取消子场景失败。代码与保留日志表明 fixture 把清理中的 Event.wait 也持续强制返回 False，制造额外故障；改为只在启动 ready 位置注入一次后，`green-native-lifecycle-2` 6 方法通过。该中途失败不包装为产品 RED，也不从记录中删除。
- 旧“Thread.start 整体抛错”fixture 改为构造阶段明确未启动；另外新增启动尝试后异常必须保留 pending 的测试。两者语义分开，没有通过识别 mock 或删断言让生产代码放行。

## 保全与增量范围

- 独立复算 actual before 384、after 386 文件全部匹配；实际增量 8 文件，与 delta-index 一致：2 个生产文件、2 个已有测试文件、新真实探针、实施报告及 project/handoff。无删除。
- 生产增量仅 `control.py` 与 `windows_job.py`；其余 120 个原有 Python 生产文件与 actual before 相同。此前 `evals/workspace.py` 的未提交修复完整保留，本轮未再修改它。评分/题库、默认配置、TaskCleanup、Agent/Session、CI 均不在本轮生产增量中。
- 8936 份历史安全保护文件、1651 份本轮实施安全证据均无变化；五个 seal 引用哈希全部匹配。旧 eval、mg28、旧 review/plan、RED 和中途失败记录继续保留。数据库/缓存等 metadata-only 内容不读取、不声称做内容哈希核验。
- 实施快照未包括 5 个已有生成型 egg-info 文本；reviewer 的 391 文件起始索引额外覆盖它们，审查期间均不变。该覆盖差异不是生产源码遗漏，也不能据此声称拥有这 5 个文件的实施前快照。
- 写交接前，391 文件起始索引全部一致；本轮唯一持久文档变更为新独立报告及 project/handoff 新条目。旧标题后历史正文按字节保留，最终保全结果见 `final-verification.json`。
- 本轮为单会话执行，没有派生子 Agent；未读 `.env.local`、真实凭据或用户数据库，未调用模型、安装依赖、推进 Skills/Replan、恢复 Python3.12、提交或推送。

## 未验证范围与后续建议

1. 本轮环境是本机 Windows / Python3.11.6，未验证其他 Python/OS 或自然 Ctrl+C 的发生频率；确定性注入证明的是指定边界行为。
2. 原生身份完全缺失、启动尝试结果未知等情况仍可能保守 pending，无法凭空证明退出；同步 native 调用及 Thread.start 内部没有新增可抢占的硬实时保证。4096 成员上限及外部进程向私有 Job 赋值等既有边界不扩展。
3. 没有新候选远端 CI，也没有独立重复全量；实施方全量日志仅作为经哈希核对的补充。用户授权后，建议提交准确候选并运行最终 SHA 的 Windows3.11 CI，再确认是否合并。不得把旧 d6d6c4c 的 CI 当作当前未提交候选的 CI。
4. **历史 CI 40721403072 两次尝试出现 public verification_error 与输出超限测试 tearDown 的 WinError32/5；旧日志缺少底层诊断，仍不能证明本轮任何所有权/退出确认修复就是那两次事件的精确根因。** 新测试通过不能追溯建立历史因果。

本轮仅给出通过结论和后续集成建议，不执行合并。
