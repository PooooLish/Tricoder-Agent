# TriCoder Verified Stdio Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the opaque official stdio cleanup boundary with a TriCoder-owned, structurally verified transport and close the remaining source-specific MCP SDK log leak.

**Architecture:** TriCoder continues to use the pinned official `ClientSession` and protocol models, but a new local transport owns the exact process handle and exposes immutable cleanup evidence. SDK imports, process helpers, message parsing, and log-source identities remain behind `load_mcp_sdk()`; `MCPClient` consumes only the typed adapter and fails closed when process exit or owned-resource cleanup cannot be proved.

**Tech Stack:** Python 3.11+, `asyncio`, pinned `mcp==2.1.1`, its guaranteed AnyIO runtime dependency, `unittest`, existing TriCoder MCP/ExtensionHost abstractions.

**Spec:** `docs/superpowers/specs/2026-09-07-tricoder-verified-stdio-remediation-design.md`

## Global Constraints

- Work only inside `D:\MaHong\AGENT_WORKSPACE_V2\projects\tricoder-cli`; preserve all unrelated dirty-tree changes.
- Do not read `.env.local`, `.local/secrets/`, `.local/envs/`, account directories, or user credentials.
- Do not access the network, call a real Provider or external MCP server, install/upgrade dependencies, modify `.venv`, or change `pyproject.toml`/`requirements.lock`.
- Do not stage, commit, push, publish, delete files, or run destructive Git commands; review uses runtime snapshots because commit permission is absent.
- MCP remains opt-in, local stdio only, `shell=False`, no automatic installation, no OS-sandbox claim, and all MCP tools remain `dangerous` behind the shared ToolRegistry gateway.
- The disabled path must not import `mcp`, `mcp_types`, or `anyio`.
- Only a non-`None` direct-process return code plus completed owned-resource cleanup can produce verified cleanup. EOF, closed pipes, completed tasks, or a successful terminate/kill call are insufficient alone.
- Raw protocol values, SDK exception text, stderr, argv values, environment values, and absolute executable paths must never enter model output, UI, audit, or host logs.
- No SDK source is copied or materially adapted. If implementation requires copying upstream code, stop before that write and re-open the license/NOTICE decision.
- Production code and new/changed tests use clear, conventional Chinese comments/docstrings where explanation is needed.

---

### Task 1: Typed process bindings and verified stdio transport

**Files:**
- Create: `src/tricoder/mcp/transport.py`
- Create: `tests/test_mcp_transport.py`
- Modify: `src/tricoder/mcp/__init__.py`
- Modify: `src/tricoder/mcp/sdk.py`
- Modify: `tests/test_mcp_dependency_boundary.py`
- Modify: `docs/open-source-assessment.md`

**Interfaces:**
- Consumes: existing `MCPSDK`, pinned `mcp==2.1.1`, approved `MCPLaunchRequest.command/args/env/cwd`, and the SDK's platform process helpers loaded only by `load_mcp_sdk()`.
- Produces: `MCPProcessExitEvidence`, `MCPTransportOutcome`, `MCPStdioBindings`, `SDKLogSource`, and `VerifiedStdioTransport`; an extended `MCPSDK` containing validated process/stream bindings and log-source identities. Production `MCPClient` constructs the local transport explicitly; `MCPSDK` does not expose the opaque official `stdio_client` or a transport factory.

- [ ] **Step 1: Record the reuse decision before implementation**

Add a dated remediation subsection to `docs/open-source-assessment.md` with this exact decision:

```text
Decision: approve-with-conditions.
Official mcp==2.1.1 reuse mode: integrate for ClientSession, protocol types,
and narrowly wrapped platform process helpers. TriCoder stdio orchestration:
greenfield. No SDK source is copied; no dependency, lockfile, or NOTICE change.
The adapter is version-bound and must fail closed if required capabilities are
missing. An SDK upgrade requires a fresh dependency/security review.
```

Also record that the existing 2026-09-04 SDK identity, license, advisory, and
runtime review remains the evidence source because this task neither changes
the package nor selects a new dependency.

- [ ] **Step 2: Write direct transport RED tests**

Create `tests/test_mcp_transport.py` with controlled in-memory process, pipe,
clock, task, and process-binding doubles. Tests must exercise the real
`VerifiedStdioTransport` state machine rather than assert mock call counts.
The observable cases are:

```python
class VerifiedStdioTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_natural_nonzero_exit_is_verified(self): ...
    async def test_terminate_then_observed_exit_is_verified(self): ...
    async def test_kill_then_observed_exit_is_verified(self): ...
    async def test_kill_without_observed_exit_is_unknown(self): ...
    async def test_eof_and_closed_pipes_do_not_prove_process_exit(self): ...
    async def test_unknown_exit_still_closes_every_owned_resource(self): ...
    async def test_concurrent_transports_keep_outcomes_isolated(self): ...
    async def test_native_cancellation_keeps_cleanup_bounded_and_unknown(self): ...
```

For the unknown case, use the literal expected value:

```python
self.assertEqual(MCPProcessExitEvidence.UNKNOWN, transport.outcome.process_exit)
self.assertTrue(transport.outcome.resources_closed)
self.assertIsNone(process.returncode)
```

The fake process must expose the same fields consumed from the real process
(`pid`, `returncode`, `stdin`, `stdout`) and never change `returncode` merely
because terminate/kill was called unless that case explicitly represents an
observed exit.

- [ ] **Step 3: Run the transport tests and verify RED**

Run:

```powershell
& .\.venv\Scripts\python.exe -B -m unittest tests.test_mcp_transport -v
```

Expected: import or assertion failures because the transport types and verified
outcome do not exist. A fixture error, timeout, or already-passing test is not a
valid RED.

- [ ] **Step 4: Define the immutable transport contract**

Implement these public-to-`tricoder.mcp` types in `transport.py`:

```python
class MCPProcessExitEvidence(str, Enum):
    NOT_STARTED = "not_started"
    VERIFIED = "verified"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class MCPTransportOutcome:
    process_exit: MCPProcessExitEvidence
    resources_closed: bool


@dataclass(frozen=True, slots=True)
class MCPStdioBindings:
    create_process: Callable[[Any, TextIO], Awaitable[Any]]
    terminate_process_tree: Callable[[Any], Awaitable[None]]
    close_process_job: Callable[[Any], None]
    close_subprocess_transport: Callable[[Any], None]
    create_memory_object_stream: Callable[[int], tuple[Any, Any]]
    parse_message: Callable[[str], Any]
    closed_resource_errors: tuple[type[BaseException], ...]
```

`MCPTransportOutcome` starts as `NOT_STARTED/False`. After process creation it
must never return to `NOT_STARTED`; every failed or unproved shutdown is
`UNKNOWN`. Use explicit finite constants for writer flush, natural exit, and
post-termination reap. Their sum plus a documented scheduling margin must fit
inside the existing 10-second client cleanup budget.

- [ ] **Step 5: Implement the greenfield transport state machine**

Implement `VerifiedStdioTransport` as an async context manager. Its behavior,
not its internal layout, must follow this exact shutdown decision:

```python
async def _prove_process_exit(self) -> MCPProcessExitEvidence:
    if self._process.returncode is not None:
        return MCPProcessExitEvidence.VERIFIED
    if await self._wait_for_returncode(_NATURAL_EXIT_SECONDS):
        return MCPProcessExitEvidence.VERIFIED
    try:
        await self._bindings.terminate_process_tree(self._process)
    except BaseException:
        return MCPProcessExitEvidence.UNKNOWN
    if await self._wait_for_returncode(_REAP_SECONDS):
        return MCPProcessExitEvidence.VERIFIED
    return MCPProcessExitEvidence.UNKNOWN
```

Reader behavior is newline-delimited JSON-RPC: retain an incomplete suffix,
pass each complete line to `bindings.parse_message`, and send either the parsed
`SessionMessage` or the resulting bounded exception object to the session
stream. Writer behavior serializes `session_message.message` with
`model_dump_json(by_alias=True, exclude_unset=True)` and writes one UTF-8 line.
Parsing must not log or format the raw line.

On exit, always close input/output pipes, in-memory streams, platform job and
underlying transport, then join/cancel only tasks created by this transport.
Set `resources_closed=True` only after those owned resources have completed
their close path. Preserve `UNKNOWN` even when all handles close successfully.
Export the transport evidence types and `VerifiedStdioTransport` from
`tricoder.mcp` without importing `mcp`, `mcp_types`, or `anyio`.

- [ ] **Step 6: Build lazy SDK bindings and source identities**

Extend `sdk.py` without importing third-party packages at module import time:

```python
@dataclass(frozen=True, slots=True)
class SDKLogSource:
    logger_name: str
    source_path: str


@dataclass(frozen=True, slots=True)
class MCPSDK:
    client_session: type[Any]
    stdio_server_parameters: type[Any]
    stdio_bindings: MCPStdioBindings
    log_sources: tuple[SDKLogSource, ...]
```

Inside `load_mcp_sdk()`, import and validate only the installed SDK capabilities
needed by Task 1. Construct a local `parse_message` that invokes
`mcp_types.jsonrpc_message_adapter.validate_json(line, by_name=False)` and wraps
the result in `SessionMessage`; return the validation exception as a value
without logging it. Normalize capability/import failures to the existing fixed
`MCPDependencyError`.

For POSIX creation, use the injected AnyIO process API with `start_new_session=True`.
For Windows, use the pinned SDK's job-aware process creator. Platform termination
and handle cleanup are injected through `MCPStdioBindings`; no transport code
imports those SDK modules globally.

- [ ] **Step 7: Prove the optional-import and capability boundary**

Add tests which run a fresh Python subprocess with `-B`, import disabled-path
TriCoder modules, and assert these exact module prefixes are absent from
`sys.modules`:

```python
for prefix in ("mcp", "mcp_types", "anyio"):
    assert not any(name == prefix or name.startswith(prefix + ".") for name in sys.modules)
```

Add a loader test where one required process helper is absent or non-callable;
assert fixed `MCPDependencyError` before `create_process` can run. Do not inspect
or assert third-party exception text.

- [ ] **Step 8: Run Task 1 focused verification and self-review**

Run:

```powershell
& .\.venv\Scripts\python.exe -B -m unittest tests.test_mcp_transport tests.test_mcp_dependency_boundary -v
& .\.venv\Scripts\python.exe -B -m compileall -q src\tricoder\mcp tests\test_mcp_transport.py tests\test_mcp_dependency_boundary.py
git diff --check -- src/tricoder/mcp/__init__.py src/tricoder/mcp/transport.py src/tricoder/mcp/sdk.py tests/test_mcp_transport.py tests/test_mcp_dependency_boundary.py docs/open-source-assessment.md
```

Review the current files directly because `src/tricoder/mcp/` is untracked in
the dirty baseline. Record changed paths, RED/GREEN evidence and remaining risk
under `runtime/sdd/2026-09-07-tricoder-verified-stdio-remediation/`; do not stage
or commit.

---

### Task 2: MCPClient integration and sticky cleanup evidence

**Files:**
- Modify: `src/tricoder/mcp/client.py`
- Modify: `src/tricoder/mcp/sdk.py`
- Modify: `tests/test_mcp_client.py`
- Modify: `tests/test_mcp_runtime.py`
- Modify: `tests/test_mcp_integration.py`
- Modify: `tests/test_mcp_manager.py`

**Interfaces:**
- Consumes: Task 1 `VerifiedStdioTransport.outcome`, `MCPTransportOutcome`, and production `MCPSDK.stdio_bindings`.
- Produces: an `MCPClient` lifecycle in which only verified process exit and closed owned resources permit `STOPPED`; UNKNOWN remains a sticky `mcp_cleanup_failed` through manager/runtime. `MCPClient` accepts a test seam for constructing the local transport, defaulting to `VerifiedStdioTransport`; this seam is independent of `MCPSDK` and cannot restore the official opaque transport.

- [ ] **Step 1: Write client and scope RED tests**

Add tests with a real `VerifiedStdioTransport` and controlled process bindings:

```python
async def test_unknown_process_exit_keeps_client_failed_across_repeated_stop(): ...
async def test_verified_nonzero_exit_allows_client_stopped(): ...
async def test_unknown_cleanup_replaces_success_through_manager_and_runtime(): ...
async def test_unknown_cleanup_does_not_replace_primary_cancelled_error(): ...
async def test_two_clients_do_not_share_cleanup_evidence(): ...
```

The first test must assert the exact public behavior:

```python
with self.assertRaisesRegex(MCPCleanupError, "^mcp_cleanup_failed$"):
    await client.stop()
self.assertEqual(MCPServerState.FAILED, client.state)
with self.assertRaisesRegex(MCPCleanupError, "^mcp_cleanup_failed$"):
    await client.stop()
```

The runtime test must execute `run_mcp_task` with a real manager/client graph and
assert a successful operation becomes `MCPCleanupError`, while metadata-only
audit contains no raw process, protocol, or exception values.

- [ ] **Step 2: Run the new client/scope tests and verify RED**

Run the named new tests with `unittest -v`. Expected failures must show the
current client accepting an UNKNOWN outcome or lacking the new transport
contract. Correct fixture/import errors before proceeding.

- [ ] **Step 3: Replace opaque cleanup success with outcome inspection**

In `_run_lifecycle`, construct and retain the exact transport object from the
Task 1 class (or the constructor-injected test seam) using the loaded
`MCPSDK.stdio_bindings`. After `stack.aclose()` completes, compute cleanup
failure as:

```python
outcome = transport.outcome
transport_verified = (
    outcome.process_exit is MCPProcessExitEvidence.VERIFIED
    and outcome.resources_closed
)
if process_was_started and not transport_verified:
    self._cleanup_failed = True
    cleanup_error = MCPCleanupError("mcp_cleanup_failed")
```

Do not infer success from `lifecycle_task.done()`. Preserve the existing sticky
failure behavior in repeated `stop()`, cancellation precedence, deferred task
reaping, and fixed safe error mapping. A process never started may retain its
ordinary fixed startup failure, but it cannot set state to `STOPPED`.

- [ ] **Step 4: Update test SDK factories to the verified transport contract**

Replace opaque fake async context managers with a reusable test-only
`FakeVerifiedStdioTransport` whose outcome defaults to
`VERIFIED/resources_closed=True` only when its controlled close path completes.
Tests that exercise cleanup failure must set UNKNOWN through behavior, not by
mutating `MCPClient._cleanup_failed` directly.

- [ ] **Step 5: Strengthen the repository fake-stdio integration**

Run the existing repository-local fake server through the production transport
and assert:

```python
self.assertEqual(MCPServerState.STOPPED, client.state)
self.assertEqual(MCPProcessExitEvidence.VERIFIED, captured_transport.outcome.process_exit)
self.assertTrue(captured_transport.outcome.resources_closed)
```

Keep existing task ownership, audit, large-result spill, offset readback and
handler cleanup assertions. The fixture must not access network, environment
values, user files, or external commands.

- [ ] **Step 6: Run Task 2 focused verification and self-review**

Run:

```powershell
& .\.venv\Scripts\python.exe -B -m unittest tests.test_mcp_transport tests.test_mcp_client tests.test_mcp_manager tests.test_mcp_runtime tests.test_mcp_integration -v
& .\.venv\Scripts\python.exe -B -m compileall -q src\tricoder\mcp tests\test_mcp_client.py tests\test_mcp_manager.py tests\test_mcp_runtime.py tests\test_mcp_integration.py
git diff --check -- src/tricoder/mcp tests/test_mcp_client.py tests/test_mcp_manager.py tests/test_mcp_runtime.py tests/test_mcp_integration.py
```

Inspect cancellation ordering, primary-error precedence and all close paths.
Write the report under the remediation runtime directory; do not stage or commit.

---

### Task 3: Source-aware SDK log isolation and final handoff

**Files:**
- Modify: `src/tricoder/mcp/sdk.py`
- Modify: `src/tricoder/mcp/client.py`
- Modify: `tests/test_mcp_client.py`
- Modify: `tests/test_mcp_dependency_boundary.py`
- Modify: `tests/test_mcp_integration.py`
- Modify: `README.md`
- Modify: `docs/framework/mcp-integration.md`
- Modify: `project.md`

**Interfaces:**
- Consumes: Task 1 `SDKLogSource` values discovered after optional SDK load and Task 2's task-local client lifecycle.
- Produces: `isolate_sdk_logs(sources)` which suppresses only actual pinned-SDK records inside the owning task context, plus accurate final documentation and verification evidence.

- [ ] **Step 1: Write real SDK logging RED tests**

Add these behavior tests before modifying the filter:

```python
async def test_locked_invalid_notification_logs_are_isolated(): ...
def test_sdk_log_source_filter_preserves_same_name_third_party(): ...
async def test_sdk_log_boundary_covers_dispatcher_and_request_paths(): ...
async def test_overlapping_log_scopes_restore_only_owned_state(): ...
```

The notification test must send a valid JSON-RPC envelope containing a synthetic
invalid `notifications/progress.params.progress` value through the real locked
`_parse_line → ClientSession → JSONRPCDispatcher → _on_notify` path. First run a
control outside the scope and assert the synthetic sentinel is observable; then
run inside the scope and assert it is absent from direct handler, root handler
and stderr captures. Do not expect `MCPProtocolError`: this SDK warns and drops
the notification.

Construct same-name third-party `LogRecord` objects using different absolute
`pathname` literals. They must remain visible even when their `name` is
`client` or `mcp.client.stdio`; a same `module="session"` value is not enough to
classify a record as SDK-owned.

- [ ] **Step 2: Run logging tests and verify RED**

Run only the new logging tests with `unittest -v`. Expected: the real `client`
notification sentinel leaks and/or the current name-only filter incorrectly
suppresses a same-name third-party record. Fix test harness errors until both are
behavioral failures.

- [ ] **Step 3: Implement exact logger-plus-source filtering**

Change the scope contract to:

```python
@contextmanager
def isolate_sdk_logs(sources: tuple[SDKLogSource, ...]) -> Iterator[None]: ...
```

The ContextVar value is an immutable set of `(logger_name, normalized_path)`
pairs. Normalize each path once with lexical `abspath/normpath/normcase` after
SDK load; never touch the filesystem or format the LogRecord in `filter()`.

Under the existing `RLock`, keep a per-logger reference count. For the first
user, insert the exact shared filter object at index `0`; for the last user,
remove only that exact object. The filter returns `False` only when current
scope, record name and normalized `record.pathname` all match. It must not
change level, disabled, handlers, propagate, root configuration, or the relative
order of any host filters.

Register actual source identities for:

```text
client
mcp.client.stdio
mcp.shared.jsonrpc_dispatcher
mcp.shared.dispatcher
mcp.os.posix.utilities
mcp.os.win32.utilities
```

Only include a source whose imported module has a concrete absolute `__file__`;
a missing required stdio/session/dispatcher identity must fail SDK loading before
spawn. Platform-inapplicable optional utility modules may be absent, but the
active platform's process utility source is required.

- [ ] **Step 4: Pass source identities through every SDK execution scope**

Load SDK before entering the lifecycle log context, store its immutable
`log_sources` on the client for the ready lifetime, and pass the same tuple into
the lifecycle, list/call request wrapper and cleanup path. Clear the client
reference only after all owned SDK tasks are reaped. Ensure child tasks inherit
the ContextVar and no task can outlive the final filter lease.

- [ ] **Step 5: Verify concurrency, restoration and disabled import behavior**

Cover two overlapping clients, nested scopes, startup failure, native/token
cancellation, cleanup error, a bypass task and a bypass thread. While a scope is
active, add an unrelated host filter; after exit it must remain. After the final
scope exits, the TriCoder filter must be absent and all original logger/root
properties unchanged.

Re-run the fresh subprocess dependency-boundary test to prove importing or using
disabled TriCoder paths still does not load `mcp`, `mcp_types`, or `anyio`.

- [ ] **Step 6: Update user and architecture documentation**

Update README, `docs/framework/mcp-integration.md`, and `project.md` to state:

- production local stdio uses a TriCoder-owned transport with a direct process
  handle and structured cleanup evidence;
- success proves the direct server process exited and TriCoder-owned streams/tasks
  closed, not that every detached descendant disappeared;
- raw SDK records are filtered only inside exact task-local, source-verified
  adapter scopes;
- the adapter is bound to `mcp==2.1.1`; SDK upgrades require capability, logging,
  lifecycle and dependency/security review;
- remote MCP, auto-install and OS sandboxing remain unsupported;
- real external MCP/Provider compatibility remains unverified.

Do not claim universal cross-platform proof from Windows-only evidence.

- [ ] **Step 7: Run final project verification**

Create
`runtime/sdd/2026-09-07-tricoder-verified-stdio-remediation/offline-doctor.env`
containing only `OPENAI_API_KEY=offline-placeholder`. Use that explicit
synthetic file and process-local value only for commands that require a config
object; never load `.env.local`. Run:

```powershell
& .\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
& .\.venv\Scripts\python.exe -B -m compileall -q src tests
git diff --check
$env:OPENAI_API_KEY='offline-placeholder'
$env:PYTHONIOENCODING='cp936'
& .\.venv\Scripts\python.exe -B -m tricoder doctor --provider openai --env-file runtime\sdd\2026-09-07-tricoder-verified-stdio-remediation\offline-doctor.env --no-color
$env:PYTHONIOENCODING='utf-8'
& .\.venv\Scripts\python.exe -B -m tricoder doctor --provider openai --env-file runtime\sdd\2026-09-07-tricoder-verified-stdio-remediation\offline-doctor.env --no-color
& .\.venv\Scripts\python.exe -B -m tricoder eval evals\smoke --dry-run --no-color
& .\.venv\Scripts\python.exe -B ..\..\capabilities\tools\workspace.py doctor tricoder-cli
```

Capture exact exit codes, test totals/skips and known diagnostics. Confirm the
synthetic key text is absent from captured doctor output. Do not run a real
Provider or external MCP server.

- [ ] **Step 8: Final self-review and handoff**

Check the full plan against the approved spec, review the complete remediation
surface and all deferred Phase 5 risks, then create a final read-only review
package under `runtime/sdd/2026-09-07-tricoder-verified-stdio-remediation/`.
Record exact rulings, limitations and verification evidence. Do not stage,
commit, push or delete the runtime review workspace.
