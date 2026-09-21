# TriCoder 独立副本迁移到 LangGraph：工程设计与实施计划

> 执行者：先阅读本文件及项目规则，使用 `superpowers:executing-plans` 按阶段实施。所有复选框初始均未完成。本文件是计划，不是迁移已经成功的证明。

**目标：** 保留原 TriCoder 的可用代码和运行环境，在独立副本中以 LangGraph 替换手写 Agent 调度，保留并补齐权限、撤销、取消、上下文和验证边界。

**架构：** LangGraph 管理执行顺序；TriCoder 管理工具实际执行和安全判定。先完成同进程、顺序执行的迁移，再单独评估持久化恢复。

**技术栈：** Python 3.11+、现有 Provider/ToolRegistry、LangGraph Python 库、unittest、现有 CLI/Textual TUI。

**设计依据：** 本文第 1—7 节为设计约束，第 8—11 节为实施与验收；另参考同目录 `2026-09-14-tricoder-reliability-top5.md`。该前五项计划尚未实施，不能把其中的新接口视为已有代码。

**状态与日期：** 2026-09-14，仅完成文档。未复制仓库、未安装 LangGraph、未改变生产代码、未运行迁移测试。本文不授权安装依赖、初始化 Git、提交或发布；实施会话应根据用户当时的明确授权行动。

## 1. 先看结论和范围

采用 **integrate（接入依赖）**：在独立副本中使用 LangGraph 编排库，不复制上游框架源码，不把 TriCoder 重写成 Dify 应用。

本次交付必须包含：独立代码副本、独立环境和数据目录、可选择的新旧引擎、保持现有接口的 LangGraph 实现、安全回归测试、真实的验收记录和回退演练。

第一版明确不包含：跨进程自动续跑、磁盘 checkpoint、并行写工具、多 Agent、远程工具服务、Dify 平台化、OS 沙箱、自动合并回原项目。没有这些能力不妨碍本次迁移完整交付，但必须如实说明。

“安全”的含义是：不破坏原项目；不绕过原有执行约束；执行结果不确定时停止；失败可退回原版。它不意味着恶意代码绝对无法访问宿主机，也不意味着外部命令可以全部撤销。

## 2. 原项目与迁移副本如何隔离

### 2.1 固定目录约定

| 用途 | 绝对路径 | 要求 |
| --- | --- | --- |
| 原项目 SOURCE | `D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli` | 迁移实施期间只读；本次文档和交接说明是此前的文档写入 |
| 新项目 TARGET | `D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-langgraph` | 必须是独立普通目录；已存在则停止检查，不覆盖 |
| 新虚拟环境 | `D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-langgraph/.venv` | 重新创建，不复制原 `.venv` |
| 迁移证据 | `D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-langgraph/runtime/migration` | 基线、复制清单、测试报告 |
| 新版会话、审计和暂存 | `D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-langgraph/runtime/app-data` | 不读取原会话，不复用原数据库 |
| 无私有数据的测试工作区 | `D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-langgraph/runtime/fixtures` | 只能放合成的小项目，每次测试创建独立子目录 |

新会话数据库必须在对应 Agent 工作区之外。现有 `sessions.py` 已检查这一点。因此日常迁移验证用 `runtime/fixtures/<case>` 作 Agent 工作区，数据放 `runtime/app-data`；不要把整个 TARGET 同时当作受操作工作区和数据库父目录。若将来让 Agent 编辑 TARGET 源码，应另选项目外的数据目录并重新检查边界。

### 2.2 复制当前工作树，不只复制 HEAD

当前原仓库存在用户修改及未跟踪文件。2026-09-14 观察到 `test/README.md`、`test/smoke_demo.py` 有修改，`test/` 下还有新文件；执行时必须重新核对。只用 `git archive HEAD` 会漏掉这些内容；直接复制整个文件夹会带走密钥、环境和运行数据。

采取“审核清单后复制普通文件”的方式：

1. 暂停其他会话对 SOURCE 的写入；记录 HEAD、文件状态和复制候选清单，不导出可能含私有内容的完整 diff。
2. 从 `src/`、`tests/`、`test/`、`evals/`、`docs/`、`.github/` 及根目录工程文件构建候选清单。根文件包括 `AGENTS.md`、`project.md`、`README.md`、`pyproject.toml`、`requirements.lock`、`run_tests.ps1`、`LICENSE`、`NOTICE`、`.gitignore`。
3. 目录名只是候选范围，不代表整个目录都可复制。逐个排除私有文档、真实数据、凭据、输出报告、日志、SQLite、二进制产物、缓存及未知用途文件；不通过读取明确禁读的密钥文件来“检查是否有密钥”。`.env.example` 仅在确认是无凭据模板后纳入。
4. 绝不复制 `.git/`、`.venv/`、`.env.local`、其他真实环境文件、`.local/`、`.worktrees/`、`.superpowers/`、`runtime/`、缓存和构建产物。不能依赖 `.gitignore` 自动保证安全。
5. 使用不跟随链接的遍历；遇到符号链接、junction、其他 reparse point 或多硬链接文件，停止该项并审查。不要穿过链接读取目标；不要用链接作为副本。
6. 对审核通过的文件保存相对路径、字节数和 SHA-256；普通内容复制到空 TARGET，禁止硬链接，写入前确认目标仍在 TARGET 内且不存在。
7. 复制后重算 SOURCE 和 TARGET 的同一清单哈希。SOURCE 前后变化或 TARGET 不匹配时，这份副本无效；停止，不覆盖重试，不动 SOURCE。
8. 在 TARGET 保存脱敏复制清单和排除原因；更新副本顶层 `AGENTS.md` 和 `project.md`，写明原目录只读、当前阶段、下一步和证据位置。

复制工具应采用 `lstat`/Windows 文件属性检查、路径包含关系检查和独占创建，而不是 `Copy-Item -Recurse` 一把复制。检查 TARGET 不能等于 SOURCE、不能是其子目录或祖先，也不能经过 reparse point；比较使用 Windows 大小写不敏感的规范路径。

哈希清单只能证明所选文件一致，不能证明整个机器没有变化。SOURCE 是保留的基线，不是异地备份；本方案不解决磁盘故障。

### 2.3 环境与启动也必须隔离

- 不修改系统 PATH、全局 Python、原虚拟环境、原启动脚本、用户全局配置。
- 不调用无法确认归属的全局 `tricoder` 命令。使用 TARGET 的 Python 绝对路径启动。
- 禁止新环境通过 `.pth`、editable 安装、`PYTHONPATH` 或配置指向 SOURCE；检查 `tricoder.__file__` 必须在 TARGET 下。
- 离线测试用假 Provider 和合成配置。清理测试子进程的凭据变量、自动跟踪变量及原数据目录覆盖项，不打印其值；不读取原 `.env.local`。
- CLI、TUI、后台 worker、审计、spill 和 MCP 均显式接收新路径。若现有入口无法覆盖某个默认数据目录，先在副本添加配置注入并测试，不能带着共享目录继续运行。
- 两版不能同时操作同一工作区。测试比较使用两个内容相同但路径独立的 fixture；真实试用亦如此。

## 3. 框架选择与依赖准入

| 方案 | 适配成本和边界 | 决定 |
| --- | --- | --- |
| LangGraph Python | 可嵌入现有 Python；仍需自行管理文件权限、进程、撤销与错误规则 | 接入编排能力 |
| Dify 平台 | 提供 Agent/工作流及插件，但本地工具需要对接平台运行环境和权限边界 | 本次不迁移到平台 |
| 继续手写 | 改动最少，但用户希望以成熟框架管理流程 | 作为基线和回退引擎保留 |

LangGraph 官方仓库采用 MIT 许可；部署服务和传递依赖需要分别检查，不能用主仓库许可证代替整棵依赖审查。[官方许可证](https://github.com/langchain-ai/langgraph/blob/main/LICENSE)

实施 M1 时，在 TARGET 的 `docs/open-source-assessment.md` 写入实际选择的精确版本、Python 支持、发布日期、来源、许可证、依赖树、已知安全问题查询日期和兼容性试验。当前文档没有选定或验证任何具体版本，不能直接安装浮动的 latest。

最小依赖策略：只接入 LangGraph 所需组件，不顺带引入新的 Provider SDK、LangGraph Server、托管服务或在线追踪。现有 `mcp==2.1.1`、Rich、Textual 的约束必须重新求解验证。若新增框架要求改变 MCP 版本，应停在兼容性决策处，不默默升级。

依赖安装须获得明确授权；授权后只在 TARGET 环境安装，使用项目现有锁文件格式锁定完整解析结果。保留旧锁文件的无凭据基线副本，不手写猜测的传递依赖版本。默认关闭网络遥测及在线 trace，并通过离线运行验证没有意外连接。

## 4. 当前代码边界与新文件安排

下表路径均相对于 SOURCE 或复制后的 TARGET，除“新增”项外为已有代码。实施前按符号重新定位，行号会随修改变化。

| 文件 | 现有职责 | 迁移方式 |
| --- | --- | --- |
| `src/tricoder/agent.py` | `CodingAgent.run_with_context_async`、模型回合、顺序调用、完成判定 | 保留为 legacy 引擎；抽取必要共享规则，不整文件重写 |
| `src/tricoder/models.py` | Message、ToolResult、SessionContext、SessionTurnResult | 公共结果结构保持兼容，新增字段使用兼容默认值 |
| `src/tricoder/providers.py`、`protocols.py` | Provider 事件归一化、native/legacy_json | 两引擎共用，首版不换 Provider SDK |
| `src/tricoder/context/manager.py` | 上下文预算、整回合裁剪 | 两引擎共用，不能被无界消息列表代替 |
| `src/tricoder/tools/__init__.py` | 工具检查、分发、结果归一化 | 新图仍只能经 ToolRegistry 调用 |
| `src/tricoder/policy.py`、`tools/binding.py` | 路径、命令、权限及路径绑定 | 每次实际执行必须生效 |
| `src/tricoder/changes.py`、`tools/write.py`、`tools/undo.py` | 修改记录、写入、冲突检测与撤销 | 两引擎共用，checkpoint 不代替它们 |
| `src/tricoder/session_runtime.py`、`tui.py` | 任务锁、界面审批、取消、会话 | 通过引擎工厂选择实现，保持事件和锁生命周期 |
| `src/tricoder/subprocess_control.py`、`mcp/runtime.py` | 子进程和 MCP 生命周期 | 保留在任务作用域内，图结束后等待清理 |
| 新增 `src/tricoder/orchestration/engine.py` | 引擎协议与选择工厂 | 未配置时返回 legacy；langgraph 未安装时明确报错 |
| 新增 `src/tricoder/orchestration/state.py` | 图状态及状态更新约定 | 保留现有领域类型，禁止运行资源进入可保存状态 |
| 新增 `src/tricoder/orchestration/graph.py` | 构图及条件路由 | 不包含直接文件写入或裸 subprocess |
| 新增 `src/tricoder/orchestration/nodes.py` | 上下文、模型、单工具、观察、完成节点 | 委托现有模块；一节点一次明确操作 |
| 新增 `src/tricoder/orchestration/langgraph_engine.py` | 引擎适配、事件、取消和异常出口 | 返回 SessionTurnResult，保留最近已知修改状态 |
| 新增 `tests/test_migration_isolation.py`、`tests/test_engine_contract.py` | 隔离和入口约定 | 不需要真实密钥 |
| 新增 `tests/test_langgraph_engine.py`、`tests/test_engine_parity.py`、`tests/test_migration_faults.py` | 图路径、行为比较、故障注入 | 验证实际工具调用及磁盘结果 |

新增包需有 `__init__.py`。只抽取新旧引擎都需要的规则；若前五项可靠性计划已有实施，复用其 `execution_state.py`、`verification.py` 等模块，不能维护两套含义不同的状态模型。

## 5. 引擎与状态契约

### 5.1 对外接口保持不变

新旧引擎均提供现有异步签名：

```python
async def run_with_context_async(
    self,
    task: str,
    context: SessionContext,
    *,
    cancellation: CancellationToken | None = None,
    event_sink: EventSink | None = None,
) -> SessionTurnResult:
    ...
```

这里展示接口约定，不是可直接替换的实现。`engine.py` 的 Protocol 引用项目现有类型；同步入口仍沿用现有异步桥接约束。拟新增配置 `agent.engine = "legacy" | "langgraph"`，CLI 选项拟为 `--engine`；两者目前均不存在。显式 CLI 优先于项目配置，缺省 legacy，非法值报错。一个任务开始后固定引擎，不允许中途变更。

### 5.2 状态字段与所有权

| 状态 | 谁可以写 | 规则 |
| --- | --- | --- |
| task_id、引擎、工作区身份 | Session runtime | 任务内不可变；不能由模型输出覆盖 |
| messages、round_number | 上下文/模型节点 | 每次调用模型才增加轮数；每轮仍按原上下文管理器裁剪 |
| pending_calls、call_index | 协议解析/执行调度 | 完整解析才入队；每次只执行一个工具；调用 ID 在所属轮内唯一 |
| tool_calls | 执行节点 | 只统计实际调用；SKIPPED 不计入 |
| modified_files、unknown_effects | 可信执行结果归并 | 调用失败也归并已发生修改；未知状态不能被模型清除 |
| verification、验证证据 | 可信测试识别和文件快照逻辑 | 文本“测试成功”不算证据；文件变化后失效 |
| stop_reason、最终结果 | 规则与完成节点 | 区分成功、失败、取消、清理失败和结果不确定 |

`unknown_effects` 和文件版本证据属于前五项计划中的拟议扩展，不是现有字段。图状态中不放 Provider 客户端、Token 对象、锁、Event、文件句柄、MCP 连接或审批回调；这些放在任务级运行资源中，通过依赖注入传给节点。

消息更新采用“节点返回完整替换后的消息列表”的单一约定，首版不混用追加 reducer；否则恢复或节点重复调用容易重复消息。每个请求包含的 tool call 都有一个结果；被取消或跳过的调用同样回填对应 ID，避免破坏协议。

### 5.3 节点顺序

准备上下文 → 可选规划（每任务一次）→ 调用模型 → 解析动作 → 单个工具执行 → 记录结果 → 下一个工具或下一轮模型 → 完成检查。

没有工具的正常回答、非法响应、空任务、达到轮数上限和 Provider 错误都必须有明确出口。工具失败时跳过同批余下调用：可修复错误回到模型重新规划；权限拒绝、取消、未知副作用或清理失败停止任务。

工具节点第一版沿用 ToolRegistry 内部审批等待，不新增 LangGraph `interrupt()` 审批，也不把审批分散到两个地方。这能先保持原来的锁与 MCP 资源生命周期。后续改为原生 interrupt 属于第 10 节的独立扩展。

不要直接以通用并行 ToolNode 替换原工具循环。图节点步数也不等于模型轮数：设置足以容纳预期节点数的框架步数上限，同时保留业务轮数上限、取消和超时；达到任一上限应安全结束，不归为成功。

## 6. 必须保住的安全规则

1. **权限检查不能只放提示词里。** 所有调用继续经过 ToolRegistry 和 policy；拒绝后不执行，不自动换工具绕过。
2. **不启用写工具的通用重试。** Provider 原有“尚未输出事件前”的网络重试可以保留；图层不再叠加一层自动重试。命令超时不代表没执行。
3. **审批必须对应实际动作。** 参数、工作区或文件状态变化后旧批准失效；晚到的批准不能唤醒已经取消的任务。首版沿用并补齐现有检查，不通过 `full_access` 规避对接。
4. **失败也可能已经改了文件。** 失败结果、异常出口和取消出口都要保留已知修改；不确定时标记并禁止 finish 成功。
5. **撤销仍依赖修改前后记录。** 用户手动改过的文件不能被自动覆盖。多文件补偿失败必须显示残留，不能声称完整事务回滚。
6. **完成不能只听模型。** 修改后要求可信验证且与当前文件状态相符；纯只读回答不强制测试。外部 MCP 返回的“验证通过”不直接写可信状态。
7. **取消必须传到执行层。** 先阻止后续工具，再取消等待/Provider，终止受管命令并清理 MCP；不能只结束图而留后台执行。
8. **异常不吞掉。** 明确区分取消、预期工具错误、程序缺陷和清理失败；最终返回带最近修改状态的失败结果，审计故障也不能显示成功。
9. **保留最小持久化。** 首版不保存完整 messages 到磁盘，不启用外部 trace；原 SQLite 不能当新图 checkpoint 数据库。
10. **原项目禁止成为测试目标。** 新版启动时对 SOURCE 及其子目录设置迁移保护拒绝；但这只是应用防误操作措施，不是 OS 强隔离。测试代码/命令本身也必须审查，不能运行不可信任意脚本。

## 7. 与前五项可靠性计划的关系

| 前五项 | 在本次迁移中的安排 |
| --- | --- |
| T1 失败改动与未知状态 | M2 在副本建立共享状态规则，M4 接入真实工具 |
| T2 工具错误分类 | M2 定义错误分类；默认不自动重试 |
| T3 同批失败停止 | M3/M4 在新路由实现；旧版差异必须写入允许变化清单 |
| T4 审批、取消与清理 | M4 补齐后才能开放写入试用 |
| T5 验证绑定文件版本 | M4 接入，M5 故障测试证明旧结果不会误放行 |

不用先在原仓库完成 T1—T5。全部在副本实施；先建立可复用规则，再由新图调用。legacy 原始行为有缺陷时不能为了“完全一致”把缺陷保留到新引擎；应保留原始基线记录并明确列出修复差异。

## 8. 分阶段实施任务

每阶段完成后更新 TARGET 的 `project.md`：完成项、命令、退出码、失败/跳过原因和下一步。阶段检查是工程验收，不意味着每一步都重新请求用户许可；只有授权缺失或重大范围改变时才停下来询问。

### M0：构建可靠副本和原始基线

**交付：** 经审核的代码副本、`runtime/migration/copy-manifest.json`、`baseline.json`、独立规则和状态文档。

- [ ] 按第 2 节检查路径、文件类型和排除项；记录 SOURCE HEAD 与所选文件哈希。
- [ ] 将审核过的当前工作树复制到空 TARGET；核对源前后及副本哈希。
- [ ] 添加 `tests/test_migration_isolation.py`：用临时目录测试同目录、嵌套目录、已有目标、链接及复制时源文件变化均被拒绝，普通文件修改副本不影响源文件。
- [ ] 确认复制工具不读密钥、不复制运行数据、不通过硬链接共享内容。
- [ ] 新副本先不初始化 Git；如用户批准独立 Git，再仅在 TARGET 初始化、检查根目录并采用 `codex/` 分支前缀，不在工作区根仓库提交项目。

**退出条件：** 清单一致，排除项没有进入副本，原文件没有变化。不能满足时不继续安装和开发。

### M1：独立环境与依赖可行性

**文件：** TARGET 的 `pyproject.toml`、`requirements.lock`、`docs/open-source-assessment.md`，必要的配置注入及隔离测试。

- [ ] 审核并记录候选 LangGraph 版本与传递依赖；取得安装授权后创建 TARGET 专用环境。
- [ ] 先用旧依赖在副本跑现有离线测试，记录迁移前真实通过/失败/跳过数；不要在原项目运行会写缓存的基线测试。
- [ ] 安装选定框架并锁定解析结果，重新跑原测试；将环境变化导致的失败与原始失败分开记录。
- [ ] 检查模块加载位置、SQLite/审计/spill 实际路径及禁用在线 trace；测试数据写入不得落到 SOURCE 或原数据目录。
- [ ] 运行最小无工具图，只返回常量，验证 Python、事件循环和 framework API 可用；不调用真实模型。

**退出条件：** 依赖来源与版本可追溯，原功能无新增失败，最小图可运行。依赖冲突时停在副本，不升级原环境。

### M2：统一引擎入口与可信状态

**文件：** 新增 `orchestration/engine.py`、`state.py`，修改副本 `session_runtime.py`、配置解析及相关 CLI 入口；新增 `tests/test_engine_contract.py`。

- [ ] 先写入口测试：缺省 legacy、非法值失败、缺 LangGraph 时选 legacy 仍能启动、任务执行中不能换引擎。
- [ ] 实现延迟导入工厂和第 5 节接口；调用端不感知内部图结构。
- [ ] 按前五项计划定义错误和副作用状态，先用假结果测试失败后 modified_files 仍存在、unknown_effects 不被成功文本清除。
- [ ] 明确会话保存的新增字段及兼容默认值，变更只作用新数据库；运行现有 sessions 和 session_runtime 测试。

**退出条件：** 默认 legacy 的外部行为稳定，状态规则有独立测试，新框架尚不能执行写操作。

### M3：只读 LangGraph 闭环

**文件：** 新增 `graph.py`、`nodes.py`、`langgraph_engine.py`；新增 `tests/test_langgraph_engine.py`、`tests/test_engine_parity.py`。

- [ ] 用固定 Provider 响应编排“读文件→搜索→回答”；先断言实际调用顺序、ID 回填及最终结果。
- [ ] 实现第 5 节节点；复用协议、上下文和 Provider，不复制出第二套解析逻辑。
- [ ] 在此阶段硬性只读：写工具、命令工具及危险扩展被拒绝；不能只依靠模型不调用。
- [ ] 测试空任务、非法参数、未知工具、多工具批次、无工具回答、轮数上限、流未完成时取消。
- [ ] 比较 legacy 与新引擎在独立 fixture 上的观察事件、调用顺序、消息结构和结果；忽略时间戳、随机 ID 等非语义差异。

**退出条件：** 只读正常路径一致；明确的错误修复差异进入 `runtime/migration/expected-differences.md`。CLI/TUI 能显示现有事件，不出现重复文本或重复工具结果。

### M4：接入受控写入、撤销、验证和清理

**文件：** 副本的工具、执行状态、验证、审批及 runtime 模块；新增 `tests/test_migration_faults.py`；扩展现有取消、会话和工具测试。

- [ ] 用合成文件先写审批拒绝、审批后文件变化、部分 patch 失败、测试后文件变化的失败测试。
- [ ] 从 ToolRegistry 逐个接入写工具；保留内部审批路径，每次一个调用，不加图重试。
- [ ] 归并正常结果和异常出口的真实修改记录；未知状态阻止完成。批次失败给剩余调用回填 SKIPPED。
- [ ] 接入前五项 T5 的文件快照和可信验证规则；保留其有界扫描、快照不完整则不认定通过等条件。
- [ ] 接入 `/undo`，验证用户修改冲突和撤销补偿失败；撤销后旧验证证据失效。
- [ ] 补齐审批单次决定、等待超时、窗口关闭和晚到批准处理；取消必须唤醒等待。
- [ ] 在假 Provider、本地合成子进程和假 MCP server 中验证取消清理；使用前五项计划的有限清理期限并记录实际结果。

**退出条件：** 第 9 节所有安全关键用例通过；失败会明确停止并保留残留信息。不能因清理超时而显示成功。

### M5：全面回归与回退演练

**文件：** TARGET 的验收报告、README、project.md 及测试证据。

- [ ] 运行完整现有测试和新增测试，记录平台、Python、依赖版本、退出码、数量及跳过原因。
- [ ] 在 Windows 验证命令和 MCP 的受管进程清理；没有 Linux 证据时只声明 Windows 已验证。
- [ ] 完成第 11 节回退演练，不将新版任务状态导入旧引擎。
- [ ] 若需真实 Provider/MCP 冒烟测试，先取得相应联网与凭据使用授权；用户配置新凭据，不复制原密钥文件。未运行则明确列为未验证集成。
- [ ] 验收通过后，仅在 TARGET 修改默认引擎为 langgraph；SOURCE 启动方式及环境继续保留。

**退出条件：** 无新增未解释失败，安全关键项不允许跳过，回退可用，原源码哈希保持基线一致。真实外部集成未测试时可以交付离线验证版本，但不能宣称真实集成全部兼容。

## 9. 验收矩阵：必须测试什么

所有测试都使用独立临时工作区和假响应；命令/MCP 测试使用审核过的本地测试程序。

| 编号 | 注入场景 | 必须看到的结果 |
| --- | --- | --- |
| A01 | 副本修改一个文件 | SOURCE 同文件哈希不变 |
| A02 | TARGET 已存在、路径重叠、junction | 复制拒绝，无覆盖 |
| A03 | 复制时 SOURCE 被修改 | 基线检查失败，不当作有效副本 |
| A04 | 配置指向原 DB/工作区 | 新版迁移入口拒绝启动或拒绝任务 |
| A05 | 未安装 LangGraph 选择 legacy | legacy 正常加载；选 langgraph 明确报缺依赖 |
| A06 | native 和 legacy_json 相同工具任务 | 调用与回填语义一致 |
| A07 | 模型流只生成一半参数就取消 | 工具执行次数为零 |
| A08 | 一轮三个工具，中间失败 | 第三个没有执行，有 SKIPPED 结果 |
| A09 | 工具未知/参数非法 | 结构化失败，无直接异常文本泄漏 |
| A10 | read_only 下写入、命令、危险 MCP | 全部拒绝，磁盘不变 |
| A11 | 越界路径、链接替换、命令绕过 | 执行层拒绝，不依赖提示词 |
| A12 | 拒绝审批、关闭界面、等待超时 | 不执行，无无限等待 |
| A13 | 审批通过后取消或文件被手动修改 | 旧批准不导致写入 |
| A14 | patch 第一文件成功第二文件失败 | 按实际结果补偿/记录残留，不漏记修改 |
| A15 | 执行后、状态归并前抛异常 | 保留日志可确认的修改；其余标不确定，不继续写 |
| A16 | 完整修改后 `/undo` | 恢复原内容；新建文件按既有撤销规则处理 |
| A17 | 用户手动改文件后 `/undo` | 冲突提示，不覆盖用户内容 |
| A18 | 验证通过后文件/测试配置变化 | 完成检查拒绝旧证据 |
| A19 | 模型/MCP 声称测试通过 | 不自动得到可信验证状态 |
| A20 | 命令超时/输出超限/清理失败 | 停止且报真实状态，不自动重跑 |
| A21 | Provider 等待/退避时取消 | 有界退出，不执行后续工具 |
| A22 | MCP 启动/调用/清理失败 | 单任务资源收敛或明确清理失败 |
| A23 | 上下文超预算、长工具输出 | 保留完整 call/result 组，输出有界、暂存路径隔离 |
| A24 | 审计写入失败 | 不显示任务成功，已知修改不丢失 |
| A25 | 达到框架步数/业务轮数上限 | 明确停止，保留上下文与修改状态 |
| A26 | 进程崩溃后重新启动 | 首版不自动恢复、不自动重放写操作 |
| A27 | 回退原版并启动新任务 | 加载 SOURCE 代码和旧独立数据，无新版数据库导入 |

建议测试代码形态：用假 Provider 返回固定工具序列，以 spy 记录真实执行列表；对磁盘读取 before/after；断言第三个工具未调用，而不是仅断言输出文字包含“跳过”。涉及进程的用例断言受管进程实际退出，不能只断言函数返回。

验收命令在 TARGET 专用环境创建并安装完成后运行。以下不是本次文档编写已经运行的命令：

```powershell
Set-Location -LiteralPath 'D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-langgraph'
& 'D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-langgraph/.venv/Scripts/python.exe' -c "import tricoder; print(tricoder.__file__)"
& 'D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-langgraph/.venv/Scripts/python.exe' -m unittest discover -s tests -p 'test_engine_contract.py' -v
& 'D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-langgraph/.venv/Scripts/python.exe' -m unittest discover -s tests -p 'test_migration*.py' -v
& 'D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-langgraph/.venv/Scripts/python.exe' -m unittest discover -s tests -v
& 'D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-langgraph/.venv/Scripts/python.exe' -m compileall -q src tests
```

逐条执行并检查 `$LASTEXITCODE`，前一条失败就停止，不以最后一条退出码代替全部结果。运行前确认日志与缓存写入位置满足隔离约束；记录报告时避免输出凭据和真实源码。

## 10. 为什么首版不开跨进程自动恢复

LangGraph 的 checkpoint 保存图状态，不会还原磁盘文件。原 TriCoder 的修改日志主要在内存里，不能因引入 checkpoint 就声称具备崩溃撤销能力。[状态保存文档](https://docs.langchain.com/oss/python/langgraph/persistence)

首版不配置磁盘 checkpointer；正常运行保持任务内存状态和已有审批等待即可。若实验需要内存 checkpoint，也不得将它描述成重启恢复能力。

后续要启用持久化/原生 interrupt，必须作为独立版本满足以下条件：

- 审批节点恢复会从节点开头执行，节点中断前不能产生写入；官方明确说明该重执行语义。[Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)
- 为操作保存稳定 ID、参数摘要、工作区身份和“准备/执行中/结果确认”状态。崩溃在执行中时，无法确认结果就进入人工核查，禁止盲目重试；不能声称通用 exactly-once。
- 重启后旧审批默认失效，重新校验文件版本和操作参数；恢复输入必须来自可信 UI，不接受模型填入批准。
- 图 checkpoint、修改日志、审批和会话都有版本标记。图版本不兼容则拒绝恢复，不拿旧数据直接跑新图。
- 单独定义 checkpoint 允许保存字段、清理周期和访问权限；默认不持久化源码正文及完整工具输出，不使用任意对象 pickle 恢复不可信内容。
- 明确暂停期间任务锁与 MCP 连接的释放/重建规则，禁止多个进程同时恢复同任务。
- 故障注入覆盖执行前、执行后但确认前、checkpoint 前后，以及恢复期间取消。安全不确定状态必须可见且不可被新成功结果覆盖。

这些条件未满足时保持恢复功能关闭，是设计边界，不是把未完成恢复伪装成已完成。

## 11. 失败回退与最终交付

### 11.1 不同失败怎么处理

| 失败 | 操作 |
| --- | --- |
| 安装/依赖冲突 | 停止副本环境修改，保留报告；原环境不变 |
| 新引擎逻辑错误 | 在副本选择 legacy，或退出副本回到原项目；不迁移进行中的任务 |
| 测试工作区被改坏 | 保留失败现场，另建合成 fixture；不清理或重置原项目 |
| 真实工作区有残留改动 | 先核查并使用可用修改日志；冲突交给用户处理，不自动整目录覆盖 |
| 原项目哈希变化 | 停止迁移，检查是否为其他会话修改；不自动把旧副本覆盖回原仓库 |

### 11.2 回退演练步骤

1. 在副本的合成工作区运行一个会失败的受控任务，记录修改与清理结果。
2. 停止新版，确认受管进程退出；存在不确定清理时先处理，不立即启动冲突任务。
3. 使用 SOURCE 原有环境和启动方式开启一个新的只读任务；确认模块来自 SOURCE。
4. 确认原数据库仍独立，新版 checkpoint/会话/验证结果没有被导入。
5. 核对 SOURCE 复制清单哈希及原环境依赖记录；记录演练结果。

回退不是删除副本，更不是 `git reset --hard`、`git clean`、覆盖原目录或降级原数据库。保留副本供排错。操作系统权限强隔离、容器/虚拟机和异地备份可以另立工程，不在此计划里假称已具备。

### 11.3 交付清单

- [ ] 副本及环境、数据路径独立，原项目未被实施修改。
- [ ] 精确依赖版本、来源及许可检查记录齐全。
- [ ] 新旧引擎选择明确，默认切换只发生在副本。
- [ ] 本文 M0—M5 与 A01—A27 有实际证据；安全关键项无跳过。
- [ ] 已知缺陷、允许行为差异、未验证平台和真实集成分别列明。
- [ ] 回退演练成功；没有将新会话导入旧环境。
- [ ] README 说明如何启动、取消、撤销，以及首版不支持崩溃自动续跑。
- [ ] project.md 可供另一个编程会话直接接手。

工程量判断：对熟悉项目的开发者，这是跨模块迁移，通常应按数周而不是“换几行 API”安排；具体排期在 M1 基线和依赖试验后确定。任何截止日期都不应跳过隔离、部分写入和取消测试。

## 12. 交给编程会话的启动指令

以下文字用于用户决定实施后复制发送；文档本身不触发执行：

> 请读取原项目的 AGENTS.md、README.md、project.md，以及 docs/superpowers/plans/2026-09-14-tricoder-langgraph-safe-migration.md。按文档从 M0 开始，在 D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-langgraph 创建经审核的独立代码副本，并只在副本内实施 LangGraph 迁移；我授权这次复制及副本内必要的代码、测试和文档修改。原项目只读，不复制密钥、私有数据、.git、环境或运行状态，不共用数据库和 Agent 工作区。依赖安装先列明精确方案等待授权；不自行初始化 Git、提交、发布或替换原版。按阶段实现并测试，记录真实证据；遇到结果不确定先停止相关执行，不能通过关闭权限检查来让测试通过。

## 13. 官方参考与核验范围

以下资料于 2026-09-14 查询；正式实施时以锁定版本的 API 和变更记录复核。

- [LangGraph overview](https://docs.langchain.com/oss/python/langgraph/overview)：编排库定位。
- [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence)：checkpoint 与持久化作用。
- [LangGraph interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)：暂停、恢复和节点重执行。
- [LangGraph LICENSE](https://github.com/langchain-ai/langgraph/blob/main/LICENSE)：框架仓库许可，不能代替传递依赖检查。
- [Dify Agent 节点](https://docs.dify.ai/en/cloud/use-dify/nodes/agent)：平台 Agent 的运行方式。
- [Dify 插件类型](https://docs.dify.ai/en/develop-plugin/getting-started/choose-plugin-type)：工具和 Agent Strategy 扩展边界。

本次仅完成工程文档静态核验，未证明迁移实现、依赖兼容或运行安全。所有拟新增路径、配置项和测试在实施前均不存在。
