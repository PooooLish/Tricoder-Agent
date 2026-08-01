# Tricoder CLI Change Management Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 TriCoder 增加任务级内存变更账本、Provider `apply_patch` 工具，以及本地 `/diff`、`/undo`，形成安全的修改—检查—撤销闭环。

**Architecture:** `changes.py` 保存每个 Session 最近一条非空任务的前后快照，`patches.py` 纯解析并在内存应用受限 unified diff，`ToolRegistry` 继续负责全部安全文件系统提交和补偿。`SessionRuntime` 管理任务事务与结构化状态恢复，Shell/UI 只在本地分发 `/diff`、`/undo`，不把源码快照写入 SQLite、JSONL 或 Provider。

**Tech Stack:** Python 3.11+、标准库 `dataclasses`/`difflib`/`re`、现有 `unittest`、Rich、SQLite Session 元数据、GitHub Actions Windows/Linux × Python 3.11/3.12。

## Global Constraints

- `/undo` 撤销当前 Session 最近一条非空用户任务产生的全部文件修改。
- `/diff` 展示与 `/undo` 相同的最近任务变更集。
- 外部内容、mode、身份、删除或替换冲突导致整次 `/undo` 零写入拒绝。
- `apply_patch` 只支持修改已有 UTF-8 文本文件和创建新文件；拒绝删除、重命名、复制、二进制和越界路径。
- `/undo` 可以删除上一条任务创建的新文件，但必须显示完整反向 diff 并要求 `y`/`yes`。
- 每个 Session 只保留最近一条非空变更集；`/clear` 和模型切换保留，进程退出后消失。
- before/after 源码快照不得进入 SQLite、JSONL、SessionMemory、日志、异常公共文本或 Provider 消息。
- 单个任务 before + after 净快照预算固定为 `2_000_000` 个字符；预计超限的写入必须在审批前拒绝。
- `--read-only` 禁止 `apply_patch` 和 `/undo`，允许 `/diff`。
- 保留 `edit_file`、`create_file` 与 `ToolResult.relative_path` 的向后兼容行为。
- 所有路径使用 `WorkspacePolicy` 产生的规范相对路径；不依赖或修改 Git index、stash、commit。
- 不新增第三方运行时依赖，不读取或提交 `.env.local`，不执行真实 Provider API 冒烟测试。
- 代码和测试使用准确、易懂的中文注释；不做无关重构。

---

## File Map

- Create: `src/tricoder/changes.py` — 内存快照、任务变更集、容量预算和 Session 账本。
- Create: `src/tricoder/patches.py` — 受限 unified diff 的纯解析与文本应用。
- Create: `tests/test_changes.py` — 账本聚合、预算和生命周期测试。
- Create: `tests/test_patches.py` — 补丁语法、hunk 与禁止能力测试。
- Modify: `src/tricoder/models.py` — 为多文件工具结果增加 `modified_paths`。
- Modify: `src/tricoder/tools.py` — 接入账本、注册 `apply_patch`、安全提交、撤销和补偿。
- Modify: `src/tricoder/agent.py` — 合并单数与复数修改路径。
- Modify: `src/tricoder/session_runtime.py` — 每 Session 账本、任务事务、diff/undo 和安全状态恢复。
- Modify: `src/tricoder/commands.py` — 解析 `/diff`、`/undo`。
- Modify: `src/tricoder/shell.py` — 本地命令分发、撤销审批。
- Modify: `src/tricoder/ui.py` — 字面渲染 diff、更新帮助。
- Modify: `README.md` — 用户命令、工具能力和内存边界。
- Modify: `tests/test_models.py`, `tests/test_agent.py`, `tests/test_tools.py`, `tests/test_commands.py`, `tests/test_session_runtime.py`, `tests/test_session_integration.py`, `tests/test_shell.py`, `tests/test_ui.py`, `tests/test_audit.py` — 分层行为与隐私回归。

### Task 1: In-memory task change journal

**Files:**
- Create: `src/tricoder/changes.py`
- Create: `tests/test_changes.py`

**Interfaces:**
- Consumes: 规范工作区相对路径字符串和文件安全层提供的 `device`/`inode`。
- Produces:

```python
MAX_TASK_CHANGE_CHARS = 2_000_000

@dataclass(frozen=True, slots=True)
class FileIdentity:
    device: int
    inode: int

@dataclass(frozen=True, slots=True)
class FileSnapshot:
    path: str
    content: str
    mode: int
    identity: FileIdentity

@dataclass(frozen=True, slots=True)
class FileChange:
    path: str
    before: FileSnapshot | None
    after: FileSnapshot | None

@dataclass(frozen=True, slots=True)
class TaskChangeSet:
    changes: tuple[FileChange, ...]
    before_modified_files: tuple[str, ...]
    before_verification: str
    after_modified_files: tuple[str, ...]
    after_verification: str

class ChangeBudgetError(ValueError): ...
class ChangeJournalError(RuntimeError): ...

class ChangeJournal:
    def __init__(self, max_chars: int = MAX_TASK_CHANGE_CHARS) -> None: ...
    def begin_task(self, modified_files: tuple[str, ...], verification: str) -> None: ...
    def reserve(self, proposed: tuple[FileChange, ...]) -> None: ...
    def record_committed(
        self,
        path: str,
        before: FileSnapshot | None,
        after: FileSnapshot | None,
    ) -> None: ...
    def seal_task(self, modified_files: tuple[str, ...], verification: str) -> TaskChangeSet | None: ...
    def latest(self) -> TaskChangeSet | None: ...
    def clear_latest(self) -> None: ...
```

- [ ] **Step 1: 写失败测试，定义快照聚合语义**

Create `tests/test_changes.py` with focused tests using literal snapshots:

```python
def snapshot(path: str, content: str, inode: int) -> FileSnapshot:
    return FileSnapshot(path, content, 0o644, FileIdentity(1, inode))

class ChangeJournalTests(unittest.TestCase):
    def test_same_file_keeps_first_before_and_last_after(self) -> None:
        journal = ChangeJournal()
        journal.begin_task(("old.py",), "passed")
        first = snapshot("app.py", "value = 1\n", 10)
        middle = snapshot("app.py", "value = 2\n", 11)
        final = snapshot("app.py", "value = 3\n", 12)

        journal.record_committed("app.py", first, middle)
        journal.record_committed("app.py", middle, final)
        result = journal.seal_task(("old.py", "app.py"), "not-run")

        self.assertEqual((FileChange("app.py", first, final),), result.changes)
        self.assertEqual(("old.py",), result.before_modified_files)
        self.assertEqual("passed", result.before_verification)

    def test_net_zero_task_keeps_previous_non_empty_change_set(self) -> None:
        journal = ChangeJournal()
        before = snapshot("app.py", "a\n", 10)
        after = snapshot("app.py", "b\n", 11)
        journal.begin_task((), "not-run")
        journal.record_committed("app.py", before, after)
        previous = journal.seal_task(("app.py",), "not-run")
        journal.begin_task(("app.py",), "not-run")
        journal.record_committed("app.py", after, before)
        journal.record_committed("app.py", before, after)

        self.assertIsNone(journal.seal_task(("app.py",), "not-run"))
        self.assertEqual(previous, journal.latest())
```

Also add exact cases for create (`before=None`), compensation back to missing (`after=None` removes net change), calling `record_committed` without `begin_task`, nested `begin_task`, and `clear_latest`.

- [ ] **Step 2: 运行测试确认 RED**

Run:

```powershell
python -m unittest tests.test_changes -v
```

Expected: import fails because `tricoder.changes` does not exist.

- [ ] **Step 3: 实现不可变模型和净变化聚合**

Implement the exact interfaces above. Use an insertion-ordered `dict[str, FileChange]`; on repeated paths retain the first `before` and latest `after`. Remove an entry when both states are absent or when both snapshots have equal `content` and `mode`; identity is a point-in-time conflict guard, not part of net-change equality. Reject mismatched `path` values and invalid lifecycle calls with stable Chinese `ChangeJournalError` messages.

- [ ] **Step 4: 写预算失败测试并确认 RED**

Add:

```python
def test_reserve_counts_projected_net_before_and_after_characters(self) -> None:
    journal = ChangeJournal(max_chars=5)
    journal.begin_task((), "not-run")
    proposed = (FileChange("a.py", None, snapshot("a.py", "123456", 1)),)

    with self.assertRaisesRegex(ChangeBudgetError, "2,000,000|预算"):
        journal.reserve(proposed)

    self.assertIsNone(journal.seal_task((), "not-run"))
```

Use a constructor `ChangeJournal(max_chars: int = MAX_TASK_CHANGE_CHARS)` so tests can exercise a small literal limit. Add a boundary case where the projected total equals the limit and a repeated path replaces rather than double-counts its earlier after snapshot.

Run: `python -m unittest tests.test_changes -v`

Expected: FAIL because constructor and `reserve` do not yet enforce the budget.

- [ ] **Step 5: 实现预算并运行 GREEN**

Count `len(before.content)` plus `len(after.content)` for the projected net map, omitting missing sides. `reserve()` must not mutate the active transaction.

Run:

```powershell
python -m unittest tests.test_changes -v
python -m compileall -q src/tricoder/changes.py tests/test_changes.py
```

Expected: all change journal tests pass; compile exits `0`.

- [ ] **Step 6: 提交 Task 1**

```powershell
git add src/tricoder/changes.py tests/test_changes.py
git commit -m "feat: add task-scoped change journal"
```

### Task 2: Restricted unified diff parser

**Files:**
- Create: `src/tricoder/patches.py`
- Create: `tests/test_patches.py`

**Interfaces:**
- Consumes: non-empty unified diff string and original UTF-8 text.
- Produces:

```python
@dataclass(frozen=True, slots=True)
class PatchHunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class FilePatch:
    path: str
    create: bool
    hunks: tuple[PatchHunk, ...]

class PatchError(ValueError): ...

def parse_unified_diff(source: str) -> tuple[FilePatch, ...]: ...
def apply_file_patch(original: str, patch: FilePatch) -> str: ...
```

- [ ] **Step 1: 写修改与创建文件的失败测试**

Create `tests/test_patches.py`:

```python
UPDATE_PATCH = """--- a/app.py
+++ b/app.py
@@ -1,2 +1,2 @@
 value = 1
-enabled = False
+enabled = True
"""

CREATE_PATCH = """--- /dev/null
+++ b/new.py
@@ -0,0 +1,2 @@
+value = 1
+print(value)
"""

class PatchParserTests(unittest.TestCase):
    def test_parses_and_applies_exact_update_hunk(self) -> None:
        patch = parse_unified_diff(UPDATE_PATCH)[0]
        self.assertEqual("app.py", patch.path)
        self.assertFalse(patch.create)
        self.assertEqual(
            "value = 1\nenabled = True\n",
            apply_file_patch("value = 1\nenabled = False\n", patch),
        )

    def test_parses_new_file_from_dev_null(self) -> None:
        patch = parse_unified_diff(CREATE_PATCH)[0]
        self.assertTrue(patch.create)
        self.assertEqual("value = 1\nprint(value)\n", apply_file_patch("", patch))
```

- [ ] **Step 2: 运行测试确认 RED**

Run: `python -m unittest tests.test_patches -v`

Expected: import fails because `tricoder.patches` does not exist.

- [ ] **Step 3: 实现最小解析器和内存应用器**

Use `splitlines(keepends=True)` and a compiled hunk header regex:

```python
_HUNK_HEADER = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?\n?$"
)
```

Rules:

- Require `---` immediately followed by `+++`; strip exactly one leading `a/` from old paths and `b/` from new paths.
- `/dev/null` is legal only on the old side and means create.
- Existing-file old and new normalized paths must match.
- Hunk body lines must start with one of space, `+`, `-`; `\\ No newline at end of file` applies to the immediately preceding body line and removes its terminal newline.
- Validate declared old/new counts, strictly increasing non-overlapping old ranges, exact context/removal text, and complete consumption of every hunk body. Preserve untouched source regions before, between, and after hunks.
- Reject empty input and a file header with no hunks.

- [ ] **Step 4: 写禁止能力与畸形输入的失败测试**

Add table-driven literal cases asserting `PatchError` for:

```python
INVALID_PATCHES = {
    "delete": "--- a/app.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-value = 1\n",
    "rename": "--- a/old.py\n+++ b/new.py\n@@ -1 +1 @@\n-old\n+new\n",
    "binary": "Binary files a/a.png and b/a.png differ\n",
    "overlap": "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-a\n+b\n@@ -1 +1 @@\n-b\n+c\n",
    "bad_count": "--- a/a.py\n+++ b/a.py\n@@ -1,2 +1 @@\n-a\n+b\n",
    "duplicate": UPDATE_PATCH + UPDATE_PATCH,
}
```

Also assert context mismatch in `apply_file_patch` raises a stable Chinese `PatchError` without embedding the source line content.

Run: `python -m unittest tests.test_patches -v`

Expected: new invalid cases fail until every grammar rule is implemented.

- [ ] **Step 5: 完成规则并运行 GREEN**

Run:

```powershell
python -m unittest tests.test_patches -v
python -m compileall -q src/tricoder/patches.py tests/test_patches.py
```

Expected: parser tests pass; no source or patch content appears in error strings.

- [ ] **Step 6: 提交 Task 2**

```powershell
git add src/tricoder/patches.py tests/test_patches.py
git commit -m "feat: parse restricted unified patches"
```

### Task 3: Existing write tools and multi-path results

**Files:**
- Modify: `src/tricoder/models.py`
- Modify: `src/tricoder/tools.py`
- Modify: `src/tricoder/agent.py`
- Modify: `tests/test_models.py`
- Modify: `tests/test_tools.py`
- Modify: `tests/test_agent.py`

**Interfaces:**
- Consumes: `ChangeJournal`, `FileSnapshot`, `FileIdentity` from Task 1.
- Produces:

```python
@dataclass(frozen=True, slots=True)
class ToolResult:
    ok: bool
    output: str
    relative_path: str | None = None
    modified_paths: tuple[str, ...] = ()

@dataclass(slots=True)
class ToolContext:
    # existing fields unchanged
    change_journal: ChangeJournal | None = None
```

`ToolRegistry` gains private snapshot and journal helpers; Agent consumes the ordered union of `relative_path` and `modified_paths`.

- [ ] **Step 1: 写多路径结果的失败测试**

Add to `tests/test_models.py`:

```python
def test_tool_result_keeps_legacy_and_multi_file_paths(self) -> None:
    result = ToolResult(True, "ok", "legacy.py", ("a.py", "b.py"))
    self.assertEqual("legacy.py", result.relative_path)
    self.assertEqual(("a.py", "b.py"), result.modified_paths)
```

Add an Agent test with a fake tool returning `ToolResult(True, "ok", None, ("a.py", "b.py"))`; assert both paths appear once in `RunResult.modified_files` and verification becomes pending.

Run:

```powershell
python -m unittest tests.test_models.StructuredModelTests.test_tool_result_keeps_legacy_and_multi_file_paths -v
python -m unittest tests.test_agent -v
```

Expected: construction or Agent assertion fails because `modified_paths` is absent or ignored.

- [ ] **Step 2: 实现向后兼容多路径结果**

Append `modified_paths` after `relative_path`. In Agent, replace the single-path branch with:

```python
changed_paths = tuple(
    dict.fromkeys(
        ([result.relative_path] if result.relative_path is not None else [])
        + list(result.modified_paths)
    )
)
for changed_path in changed_paths:
    if changed_path not in modified_files:
        modified_files.append(changed_path)
if result.ok and changed_paths:
    verification = "待验证"
```

Preserve current handling for failed results and legacy tools.

- [ ] **Step 3: 写现有编辑/创建工具记录账本的失败测试**

In `tests/test_tools.py`, construct and begin a journal before calls:

```python
journal = ChangeJournal()
journal.begin_task((), "not-run")
registry = ToolRegistry(
    ToolContext(
        workspace_policy=WorkspacePolicy(self.workspace),
        command_policy=CommandPolicy(),
        approver=RecordingApprover([True]),
        change_journal=journal,
    )
)
result = registry.execute(
    "edit_file",
    {"path": "src/app.py", "old_text": "return 41", "new_text": "return 42"},
)
change_set = journal.seal_task(("src/app.py",), "not-run")

self.assertTrue(result.ok)
self.assertEqual("def answer():\n    return 41\n", change_set.changes[0].before.content)
self.assertEqual("def answer():\n    return 42\n", change_set.changes[0].after.content)
```

Add create-file assertions for `before is None`, and a small-budget case proving the tool rejects before requesting approval and leaves the file untouched.

- [ ] **Step 4: 运行账本接入测试确认 RED**

Run selected new tests from `tests.test_tools.ToolTests`.

Expected: `ToolContext` rejects `change_journal` or no change is recorded.

- [ ] **Step 5: 实现快照、预留和提交后记录**

- Convert existing `_FileIdentity` uses to Task 1's public `FileIdentity` without changing Windows/POSIX comparison semantics.
- `_snapshot(binding, name, relative)` returns UTF-8 content, normalized mode and identity.
- `edit_file` reserves `FileChange(before, projected_after)` before approval, and calls `record_committed` after `binding.replace` succeeds, including committed-with-close-warning paths.
- `create_file` reserves `FileChange(None, projected_after)` before approval. After the hard-link commit, read the published target for its real identity and call `record_committed` even when temporary cleanup or binding close later warns.
- When `change_journal is None`, preserve existing behavior without imposing the 2,000,000-character journal limit on non-session callers.

- [ ] **Step 6: 运行 Task 3 测试并提交**

Run:

```powershell
python -m unittest tests.test_models tests.test_agent tests.test_tools -v
python -m compileall -q src tests
git diff --check
```

Expected: all selected suites pass, including Windows cleanup-warning fixtures.

Commit:

```powershell
git add src/tricoder/models.py src/tricoder/tools.py src/tricoder/agent.py tests/test_models.py tests/test_tools.py tests/test_agent.py
git commit -m "feat: track committed tool file changes"
```

### Task 4: Multi-file `apply_patch` tool

**Files:**
- Modify: `src/tricoder/tools.py`
- Modify: `tests/test_tools.py`
- Modify: `tests/test_audit.py`

**Interfaces:**
- Consumes: `parse_unified_diff`, `apply_file_patch`, `FilePatch`, `PatchError`; active `ChangeJournal`.
- Produces: Provider tool `apply_patch` with schema `{patch: string}` and ordered `ToolResult.modified_paths`. Undo types and methods are introduced together in Task 5, so this task leaves no public stub that raises `NotImplementedError`.

- [ ] **Step 1: 写工具定义和成功多文件补丁的失败测试**

Update expected tool order to include `apply_patch` immediately after `create_file`. Assert its exact schema:

```python
"apply_patch": ({"patch": {"type": "string"}}, ["patch"])
```

Add a test patching `src/app.py` and creating `src/new.py`; begin a journal and approve once. Assert:

```python
self.assertTrue(result.ok)
self.assertEqual(("src/app.py", "src/new.py"), result.modified_paths)
self.assertEqual(1, len(self.approver.requests))
self.assertEqual("apply_patch", self.approver.requests[0][0])
self.assertIn("--- src/app.py", self.approver.requests[0][1])
self.assertEqual("def answer():\n    return 42\n", app.read_text(encoding="utf-8"))
self.assertEqual("created = True\n", new.read_text(encoding="utf-8"))
```

- [ ] **Step 2: 运行测试确认 RED**

Run selected definition/schema/success tests.

Expected: unknown tool or missing definition.

- [ ] **Step 3: 实现全量预检、一次审批和提交**

Implementation order must be exact:

1. Parse every file patch.
2. Resolve paths and reject sensitive/outside/non-regular targets.
3. Open bindings for sorted unique parent paths and hold them through approval/commit.
4. Snapshot each existing target and compute all after text in memory.
5. Create projected `FileChange` values and call one `journal.reserve()`.
6. Render the complete combined diff and request one approval.
7. Re-read and compare every before snapshot after approval; reject with zero writes on any mismatch.
8. Commit in normalized path order; after every commit read the real after snapshot and record it.
9. Return all committed paths in `modified_paths`.

Do not expose patch content in `ToolResult.output`; use counts and normalized paths only.

- [ ] **Step 4: 写预检、TOCTOU 和只读失败测试**

Add exact cases:

- malformed second file means no approval and no first-file change;
- sensitive or `..` path means no approval;
- new target exists means no approval;
- journal budget overflow means no approval;
- approver mutates one target before returning `True`, causing zero writes;
- `read_only=True` rejects before parsing/approval;
- Provider arguments omit `patch`, use non-string value, or include extra keys.

Run selected tests; expected failures until checks are ordered before writes.

- [ ] **Step 5: 写提交失败补偿测试并确认 RED**

Use a binding proxy that delegates real operations but raises on the second target's publish. Assert the first target is restored and the journal seals to no net change. Add a second proxy where rollback of the first target also fails; assert output names only normalized affected paths, `result.ok` is false, and the journal retains the actual first-file state.

- [ ] **Step 6: 实现补偿和结构化审计边界**

- Before compensating a committed file, verify its current identity/content/mode still equals the just-recorded after snapshot; otherwise mark compensation failure and never overwrite it.
- Restore an existing file by atomic replace from its before snapshot; remove a newly created target only after bound identity verification.
- Feed compensation commits back through `record_committed` so a complete rollback removes net changes and an incomplete rollback preserves actual state.
- Agent audit remains handled by existing tool events. Ensure redaction converts `patch` to `patch_chars` and never stores the patch string; add `tests/test_audit.py` assertion using a unique sentinel absent from serialized JSONL.

- [ ] **Step 7: 运行 Task 4 测试并提交**

Run:

```powershell
python -m unittest tests.test_patches tests.test_tools tests.test_agent tests.test_audit -v
python -m compileall -q src tests
git diff --check
```

Commit:

```powershell
git add src/tricoder/tools.py tests/test_tools.py tests/test_audit.py
git commit -m "feat: apply multi-file unified patches"
```

### Task 5: Session-scoped diff and safe undo runtime

**Files:**
- Modify: `src/tricoder/changes.py`
- Modify: `src/tricoder/tools.py`
- Modify: `src/tricoder/session_runtime.py`
- Modify: `tests/test_session_runtime.py`
- Modify: `tests/test_session_integration.py`
- Modify: `tests/test_tools.py`

**Interfaces:**
- Consumes: `ChangeJournal` and `TaskChangeSet`.
- Produces:

```python
@dataclass(frozen=True, slots=True)
class UndoPreview:
    diff: str
    paths: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class UndoExecution:
    ok: bool
    paths: tuple[str, ...]
    conflicts: tuple[str, ...] = ()
    compensation_failed: tuple[str, ...] = ()

@dataclass(frozen=True, slots=True)
class ActiveSession:
    record: SessionRecord
    memory: SessionMemory
    context: SessionContext
    config: AppConfig
    agent: ContextAgent
    tools: ToolRegistry | None = None
    journal: ChangeJournal = field(default_factory=ChangeJournal)
    audit: AuditLogger | None = None

class SessionRuntime:
    def diff_latest(self) -> str | None: ...
    def prepare_undo(self) -> UndoPreview: ...
    def undo_latest(self) -> UndoExecution: ...

class ToolRegistry:
    def preview_undo(self, change_set: TaskChangeSet) -> UndoPreview: ...
    def undo_change_set(self, change_set: TaskChangeSet) -> UndoExecution: ...
```

Defaults preserve test factories that construct `ActiveSession` with the original five positional values.

- [ ] **Step 1: 写 Session 任务事务和隔离失败测试**

Use an injected fake `ContextAgent` whose `run_with_context` records a committed change into `runtime.current.journal` before returning. Assert:

- `run_task()` begins and seals exactly one transaction;
- a failed `RunResult` with an actual committed write still produces `diff_latest()`;
- a later no-write task does not replace the previous non-empty diff;
- Session A and B retain separate latest diffs across A→B→A;
- `clear_current()` and successful `change_model()` preserve the journal;
- constructing a new runtime from the same SQLite database has `diff_latest() is None`.

Run selected tests; expected failures because ActiveSession has no journal and Runtime methods are absent.

- [ ] **Step 2: 装配每 Session journal/tools/audit 并封存任务**

- In `_build_active`, create or accept a journal before `ToolRegistry`; pass it through `ToolContext.change_journal` and store tools/journal/audit on `ActiveSession`.
- Model rebuild passes the current journal into the replacement ActiveSession.
- New and restored-on-process-start sessions receive an empty journal.
- Wrap `run_with_context` in `begin_task` plus `try`/`except`: when a turn returns, seal with `turn.context` state. If an exception escapes after writes, seal with the pre-task structured metadata available to Runtime and re-raise the original exception unchanged; do not broaden exception wrapping or discard an actual journaled write.
- `diff_latest()` renders the journal's before→after snapshots with `difflib.unified_diff`, sorted by path, and returns `None` when empty.

- [ ] **Step 3: 写 undo 冲突和成功路径失败测试**

Create real workspace integration fixtures for:

- one modified file restored with original mode;
- one task-created file deleted;
- mixed update/create undone together;
- content changed externally after task → conflicts tuple and zero writes;
- same content but replaced identity → conflict;
- mode changed externally → conflict;
- `read_only` → stable runtime error before preview/approval;
- successful undo restores `SessionContext` and `SessionMemory` modified files/verification to `TaskChangeSet.before_*`, clears latest, and persists safe metadata.

- [ ] **Step 4: 实现两阶段 undo 与补偿**

`ToolRegistry.preview_undo(change_set)` performs the first full after-snapshot validation and returns a reverse diff. `undo_change_set(change_set)` repeats the same full validation before any write, then restores in normalized path order.

On failure after partial undo:

- verify each already-restored file still equals its before state;
- compensate it back to after;
- report `compensation_failed` without raw content;
- keep journal latest unless every undo operation succeeds.

`SessionRuntime.undo_latest()` rejects when no latest change set, no tools, or read-only. On full success it clears the journal, replaces context/memory with before structured state, writes an audit event containing only event/status/paths/file_count/conflict_count/compensation status, caches current, marks memory dirty, and calls `persist_current()`.

- [ ] **Step 5: 写审计和持久化失败测试**

Assert undo JSONL excludes unique before/after sentinels and reverse diff. Simulate `save_memory` failure after successful workspace undo; assert files remain restored, journal is cleared, `status().warning == "本次记忆未持久化"`, and `retry_persist()` can later succeed.

- [ ] **Step 6: 运行 Task 5 测试并提交**

Run:

```powershell
python -m unittest tests.test_changes tests.test_tools tests.test_session_runtime tests.test_session_integration tests.test_audit -v
python -m compileall -q src tests
git diff --check
```

Commit:

```powershell
git add src/tricoder/changes.py src/tricoder/tools.py src/tricoder/session_runtime.py tests/test_tools.py tests/test_session_runtime.py tests/test_session_integration.py tests/test_audit.py
git commit -m "feat: add session diff and safe undo runtime"
```

### Task 6: Local slash commands and terminal UI

**Files:**
- Modify: `src/tricoder/commands.py`
- Modify: `src/tricoder/shell.py`
- Modify: `src/tricoder/ui.py`
- Modify: `tests/test_commands.py`
- Modify: `tests/test_shell.py`
- Modify: `tests/test_ui.py`

**Interfaces:**
- Consumes: `SessionRuntime.diff_latest()`, `prepare_undo()`, `undo_latest()`.
- Produces: local no-argument `/diff` and `/undo`; `ShellUI.show_diff(diff: str, *, title: str) -> None`.

- [ ] **Step 1: 写命令解析失败测试**

Add `diff` and `undo` to the simple-command table. Assert case-insensitive `/DIFF`, `/undo`, and rejection of `/diff extra` and `/undo now`.

Run: `python -m unittest tests.test_commands -v`

Expected: unknown command failures.

- [ ] **Step 2: 实现解析并写 Shell 本地分发失败测试**

Extend test doubles with counters and literal previews. Assert:

```python
shell.execute("/diff")
self.assertEqual(0, runtime.run_task_calls)
self.assertEqual([("最近任务变更", EXPECTED_DIFF)], ui.diffs)

shell.execute("/undo")
self.assertEqual(1, runtime.prepare_undo_calls)
self.assertEqual(1, ui.confirm_calls)
self.assertEqual(1, runtime.undo_latest_calls)
self.assertEqual(0, runtime.run_task_calls)
```

Also cover no diff, no undo, user rejection, preview conflict, execution conflict after approval, read-only error, and successful notice.

- [ ] **Step 3: 实现 Shell 协议和本地流程**

Add RuntimeLike methods with exact Task 5 signatures. Dispatch order:

```python
elif command.name == "diff":
    self._show_diff()
elif command.name == "undo":
    self._undo_latest()
```

`_undo_latest()` calls `prepare_undo()`, displays title `撤销预览`, asks `撤销最近一条任务的全部文件修改？[y/N] `, and calls `undo_latest()` only after confirmation. Runtime performs the second validation.

- [ ] **Step 4: 写 UI 字面渲染和帮助失败测试**

Use a diff containing `[bold red]not markup[/bold red]`. Render into a Rich `Console(record=True, markup=True)`, call `show_diff`, and assert exported text contains the literal brackets. Assert help output contains `/diff` and `/undo` with their task-scoped descriptions.

- [ ] **Step 5: 实现 UI 并运行 GREEN**

Use Rich `Syntax(diff, "diff", theme="ansi_dark", word_wrap=False)` or an equivalent literal-safe renderable; never pass model/file diff content as Rich markup. Keep existing dynamic-output safety conventions.

Run:

```powershell
python -m unittest tests.test_commands tests.test_shell tests.test_ui -v
python -m compileall -q src tests
git diff --check
```

- [ ] **Step 6: 提交 Task 6**

```powershell
git add src/tricoder/commands.py src/tricoder/shell.py src/tricoder/ui.py tests/test_commands.py tests/test_shell.py tests/test_ui.py
git commit -m "feat: add local diff and undo commands"
```

### Task 7: Documentation, privacy regression, and full verification

**Files:**
- Modify: `README.md`
- Modify: any existing test file only when a final cross-module acceptance assertion is missing; do not add new behavior in this task.

**Interfaces:**
- Consumes: all Tasks 1–6 public behavior.
- Produces: accurate user documentation and verified release-ready branch; no tag or Release.

- [ ] **Step 1: 更新 README 的能力、命令和边界**

Make these exact documentation changes:

- Add `apply_patch` to core highlights as a restricted multi-file unified diff tool.
- Add `/diff` and `/undo` rows to the command table.
- Explain `/diff` and `/undo` refer to the current Session's latest non-empty task.
- State `/undo` refuses all writes on external conflict and may delete files created by that task after approval.
- State change snapshots are memory-only, limited to 2,000,000 characters, and disappear on process restart.
- Add unsupported items: file deletion/rename patches, `/redo`, multi-level/history-select undo, persistent undo.
- Keep real API smoke tests local and optional; do not claim they were run for this feature.

- [ ] **Step 2: 运行聚焦隐私扫描**

Run tests that assert SQLite/JSONL omit source sentinels, then run the repository's filename-only tracked credential scan with its explicit allowlist from the previous plan. Expected: zero unexpected filenames and zero tracked `.env`, `.env.local`, `*.key`, `*.pem`.

- [ ] **Step 3: 运行完整本地验收**

Run:

```powershell
python -m unittest discover -s tests -v
python -m compileall -q src tests
git diff --check
git status --short --branch
```

Expected: all tests pass, with only the existing Windows symlink permission skip when the account cannot create symlinks; compile and diff checks exit `0`.

- [ ] **Step 4: 自审功能范围**

Inspect the final diff and confirm:

- no provider key, patch text, source snapshot or reverse diff is persisted;
- `apply_patch` is present in stable tool definitions and Provider request serialization tests still pass;
- `/diff` and `/undo` are local-only;
- no Git mutation, file delete patch, redo, persistent history or third-party dependency was introduced;
- all new public errors are stable Chinese text without raw low-level exception details.

- [ ] **Step 5: 提交文档与最终测试调整**

```powershell
git add README.md
git commit -m "docs: explain task-scoped change management"
```

- [ ] **Step 6: 推送并验证 GitHub Actions**

After all task reviews and the final whole-branch review are clean:

```powershell
git push origin main
$changeHead = git rev-parse HEAD
$changeCiRun = gh run list --workflow ci.yml --commit $changeHead --limit 1 --json databaseId,headSha | ConvertFrom-Json | Select-Object -First 1
if (-not $changeCiRun -or $changeCiRun.headSha -ne $changeHead) { throw 'No CI run found for the pushed commit.' }
$changeCiRunId = $changeCiRun.databaseId
if (-not $changeCiRunId) { throw 'No CI run found for the pushed commit.' }
gh run watch $changeCiRunId --exit-status
```

Expected: run head SHA equals pushed HEAD; Ubuntu/Windows × Python 3.11/3.12 all succeed. Do not create a tag, Release, PR, or PyPI artifact.
