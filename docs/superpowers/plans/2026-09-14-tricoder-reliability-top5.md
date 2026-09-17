# TriCoder 前五项可靠性改进任务计划书

版本：2026-09-16 · 状态：实施完成并通过独立整体复审（T1–T5、I01–I07 已通过）

**目标：**让工具失败、批次停止、任务取消和文件验证使用一致的事实，避免“文件已经改变，但任务仍显示没改动或验证通过”。

**执行方式：**按任务逐项实施和验证，默认在当前任务内顺序推进。实施时使用 executing-plans 的逐项执行方式；不得因本文自动创建并行 Agent、安装依赖、提交或发布。本文中的新增类型、字段、函数和测试均是拟议设计，不是已存在功能。

**技术栈：**Python 3.11+、asyncio、threading、unittest、现有 Textual 界面、MCP stdio 适配器与跨平台进程管理。不引入第三方依赖。

## 1. 范围、事实与交付物

本计划只覆盖上一份清单的前五项：

| 编号 | 任务 | 交付结果 |
| --- | --- | --- |
| T1 | 失败操作的实际改动追踪 | 成功、失败与实际副作用分开记录 |
| T2 | 工具错误分类 | 稳定错误类别、恢复建议、正确的停止边界 |
| T3 | 同批工具失败传播 | 安全地停止剩余动作，调用结果保持完整 |
| T4 | 取消与资源清理 | 审批、命令、MCP 的退出路径可复核 |
| T5 | 验证绑定文件状态 | 陈旧或不完整的验证证据不能支持成功 |

不包含：容器沙箱、多用户服务、自动重试框架、统一费用预算、RAG、LSP、多语言工具链、持久化断点恢复和语义长期记忆。上述能力不作为五项任务的验收条件。

**本次计划编写的证据边界：**读取当前代码、测试和设计文档；本轮未运行项目测试、模型、攻击或故障复现。取消死锁和最终成功误判的完整触发链仍需实验。不得把下表中的静态事实说成已经修复或已经证实的线上故障。

| 当前事实 | 依据 | 计划含义 |
| --- | --- | --- |
| 只有 `result.ok` 为真才消费修改路径 | [agent.py:657](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/src/tricoder/agent.py:657) | T1 不能只靠改返回文字 |
| 现有测试要求失败路径不进入修改列表 | [test_agent.py:1427](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/tests/test_agent.py:1427) | 必须区分不可信自报路径与真实残留，不能直接删除旧测试 |
| 补偿失败可记录实际状态或 tainted 冲突 | [write.py:629](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/src/tricoder/tools/write.py:629) | 尽量复用现有账本，不另建源码持久化库 |
| 工具错误主要表现为 `ok=False + output` | [models.py:249](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/src/tricoder/models.py:249) | T2 补机器可判定的类别，不从中文文案猜错误 |
| 普通失败后同批动作可能继续 | [agent.py:563](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/src/tricoder/agent.py:563) | T3 明确采用保守的批次停止策略 |
| TUI 审批采用无显式截止时间的 `done.wait()` | [tui.py:472](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/src/tricoder/tui.py:472) | T4 先验证异常、退出与取消是否释放等待 |
| finish 依赖文件列表与字符串验证状态 | [agent.py:738](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/src/tricoder/agent.py:738) | T5 添加本地可信证据，并保留 UI 字符串兼容 |

关联设计：[变更管理闭环](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/docs/superpowers/specs/2026-08-01-change-management-loop-design.md)、[MCP 清理修订](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/docs/superpowers/specs/2026-09-07-tricoder-verified-stdio-remediation-design.md)。原有“会话隔离、源码快照不持久化、撤销先核验冲突”的约束继续生效。

## 2. 顺序、工作量与统一验收规则

**实施顺序：T1 → T2 → T3 → T4 → T5 → 集成验收。** T4 可以独立调查，但生产修改仍顺序推进，避免同时改动 Agent、Runtime 和结果类型。

| 阶段 | 预估有效工程日 | 前置条件 | 通过条件 |
| --- | --- | --- | --- |
| 基线与复现准备 | 0.5–1 | 保存当前差异清单 | 环境和既有失败有记录 |
| T1 | 2–3 | 基线 | 残留可见，不可信路径被拒绝 |
| T2 | 1–2 | T1 的副作用约定 | 各层错误含义一致 |
| T3 | 1–2 | T1、T2 | 同批停止不破坏工具消息配对 |
| T4 | 2–3 | T2、T3 | 取消响应与资源回收分别有证据 |
| T5 | 2–3 | T1–T4 | 版本变化、覆盖不全、恢复旧状态均不能假通过 |
| 集成与文档 | 1 | 五项通过 | 完整测试和用户路径一致 |

合计约 9.5–15 个有效工程日，是排期参考，不是承诺；跨平台进程问题可能增加时间。先完成每项最小闭环，再扩大覆盖，不在复现前做全项目重构。

每项任务都执行：新增有意义的失败用例 → 确认失败原因 → 最小修改 → 聚焦回归 → 自我审查 → 记录结果。既有行为本来正确时，新增用例可以直接通过，保留为特征测试，不人为制造失败。

### 开始实施前的准备

- [x] 阅读仓库 AGENTS.md、README.md、pyproject.toml 和上面关联设计。
- [x] 保存 HEAD、工作区差异和测试环境版本。当前已有用户改动在 `test/` 演示目录，不能清理、覆盖或混入本次修复。
- [x] 使用已有项目 `.venv`；若不可用，先报告原因，不自动安装依赖或借用不明环境。
- [x] 在仓库自己的 `runtime/reliability-top5/` 保存测试日志和不含敏感信息的证据；测试临时目录只放虚构文本和测试进程。
- [x] 先跑一次全量基线，将既有失败与新增失败分开。修改前后必须使用一致环境比较。

下列命令均为**实施时要执行的命令，本次仅写入计划**：

```powershell
Set-Location -LiteralPath 'D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli'
& '.\.venv\Scripts\python.exe' -B --version
& '.\.venv\Scripts\python.exe' -B -m unittest discover -s tests -q
git diff --check
```

不要使用 `test/` 代替 `tests/`。后者才是本计划的项目回归测试目录。测试记录应包含完整命令、退出码、数量、失败、跳过项目和平台；不预填“全部通过”。

## 3. 五项任务共用的设计约定

### 3.1 三件事分开表达

1. **操作结果**：工具要求的事情是否成功，继续使用 `ToolResult.ok`。
2. **副作用事实**：文件是否实际改变，是否还有无法确定的影响。
3. **验证证据**：哪次检查针对哪份文件状态，通过还是失败。

`ok=False` 既可能是“参数被拒绝、根本没执行”，也可能是“写了一半、补偿失败”。任何后续策略都不能把这两种情况合并。

### 3.2 拟新增的最小共享类型

新增 [execution_state.py](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/src/tricoder/execution_state.py)（拟新增）。它只定义不可变状态与纯函数，不读取文件、不审批、不依赖 SDK。

```python
from dataclasses import dataclass
from enum import Enum

class EffectState(str, Enum):
    NONE = 'none'
    CONFIRMED = 'confirmed'
    UNKNOWN = 'unknown'

@dataclass(frozen=True, slots=True)
class FileEffects:
    state: EffectState
    paths: tuple[str, ...] = ()

@dataclass(frozen=True, slots=True)
class ExecutionState:
    modified_files: tuple[str, ...] = ()
    verification: str = '未运行'
    unknown_effects: bool = False

    def observe(self, effects: FileEffects) -> 'ExecutionState':
        paths = tuple(dict.fromkeys((*self.modified_files, *effects.paths)))
        uncertain = self.unknown_effects or effects.state is EffectState.UNKNOWN
        invalidated = effects.state is not EffectState.NONE
        return ExecutionState(
            paths,
            '待验证' if invalidated else self.verification,
            uncertain,
        )
```

以上是状态转换的最小实现形状。构造／入口校验还必须拒绝 NONE 携带路径、CONFIRMED 没有路径、非字符串路径、越界路径及不合法枚举；UNKNOWN 可以同时包含已确认的部分路径。实际接入要遵守：`FileEffects` 只接受本地执行边界确认后的字段。它不是凭类型就可信。`ToolRegistry` 必须校验来源和规范路径，不能让模型参数或 MCP 自报的同名字段取得信任。

在 `ToolResult` 末尾追加 `file_effects: FileEffects | None = None`；在 `RunResult`、`SessionContext`、`SessionMemory` 末尾追加 `unknown_effects: bool = False`，避免破坏既有位置参数构造。T5 再追加验证证据。旧字段 `relative_path/modified_paths/verification` 暂时保留供兼容，逐个迁移消费者，不一次性重写全部模块。

### 分阶段兼容与持久化

- T1 先迁移内置文件工具的真实副作用；未迁移命令保留现有行为，不在这一阶段把所有成功验证命令一概改成 UNKNOWN。T2 对已经开始且结果不确定的失败执行收紧为 UNKNOWN，T5 再建立成功检查命令的文件快照证明。中间阶段不宣称具备完整文件状态保障，每项聚焦测试必须能独立通过。
- 追加 `RunResult.cleanup_failed: bool = False`，用于 T4 的稳定失败表达；另在 Runtime 保留未完成清理的资源登记。清理失败不能由后续一次工具成功覆盖。
- T1 的 `unknown_effects` 通过 SessionMemory 保存为 SQLite 的单个布尔元数据，禁止保存源码快照。修改 sessions.py：建表包含 `unknown_effects INTEGER NOT NULL DEFAULT 0`；既有数据库通过事务内 `PRAGMA table_info(session_memory)` 判断后增列；load/save 明确验证只接受 0/1，缺列旧库只通过迁移兼容。迁移仅在实施时运行，本次不接触真实数据库。
- 对迁移使用独立临时数据库验证旧结构升级、重复初始化、新旧行读取、失败回滚。不复制带私人内容的真实数据库充当测试数据。不删除用户记录。
- UNKNOWN 在跨轮次和重启后仍保留，默认阻止该 Session 继续执行写入／命令。用户检查实际文件后，可通过现有 `/clear` 发起一次明确确认：提示它只清理会话记录、不恢复文件；取消确认则保持阻断。需要在 shell.py、tui.py 和 session_runtime.py 的 clear 入口统一处理，不能通过普通新任务自动解除。对应测试覆盖 CLI、TUI 与重启。
- 多个旧版本程序同时读写同一数据库不在保证范围内。代码回退后旧程序可能忽略 UNKNOWN 列，应停止执行有未确认状态的 Session，而不能把数据库向后兼容当成安全语义向后兼容。

### 3.3 副作用归属规则

| 情况 | 本地形成的副作用证据 | 任务行为 |
| --- | --- | --- |
| 参数拒绝、审批拒绝、未启动执行 | NONE | 不污染文件列表 |
| 成功完成并核验的内置文件修改 | CONFIRMED＋规范路径 | 记录路径，旧验证失效 |
| 写入失败但残留可证明属于本工具 | CONFIRMED＋残留路径 | 记录残留，停止当前批次；下一轮可修复 |
| 补偿完整且核验恢复到原状态 | NONE | 不凭空增加残留；T5 仍自行核对版本 |
| 部分路径可确认，另有 tainted／无法读取路径 | UNKNOWN＋已确认路径 | 保留已知部分，停止任务并报告未确认状态 |
| 已启动命令或 MCP 调用，影响范围无法观察 | UNKNOWN | 不把外部自报的“无修改”当证明；T5 的受控命令快照只解决覆盖范围内的文件状态 |

UNKNOWN 表示需要核对的状态，不表示已经确认文件遭到破坏。第一版不自动清除 UNKNOWN、不自动重试；用户检查后重新发起任务。任务返回和会话状态须保留“未确认”提示，不能在 Runtime 收尾中悄悄归零。

## 4. T1：修正失败操作的实际改动追踪

### 目标与边界

让工具失败后留下的真实改动进入 Agent、任务结果、会话状态和账本。保留“失败工具随意附带的路径不能污染状态”的安全约束。首个闭环以 `apply_patch` 的真实残留为核心，同时覆盖 edit/create、取消和异常收尾。

### 文件与职责

| 文件 | 改动 |
| --- | --- |
| [models.py](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/src/tricoder/models.py:249) | 追加状态字段，保持旧构造兼容 |
| [execution_state.py](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/src/tricoder/execution_state.py)（新增） | 上节纯状态类型 |
| [tools/write.py](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/src/tricoder/tools/write.py:312) | 完成发布／补偿后依据实际后态生成证据 |
| [changes.py](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/src/tricoder/changes.py:212) | 增加活动账本只读状态出口，保留 tainted，即使净修改为空 |
| [tools/__init__.py](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/src/tricoder/tools/__init__.py:296) | 归一化来源、路径和副作用，不信任外部自报 |
| [agent.py](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/src/tricoder/agent.py:657) | 以证据更新状态，移除对 ok 的单一依赖 |
| [session_runtime.py](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/src/tricoder/session_runtime.py:455) | 正常、异常、取消时均对账，不能拿旧 context 封存新残留 |
| [sessions.py](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/src/tricoder/sessions.py:102)、[shell.py](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/src/tricoder/shell.py:1)、[tui.py](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/src/tricoder/tui.py:1) | 最小状态迁移、恢复阻断与明确确认清除 |
| [tests/test_effect_state.py](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/tests/test_effect_state.py)（新增）及既有 test_tools、test_changes、test_agent、test_session_runtime | 状态契约和真实文件集成回归 |

**新增账本接口：**`ChangeJournal.active_effects() -> FileEffects`。返回活动任务的净改动路径与 tainted 状态，不返回源码正文、不封存、不清空；没有变更且没有 tainted 返回 NONE，有 tainted 返回 UNKNOWN。每个文件工具还需比较本次操作的前后态，不能把“任务此前改过”错当“这次又改了”。

**消费接口：**`ExecutionState.observe(effects: FileEffects) -> ExecutionState`。其输入必须已经通过 Registry 本地信任校验。Runtime 的异常收尾通过 `active_effects()` 合并事实，并保持原始异常为主异常。

### 实施步骤

- [x] T1.1 先保留 `test_failed_result_does_not_record_legacy_or_multi_file_paths` 的不可信路径场景。新增另一组使用真实 ToolRegistry 与真实文件写入的残留用例，明确不是靠假路径证明问题。
- [x] T1.2 添加 FileEffects 和状态转换测试；以下测试放入拟新增 test_effect_state.py，确认新增文件尚未实现时因缺少接口失败。

```python
import unittest
from tricoder.execution_state import EffectState, ExecutionState, FileEffects

class EffectStateTests(unittest.TestCase):
    def test_residual_write_invalidates_previous_verification(self):
        state = ExecutionState(verification='通过')
        updated = state.observe(FileEffects(EffectState.CONFIRMED, ('a.py',)))
        self.assertEqual(('a.py',), updated.modified_files)
        self.assertEqual('待验证', updated.verification)

    def test_uncertainty_survives_a_later_noop(self):
        state = ExecutionState().observe(FileEffects(EffectState.UNKNOWN))
        updated = state.observe(FileEffects(EffectState.NONE))
        self.assertTrue(updated.unknown_effects)
```

- [x] T1.3 在 write.py 的发布、补偿及补偿失败出口补证据。保留既有目录绑定和身份核验；核对不清楚的对象标 UNKNOWN，不把外部快照纳入可撤销记录。
- [x] T1.4 在 Registry 做来源校验：内置写工具的可信证据可消费；扩展返回的 FileEffects 一律不直接采纳；旧的成功内置路径只在完成路径核验后走兼容分支。
- [x] T1.5 Agent 每个结果先更新副作用，再处理错误或 finish。增加临时结束条件：`unknown_effects` 为真时不得返回成功。
- [x] T1.6 Runtime 在异常与取消时读取活动账本对账，保留原异常；封存时 tainted 不得因零净变化丢失。取消前已完成写入也必须反映到任务结果或状态。
- [x] T1.7 接入上节 SessionMemory／SQLite 的最小状态字段及 clear 确认，先在临时数据库验证迁移与重启阻断。
- [x] T1.8 跑下列用例，核对实际磁盘、返回值、会话状态、账本四者一致；审查 diff，记录结果。

### 验收用例

| ID | 输入或故障注入 | 必须观察到 |
| --- | --- | --- |
| E01 | 第二个文件发布失败，首个文件补偿失败 | 首个残留路径可见；验证待验证；不能直接 finish 成功 |
| E02 | 第二个发布失败，全部补偿成功 | 无虚构残留；错误仍报告；不宣称整个操作成功 |
| E03 | 失败结果携带 `../outside` 或任意假路径 | 不进入 modified_files／账本 |
| E04 | 一条已确认残留，另一条身份变化 | 已知路径保留，unknown_effects 为真 |
| E05 | 多次修改同一文件后失败 | 路径去重；任务最早 before 与最新可信 after 正确 |
| E06 | 写入后取消或内置工具抛未知异常 | Runtime 收尾仍保留实际改动，主异常不被覆盖 |
| E07 | 没有净变化，但存在 tainted | 不丢弃冲突状态，不允许无条件撤销或成功 |
| E08 | UNKNOWN 后重启、确认清除、拒绝清除 | 重启仍阻断；仅明确确认后清记录；文件不被自动恢复 |
| E09 | 旧数据库升级、重复升级、迁移失败 | 记录保留、迁移幂等、失败不留下半迁移状态 |

```powershell
& '.\.venv\Scripts\python.exe' -B -m unittest tests.test_effect_state tests.test_tools tests.test_changes tests.test_agent tests.test_session_runtime tests.test_sessions tests.test_shell tests.test_tui -q
```

## 5. T2：建立可判定的工具错误分类

### 目标与接口

保留人能读的 output，增加程序能用的类别。**本任务不实现自动重试。** “可以修正参数”“可以重新规划”和“重复执行安全”是不同判断。

在 execution_state.py 中复用上节的 dataclass、Enum 导入，增加以下类型，并在 ToolResult 末尾追加 `error: ToolError | None = None`：

```python
class ErrorCode(str, Enum):
    UNKNOWN_TOOL = 'unknown_tool'
    INVALID_ARGUMENT = 'invalid_argument'
    POLICY_DENIED = 'policy_denied'
    APPROVAL_DENIED = 'approval_denied'
    EXECUTION_FAILED = 'execution_failed'
    TIMEOUT = 'timeout'
    OUTPUT_LIMIT = 'output_limit'
    RESULT_UNCERTAIN = 'result_uncertain'
    CANCELLED = 'cancelled'
    CLEANUP_FAILED = 'cleanup_failed'
    INVALID_RESULT = 'invalid_result'
    SKIPPED = 'skipped'

class RecoveryAction(str, Enum):
    REPLAN = 'replan'
    STOP_TASK = 'stop_task'

@dataclass(frozen=True, slots=True)
class ToolError:
    code: ErrorCode
    recovery: RecoveryAction
    retryable: bool = False
```

第一版所有工具错误默认 `retryable=False`。尚未设计通用幂等保证，不开放“按类别自动重试”。Provider 的网络重试属于另一层，保留原有行为和边界。

### 修改位置

- [tools/__init__.py](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/src/tricoder/tools/__init__.py:296)：名称、schema、异常分类与契约校验。
- [tools/command.py](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/src/tricoder/tools/command.py:83)：审批、启动失败、退出码、超时、输出超限和清理失败。
- [tools/write.py](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/src/tricoder/tools/write.py:49)、[tools/filesystem.py](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/src/tricoder/tools/filesystem.py:13)、[tools/search.py](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/src/tricoder/tools/search.py:26)：在错误发生点设置类别。
- [mcp/tool_adapter.py](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/src/tricoder/mcp/tool_adapter.py:34)、[mcp/client.py](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/src/tricoder/mcp/client.py:1)：区分调用前拒绝与已经发出的调用，未知结果不当安全重试。
- [protocols.py](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/src/tricoder/protocols.py:114)：原生、legacy 输出加入可选 error 结构；不向模型公开本地文件身份、完整快照或原始异常。
- [agent.py](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/src/tricoder/agent.py:685)：审计记录稳定代码与状态；不增加原始参数、源码和凭据日志。
- 拟新增 [tests/test_tool_errors.py](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/tests/test_tool_errors.py)，并更新 test_tools、test_protocols、test_mcp_tool_adapter、test_models。

### 分类决策

| 故障 | 类别 | 恢复建议 |
| --- | --- | --- |
| 未注册名称／参数非法 | UNKNOWN_TOOL／INVALID_ARGUMENT | REPLAN |
| 策略拒绝／用户拒绝 | POLICY_DENIED／APPROVAL_DENIED | STOP_TASK；用户重新确认任务前不重复申请相同动作 |
| 可观察的非零退出／文本匹配失败 | EXECUTION_FAILED | 无未知副作用时 REPLAN，否则 STOP_TASK |
| 超时／输出超限 | TIMEOUT／OUTPUT_LIMIT | 确认未执行或结果已核对才可 REPLAN，否则 STOP_TASK |
| 已发外部请求但无法确认结果 | RESULT_UNCERTAIN | STOP_TASK |
| 清理无法确认 | CLEANUP_FAILED | STOP_TASK |
| 内置工具违反返回契约或未知编程异常 | 保留抛异常语义 | 中止并保留根因，不统一吞成业务失败 |
| 外部工具返回无效结构 | INVALID_RESULT | STOP_TASK，受控文案 |

### 实施步骤与契约测试

- [x] T2.1 枚举工具所有 `ToolResult(False, ...)` 和异常出口，按上表逐一归类。建立测试表，不按 output 中出现的词做分类。
- [x] T2.2 写以下真实入口测试；ToolContext 使用临时工作区，审批明确拒绝，无模型调用。

```python
import tempfile
import unittest
from pathlib import Path
from tricoder.policy import CommandPolicy, WorkspacePolicy
from tricoder.tools import ToolContext, ToolRegistry
from tricoder.execution_state import ErrorCode

class ToolErrorTests(unittest.TestCase):
    def test_unknown_tool_has_machine_readable_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = ToolRegistry(ToolContext(
                workspace_policy=WorkspacePolicy(root),
                command_policy=CommandPolicy(workspace=root),
                approver=lambda action, detail: False,
            ))
            result = registry.execute('not_registered', {})
            self.assertFalse(result.ok)
            self.assertEqual(ErrorCode.UNKNOWN_TOOL, result.error.code)
            self.assertFalse(result.error.retryable)
```

- [x] T2.3 修改错误产生点与 `_safe_execution_failure`。成功结果不得携带 error；失败结果缺 error 的旧内置路径可暂按 EXECUTION_FAILED 兼容并逐步补齐；旧扩展失败默认不确定，不能从其原始文字猜恢复策略。
- [x] T2.4 更新两种协议的序列化、CLI／TUI 展示和审计；取消仍通过 CancellationError 传播，Agent 在边界统一形成 CANCELLED，不吞掉 KeyboardInterrupt 或 asyncio 取消。
- [x] T2.5 检查参数、审批、超时、非零退出、无效扩展返回、未知内置异常、日志敏感假标记七组用例。使用虚构敏感标记验证不泄露，不读取真实凭据。

```powershell
& '.\.venv\Scripts\python.exe' -B -m unittest tests.test_tool_errors tests.test_tools tests.test_protocols tests.test_mcp_tool_adapter tests.test_models -q
```

**退出条件：**程序分支不再依靠中文字符串判断工具错误；代码、协议、展示一致；原先会抛出的内置未知异常仍能到达上层。

## 6. T3：同批工具失败后停止剩余动作

### 明确选用的第一版策略

当前模型一次响应可返回多个工具，但没有依赖图。第一版采用：**本批任何一个动作失败，就不执行该批剩余动作。** 普通可修正错误把完整结果交回模型，下一轮重新决策；权限拒绝、取消、清理失败或未知副作用直接停止任务。

这会牺牲一部分独立只读动作的吞吐量，但避免声称能够自动推断依赖。暂不增加 `depends_on` 参数，不并行调用工具，不由字符串分析猜动作是否独立。

### 接口与关键代码形状

在 execution_state.py 增加停止判定纯函数；错误是 T2 的 ToolError，副作用来自 T1：

```python
def should_stop_task(error: ToolError | None, effects: FileEffects) -> bool:
    return (
        effects.state is EffectState.UNKNOWN
        or (error is not None and error.recovery is RecoveryAction.STOP_TASK)
    )
```

修改 [agent.py](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/src/tricoder/agent.py:563) 的内层动作循环：收到结果 → 合并副作用 → 发完成事件并记录审计 → 回填当前结果 → 若失败，给剩余调用回填 SKIPPED → 决定结束任务或请求下一轮模型。

在 CodingAgent 内新增 `_skipped_result(blocked_by: str) -> ToolResult`，只接受本轮已知调用 ID，形成 `ToolError(SKIPPED, REPLAN)`；blocked_by 以受控字段写入结果，不能当可执行指令。未知副作用时不得使用“任务已完成”的成功出口。

**文件：**agent.py、protocols.py；拟新增 [tests/test_batch_failure.py](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/tests/test_batch_failure.py)，更新 test_agent、test_agent_async、test_core_events、test_protocols。

### 实施步骤

- [x] T3.1 用现有 ScriptedProvider／原生 ToolCall 测试样式创建三动作响应：创建 A、测试 A、finish；注入第一个动作失败，先记录当前第二个动作是否执行。
- [x] T3.2 实现单个统一的“回填剩余动作”入口，复用 finish 和 cancel 已有消息配对思路；每个调用 ID 恰好得到一个结果。
- [x] T3.3 添加以下停止判定测试，再接入真实内层循环。纯函数测试不能替代上一步的 Agent 集成用例。

```python
import unittest
from tricoder.execution_state import (
    ErrorCode, RecoveryAction, ToolError, EffectState,
    FileEffects, should_stop_task,
)

class BatchDecisionTests(unittest.TestCase):
    def test_argument_error_can_return_to_model(self):
        error = ToolError(ErrorCode.INVALID_ARGUMENT, RecoveryAction.REPLAN)
        self.assertFalse(should_stop_task(error, FileEffects(EffectState.NONE)))

    def test_unknown_effect_stops_even_if_tool_says_replan(self):
        error = ToolError(ErrorCode.EXECUTION_FAILED, RecoveryAction.REPLAN)
        self.assertTrue(should_stop_task(error, FileEffects(EffectState.UNKNOWN)))
```

- [x] T3.4 同步更新事件语义：被跳过动作不发 ToolExecutionStarted，不算实际执行次数，不触发审批；另发完成／跳过信息时明确 skipped，不能伪造成执行成功。
- [x] T3.5 保留 legacy 单动作兼容、finish 后不执行后续动作、取消后不再请求模型。补回归后更新 README 的串行批次说明。

### 验收用例

| ID | 场景 | 预期 |
| --- | --- | --- |
| B01 | 第一个动作失败，后面是写入、测试、finish | 后三个不执行，调用结果都存在 |
| B02 | 普通参数错误，下一轮给出正确动作 | 可恢复继续，不能重复执行被跳过批次 |
| B03 | 用户拒绝审批，后面还有动作 | 停止任务，不再次申请同一批动作 |
| B04 | 中间动作失败 | 前面已完成的结果与改动保留，后面跳过 |
| B05 | 失败伴随 UNKNOWN 或清理失败 | 不再请求下一轮模型 |
| B06 | finish／取消／legacy 单动作 | 原有结束与协议完整性不回退 |

```powershell
& '.\.venv\Scripts\python.exe' -B -m unittest tests.test_batch_failure tests.test_agent tests.test_agent_async tests.test_protocols tests.test_core_events -q
```

## 7. T4：取消、审批等待与资源清理

### 先调查，再修复

无截止时间的 wait 不必然等于死锁。先复现：审批弹窗异常、用户退出界面、等待审批时取消、命令启动后取消、MCP 初始化中取消、MCP 已发请求后取消。用同步屏障／事件稳定控制时序，不靠随机 sleep 猜窗口。

**两类证据必须分开：**“调用方已经返回取消”和“底层资源已经回收”。前者不能替代后者。

### 文件与拟议接口

- [tui.py](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/src/tricoder/tui.py:472)：审批请求注册、弹窗异常、应用卸载与退出。
- 拟新增 [approval_wait.py](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/src/tricoder/approval_wait.py)：线程安全的一次性审批结果。
- [session_runtime.py](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/src/tricoder/session_runtime.py:406)：令牌、任务 ID、清理结果、释放任务锁。
- [subprocess_control.py](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/src/tricoder/subprocess_control.py:34)：复用既有 POSIX 进程组和 Windows Job；仅针对实际复现的问题修改。
- [mcp/transport.py](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/src/tricoder/mcp/transport.py:48)、[mcp/runtime.py](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/src/tricoder/mcp/runtime.py:44)：资源所有权与有界退出。
- [providers.py](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/src/tricoder/providers.py:184)：区分等待取消与阻塞线程退出，不宣称强杀线程。
- 拟新增 [tests/test_approval_wait.py](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/tests/test_approval_wait.py)，更新 test_tui、test_cancellation、test_subprocess_control、test_mcp_transport、test_mcp_runtime、test_session_runtime、test_agent_async。

`ApprovalWait.resolve(approved: bool) -> bool`：第一次结束请求返回 True，后续调用返回 False；`close() -> bool` 等价于拒绝；`wait(cancellation: CancellationToken, timeout: float = 300.0) -> bool`：批准才返回 True，关闭／取消／超时返回 False。超时只作为审批保护，不等同全任务执行预算。

Runtime 提供 `current_task_cancellation() -> CancellationToken | None`，在状态锁下读取当前令牌供 UI 等待使用。没有活动任务的本地确认使用独立令牌，窗口关闭时仍可统一拒绝。

### 实施步骤与测试

- [x] T4.1 在现有实现复现各场景，记录“已复现／测试已通过／环境不可验证”。不把所有场景都写成 bug。
- [x] T4.2 实现 ApprovalWait：一个锁保护结果的单次写入，一个 Event 唤醒等待；每次等待至多 50ms 检查取消；300 秒默认超时可由构造参数替换，测试不等待真实 300 秒。
- [x] T4.3 给弹窗协程加 finally 释放请求；应用退出时先拒绝所有待决审批再取消任务。线程投递 UI 失败也要关闭请求；迟到的批准不得让已关闭请求重新生效。

```python
import unittest
from tricoder.approval_wait import ApprovalWait
from tricoder.core.cancellation import CancellationToken

class ApprovalWaitTests(unittest.TestCase):
    def test_close_cannot_be_overridden_by_late_approval(self):
        pending = ApprovalWait()
        self.assertTrue(pending.close())
        self.assertFalse(pending.resolve(True))
        self.assertFalse(pending.wait(CancellationToken(), timeout=0.01))

    def test_already_cancelled_wait_denies(self):
        token = CancellationToken()
        token.cancel()
        self.assertFalse(ApprovalWait().wait(token, timeout=0.01))
```

- [x] T4.4 明确批准／取消竞争顺序：审批单次完成只是返回决定；真正执行前必须再次检查取消令牌。不要持有任务状态锁等待 UI 或进程退出。
- [x] T4.5 对进程和 MCP 采用既有清理机制，增加“任务清理共用截止时间”，建议测试目标 5 秒加调度余量 2 秒。各资源只使用剩余时间，不能每个资源重新获得完整 5 秒。正常取消与清理失败分别记录。
- [x] T4.6 清理超时后返回明确 cleanup_failed，并保持失败任务状态；不得留无所有者的后台清理。延后清理必须进入 Runtime 持有的资源登记，退出时再次处理。下一任务不得共享仍被旧任务占用的执行资源。
- [x] T4.7 用真实本地测试进程验证主进程、普通后代、持有输出管道的后代；MCP 用仓库 fake server，测试合作关闭和拒绝关闭。未能验证的平台明确保留限制。

### 验收条件

- C01：审批拒绝、超时、UI 异常、退出、取消都能结束等待；关闭后批准无效。
- C02：普通取消反馈目标为 1 秒内；此为本地测试目标，不宣称任何负载下的实时保证。
- C03：受管进程与流在清理期限内关闭；期限内无法确认时报告失败，并有后续归属。
- C04：清理失败不能覆盖原始业务异常；成功路径遇到清理失败必须降为失败。
- C05：任务锁最终释放，迟到回调不能写入另一个 Session 或新的任务。
- C06：取消发生在写入后时，T1 的文件状态不丢失；取消不自动等同文件撤销。
- C07：明确只证明受管资源；任意脱离进程管理的后代和阻塞网络线程不得写成已全部终止。

```powershell
& '.\.venv\Scripts\python.exe' -B -m unittest tests.test_approval_wait tests.test_tui tests.test_cancellation tests.test_subprocess_control tests.test_mcp_transport tests.test_mcp_runtime tests.test_session_runtime tests.test_agent_async -q
```

## 8. T5：把验证证据绑定到文件状态

### 目标与承诺范围

“通过”必须有证据：哪条受控检查命令、针对哪些文件状态、命令是否通过、前后文件是否稳定。它依然不是业务需求正确性的证明，也不是对抗恶意测试的独立沙箱。本任务不把所有 Python 命令都提升成可信验收器。

第一版保留现有测试／编译命令识别，但增加工作区文件状态核对。这样从根上避免只追踪模型自报路径；也能发现用户同时修改文件或命令顺手改源码。

### 文件状态范围与成本

1. 对工作区内允许读取的普通文件建立排序清单，包含相对路径、类型、字节数、内容 SHA-256、权限和平台可用身份；记录新增、删除、替换。
2. 固定排除 `.git`、`.venv`、`__pycache__`、`.pytest_cache` 目录及沙箱外部工具自身的运行日志。把这些排除项作为范围定义写入证据；不能采用项目任意 `.gitignore` 规则而隐藏测试、配置或源码变化。
3. 权限策略禁止读取的敏感路径只记录“范围不覆盖”的类别，不读取正文。范围说明必须清楚：验证不涵盖这些内容。新增／删除范围外条目不代表已经获得完整仓库证明。
4. 不跟随符号链接、junction、重解析点；范围内发现它们、无法读取对象或扫描中身份变化时，标记 snapshot 不完整，拒绝生成有效证据。
5. 初始上限建议：10,000 个文件、总量 100 MiB、单文件 8 MiB、单次扫描 3 秒。分块读取计算哈希。上限是可调整设计值，需要基准测试；超限不静默略过，不把不完整清单称为完整验证。
6. 将排除规则版本纳入 scope_id；证据留在内存，不把源码或完整快照写入 SQLite。哈希也不默认写入对外日志。

### 拟新增类型和接口

新增 [verification.py](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/src/tricoder/verification.py)，负责安全扫描、证据和比较。`models.py` 只引用类型，不在模块导入时访问文件系统。

```python
from dataclasses import dataclass

@dataclass(frozen=True, slots=True)
class WorkspaceSnapshot:
    scope_id: str
    digest: str
    complete: bool

@dataclass(frozen=True, slots=True)
class VerificationEvidence:
    task_id: str
    command_id: str
    before: WorkspaceSnapshot
    after: WorkspaceSnapshot
    passed: bool

    def is_valid_for(self, current: WorkspaceSnapshot) -> bool:
        return (
            self.passed
            and self.before.complete and self.after.complete and current.complete
            and self.before.scope_id == self.after.scope_id == current.scope_id
            and self.before.digest == self.after.digest == current.digest
        )
```

扫描接口：`capture_workspace(policy: WorkspacePolicy, *, scope_id: str, max_files: int = 10000, max_total_bytes: int = 104857600, max_file_bytes: int = 8388608, timeout: float = 3.0) -> WorkspaceSnapshot`。其文件明细仅用于内存内差异计算；对外结果不暴露正文。具体清单内部结构在本模块封装，不让模型输入 digest。

追加 `ToolResult.verification_evidence: VerificationEvidence | None = None` 与 `SessionContext.verification_evidence: VerificationEvidence | None = None`。证据仅允许本地受控命令路径生成；MCP 或模型伪造同名字段在 Registry 被丢弃并按无可信证据处理。

### 验证状态转换

| 事件 | 状态 |
| --- | --- |
| 成功受控检查，前后快照完整且相同 | 保存证据；在原有失败规则允许时显示通过 |
| 文件写入、外部文件变化、扫描范围变化 | 证据失效，待验证 |
| 检查命令失败 | 失败，记录本次检查对应状态 |
| 同一版本先失败后成功 | 第一版保留当前保守规则，不静默改成通过 |
| 已证明新的文件版本：完整、同 scope 快照的 digest 明确不同，或本地确认写入 | 清除旧版本失败约束，重新等待验证 |
| incomplete、不同 scope 或无法比较 | 通过证据失效，但不解除已有失败约束 |
| 工具结果后通知/审计异常 | 异常仍原样抛；本地任务通道保留新失败与 UNKNOWN，不恢复旧通过 |
| 检查过程本身修改受覆盖文件 | 不采纳通过结果，提示文件状态变化 |
| 取消、清理失败、UNKNOWN 未解决 | 不允许成功 |
| 重启恢复字符串“通过”，但无内存证据 | 降为待验证；不能把 SQLite 字符串当有效证据 |

### 最终成功门槛

- 没有已知修改、没有执行影响未确认、没有旧证据冲突的纯读取任务，仍允许不运行测试就结束。
- 有已知修改的任务：finish 工具成功、UNKNOWN 为假、未取消、清理无失败、当前版本没有未解除的失败记录，且本地 VerificationEvidence 对结束前快照有效，才允许成功。
- 之前已有证据但当前状态变化，即使 modified_files 恰好为空，也不能把它当成未修改任务绕过验证。
- 内存中有效证据可在同一 Session 后续轮次重新核对后使用；task_id 表示产生证据的任务，不要求每轮重新生成，但 Session／工作区归属必须一致。证据不能跨 Session 或重启恢复。
- 任务标识、Session 归属和规范化工作区根由 Runtime 创建，不采用模型输入。scope_id 同时绑定工作区、Session 与排除规则版本。

### 实施步骤

- [x] T5.1 新增 [tests/test_verification_evidence.py](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/tests/test_verification_evidence.py)，先覆盖比较规则与扫描边界。以下为最小证据测试。

```python
import unittest
from tricoder.verification import WorkspaceSnapshot, VerificationEvidence

class VerificationEvidenceTests(unittest.TestCase):
    def test_changed_files_invalidate_success(self):
        old = WorkspaceSnapshot('source-v1', 'a' * 64, True)
        changed = WorkspaceSnapshot('source-v1', 'b' * 64, True)
        evidence = VerificationEvidence('task-1', 'check-1', old, old, True)
        self.assertTrue(evidence.is_valid_for(old))
        self.assertFalse(evidence.is_valid_for(changed))

    def test_incomplete_snapshot_cannot_pass(self):
        incomplete = WorkspaceSnapshot('source-v1', 'a' * 64, False)
        evidence = VerificationEvidence('task-1', 'check-1', incomplete, incomplete, True)
        self.assertFalse(evidence.is_valid_for(incomplete))
```

- [x] T5.2 实现安全扫描；使用已有 WorkspacePolicy 和安全文件访问规则。一次扫描不等于原子快照：前后复核身份和文件清单；发现变化就失败，不尝试读到“差不多稳定”为止。
- [x] T5.3 在 [tools/command.py](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/src/tricoder/tools/command.py:83) 执行前、清理完成后采集快照。只对认可的检查命令生成 VerificationEvidence。实际按 brief 收紧：变化/覆盖不全保持 UNKNOWN，不把外部命令差异伪造为有源码归属凭据的内置工具账本；快照不能证明无网络等外部副作用。
- [x] T5.4 解决 T1 的保守兼容：一般外部命令与非只读 MCP/扩展保持 UNKNOWN；认可检查命令在进程和资源已退出、前后完整一致时，允许就受覆盖文件形成 NONE。命令清理失败或范围不全仍为 UNKNOWN。该判断由本地执行层完成，不能由模型声明命令是只读来触发。
- [x] T5.5 在 [agent.py](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/src/tricoder/agent.py:738) 的 finish 前重新扫描，核对证据仍有效；新写入立即清除证据。UNKNOWN、取消、清理失败是独立阻断条件，不被一次验证通过覆盖。
- [x] T5.6 Runtime、SessionContext 传递证据，数据库恢复降级。实际撤销的证据状态在 Runtime 收尾失效；`tools/undo.py` 仅负责文件事务，无须加入 Session 状态或持久化证据。
- [x] T5.7 更新 CLI、TUI 与状态展示，明确区分“命令退出成功”“文件状态验证有效”“业务需求已验收”。保留字符串接口兼容，但核心成功条件只信任本地证据。
- [x] T5 fix round 1：R1/R2/R3 公开入口先 RED 后 GREEN；真实 scanner 一次性 PermissionError 覆盖入口/检查前/finish，新增每任务本地事实对账并保留 T1 消费确认、T3 配对首异常及 T4 终态撤销。报告见 `runtime/reliability-top5/task-5-report.md`，状态为修复完成待定向复审，不是最终验收通过。
- [x] T5 fix round 2：N1/N2/N3 公开入口 RED→GREEN；以真实提交/首次 taint 修订游标替代 whole-task 布尔重放，Registry 在可抛后处理前发布共享纯验证 transition，获批后非只读外部 dispatch 先登记 UNKNOWN。保留旧断言、真实未消费写入与未批准/预取消控制，详见报告 Fix round 2；修复完成待定向复审。
- [x] T5 fix round 3：F1/F2/M1 正式 RED→GREEN；Runtime 在任意 Agent 前 seed 原状态/游标 0，正常/异常都合并本地发布并验证 scope authority；Agent 合成取消前刷新新失败；净零新提交仍立即失效旧证据，begin/seal 与修订读取共用短锁。保留所有原断言，新增 custom/伪造/迟到发布/并发控制；详见报告 Fix round 3，修复完成待定向复审，不提前关闭 V08。
- [x] T5 fix round 4：仅修复 I1/I2；修改前机械复制/核对 118 文件基线。Runtime 最终 token 取消独立拒绝成功（纯读不伪造 required）；scope-owned evidence 需对结束前当前快照有效，不完整/普通扫描异常保守失败。7 项新测试保留旧断言，Native/structured 取消仍原样经过异常收尾，同步扫描保留 task owner/cleanup scope。65/385/152/356 项门禁非跳过项通过；修复完成待定向复审，不关闭 T5 或 I01–I07。
- [x] T5 fix round 5：两个 Important 正式 RED→GREEN；私有取消接收标志在 seal/current/memory 准备后、persist 前与 cancel_current 共用状态锁形成单一结果决议，提交后拒绝取消但保留任务/退出清理所有权。final capture 阶段中断经既有异常对账撤销 pass、保留 failure/首异常，不扩张为全部通知异常。新增 4 方法、保留旧断言；69/389/152/356 项非跳过项通过及 compileall/diff/cached 通过，修复完成待定向复审。
- [x] T5 fix round 6：唯一 Important 正式 RED→GREEN；仅在三处已有主异常的补偿 seal/persist 捕获 BaseException，保留裸 raise，正常主流程不吞中断。新增 4 方法覆盖 structured/native/系统中断与双补偿失败、failure/dirty、下一任务取消复位；首次 fixture 中文/英文展示差异已按既有契约修正并记录，生产未放宽。73/393/152/356 项非跳过项及 compileall/diff/cached 通过；修复完成待独立复审，不关闭 T5/I01–I07。

### 验收用例

| ID | 场景 | 预期 |
| --- | --- | --- |
| V01 | 修改 → 检查通过 → 无变化 → finish | 可通过文件状态门槛 |
| V02 | 检查通过 → 用户外部修改源码 → finish | 证据失效，不成功 |
| V03 | 检查通过 → 新增／删除文件、修改测试或 pyproject.toml | 证据失效 |
| V04 | 只修改允许排除的 pycache | 范围内证据不因缓存噪声失效 |
| V05 | 检查命令自己修改源码／测试 | 本次“通过”不形成有效证据 |
| V06 | 文件过大、数量过多、链接、权限错误、扫描竞争 | 不完整，不静默跳过后通过 |
| V07 | MCP 或模型伪造 verification_passed／证据 | 无法进入可信验证通道 |
| V08 | 同版本失败后成功；新版本再次成功 | 前者保守失败，后者可重新形成证据 |
| V09 | 重启、切 Session、撤销 | 旧字符串不能成为新任务的有效凭证 |
| V10 | T1 残留未确认、T4 清理失败 | 即使检查退出码为零也不能成功 |

```powershell
& '.\.venv\Scripts\python.exe' -B -m unittest tests.test_verification_evidence tests.test_tools tests.test_agent tests.test_session_runtime tests.test_sessions tests.test_shell tests.test_tui -q
```

**剩余边界：**快照后到实际用户使用文件前仍存在时间窗口；完整消除竞争需要工作区隔离／锁等后续设计。被批准的恶意程序也可能篡改检查设施，本阶段不提供对抗性独立评测或 OS 沙箱。文档应写“通过受覆盖文件状态检查”，不能写“保证代码正确”。

## 9. 集成验收与自我审查

### 必须连起来验证的用户场景

- [x] I01：补丁第二文件失败、首文件残留 → 同批测试与 finish 跳过 → 下一轮读文件修复 → 重新验证后才完成。
- [x] I02：补丁失败且身份冲突 → 标 UNKNOWN → 不再调用模型 → 用户能看到已知残留和需检查状态。
- [x] I03：等待审批时取消／关闭窗口 → 审批线程退出 → 无新命令启动 → 任务锁释放。
- [x] I04：命令取消并发生清理失败 → 保留取消主原因与清理失败证据 → 不显示成功，不复用占用资源。
- [x] I05：验证通过后外部修改测试文件 → finish 拒绝 → 重新检查后按新版本判断。
- [x] I06：同一 Session 跨轮次、切换独立 Session、重启加载、撤销四条路径分别验证，不串用证据。
- [x] I07：原生多工具调用和 legacy 单动作协议都能恢复或正确停止；调用 ID 无缺失、无重复。

最终运行：

```powershell
& '.\.venv\Scripts\python.exe' -B -m unittest discover -s tests -q
git diff --check
```

Windows 是当前主要验证平台。POSIX 目录绑定、进程组和链接语义需要 Linux 环境另验；若没有可用环境，交付必须明确“Windows 已验，Linux 未验”，不能用跳过的测试支撑跨平台保证。

实际集成结果：I01–I07 七项通过；T1–T5 重点门禁通过；第四次完整 Windows
串行运行 1,072 项全部通过，6 项因平台能力跳过。前三次完整运行暴露并保留了过期
测试契约、隔离 verifier 依赖闭包和测试模块双身份问题的失败记录；修复后没有通过
放宽生产清理期限或降低安全断言取得 GREEN。逐项证据见
`runtime/reliability-top5/integration-report.md`。

### 回归审查清单

- [x] 所有新增 dataclass 字段追加在末尾，旧位置参数构造仍可用。
- [x] 同一字段在 Registry、Agent、Runtime、Session 和 UI 中含义一致。
- [x] 外部工具返回的路径、错误策略、验证证据未被直接提升为可信事实。
- [x] 在停止／抛异常／取消之前，已发生的副作用得到记录；没有以旧 context 覆盖新状态。
- [x] 没有隐式自动重试、权限放宽、额外源码持久化或新增凭据输出。
- [x] 被跳过的工具没有执行计数和审批副作用，但协议结果完整。
- [x] 失败保持、验证过期、重启降级等新行为在测试和文档中一致。
- [x] README、project.md 与变更管理／MCP 文档已按最终实现更新；旧面试材料不在本计划工作树范围，交付时另行同步，避免继续宣称旧行为。

## 10. 回退、交接与执行记录

**回退策略：**每项作为独立可审查的变更单元，不自动提交。需回退时只撤回该任务的具体变更，不使用破坏性工作区重置，不覆盖原有 `test/` 用户改动。若后续任务已经依赖新增字段，按 T5→T4→T3→T2→T1 逆序评估回退；禁止只删类型却留下调用者。

**行为兼容说明：**T2 的权限拒绝停止任务、T3 的失败停止整批、T5 的重启后验证降级和扫描成本，属于有意改变。实施时分别记录前后例子，不当成偶然测试失败掩盖。若安全证据无法建立，采用明确失败而非回退到旧的乐观成功逻辑。

| 任务 | 当前状态 | 实施后必须填写的交接内容 |
| --- | --- | --- |
| T1 | 完成（独立复审通过） | 真实残留已复现；内置处理器身份与规范工作区路径共同建立信任；结果、Runtime、SQLite 与账本一致。复审发现的“同路径旧验证＋写入后取消”缺口已 RED→GREEN 修复并定向复审通过。副作用测试 25/25、计划聚焦 325/325、邻近 81/81 通过；此前全量复跑 881 项通过（5 个 Windows 权限跳过）。冷启动 MCP 超时可在未改基线同样复现，作为时序风险保留。 |
| T2 | 完成（独立复审通过） | 错误产生点、Registry 信任、native/legacy 协议、UI 与审计已统一。复审的 4 个 Important 均 RED→GREEN 并定向复审通过。计划聚焦 158/158、邻近与 T1 回归 396/398 通过（2 个平台跳过）。 |
| T3 | 完成（独立复审通过） | B01–B08 已覆盖；复审发现的通知异常截断配对问题已 RED→GREEN 并定向复审通过。协议结果在本地构造成功后先配对、后通知。最终聚焦 125、邻近 242、UI/审计 22，共 389 项通过。 |
| T4 | 完成（独立复审通过） | C01–C07 已覆盖；三轮定向修复依次关闭活动退出、成功路径清理降级、跨任务 deadline、首取消异常竞争和异步 worker 迟到登记所有权。最终聚焦 166、邻近 400 项通过；额外 worker/MCP 取消边界各 2 项通过。只证明受管资源，永久不合作线程、脱离管理的后代和 POSIX 语义仍是明确限制。 |
| T5 | 完成（独立复审通过） | `task-5-fix6-review.md` 独立复审确认 V01–V10 通过，Critical／Important／Minor 均为 0；补偿 BaseException 首异常优先级与正常主流程边界已关闭。独立门禁为 T5 73（1 个既有 Windows symlink 权限 skip）、T1/journal/runtime 96、T3/T4 60 项通过；I01–I07 另行集成验收。 |
| 集成 | 完成（独立整体复审通过） | `test_reliability_integration.py` 的 I01–I07 七条公共流程通过。最终整体复审发现并关闭原生 Task cancel 迟到写、缺失路径错误分流、MCP 冷导入循环与 live-process 取消覆盖缺口；惰性 re-export 保持旧包级导入。Windows 串行全量 1,077 项通过、6 项平台条件跳过；独立整改复审为 0 Critical／Important／Minor。POSIX 与真实 Provider／外部 MCP 仍未验证。完整证据见 `runtime/reliability-top5/integration-report.md` 和 `runtime/reliability-top5/final-remediation-review.md`。 |

**本计划的完成条件：**五项都有具体入口、状态契约、实现步骤、验收场景、聚焦测试命令和交接要求。**代码改进的完成条件不同：**上述测试实际执行且证据通过，才可将任务状态改为完成。本次不预先填写实现和测试成绩。
