# Project: tricoder-cli

## 2026-10-05 Windows CI 修复（本地完成，等待远端验证）

- 按用户要求取消 Linux CI，保留 Windows Python 3.11/3.12。新增 `test` 可选依赖 `setuptools>=68`，CI 安装 `.[test]`，解决包发现测试缺少构建工具的问题；未在本机安装依赖。
- 敏感路径扫描测试的 guard 改用规范化根路径，避免 Windows 临时目录别名等路径写法差异导致 relative_to 抛 ValueError 并被包装成 io_error。用包含 `..` 的等价目录稳定复现旧失败；关闭路径排除的反向验证仍触发敏感文件断言，没有放宽安全检查或修改生产扫描逻辑。
- 在 `runtime/ci-windows-fix` 独立 worktree（`codex/fix-windows-ci`，基线 `47d08af`）验证后，将 CI、pyproject、测试和 README 的最小补丁回填当前工作区；命令兼容任务的既有改动保留。本次未修改 src、未提交或推送。
- 隔离完整回归：Windows / Python 3.11.6，1355 tests，OK，11 skipped，212.969s。隔离与回填后聚焦回归均为 22 tests、OK、1 skipped；185 个 Python 文件内存语法检查、CLI --help、git diff --check 通过。完整回归不包含主工作区另行进行的命令兼容修改。
- 证据：`runtime/ci-windows-verification.md`。本机没有 Python 3.12，未执行新环境依赖安装；本次提交包含该修复，推送后仍须检查两组 Windows CI。Linux inode 重用相关测试问题未修复，恢复 Linux CI 前需要处理。

## 2026-10-05 命令兼容与错误恢复（S0—S5 已实施）

- 执行文档：`docs/superpowers/plans/2026-10-05-command-compatibility-and-recovery.md`；阶段证据位于 `runtime/command-compatibility/`。实现基线为已经提交并推送的 `47d08afb02f421f6130ea963ffcf33a4f1b9efad`，本轮改动由本次提交收口。
- 六个 Python 请求别名统一到当前会话可信解释器；固定版本查询归一化为隔离参数且不产生 UNKNOWN 或验证证据。脚本、unittest 目标、discover 路径与审计均按同一个单次有效 cwd 解析，不修改共享策略。
- unittest 单段简写只映射到 cwd 内已存在的 `.py` 普通文件。两个枚举化命令形式错误可返回固定脱敏的 `INVALID_ARGUMENT / REPLAN`；直接测试工具先经过等价 `python -m` 的完整安全校验。越界、敏感路径、禁止选项、未知程序、拒绝、取消与未知副作用继续停止，没有全局降级 `POLICY_DENIED`。
- native/legacy_json 共用命令说明。审批显示原请求、归一化 argv、有效 cwd 与超时；Console 标题区分补丁、命令、扩展、MCP 和工作区门禁。strict/relaxed/fullaccess、read_only 与危险 MCP 边界保持既有语义。
- 合成计算器使用 fake Provider 和当前虚拟环境的真实 Python 进程验证：native/legacy 成功路径获得真实 unittest 输出和有效工作区证据；错误实现测试失败、修改后仅查版本均不能被 finish 文案提升为成功；同步与异步工具入口一致。未调用真实 Provider、未读取真实密钥或用户会话数据库。
- 最终 Windows / Python 3.11.6 新鲜回归：`Ran 1384 tests in 263.940s`，OK，12 项条件跳过；186 个 Python 文件内存语法检查、CLI `--help` 与 `git diff --check` 均退出 0。独立审查最初发现兼容构造的 CommandPolicy scoped view 未重新过滤目标工作区 PATH，新回归先证明会命中工作区 fake `git.exe`，修复后复核关闭；当前命令实现无剩余 Critical/Important。未验证 Linux/macOS、Python 3.12 和真实 Provider 端到端质量。并行出现的 Windows CI/pyproject/快照测试修改由上方独立任务记录，不归入本轮命令改造。
- 提交前合并工作树复验首次发现两个真实 MCP stdio 用例在当前 Windows 机器上超时：fixture 仅导入 SDK 后立即退出已需 1.83—1.90 秒，而测试初始化预算为 2 秒。只将这两个测试的初始化/清理预算放宽到 5 秒、外层看门狗放宽到 7 秒，未修改生产超时或错误分类；聚焦 `2/2` 通过，随后完整回归 `Ran 1384 tests in 416.355s`，OK，12 项条件跳过。

## 2026-10-05 第二轮模块整理（S0—S5 已完成）

- 执行文档：`docs/superpowers/plans/2026-10-05-module-organization-round2.md`；实施证据位于 `runtime/module-organization-round2/`。本轮以第一轮未提交工作树为事实基线，没有用 HEAD 覆盖既有改动，也未重做第一轮。
- 14 个唯一实现已分别迁入 `tricoder.process`、`tricoder.workspace`、`tricoder.session`、`tricoder.presentation`；原根模块现在是显式薄转发层。内部生产代码和常规行为测试使用规范新路径，新旧常用类型、函数、异常与状态对象保持对象同一性。
- Eval 隐藏进程 helper 不再假定依赖文件同目录，而是分别定位 `process.control`、`task_cleanup`、`core.cancellation` 的真实源码。隔离测试清空 `PYTHONPATH`、使用 `-S -B`、放置恶意同名包，并真实执行成功命令和超时清理命令。
- 导入守卫覆盖 14 个旧入口、绝对/包级/相对/动态导入形式、四个轻量包、不同冷导入顺序、MCP/tools 与 Agent 边界；`session.store` 不会带入 Runtime/Agent/TUI，`presentation` 包不会提前加载 Textual。第一轮记忆异常进度、finally 单次合并和两实例隔离测试保持通过。
- 聚焦验证：process/Eval/MCP 相关 145 项通过（隐藏 helper 组 1 项按符号链接权限跳过）；workspace 138 项通过（6 项平台/权限跳过）；session 221 项通过；presentation 126 项通过；最终模块/Agent/MCP 导入守卫 36 项通过。各集合有交叠，不能相加为全量总数。
- 最终 Windows / Python 3.11.6 新鲜回归：`Ran 1355 tests in 289.426s`，OK，11 项既有平台/权限跳过；compileall（缓存仅写 `runtime/module-organization-round2/pycache`）、CLI `--help` 与 `git diff --check` 均退出 0。
- 分发产物未验证：当前环境没有 `build` 和 `wheel`，且 setuptools 65.5.0 低于 `pyproject.toml` 的构建要求 68；按授权未安装或升级依赖，因此没有声称 wheel 验收通过。Linux/macOS、Python 3.12、手工 TUI 和真实 Provider 也未验证。
- 新结构说明：`docs/framework/module-layout.md`。本轮没有拆分 `SessionRuntime` 内部状态机、扩展 Provider、改变 SQLite schema/CLI 参数/审批边界，也没有提交或推送。

## 2026-10-05 第一轮补齐再次复审（通过，第二轮可开始）

- 对照补齐前有限源码基线审查 coordinator/loop/agent；F1 的依赖边界、窄字段合并、异常部分提交与 F2 的两实例隔离测试均已落实，未发现阻塞性问题。
- 本次实际运行 `.venv/Scripts/python.exe -B -m unittest discover -s tests -q`：1337 tests，OK，11 skipped，301.275s，退出码 0。运行中有既有 asyncio/Textual 慢回调诊断；没有为测试通过修改源码或断言。
- 独立只读复核完成 52 个纯内存基线对照场景，状态、审计事件及异常身份一致，覆盖取消、KeyboardInterrupt/SystemExit 和回调异常；未调用真实 Provider。
- 复审内容标识与环境限制保存在第二轮计划第 0 节。第二轮文档已更新前置状态、记忆异常进度/隔离回归要求，以及新目录下导入守卫的适配要求。
- 下一步：将 `docs/superpowers/plans/2026-10-05-module-organization-round2.md` 第 8 节提示词交给 coding session，按 S0—S5 实施。本次未修改功能代码、未启动目录迁移、未安装依赖或提交。

## 2026-10-05 第一轮重构审查补齐（C0—C4 已完成）

- 执行文档：`docs/superpowers/plans/2026-10-05-agent-refactor-round1-review-fixes.md`；实施证据位于 `runtime/agent-refactor-round1-review-fixes/`。本轮以未提交的第一轮工作树为事实基线，没有从 HEAD 覆盖用户改动，也未启动第二轮目录迁移。
- F1 已补齐：`MemoryCoordinator` 不再导入 engine 或接收 `AgentRunState`。Runner 通过不可变 `MemoryStepInput` 传入 Session 快照、完整固定前缀与工具定义；协调器返回窄 `MemoryStepResult`，异常进度由每次调用独享的 `MemoryStepProgress` 保存，Runner 在统一 `finally` 中单点合并。
- 异常语义保持：摘要调用前计数、收到 usage 后计量、审计成功后才发布压缩/候选。第一批压缩成功后第二批摘要失败、审计失败或原生取消仍保留第一批；`on_error`/`log` 抛出的首异常保持对象身份；保存候选全批与候选审计通过前不发布中间候选，也不擅自提前合并旧实现未统计的调用/usage。
- F2 已补齐：新增两个独立 Agent 的 `asyncio.Event` 确定性交错测试，覆盖消息、call ID、候选、业务/记忆用量、工具计数、显式父令牌取消和 `Task.cancel()` 隔离；另固定工具调用事件后断流、缺少完成事件及 event sink 首异常三个 Provider 边界。未实现或声明同一 `CodingAgent` 实例并发支持。
- 阶段证据：C0 基线 Agent 113、记忆 84、跨边界 122（跳过 1）项通过；C1 表征 6 项、C2 最终聚焦 34 项、C3 新专项 6 项及异步组合 23 项通过。独立复审最初发现导入守卫绕过（Important）与交错测试无界等待（Minor），均已补回归并由同一审查 Agent 复核关闭；当前无 Critical/Important。
- 最终 Windows / Python 3.11.6 验证：`Ran 1337 tests in 309.134s`，OK，11 项既有平台/权限门禁跳过；本轮新增测试无跳过。compileall 与 `git diff --check` 通过。未调用真实 Provider，未验证 Linux/macOS、Python 3.12 或同一 `CodingAgent` 实例并发；运行中保留了既有 asyncio/Textual 慢回调诊断。第二轮目录整理的前置条件现已满足，但本轮没有启动第二轮。

## 2026-10-05 第二轮目录整理交接（待实施，前置复审通过）

- 执行文档与 coding session 提示词：`docs/superpowers/plans/2026-10-05-module-organization-round2.md`。
- 范围：将 14 个实现模块分组迁入 `process/`、`workspace/`、`session/`、`presentation/`；旧路径保留薄兼容模块，内部使用新路径。保持业务行为，不拆 SessionRuntime 内部逻辑，不扩展 Provider。
- 第一轮现已产生 `engine/` 与 `context/coordinator.py` 等边界；第二轮 S0 仍须以当时最新工作树重新核实 R0—R6 证据、旧导入与 Eval 独立依赖闭包，不能仅依赖本条历史记录。
- 前置补充：`docs/superpowers/plans/2026-10-05-agent-refactor-round1-review-fixes.md` 的 C0—C4 已完成；本轮未启动第二轮。后续开始迁移时仍须重新核对当时工作树和独立依赖闭包。
- 关键风险：Eval 隐藏 helper 按 `__file__` 复制源码；搬目录必须保持其独立运行依赖闭包。另需覆盖实际 patch 查找位置、类型/ContextVar 唯一性、冷导入、锁与快照门禁、分发包资源。
- 本次只写第二轮计划和本交接记录，未改功能代码、未运行功能回归。下一步由 coding session 按 S0—S5 执行，保留现有工作树改动；证据放 `runtime/module-organization-round2/`。
- 文档验证：14 对迁移路径及其源文件、S0—S5 阶段、引用测试文件、代码围栏和空白检查通过；`git diff --check` 通过。这些检查不代表源码重构已实施或功能回归已通过。

## 2026-10-05 Agent 第一轮职责拆分（R0—R6 已实施）

- 执行文档：`docs/superpowers/plans/2026-10-05-agent-refactor-round1.md`。
- 范围：保留 `agent.CodingAgent` 入口，拆分历史、Provider 消费、观测审计、单任务状态、记忆协调、工具批次与结果收尾；本轮不整体搬目录、不重构 SessionRuntime、不新增 Provider 或断流恢复。
- 实施结果：`agent.py` 从 1857 行的综合实现缩减为公开门面；单任务编排、状态、Provider 收集、观测审计、工具批次和收尾分别落在 `engine/`，历史与记忆协调分别落在 `context/history.py`、`context/coordinator.py`。
- `CodingAgent` 的构造参数、同步/异步入口、`context_manager` 与现有公开导入保持兼容；内部执行每次创建独立 `AgentRunState`，没有跨任务共享消息、计数、验证或记忆候选。
- 保持的关键边界：工具副作用先发布再构造/通知；批次首次停止后剩余调用只补 `skipped`；取消、清理、审计首异常、验证 authority、`unknown_effects`、消息配对、记忆覆盖水位和业务/记忆用量分离均由既有专项回归覆盖。
- 本轮未重构 `SessionRuntime`、providers.py、数据库/CLI 配置，也未改变 Session/工作区锁和快照门禁；没有调用真实 Provider、读取真实密钥或安装依赖。
- 实施证据与阶段日志：`runtime/agent-refactor-round1/`。完整结果及剩余限制见其中 `progress.md`；本地验证不能替代 Linux/macOS、Python 3.12 或真实 Provider 验证。
- 最终 Windows / Python 3.11.6 确认：全量 1318 tests，OK，11 skipped；Agent 113、批次 24、记忆 84、验证/错误/副作用 131、Eval 118、工作区 37、导入/MCP 边界 16 均有独立日志。compileall 与 `git diff --check` 通过；独立复审无 Critical/Important。一次全量曾在 BaseException 压力用例中出现 Windows 锁非稳定失败，目标用例 6 次独立复跑及随后全量均通过，详见实施记录，未据此修改锁语义。

## 2026-10-04 工作区锁与任务前快照确认（W1—W6 已实施）

- 任务文档：`docs/superpowers/plans/2026-10-04-workspace-lock-snapshot-gate.md`。
- W1：新增跨进程 `WorkspaceLock`。锁只按规范工作区根确定，和 Session ID、
  会话数据库无关；Windows 真实子进程已验证同根竞争、异根并行、不同数据库同根竞争。
  内部锁另有稳定用户级守卫；Linux 实现抽象 socket 内核锁以避免两个文件系统锁名同时被
  替换后形成 split-lock。空闲入口不持锁；活动标记仅在真实执行前建立，崩溃遗留必须人工
  确认资源已结束后恢复。
- W2/W3：新增有界双清单内容快照与三分支门禁。无变化直接执行；有变化完整分页展示，
  明确确认后再次全扫；再次变化重新确认。扫描失败、超限、不稳定、无确认器或取消均在
  Provider/工具/待执行消息之前阻断，`fullaccess` 与只读任务也不能绕过。
- W4：每个 Session 的源码基线仅驻当前 Runtime 内存；切换/关闭即清除。恢复会话无基线时
  明确确认初始化。外部变化撤销与当前代码不匹配的验证证据，并向 Agent 注入不含源码正文的
  临时安全提示；语义记忆、审批、`unknown_effects` 和原始对话保持原语义。
- W5：单次 CLI、Shell/TUI 的 `SessionRuntime` 路径、撤销和 Eval 合成副本均接入门禁。
  撤销预览持有工作区锁，提交前复扫绑定快照；拒绝或旧预览不会写入。任务末仅在结果成功、
  账本可完整解释且无 taint/未知效果时更新基线，否则保留待确认差异。撤销成功还会用工具
  返回的真实发布后 inode/内容/权限证据做收尾归因；Eval 的 Runtime 清理返回 `False` 时
  保留诊断数据库并阻止进入验证。
- W6 新鲜验证（Windows / Python 3.11.6）：锁专项 `Ran 13`，OK（4 项平台/权限跳过）；
  CLI/TUI/Eval/门禁组合回归 `Ran 225`，OK（5 项平台/权限跳过）；最终完整回归
  `Ran 1306 tests in 308.253s`，OK（11 项平台/权限跳过）；`compileall -q src tests` 与
  `git diff --check` 均通过。
- 实施证据：`runtime/workspace-consistency-implementation/verification.md`。未读取密钥、
  `.env.local` 或真实会话库，未调用真实 Provider、安装依赖、恢复 Docker、提交或推送。
  Linux/POSIX、网络文件系统、Python 3.12、手工 TUI 交互和非协作外部进程仍未实测。

## 2026-09-28 无占用入口与 Session 延迟创建（任务 1—5 已实施）

- 文档：`docs/superpowers/plans/2026-09-28-session-landing-lazy-creation.md`。默认
  `chat`、裸 CLI 与 TUI 现在进入纯内存入口：不恢复 latest、不生成伪 `default` 行、
  不取得 Session 锁，也不装配 Provider/Agent。
- 首次非空普通任务在任务互斥区内创建、自动命名、锁定并发布唯一 UUID Session；
  任务正文不参与名称生成。首次创建提交前取消/退出不留行，提交后 Provider 失败保留
  已创建 ID，避免静默重试为另一个会话。
- `initial_session_id` 是唯一显式恢复入口。真实 Session（包括历史名称 `default`）继续
  跨进程互斥；被占用的历史会话不会降级到 latest、新建或使交互入口退出。
- Shell/TUI 使用可空 Runtime 状态投影。入口中的模型和权限是首次任务待用草稿；需要
  真实 Session 的命令返回稳定提示。TUI 用完整 UUID 作为选项值，名称和遇到碰撞会延长
  的 ID 前缀只作展示，同名/同模型不会串会话。
- Eval 重启在关闭前保存完整活动 ID，并显式恢复同一 ID；按名称切换匹配到多条记录会
  拒绝歧义，完整 UUID 仍精确匹配。
- 聚焦证据：入口/Runtime/所有权/CLI/Shell/UI/Eval `Ran 182`，OK；迁移后记忆/
  Effect 专项 `Ran 64`，OK；Session/可靠性/验证证据专项 `Ran 86`，OK（1 项平台跳过）。
  Windows 两个真实子进程可同时停在入口且不增行，并发首次提交创建不同 UUID。
- 最终新鲜门禁（Windows / Python 3.11）：`unittest discover -s tests -q` 运行
  `1255 tests in 313.760s`，OK，6 项为既有平台/权限跳过；`compileall -q src tests`
  与 `git diff --check` 均退出 0。Textual/asyncio 的慢回调行是诊断输出，不是失败。
- 未读取 `.env.local`、真实密钥或用户会话数据库，未调用真实 Provider、安装依赖、
  提交或推送。POSIX、手工 TUI 与 Python 3.12 尚未验证。

## 2026-09-28 Session 跨终端独占与切换交接（已完成）

- 用户选择：切换成功后释放旧 Session；再次切回必须重新读取持久化状态，不复用旧的完整消息、未保存语义候选或撤销账本。模型切换仍保留同一个 Session 的上下文与账本。
- 实现：`session_lock.py` 用标准库提供本机非阻塞文件锁，Windows 字节区间锁 / POSIX flock；锁按数据库规范路径与 Session ID 散列区分，位于数据库同级 `session-locks/`，不使用 PID、过期抢占或删除锁文件。数据库 schema 不变，无新增依赖。
- 生命周期：先取得目标所有权再加载记忆、构建工具与清理 spill；成功交接后释放旧锁。占用、候选构建失败、旧记忆保存失败或 pending-clear 未完成均不交出旧会话。遗留任务资源未回收时禁止切换；关闭请求交由任务所有者收尾，正常退出/初始化失败释放，进程终止由 OS 回收。
- 入口：CLI/TUI 显示固定占用提示；Shell 异常及 TUI unmount 清理 Runtime。Eval 模拟重启显式关闭旧实例，异常和 trial 结束也释放资源。既有测试中模拟重启的实例相应补真实 close，不关闭互斥保护来迁就测试。
- 自审修复：普通状态变更即将释放任务锁时，并发 close 原可漏接请求；新增受控线程竞争用例先复现，再将关闭标记检查与任务锁释放置于同一个状态锁交接。新增独占专项 18 项通过（含真实独立进程竞争、正常/强制退出、失败交接、CLI/TUI 与退出竞争）。
- 最终验证（Windows / Python 3.11）：独占专项 18 项通过；最终代码完整回归 `Ran 1233 tests in 313.362s`，OK，6 项既有平台/权限跳过（1227 通过）；证据 `runtime/session-ownership-final-verification.log`。Session 邻接 95 项、记忆 84 项通过；验证证据 75 项中 1 项平台跳过。compileall 与 `git diff --check` 通过。日志中的 asyncio/Textual slow-callback 为诊断，不是失败。
- 边界：只保护同一数据库路径下遵守协议的 Runtime；不同 Session 指向同一工作区仍无工作区锁。POSIX、网络文件系统、旧版进程和脱离管理的外部子进程不在本次 Windows 验证范围内。未访问真实会话库、未读取密钥、未调用真实 Provider、未安装依赖、未提交或推送；既有 Eval 未提交改动保留。
- 后续可在 Linux CI 验证 flock 分支，本次无阻塞项。日常使用要保留语义记忆时，在切换前明确预览确认 `/memory save`；退出或切换不会隐式保存未审阅内容。

## 2026-09-23 Agent 效果量化（E1—E6 已实施，真实质量未测）

- 文档：`docs/superpowers/plans/2026-09-23-agent-evaluation-implementation.md`；证据：`runtime/agent-evaluation-implementation/`。
- E1/E2：新增严格 v2 suite/实验定义、旧 smoke/v1 适配、分维度指标、确定性重复调度、条件轮换、时间/取消预算、独立 trial 状态和逐次原子结果。代码正确、正常 finish 与端到端完成分开；未执行、基础设施错误和缺失 usage 不会被删除或当零。
- E3：普通多轮走 `CodingAgent.run_with_context`；固定保存/重启/切换/撤销动作走临时 SQLite 与 `SessionRuntime` 公开接口。实验的 structured/reviewed-summary 配置真实传入 Agent，摘要调用及独立 token 写入结果；不同重复不共享上下文。
- E4：新增白名单故障注入，区分 Provider 透明传输重试、Agent 工具重规划和审批拒绝；只在实际命中时统计恢复。安全观测记录危险请求、实际执行、重复绕过及合法操作放行，不执行真实攻击代码。
- E5：新增 `evals/quality-v1/` 30 道合成自建题，六类各 5，dev/holdout 为 18/12；正确结果和典型错误结果各运行一次隐藏 verifier，契约分别通过/失败。原 smoke 保留。
- E6：v2 报告包含条件、不可逆实验指纹、quality/contract 与类别分组、恢复/安全/记忆/效率、失败阶段及全部代码；`eval-compare` 默认拒绝不兼容实验，仅允许显式变量并保留未配对、提升和回退。未提供价格表时费用保持 null。
- 新鲜离线验证（Windows / Python 3.11）：最后代码改动后的 Eval 专项 `Ran 114 tests in 35.541s`，OK，2 项既有链接权限跳过；全项目 `Ran 1215 tests in 175.311s`，OK，6 项既有平台/权限跳过；30 题正确/错误各一次共 60 个 verifier 结果全部符合预期；compileall、旧 smoke dry-run、Quality V1 dry-run、`git diff --check` 均 exit 0。测试期间的 asyncio/Textual slow-callback 输出是诊断，不是失败。
- 兼容与边界：默认单次 `tricoder eval evals/smoke` 行为和 v1 报告保留；新能力不新增依赖。Harbor、Inspect AI、Langfuse、Docker 和真实 Provider 调用均未纳入本次；离线 fake/fixture 结果不能表述为 Agent 质量提升。
- 回退：继续使用旧 smoke 单次入口即可绕过实验层；删除新生成的 `runtime/evals/<run-id>/` 不影响会话数据库或项目源码。禁用记忆条件使用 `memory_compaction="off"`、`memory_persistence="off"`。

## 2026-09-22 Docker 真实验证未通过并回退

- 实现提交 `eece357` 曾完成 Docker P0—P5 代码、模拟生命周期测试与本地回归，但从未推送到 `origin/main`。
- 真实验证尝试安装了 Docker Desktop 4.91.0 per-user 版本；安装器 SHA-256 与 Docker Inc. 数字签名验证通过，WSL2 数据盘开始初始化。
- Docker 首次启动的订阅服务协议未被接受，engine 随后退出；因此没有拉取镜像、启动容器或验证真实挂载、断网、资源限制与清理闭环。本次真实 Docker 验收标记为**未通过**，不能沿用模拟测试结论声称真实隔离可用。
- 按用户要求，本节所在提交撤销 `eece357` 的源码、测试和 README 变更，当前产品重新回到默认本地执行、尚未实现 Docker 沙箱的状态。Docker Desktop 进程已停止，但软件保留安装，方便后续由用户本人接受协议后重新测试。
- 后续恢复时可 revert 本次回退提交或重新应用 `eece357`，然后使用合成项目与无秘密镜像重新完成真实验收；在此之前不要发布或启用 Docker 模式。

## 2026-09-21 Docker 沙箱可行性与实施交接（待实施）

- 方案：`docs/superpowers/plans/2026-09-21-docker-sandbox-implementation.md`。
- 结论：架构可行；采用独立工作副本、容器执行、确认后回写。需覆盖 CLI、SessionRuntime、Eval 及 MCP/扩展边界。
- 环境：当前 PATH 未找到 Docker，未验证 daemon、镜像、挂载或实际隔离；未安装、拉取或启动容器。
- 本次仅创建方案并更新交接记录，未修改功能代码或运行功能回归。
- 下一步：按 P0—P5 实施；保留现有未提交修改，先完成可独立实施的接口与模拟测试，再在获授权的 Docker 环境完成真实验收。

## 2026-09-21 会话记忆第二轮审查修复（S1—S3 已完成）

- 文档：`docs/superpowers/plans/2026-09-21-session-memory-review-round2.md`；证据：`runtime/session-memory-review-round2/`。
- S1：新增只由可信成功任务推进的完成水位；保存预览与最终提交统一拒绝覆盖不足/越界候选。`/memory refresh` 只运行无工具摘要、最多两批，不重跑业务工具或直接写库；失败、取消、迟到结果、部分批次和压缩来源断层均不提交。`/memory` 与保存预览显示候选、运行时、已保存 revision 及覆盖范围。
- S2：决策 `old → new` 的同 ID/同替代关系重放保持幂等，归档清理后仍可凭活跃替代元数据识别；冲突替代关系和未知旧 ID 继续拒绝。相同终结语义即使来自新消息来源也不重复归档。
- S3：新增 `/memory archive` 和 `/memory archive delete <条目ID>`。列表只显示 ID/类别/状态与条目、字符容量；删除使用 Session/generation/revision/原始快照/消息序号绑定的精确预览，确认只改当前内存目标，不改活跃条目或可信执行状态，也不自动保存。再次 `/memory save` 后删除才跨重启生效。
- 连续流程覆盖 A 候选→B 摘要失败→保存拒绝→刷新→保存→替代重放→40 条归档→确认删除→再保存→重启。该流程还发现并修复 `summary_max_chars > 6000` 时可保存但按默认上限加载失败的问题；Runtime 现用当前配置读取，SQLite schema 未变化。
- 最终新鲜验证（Windows / Python 3.11.6）：记忆专项 `Ran 84`；Context `Ran 9`；SessionRuntime `Ran 59`；CLI/Shell/TUI `Ran 61`；全项目 `Ran 1180 tests in 186.769s`，OK，6 项为既有 Windows symlink/reparse 权限跳过；`compileall` 和 `git diff --check` 均 exit 0。TUI 测试出现约 0.109—0.125 秒慢回调诊断但无失败。
- 兼容性：语义记忆仍默认关闭；`persistence=off` 不新增摘要请求或加载已保存语义记忆。schema v2 与旧行兼容规则不变，无数据库迁移。关闭功能不能恢复此前已压缩掉的原始历史。
- 真实 Provider 补充验证：OpenAI、DeepSeek、GLM 的真实 Key 配置、网络和 native 工具协议均可用；三家无工具结构化记忆摘要均通过严格解析。DeepSeek 的 `fix-subtract` 隔离 Eval 完整通过。OpenAI 与 GLM 的同一 Eval 都完成正确文件修改且隐藏验证通过，但分别因 8 轮内未调用 `finish`、触发命令策略拒绝而以 `agent_failed` 结束；两家的最小 `read_file → finish` 真实任务均通过。因此不能表述为“三家完整 Coding Eval 全绿”。
- 仍未验证：真实用户数据库、手工交互式 TUI、Linux/macOS、Python 3.12。真实验证未回显/保存 Key；未安装依赖、未提交或推送。
- 回退：把 `[memory]` 的 `compaction` 与 `persistence` 设为 `off` 并新开会话；不要删除数据表或覆盖工作区。已确认保存的数据会保留但不加载。

## 2026-09-20 会话记忆审查修复（R1—R4 已完成）

- 文档：`docs/superpowers/plans/2026-09-20-session-memory-review-fixes.md`。
- 修复前真实复现：编辑预览缺少会话绑定；收尾候选未覆盖最近任务；20 条待办后新增条目合并失败。跨会话问题在 runtime 接口层复现；CLI 同步确认期间没有切换入口，TUI 异步确认窗口由 runtime 绑定校验兜底。
- R1 已完成：编辑和保存预览绑定 Session、generation、原始记忆快照、消息序号及持久化版本；最终提交重新校验候选结构与敏感内容。4 项原始缺陷复现和 2 项提交边界复现均先失败后通过；R1 最终 8 项及 56 项邻接回归通过。CLI 同步确认下未发现直接切换入口，TUI 异步模态期间仍由 runtime 绑定兜底。证据见 `runtime/session-memory-review/progress.md`。
- R2 已完成：运行时压缩摘要与 `review_memory_candidate` 保存候选分离；保存覆盖位置之后的全部闭合任务，包括最后一个已完成任务，同时不删除近期内存历史。输入超限只按完整任务最多拆成两批，两批仍不足时拒绝标称完整保存且不发布部分候选。保存预览使用独立候选，确认后写库，重启再加载为运行时记忆。修复前单/双任务复现失败，修复后记忆专项 50 项通过。
- R3 已完成：schema v2 增加 `state`、`replaces_id` 与有界归档；运行时压缩保持保守合并，待保存候选支持同 ID 更新、待办终结、决策替代和新任务目标归档。每类活跃项上限 20、归档上限 40；超限拒绝并保留旧记忆/历史。`/memory edit` 在 CLI/TUI 中可选择固定枚举状态，编辑待保存候选不会提前替换运行时正式记忆。
- 数据库兼容：v1 行读取时在内存映射为 v2（旧待办→pending，目标/约束/决策→active），不会后台回写；下一次用户确认保存才以 v2 写入。未知版本或列/payload 不一致继续拒绝。
- R4 已完成：新增假 Provider 连续流程，覆盖任务 A→任务 B 修正→模型切换→保存→重启恢复→会话隔离→clear，并断言真实 context/SQLite 状态和调用次数。README 已同步保存覆盖、状态/归档、额外调用和恢复边界。
- 最终新鲜验证（Windows / Python 3.11.6）：记忆专项 62 项通过；全项目 `Ran 1154 tests in 152.584s`，OK，6 项均为既有 Windows symlink/reparse 权限跳过；`compileall` 与 `git diff --check` exit 0。证据见 `runtime/session-memory-review/`。
- 未验证：真实 Provider 摘要质量、Linux/macOS、Python 3.12、真实用户数据库迁移。未安装依赖、未读取密钥/真实数据库、未调用真实模型、未提交或推送。
- 下一步：在非敏感测试会话手工试用 structured/reviewed_summary；根据真实摘要质量决定是否继续优化候选差异展示和归档选择界面。

## 2026-09-19 真实 Provider 会话记忆兼容性修复

- 真实试用确认业务工具任务成功，但收尾记忆候选被严格解析器拒绝；旧 UI 只显示统一警告，无法区分 JSON、截断、超时和 Provider 故障。
- 修复采用低信任语义对象：模型只返回目标、约束、决定和待办，本地注入 schema/revision/generation/covered_through；兼容包住整个响应的单个 JSON 围栏，但继续拒绝附加说明、未知字段、伪造来源和工具调用。
- `MemorySummaryError` 增加固定失败类别；UI 与审计仅记录安全类别，不记录 Provider 原文。失败类别审计本身失败时继续按现有 fail-closed 规则停止且不提交候选。
- `ContextSnapshot` 区分协议噪声归一化与预算裁剪；结构化模式只拒绝真正的预算裁剪。已经纠错并形成完整工具回合的旧任务可作为原子摘要来源，原始 Session 历史不会因请求视图归一化而丢失。
- 新增真实故障形态的回归测试；未读取密钥或真实会话数据库，未调用真实 Provider。最终验证证据记录在 `runtime/session-memory/provider-compatibility-repair.md`。
- 最终验证：记忆专项 36 项、ContextManager 8 项、完整项目 1128 项均通过；完整项目有 6 个既有平台/权限跳过，compileall 与 `git diff --check` 退出码均为 0。

## 2026-09-18 会话记忆改造（P0—P5 已完成）

- 文档：`docs/superpowers/plans/2026-09-18-tricoder-session-memory.md`；唯一目标目录为 `D:\MaHong\AGENT_WORKSPACE_V2\projects\tricoder-cli`，不修改或创建迁移副本。
- 设计：结构化任务记忆＋近期完整回合；可信验证、审批和未知影响继续由程序管理。先内存摘要，后可选预览确认持久化，再验证恢复与 clear。
- P0—P5 六阶段、M01—M20 验收场景；默认关闭新能力，未添加依赖，未读取密钥或调用真实 Provider。
- 已检查当前代码存在验证证据与未知影响字段，旧计划的“尚未实施”标题不作为当前功能状态依据。
- P0 已完成：读取规则、README、当前代码和事务/清除/命令路由；确认开始时 `src/tricoder/` 与 `tests/` 无用户未提交修改，既有手工实验与计划改动保持不动。
- P0 新鲜基线：context 8、sessions 17、session runtime 59、cancellation 7 项测试均通过；证据见 `runtime/session-memory/baseline.md`。未调用真实模型。
- P1 已完成：新增结构化记忆模型、严格 JSON/来源/长度校验、保守合并、稳定消息编号及 Agent 字段传播；9 项新增测试与 101 项 Agent 邻接回归通过，证据见 `runtime/session-memory/p1.md`。
- P2 已完成：上下文预算纳入固定消息、工具 schema、输出预留及 token/字符限制；只压缩连续闭合任务前缀，工具调用与结果不拆分；校验成功后原子提交，证据见 `runtime/session-memory/p2.md`。
- P3 已完成：独立无工具异步摘要、15 秒可配置超时、取消/截断/非法 JSON 失败关闭、最多两批总结、低信任请求视图注入和独立 memory usage；默认 off 无新增调用，证据见 `runtime/session-memory/p3.md`。
- P4 已完成：新增独立 `conversation_memory` 表、事务 CAS、合成旧库升级、reviewed-summary 恢复、`/memory` 查看/编辑/保存的精确预览确认。候选文本不写审计，关闭模式不加载也不写语义内容，证据见 `runtime/session-memory/p4.md`。
- P5 已完成：`/clear` 递增 generation、清除消息与目标 Session 的语义行，数据库失败进入 pending-clear 并阻止旧记忆复活；补齐取消、会话隔离、失败重试、长历史对比、README 和 M01—M20 映射，证据见 `runtime/session-memory/p5.md` 与 `runtime/session-memory/acceptance.md`。
- 设计调整：正常任务结束的 reviewed 候选固定保留最近两项完整任务；记忆替换改为审计元数据写入成功后才提交，审计失败沿用既有 fail-closed 语义。SQLite 在初始化时创建空表，但 `persistence=off` 不写入语义行。
- 本次最终验证：结构模型 9 项、记忆专项 30 项、邻接回归 162 项均通过；最终完整项目 `Ran 1122 ... OK (skipped=6)`，跳过项均为既有平台/权限门控，本次记忆测试无跳过。未验证真实 Provider 摘要质量、Linux/macOS 和真实用户数据库迁移。
- 下一步：如需实际试用，先在非敏感测试会话启用 `[memory] compaction="structured"`；确认候选质量后再启用 `persistence="reviewed_summary"` 并通过 `/memory save` 保存。默认配置继续保持 off。

## 2026-09-14 LangGraph 独立副本迁移计划（尚未实施）

- 用户要求保留原代码，在独立副本迁移；本次只编写工程文档。
- 文档：`docs/superpowers/plans/2026-09-14-tricoder-langgraph-safe-migration.md`。
- 拟定副本：`D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-langgraph`；实施时原仓库只读，环境、会话、审计、暂存和测试工作区均隔离。
- 设计：LangGraph 仅替换编排，保留可信工具入口；首版顺序执行、不启用磁盘 checkpoint 或崩溃自动续跑。M0—M5 六阶段，A01—A27 验收场景，包含回退演练。
- 当前未复制代码、未安装依赖、未改 src/tests、未执行迁移测试；已有用户修改保持原样。文档静态检查不代表迁移验收通过。
- 下一步：实施授权后重新检查源工作树，从 M0 审核复制清单开始；依赖安装与 Git 操作按实际授权处理。

## 2026-09-14 可靠性前五项任务计划（尚未实施）

- 用户要求将失败改动追踪、工具错误分类、同批失败传播、取消清理、验证绑定文件状态五项写成完整计划。
- 计划：[2026-09-14-tricoder-reliability-top5.md](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/docs/superpowers/plans/2026-09-14-tricoder-reliability-top5.md)。包含共享状态接口、改动文件、分阶段兼容、SQLite 最小状态迁移、实现步骤、验收矩阵、测试命令、排期、回退及交接。
- 关键决定为拟议设计：失败批次保守停止、未知副作用不静默消失、审批单次结束、验证前后与 finish 前核对文件状态。实施顺序 T1→T2→T3→T4→T5。
- 当前状态：只完成计划；没有修改 src/ 或 tests/、运行项目测试、调用模型、安装依赖或提交。已有 test/ 用户改动保持原样。
- 文档静态核验见 runtime/reliability-top5/plan-verification.json；示例仅做语法解析，非新增接口已实现或测试通过的证明。
- 下一步：用户要求实施时，从 T1 基线与真实残留复现开始；执行前重新核对工作区变化，不将现有待复现风险当作已确认缺陷。

## Status

active

## Goal

Evolve the tricoder CLI from a safe, multi-provider local coding agent into a
complete Agent platform while preserving its permission, audit, workspace,
change-journal, and minimal-persistence boundaries. Current milestone: migrate
Hcode capabilities through the approved native-adaptation roadmap.

## Scope

- `src/tricoder/` source, `tests/`, `docs/`, and README inside this project's
  independent Git repository.
- Shared workspace launcher `capabilities/tools/opencode-v2.ps1` and its test
  `capabilities/tools/test_opencode_v2.py`.

## Non-goals

- No OS-level sandbox implementation in the current migration; human approval
  plus policy remain the primary boundary.
- No Hcode Remote/WebSocket server or full free-text conversation persistence.
- No dependency addition, external binary, or source-code reuse before its
  explicit approval and dependency/license gate.

## Constraints

- Follow workspace and project `AGENTS.md`.
- Do not read `.env.local`, `.local/secrets`, `.local/envs`.
- Do not install dependencies or commit/push without explicit approval.

## Acceptance Criteria

- `python -m unittest discover -s tests` passes under the declared Python 3.11+
  runtime; historical test counts remain recorded below and are not current evidence.
- `python -m compileall -q src tests` passes.
- `workspace.py doctor tricoder-cli` reports no findings for this project.
- CommandPolicy rejects qualified executable paths, direct `pytest`/`ruff`/`mypy`
  invocation, external/absolute/`..`/symlink paths in tool commands, and git
  write/pager/textconv/config-override options; git repo-root boundary preserved.
- relaxed 不自动放行任何代码执行命令（仅 git 只读）；子进程环境剔除敏感凭据变量。
- Protocol abstraction keeps native and legacy_json behavior identical
  (covered by `tests/test_protocols.py`).

## Decisions

- 2026-09-07 已批准 verified-stdio 补救：生产本地 stdio 使用 TriCoder 自持
  transport 和 direct process handle，结构化记录进程退出与自持流/任务关闭证据。
  成功不代表任意后台化/脱离进程组后代已消失，不是 OS 沙盒。
- SDK 日志仅在 task-local、来源验证的精确 logger + 词法绝对 pathname 作用域
  过滤；lifecycle、请求和延迟清理 helper 各自持有租约。适配器绑定 `mcp==2.1.1`；
  升级必须重做能力、日志、生命周期及依赖安全评审。remote/auto-install 不支持，
  Windows 离线证据不代表真实外部 MCP/Provider 或跨平台兼容性。

- Hcode migration uses native adaptation: TriCoder remains the architecture and
  security authority; Hcode is a read-only source of designs, algorithms, and
  test scenarios. Approved design and staged execution live in
  docs/superpowers/specs/2026-09-03-hcode-capability-migration-design.md and
  docs/superpowers/plans/2026-09-03-hcode-capability-migration.md.
- The user selected MIT for TriCoder on 2026-09-03. Root `LICENSE`, `NOTICE`,
  and package metadata now record that decision. Later Hcode adapt/copy work
  must update `NOTICE` with concrete source and target files.
- Phase 0 dependency research recommends official `mcp>=2.1.1,<2.2` and
  `PyYAML>=6.0.3,<7` only as `approve-with-conditions` candidates for their
  later phases. That was the Phase 0 decision; Phase 5 subsequently received
  explicit approval and installed `mcp==2.1.1` on 2026-09-04, including its
  manifest/lock changes. PyYAML remains subject to separate Phase 6 approval.
- The migration includes streaming/runtime foundations, context management,
  MCP, Skills, Hooks, Worktree, and child Agents. Remote and OS sandboxing are
  explicitly excluded from this migration.
- `tools.py` split into a `tools/` package (binding, gitignore, undo, handlers,
  filesystem, search, write, command) aggregated by `ToolRegistry`.
- Provider action parsing extracted into `protocols.py` with an
  `ActionProtocol` registry (native + legacy_json); the agent loop no longer
  branches per protocol, and full-round detection delegates to protocol objects.
- CommandPolicy moved from a growing blacklist to per-tool allow-sets plus
  `--opt=value` path checks; executables are resolved to trusted absolute
  paths via `shutil.which` (approval shows the actual program).
- Direct `pytest`/`ruff`/`mypy` invocation is blocked; only `python -m ...` is
  allowed to avoid Windows cwd hijack.
- `opencode-v2.ps1` verifies the reparse-point chain (junction escape) before
  launching; the traversal variable is named `$probe` to avoid a Windows
  PowerShell `$current` assignment quirk.
- Search/glob are bounded: pattern length, `**` count, scan cap, regex length,
  and per-line length limits; `.gitignore` supports common basename/directory
  rules and is documented as a subset, not full Git semantics.
- Phase 3 Context Manager keeps system/current-task boundaries and complete
  tool rounds, anchors estimates to Provider usage when available, and falls
  back to a conservative UTF-8 estimator without adding a tokenizer package.
- Large tool results live under the Session state root, or for one-shot MCP
  runs under the configured audit directory's `runtime/tool-results/`, with opaque
  references, per-entry/session quotas, link/junction rejection, bounded
  preview reads, metadata-only audit, startup cleanup, and `/clear` cleanup.
- Phase 4 Extension Host owns only provider lifecycle, failure isolation and
  aggregation. ToolRegistry remains the sole execution gateway and freezes
  dynamic schemas with explicit origin/risk metadata and current-context binding.
- Project extension configuration cannot authorize access to process secrets:
  `credential_env` names require a second, process-only
  `TRICODER_EXTENSION_ENV_ALLOWLIST` grant. Real extensions remain inactive.

## Progress

- 2026-09-07 verified-stdio remediation：Task 1/2 已通过控制器独立复审；Task 3
  精确日志隔离、文档与最终离线门禁已完成，最终验收仍由控制器独立复审。
  本轮仅修改 SDK/client 日志边界、测试与文档；未改 manifest/lock/.venv，未提交。
  完整证据与遗留风险见
  `runtime/sdd/2026-09-07-tricoder-verified-stdio-remediation/task-3-report.md`。

- Hcode migration Phase 5 implementation and Task 9 review completed on
  2026-09-06; the final whole-feature fix round still awaits controller review
  and fresh complete-suite verification. MCP is an explicitly
  enabled, task-local local-stdio integration. Each Coding Task rebuilds and
  re-approves its servers; every MCP tool is `dangerous`, stays behind the
  asynchronous ToolRegistry gateway, and is removed during deterministic
  cleanup. Remote MCP and automatic server installation remain unsupported.
  Unsupported Schema, malformed output, timeout/cancellation and SDK failures
  fail closed. The repository-local fake server uses only the official API and
  does not access network, environment values, user directories or files.
- Phase 5 changed files: added `src/tricoder/mcp/{__init__,client,manager,
  models,runtime,schema,sdk,security,tool_adapter}.py`,
  `tests/fixtures/fake_mcp_server.py`, and
  `tests/test_mcp_{client,dependency_boundary,integration,manager,runtime,
  schema,security,tool_adapter}.py`; modified `pyproject.toml`,
  `requirements.lock`, `src/tricoder/{config,cli,session_runtime,ui}.py`,
  `src/tricoder/tools/{__init__,command,handlers}.py`,
  `src/tricoder/extensions/host.py`, `tests/test_{config,cli,session_runtime,
  tools,extension_host,ui,context_spill}.py`, `README.md`, `project.md`,
  `docs/framework/mcp-integration.md`,
  `docs/open-source-assessment.md`, and the two MCP/migration plans under
  `docs/superpowers/`. `NOTICE` is unchanged: this phase did not materially
  copy third-party implementation code; the fake fixture only invokes the
  official MCP API.
- The approved direct dependency is `mcp==2.1.1`. `requirements.lock` records
  only the observed Windows / Python 3.11.6 resolution; it is not proof of
  Linux/macOS, Python 3.12, hash-pinned, or universal reproducibility. The
  local stdio transport/pipe retains a residual risk for very large single-line
  messages: bounded result handling cannot prove an end-to-end memory bound
  before line buffering and JSON parsing.
- Historical Phase 5 verification before the final fix round on Windows /
  Python 3.11.6: MCP suite passed 110 tests
  in 27.767s with 1 skip; complete suite passed 762 tests in 235.060s with 5
  skips. The run emitted non-fatal asyncio/Textual slow-callback diagnostics
  in fake-stdio and TUI paths; they were retained in the task report rather
  than suppressed.
- Final-fix focused verification on 2026-09-06: 121 MCP tests (1 existing
  Windows permission skip) and 274 adjacent CLI/Session/tool/cancellation/
  Shell/TUI tests passed; compileall and `git diff --check` exited 0. These
  are scoped results, not a fresh complete-suite claim. The controller owns
  final review and complete-suite verification.
- Doctor output is additionally verified on Windows-compatible CP936 and UTF-8
  streams with an explicit empty offline env file and a process-scoped
  synthetic Key: both exit 0 without a network request or Key disclosure.
- Hcode migration Phase 4 completed on 2026-09-03: added the Extension
  descriptor/provider/host contract, collision-safe lifecycle aggregation,
  cleanup and retry for partially started providers,
  dynamic ToolRegistry registration with immutable schemas and origin-aware
  audit, strict default-off extension configuration, and a secret-safe doctor
  view. No real extension or dependency was enabled. Evidence is in
  `docs/migration/phase-4-verification.md`.
- Hcode migration Phase 3 completed on 2026-09-03: `ContextManager` now owns
  token/character request preparation while the old compaction helpers remain
  compatibility proxies. `ToolResultSpillStore` provides Session-isolated,
  bounded large-result storage and `read_tool_result` supplies path-free chunked
  access. SQLite retains only minimal Session data. Evidence is in
  `docs/migration/phase-3-verification.md`.
- Hcode migration Phase 2 completed on 2026-09-03: OpenAI-compatible
  Providers now expose a bounded SSE stream over the existing urllib transport;
  fragmented tool calls become executable only at a complete response boundary,
  and final usage is delivered before completion. `run_with_context_async()` is
  the normative Agent loop while synchronous callers retain a guarded wrapper.
  Cancellation reaches Provider reads/backoff, tool boundaries, managed command
  process trees, SessionRuntime, one-shot CLI, and TUI. Evidence is in
  `docs/migration/phase-2-verification.md`.
- Hcode migration Phase 1 completed on 2026-09-03: added immutable typed
  Provider/Agent events with secret-safe repr boundaries, a thread-safe
  parent-to-child cancellation token, and an atomic multi-dimensional execution
  budget under `src/tricoder/core/`. The 12 new contract tests were developed
  red-green; no existing Agent behavior or third-party dependency changed.
  Evidence is in `docs/migration/phase-1-verification.md`.
- Phase 0 partially completed on 2026-09-03: created the 17-capability Hcode
  source/target/security/test map, verified Hcode provenance and MIT license,
  and completed current MCP/YAML dependency candidate research. Detailed
  evidence is in `docs/migration/hcode-capability-map.md`,
  `docs/migration/phase-0-verification.md`, and
  `docs/open-source-assessment.md`.
- Phase 0 license gate resolved on 2026-09-03: the user selected MIT; no Hcode
  implementation has yet been copied or substantially adapted.
- Phase 0 environment repaired with explicit user approval: the Python 3.10
  `.venv` was preserved under `runtime/env-backups/venv-py310-20260903/`, a
  Python 3.11.6 `.venv` was created, and only existing declared dependencies
  were installed. Current exact baseline is green: 545 tests pass (4 skips),
  compileall passes, and all 3 smoke Eval cases validate in dry-run.
- Phase 0 self-review completed: after explicit user approval, the one
  pre-existing extra EOF blank line in `test/README.md` was removed and the
  whole-repository `git diff --check` now passes.
- Initial Phase 0 baseline before the authorized environment repair: project
  `.venv` was Python 3.10.16 although
  `pyproject.toml` requires Python 3.11+. Exact unittest ran 452 tests and ended
  with 9 import errors plus 2 skips; all 9 errors and Eval dry-run failure share
  the confirmed missing-`tomllib` environment cause. `compileall` passed. No
  code or environment change was made.
- Completed: read-only Hcode/TriCoder architecture comparison and approved the
  native-adaptation target architecture, end-to-end data flow, security
  invariants, phased gates, rollback rules, and cross-session handoff plan.
- Completed: policy P0/P1 hardening + regression tests; launcher junction
  escape check + test; protocol delegation + `test_protocols.py`; glob/search
  resource bounds + gitignore basename fix + test fixes; README updates.
- Completed: Textual TUI (`src/tricoder/tui.py`, `tricoder tui` entry) with
  modal approval, thread-safe event stream, and pilot tests
  (`tests/test_tui.py`, 3 tests). Dependency review recorded in
  `docs/framework/tui-framework.md`.
- Completed: Planner-Executor (`agent.py` planning round 0 + plan injection +
  degrade; `--no-plan`/`TRICODER_PLAN`/`[agent] plan` config; 5 agent + 4 config
  tests). Design in `docs/framework/planner-executor.md`.
- Completed: command registry (`commands.py` `COMMAND_SPECS`, `/help` generated
  from it in both UIs) and `git_diff` tool demonstrating the tool extension
  point (registered in `tools/__init__.py`, 2 new tests).
- Completed: multi-tool-call rounds (native protocol accepts N calls per round,
  executed sequentially with independent approval/audit) and default
  max_rounds raised 12 → 30. Compaction now groups variable-length tool rounds.
  (2 updated + 2 new tests.)
- Completed: `/permission` command (strict/relaxed levels; relaxed auto-allows
  policy-whitelisted read-only/test commands while file writes stay approved).
  Registered in `commands.py`, enforced via `SessionRuntime._effective_approver`,
  wired into shell + TUI. (3 new tests.)
- Completed: TUI arrow-key selection (`OptionListScreen` modal); `/permission`,
  `/session`, `/model` without args open a selectable list (↑/↓ + Enter/Esc).
  (2 new pilot tests.)
- Completed: TUI collapsible round blocks (each tool round folded into a
  `Collapsible` with a tool-summary title; task/result lines stay visible).
  (1 new pilot test.)
- Completed: `/permission fullaccess` level — auto-allows all non-dangerous
  tools while keeping CommandPolicy/read-only/sensitive-path hard boundaries;
  extensible `_DANGEROUS_TOOLS` set for future delete/rename tools.
  (1 new test.)
- Completed: workspace script execution — `python <relative .py script>`
  allowed by CommandPolicy (relative, no `..`, no absolute, `.py` only);
  auto-execution gated by permission level (fullaccess auto, else approved).
  (2 new tests.)
- Completed: TUI sidebar (double-column layout, live session/state panel,
  thread-safe refresh) + collapsible rounds; fixed sidebar refresh on UI-thread
  command handlers.
- Completed: git boundary protection — git read-only commands are refused when
  the workspace is a subdirectory of a git repo (git would read the repo root
  history/sources outside the workspace). (1 new test.)
- Completed: permission is persisted per session (`SessionMemory.permission_level`
  in SQLite with schema migration); sidebar refresh bug fixed (mis-indented call
  in the permission selector callback). (2 new tests.)
- Completed: P0/P1/P2 security hardening (uncommitted, review before commit):
  - relaxed 不再自动放行任何代码执行命令，仅受限的 git status/diff 元数据查询
    由策略分类后在工具层 auto-approve；show/log/补丁正文仍需审批；fullaccess
    保留自动执行但文档明确非沙盒；子进程环境过滤 API Key/token/
    password/secret/credential 等敏感变量（`_filtered_env`）。
  - CommandPolicy 接受 workspace，所有路径参数经 WorkspacePolicy 真实解析
    （符号链接/junction/存在性/敏感段）；unittest/compileall 建立允许集；
    git 每个只读子命令参数白名单（拒绝 --output/--ext-diff/--textconv/--no-index/
    --git-dir/--work-tree/-C/-c 等）；脚本必须是工作区内存在的普通 .py 文件。
  - TRICODER_BASE_URL 只从进程环境读取，.env.local 不再控制；urlsplit 结构化
    验证（HTTPS、host、拒绝 userinfo/query/fragment）。
  - audit_metadata 区分 `-m <module>` 与 `<script.py>`（execution_kind + 规范化
    相对路径），修复脚本审计 IndexError。
  - 验证状态只由认可的测试/编译/静态检查命令产生（ToolResult.verification_passed）；
    git 只读与普通脚本成功不再标记“通过”，同一修改版本内失败不被任何后续
    成功命令覆盖，新的文件修改会将状态重置为待验证。
  - TUI 动态文本（任务/错误/摘要/diff/session 名/审批详情）一律按纯文本渲染
    （rich.text.Text），固定内部样式才用 markup。
  - SessionRuntime 统一互斥锁：任务启动与 session/model/permission/undo/
    持久化等状态修改原子互斥；审批使用任务启动时的权限快照；完成后只更新
    启动会话。
  - set_permission 采用事务语义，持久化失败时恢复原内存权限；敏感路径覆盖
    .env.*/credentials.*/secrets.*/私钥/服务账号；Provider 响应字节上限
    （超限抛不含正文的 ProviderProtocolError）；finish 非最后时回填“未执行”
    结果保证 tool-result 完整；README 中“沙盒/只读测试/验证通过”描述已对齐。
  - 回归测试：workspace 逃逸 4 条攻击命令、relaxed 不放行代码、env 过滤、
    Base URL 泄露、审计脚本、验证状态绑定、TUI markup 字面、并发锁、
    set_permission 回滚、Git 历史敏感读取、unittest dotted import、compileall
    间接路径清单、Provider 超限、finish 顺序。
- Verified locally: 459 tricoder tests pass (2 Windows symlink skips),
  compileall OK.

## Next Action

- 第二轮模块整理代码与本地回归已完成，等待用户决定是否提交；本任务没有自动提交或推送。
- 如需补齐分发验收，应另行授权准备满足 `setuptools>=68` 的现有隔离构建环境及 `build/wheel`，
  再从解压 wheel 执行冷导入、CLI `--help` 和 Eval 隐藏 helper 冒烟，不能复用开发树。
- 后续若要拆分 `SessionRuntime`，应另立计划并重新固定锁、取消、记忆异常进度和资源所有权；
  不要把它夹带进本轮目录迁移。

## Later Roadmap

- Confirm the full CI matrix (Linux/Windows, Python 3.11/3.12) once pushed.
- Consider stage-two provider registry consolidation (key_env/base_url/model/
  label/choices in one place) to cut the 5-touchpoint provider onboarding.
- TUI roadmap: `/session` cross-workspace switching, command-output paging,
  and coalescing very small streaming chunks into fewer RichLog entries.
- Planner-Executor roadmap: planning with read-only exploration tools;
  per-task plan persistence for `/diff`/`/undo` context.
- Round-budget roadmap: remaining-rounds prompt injection; stagnation
  detection; per-plan round budget.

## Blockers

- 源码迁移和本地测试无阻塞项。
- 分发产物验收受本机缺少 `build`/`wheel` 且 setuptools 版本低于构建要求阻塞；用户未授权安装
  或升级依赖，因此本轮正确停在“源码与模拟/本地回归通过，wheel 未验证”。

## Eval

- 架构：`evals/smoke/` 是只读、版本控制内的评测定义；每次真实运行只在
  `runtime/evals/<run-id>/` 创建隔离工作副本、结构化结果和报告。
- 隐藏 verifier 决策：Agent 返回并完成修改快照后，框架才把 verifier 注入保留目录
  `.tricoder_eval_verifier/`，执行确定性标准库测试后立即清理；fixture 不包含 Key、
  网络访问或真实 Provider 调用。
- 本轮离线验证：smoke suite 加载、三个 case 的 no-op Agent 均未预先通过，以及
  `tricoder eval evals/smoke --dry-run --no-color`。完整命令证据记录在当前 Eval 任务
  报告中。
- 下一步：真实 OpenAI、DeepSeek、GLM Eval 仅由用户显式手动执行；自动测试不运行
  真实 Provider，也不将离线结果表述为 Provider 质量结论。

## Verification

- 2026-09-07 verified-stdio remediation 历史门禁（Task 3 fix 前），Windows / Python 3.11.6 / `mcp==2.1.1`：
  - MCP 全套：`Ran 172 tests in 41.827s`，exit 0，1 项既有权限 skip。
  - 全项目：`Ran 828 tests in 123.703s`，exit 0，5 skips；没有真实 Provider 或外部 MCP。
  - compileall、`git diff --check` 均 exit 0；pycache 仅写入 remediation runtime。
  - CP936/UTF-8 doctor 使用显式合成 env 文件与进程局部 placeholder，均 exit 0，
    输出不含 placeholder；不发送模型请求。smoke Eval 只 dry-run，3/3 validated；
    `workspace.py doctor tricoder-cli` exit 0、0 findings。
  - 原始测试与命令输出保存在同一 remediation runtime；慢回调诊断未隐藏。
    此后 Task 3 fix 修改 sdk.py 与两项日志测试，仅重跑 73 项 focused，不能把
    上述 172/828 当作 fix 后证据；见 remediation runtime 的 Task 3 fix report。

- 2026-09-07 verified-stdio 最终修复新鲜门禁（最后代码/测试修改之后）：
  - I1–I5 的最终修复与 M1 证据更正已实施，等待控制器独立复审；不认领可提交或发布。
  - focused：113 tests / 32.468s，exit 0、无 skips；MCP 全套：202 tests / 48.040s，
    exit 0、1 项既有符号链接权限 skip；完整项目：858 tests / 131.336s，exit 0、5 项
    既有符号链接权限 skips。完整项目另有 6 条 TUI slow-callback 诊断（0.109–0.110s）。
  - compileall（pycache 写入 remediation runtime）、`git diff --check`、CP936/UTF-8
    离线 doctor 均 exit 0；显式使用本轮 `offline-doctor.env` 假值且输出不含 placeholder。
    CP936 捕获显示替代字符仍是展示限制，不作编码或跨平台完备证明。
  - eval dry-run 3/3 validated；workspace doctor exit 0、0 findings。没有联网、真实
    Provider/外部 MCP、依赖变化、暂存或 commits；HEAD 仍为 `72a501f1`、分支 `main`。
  - 证据及边界见 `runtime/sdd/2026-09-07-tricoder-verified-stdio-remediation/final-fix-report.md`；
    `final-fix-focused.log`、`final-fix-mcp.log`、`final-fix-full.log` 为原始测试记录。
    后续改动仅为本证据文档与报告，不改变以上已验证代码或测试。

- 2026-09-03 Phase 4 verification under Python 3.11.6:
  - focused Host/Registry/Config/doctor/Agent/Runtime suite → exit 0,
    `Ran 387 tests in 23.309s`, OK.
  - `.\.venv\Scripts\python.exe -B -m unittest discover -s tests -q` → exit 0,
    `Ran 631 tests in 95.451s`, OK, 4 skipped.
  - compileall → exit 0; smoke Eval dry-run → exit 0, 3/3 validated;
    workspace doctor → exit 0, 0 findings; `git diff --check` → exit 0.
- 2026-09-03 Phase 3 verification under Python 3.11.6:
  - focused Context/spill/Agent/Tool/Runtime/Session/Protocol/Provider suite →
    exit 0, `Ran 309 tests`, OK.
  - `.\.venv\Scripts\python.exe -B -m unittest discover -s tests -q` → exit 0,
    `Ran 606 tests in 82.877s`, OK, 4 skipped.
  - compileall → exit 0; smoke Eval dry-run → exit 0, 3/3 validated;
    workspace doctor → exit 0, 0 findings; `git diff --check` → exit 0.
- 2026-09-03 Phase 2 verification under Python 3.11.6:
  - focused Provider/Agent/Session/subprocess suite → exit 0, `Ran 100 tests`, OK.
  - `.\.venv\Scripts\python.exe -B -m unittest discover -s tests` → exit 0,
    `Ran 581 tests in 252.453s`, OK, 4 skipped.
  - `.\.venv\Scripts\python.exe -B -m compileall -q src tests` → exit 0.
  - smoke Eval dry-run → exit 0, 3/3 cases validated.
  - workspace doctor → exit 0, 0 findings; `git diff --check` → exit 0.
- 2026-09-03 Phase 1 verification under Python 3.11.6:
  - focused red run exited 1 with the expected three missing `tricoder.core`
    import errors; the green run passed all 12 new contract tests.
  - `.\.venv\Scripts\python.exe -B -m unittest discover -s tests` → exit 0,
    `Ran 557 tests in 76.192s`, OK, 4 skipped.
  - `.\.venv\Scripts\python.exe -B -m compileall -q src tests` → exit 0.
  - `.\.venv\Scripts\python.exe -B -m tricoder eval evals\smoke --dry-run --no-color`
    → exit 0, 3/3 cases validated.
  - isolated core import boundary check → exit 0; no Hcode, TUI, Session,
    Tools, Provider implementation or Provider SDK import.
- 2026-09-03 refreshed Python 3.11.6 baseline after the authorized environment
  rebuild:
  - `.\.venv\Scripts\python.exe -B -m unittest discover -s tests` → exit 0,
    `Ran 545 tests in 72.607s`, OK, 4 skipped.
  - `.\.venv\Scripts\python.exe -B -m compileall -q src tests` → exit 0.
  - `.\.venv\Scripts\python.exe -B -m tricoder eval evals\smoke --dry-run --no-color`
    → exit 0, 3/3 cases validated.
- 2026-09-03 initial failing evidence retained for diagnosis history:
  - `.\.venv\Scripts\python.exe -B -m unittest discover -s tests` → exit 1,
    `Ran 452 tests`, 9 import errors, 2 skips; confirmed cause is Python 3.10
    lacking `tomllib` while the project requires 3.11+.
  - `.\.venv\Scripts\python.exe -B -m compileall -q src tests` → exit 0.
  - `.\.venv\Scripts\python.exe -B -m tricoder eval evals\smoke --dry-run --no-color`
    → exit 1 at the same missing-`tomllib` import boundary.
- `.venv\Scripts\python -m unittest discover -s tests` → Ran 459, OK (2 skips:
  Windows cannot create symlinks).
- `.venv\Scripts\python -m compileall -q src tests` → OK.
- `python -B capabilities/tools/test_opencode_v2.py` → 3 OK (includes junction
  escape rejection).
- `python -B capabilities/tools/workspace.py doctor tricoder-cli` → no
  tricoder-cli findings after this update.
- `test_workspace_tools` / `check_workspace` fail only on
  `seven-sins-roguelite-codex-starter` (pre-existing, out of scope).
- Not yet verified: Linux/macOS posix binding paths and symlink tests, Python
  3.12, real provider smoke tests (require API keys).
