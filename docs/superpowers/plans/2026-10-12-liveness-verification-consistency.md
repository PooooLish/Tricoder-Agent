# TriCoder 阻塞保护与验证状态一致性实施方案

> 2026-10-12 实施状态：A1→A2→B1→B2 已完成，待独立复审。最终代码全量 1734 项，0 failures/errors，13 skipped，349.223s；compileall 与 diff --check 退出 0。证据见 `runtime/liveness-verification-20261012/verification.md`。首次全量失败日志保留，不作为通过证据。没有真实 Provider 质量测试。
>
> 实施校准：共享规范化保留旧 Runtime 的合法根内 `sub/../app.py`，仍拒绝越界；参数增长专项的注入时间从 80ms/35ms 改为 300ms/100ms，以容纳 Windows 调度，累计仍超过 T 且小于 3T，生产预算不变。未确认身份的缓存保留归属，不自动采样新身份后递归删除。

> **For agentic workers:** 使用 `superpowers:executing-plans` 按任务执行；本方案不要求并行 Agent。用户将文档交给 coding session 实施，本会话只编写方案。步骤使用复选框记录进度，不自动提交或推送。

**Goal:** 修复流式响应和正则搜索的阻塞风险，统一单任务与会话的验证义务，并使受控语法检查不污染评测工作区。

**Architecture:** 保留现有 Provider → AgentRunner → ToolRegistry → SessionRuntime 链路。在资源所有者处实现有界等待和清理；抽取共享验证覆盖规则，保留任务完成与历史义务的不同作用域；将编译生成物移入宿主控制的临时目录，保持评测范围检查严格。

**Tech Stack:** Python 3.11+、asyncio、标准库 urllib/subprocess/unittest、现有 TaskCleanup/VerificationScope/SessionRuntime。首版不增加依赖。

**Spec:** 本文第 1～3 节是需求与设计约束，第 4～7 节是执行任务，第 8～9 节是验收和交付要求。依据 2026-10-12 整体审查及用户指定的优化顺序 1、2。

## 1. 范围与审查基线

分为两个可独立验收的批次，先 A 后 B：

- **A：阻塞保护。** A1 流式结束、无进展超时和资源回收；A2 正则搜索隔离和搜索预算。
- **B：验证一致性。** B1 验证覆盖与父目录义务一致；B2 编译缓存外置和原始评测端到端通过。

不包含：Plan/Replan、计划 system 角色修复、记忆分批算法、spill 检索增强、Docker、命令白名单扩张、Provider 厂商扩展、全仓重构、真实模型评测。

本轮审查基线：1698 项测试、0 failures/errors、13 skipped，耗时 275.295 秒；`git diff --check` 通过。这是修改前证据，不可当作实施后验收。

已确认现象：

| 问题 | 代码入口 | 修改前证据 |
| --- | --- | --- |
| `[DONE]` 后仍等待 EOF | `providers.py:_stream_once` | 完整响应后保持连接，collector 必须由外部超时结束 |
| 心跳不能代表模型有进展 | 同上及 `UrllibTransport.post_stream` | 持续注释心跳超过请求 timeout，内部仍不结束 |
| 短正则可阻塞 | `tools/search.py:SearchTextTool.run` | `(a+)+$` 匹配 30 个 a 加一个 !，隔离探针超过 1 秒 |
| 已完成任务仍残留父目录 pending | `engine/completion.py:check_coverage` 与 `session/runtime.py:_merge_verification_obligation` | 定向检查 main.py 后 ok=True，但 pending 为 app、app/demo；下轮只读后仍保留 |
| 编译缓存导致评测越界 | `process/env.py`、`tools/command.py`、eval | 原 q-nested-create 的离线恢复测试仍断言 change_out_of_scope |

### 全局约束

- 先读 `../../AGENTS.md`、项目 `AGENTS.md`、README、pyproject、project.md 和本文。保留当前全部未提交修改，不从 HEAD 覆盖已有修复。
- 不读取 `.env.local`、真实凭据或用户 sessions.db；不安装依赖、联网调用模型、自动提交/推送。
- 测试使用合成工作区、临时 SQLite 和离线 Provider。测试日志写到 `runtime/liveness-verification-20261012/`，不要覆盖旧审查日志。
- 不把普通脚本退出码 0 当作验证通过，不让隐藏 verifier 回写 Agent 状态，不降低失败/取消/UNKNOWN/清理失败门禁。
- 保留 finish 最多一次补验证、当前最大轮数、审批和锁所有权语义。审查/问答不能为了结束而强制执行无关检查。
- 用户命令策略不变；内部固定 helper 不是新增任意命令执行入口。
- Windows/Python 3.11 是本机验收环境；保留 Python 3.12 与已有跨平台分支，不能声称未运行平台已验证。

## 2. 设计选择与边界

### 2.1 流式等待

仅增加 socket timeout 不能解决持续心跳；仅给 coroutine 套超时也不能证明后台线程或连接已结束。采用**协议完成边界 + 单次请求总预算 + 有效进展空闲预算 + 资源所有权清理**。

以现有 Provider `timeout` 为 T，首版不增加 CLI/配置项：

- 连接/读取的底层 timeout 仍为 T；每次尝试首个有效进展等待最多 T。
- 首次有效进展后，连续无有效进展最多 T。
- 一次逻辑 stream 请求（包含最多 3 次既有尝试和退避）总预算为 3T；退避不能重置总截止时间。
- 使用 monotonic 时钟；测试可注入较小时间值或时钟。默认 T=30 秒时，总预算为 90 秒；README 明确此新增语义。
- 非空文本、非空 reasoning、新增且格式合法的工具参数片段属于进展；首次合法 finish 边界可推进一次。注释心跳、空 delta、空行、重复元数据、单独 usage 不续命。不能只依赖 ToolCallStarted，因为工具参数可能持续增长而没有该事件。
- `[DONE]` 是成功终止边界；当前事件正确解析、工具参数完整、此前 usage 已收集后立即结束读取，执行 finally 清理，不再等待 EOF。`finish_reason` 后仍可读取终止前的 usage；缺少完整终止边界的截断不冒充成功。
- 保持现有重试边界：已经向上游发布事件后不自动重放本次请求；没有发布事件的可重试故障，在既有尝试数和总预算内重试；总预算耗尽直接停止。
- 异常使用固定类别，例如 `first_progress_timeout`、`idle_timeout`、`request_deadline_exceeded`；可为 ProviderError 添加兼容默认值的 code 字段。UI/审计不携带响应正文、URL 参数或凭据。

取消、timeout 和正常 DONE 都必须收敛底层资源。不能把 `asyncio.to_thread` 的 awaiter 被取消误认为阻塞函数已结束。连接建立后迟到的 response 也必须被关闭；读取/关闭 worker 未确认结束时，沿 TaskCleanup 保留 exact owner 并报告清理未确认，不得释放工作区锁或开始下一轮请求。复用现有终止清理截止时间，不能每层新领 5 秒。若必须调整 urllib worker 管理，限制在传输层，避免引入通用调度框架。

### 2.2 搜索预算

保留 literal 和 regex 两种模式，不用禁用 regex 掩盖问题。选择**固定标准库子进程 helper**运行正则，而非无法强制终止的 Python worker 线程。

- 父进程依照现有 WorkspacePolicy、链接策略、gitignore、二进制/单文件大小规则枚举并读取文本；helper 只消费有界文本数据，不接收可执行代码，不自行遍历用户路径。
- 每次 search_text 最多检查 10,000 个目录/文件条目、累计读取 16 MiB 文本字节；扫描合作式截止时间为 `min(context.timeout, 10秒)`，匹配消耗其剩余预算。保留已有正则 200 字符、行长 4096、最多 200 条匹配等限制。
- 正则使用单个短生命周期子进程处理本次有界输入；固定可信解释器和包内绝对 helper 路径，以隔离方式启动（如 `-I -S`），不经 shell，不加载工作区同名模块。沿用进程树终止、输出上限、取消、清理所有权。
- 推荐扩展 `run_bounded_process(..., stdin_bytes: bytes | None = None)`；默认 None 完全保持原行为。仅内部调用提供严格上限（32 MiB）的 UTF-8 JSON 输入，stdin writer、stdout/stderr reader 和子进程共同受同一 deadline/cleanup 管理。不要把源码放在命令行参数、日志或审计中。
- 超限输出可以保留有限匹配供诊断，但必须明确未完整搜索；不能把超时/扫描预算耗尽返回为成功的“无匹配”。达到正常 200 条结果上限可成功返回截断标记。
- 普通文件 I/O 的合作式预算不等于操作系统硬超时；本期硬隔离解决的是正则计算。不得用线程取消声称所有文件 I/O 已能强制终止。
- 已确认无文件写入且清理成功的搜索 timeout 返回 TIMEOUT/REPLAN；清理未确认沿既有 CLEANUP_FAILED/STOP_TASK 路径，不伪造安全完成。

### 2.3 验证义务

共享**规则**，不混淆两种**作用域**：当前任务只要求当前修改得到适用验证；会话记录尚未解除的历史义务。共享规则不等于强迫每个只读任务处理全部历史问题。

- 在 `workspace/check_coverage.py` 抽取纯路径规范化、目标并集覆盖和当前任务结构目录判定；engine/session 不再分别复制前缀覆盖算法。
- 宿主先核验记录 authority、task、当前完整快照、workspace_stable、kind 和有效结果，再传给覆盖计算。只认可 tests/syntax/static；information/script/other 不能清除义务。
- 多条当前有效检查取并集。失败、旧任务、过期、被同签名新记录替代的旧记录、零测试、缩小范围的筛选命令不能复活证据。
- 本轮可信账本确认创建、最终扫描/身份核验仍成立的父目录，与本轮已检查文件形成结构关系后，不再额外残留源码验证义务。只在其本轮受影响子文件全部得到适用覆盖后解除这些父目录条目。
- 独立空目录沿用现有验证要求，本期不改变 directory-only 任务完成规则。外部目录、替换目录、失败补偿目录不能借父目录规则清除。
- 旧数据库中的 pending 只有字符串路径，缺少来源时不得按“当前看起来是目录”自动删除；保留历史 pending/legacy_unknown，并明确其来源限制。只有现有可信检查覆盖或明确、另行设计的迁移证据可以解除。
- 优先不变更数据库 schema：本次新产生的结构目录事实在当前任务收尾正确消费。旧来源不明条目保守保留，不恢复跨重启 evidence。
- UI/结果/持久化复用同一宿主计算结果：允许“当前任务完成，仍有历史待验证项”，但不得把本轮已经解除的结构目录显示为遗留问题。持久化失败不能提前宣称状态已经落库。

### 2.4 检查生成物

本批先处理已复现的 compileall 缓存；不泛化为任意生成物白名单。

- 在 `tools/command.py` 的宿主执行路径为受控 compileall 分配每次命令独占的临时缓存目录，确认其位于被检查/评分工作区之外。
- 仅修改该子进程环境的 `PYTHONPYCACHEPREFIX`，覆盖父环境同名值；不改变用户全局环境，不接受模型指定缓存位置。
- Python 的 `PYTHONDONTWRITEBYTECODE=1` 不足以阻止 compileall 显式写 pyc，因此不能以该变量已设置作为修复。
- 新增小模块 `process/cache.py` 管理目录所有权和生命周期。只有宿主创建并持有的精确临时目录可清理；正常、失败、timeout、取消均走现有清理路径，未确认清理时保留 owner，不能通过 finally 删除任意计算路径。
- 不改变模型原始命令的审批语义；可显示“编译缓存由宿主管理”，审计仅记录受控元数据，不公开主机临时路径。
- 不修改 q-nested-create 的 task、allowed_changes、fixture、hidden verifier，不在 eval 快照中全面忽略 `__pycache__`。
- 保留负例：用户/Agent 直接在工作区新增不允许的缓存文件，仍应被评为 change_out_of_scope；隐藏 verifier 仍只能评分，不能解除 Agent 义务。

## 3. Review Focus

1. DONE 与 usage 在同一字节块、任意分块或没有后续 EOF：A1 必测一次完成和不漏 usage。
2. timeout/取消发生在连接建立、阻塞读取、关闭期间：A1 必测迟到资源、旧 worker 不影响新任务、清理未确认不释放锁。
3. 正则灾难性回溯、stdin 堵塞、工作区同名 helper：A2 必测超时终止、输入输出有界和可信程序来源。
4. 子文件成功但父目录替换、历史目录同名、部分文件仍失败：B1 必测来源/身份/版本不被路径前缀代替。
5. 缓存目录创建/删除失败、父环境注入路径、评测恶意缓存文件：B2 必测严格失败、清理所有权和 scope 负例。

## 4. A1：流式终止与有界等待

**Files:** 修改 `src/tricoder/providers.py`；必要时新增 `src/tricoder/process/provider_stream.py` 封装受管 urllib worker；只在确有需要时最小调整 `task_cleanup.py`、Provider 失败审计。新增 `tests/test_provider_liveness.py`，扩展 `test_provider_streaming.py`、`test_agent_async.py`、`test_session_integration.py`。

**接口要求：** 保持 ModelProvider.stream 和传输层既有调用契约；可增加带默认值的内部预算对象/时钟注入。公开 ProviderError 保持旧构造方式可用。不得靠 collector 单方面 break 后遗留未关闭传输。

- [x] 先写 RED：完整响应+DONE 后连接不关闭，request 应返回一次且关闭资源；当前实现应由测试外部保护超时报错。
- [x] 增加 RED：仅注释心跳、无首进展、一次文本后停滞、持续有效工具参数、总时长耗尽、退避占用总预算。
- [x] 实现第 2.1 节规则；原始 SSE 字节流到 ProviderResponseCollector 全链路验证，不能只 mock 最终响应。
- [x] 加取消/资源回收测试：token 取消与 asyncio 取消、读/close 阻塞、连接迟到、清理失败、重试前旧资源结束。至少一组本地回环 HTTP 或可控阻塞 response 测试覆盖真实 urllib worker；不得访问公网。
- [x] 检查已发布文本/工具开始后不重试、不重复副作用、残缺工具不执行、终态事件只发一次；保留现有摘要更短 timeout 的约束。
- [x] 聚焦 GREEN，更新 README 的 timeout 语义和 project.md；保存实际日志后进入 A2。

## 5. A2：可终止的正则搜索

**Files:** 修改 `tools/search.py`、`process/control.py`（仅内部有界 stdin 能力）、必要的 ToolContext 内部注入；新增 `process/regex_worker.py`、`tests/test_search_limits.py`；扩展 `test_tools.py`、`test_subprocess_control.py` 和清理/取消集成测试。

**接口要求：** `search_text` 模型工具 schema 保持不变；helper 的输入为有界文本记录与 pattern，输出为匹配/截断/错误的 JSON 数据。不得赋予 helper 文件写入或模型代码执行能力。

- [x] 先写 RED：合法长度的 `(a+)+$` 与 30 个 a 加 !，工具应按小预算终止；测试自身在独立子进程中设更大的硬截止时间，避免 RED 卡住测试主进程。
- [x] 为可选 stdin_bytes 补默认兼容、输入拒绝超限、writer 堵塞/提前退出/BrokenPipe、取消与进程树清理测试，再实现最小传输。
- [x] 实现固定 helper 和父进程扫描预算；每批/每文件检查取消，传递剩余预算，不对每行重新分配完整 timeout。
- [x] 验证正常 regex/literal、中文、无匹配、无效表达式、200 条截断、条目/字节/时间预算、gitignore、敏感路径/链接、子进程非法 JSON/超大输出。
- [x] 验证 timeout 后能执行下一次正常搜索，且 TaskCleanup 没有遗留；清理故障对照必须阻止复用，不能通过吞异常通过测试。
- [x] 聚焦 GREEN，修正 README 中“长度限制即可防灾难性回溯”的表述。记录 A 批结果；A 的未解决资源清理问题必须先解决再进入 B。

## 6. B1：统一覆盖规则与目录义务

**Files:** 新增 `workspace/check_coverage.py`；修改 `engine/completion.py`、`engine/validation.py`、`engine/verification_feedback.py`、`session/runtime.py` 的对应小函数。扩展 `test_finish_coverage.py`、`test_verification_obligation.py`、`test_session_integration.py`；必要时扩展 Store/恢复和展示测试。

**接口要求：** 共享模块提供规范相对路径、目标覆盖并集、结构父目录归属计算的纯函数；不依赖 engine/session/UI，不签发 capability，不读取模型摘要。调用者负责 authority 和快照门禁；不要将 SessionRuntime 整体拆分混入本批。

- [x] 先写 RED：真实 Runtime 创建 app/demo/main.py（补父目录）→定向 compileall→finish；断言 ok=True、pending 不含本轮 app/app/demo/main.py 与父目录，而非仅断言文件被清除。
- [x] 同一场景覆盖显式 create_directory、预建目录、a.py/b.py 分次检查、部分覆盖；核对 finish feedback、最终 RunResult、会话内存及持久化恢复中的义务。
- [x] 提取共享覆盖规则，使用当前有效记录；修正 Runtime 的 `kind != information` 宽泛条件和旧记录选择，避免 script/other 或被替代旧成功清除历史义务。
- [x] 结构目录只能由本轮可信效果+最终状态确认解除；增加父目录被替换、扫描不完整、补偿失败、同名历史 pending 对照。独立目录原规则继续通过。
- [x] 确认只读任务可以完成且保留真实历史 pending；legacy_unknown 不因本轮一个文件检查消失；旧数据库加载不自动擦除目录条目、不恢复 evidence。
- [x] 保留一次 finish 恢复、失败/过期/零测试/过滤检查不通过、两文件缺一个检查仍未完成等全部既有测试；最后扫描/持久化失败仍能否决提前成功。
- [x] 聚焦 GREEN，更新状态字段解释，记录新旧数据兼容限制后进入 B2。

## 7. B2：缓存外置与固定评测闭环

**Files:** 新增 `process/cache.py`；最小修改 `tools/command.py`、`process/env.py`；新增 `tests/test_command_cache.py`，扩展 `test_subprocess_env.py`、`test_eval_regression_suite.py`、取消/清理测试。只有确有装配需要时修改 ToolContext/Runtime；不修改题目与评分规则。

**接口要求：** 缓存资源由宿主创建，执行路径取得受控 child env，并在同一资源所有权链上关闭。缓存临时目录不进入验证覆盖目标、不进入会话源码账本，不作为额外模型输出。

- [x] 先写 RED：保留原 q-nested-create 离线动作序列，把旧“预期 change_out_of_scope”的断言改为整题 passed；记录确由缓存导致的失败。
- [x] 用真实 Python compileall 验证 child env 缓存重定向，不接受 mock 环境或单纯设置 PYTHONDONTWRITEBYTECODE 的假通过。
- [x] 验证目标文件与目录两种 compileall 命令；父环境 PYTHONPYCACHEPREFIX 指向工作区时仍强制使用宿主目录，父进程环境不变。
- [x] 增加缓存目录创建失败、正常/失败/timeout/取消清理、清理故障与所有权保留测试。目录清理前校验精确创建路径，禁止按宽泛前缀递归删除。
- [x] 原题保持 Agent 自身验证通过、隐藏产物检查通过、范围合规、整体 passed；同时保留非法缓存修改负例和“只有脚本成功仍缺验证”的负例。
- [x] B1+B2 联合测试真实 Runtime：创建嵌套源码→运行脚本→第一次 finish 得到缺口→定向 compileall→第二次 finish 完成→保存/关闭/恢复→只读回顾；没有本轮父目录残留，没有缓存越界，没有隐式新增业务 Provider 调用。
- [x] 聚焦 GREEN，再执行第 8 节最终回归并记录交付结果。

## 8. 验证命令与验收口径

在项目根使用已有虚拟环境，不安装工具。每一任务先执行相应文件 pattern；相邻聚焦测试可用 Python unittest loader 组合，避免无意义地反复跑全量。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_provider_liveness.py" -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_provider_streaming.py" -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_search_limits.py" -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_subprocess_control.py" -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_finish_coverage.py" -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_verification_obligation.py" -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_command_cache.py" -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_eval_*.py" -v
```

两批全部完成后运行一次完整回归：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q src tests
git diff --check
```

另纳入已有 `test_agent_async.py`、`test_session_integration.py`、`test_finish_verification_recovery.py`、Provider 同步兼容、模块导入边界及涉及的取消/清理/基线恢复测试。skip 按实际原因报告，不将跳过计为通过。超时测试的外部保护必须大于被测预算，清晰区分“产品按时返回”与“测试强杀兜底”。

最终验收必须同时满足：

1. DONE 后不等 EOF，心跳不无限续命；有效 reasoning/工具参数增长不误触 idle；总预算覆盖重试退避。
2. 正则阻塞能终止，退出后资源可用；清理不明时保持拒绝复用。
3. 本轮已确认的父目录不残留 pending；真正历史义务与未知状态不丢失。
4. 原 q-nested-create 完整离线路径通过，非法额外修改仍失败；隐藏评分不修改 Agent 完成事实。
5. 既有审批、工具结果配对、一次恢复、取消、会话/工作区锁、基线恢复和持久化失败行为不退化。

## 9. 交付与停止边界

- 每批更新 project.md：完成项、设计取舍、实际命令/结果、未验证平台与剩余问题；在 runtime 指定目录保存 RED/GREEN 证据。
- 如果发现必须放松安全边界、修改原题评分、访问真实凭据或安装依赖才能继续，停止依赖该动作的工作，说明具体原因；其他独立测试/文档仍可继续。
- 交付只报告真正通过的批次，不把局部通过、mock 通过或既有 1698 项结果写成全链路已修复。不得宣称真实模型成功率已经提升。
- 本文的阈值是第一版实施决策，实施中如有实测不合理，先给出证据和替代值并更新文档；不能为了测试变绿暗中放宽预算。
- 本轮不自动 commit/push。最终汇报修改文件、复现与修复证据、全量结果、保留风险以及下一次独立复审入口。
