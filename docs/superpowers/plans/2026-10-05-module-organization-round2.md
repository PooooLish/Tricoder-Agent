# TriCoder 第二轮目录整理与导入迁移 Implementation Plan

> 执行会话：使用 superpowers:executing-plans 按 S0—S5 小步实施。用户选择将本文交给另一个 coding session；当前会话只写计划。无需重新选择执行方式，不自动提交、不安装依赖。

**Goal：** 把散落在包根目录的进程、工作区、会话和交互代码归入对应目录，让开发者能按功能找代码，同时保持现有行为和常用导入入口。
**Architecture：** 第一轮解决 Agent 内部职责拆分；第二轮只解决模块归属和依赖方向。实现迁入四个子包，原路径保留薄兼容模块；内部统一使用新路径，类型和运行时状态保持唯一。
**Tech Stack：** Python 3.11+、现有 setuptools、unittest、asyncio、SQLite、Rich/Textual；无新增依赖。
**Spec：** 本文第 1—4 节为本轮范围与设计；前置计划为 [第一轮职责拆分](2026-10-05-agent-refactor-round1.md) 和 [第一轮审查补齐](2026-10-05-agent-refactor-round1-review-fixes.md)。
**状态：** 第二轮待实施，前置复审已通过。2026-10-05 本次新跑全量 1337 tests，OK，11 skipped（301.275s）；代码审查未发现阻塞问题。不要再按旧版“第一轮未实施”判断，也不要重做第一轮。

项目根：D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli。下文代码路径均相对此根。

## 0. 本版依据与复审结论

- F1 已关闭：MemoryCoordinator 无 engine 依赖；快照输入、窄结果及每调用独立进度已实现，Runner 的两处 finally 只合并记忆允许字段。
- F2 已关闭：两实例的确定性交错、候选/用量隔离与显式/原生取消测试已纳入仓库；流异常探针也已固定。此结论不包括同一个 Agent 实例并发。
- 新鲜验证命令：`.\.venv\Scripts\python.exe -B -m unittest discover -s tests -q`，退出码 0，1337 项、11 跳过；保留既有 asyncio/Textual 慢回调诊断，没有据此修改代码。
- 独立复核用 52 个纯内存场景对照补齐前的有限源码基线与当前实现，状态、审计事件及异常身份一致；覆盖部分提交、取消、BaseException 与观察/审计回调失败。
- 本次源码/测试校验标识：165 个 Python 文件，SHA-256 为 `71cecf77dc2ca2517837eaf09bd5d2217bf49f1c0df9d1c018cb2bde6a3ca7b4`。算法：取 src/ 与 tests/ 下所有 .py，按相对路径 as_posix() 字典序排序，依次向 SHA-256 输入 UTF-8 路径、NUL、文件原始字节、NUL。此标识仅证明文件内容相同，不能替代运行环境/依赖核对。
- 本轮审查和文档更新没有修改功能代码或执行第二轮迁移；真实模型、Linux/macOS 与 Python 3.12 未验证。

## 1. 本轮解决什么、不解决什么

第一轮让 agent.py 从“大部分事情自己做”变成“调度几个职责明确的组件”。第二轮让 src/tricoder 从“相关文件散落在同一层”变成“进程相关放一起，会话相关放一起”。这两个改动分开验收，出问题时才容易判断原因。

本轮移动 14 个实现模块，不重写其中的业务算法。不以减少总文件数或根目录文件数为验收指标：兼容模块暂时保留，打开旧文件会明确指向新实现。

必须保持：

- CodingAgent、CLI 入口、命令参数、斜杠命令、配置默认值、Provider 请求及工具协议不变。
- 会话延迟创建、会话锁与工作区锁的获取/释放顺序不变；锁名、锁文件位置、控制目录、数据库位置不变。
- 工作区门禁仍为：无变化继续；有变化展示并确认，确认后复扫；扫描失败或不能确认则阻断。
- SQLite schema、持久化字段、记忆确认流程、审计与 Eval 报告格式不变。
- 原生取消、清理预算、未确认资源归属、UNKNOWN 副作用、验证证据及撤销绑定语义不变。
- 保留本次补齐：coordinator 不依赖任何 engine 模块；MemoryStepInput/Result/Progress 为现有记忆边界，Runner 在 finally 中只合并一次允许字段；异常前的计数、已提交压缩与未审计候选的隔离不变。
- 保留 test_agent_memory_boundary、test_agent_instance_isolation、test_agent_provider_request 的断言；本轮不能因改路径而削弱消息/候选/用量/取消隔离或恢复传递整个 AgentRunState。

明确排除：拆分 SessionRuntime 内部状态机、继续改变 Agent 算法、移动全部根模块、移动测试目录、新增 Provider/MCP 能力、增加重试/沙箱、引入 LangGraph 或通用依赖注入框架。

## 2. 新目录和逐文件迁移表

每个新包都有轻量 __init__.py，只写包说明，不集中导入所有子模块。以下是目标，不是当前已存在的结构。

```text
src/tricoder/
  agent.py                    第一轮留下的 Agent 门面
  cli.py / __main__.py         保留启动入口
  engine/                     第一轮产物，本轮只适配 import
  context/                    现有上下文模块，归属不变
  process/
    control.py                子进程运行、输出限制、取消和进程树清理
    env.py                    受控环境变量与可信可执行文件解析
  workspace/
    lock.py                   工作区互斥锁与恢复保护
    verification.py           验证证据和用于验证的工作区快照
    snapshot.py               任务前后的内容清单、差异与变更账本比对
    gate.py                   差异确认与执行门禁
  session/
    lock.py                   Session 独占锁
    store.py                  SQLite 会话存储
    runtime.py                Session 生命周期和任务调度；本轮不拆内部逻辑
  presentation/
    commands.py               本地斜杠命令解析和规格
    approval_wait.py           审批等待与取消协调
    console.py                终端文本展示
    shell.py                  交互式 Shell
    tui.py                    Textual 界面
  core/ tools/ mcp/ extensions/ evals/   现有归属不变
  旧路径兼容模块               仅转发已有接口，不重复实现
```

| 当前文件：src/tricoder/ 下 | 唯一实现的新位置 | 旧位置本轮处理 |
| --- | --- | --- |
| subprocess_control.py | process/control.py | 显式兼容导出 |
| subprocess_env.py | process/env.py | 显式兼容导出 |
| workspace_lock.py | workspace/lock.py | 显式兼容导出 |
| verification.py | workspace/verification.py | 显式兼容导出 |
| workspace_snapshot.py | workspace/snapshot.py | 显式兼容导出 |
| workspace_gate.py | workspace/gate.py | 显式兼容导出 |
| session_lock.py | session/lock.py | 显式兼容导出 |
| sessions.py | session/store.py | 显式兼容导出 |
| session_runtime.py | session/runtime.py | 显式兼容导出 |
| commands.py | presentation/commands.py | 显式兼容导出 |
| approval_wait.py | presentation/approval_wait.py | 显式兼容导出 |
| ui.py | presentation/console.py | 显式兼容导出 |
| shell.py | presentation/shell.py | 显式兼容导出 |
| tui.py | presentation/tui.py | 显式兼容导出 |

暂留根目录：config.py、models.py、policy.py、audit.py、changes.py、patches.py、protocols.py、providers.py，以及 execution_state.py、task_cleanup.py、task_observation.py。
后三者承载跨模块状态、ContextVar 或可信副作用转换，不能为了目录整齐复制定义或塞进一个会造成反向依赖的包。providers.py 的拆分留给新增非 OpenAI-compatible Provider 时单独设计。

### 2.1 依赖方向

表中的箭头表示“调用/导入”，不是完整运行时流程：

| 使用方 | 可以依赖 | 本轮禁止新引入的反向依赖 |
| --- | --- | --- |
| cli、presentation | session、Agent 门面、workspace、共享类型 | process/workspace 导入界面；session 导入具体 Shell/TUI |
| session | Agent 门面、context、store/lock、workspace、现有基础组件 | 通过 presentation 取得运行时能力 |
| engine、tools | 现有基础组件、workspace、process | 为了装配调用 session/runtime |
| workspace | policy、changes、core、同包底层模块 | 导入 Agent、SessionRuntime 或 UI |
| process | core/cancellation、task_cleanup、标准库 | 导入 SessionRuntime、CLI 或 TUI |

现有 workspace_snapshot.py 会使用 verification.py 的 _bound_directory、_is_reparse、_metadata、_open_binary。迁到同包后保持这组依赖，不在本轮另造一套文件安全辅助函数。workspace/__init__.py 不得提前加载 gate，以免触发 snapshot → verification → policy 的循环导入。

## 3. 兼容规则：旧路径能用，内部只认新路径

1. S0 先列出旧模块中被 src、tests、入口、文档示例实际使用的符号；以此确定兼容导出清单，包含必要的异常类型、常量和已使用辅助函数。
2. 原模块只显式导入并导出这些符号，说明新位置；不使用 import *，不通过 sys.modules 偷换整个模块对象，不保留两份业务实现。
3. 普通类、函数、异常应满足 old.Symbol is new.Symbol。只保留一个 ContextVar、锁注册表、缓存和可变状态定义，禁止在兼容文件重新构造。
4. 内部实现和常规行为测试全部使用新路径；单独的兼容测试刻意走旧路径。业务代码不得经旧模块绕回新实现。未迁移模块如 tricoder.agent 仍是有效入口。
5. 不宣称“旧模块的所有 monkeypatch 自动兼容”。函数在新模块定义后，会在新模块查找全局变量；patch 旧模块通常不会影响它。内部测试应迁移到真实查找位置，并断言 hook 被调用。若发现项目明确承诺某个外部 patch 扩展点，先保留该边界并记录例外，不能悄悄破坏。
6. 类的 __module__ 会随迁移变化。检查是否有 pickle、动态 import 字符串、日志/序列化格式或持久化数据依赖该路径；不要仅为表面相同批量改 __module__。已知 SQLite 数据格式应保持原样，用合成旧库测试。

当前已发现的测试迁移样例：

| 旧 patch 位置 | 新的实际查找位置 |
| --- | --- |
| tricoder.subprocess_control._terminate_process_tree | tricoder.process.control._terminate_process_tree |
| tricoder.session_runtime.CancellationToken | tricoder.session.runtime.CancellationToken |
| tricoder.sessions._is_windows | tricoder.session.store._is_windows |
| tricoder.tui.ApprovalWait | tricoder.presentation.tui.ApprovalWait |

还要检索 patch.object、字符串 import、__file__、Path.home patch 与 TYPE_CHECKING；不能只全局替换 from/import 文本。若测试只因 mock 不再生效而“通过”，必须修正。

## 4. 重点审查点

- **独立解释器首次导入：** 已加载模块可能掩盖循环依赖；S1—S5 用新进程验证新旧入口及不同导入顺序。
- **隐藏验证器离开开发环境：** evals/workspace.py 会复制源码，搬文件后可能复制到兼容壳；S1 直接执行隔离后的 helper。
- **锁与清理状态分叉：** 新旧类或 ContextVar 重复定义会造成“各自都认为自己持有锁”；S2/S3 检查对象同一性和真实子进程竞争。
- **mock 失效但测试假绿：** S1/S3/S4 验证故障注入实际到达生产调用点，不用降低断言掩盖问题。
- **开发目录能跑、分发包不能跑：** S5 检查新子包发现、分发产物导入和代码资源复制；未具备构建工具则明确报告未验证。

特别注意 evals/workspace.py 的 _install_bounded_process_runtime：当前先取 subprocess_control.__file__ 的父目录，再拼接 subprocess_control.py、task_cleanup.py、core/cancellation.py。搬到 process/control.py 后，这个“几个文件同目录”的假设不再成立。

本轮推荐直接定位三个真实模块的 __file__，列出固定源文件到目标文件的映射：process.control → _tricoder_bounded_process.py；task_cleanup → tricoder/task_cleanup.py；core.cancellation → tricoder/core/cancellation.py。保留现有隐藏目录布局、空包标记和 _copy_framework_file 的路径/链接检查。control.py 继续使用现有绝对基础模块导入，使最小依赖闭包能独立运行。不要复制兼容壳、整个项目、用户工作区代码或依赖开发机 PYTHONPATH。

## 5. 按阶段执行

每阶段按“补必要迁移回归 → 确認新目标相关断言在迁移前失败 → 迁移最小内容 → 聚焦验证 → 自审并记账”推进。既有行为测试复用，不为每条 import 写重复测试。下列新增测试文件是建议固定落点，测试方法名可按现有风格微调，断言含义不能削弱。

### S0：前置验收与当前工作树基线

**文件：** 只读第一轮计划、project.md、README.md、pyproject.toml、src/tricoder、tests；输出 runtime/module-organization-round2/baseline.md 和 import-inventory.md。

- [ ] 核实当前分支包含第一轮 R0—R6 和补齐 C0—C4：Agent 门面、engine/、记忆快照/结果/异常进度及隔离测试实际存在，交接说明与当前源码一致。前置已在本次复审中检查，实施前的核对是防止换分支或并行修改造成状态变化，不是重新实施。
- [ ] 如实际工作树缺少这些前置或出现新的相关失败，记录具体差异并停止源码迁移；不要自行混做两轮或自动修复范围外问题。
- [ ] 记录当前 Git 状态及待迁移模块的内容摘要/哈希，不以 HEAD 作为唯一基线。保留已有未提交和未跟踪代码；受控备份如有需要，仅包含本任务相关源码/测试/文档，置于 runtime/，不备份密钥或会话数据。
- [ ] 建立 14 对路径、实际导出符号、反向引用、patch 点、动态路径与冷导入清单；核对第一轮新增 engine/context 内的引用。
- [ ] 取得一次当前完整 unittest 基线并记录失败/跳过原因。可复用本次复审的通过记录，但必须确认源码/测试内容、Python 环境及依赖与记录一致，并在 baseline.md 写明复用依据；无法确认则重新运行。与本轮关键行为相关或无法归因的失败未解决前，不进入下一阶段。

**接口/交付：** 后续阶段使用本阶段的符号清单与基线；不新增业务 API。

### S1：迁移 process，并修复隐藏验证器的源码定位

**创建：** src/tricoder/process/{__init__,control,env}.py；tests/test_module_compatibility.py、tests/test_module_import_boundaries.py。
**修改：** 原 subprocess_control.py、subprocess_env.py；policy.py、tools/command.py、mcp/security.py、evals/runner.py、evals/workspace.py 及清单内其他调用点；相关测试。
**接口：** run_bounded_process、BoundedProcessResult、ProcessExecutionUncertain、filtered_subprocess_env 和可信可执行文件解析函数沿用当前签名/默认值。

- [ ] 为新旧 process 导入的对象同一性增加回归；先确认新包不存在导致这些断言失败，不能将其他异常当作期望失败。
- [ ] 迁移两份实现、保留显式旧导出、更新内部调用路径；不改变进程启动参数、输出上限、时间预算或环境过滤逻辑。
- [ ] 按第 4 节的固定映射修改隐藏 helper 来源；原 _copy_framework_file 的拒绝链接、复核普通文件和目标边界检查全部保留。
- [ ] 扩展 tests/test_eval_workspace.py 现有 test_installed_process_helper_has_local_runtime_dependency_closure：清掉子进程 PYTHONPATH，使用 -S -B，放置恶意同名工作区包；除能 import 外，还真正调用 helper 运行合成成功命令和受限超时命令，验证输出与停止状态。子进程设置外层 timeout，避免测试自身挂住。
- [ ] 执行 subprocess_control/env、cancellation、eval_workspace/runner、mcp_dependency_boundary 相关测试。检查故障注入的清理函数确被调用、清理失败仍保留资源归属。

**阶段完成：** 独立 helper 不依赖当前源码目录或已安装 tricoder；旧入口和新入口共享同一实现。

### S2：迁移 workspace，保持两类快照语义

**创建：** src/tricoder/workspace/{__init__,lock,verification,snapshot,gate}.py。
**修改：** 四个旧模块；models.py 的类型引用、task_observation.py、tools、cli、session_runtime、ui、evals 及第一轮 engine 的实际引用；tests/test_module_compatibility.py、tests/test_module_import_boundaries.py。
**接口：** WorkspaceLock、VerificationScope、WorkspaceGate、capture_workspace_baseline、compare_baselines 等沿用当前签名；Verification 用的 WorkspaceSnapshot 与门禁用的 WorkspaceBaseline 不合并。

- [ ] 增加新旧 WorkspaceLock/异常、VerificationScope、WorkspaceBaseline/Gate 类型同一性回归；迁移前验证新入口失败。
- [ ] 按 lock → verification → snapshot → gate 顺序迁入实现，统一同包引用和外部引用；保留四个薄兼容模块。不要为了共享辅助函数改写扫描策略。
- [ ] 用独立进程覆盖“先导入 models/policy 后 workspace”和“先 workspace 后 tools/agent/session”两种顺序，确认没有部分初始化模块错误。
- [ ] 运行 workspace_lock/snapshot/gate/consistency、verification_evidence、effect_state 相关测试，保留真实多进程锁竞争测试；按当前平台如实记录跳过。
- [ ] 验证无变化、确认后变化、拒绝、扫描失败、取消五类任务入口；确认/扫描失败时 Provider 和业务工具调用数为 0；验证证据失效与撤销预览绑定不变。

**阶段完成：** 同根工作区的会话仍互斥，异根不被错误串行；所有扫描失败仍阻断，锁路径和恢复标记不变。

### S3：迁移 session，保留运行时内部结构

**创建：** src/tricoder/session/{__init__,lock,store,runtime}.py。
**修改：** 三个旧模块；cli、shell、tui、evals/service.py、evals/scenarios.py 及清单内其他调用点；会话与兼容测试。
**接口：** SessionRuntime、RuntimeOptions、ActiveSession、SessionStore、SessionLock、相关错误及 default_sessions_db 沿用当前签名和默认路径。

- [ ] 增加新旧会话类与异常同一性测试；迁移前确认新入口失败。
- [ ] 按 lock/store → runtime 顺序迁入；session/__init__.py 不自动导入 runtime，单独 import store 不能因此加载 Agent/界面。
- [ ] 更新 SessionRuntime 内部和外部导入；只允许迁移必需的路径改动与说明，不拆方法、不改变 await/try/finally 范围或锁的作用域。
- [ ] 将 CancellationToken、_is_windows 等测试 patch 到新定义查找位置；至少保留取消、持久化失败的故障注入，并确认注入已命中。
- [ ] 运行 session_ownership/landing/runtime/integration/sessions、memory_*，以及 test_agent_memory_boundary/test_agent_instance_isolation/test_agent_provider_request。用合成旧 schema 库验证恢复、切换、保存、clear，不接触真实会话库。
- [ ] 检查空闲入口不创建会话；首次任务只创建一次；切换会话重新加载；会话内切换模型仍保留同一会话上下文；资源未确认清理时不能错误释放所有权。

**阶段完成：** SessionRuntime 虽然换位置，生命周期与存储内容没有变化。其文件仍可能较长，这是明确留给后续单独设计的工作。

### S4：迁移 presentation，保留 CLI 启动方式

**创建：** src/tricoder/presentation/{__init__,commands,approval_wait,console,shell,tui}.py。
**修改：** 五个旧模块；cli.py 及清单内其他调用点；UI、Shell、TUI、CLI 和兼容测试。
**接口：** TerminalUI、InteractiveShell、TricoderApp、ApprovalWait、parse_command、CommandError 等保留原签名；UI 继续调用 SessionRuntime 公开行为，不新建运行时。

- [ ] 增加常用新旧 UI 类和命令解析函数同一性回归；迁移前确认新入口失败。
- [ ] 依 commands/approval_wait/console → shell/tui 顺序迁移；核查 Textual 资源路径、类声明、异步回调和测试 patch。__init__.py 不自动加载 TUI。
- [ ] 保留 cli.py、__main__.py、pyproject.toml 中 tricoder = tricoder.cli:main；保留 CLI 原本在需要时才导入 TUI 的边界。
- [ ] 运行 cli/shell/ui/tui/reliability_integration 和门禁交互测试；ApprovalWait 故障/取消注入必须命中实际界面调用。
- [ ] 用现有 headless 测试验证审批、差异分页、取消、会话选择和模型选择；CLI --help 冒烟不调用 Provider。没做人工 TUI 交互时明确标注，不能把导入成功当成交互已验证。

**阶段完成：** 用户启动、输入和审批方式不变，界面层没有新业务状态机。

### S5：收口导入边界、分发验证与文档交接

**修改：** tests/test_module_compatibility.py、tests/test_module_import_boundaries.py、tests/test_agent_import_boundaries.py、tests/test_mcp_dependency_boundary.py、README.md、project.md；创建 docs/framework/module-layout.md。

- [ ] 兼容测试逐项覆盖 S0 符号清单；常规测试改用新路径后，仍保留经过旧入口的一条代表性行为路径，不能只测 import 成功。
- [ ] 用 AST 检查 14 个旧模块引用：仅允许兼容模块、兼容测试及已记录的特例使用旧路径；同时搜索动态字符串导入。新包不得反向依赖旧壳。
- [ ] 为每个新包子模块、旧入口与 MCP/tools 顺序建立冷导入用例，在全新解释器执行；用 AST 加实际导入测试验证轻量 __init__.py 和依赖方向，不编写仅靠总文件数断言的测试。
- [ ] 同步现有 Agent 导入守卫：旧 tricoder.session_runtime、tricoder.tui 等禁用路径仍保留，新 tricoder.session.runtime 与 tricoder.presentation 也必须受相同边界保护。保留 coordinator 禁止导入 engine 的绝对/相对/动态导入样例；冷导入既测兼容入口，也测新的规范入口，不能只换业务 import 而让守卫停留在旧名字。
- [ ] 检查 setuptools 包发现包含四个新包。已有构建工具可用时，在项目 runtime/ 下构建 wheel（禁止依赖下载和构建隔离自动安装），从解压产物运行冷导入/CLI --help/隐藏 helper 冒烟，核对模块 __file__ 指向产物而非开发树；子进程排除源码路径和 editable install 的干扰，仅使用显式受控路径。工具缺失则停止该项并报告，不能安装依赖或声称分发验证通过。
- [ ] 跑一次最终完整 unittest、语法检查和 git diff --check；比较 S0 失败/跳过清单，说明新增差异。若最终又改了代码，补跑受影响测试；不无依据重复全量测试。
- [ ] README 增加目录入口与阅读顺序；module-layout.md 保存迁移表、依赖方向、兼容策略和后续事项。project.md 记录阶段状态、实际命令、通过/失败/跳过、未验证平台、下一步与阻塞。历史计划不批量改成新路径，它们仍是历史记录。

**阶段完成：** 新结构可从文档定位，包外部入口兼容，有测试证据；构建/平台限制单独列出，不用“全部完成”掩盖验收缺口。

## 6. 验证命令与执行证据

以下命令在项目根运行，使用已有 .venv。不要安装依赖。聚焦测试的 pattern 按阶段选择；如现有测试需要 PYTHONPATH，仅对子进程显式指定本项目 src，避免修改用户全局环境。

```powershell
# S0 和 S5 各一次全量；保存实际输出、退出码和跳过原因
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -q

# 聚焦命令示例：按阶段替换 pattern，每项失败即先处理
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p 'test_module_*.py' -v
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p 'test_eval_workspace.py' -v
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p 'test_workspace_*.py' -q
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p 'test_session_*.py' -q
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p 'test_memory_*.py' -q
.\.venv\Scripts\python.exe -B -m unittest tests.test_agent_memory_boundary tests.test_agent_instance_isolation tests.test_agent_provider_request tests.test_agent_import_boundaries -q

# 语法检查缓存放 runtime；不污染源码目录
.\.venv\Scripts\python.exe -X pycache_prefix=runtime/module-organization-round2/pycache -m compileall -q src tests
.\.venv\Scripts\python.exe -B -m tricoder --help
git diff --check
```

通配 test_session_*.py 不含 test_sessions.py；test_subprocess_*.py 不含 test_cancellation.py。各阶段列出的专项须逐项执行或由覆盖它们的更大集合执行，不能因为示例没列就跳过。

证据置于 runtime/module-organization-round2/：baseline.md、import-inventory.md、verification.md；长日志可以独立保存，project.md 保存不依赖临时日志的结论。不保存真实环境变量、密钥、用户消息或真实数据库内容。

## 7. 回滚和完成标准

每阶段迁移一组文件并验证，跨组失败先停在当前阶段。回退仅撤销本轮确认属于自己的 hunk 和新增文件；以 S0 工作树为参照，不执行 git reset --hard、git clean 或覆盖式 checkout，不覆盖其他会话后续写入。发现同文件有其他会话活动修改时，先协调写入范围，不能凭哈希不一致直接覆盖。

全部满足才可宣布实施完成：

- [ ] 第一轮验收通过，S0—S5 均有记录；未解决前置和关键回归失败为零。
- [ ] 14 个模块的唯一实现位于新目录；旧文件只转发，类型/状态无副本，内部不绕经旧路径。
- [ ] 独立 helper、mock 注入、跨进程锁与门禁、会话/记忆、CLI/UI、冷导入回归通过。
- [ ] 第一轮补齐的记忆异常进度、单次合并、两实例隔离和流异常测试仍通过；导入守卫覆盖迁移后的规范路径。
- [ ] 分发产物验证通过；若因工具/环境不足未完成，明确标为部分验收，保留待办。
- [ ] 完整测试没有未解释的新失败；平台跳过按真实情况记录，不能宣称 Windows 结果同时证明 POSIX。
- [ ] 目录说明、兼容导出清单、后续工作和限制已写入项目文档；没有功能扩展、依赖安装或自动提交。

后续建议另立任务：拆 SessionRuntime 的生命周期、记忆命令与任务执行协调。不要把这项塞进本轮目录搬迁。

## 8. 可直接交给 coding session 的提示词

```text
请在 D:\MaHong\AGENT_WORKSPACE_V2\projects\tricoder-cli 实施第二轮代码整理。

先读 ../../AGENTS.md、AGENTS.md、README.md、pyproject.toml、project.md，以及：
docs/superpowers/plans/2026-10-05-agent-refactor-round1.md
docs/superpowers/plans/2026-10-05-agent-refactor-round1-review-fixes.md
docs/superpowers/plans/2026-10-05-module-organization-round2.md

按第二轮文档的 S0—S5 小步执行。本次授权修改计划范围内的源码、测试和项目文档；不是只给建议。每阶段聚焦验证和自审通过后继续，无需逐阶段再次询问。

第一轮和审查补齐已实施并有复审记录。先核对当前工作树仍包含这些产物，不要重做第一轮。若发现实际分支缺少前置或有新失败，记录差异并暂停迁移；正常情况下直接进入 S0 基线和迁移清单。可按文档规定核对后复用本次全量复审记录，否则重新建立基线。

目标是把 process、workspace、session、presentation 相关模块归类，保留旧导入兼容，内部统一新路径。不得改业务行为、继续拆 SessionRuntime、扩展 Provider、改变审批/记忆/锁/快照门禁，也不要移动全部根模块。

重点检查：Eval 隐藏 helper 的真实源码定位和最小依赖闭包；旧路径与新路径的类型及状态同一性；测试 patch 必须命中实际调用；冷导入循环依赖；分发包中子包和代码资源是否齐全。不能用删测试、弱化断言或跳过关键场景让结果通过。

保留第一轮补齐的记忆快照/结果边界、异常进度及 finally 单次合并；保留两实例隔离与流异常测试。迁移后更新导入守卫的新旧路径，不能让它只检查旧模块名。

保留当前未提交和未跟踪修改。不读取 .env.local、凭据目录或真实会话库，不调用真实模型，不安装依赖，不自动提交/推送，不执行 reset --hard/clean/覆盖式 checkout。测试只使用合成数据和 fake Provider；生成证据放 runtime/module-organization-round2/。

实施后更新 README.md、docs/framework/module-layout.md、project.md，记录阶段进展、实际验证、跳过、阻塞及下一步。最后汇报迁移文件、兼容策略、测试结果、剩余风险和未验证项；若前置或验收未满足，明确说明，不能称为完成。
```
