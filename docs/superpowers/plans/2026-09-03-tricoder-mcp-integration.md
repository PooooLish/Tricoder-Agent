# TriCoder MCP Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 TriCoder 增加默认关闭、逐任务启停、通过现有 Tool Gateway 审批和审计的本地 MCP stdio 工具能力，并从一次性 CLI 与 SessionRuntime 完整调用。

**Architecture:** 每个 Coding Task 创建临时 ToolRegistry、MCPManager 和 ExtensionHost，在同一个异步事件循环中启动已批准的 server、注册危险工具、运行 CodingAgent，并在 `finally` 中反向关闭。第三方 SDK 只存在于 `tricoder.mcp` 包内；配置未启用 MCP 时不导入 SDK，也不改变现有 Agent 路径。

**Tech Stack:** Python 3.11+、标准库 asyncio/AsyncExitStack、TriCoder ToolRegistry/ExtensionHost/CancellationToken、官方 Python MCP SDK `mcp==2.1.1`、unittest、仓库内 fake stdio server。

**Spec:** `docs/superpowers/specs/2026-09-03-tricoder-mcp-integration-design.md`

## Global Constraints

- 只支持本地 `stdio`；不实现 HTTP/SSE、OAuth、resources、prompts 或后台常驻连接。
- MCP SDK 精确固定为 `mcp==2.1.1`，不安装 `[cli]` extra，不顺带升级 Rich/Textual。
- 依赖安装、manifest/lock 变更、Git commit 和 push 均须在执行时取得明确人工批准。
- 不读取 `.env.local`、`.local/secrets/`、`.local/envs/`，不保存、打印或审计真实凭据值。
- MCP server 启动始终强制人工审批；所有 MCP 工具风险固定为 `dangerous`，不能被 server 降级。
- server 使用 `shell=False`、固定工作区 cwd、可信可执行文件和最小环境；不得动态下载安装 server。
- MCP 工具必须走 ToolRegistry 的同一参数验证、审批、审计和输出预算管线。
- 工具公开名为稳定的 `mcp__<server-id>__<tool-name>`，最长 64 字符；规范化冲突双方拒绝。
- 只支持设计文档定义的有界 JSON Schema 子集；不支持的 Schema 显式拒绝，不能跳过校验。
- 只有有界文本进入模型；图片、资源和二进制内容只生成不含 payload 的元数据占位符。
- 任务结束后不得遗留动态工具、异步 watcher、stdio task 或 server 子进程。
- 生成态和依赖解析报告放在 `runtime/`；源码、测试和文档只写当前项目。
- 当前工作树包含 Phase 0–4 的未提交改动；每步仅暂存明确路径，不使用 `git add -A`、`git add -u` 或 `git commit -am`。

## File Structure

### New runtime modules

- `src/tricoder/mcp/__init__.py`：只导出 TriCoder 内部 MCP 类型，不在包导入时加载第三方 SDK。
- `src/tricoder/mcp/sdk.py`：可选 SDK 导入边界与 `MCPDependencyError`。
- `src/tricoder/mcp/models.py`：内部不可变 tool/result/state 数据类，不暴露 SDK 类型。
- `src/tricoder/mcp/schema.py`：有界 Schema 检查和递归参数校验。
- `src/tricoder/mcp/security.py`：可执行文件、argv、cwd、最小环境和强制启动审批。
- `src/tricoder/mcp/client.py`：单个 stdio server 的会话、超时、取消和关闭。
- `src/tricoder/mcp/tool_adapter.py`：稳定名称、输出归一化和 `MCPToolHandler`。
- `src/tricoder/mcp/manager.py`：多个 server、ExtensionHost、失败隔离和调用路由。
- `src/tricoder/mcp/runtime.py`：一次 Coding Task 的统一异步作用域。

### New tests and fixture

- `tests/test_mcp_dependency_boundary.py`
- `tests/test_mcp_schema.py`
- `tests/test_mcp_security.py`
- `tests/test_mcp_client.py`
- `tests/test_mcp_manager.py`
- `tests/test_mcp_tool_adapter.py`
- `tests/test_mcp_runtime.py`
- `tests/test_mcp_integration.py`
- `tests/fixtures/fake_mcp_server.py`

### Existing files modified

- `src/tricoder/tools/handlers.py`：默认异步 handler 接口。
- `src/tricoder/tools/command.py`：受取消控制的原生异步命令入口。
- `src/tricoder/tools/__init__.py`：同步/异步共用的安全执行管线和递归 Schema 验证入口。
- `src/tricoder/session_runtime.py`：MCP 启用时创建任务级 registry/agent/scope。
- `src/tricoder/cli.py`：一次性 `run` 接入相同 task scope。
- `pyproject.toml`、`requirements.lock`：获得许可后固定依赖。
- `docs/open-source-assessment.md`、`docs/framework/mcp-integration.md`、`README.md`、`project.md`：证据、运维边界和进度。

---

### Task 1: Dependency gate and optional SDK boundary

**Files:**

- Create: `src/tricoder/mcp/__init__.py`
- Create: `src/tricoder/mcp/sdk.py`
- Create: `tests/test_mcp_dependency_boundary.py`
- Modify: `docs/open-source-assessment.md`
- Create: `docs/framework/mcp-integration.md`
- Modify after explicit approval: `pyproject.toml`
- Create after explicit approval: `requirements.lock`

**Interfaces:**

- Produces: `MCPDependencyError(RuntimeError)`
- Produces: `MCPSDK` with `client_session`, `stdio_server_parameters`, and `stdio_client`
- Produces: `load_mcp_sdk() -> MCPSDK`
- Consumers: Task 5 `MCPClient`; disabled paths must not call this function

- [ ] **Step 1: Record the dependency decision before changing the environment**

Update `docs/open-source-assessment.md` to replace the candidate range with the accepted exact pin and decision:

```markdown
| Selected version | `mcp==2.1.1`（不安装 `[cli]` extra） |
| Reuse mode | `reference`：Hcode 仅提供生命周期与测试场景参考，适配层独立实现 |
| License | MIT；保留项目 LICENSE/NOTICE 证据 |
| Current limitation | 首份 requirements.lock 记录 Windows/Python 3.11 解析结果，不宣称跨平台或全哈希可重现 |
| Runtime scope | 只启用 stdio client；HTTP/SSE/OAuth/resources/prompts 不进入 Phase 5 |
```

Create `docs/framework/mcp-integration.md` with these durable rules: task-scoped lifetime, forced start approval, dangerous tools, environment allowlist, bounded Schema/output, SDK containment, and rollback by disabling `[mcp]`.

- [ ] **Step 2: Write the failing optional-import tests**

Create `tests/test_mcp_dependency_boundary.py`:

```python
import builtins
import importlib
import unittest
from unittest.mock import patch

from tricoder.mcp.sdk import MCPDependencyError, load_mcp_sdk


class MCPDependencyBoundaryTests(unittest.TestCase):
    def test_missing_sdk_has_stable_safe_error(self) -> None:
        real_import = builtins.__import__

        def blocked(name, *args, **kwargs):  # type: ignore[no-untyped-def]
            if name == "mcp" or name.startswith("mcp."):
                raise ModuleNotFoundError("MCP-IMPORT-INTERNAL-SENTINEL")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=blocked):
            with self.assertRaisesRegex(MCPDependencyError, "未安装 MCP SDK") as caught:
                load_mcp_sdk()
        self.assertNotIn("SENTINEL", str(caught.exception))

    def test_importing_tricoder_mcp_does_not_eagerly_import_sdk(self) -> None:
        real_import = builtins.__import__

        def guard(name, *args, **kwargs):  # type: ignore[no-untyped-def]
            if name == "mcp" or name.startswith("mcp."):
                raise AssertionError("third-party SDK imported eagerly")
            return real_import(name, *args, **kwargs)

        package = importlib.import_module("tricoder.mcp")
        with patch("builtins.__import__", side_effect=guard):
            package = importlib.reload(package)
        self.assertIn("MCPDependencyError", package.__all__)
```

- [ ] **Step 3: Run the boundary test and verify RED**

Run:

```powershell
& .\.venv\Scripts\python.exe -m unittest tests.test_mcp_dependency_boundary -v
```

Expected: FAIL because `tricoder.mcp.sdk` does not exist.

- [ ] **Step 4: Implement the optional SDK loader**

Create `src/tricoder/mcp/sdk.py` around this exact internal shape:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class MCPDependencyError(RuntimeError):
    """启用 MCP 但官方 SDK 不可用。"""


@dataclass(frozen=True, slots=True)
class MCPSDK:
    client_session: type[Any]
    stdio_server_parameters: type[Any]
    stdio_client: Any


def load_mcp_sdk() -> MCPSDK:
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except (ImportError, ModuleNotFoundError) as exc:
        raise MCPDependencyError(
            "已启用 MCP，但未安装 MCP SDK；请安装项目锁定依赖"
        ) from exc
    return MCPSDK(ClientSession, StdioServerParameters, stdio_client)
```

`src/tricoder/mcp/__init__.py` may re-export only `MCPDependencyError`; it must not import `mcp` or call `load_mcp_sdk()`.

- [ ] **Step 5: Run the boundary test and existing disabled-path checks**

Run:

```powershell
& .\.venv\Scripts\python.exe -m unittest tests.test_mcp_dependency_boundary tests.test_config tests.test_cli -v
```

Expected: PASS while the SDK is still absent; `doctor` and non-MCP CLI imports remain usable.

- [ ] **Step 6: Stop at the dependency approval gate**

Report the exact intended mutations: add `"mcp==2.1.1"` to `pyproject.toml`, install it into project `.venv`, resolve a clean Windows/Python 3.11 closure under `runtime/mcp-lock-311`, and create `requirements.lock`. Do not run pip or edit these two dependency files until the user explicitly approves.

- [ ] **Step 7: After approval, resolve and verify the dependency**

Run only after approval:

```powershell
& .\.venv\Scripts\python.exe -m pip install "mcp==2.1.1"
& .\.venv\Scripts\python.exe -c "import importlib.metadata as m; print(m.version('mcp'))"
& .\.venv\Scripts\python.exe -m pip check
```

Expected: version output exactly `2.1.1`; `pip check` reports no broken requirements.

Add exactly this direct dependency to `pyproject.toml`:

```toml
dependencies = [
    "mcp==2.1.1",
    "rich>=15.0.0,<16",
    "textual>=8.0.0,<9",
]
```

Resolve the platform lock in a new, non-destructively retained runtime venv:

```powershell
& .\.venv\Scripts\python.exe -m venv runtime\mcp-lock-311
& .\runtime\mcp-lock-311\Scripts\python.exe -m pip install -e .
& .\runtime\mcp-lock-311\Scripts\python.exe -m pip freeze --local
& .\runtime\mcp-lock-311\Scripts\python.exe -m pip check
```

Use `apply_patch` to create `requirements.lock` from the exact normalized `name==version` rows printed by the clean venv. Exclude the editable local project row, comments containing absolute paths, and unrelated packages. Verify every direct/transitive requirement of `mcp`, Rich and Textual is represented. Record that this is the Windows/Python 3.11 resolution.

- [ ] **Step 8: Verify dependency metadata without exposing local state**

Run:

```powershell
& .\.venv\Scripts\python.exe -c "import importlib.metadata as m; d=m.metadata('mcp'); print(d['Name'], d['Version'], d['License-Expression'] or d['License'])"
& .\.venv\Scripts\python.exe -m unittest tests.test_mcp_dependency_boundary -v
git diff --check -- pyproject.toml requirements.lock docs/open-source-assessment.md docs/framework/mcp-integration.md src/tricoder/mcp tests/test_mcp_dependency_boundary.py
```

Expected: package identity/version/license match the assessment; tests and diff check pass.

- [ ] **Step 9: Review checkpoint and optional commit**

Inspect only the listed paths. If and only if the user separately authorizes a commit:

```powershell
git add pyproject.toml requirements.lock docs/open-source-assessment.md docs/framework/mcp-integration.md src/tricoder/mcp/__init__.py src/tricoder/mcp/sdk.py tests/test_mcp_dependency_boundary.py
git commit -m "build: pin official MCP SDK"
```

---

### Task 2: Native asynchronous Tool Gateway

**Files:**

- Modify: `src/tricoder/tools/handlers.py`
- Modify: `src/tricoder/tools/command.py`
- Modify: `src/tricoder/tools/__init__.py`
- Modify: `tests/test_tools.py`
- Modify: `tests/test_extension_host.py`

**Interfaces:**

- Produces: `ToolHandler.run_async(arguments, *, cancellation=None) -> ToolResult`
- Preserves: `ToolHandler.run(arguments) -> ToolResult`
- Produces: `ToolRegistry.execute_async(...)` using the same validation/approval/output pipeline as `execute(...)`
- Consumers: Task 6 `MCPToolHandler`, existing `CodingAgent.run_with_context_async`

- [ ] **Step 1: Write RED tests for native async dispatch and identical policy**

Add an `AsyncOnlyProbeHandler` to `tests/test_extension_host.py`:

```python
class AsyncOnlyProbeHandler(ToolHandler):
    name = "async_probe"
    description = "异步探针"
    parameters = ToolHandler._schema({"text": {"type": "string"}}, ["text"])
    risk = "dangerous"

    def run(self, arguments):  # type: ignore[no-untyped-def]
        return ToolResult(False, "同步入口禁止执行异步扩展工具")

    async def run_async(self, arguments, *, cancellation=None):  # type: ignore[no-untyped-def]
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        return ToolResult(True, str(arguments["text"]))
```

Add tests asserting:

```python
result = await registry.execute_async("async_probe", {"text": "ok"})
self.assertTrue(result.ok)
self.assertEqual("ok", result.output)
self.assertEqual([("dangerous_extension_tool", expected_detail)], approvals)
```

Also assert invalid arguments never call `run_async`, read-only rejects before approval, cancellation propagates as `CancellationError`, unexpected async extension exceptions become `扩展工具执行失败，已安全隔离`, and output budget/spill metadata matches the synchronous path.

- [ ] **Step 2: Run focused tests and verify RED**

```powershell
& .\.venv\Scripts\python.exe -m unittest tests.test_extension_host.DynamicToolRegistryTests tests.test_tools -v
```

Expected: the async-only handler test fails because `execute_async()` currently calls synchronous `execute()` in a worker thread.

- [ ] **Step 3: Add the default async handler contract**

In `src/tricoder/tools/handlers.py` add:

```python
async def run_async(
    self,
    arguments: dict[str, Any],
    *,
    cancellation: CancellationToken | None = None,
) -> ToolResult:
    if cancellation is not None:
        cancellation.raise_if_cancelled()
    return await asyncio.to_thread(self.run, arguments)
```

Import `asyncio` and `CancellationToken`. In `src/tricoder/tools/command.py`, override `run_async()` so `run_with_cancellation(arguments, cancellation)` executes through `asyncio.to_thread`; this preserves termination of a running command.

- [ ] **Step 4: Refactor registry execution into shared helpers**

In `src/tricoder/tools/__init__.py`, introduce private helpers with these responsibilities:

```python
def _prepare_execution(
    self, name: str, arguments: dict[str, Any]
) -> tuple[ToolHandler, ToolOrigin] | ToolResult:
    """取消之外：查找、冻结 Schema 校验、只读边界和扩展审批。"""

def _normalize_execution_result(
    self,
    name: str,
    arguments: dict[str, Any],
    result: ToolResult,
    call_id: str | None,
) -> ToolResult:
    """应用 patch 审计元数据与统一 output/spill budget。"""

def _safe_execution_failure(
    self, name: str, arguments: dict[str, Any], origin: ToolOrigin, exc: Exception
) -> ToolResult:
    """保持现有 Policy/Change/文件错误语义并隔离扩展异常。"""
```

`execute()` 调用 `handler.run()`；`execute_async()` 调用 `await handler.run_async(..., cancellation=...)`。两者必须共用上面三个 helper，且都保留 `CancellationError` 原样传播。

- [ ] **Step 5: Run focused and Agent async tests**

```powershell
& .\.venv\Scripts\python.exe -m unittest tests.test_tools tests.test_extension_host tests.test_agent_async -v
```

Expected: PASS;现有同步工具、受管命令取消和动态工具审计语义均不变。

- [ ] **Step 6: Self-review and optional commit**

Check that `execute_async()` contains no `asyncio.run()` and no direct private-handler bypass. If commit approval exists:

```powershell
git add src/tricoder/tools/handlers.py src/tricoder/tools/command.py src/tricoder/tools/__init__.py tests/test_tools.py tests/test_extension_host.py
git commit -m "refactor: add native async tool execution"
```

---

### Task 3: Bounded MCP models, names, Schema, and output conversion

**Files:**

- Create: `src/tricoder/mcp/models.py`
- Create: `src/tricoder/mcp/schema.py`
- Create: `src/tricoder/mcp/tool_adapter.py`
- Create: `tests/test_mcp_schema.py`
- Create: `tests/test_mcp_tool_adapter.py`
- Modify: `src/tricoder/tools/handlers.py`
- Modify: `src/tricoder/tools/__init__.py`

**Interfaces:**

- Produces: `MCPToolSpec(server_id, raw_name, public_name, description, input_schema)`
- Produces: `MCPCallResult(ok, text, omitted_content_types=())`
- Produces: `normalize_tool_name(server_id: str, raw_name: str) -> str`
- Produces: `validate_mcp_schema(schema: object) -> dict[str, object]`
- Produces: `validate_json_value(schema: dict[str, object], value: object) -> None`
- Produces: `normalize_mcp_result(result: object, *, max_chars: int = 200_000) -> MCPCallResult`
- Consumers: Tasks 5–7

- [ ] **Step 1: Write bounded Schema tests**

Create `tests/test_mcp_schema.py` with table-driven cases for object, array, string, integer, number, boolean, null, scalar enum, required and `additionalProperties: false`. Include explicit rejection tests:

```python
for rejected in (
    {"$ref": "#/$defs/X"},
    {"type": "object", "$defs": {}},
    {"oneOf": [{"type": "string"}, {"type": "integer"}]},
    {"type": "object", "patternProperties": {".*": {"type": "string"}}},
):
    with self.subTest(schema=rejected):
        with self.assertRaisesRegex(ValueError, "不支持"):
            validate_mcp_schema(rejected)
```

Generate nested dictionaries in the test to exceed constants `MAX_SCHEMA_BYTES = 32_768`, `MAX_SCHEMA_DEPTH = 8`, `MAX_SCHEMA_PROPERTIES = 128`, and assert deterministic rejection. Verify `integer` rejects bool and `additionalProperties: false` rejects unknown keys recursively.

- [ ] **Step 2: Write name and output tests**

In `tests/test_mcp_tool_adapter.py`, assert:

```python
self.assertEqual("mcp__docs_server__read_page", normalize_tool_name("docs-server", "Read Page"))
self.assertEqual(normalize_tool_name("s", "X" * 200), normalize_tool_name("s", "X" * 200))
self.assertLessEqual(len(normalize_tool_name("s", "X" * 200)), 64)
```

Use fake content objects to verify text concatenation is bounded, `image`/`audio`/`resource` produce `[已省略 MCP image 内容]`-style metadata only, and base64/blob sentinel strings never appear in `MCPCallResult.text`.

- [ ] **Step 3: Run pure adapter tests and verify RED**

```powershell
& .\.venv\Scripts\python.exe -m unittest tests.test_mcp_schema tests.test_mcp_tool_adapter -v
```

Expected: FAIL because the modules do not exist.

- [ ] **Step 4: Implement immutable internal models**

Use SDK-independent dataclasses in `models.py`:

```python
class MCPServerState(str, Enum):
    DISABLED = "disabled"
    STARTING = "starting"
    READY = "ready"
    FAILED = "failed"
    STOPPING = "stopping"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class MCPToolSpec:
    server_id: str
    raw_name: str
    public_name: str
    description: str
    input_schema: dict[str, object]


@dataclass(frozen=True, slots=True)
class MCPCallResult:
    ok: bool
    text: str
    omitted_content_types: tuple[str, ...] = ()
```

Enforce bounded identifiers and copy Schema dictionaries before storage.

- [ ] **Step 5: Implement the recursive Schema subset**

`validate_mcp_schema()` first serializes with deterministic JSON to enforce byte size, then recursively checks allowed keys and counts depth/properties. `validate_json_value()` must use exact type rules:

```python
if expected == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
    raise ValueError("参数必须是 integer")
if expected == "number" and (
    not isinstance(value, (int, float)) or isinstance(value, bool)
):
    raise ValueError("参数必须是 number")
```

Do not invoke `jsonschema` at runtime; the local subset is the enforceable contract.

- [ ] **Step 6: Let ToolRegistry validate the same bounded subset**

Replace the current top-level-only schema checks in `ToolHandler._validate_arguments()` and `ToolRegistry._validate_definition_schema()` with calls to `validate_json_value()` and `validate_mcp_schema()`. Preserve existing error text where current built-in tests assert it. Add regression cases for all current built-in definitions.

- [ ] **Step 7: Implement stable names and bounded output**

Normalize each name component to lower-case `[a-z0-9_]`, collapse runs, prefix invalid starts with `x_`, and when the final name exceeds 64 characters append the first 10 hex characters of SHA-256 over `server_id + "\0" + raw_name`. Limit description to 2,000 characters. `normalize_mcp_result()` applies a 200,000-character transport safety ceiling; the lower ToolRegistry budget then decides inline preview versus spill storage. Never stringify unknown SDK objects wholesale.

- [ ] **Step 8: Run tests and optional commit**

```powershell
& .\.venv\Scripts\python.exe -m unittest tests.test_mcp_schema tests.test_mcp_tool_adapter tests.test_tools tests.test_extension_host -v
```

Expected: PASS. If commit approval exists:

```powershell
git add src/tricoder/mcp/models.py src/tricoder/mcp/schema.py src/tricoder/mcp/tool_adapter.py src/tricoder/tools/handlers.py src/tricoder/tools/__init__.py tests/test_mcp_schema.py tests/test_mcp_tool_adapter.py tests/test_tools.py
git commit -m "feat: add bounded MCP tool contracts"
```

---

### Task 4: MCP launch security and forced approval

**Files:**

- Create: `src/tricoder/mcp/security.py`
- Create: `tests/test_mcp_security.py`
- Modify: `src/tricoder/session_runtime.py`
- Modify: `tests/test_session_runtime.py`

**Interfaces:**

- Produces: `MCPLaunchRequest(command: str, args: tuple[str, ...], cwd: Path, env: Mapping[str, str], approval_detail: str)`
- Produces: `MCPLaunchError(RuntimeError)` and `MCPStartRejected(MCPLaunchError)`
- Produces: `prepare_mcp_launch(config, *, workspace, source_env) -> MCPLaunchRequest`
- Produces: `approve_mcp_start(request, server_id, approver) -> bool`
- Consumers: Task 5 `MCPClient.start`

- [ ] **Step 1: Write executable and argv RED tests**

Create `tests/test_mcp_security.py` using temporary PATH directories and regular fake executable files. Cover:

- qualified command (`C:\\outside\\server.exe`, `./server`) rejected;
- empty/relative PATH entries ignored;
- symlink/reparse executable rejected or platform-skipped when creation is unavailable;
- `python` resolves with `trusted_python_executable()`;
- `npx -y`, `npm exec --yes`, `pip install`, `python -m pip install`, and `uv run --with` rejected;
- standalone path args and `--config=<path>` values outside WorkspacePolicy rejected;
- NUL, more than 64 args, an arg over 4,096 chars, or total argv over 32,768 chars rejected;
- `approval_detail` contains resolved executable and bounded args but no env values.

- [ ] **Step 2: Write minimal-environment RED tests**

Provide a source mapping containing `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`, `USERPROFILE`, `HOME`, `DOCS_MCP_TOKEN`, PATH and SystemRoot. Assert only the authorized `DOCS_MCP_TOKEN` value is added, Provider keys are absent, SDK-default personal keys are explicitly blank, and essential platform values are deliberately retained.

Keep the reviewed SDK default-key sets explicit and version-coupled:

```python
WINDOWS_SDK_DEFAULT_KEYS = frozenset({
    "APPDATA", "HOMEDRIVE", "HOMEPATH", "LOCALAPPDATA", "PATH", "PATHEXT",
    "PROCESSOR_ARCHITECTURE", "SYSTEMDRIVE", "SYSTEMROOT", "TEMP", "USERNAME",
    "USERPROFILE",
})
POSIX_SDK_DEFAULT_KEYS = frozenset({"HOME", "LOGNAME", "PATH", "SHELL", "TERM", "USER"})
```

Tests must prove every key in the applicable set is explicitly supplied, so an SDK upgrade cannot silently reintroduce inherited personal values.

Use an unauthorized `MCPServerConfig(credentials_authorized=False)` and assert launch preparation fails before approval or process creation.

- [ ] **Step 3: Write forced-approval permission tests**

Extend `tests/test_session_runtime.py`:

```python
self.runtime.set_permission("fullaccess")
self.assertFalse(self.runtime._effective_approver("dangerous_mcp_server_start", "server"))
self.assertEqual(1, len(self.approvals))
```

The injected human approver decides the final bool. Repeat for relaxed and strict. This protects startup from permission-level auto-approval.

- [ ] **Step 4: Run focused tests and verify RED**

```powershell
& .\.venv\Scripts\python.exe -m unittest tests.test_mcp_security tests.test_session_runtime -v
```

Expected: launch-policy tests fail; fullaccess currently auto-approves the new action.

- [ ] **Step 5: Implement launch preparation**

`prepare_mcp_launch()` must:

1. confirm config is enabled, authorized and credential-complete;
2. resolve `python` with `trusted_python_executable()` and other pure names with `trusted_path_executable()` over `filtered_subprocess_env()`;
3. validate downloader/install signatures before any process call;
4. validate path-like standalone and `--name=value` arguments through WorkspacePolicy;
5. build an explicit env overriding every SDK default inherited key; and
6. return immutable/safely copied launch data.

Define the forced action exactly once:

```python
MCP_START_APPROVAL_ACTION = "dangerous_mcp_server_start"
```

Add this action to `_DANGEROUS_TOOLS` in `session_runtime.py`. `approve_mcp_start()` calls the supplied approver once and raises a fixed `MCPStartRejected` category on false; it never logs or returns env values.

- [ ] **Step 6: Run security tests and optional commit**

```powershell
& .\.venv\Scripts\python.exe -m unittest tests.test_mcp_security tests.test_policy tests.test_subprocess_env tests.test_session_runtime -v
```

Expected: PASS. If commit approval exists:

```powershell
git add src/tricoder/mcp/security.py src/tricoder/session_runtime.py tests/test_mcp_security.py tests/test_session_runtime.py
git commit -m "feat: secure MCP server startup"
```

---

### Task 5: Single-server stdio client

**Files:**

- Create: `src/tricoder/mcp/client.py`
- Create: `tests/test_mcp_client.py`
- Modify: `src/tricoder/mcp/__init__.py`

**Interfaces:**

- Consumes: `MCPSDK`, `MCPLaunchRequest`, `MCPToolSpec`, `MCPCallResult`, `CancellationToken`
- Produces: `MCPClient.start(cancellation)`, `list_tools(cancellation)`, `call_tool(name, arguments, cancellation)`, `stop()`
- Produces: stable exception classes `MCPClientError`, `MCPTimeoutError`, `MCPProtocolError`, `MCPCleanupError`
- Consumers: Task 6 `MCPManager`

- [ ] **Step 1: Build a fully fake SDK/session harness**

In `tests/test_mcp_client.py`, define async context managers that record entry/exit and a fake session with `initialize`, `list_tools`, and `call_tool`. Do not spawn a process or access network. Inject it through `sdk_loader=lambda: fake_sdk` and inject a pre-approved `MCPLaunchRequest`.

The fake `stdio_server_parameters` must capture:

```python
{
    "command": resolved_command,
    "args": list(args),
    "env": dict(env),
    "cwd": str(workspace),
    "encoding_error_handler": "replace",
}
```

- [ ] **Step 2: Write lifecycle, timeout, cancellation and redaction tests**

Cover:

- start enters transport/session and calls initialize once;
- `list_tools` returns SDK-independent specs;
- call routes raw name/arguments and normalizes content;
- initialize/list/call timeout returns fixed exception text without fake secret sentinel;
- cancellation before and during operation raises `CancellationError`;
- transport/protocol exceptions become fixed categories;
- stderr sink is OS null, not a captured string buffer;
- `stop()` exits contexts in reverse order and is idempotent;
- failed partial start still closes entered contexts;
- cleanup failure produces `MCPCleanupError` without raw exception text.

- [ ] **Step 3: Run client tests and verify RED**

```powershell
& .\.venv\Scripts\python.exe -m unittest tests.test_mcp_client -v
```

Expected: FAIL because `MCPClient` does not exist.

- [ ] **Step 4: Implement bounded operation/cancellation racing**

Use a helper equivalent to:

```python
async def _await_bounded(operation, *, timeout, cancellation):  # type: ignore[no-untyped-def]
    operation_task = asyncio.create_task(operation)
    cancel_task = asyncio.create_task(_wait_for_cancellation(cancellation))
    try:
        done, _ = await asyncio.wait(
            {operation_task, cancel_task},
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            operation_task.cancel()
            raise MCPTimeoutError("MCP 操作超时")
        if cancel_task in done:
            operation_task.cancel()
            raise CancellationError("操作已取消")
        return await operation_task
    finally:
        cancel_task.cancel()
        try:
            await asyncio.wait_for(
                asyncio.gather(operation_task, cancel_task, return_exceptions=True),
                timeout=0.5,
            )
        except TimeoutError as exc:
            raise MCPCleanupError("MCP 异步操作未能按时回收") from exc
```

`_wait_for_cancellation()` uses `await asyncio.sleep(0.05)` polling, not a worker thread.

- [ ] **Step 5: Implement SDK lifecycle with AsyncExitStack**

Construct `StdioServerParameters` with the exact resolved command/args/env/cwd and `encoding_error_handler="replace"`. Enter `stdio_client(..., errlog=os.devnull handle)` and `ClientSession(read, write)` through one `AsyncExitStack`, then initialize through `_await_bounded()`.

Never return SDK objects. `list_tools()` validates names/descriptions/Schemas into `MCPToolSpec`; `call_tool()` passes only raw tool name and copied dict, then calls `normalize_mcp_result()`.

- [ ] **Step 6: Implement deterministic close**

State transitions are `starting -> ready -> stopping -> stopped` or `failed`. `stop()` uses a separate short timeout, shields the close task from caller cancellation, closes the null stderr handle, clears session references, and is safe on repeated calls.

- [ ] **Step 7: Run client tests and optional commit**

```powershell
& .\.venv\Scripts\python.exe -m unittest tests.test_mcp_client tests.test_cancellation -v
```

Expected: PASS and no pending asyncio task warnings. If commit approval exists:

```powershell
git add src/tricoder/mcp/client.py src/tricoder/mcp/__init__.py tests/test_mcp_client.py
git commit -m "feat: add task-scoped MCP stdio client"
```

---

### Task 6: Multi-server manager and MCP ToolHandler

**Files:**

- Modify: `src/tricoder/mcp/manager.py` (create if absent)
- Modify: `src/tricoder/mcp/tool_adapter.py`
- Create: `tests/test_mcp_manager.py`
- Modify: `tests/test_mcp_tool_adapter.py`
- Modify: `tests/test_extension_host.py`

**Interfaces:**

- Produces: `MCPToolHandler(ToolHandler)` with `risk = "dangerous"`
- Produces: `MCPManager.start_all(cancellation) -> None`
- Produces: `MCPManager.register_tools(registry) -> int`
- Produces: `MCPManager.call_tool(server_id, raw_name, arguments, cancellation) -> MCPCallResult`
- Produces: `MCPManager.stop_all() -> None` and `failures -> tuple[ExtensionFailure, ...]`
- Produces: `MCPManager(config: AppConfig, context: ToolContext, source_env: Mapping[str, str], audit: AuditLogger | None, *, client_factory=MCPClient)`
- Consumers: Task 7 task scope

- [ ] **Step 1: Write MCPToolHandler RED tests**

Construct a fake manager whose async `call_tool()` records calls. Assert:

```python
self.assertEqual("dangerous", handler.risk)
self.assertFalse(handler.run({"query": "x"}).ok)
result = await handler.run_async({"query": "x"}, cancellation=token)
self.assertTrue(result.ok)
self.assertEqual("safe text", result.output)
```

Register it in ToolRegistry and verify strict, relaxed and fullaccess each call the human approver with action `dangerous_extension_tool`; rejection prevents manager invocation. Audit contains origin `{kind: "mcp", id: server_id, risk: "dangerous"}` and argument keys only, never values.

- [ ] **Step 2: Write manager isolation and collision RED tests**

Inject fake clients for three ordered configs: ready A, failed B, ready C. Verify:

- A and C tools register; B produces one fixed `ExtensionFailure`;
- clients start A/B/C in configuration order and ready clients stop C/A in reverse order;
- call routing rejects unknown/failed server ids;
- repeated `stop_all()` has no effect;
- normalized-name collisions within one server reject both handlers;
- collisions across server ids remain distinct;
- collision with a built-in tool is recorded and built-in remains;
- cancellation rolls back every already-started client and propagates.

Provide an `AuditLogger` fake and assert lifecycle records contain only event category, server id, phase, status, duration, tool-name digest and size/count metadata. Raw argv, environment values, tool arguments, stderr and exception text must be absent.

- [ ] **Step 3: Run tests and verify RED**

```powershell
& .\.venv\Scripts\python.exe -m unittest tests.test_mcp_manager tests.test_mcp_tool_adapter tests.test_extension_host -v
```

Expected: FAIL because manager/handler behavior is absent.

- [ ] **Step 4: Implement server extensions over ExtensionHost**

Create one internal `MCPServerExtension` per enabled server. Its descriptor is:

```python
ExtensionDescriptor(
    id=config.id,
    kind=ExtensionKind.MCP,
    source=f"mcp/{config.id}",
    enabled=True,
    trust=ExtensionTrust.PROJECT,
)
```

`start()` opens its MCPClient and lists tools; `tool_handlers()` returns handlers only after all names/Schemas are validated and collisions removed; `stop()` delegates to idempotent client close. The manager owns a fresh `ExtensionHost` for this one task.

- [ ] **Step 5: Implement manager routing and handlers**

`MCPToolHandler.run_async()` validates cancellation, calls `manager.call_tool()`, and maps `MCPCallResult` to `ToolResult`. Its sync `run()` returns exactly `ToolResult(False, "MCP 工具仅支持异步执行")`.

`MCPManager.call_tool()` only accepts an exact configured server id and exact raw tool name present in that server's immutable tool map. No fuzzy lookup and no direct public-name-to-SDK call.

Manager lifecycle audit uses `AuditLogger.log()` with fixed structured fields. Audit failure follows the existing fail-closed policy: a server must not start if its required startup audit cannot be written; cleanup continues even if a later audit append fails.

- [ ] **Step 6: Run manager/gateway tests and optional commit**

```powershell
& .\.venv\Scripts\python.exe -m unittest tests.test_mcp_manager tests.test_mcp_tool_adapter tests.test_extension_host tests.test_tools tests.test_audit -v
```

Expected: PASS. If commit approval exists:

```powershell
git add src/tricoder/mcp/manager.py src/tricoder/mcp/tool_adapter.py tests/test_mcp_manager.py tests/test_mcp_tool_adapter.py tests/test_extension_host.py
git commit -m "feat: route MCP tools through extension host"
```

---

### Task 7: Task-scoped runtime, SessionRuntime, and one-shot CLI wiring

**Files:**

- Create: `src/tricoder/mcp/runtime.py`
- Create: `tests/test_mcp_runtime.py`
- Modify: `src/tricoder/session_runtime.py`
- Modify: `src/tricoder/cli.py`
- Modify: `tests/test_session_runtime.py`
- Modify: `tests/test_cli.py`

**Interfaces:**

- Produces: `run_mcp_task(config, registry, *, source_env, audit, cancellation, operation, manager_factory=MCPManager) -> T`
- Produces: `run_mcp_task_sync(config, registry, *, source_env, audit, cancellation, operation, manager_factory=MCPManager) -> T` for current synchronous CLI/runtime callers
- Preserves: non-MCP `SessionRuntime.run_task()` and one-shot CLI paths without SDK import
- Consumers: interactive shell/TUI through SessionRuntime and `tricoder run`

- [ ] **Step 1: Write task-scope RED tests**

Create `tests/test_mcp_runtime.py` with an injected fake manager and async operation:

```python
result = await run_mcp_task(
    config,
    registry,
    source_env={},
    audit=None,
    cancellation=token,
    operation=lambda active_registry: probe(active_registry),
    manager_factory=factory,
)
self.assertEqual("done", result)
self.assertEqual(["start", "register", "operation", "stop"], events)
```

Repeat with operation failure, cancellation and stop failure. Assert stop always runs; cleanup failure prevents a success result; manager and dynamic handlers are unreachable after scope exit.

- [ ] **Step 2: Write SessionRuntime isolation RED tests**

Use a fake manager factory and two sequential tasks. Assert the first task sees `mcp__docs__echo`, the second task receives a fresh manager/registry, and `current.tools` retains only built-ins. Assert SessionContext, journal, spill store, audit and permission snapshot are still the current session's objects.

Add a disabled-config test that patches `tricoder.mcp.sdk.load_mcp_sdk` to raise if called and asserts existing `current.agent` is used unchanged.

- [ ] **Step 3: Write one-shot CLI and Ctrl+C RED tests**

Inject a manager/task-scope factory into `main()` or a narrow `_run_once()` helper. Verify enabled MCP uses the scope, disabled MCP never constructs the manager, missing SDK exits 2 with fixed guidance, and `KeyboardInterrupt` cancels then closes before returning 130.

- [ ] **Step 4: Run runtime tests and verify RED**

```powershell
& .\.venv\Scripts\python.exe -m unittest tests.test_mcp_runtime tests.test_session_runtime tests.test_cli -v
```

Expected: MCP-specific tests fail because no runtime wiring exists.

- [ ] **Step 5: Implement the generic task scope**

In `mcp/runtime.py` define a generic async operation protocol:

```python
T = TypeVar("T")

async def run_mcp_task(
    config: AppConfig,
    registry: ToolRegistry,
    *,
    source_env: Mapping[str, str],
    audit: AuditLogger | None,
    cancellation: CancellationToken,
    operation: Callable[[ToolRegistry], Awaitable[T]],
    manager_factory: Callable[
        [AppConfig, ToolContext, Mapping[str, str], AuditLogger | None], MCPManager
    ] = MCPManager,
) -> T:
    manager = manager_factory(config, registry.context, source_env, audit)
    primary_error: BaseException | None = None
    try:
        await manager.start_all(cancellation)
        manager.register_tools(registry)
        return await operation(registry)
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            await manager.stop_all()
        except Exception:
            if primary_error is None:
                raise
```

The concrete implementation must preserve a primary operation exception if cleanup also fails. If the operation succeeded and cleanup cannot be confirmed, raise `MCPCleanupError` so the task cannot report success; if the operation already failed, record the fixed cleanup category and re-raise the primary failure. `run_mcp_task_sync()` uses one `asyncio.run()` around this complete coroutine and rejects invocation from an already-running loop. No client/session call creates its own event loop.

- [ ] **Step 6: Wire SessionRuntime without changing the disabled path**

Extract current Agent construction into `_create_agent(config, tools, audit)`. In `_run_task_locked()`:

- if `extensions.enabled`, `mcp.enabled`, and at least one server is effectively enabled, create `ToolRegistry(original.tools.context)` and a task-level Agent, then call `run_mcp_task_sync()` with `agent.run_with_context_async`;
- otherwise call the existing `original.agent.run_with_context` exactly as today.

Pass `self.options.environ if self.options.environ is not None else os.environ` only to MCP environment filtering and pass `original.audit` for safe lifecycle events; never load `.env.local` values for `TRICODER_EXTENSION_ENV_ALLOWLIST`. Keep journal begin/seal and persistence outside the MCP branch so both paths share task accounting.

- [ ] **Step 7: Wire one-shot CLI through the same helper**

Move only one-shot Agent execution into a narrow `_run_once_with_config()` helper. When MCP is enabled, call the same `run_mcp_task_sync()` with the already prepared registry and an async Agent operation. When disabled, preserve `agent.run(...)`. On Ctrl+C, cancel the token and wait for scope cleanup before returning 130.

- [ ] **Step 8: Run runtime/CLI tests and optional commit**

```powershell
& .\.venv\Scripts\python.exe -m unittest tests.test_mcp_runtime tests.test_session_runtime tests.test_cli tests.test_tui tests.test_shell -v
```

Expected: PASS; TUI continues using SessionRuntime and gains MCP without a second implementation. If commit approval exists:

```powershell
git add src/tricoder/mcp/runtime.py src/tricoder/session_runtime.py src/tricoder/cli.py tests/test_mcp_runtime.py tests/test_session_runtime.py tests/test_cli.py
git commit -m "feat: wire task-scoped MCP into runtimes"
```

---

### Task 8: Real local fake-stdio integration

**Files:**

- Create: `tests/fixtures/fake_mcp_server.py`
- Create: `tests/test_mcp_integration.py`
- Modify: `tests/test_mcp_client.py`
- Modify: `tests/test_mcp_runtime.py`

**Interfaces:**

- Consumes: installed `mcp==2.1.1`, real `stdio_client`, task runtime
- Produces: deterministic local protocol proof without network or credentials

- [ ] **Step 1: Create the repository-local fake server**

Use the installed official server API only inside the fixture:

```python
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("tricoder-test")


@mcp.tool()
def echo(text: str) -> str:
    """返回确定性测试文本。"""
    return f"echo:{text}"


@mcp.tool()
def bounded_large_output(size: int) -> str:
    """生成可预测的大结果，用于验证客户端输出边界。"""
    return "X" * min(max(size, 0), 200_000)


if __name__ == "__main__":
    mcp.run(transport="stdio")
```

The fixture must not read environment variables, filesystem content, user directories or network.

- [ ] **Step 2: Write a real stdio integration test**

Construct an enabled `MCPServerConfig` with `command="python"` and relative arg `tests/fixtures/fake_mcp_server.py`. Use a temporary project workspace containing a copied fixture or set the repository root as the explicitly bound workspace. Approver records startup/tool approvals and returns true.

The test must assert:

```python
self.assertIn("mcp__local_test__echo", registry_names)
self.assertEqual("echo:hello", result.output)
self.assertEqual("dangerous", registry.origin("mcp__local_test__echo").risk)
```

After scope exit, use `asyncio.all_tasks()` filtered to MCP-created tasks and assert none remain. Do not inspect system-wide processes.

- [ ] **Step 3: Add overflow and server-failure integration cases**

Call `bounded_large_output` beyond the inline budget and assert a bounded ToolResult plus spill reference, never 200,000 inline characters. Add fixture modes for delayed response and immediate clean exit through non-secret argv flags; assert timeout/failure categories and successful cleanup.

- [ ] **Step 4: Run the real local integration tests**

```powershell
& .\.venv\Scripts\python.exe -m unittest tests.test_mcp_integration -v
```

Expected: PASS with no network, no API key and no manual process cleanup.

- [ ] **Step 5: Run all MCP tests as one gate**

```powershell
& .\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_mcp*.py" -v
```

Expected: all MCP unit/integration tests pass; platform-only reparse tests may skip with an explicit reason.

- [ ] **Step 6: Optional commit**

If commit approval exists:

```powershell
git add tests/fixtures/fake_mcp_server.py tests/test_mcp_integration.py tests/test_mcp_client.py tests/test_mcp_runtime.py
git commit -m "test: cover real local MCP stdio flow"
```

---

### Task 9: Documentation, full regression, and Phase 5 handoff

**Files:**

- Modify: `README.md`
- Modify: `project.md`
- Modify: `docs/superpowers/plans/2026-09-03-hcode-capability-migration.md`
- Modify: `docs/framework/mcp-integration.md`
- Modify if reference code was materially copied: `NOTICE`

**Interfaces:**

- Produces: user-facing configuration/safety guide and durable Phase 5 evidence
- Produces: Phase 5 exit-gate decision and next action for Phase 6

- [ ] **Step 1: Update README with truthful MCP operation**

Document an explicitly disabled example and an enabled local stdio example. Include these warnings verbatim in meaning:

- MCP server is local code execution, not an OS sandbox.
- Every task restarts and re-approves each server.
- Every MCP tool is dangerous and requires approval.
- `credential_env` requires trusted process-level `TRICODER_EXTENSION_ENV_ALLOWLIST`; values never belong in TOML.
- Remote MCP and automatic server installation are unsupported.
- Unsupported JSON Schema is rejected rather than run without validation.

Do not include a real public server install command or credential value.

- [ ] **Step 2: Update durable project state**

In `project.md`, record exact changed files, dependency version, test totals, skips, platform, known SDK line-size risk, Windows/Python 3.11 lock limitation, and Phase 6 as the next action. In the master migration plan, check only Phase 5 steps actually evidenced by tests.

- [ ] **Step 3: Run focused static checks**

```powershell
& .\.venv\Scripts\python.exe -m compileall -q src tests
& .\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_mcp*.py" -v
git diff --check
```

Expected: exit 0. Inspect warnings rather than hiding them.

- [ ] **Step 4: Run the complete project regression**

```powershell
& .\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Expected: all tests pass; only documented platform capability skips are allowed. Record the actual count rather than copying an earlier count.

- [ ] **Step 5: Run existing operational checks**

Use the repository's current commands discovered from README/project state:

```powershell
& .\.venv\Scripts\python.exe -m tricoder doctor --provider openai --workspace . --no-color
& .\.venv\Scripts\python.exe -B -m tricoder eval evals\smoke --dry-run --no-color
& ..\..\.venv\Scripts\python.exe -B ..\..\capabilities\tools\workspace.py doctor tricoder-cli
```

If the workspace tool uses a different verified interpreter recorded in `project.md`, use that exact interpreter. Do not read `.env.local` and do not perform a real Provider request. Expected: doctor/eval dry-run/workspace doctor exit 0.

- [ ] **Step 6: Perform security-oriented diff review**

Inspect:

```powershell
git diff --stat
git diff -- src/tricoder/mcp src/tricoder/tools src/tricoder/session_runtime.py src/tricoder/cli.py pyproject.toml requirements.lock
rg -n "shell=True|os\.system|create_subprocess_shell|\.env\.local|OPENAI_API_KEY|DEEPSEEK_API_KEY|ZAI_API_KEY|except Exception: pass" src/tricoder/mcp tests/test_mcp*.py
```

Expected: no shell execution, secret access, swallowed cleanup error, SDK type leakage or automatic install code. Confirm dynamic tools are task-local and every successful start has a matching close path.

- [ ] **Step 7: Optional final Phase 5 commit**

Only with explicit commit approval:

```powershell
git add README.md project.md docs/framework/mcp-integration.md docs/superpowers/plans/2026-09-03-hcode-capability-migration.md NOTICE
git commit -m "docs: complete MCP integration phase"
```

Do not push unless the user separately requests a push.

## Phase 5 Exit Gate

Before declaring completion, verify all items:

- [ ] Disabled MCP path neither imports the SDK nor changes current behavior.
- [ ] Enabled MCP path is callable from one-shot CLI and SessionRuntime/TUI.
- [ ] Server startup remains human-approved under strict, relaxed and fullaccess.
- [ ] MCP tools remain `dangerous` and use ToolRegistry async execution.
- [ ] Name collisions, unsupported Schema, malformed output and SDK failures fail closed.
- [ ] Environment tests prove Provider credentials and personal defaults are not inherited.
- [ ] Cancellation/timeout/error paths close every started client in reverse order.
- [ ] Real repository-local fake stdio flow passes without network or secrets.
- [ ] Full tests, compileall, eval dry-run, doctor and diff review are green.
- [ ] README/project state disclose no OS sandbox, stdio line-size residual risk and lock limitations.
- [ ] No dependency, commit or push action occurred without its separate approval.
