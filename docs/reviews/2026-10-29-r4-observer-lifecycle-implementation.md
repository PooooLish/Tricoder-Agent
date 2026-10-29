# 2026-10-29 R4观察线程生命周期实施报告

状态：**R4实现完成，待独立复审**。实施自测不是独立验收，不关闭发现、不合并。

## 基线与本轮范围

- 实际分支`codex/eval-metrics-delivery-guidance`，HEAD仍`d6d6c4c8245bdffc19cba03c37b2bfe316f7b448`，本地main仍`a81ec2457ece970756b00424393e5f2df9253858`。本轮修改叠加在原R1/R2/R3未提交实现上，actual before不是HEAD。
- 主计划为`docs/superpowers/plans/2026-10-29-r4-observer-lifecycle-fix.md`；独立R4报告及旧R1/R2/R3关闭结论保留。本实施仅处理observer生命周期；不重新解释历史CI原因。
- 独占证据：`runtime/ec29-r4fix-93fd44da/`。开始保存384份安全项目文件，包含原未跟踪生产/测试/文档。保护集合按旧5723索引、旧实施2720安全文件、reviewer480安全文件及相关索引/规划文件合并，共8936个唯一文件；旧3505是5723的子集，不重复相加。
- 生产修改仅`src/tricoder/process/windows_job.py`和必要的`control.py`接线。新增`tests/windows_observer_lifecycle_probe.py`，扩展两个既有测试模块；没有新增生产模块，既有隔离helper复制闭包不变。

## 实现及完成证明

| 生命周期事实 | 实际字段/行为 | 安全含义 |
|---|---|---|
| 未跨启动尝试边界 | `start_attempted=False` | 仅端口/关联/线程构造等启动前失败可确认没有worker |
| 已尝试但结果未知 | 调用`Thread.start()`前置`start_attempted=True` | start异常或ident为空都不能推导无线程；持续保留owner |
| 精确线程身份 | worker调用`GetCurrentThread`，经`DuplicateHandle`写入owner持有的`observer_handle` | 只申请SYNCHRONIZE，不继承、没有CLOSE_SOURCE，不关闭伪句柄，不靠TID重开身份 |
| 准备完成 | `observer_ready`及有效句柄检查 | control在root仍挂起时等待，失败不恢复用户代码 |
| 静止确认 | `_observe`最外层finally最后发布`observer_done` | 此后该target不再操作native资源/账本；单独done不能证明OS线程退出 |
| 精确退出 | `_stop_monitor`按原期限Wait该真实线程句柄返回0，且done已发布 | 不使用Python join/is_alive/ident作为成功证据，包括被中断后的失真状态 |
| 关闭失败 | 保留原`observer_handle`及Job/其他未关闭句柄 | 重试不丢exact引用；成功才清除句柄值 |

Job在原`_ProcessResources`中提前登记，tracker在观察设施分配前归Job所有。线程句柄的native输出槽本身就位于tracker，而非先写局部变量再交接；即使DuplicateHandle返回前/后被打断，owner仍覆盖在途获取。OpenProcess已取得但尚未登记的句柄由尚未退出的worker栈及持续持有的tracker覆盖，不能先释放Job/port。

停止消息根据`start_attempted/stop_sent/observer_done`发送，完全不依赖`is_alive`。未知启动也会保留port并发送停止请求；晚启动worker取得身份后可以消费请求。发送失败继续未确认；done为真时不必再发停止，但仍必须等待真实线程退出。完成握手或原生等待被中断时，不设置`observer_exited/complete`；主异常后的自动重试仍需要同一证据。

确认observer退出后，既有R1逻辑重新等待全部最终成员句柄并核对累计TotalProcesses；随后关闭observer/成员/extras/port，Job最后关闭。这样迟到OpenProcess登记、outer target尾部或包装器尚未退出，都不能越过完成边界。`_close_owned`增加未证明observer退出时的防御拒绝；没有用已关闭Job分支临时扫extras代替生命周期修复。

正常ready等待不启动任务终止期限。control从现有命令timeout产生一次执行deadline，传入启动握手，后续收集只使用剩余执行时间；不增加配置、默认预算或5秒清理期限。ready轮询每次最多等待20ms以响应本/父CancellationToken，所有等待共用该deadline。工厂单独重抛CancellationError，避免被原`except Exception`吞成普通初始化错误。清理沿用原资源/任务deadline，过期后只作零等待确认。

采用标准[DuplicateHandle伪句柄转真实句柄规则](https://learn.microsoft.com/en-us/windows/win32/api/handleapi/nf-handleapi-duplicatehandle)与[线程退出对象signal规则](https://learn.microsoft.com/en-us/windows/win32/procthread/terminating-a-thread)。本轮原型记录worker内获取、不可继承、活动时Wait258、释放后Wait0及Close成功；这只是机制依据，完整正确性仍待独立reviewer核查。

## RED、fixture适配及逐层验证

| label | 实际结果 | 说明 |
|---|---|---|
| red-startup_boundary | 1方法/1失败/0error/skip，exit1 | 真实启动确认中断后scope无pending |
| red-join_boundary_v2 | 1方法/1失败/0error/skip，exit1 | native模型+真实Python线程，重试错误确认 |
| red-real_join_boundary | 1方法/1失败/0error/skip，exit1 | 真实Job/IOCP/句柄，经ProcessResources重试错误确认 |
| red-public_join_boundary | 1方法/1失败/0error/skip，exit1 | 公开run_bounded_process主中断后丢失owner |
| red-formal → green-formal | 正式2方法/2失败 → 2通过，exit1→0 | 启动确认和公开执行链回归先RED再最小实现 |
| green-original-boundaries | 41通过、0skip，exit0，25.537s | 原39边界/所有权方法+初始2新增方法 |
| green-native-lifecycle | 6方法，1方法中的2个取消子场景失败，exit1 | **fixture错误**：取消注入同时把后续清理Event.wait也改成零等待，非产品RED |
| green-native-lifecycle-2 | 6通过、0skip，exit0，0.070s | 注入仅触发一次，之后委托真实等待 |
| green-owner-lifecycle | 8通过、0skip，exit0，1.172s | owner回归及native生命周期边界 |
| red-owner-actual-before | 1方法/4子场景失败，0error/skip，exit1，1.041s | 新owner测试另对actual-before源码运行；result误判通过，exception丢引用。不是HEAD回滚，也不声称这次晚执行的对照是该方法首次RED |
| green-real-expanded | 4通过、0skip，exit0，7.385s | 启动、真实finish、公开主中断、二次中断 |
| green-real-ready | 1方法、2真实取消子场景通过，0skip，exit0，0.643s | 本/父令牌ready取消均无ResumeThread、脚本未执行 |
| boundaries-final | 66项、65通过、1平台skip，0失败/error，exit0，28.902s | 新旧边界/owner及真实隔离`-S`依赖闭包与smoke隐藏验证 |

四份有效reviewer脚本仅复制到新目录运行。模型join原版改写私有锁的注入，在本轮副本中改为局部Python tracing触发同一真实等待异常路径；没有改CPython私有锁/标志。旧无效首版`test_join_boundary.py`没有当作RED。正式新机制不走join，注入移到实际observer句柄Wait边界，先取得真实Wait(0)=258再抛主/二次中断，收据确认门闩确实触发；另以真实join异常路径污染Python状态后检验产品仍依赖原生退出。

旧“整个Thread.start抛RuntimeError→立即清理成功”的fixture不能表示生产确定未启动。本轮保留端口/关联成功关闭断言，把明确未启动子场景改到Thread构造前失败；另加已尝试start抛异常仍pending的独立方法。不是识别mock或按异常正文特判。旧native Kernel进程结果仍为模型，但新增observer句柄由真实Windows线程取得、等待、关闭，通过测试侧独立映射避免与模型进程ID混淆；不伪造线程退出成功。

新增13个unittest方法：真实子进程边界5、native生命周期7、Eval owner集成1。测试重复轮次和子场景不计额外方法。正常成功、明确启动前失败、启动未知、done先于OS退出、身份获取失败、Wait TIMEOUT/FAILED、Close/Post失败、原期限已耗尽及二次中断均有对应断言。

## 真实资源、owner与期限收据

`*-v3/observer-*/`保存每次真实调用的JSON和独立子进程log：

- startup：原生线程已进入bootstrap而ident未确认，主中断后Job/port仍持有、scope pending/failed、root未执行；释放门闩并确认原生退出后，用原已耗尽deadline清理，不续预算。
- public/second：真实OpenProcess已取得句柄但尚未返回登记；主中断保持、异常携带exact resource及cleanup_failed，自动二次清理不能提前complete；释放后每个observer取得的进程句柄和observer线程句柄各关闭一次，extras最终为空，failed黏着、原期限未增长。
- finish：实际ProcessResources.finish中断及重试，observer未退出时返回未确认，真实退出后原期限重试确认。
- ready-child/ready-parent：新增ready等待中取消，root恢复调用0，脚本标记不存在；保留CancellationError分类和清理映射，Job/port/observer句柄均关闭。

Eval新方法覆盖返回值/异常×有环境scope/无环境scope四子场景。丢弃结果及局部strong reference并GC后，weakref仍由持久owner维持；下一verifier与下一case executor/prepare均不调用，新的host不能遮蔽旧环境pending。最后退出后原deadline回收、pending移除，但原failed与失败评分不改，资源不进入序列化结果，阻断case保持timings结构。

测试自有finally收尾和用于观察OS退出的独立等待单列，不作为首次产品清理成功；真实API返回值未改成成功。没有sleep、关闭正在运行worker的port、强杀线程或禁用中断。

## 最终冻结验证与保全

最终候选265份源码/测试树SHA256：`651577fb1ae1dc5cb73fd6b501489cabd4c905e7a3c1e682de30d360e787584f`。该值是未提交工作树候选指纹，不是Git提交SHA。最终验证期间源码与辅助测试文件未变；HEAD仍为d6d6c4c。

| 最终独占日志 | 运行方法 | 通过 | 失败/error | skip | exit | unittest秒 / wall秒 |
|---|---:|---:|---:|---:|---:|---:|
| focused-candidate | 220 | 220 | 0/0 | 0 | 0 | 201.347 / 202.711 |
| full-candidate | 2312 | 2297 | 0/0 | 15 | 0 | 1296.721 / 1307.468 |
| compileall-candidate | 不适用 | 不适用 | 0 | 不适用 | 0 | — / 1.885 |

每条实际命令、解释器绝对路径、证据目录与中途失败索引见`commands-and-results.json/.md`；完整输出见同label的.log，平台skip逐条列在`full-candidate-skips.json`，不算通过。执行使用已有`.venv/Scripts/python.exe -X utf8 -B`，通过本轮`run.py`设置独占TRICODER_V3_EVIDENCE：

```text
-m unittest test_windows_cleanup_boundaries test_eval_cleanup_ownership test_subprocess_control test_search_timeout_recovery test_session_ownership test_eval_runner test_eval_timing test_eval_quality_v3_review_fixes test_agent_async test_mcp_transport test_tui -v
-m unittest discover -s tests -v
-m compileall -q src tests
```

本轮新增13方法：原聚焦207→220，全量2299→2312。不是借用旧绿色结果。每次最终运行都重跑R1 query/terminate各10轮（精确句柄Wait0及立即目录删除）、R3本/父×snapshot/OpenThread四组、输出超限10轮、冷启动/快速/孙进程；`native-probes-final.json`指向本轮逐轮JSON。原搜索恢复>=5.2s、未确认清理停止、取消/既有期限/Session owner均在最终日志有实际通过记录。真实R4 startup/public/second/finish/本父ready六组收据同样单列。

actual before384份和after386份安全文件、`before-sha256.json`/`after-sha256.json`、包含未跟踪新增文件的`delta-index.json`/`delta.patch`便于逐字节复查。实际增量8文件：2生产、2既有测试、1新测试helper、1新报告、project/handoff；旧R1/R2/R3未提交内容不是本轮新增。保护8936唯一安全文件内容不变；旧3505/5723索引、2720实施证据和reviewer索引保持，重叠不重复计数。DB/缓存/非白名单仅metadata，不宣称内容哈希已验。

`preservation-final.json`、`source-and-history-final.json`核对保护集合、265最终字节、非本轮生产文件及评分AST、project/handoff历史正文。`git-diff-check-final`和新文件no-index检查保留退出码；新文件使用命令级CRLF空白规则，不改Git配置。`evidence-file-index.json`/`evidence-safe-sha256.json`/`seal.json`索引本轮安全证据，`final-verification.json`记录最终结果。

## Task完成映射（实施自审）

| Task | 交付与证据 |
|---|---|
| 1 保全与RED | actual-before、8936保护索引、四探针+正式RED、启动/公开调用业务失败；旧日志不变 |
| 2 生命周期与退出证明 | windows_job的start/_observe/_stop_monitor/_close_owned；control deadline/token接线；native原型、7边界方法、真实本/父取消 |
| 3 持久owner与迟到句柄 | 5真实子进程方法、Eval四子场景；主/二次中断、晚登记/最终Close、原deadline、failed黏着及下一执行阻断 |
| 4 最终验证与交接 | 220聚焦、2312全量、compileall、冻结字节、before/after/delta/保护/完整日志/自审；不含新远端CI |

自审见`self-review.md`，源码/测试定义位置见`implementation-map.json`。这些结果仅供独立复审，不构成reviewer关闭R4或整体验收通过。

## 风险、限制与后续

- 原生身份获取失败或start结果长期无法确认时，宁可保留pending；不能仅凭done或Python线程状态解除。尚无可靠身份的已退出线程可能因此不能自动回收整个owner，这是保守可用性限制，不伪称所有故障都可恢复。
- Thread.start内部Python启动确认及同步native调用仍有不可抢占边界；新增ready等待、停止/退出等待有原期限，不宣称整个OS调用都有硬实时超时。没有新增无owner后台线程来隐藏等待。
- 仅本机Windows/Python3.11.6及确定性调度/中断注入；没有自然Ctrl+C发生频率、其他Python补丁/OS版本、无限恶意树或全部native故障组合的实测。R1原有累计成员限制、通知缺口及Job外代理边界保留。
- 本轮没有新生产模块，既有隔离helper复制契约经`-S`子进程验证；未修改TaskCleanup、Eval runner/cleanup、Agent、Session、题库、权限、评分、默认预算、CI配置或Replan/Skills功能。
- 没有真实模型、凭据/用户库读取、依赖安装、提交/推送/合并。最终候选没有新远端CI；旧SHA绿色CI不能作本轮证据。历史CI40721403072两次失败的精确根因仍未知，不能从本次修复追溯认定。
- R4及整体验收交独立reviewer；原R1/R2/R3具体条件关闭结论保持。本实施不会自行宣布复审通过，即使后续复审通过，仍等待明确推送/合并指令。
