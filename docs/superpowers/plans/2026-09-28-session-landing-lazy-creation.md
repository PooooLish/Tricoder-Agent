# 无占用入口与 Session 延迟创建实施任务

> **状态：待其他会话实施。本次仅做代码阅读与文档编写，未修改产品代码或测试。**
>
> **For agentic workers:** 使用 `superpowers:executing-plans` 按任务实施；遵守当前用户和仓库规则。本文不授权自动开新会话、派发子代理、安装依赖、提交或推送。当前分析会话只交付任务文档和提示词。

**Goal:** 多个终端都能进入不占用真实 Session 的初始界面；首次提交普通任务时各自创建有独立 UUID 和自动名称的真实 Session，同时保留真实会话的跨进程互斥。

**Architecture:** 将进程内入口状态与持久化业务会话分开。默认启动不恢复 latest、不创建 default、不持有 Session 锁；真实会话只在首个普通任务、明确新建或明确选择已有会话时激活。创建和激活在 Runtime 的同一任务互斥边界内完成，CLI/TUI 共同调用这条路径。

**Tech Stack:** Python 3.11+、SQLite、Rich CLI、Textual TUI、现有 `threading.Lock`、标准库 UUID 与 Windows/POSIX 文件锁；不新增依赖。

**Spec:** 本文“产品约定与验收边界”为实施依据；定位对应 2026-09-28 本地代码，行号可能随后续修改变化。

**项目根目录：** `D:\MaHong\AGENT_WORKSPACE_V2\projects\tricoder-cli`。

## 全局约束

- 当前分析会话以后只分析、写任务文档和提示词。用户将代码实施交给其他会话；这一分工不是禁止整个仓库修改代码。
- 实施前读取 `../../AGENTS.md`、`AGENTS.md`、`README.md`、`pyproject.toml` 和 `project.md`。
- 保留当前未提交的 Session 锁及 Eval 改动；不得重置工作区或用旧 HEAD 覆盖它们。
- 不读取 `.env.local`、真实会话数据库或密钥，不安装依赖；测试使用临时工作区、临时 SQLite 和离线 Provider。
- 保留真实 Session 的跨进程独占、实例内任务锁、审批、只读模式、取消与资源清理规则。
- 不以名称 `default`、空任务或“当前没有运行任务”为理由豁免真实 Session 的锁。
- 不依靠 PID、超时抢占、删除锁文件、单纯 SQLite 写事务来替代现有所有权锁。
- 不将首次任务正文、代码、工具结果或模型自由文本自动持久化到会话名称。
- 本次不引入多 Agent 调度、自动任务队列、工作区锁、全文聊天持久化或新的摘要调用。
- 既有的切换语义继续有效：成功切换后释放旧 Session、丢弃其内存上下文/未保存候选/撤销账本；失败时保留原会话。

## 现状与根因

### 1. 启动与恢复已有会话被绑定在一起

位置：[SessionRuntime 初始化](/D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/src/tricoder/session_runtime.py:334)。当前真实代码片段：

```python
self.store.initialize(resolved_workspace)
record = self.store.latest_for_workspace(resolved_workspace)
if record is None:
    self.current = self._activate_new_session("default", resolved_workspace)
else:
    self._session_lock = self._acquire_session(record.id)
    record = self.store.get(record.id)
    memory = self.store.load_memory(record.id)
```

因此终端 A 一启动就占用真实会话；终端 B 也尝试恢复同一工作区的 latest，随后被锁拒绝。A 是否发送过任务不影响此行为。问题并不局限于名为 default 的会话：latest 换成任何名称，入口仍可能堵塞。

前一轮改造解决了“同一 Session 被并发操作”的正确性问题，但没有分离入口与会话所有权。这次应调整激活时机，不能撤掉互斥保护。

### 2. 唯一会话 ID 已存在，名称不是主键

位置：[SessionStore](/D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/src/tricoder/sessions.py:110)。当前实现：

```python
id_factory: Callable[[], str] = lambda: str(uuid.uuid4())
```

`sessions.id` 是 SQLite 主键；`name` 是普通显示字段。`prepare_record()` 先分配 ID，`insert_prepared()` 事务插入会话与空记忆。

UUID 防止身份冲突，不会自动禁止两个显示名称相同。本任务应复用现有 UUID 与数据库主键，同时解决名称可辨识性和 UI 选择歧义，不能用名称当锁键或记忆键。

### 3. UI 假定启动后必有 current.record

受影响位置：`shell.py` 的 `run()`、状态/模型/会话选择；`tui.py` 的 `on_mount()`、侧栏、会话/模型选择和任务提交；`ui.py` 的启动和状态展示。

另外，当前 TUI 会话选择使用：

```python
options = [
    f"{record.name}  ({record.provider} · {record.model})"
    for record in sessions
]
# respond 回调中：
index = options.index(value)
record = sessions[index]
```

两个记录名称、Provider、模型相同，就会得到相同的显示字符串，`index()` 可能总是取第一项。需要将选项值绑定到完整 Session ID，而不是依赖字符串反查。

### 4. Eval 的“重启”目前依赖隐式 latest 恢复

`evals/scenarios.py::run_runtime_scenario()` 的 `restart_session` 关闭原 Runtime 后直接调用工厂。默认启动改为入口状态后，必须先保存旧 Session ID，再显式恢复该 ID，否则会悄悄创建新会话，破坏记忆恢复测评。

## 产品约定与验收边界

### A. 初始入口是进程内状态，不是共享数据库记录

- 裸 `tricoder`、`tricoder chat`、`tricoder tui` 默认进入“新会话 / 尚未开始”。建议不再把这个界面称为持久化的 default。
- 各终端有自己的工作区和启动配置草稿；它们只是显示相似，没有共享 Session ID。
- 不创建 Session 行，不插入 session_memory/conversation_memory 行，不持有 Session 文件锁，不装配会话级工具/Agent、不清理任何已有会话的 spill。
- 可以按当前规则初始化数据库结构、校验工作区并读取会话列表；这些不等于创建或激活 Session。不得占住长事务。
- 不自动选择 latest，即使工作区已有多个历史会话，也保持入口状态。
- 入口应允许查看帮助、工作区、配置和会话列表；没有模型密钥也应能进入入口。需要 Provider 的配置错误推迟到真实会话装配时报告。
- 初始界面直接退出，不留下空 default 会话，也不尝试保存不存在的 current.memory。

### B. 首个普通任务原子创建并激活真实 Session

预期顺序：

```text
非空普通任务
  → 取得当前 Runtime 的任务互斥锁，建立取消/退出所有权
  → 确认尚无真实 Session
  → 校验实际运行配置，分配完整 UUID 与最终显示名称
  → 取得新 UUID 的 Session 锁
  → 构建候选配置、策略、工具、Agent
  → 事务插入 Session 与空记忆
  → 发布 current，刷新界面中的名称与 ID
  → 将用户输入原样作为第一次任务执行一次
```

- 第二条及之后的普通输入沿用这个 Session，不每轮创建，也不自动重复改名。
- 不先创建共享 default 再重命名；直接用最终名称创建新记录。
- 同一终端并发首条输入只能有一个创建者；另一个沿用现有非阻塞任务拒绝规则，不排队、不创建第二个 Session。
- 两个终端同时首发相同任务，必须创建两个不同的 Session ID，并分别持锁。
- 首次命名不调用 LLM，不增加费用或额外摘要轮次。
- `run_task()` 内不能在已持有 `_task_lock` 时调用同样带 `_idle_runtime_change` 的公开 `create()`；应共享一个明确要求调用方持锁的私有激活函数。
- 不持有 `_task_state_lock` 执行 SQLite、Provider 构建或资源清理等耗时操作。

失败与取消以创建提交点区分：

- 持久化提交前，配置/构建/锁定失败：释放候选资源，保留入口，不留下 Session 行，允许用户修正后再试。
- 会话已成功提交并发布后，模型错误、审批拒绝或任务取消：保留该真实 Session 及失败状态，便于继续或检查；不能悄悄回入口后在下次输入再建一个。
- 对 SQLite 已提交但发布/通知阶段发生异常的路径，必须保留已提交 ID 的可追踪性，禁止盲目重试创建或删除真实会话掩盖错误。
- 初始化期间退出：不能在关闭后迟到启动 Agent；锁释放继续遵守资源所有者完成收尾的规则。

### C. 自动名称、UUID 与展示

- 复用完整 UUID4 作为不可变身份，所有锁、持久化、切换、恢复和审批绑定仍使用完整 ID。
- 自动名称推荐固定格式 `会话-YYYYMMDD-HHMMSS-xxxxxxxx`，其中后八位是 UUID 的展示前缀。例如 `会话-20260928-173005-8a21d3f0`，满足当前 1–50 字符名称约束。
- 时间与随机源在测试中可控制；同秒生成和相同首条任务不得共享身份。
- 短 ID 只供阅读，不作为数据库主键。若列表中短前缀重复，延长到可区分长度或显示完整 UUID。
- 手动 `/session rename` 允许同名；列表和当前状态展示“名称＋短 ID”，选择值绑定完整 UUID。重命名不改变 ID、锁或记忆归属。
- 自动名称无须全局唯一约束；完整 ID 才提供身份唯一性。不得为了“去重”检索名称后复用已有会话。
- 不截取用户任务正文来命名：这样会绕过现有“任务原文不自动进入 SQLite”的持久化边界。若以后要语义标题，应另行设计脱敏与确认，不属于本任务。

### D. 入口状态下的命令矩阵

| 输入/操作 | 行为 | 是否创建/占用真实 Session |
|---|---|---|
| 空白、仅回车、无效斜杠命令 | 不操作或显示语法错误 | 否 |
| `/help`、`/status`、`/session current` | 显示入口帮助/状态，不捏造 Session ID | 否 |
| `/session` | 展示历史列表 | 仅选中并成功激活后占用 |
| `/session new <名称>` | 用户显式要求新建，直接创建并锁定 | 是 |
| `/session rename <名称>` | 提示“尚未创建会话” | 否 |
| `/model` | 修改当前终端的待用模型设置；不调用模型、不改历史会话 | 否 |
| `/permission` | 查看/修改待用权限，沿用现有显式降级规则；默认 strict | 否 |
| `/diff`、`/undo`、`/memory` 及其子命令 | 返回明确的无会话/无记录提示；不摘要、不写库 | 否 |
| `/clear` | 提示当前无会话；不清理其他会话的数据 | 否 |
| `/exit`、EOF、关闭界面 | 幂等退出，释放入口拥有的本地资源 | 否 |
| 普通非空任务 | 自动命名并创建，原任务执行一次 | 是 |

进入真实会话后沿用既有命令语义。启动参数中的 workspace、read_only、模型/协议、轮数和上下文预算必须传给首次任务；待用权限不能放宽只读与命令策略。

### E. 显式恢复与历史数据

- 从入口选择已有会话，必须先锁定再加载；被占用时只提示失败并留在入口，CLI/TUI 仍可操作，不整个退出，也不转而新建。
- 活动 A 切换 B 继续采用现有“目标先准备成功，再交出 A”的规则。
- 已持久化、恰好叫 default 的历史记录是普通真实 Session，继续上锁，不改名、不删除、不迁移内容。
- 建议 Runtime 增加可选构造参数 `initial_session_id: str | None = None`：None 表示入口；非空仅显式恢复这个 ID，失败不降级到 latest/default。交互入口默认不传该参数。
- `switch(session_id, confirm=...)` 必须同时支持从入口激活与从真实会话切换；跨工作区确认继续保留。
- Eval 重启必须恢复重启前的完整 ID；禁止简单恢复 latest，禁止新增普通任务造成第二次自动创建。
- Eval fixture 若继续允许 name 作为 target，匹配到多条记录必须报歧义；不得 `matches[0]` 静默选择。
- 现有 UUID 与表结构足够使用，预计无需 SQLite schema 迁移。

## 建议接口边界

以下是实施目标接口，不是当前已存在的实现。实施者可以调整内部命名，但公开行为和验收结果必须一致，并同步更新本文。

```python
# SessionRuntime
current: ActiveSession | None

@property
def has_active_session(self) -> bool: ...

def _require_active_session(self) -> ActiveSession: ...
# 无会话时给出稳定业务错误，不允许 AttributeError。

def _ensure_session_for_task_locked(self) -> ActiveSession: ...
# 仅供已持任务锁的路径使用；已有 Session 直接返回，否则创建并发布。

# 既有接口保留名称，扩展为能处理入口状态。
def run_task(self, task: str) -> RunResult: ...
def create(self, name: str) -> ActiveSession: ...
def switch(self, session_id: str, *, confirm: Callable[[Path], bool]) -> ActiveSession: ...
def close(self) -> bool: ...
```

`RuntimeStatus.record` 应允许为空，并提供入口展示所需的工作区和待用模型/权限状态。由 Runtime 统一提供状态投影，UI 不从假造的 `SessionRecord("default", ...)` 取值。不得用散落的 `getattr(..., default)` 吞掉真实装配错误。

## 文件责任映射

| 文件 | 本任务职责 |
|---|---|
| `src/tricoder/session_runtime.py` | 入口/活动状态、首次创建、显式恢复、空状态命令、取消退出与锁交接 |
| `src/tricoder/sessions.py` | 复用 UUID/事务；如需命名辅助，只生成安全显示名称，不保存用户正文 |
| `src/tricoder/session_lock.py` | 原有真实会话锁保持有效，不能加入 default 豁免 |
| `src/tricoder/cli.py` | 裸入口/chat/tui 默认采用入口状态，保持退出码与资源关闭 |
| `src/tricoder/shell.py`、`src/tricoder/ui.py` | 无 current 的启动/状态/选择，首任务后展示真实名称和 ID |
| `src/tricoder/tui.py` | 初始侧栏、任务激活后刷新、稳定 ID 选项映射、占用错误留在界面 |
| `src/tricoder/evals/scenarios.py`、`src/tricoder/evals/service.py` | 明确恢复相同 ID、处理重名目标、正确关闭生命周期 |
| `tests/test_session_landing.py`（新增） | 入口与首次激活的主验收 |
| `tests/test_session_ownership.py` | 保留真实会话互斥；新增入口不抢锁和首次输入的双进程场景 |
| `tests/test_session_runtime.py`、`tests/test_session_integration.py` | 更新隐式 default/latest 假设，验证切换/记忆/上下文边界 |
| `tests/test_cli.py`、`tests/test_shell.py`、`tests/test_tui.py`、`tests/test_ui.py` | 入口状态、名称/ID、重名选择、不退出的占用失败 |
| `tests/test_eval_scenarios.py` | 重启恢复精确会话 ID及目标歧义 |
| `README.md`、`project.md` | 用户可见行为和验证证据 |

## 重点审查情形

1. 只打开两个终端、一个字都没输入：都能用，数据库无新增会话。归属任务 1、5。
2. 同秒、同任务、同名、短 ID 前缀相同：身份不同，选择不串会话。归属任务 2、4。
3. 首次构建未完成时退出或收到第二次提交：不重复执行、不迟到激活、不死锁。归属任务 2。
4. 只有 `/help` 或 `/memory` 等本地命令：不能意外创建、请求模型或清掉他人的 spill。归属任务 3。
5. Eval 在多个历史会话中重启：仍恢复明确的旧 ID、已审阅记忆及序号，不把新建会话当恢复成功。归属任务 5。

## 分步实施任务

### 任务 1：分离入口状态与显式恢复

**文件：** `session_runtime.py`；新增 `tests/test_session_landing.py`，调整 `tests/test_session_runtime.py`。

**接口：** 构造函数增加 `initial_session_id`；发布可空 current、`has_active_session` 和 `_require_active_session()`；后续任务使用同一状态定义。

- [x] 先写 `test_startup_stays_unbound_even_when_latest_is_occupied`：持有一个真实 Session，同时新建两个默认 Runtime；它们都无 current、会话数量不变、未调用 Agent 工厂/已有 spill cleanup。
- [x] 写 `test_explicit_resume_locks_exact_id`：显式恢复 ID 仍互斥；不恢复另外一个更新时间更晚的记录。
- [x] 写 `test_empty_entry_close_is_idempotent`：入口重复 close 不报错，不新增会话/记忆。
- [x] 运行新增测试，确认失败来自现有隐式恢复，不是 fixture 或导入问题。
- [x] 最小实现入口状态和显式恢复；保持数据库初始化短事务，禁止创建伪 default 行。
- [x] 跑新增测试；将旧恢复测试改为明确选择 ID，不能通过关闭锁或全部删除旧测试来变绿。

### 任务 2：首次任务的延迟创建、命名和并发

**文件：** `session_runtime.py`、必要时 `sessions.py`；`tests/test_session_landing.py`、`tests/test_session_ownership.py`。

**接口：** `_ensure_session_for_task_locked()` 被 `run_task()` 在访问 current/权限/审计之前调用；新建显式会话和首任务共享候选构建逻辑，避免重入公开锁装饰器。

- [x] 写 `test_first_task_creates_once_and_is_delivered_once`：首任务创建一行，第二任务同 ID，Provider 收到的任务次数准确。
- [x] 写 `test_auto_name_does_not_persist_prompt`：输入含合成敏感标记/长代码时，Session 名称只含时间和 ID 信息，长度 1–50，不出现任务片段。
- [x] 写 `test_two_entries_with_same_first_task_get_distinct_ids`：固定同一时间，两个 Runtime 激活为不同 UUID，各自的第二个占用者被拒绝。
- [x] 写 `test_concurrent_first_submission_and_shutdown`：受控阻塞构建，分别注入第二条任务、取消、close；验证执行至多一次、无迟到任务和泄漏。
- [x] 写配置失败、候选构建失败、SQLite 插入失败，以及发布成功后 Provider 失败的不同断言，严格遵守提交前/后边界。
- [x] 先观察失败，再实现最小创建路径和本地名称；不添加 LLM 命名、不存用户首句。
- [x] 运行入口/占用测试，确认既有退出竞争窗口测试仍通过。

### 任务 3：CLI/Shell 的入口命令与首次激活展示

**文件：** `cli.py`、`shell.py`、`ui.py`、Runtime 的状态/本地命令接口；对应 CLI/Shell/UI 测试。

**接口：** UI 从 Runtime 状态投影读取入口配置，`choose_session(..., current_id: str | None)` 返回完整 ID；普通任务仍走 `run_task()`，不在 UI 另写创建逻辑。

- [x] 按命令矩阵补表驱动测试：入口本地命令不建会话、不调用 Provider、不访问其他会话记忆/spill。
- [x] 写裸启动/chat、缺少 Key、EOF 和空输入用例；缺少 Key 只在需要真实装配时阻止任务，并保留入口。
- [x] 写首次任务继承 workspace/read_only/model/预算/待用权限的测试，确保启动参数没有在自动创建时丢失。
- [x] 写选择已占用 Session 后仍可输入 `/help` 的流程测试；不能因恢复失败退出整个 Shell。
- [x] 实现入口启动/状态文案和创建成功后名称＋ID 展示；对需要真实 Session 的命令给出明确提示。
- [x] 运行 CLI/Shell/UI 与入口邻接测试。

### 任务 4：TUI 入口状态与稳定会话选择

**文件：** `tui.py`、必要时选项模态内部值结构；`tests/test_tui.py`、`tests/test_session_landing.py`。

**接口：** Session 选择事件携带完整 ID；显示文本不是身份。若共享 OptionListScreen 被模型选择使用，保持其既有行为或提供独立的会话选项数据，不破坏模型菜单。

- [x] 写两个真实同名、同 Provider、同 model 的记录，选第二项必须激活第二个 UUID；再加短 ID 前缀相同的 fixture。
- [x] 写入口 `on_mount`、侧栏刷新、`/status` 和模型选择，断言无 None 属性异常、无隐式 Session 行。
- [x] 写首次提交后标题/侧栏显示新名称及 ID；任务失败后仍显示已创建的真实会话。
- [x] 写占用选择失败留在入口、成功从 A 切 B 后 A 可由另一 Runtime 接管、退出释放 B 的界面流程。
- [x] 先观察失败，再实现空状态展示及 ID 绑定；不得用 `options.index(显示文本)` 判定 Session。
- [x] 运行 TUI 与入口/锁测试。

### 任务 5：Eval 兼容、真实双进程验收与文档

**文件：** `evals/scenarios.py`、`evals/service.py`、Eval/记忆/恢复相关测试、`README.md`、`project.md`。

**接口：** 重启保存当前完整 Session ID，关闭旧 Runtime 后显式恢复；有意义的成功返回继续携带当前真实 Session。

- [x] 写 `test_restart_restores_exact_id_not_latest`：存在另一个更新更晚的 Session，重启仍恢复旧 ID 与已确认保存的语义记忆。
- [x] 写 `test_named_scenario_target_rejects_duplicates`：两条同名记录不能取第一条伪装成正确选择；完整 UUID 选择正常。
- [x] 增加真实双进程：都停在入口时互不阻塞、都不新增行；同时提交时分别创建；显式选同一个真实 ID 时第二个被拒绝且入口仍可用。
- [x] 验证旧 default 记录原样保留、仍受锁保护；工作区不同/已有会话被占用均不影响默认进入入口。
- [x] 更新既有 mock/factory 的显式恢复语义，保留审批、UNKNOWN、验证证据、只读、记忆预览绑定和退出清理断言。
- [x] 运行聚焦测试，再运行完整离线测试、compileall、`git diff --check`；记录实际数量和跳过原因，不沿用上次锁改造的 1233 项结论。
- [x] 自审：无名称豁免、无假 Session 行、无原文标题泄漏、无首次输入重复执行、无隐式 latest、无 UI 文本定位会话、无关闭后写入。
- [x] 在 project.md 记录结果和仍未验证的平台；只汇报真实测试证据，不自动提交或推送。

## 验证命令（供实施会话执行）

Windows 本仓库已有虚拟环境，可用以下命令；环境缺失时报告，不自行安装：

```powershell
.venv/Scripts/python.exe -m unittest tests.test_session_landing tests.test_session_ownership -v
.venv/Scripts/python.exe -m unittest tests.test_cli tests.test_shell tests.test_ui tests.test_tui -q
.venv/Scripts/python.exe -m unittest tests.test_eval_scenarios -v
.venv/Scripts/python.exe -m unittest discover -s tests -q
.venv/Scripts/python.exe -m compileall -q src tests
git diff --check
```

成功标准：入口及交接场景全部通过；原有真实 Session 独占仍通过；全量测试无未解释失败。Windows 与 POSIX 的实机证据分别记录，未运行的平台不得写成已验证。

## 可直接复制给实施会话的提示词

```text
请在 D:\MaHong\AGENT_WORKSPACE_V2\projects\tricoder-cli 实施：
docs/superpowers/plans/2026-09-28-session-landing-lazy-creation.md

目标：默认启动进入每个终端独立、无持久化 Session ID、无会话锁的初始入口；首个非空普通任务才原子创建并激活真实 Session。继续使用已有 UUID4 与跨进程独占锁，自动名称用时间＋短 ID，不保存首条任务正文，不额外调用 LLM。不同终端可同时进入入口，各自首次输入后获得不同 ID；显式操作同一个真实 Session 仍必须互斥。

先读 ../../AGENTS.md、AGENTS.md、README.md、pyproject.toml、project.md 和任务文档。保留现有未提交的 Session 锁与 Eval 改动，按任务 1—5 小步实现并验证，不需要再次讨论是否应该有入口状态。

重点：不能豁免名叫 default 的真实会话；不能只在 UI 延迟创建却让 Runtime 仍恢复 latest；不能让 run_task 重入带任务锁的 create；要处理取消/退出与首次提交竞争；TUI 选择绑定完整 UUID，不能通过同名显示文本反查；Eval 重启显式恢复原 ID；切换成功释放旧 Session，失败保留原状态和锁。入口的斜杠命令按文档矩阵处理。

请先写失败测试，再实现，最后执行聚焦测试与完整离线回归，完成差异自审，并更新 README.md、project.md 和任务清单。报告用户可见行为、真实测试结果、兼容变化及未验证平台。

不要读取 .env.local、真实会话数据库或密钥，不安装依赖，不调用真实 Provider，不自动提交或推送，也不要自动派发其他会话。文档作者会话只负责分析，本实施会话按以上范围修改代码。
```

## 本次分析交接记录

- 已确认：默认启动的 latest 恢复＋立即上锁是入口阻塞根因；UUID/主键已存在；TUI 同显示文本存在选择歧义；Eval 重启依赖旧默认行为。
- 已产出：产品约定、文件映射、5 个实施任务、测试与异常边界、实施提示词。
- 未实施：本文所有目标行为；未新增或运行功能测试，未访问真实用户会话库。
- 下一步：用户将上述提示词交给另一个代码实施会话。
