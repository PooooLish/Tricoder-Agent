# TriCoder Interactive Slash Commands and Sessions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 TriCoder 增加 `tricoder` / `tricoder chat` 持续交互入口、本地斜杠命令和基于 SQLite 的独立 Session 混合记忆。

**Architecture:** `InteractiveShell` 只维护输入循环，`CommandRouter` 只解析本地命令，`SessionStore` 只负责 SQLite，`SessionRuntime` 负责配置、Agent 与工作区的原子切换。`CodingAgent.run_with_context()` 返回更新后的不可变会话上下文，现有 `run()` 保持兼容。

**Tech Stack:** Python 3.11+、标准库 `sqlite3` / `uuid` / `dataclasses`、Rich 15.x、`unittest`

## Global Constraints

- 所有新增和修改的代码使用规范、容易理解的中文注释。
- 不读取、打印、复制、持久化或扫描真实 `.env.local` 和真实 API Key。
- 不发起真实网络请求；Provider 测试使用 Fake Provider。
- 不安装第三方依赖，不初始化或操作 Git。
- 每项生产行为必须先有能够按预期失败的测试。
- SQLite 数据库不得位于目标工作区，不得保存完整源码、完整工具输出、完整命令、Provider 原始响应或模型动作 JSON。
- 斜杠命令永远在本地处理，不得调用 Provider。
- 跨工作区切换必须确认，且只有全部配置、策略、工具和 Agent 构建成功后才能替换当前 Session。
- 现有 `doctor`、`run`、审批、审计、验证状态和上下文预算行为必须保持兼容。

---

### Task 1: Session 数据模型与 SQLite 仓库

**Files:**
- Create: `src/tricoder/sessions.py`
- Create: `tests/test_sessions.py`
- Modify: `src/tricoder/models.py`

**Interfaces:**
- Produces: `SessionRecord`、`SessionMemory`、`SessionStore`
- Produces: `default_sessions_db(environ: Mapping[str, str] | None = None) -> Path`
- Produces: `validate_session_name(name: str) -> str`
- Produces: `safe_requirement_summary(text: str, max_chars: int = 500) -> str`
- Produces: `safe_result_summary(text: str, max_chars: int = 2_000) -> str`

- [ ] **Step 1: 写默认目录和名称校验失败测试**

```python
def test_default_sessions_db_uses_windows_local_app_data() -> None:
    with patch("tricoder.sessions._is_windows", return_value=True):
        path = default_sessions_db({"LOCALAPPDATA": "D:/state"})
    self.assertEqual(Path("D:/state/TriCoder/sessions.db").resolve(), path)

def test_default_sessions_db_uses_xdg_state_home() -> None:
    with patch("tricoder.sessions._is_windows", return_value=False):
        path = default_sessions_db({"XDG_STATE_HOME": "/var/state"})
    self.assertEqual(Path("/var/state/tricoder/sessions.db").resolve(), path)

def test_validate_session_name_rejects_control_characters() -> None:
    for value in ("", " ", "bad\nname", "x" * 51):
        with self.subTest(value=value):
            with self.assertRaises(SessionError):
                validate_session_name(value)
```

- [ ] **Step 2: 运行 RED**

Run: `$env:PYTHONPATH='src'; python -B -m unittest tests.test_sessions -v`

Expected: FAIL，因为 `tricoder.sessions` 尚不存在。

- [ ] **Step 3: 实现模型与数据库初始化**

```python
@dataclass(frozen=True, slots=True)
class SessionRecord:
    id: str
    name: str
    workspace: Path
    provider: str
    model: str
    created_at: str
    updated_at: str

@dataclass(frozen=True, slots=True)
class SessionMemory:
    summary: str = ""
    requirements_summary: str = ""
    last_task_summary: str = ""
    modified_files: tuple[str, ...] = ()
    verification: str = "未运行"
```

`SessionStore.__init__` 接受数据库绝对路径、可注入 `clock` 和 `id_factory`。`initialize()` 创建设计文档中的两个表并执行 `PRAGMA foreign_keys = ON`。

- [ ] **Step 4: 写 CRUD、顺序和独立记忆失败测试**

```python
def test_create_list_rename_and_memory_are_isolated(self) -> None:
    first = self.store.create("one", self.workspace, "deepseek", "model-a")
    second = self.store.create("two", self.workspace, "glm", "model-b")
    self.store.save_memory(first.id, SessionMemory(summary="first"))
    self.store.rename(second.id, "renamed")

    self.assertEqual("first", self.store.load_memory(first.id).summary)
    self.assertEqual("", self.store.load_memory(second.id).summary)
    self.assertEqual(["renamed", "one"], [item.name for item in self.store.list_all()])
```

- [ ] **Step 5: 实现显式事务方法**

实现：

```python
create(name, workspace, provider, model) -> SessionRecord
get(session_id) -> SessionRecord
list_all() -> list[SessionRecord]
latest_for_workspace(workspace: Path) -> SessionRecord | None
rename(session_id, name) -> SessionRecord
load_memory(session_id) -> SessionMemory
save_memory(session_id, memory) -> None
clear_memory(session_id) -> None
```

JSON 字段只接受字符串数组，读取损坏 JSON 时抛 `SessionError`，不静默覆盖数据库。

- [ ] **Step 6: 写安全摘要和敏感内容测试**

```python
def test_safe_requirement_does_not_persist_multiline_source_or_secret() -> None:
    text = "修复问题\nOPENAI_API_KEY=sk-" + "x" * 40 + "\nprint('source')"
    summary = safe_requirement_summary(text)
    self.assertIn("原文未持久化", summary)
    self.assertNotIn("sk-", summary)
    self.assertNotIn("print", summary)
```

单行短要求可经凭据模式替换后保存；多行或超过 500 字符只保存字符数提示。结果摘要移除 Markdown 代码块、Bearer/Key 模式并截断到 2,000 字符。

- [ ] **Step 7: 运行 Task 1 GREEN**

Run: `$env:PYTHONPATH='src'; python -B -m unittest tests.test_sessions -v`

Expected: PASS。

### Task 2: 本地斜杠命令解析

**Files:**
- Create: `src/tricoder/commands.py`
- Create: `tests/test_commands.py`

**Interfaces:**
- Produces: `ParsedCommand(name: str, subcommand: str | None, argument: str | None)`
- Produces: `CommandError`
- Produces: `parse_command(text: str) -> ParsedCommand`
- Produces: `is_slash_command(text: str) -> bool`

- [ ] **Step 1: 写命令语法失败测试**

```python
def test_parses_session_commands_without_losing_spaces() -> None:
    self.assertEqual(
        ParsedCommand("session", "new", "认证模块 修复"),
        parse_command("/session new 认证模块 修复"),
    )
    self.assertEqual(
        ParsedCommand("session", None, None),
        parse_command("/session"),
    )

def test_rejects_unknown_and_extra_arguments() -> None:
    for text in ("/unknown", "/help extra", "/exit now", "/session bad"):
        with self.subTest(text=text):
            with self.assertRaises(CommandError):
                parse_command(text)
```

- [ ] **Step 2: 运行 RED**

Run: `$env:PYTHONPATH='src'; python -B -m unittest tests.test_commands -v`

Expected: FAIL，因为命令模块不存在。

- [ ] **Step 3: 实现固定命令表和解析器**

允许：

```python
_SIMPLE_COMMANDS = {"help", "status", "model", "clear", "exit"}
_SESSION_SUBCOMMANDS = {"new", "current", "rename"}
```

`/session` 无参数合法；`new`、`rename` 必须有完整剩余字符串；`current` 不接受参数。命令名和子命令大小写不敏感，Session 名称保留原大小写。

- [ ] **Step 4: 验证斜杠命令不会被当普通文本**

```python
def test_is_slash_command_only_accepts_first_non_space_character() -> None:
    self.assertTrue(is_slash_command("  /status"))
    self.assertFalse(is_slash_command("请解释 /status"))
    self.assertFalse(is_slash_command(""))
```

- [ ] **Step 5: 运行 Task 2 GREEN**

Run: `$env:PYTHONPATH='src'; python -B -m unittest tests.test_commands -v`

Expected: PASS。

### Task 3: CodingAgent 会话上下文

**Files:**
- Modify: `src/tricoder/models.py`
- Modify: `src/tricoder/agent.py`
- Modify: `tests/test_agent.py`

**Interfaces:**
- Produces: `SessionContext(messages: tuple[Message, ...] = (), persisted_summary: str = "")`
- Produces: `SessionTurnResult(result: RunResult, context: SessionContext)`
- Produces: `CodingAgent.run_with_context(task: str, context: SessionContext) -> SessionTurnResult`
- Preserves: `CodingAgent.run(task: str) -> RunResult`

- [ ] **Step 1: 写兼容性和独立上下文失败测试**

```python
def test_run_with_context_returns_reusable_history(self) -> None:
    first = agent.run_with_context("检查模块", SessionContext())
    second = agent.run_with_context("继续补测试", first.context)

    self.assertTrue(first.result.ok)
    self.assertIn("用户任务：检查模块", provider.calls[0])
    self.assertIn("用户任务：继续补测试", provider.calls[-1])
    self.assertGreater(len(second.context.messages), len(first.context.messages))

def test_run_still_returns_plain_run_result(self) -> None:
    self.assertIsInstance(agent.run("分析项目"), RunResult)
```

- [ ] **Step 2: 运行 RED**

Run: `$env:PYTHONPATH='src'; python -B -m unittest tests.test_agent -v`

Expected: FAIL，因为上下文接口不存在。

- [ ] **Step 3: 增加不可变上下文类型并抽取内部循环**

```python
@dataclass(frozen=True, slots=True)
class SessionContext:
    messages: tuple[Message, ...] = ()
    persisted_summary: str = ""

@dataclass(frozen=True, slots=True)
class SessionTurnResult:
    result: RunResult
    context: SessionContext
```

`run()` 调用 `run_with_context(task, SessionContext()).result`。内部循环不修改输入 tuple，每次创建新 list。

- [ ] **Step 4: 写跨任务压缩和恢复摘要失败测试**

```python
def test_persisted_summary_is_sent_without_raw_tool_history() -> None:
    context = SessionContext(persisted_summary="此前修改 src/app.py，验证通过")
    turn = agent.run_with_context("继续检查", context)

    first_request = provider.requests[0]
    self.assertIn("持久化会话摘要", first_request[1].content)
    self.assertNotIn("tool_result", first_request[1].content)
```

会话压缩必须以完整用户任务块为单位：system、持久化摘要、最新用户任务始终保留；历史任务及其 assistant/tool-result 对整体保留或整体淘汰，不保留半个任务或半个工具回合。

- [ ] **Step 5: 确保 finish 结果进入上下文**

在 `finish` 返回前把对应工具结果追加为 `Message("user", ..., kind="tool_result")`，使下一次运行得到完整回合。`Message` 新增默认字段 `kind: str = "generic"`，`as_dict()` 仍只输出 `role` 和 `content`。

- [ ] **Step 6: 运行 Task 3 GREEN**

Run: `$env:PYTHONPATH='src'; python -B -m unittest tests.test_agent -v`

Expected: PASS，现有 Agent 测试不回归。

### Task 4: SessionRuntime 原子切换与持久化

**Files:**
- Create: `src/tricoder/session_runtime.py`
- Create: `tests/test_session_runtime.py`
- Modify: `src/tricoder/models.py`

**Interfaces:**
- Produces: `RuntimeOptions`，保存启动时的 env-file、audit-dir、限制和只读选项
- Produces: `ActiveSession(record, memory, context, config, agent)`
- Produces: `SessionRuntime`
- Consumes: `SessionStore`、`load_config`、Provider/Tool/Agent factories

- [ ] **Step 1: 写同工作区和跨工作区失败回滚测试**

```python
def test_cross_workspace_switch_rebuilds_before_commit(self) -> None:
    runtime.switch(target.id, confirm=lambda _: True)
    self.assertEqual(target.id, runtime.current.record.id)
    self.assertEqual(target.workspace, runtime.current.config.workspace)

def test_failed_rebuild_keeps_original_session(self) -> None:
    original = runtime.current
    factory.fail_for(target.id)
    with self.assertRaises(SessionRuntimeError):
        runtime.switch(target.id, confirm=lambda _: True)
    self.assertIs(original, runtime.current)
```

- [ ] **Step 2: 运行 RED**

Run: `$env:PYTHONPATH='src'; python -B -m unittest tests.test_session_runtime -v`

Expected: FAIL，因为运行时模块不存在。

- [ ] **Step 3: 实现运行时装配边界**

`SessionRuntime` 方法：

```python
create(name: str) -> ActiveSession
switch(session_id: str, confirm: Callable[[Path], bool]) -> ActiveSession
rename_current(name: str) -> SessionRecord
clear_current() -> None
change_model(provider: str) -> ActiveSession
run_task(task: str) -> RunResult
persist_current() -> bool
status() -> RuntimeStatus
```

构建候选 Session 时先完成配置、审计、Provider、WorkspacePolicy、ToolRegistry 和 CodingAgent；成功后才赋值 `self.current`。

- [ ] **Step 4: 写两个 Session 独立记忆与模型测试**

```python
def test_sessions_do_not_share_context_provider_or_workspace(self) -> None:
    runtime.run_task("first task")
    first_context = runtime.current.context
    runtime.switch(second.id, confirm=lambda _: True)
    runtime.run_task("second task")

    self.assertNotEqual(first_context, runtime.current.context)
    self.assertEqual("glm", runtime.current.config.provider.name)
    self.assertEqual(second.workspace, runtime.current.config.workspace)
```

- [ ] **Step 5: 实现混合记忆保存**

每次任务后使用 `safe_requirement_summary(task)`、`safe_result_summary(result.summary)`、修改文件与验证状态更新 `SessionMemory`。SQLite 保存失败返回 `False` 并保留内存状态；切换前保存失败时取消切换。

- [ ] **Step 6: 写模型切换失败回滚和 clear 测试**

```python
def test_model_failure_and_clear_are_atomic(self) -> None:
    original = runtime.current
    with self.assertRaises(SessionRuntimeError):
        runtime.change_model("missing-key-provider")
    self.assertIs(original, runtime.current)

    runtime.clear_current()
    self.assertEqual((), runtime.current.context.messages)
    self.assertEqual("", runtime.current.memory.summary)
    self.assertEqual(original.memory.modified_files, runtime.current.memory.modified_files)
```

- [ ] **Step 7: 运行 Task 4 GREEN**

Run: `$env:PYTHONPATH='src'; python -B -m unittest tests.test_session_runtime -v`

Expected: PASS。

### Task 5: InteractiveShell 与命令处理

**Files:**
- Create: `src/tricoder/shell.py`
- Create: `tests/test_shell.py`
- Modify: `src/tricoder/ui.py`
- Modify: `tests/test_ui.py`

**Interfaces:**
- Produces: `InteractiveShell.run() -> int`
- Consumes: `parse_command()`、`SessionRuntime`
- Adds UI methods: `show_shell_start`、`show_help`、`show_status`、`choose_session`、`choose_model`、`show_memory_warning`

- [ ] **Step 1: 写本地命令不调用 Provider 测试**

```python
def test_slash_commands_are_local(self) -> None:
    inputs = iter(["/status", "/help", "/exit"])
    code = shell(input_fn=lambda _: next(inputs)).run()
    self.assertEqual(0, code)
    self.assertEqual([], runtime.tasks)
```

- [ ] **Step 2: 运行 RED**

Run: `$env:PYTHONPATH='src'; python -B -m unittest tests.test_shell -v`

Expected: FAIL，因为 Shell 不存在。

- [ ] **Step 3: 实现循环和基础命令**

```python
while True:
    try:
        text = self.input_fn(self.prompt).strip()
    except KeyboardInterrupt:
        self.ui.show_notice("已清空当前输入")
        continue
    except EOFError:
        return self._exit()
```

空输入忽略；普通文本调用 `runtime.run_task`；未知命令显示错误和 `/help` 提示。

- [ ] **Step 4: 写 `/session` 选择、跨工作区确认和取消测试**

```python
def test_session_selection_confirms_cross_workspace(self) -> None:
    ui.session_choice = 2
    ui.answers = ["n"]
    shell.execute("/session")
    self.assertEqual(first.id, runtime.current.record.id)

    ui.answers = ["y"]
    shell.execute("/session")
    self.assertEqual(second.id, runtime.current.record.id)
```

- [ ] **Step 5: 实现 `/session`、`/model`、`/clear`**

- `/session` 调用 UI 列表并按编号返回 ID。
- 跨工作区确认委托 `runtime.switch`。
- `/model` 展示全部三个 Provider 及其模型；选择后再加载配置，Key 或配置缺失时保持原值并显示错误。
- `/clear` 明确确认后调用 `runtime.clear_current()`。

- [ ] **Step 6: 写持久化失败与退出码测试**

```python
def test_exit_reports_unsaved_memory(self) -> None:
    runtime.persist_ok = False
    code = shell.execute("/exit")
    self.assertEqual(1, code)
    self.assertIn("未持久化", ui.text)
```

- [ ] **Step 7: 运行 Task 5 GREEN**

Run: `$env:PYTHONPATH='src'; python -B -m unittest tests.test_shell tests.test_ui -v`

Expected: PASS。

### Task 6: CLI 入口与装配

**Files:**
- Modify: `src/tricoder/cli.py`
- Modify: `tests/test_cli.py`
- Modify: `src/tricoder/__main__.py`

**Interfaces:**
- Preserves: `tricoder doctor`、`tricoder run`
- Produces: 裸 `tricoder` 与 `tricoder chat`
- Produces: 可注入 `shell_factory` / `session_store_factory` 测试边界

- [ ] **Step 1: 写解析器入口失败测试**

```python
def test_bare_and_chat_enter_same_shell(self) -> None:
    for argv in ([], ["chat"]):
        with self.subTest(argv=argv):
            exit_code = main(
                argv,
                environ={"DEEPSEEK_API_KEY": "test-key"},
                shell_factory=recording_shell,
            )
            self.assertEqual(0, exit_code)

def test_root_help_does_not_create_shell(self) -> None:
    with self.assertRaises(SystemExit) as captured:
        main(["--help"], shell_factory=forbidden_shell)
    self.assertEqual(0, captured.exception.code)
```

- [ ] **Step 2: 运行 RED**

Run: `$env:PYTHONPATH='src'; python -B -m unittest tests.test_cli -v`

Expected: FAIL，当前解析器要求子命令。

- [ ] **Step 3: 重构公共参数并增加 chat**

根子解析器不再 `required=True`。`args.command is None` 与 `args.command == "chat"` 走相同 `_run_chat()`。`chat` 与 `run` 共用配置选项，只有 `run` 拥有 `task` 位置参数。

- [ ] **Step 4: 装配 SessionStore 与初始 Session**

数据库路径来自 `default_sessions_db(environ)`。交互入口以当前目录或显式 `--workspace` 为起点，恢复该工作区最近更新的 Session；没有匹配项时创建名称 `default` 的 Session。显式 `--provider` / `--model` 通过配置验证后覆盖恢复值；新 Session 未显式指定 Provider 时使用 `openai`。数据库初始化失败返回退出码 `2`，不能自动跳转到其他工作区。

- [ ] **Step 5: 确认现有 run 测试和退出码不回归**

Run: `$env:PYTHONPATH='src'; python -B -m unittest tests.test_cli tests.test_agent -v`

Expected: PASS。

### Task 7: README、集成验证与安全自检

**Files:**
- Modify: `README.md`
- Modify: `.gitignore` only if new generated paths require it
- Test: all `tests/test_*.py`

**Interfaces:**
- Documents: 交互入口、全部斜杠命令、Session 数据位置、混合记忆和安全限制

- [ ] **Step 1: 更新 README**

加入可复制示例：

```powershell
tricoder
tricoder chat --provider deepseek --workspace D:\path\to\project
```

记录 `/session` 选择、跨工作区确认、`/clear` 语义、数据库默认位置和不持久化内容。

- [ ] **Step 2: 运行完整测试**

Run: `$env:PYTHONPATH='src'; python -B -m unittest discover -s tests -v`

Expected: 全部测试通过；Windows 无符号链接权限时只允许既有链接测试跳过。

- [ ] **Step 3: 运行语法检查**

Run: `$env:PYTHONPATH='src'; python -B -m compileall -q src tests`

Expected: exit code `0`。

- [ ] **Step 4: 验证 CLI 帮助**

Run:

```powershell
$env:PYTHONPATH='src'
python -B -m tricoder --help
python -B -m tricoder chat --help
python -B -m tricoder run --help
```

Expected: root 帮助包含 `chat`、`doctor`、`run`；chat 帮助包含 Provider、workspace、audit、context、read-only 选项。

- [ ] **Step 5: 运行限定范围安全扫描**

只扫描 `src/`、`tests/`、`docs/`、`README.md`、`pyproject.toml`、`.env.example` 和 `.gitignore`。明确排除 `.env.local`、`.superpowers/`、`runtime/` 和缓存；只输出文件数和匹配数量，不输出疑似值。

- [ ] **Step 6: 最终自审**

逐项核对设计文档：入口兼容、本地命令、Session 独立性、混合记忆、跨工作区原子切换、SQLite 错误、无 Key/源码持久化和非目标边界。
