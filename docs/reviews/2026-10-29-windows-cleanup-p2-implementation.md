# 2026-10-29 Windows清理R1/R2/R3实施报告

状态：**R1/R2/R3实现完成，待独立复审**。这是coding实施与自测记录，不是独立验收；原不通过结论保留，不关闭发现、不合并main。

## 基线与证据

- 当前分支`codex/eval-metrics-delivery-guidance`，HEAD仍为`d6d6c4c8245bdffc19cba03c37b2bfe316f7b448`。本轮没有提交或推送，新修改以工作树及哈希为准；不是一个已有远端绿色CI覆盖的SHA。
- actual before保存了382份安全文件，包括接手时未提交/未跟踪的project、handoff、旧实施/独立复审报告和规划文档；不从HEAD重建。初始状态详见`runtime/ec29-p2fix-5e0b4ed2/status-before.txt`。
- 独占证据根：`runtime/ec29-p2fix-5e0b4ed2/`。before及其SHA、冻结源码、每轮命令/完整日志/退出码、逐轮真实探针收据、机制论证均保留。没有原地重跑或覆盖reviewer脚本。
- 保护索引5723份：旧evals766、mg28安全证据2739（原3505索引），旧实施2131、独立复审79、规划记录8。数据库/锁/缓存等metadata-only内容不读取，不虚称其内容hash已验证。
- 冻结264份src/tests Python文件（含新辅助脚本）：`candidate-frozen/`、`candidate-source-sha256.json`；排序path+hash行的树摘要`50f5890b77e607d468f94c5b31cbf491b08335235e5b706987edf1f53e979a8f`。最终全量之后另核对没有漂移。

## 实际改动与要求映射

| 发现/要求 | 实现 | 验证重点 |
|---|---|---|
| R1成员完整性 | 新增`src/tricoder/process/windows_job.py`，按Job生命周期持有精确成员句柄；IOCP只发现候选，`IsProcessInJob`校验身份；累计TotalProcesses核对完整性，逐个Wait证明退出 | 查询后/终止前两个真实交错点各10轮；64线程晚到子进程、普通/快速退出后代及孙进程；丢失/重复通知、身份不明、累计计数倒退/容量上限负例 |
| R2失败跨重试 | capture_uncertain、protocol_failed、完整成员表、未关句柄及complete分别保存；空列表不解除缺口；只在完整累计数吻合且各精确句柄退出后解除捕获不确定 | OpenProcess error5、Query error5、error234容量不足三种失败→空列表重试仍pending；部分句柄、Wait错误/超时、Close进程/端口/Job失败；获得真实新证据后才解除 |
| R3取消 | `control.py`透传原CancellationToken；入口、快照取得后、枚举循环、OpenThread成功登记后且ResumeThread前复检 | Snapshot/OpenThread两阶段×本令牌/父令牌真实事件探针；未调用ResumeThread且用户脚本未SetEvent；原句柄仍被清理或持有 |
| 新观察资源owner | Job刚取得即登记到原ProcessResources；tracker在端口/worker创建前归Job；启动不完整ready=false，不能恢复root；退出证明前有界停止并join观察线程 | 新自审RED证实工厂启动主中断丢observer，GREEN保留主中断身份并清理；端口/关联/线程启动失败；native观察阻塞不能持有无界清理锁 |
| 上层门禁保持 | evals/cleanup.py、runner.py、TaskCleanup/Session/Agent均未改；只在原下层资源得到证据时确认关闭 | 新native模型经真实ProcessResources.finish→Eval retain/retry，返回值/异常两路径均阻断下一verifier及case executor；原期限、failed、序列化和失败评分保持 |

生产增量为`control.py`、职责单一的`windows_job.py`，以及`evals/workspace.py`中隔离helper依赖闭包的必要接线；没有通用重构。新增`tests/test_windows_cleanup_boundaries.py`、`tests/windows_cleanup_boundary_probe.py`；扩展`tests/test_eval_cleanup_ownership.py`并让其中既有取消fixture透传新增私有resources关键字，原断言保留。其他旧测试没有删减、skip或弱化。

## R1完整性、退出及资源论证

完整说明见证据`mechanism.md`。两个条件分开：

1. **覆盖完整性**：root在挂起时已有Popen精确句柄，Job没有名称、不继承、不允许breakaway；本宿主只分配root，普通后代继承Job。通知与快照仅提供PID候选，OpenProcess后先持有句柄，再用IsProcessInJob验证精确Job成员。成员句柄一直保留到最终证明，防止PID复用；重复消息不增加成员数，身份不明的候选句柄也不能丢弃。
2. **逐个退出**：先发终止请求，按原剩余期限等待精确句柄；停止并join观察线程后，再等待它最后补入的所有句柄。随后累计TotalProcesses必须等于唯一成员表数量且不超过4096。该计数包括已退出成员和失败关联，只用来发现缺口，不用来证明退出。ActiveProcesses、第二次空列表和父进程退出均不作为替代。
3. **最后读取之后**：数量相等意味着整个Job生命周期没有遗漏成员；这些成员均已被精确等待确认退出，已无内部创建者继续派生。已有在途普通创建必须在创建者退出前成为累计成员，或不能再由已终止线程执行。外部特权进程强行分配/注入、WMI等Job外代理不在原受管进程树契约中，本实现不是OS沙箱。
4. **漏消息/API失败**：若成员未及时取得精确句柄，累计数与成员表不符，重试空列表仍失败。后续只有补到真实成员句柄、确认退出并完成累计核对才可解除；计数倒退/越界或错误消息代次保守阻断。不是把所有Windows命令统一判失败；无后代/快速后代/孙进程及正常验证器成功路径已实际运行。
5. **释放顺序**：观察线程先停止并join，不能在成功发布后写账本；继而确认全部成员退出、完整性，再关闭自己持有的进程/候选/端口句柄，最后关闭Job。Close失败保留精确引用；部分关闭后的成员身份仍留在已证明完整的账本中。root句柄由被强引用的Popen持有，不重复关闭。所有等待均消费同一原期限。

依据为Microsoft标准Job、IOCP、进程和线程API，没有采用未文档化freeze API或猜测ActiveProcessLimit语义。参考：[完成端口通知及PID复用限制](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_associate_completion_port)、[生命周期累计计数](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_basic_accounting_information)、[进程退出与对象signal](https://learn.microsoft.com/en-us/windows/win32/procthread/terminating-a-process)、[Job继承与范围](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)。以上是供独立reviewer核查的实现论证，不是独立验收声明。

## RED/GREEN与中间失败

| 日志label | 实际结果 | 证据性质 |
|---|---|---|
| red-native | 4方法、5失败断言、0error/skip，exit1 | 原native故障模型的新副本，涵盖R1/R2/R3 |
| red-r1 | 10轮中9轮提前成功、10轮漏捕获，exit1 | 真API、精确预持有子句柄；失败观察后测试清理另行执行 |
| red-r3 | 1项失败、exit1 | 取消已置位但真实脚本SetEvent |
| red-formal → green-formal | 3方法/4失败/exit1 → 3通过/exit0 | 正式R2/R3测试的首次RED→最小修复 |
| green-boundaries / green-r3 | 7通过 / 真实取消1通过，exit0 | 补边界；已覆盖条件不虚构单独RED |
| prototype-late / prototype-normal | 晚到成员10轮；plain/quick/grandchild各10轮，均通过 | 隔离原型，之后才集成生产 |
| integrated-late | 真实晚到成员10轮均即时signal并删目录，exit0 | 生产集成 |
| red-observer → green-tracker-boundaries | 观察native调用持锁导致1失败 → 11通过 | 自审发现的新增期限风险；native调用移出短账本锁 |
| real-helper-cancel | IndentationError，exit1 | 生成正式探针时的fixture错误，不作产品RED；完整日志保留 |
| green-integrated / boundaries-expanded | 33 / 37通过，exit0 | 扩展集成与真实场景 |
| red-startup-interrupt → green-startup-interrupt | 1失败 → 1通过 | 真实observer启动主中断，确认Job提前登记owner |
| final-boundaries | 39通过、0skip、exit0，23.396s | 中间版本；后续发现终止门闩没有真正移动，不能作为最终双边界证据 |
| focused-1 | 206通过、exit0，215.476s | 中间已导入版本；随后新增启动中断修复，不作最终冻结证据 |
| focused-frozen | 207通过、0skip、exit0；199.702s，墙钟202.114s | 第一次冻结，后续修正辅助探针，不作为最终字节证据 |
| red-termination-gate → green-termination-gate | 1失败 → 5真实方法通过、exit0 | 新实际trigger_stage断言揭示门闩仍在query；这是fixture错误，不是新产品RED；修正为真实TerminateJobObject入口 |
| boundaries-final-freeze | 39通过、0skip、exit0；32.055s，墙钟32.968s | 加严即时目录删除后；后续另修隔离helper依赖闭包 |
| focused-final / compileall-final | 207通过、0skip、exit0；147.804s / 编译exit0 | 第二次冻结；不是最后依赖接线后的全量证据 |
| full-frozen | 2299项、2282通过、2失败、15skip、0error、exit1；1295.402s，墙钟1303.572s | 中途全量；既有隔离helper测试暴露windows_job依赖未复制，另导致smoke验证失败；测试辅助脚本在运行期间修正，不能称最终冻结覆盖 |
| green-dependency-closure | 14项、13通过、1平台skip、0失败/error、exit0；10.702s，墙钟11.405s | 修正workspace.py最小依赖复制后，真实-S子进程不依赖开发安装即可正常运行/超时清理，smoke真实隐藏检查通过 |

中途全量暴露的是新增模块的隔离运行依赖缺失，原有`test_installed_process_helper_has_local_runtime_dependency_closure`给出确定性失败；它不是R1/R2/R3的产品RED，也不能归因于旧CI。最小接线只把受控框架模块和空process包标记复制进既有保留verifier目录，复用原链接/普通文件/独占路径核查；不修改隐藏题目或判分。原有测试已覆盖该条件，因此没有虚构一个新增RED。修正后以`candidate-frozen`重新冻结所有264文件，最终命令统一为`*-candidate`。

真实轮次与subTest不能当独立unittest方法累加。测试采用受控事件/队列交错，不使用无依据sleep；真实进程Wait返回值没有mock成0。原搜索P2测试按原契约实际等待至少5.2秒，不能把这个必要回归延迟误作修复中的宽限期。

完整命令与独占日志由本轮run.py保存，环境为PYTHONPATH=src;tests、PYTHONDONTWRITEBYTECODE=1、PYTHONUTF8=1；每个label设置新TRICODER_V3_EVIDENCE，compileall缓存前缀也指向本轮目录。示例为已执行命令，重跑必须换label：

```powershell
.venv/Scripts/python.exe -X utf8 -B runtime/ec29-p2fix-5e0b4ed2/run.py focused-candidate -m unittest test_windows_cleanup_boundaries test_eval_cleanup_ownership test_subprocess_control test_search_timeout_recovery test_session_ownership test_eval_runner test_eval_timing test_eval_quality_v3_review_fixes test_agent_async test_mcp_transport test_tui -v
.venv/Scripts/python.exe -X utf8 -B runtime/ec29-p2fix-5e0b4ed2/run.py full-candidate -m unittest discover -s tests -v
```

## 最终候选验证与交接索引

| 最终命令label | 实际结果 | 耗时 |
|---|---|---|
| focused-candidate | 207项、207通过、0失败/error/skip，exit0 | unittest155.256s；墙钟157.095s |
| full-candidate | **2299项、2284通过、0失败/error、15skip，exit0** | unittest1004.484s；墙钟1018.279s |
| compileall-candidate | src/tests编译，exit0 | 墙钟2.183s |

全量是原2279项加新增20项（新边界模块19方法、Eval owner集成1方法）。子场景/10轮重复不再加入总数。平台skip不算通过，逐个名称与原因见`full-candidate-skips.json`；这里没有跳过本机应执行的新增Windows测试。完整逐命令参数、数量、失败/error/skip、退出码、墙钟与所有中途结果见`commands-and-results.json/.md`及同名完整log。最终编译命令为：

```powershell
.venv/Scripts/python.exe -X utf8 -B runtime/ec29-p2fix-5e0b4ed2/run.py compileall-candidate -m compileall -q src tests
```

- `focused-candidate-v3/`与`full-candidate-v3/`各自保存query/terminate两个真实门闩的10轮收据：20/20实际触发、精确Job归属、被实现捕获、返回即时Wait=0、测试兜底前严格目录删除。不能把两次运行合成单个更大独立样本；汇总索引为`native-probes-final.json`。
- 四个真实取消子场景为snapshot/OpenThread × 本令牌/父令牌，均无ResumeThread，脚本的即时event检查未触发，父进程均退出且cleanup_failed=false。JSON记录调用轨迹；event负例断言在辅助脚本第180行，须连同子进程exit0核查，不能只读JSON末行。
- 原输出超限真实进程树10轮、冷启动归属、普通/快速退出/孙进程、搜索超时后实际5.2秒等待再搜索/compileall/finish、取消/已有期限/未确认清理/Session owner均在最终聚焦及全量执行。没有重新领取清理预算或忽略目录删除失败。
- R2三类失败×返回/异常路径使用实际ProcessResources与Eval owner；后续重试仍不能误确认或启动下一个verifier/case。Wait超时/失败、部分句柄、Close进程/port/Job失败、丢消息/身份/代次、启动失败和主中断均保留测试。

### actual before/after与文件位置

actual before382份，after386份（新增4份；旧有本地未提交文档不从HEAD重建）。本轮9个增量：

| 文件 | 本轮作用 |
|---|---|
| src/tricoder/process/control.py:142、352、464 | Job提前登记owner、接入tracker、恢复前取消复检 |
| src/tricoder/process/windows_job.py:30、66、126、163（新增） | 生命周期账本、身份绑定、同期限停止观察、逐句柄退出及累计完整性核对 |
| src/tricoder/evals/workspace.py:109 | 隔离helper依赖闭包复制新增模块；既有文件安全检查不变 |
| tests/test_windows_cleanup_boundaries.py:100（新增） | 14个native模型方法+5个真实Windows方法 |
| tests/windows_cleanup_boundary_probe.py:11、126（新增） | 真API双交错10轮和取消事件探针；立即删除断言 |
| tests/test_eval_cleanup_ownership.py:246、376 | native缺口到Eval持久owner与执行阻断；原取消fixture仅透传私有参数 |
| docs/reviews/2026-10-29-windows-cleanup-p2-implementation.md（新增） | 本实施报告 |
| project.md、docs/reviews/2026-10-19-reviewer-handoff.md | 追加当前状态，旧正文保留 |

证据根`runtime/ec29-p2fix-5e0b4ed2/`内：

- `before/`、`before-sha256.json`、`after/`、`after-sha256.json`：实际文件字节；`delta-index.json`及`delta.patch`基于actual before，不基于HEAD，并包含新增未跟踪文件。
- `candidate-frozen/`、`candidate-source-sha256.json`、`candidate-freeze-receipt.json`：最终264份源码/测试/辅助脚本；`source-and-history-final.json`验证无漂移、其他源码与评分AST不变、旧交接正文仍保留。
- `preservation-final.json`：原5723安全文件逐份SHA复核（其中旧evals766+mg28 2739=3505），changed必须为空。metadata-only数据库/锁/缓存不读取内容，不宣称对其做字节hash。
- `git-*-final.log/.json`与`new-file-check-*.log/.json`：Git状态、HEAD/本地及远端main/分支、git diff --check及新增文件单独检查。`evidence-safe-sha256.json`、`evidence-file-index.json`和`seal.json`供独立复核证据完整性。
- `mechanism.md`、`self-review.md`：机制依据、实施自审与尚未穷尽的边界；不是reviewer结论。

HEAD仍d6d6c4c，本轮候选是未提交工作树。**最终候选远端Windows3.11干净CI尚未执行**；没有借用旧SHA绿色CI，未提交/推送/合并。main最后核查结果保存在独立Git收据；等待用户另行授权推送后才能取得对应候选SHA的远端CI。下一步只交独立reviewer核查R1完整性论证、R2持续状态、R3实际恢复边界及全部新失败/成功证据。

## 风险与未验证范围

- 通知是尽力送达；累计核对可阻止误确认，但丢失且无法再取得句柄的成员会保守保留pending，可能需要宿主处置，不能通过空列表洗掉事实。TotalProcesses含失败关联，超过4096个生命周期成员也不在支持范围内。
- Windows本机之外的OS版本、POSIX执行、全部native故障组合、无限/恶意进程树、特权外部修改或Job外代理未穷尽验证；同步native调用没有新增可抢占OS超时保证，但等待/join和新锁边界均受原预算约束。
- R3保证恢复决策前已观察到的取消阻止恢复；决策后抵达的取消沿用运行中终止语义，不承诺撤销已执行代码，不持有CancellationToken私有锁。
- 旧历史CI40721403072两次失败的精确根因仍未证实。本次新机制、新探针和未来新CI都不能追溯证明历史因果。
- 本轮没有真实模型、真实用户库/凭据读取、本机依赖安装、题库/权限/评分/默认预算修改，未推进Replan或恢复Python3.12。没有提交、推送或合并；等待用户另行授权推送后才可能取得最终SHA的远端CI。
- 完成本轮本地验证后只交独立reviewer，不自行关闭R1/R2/R3或合并main。
