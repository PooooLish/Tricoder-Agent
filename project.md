# Project: tricoder-cli

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

- 先完成 verified-stdio remediation Task 3 的控制器独立复审（离线门禁已通过）；
  本轮证据见 `runtime/sdd/2026-09-07-tricoder-verified-stdio-remediation/task-3-report.md`。
- Phase 5 最终验收后进入 Phase 6：Skills 与项目指令。先重新读取项目规则、Phase 6 设计/计划和当前
  Git 状态，确认 YAML 依赖、解析范围与写入边界；不得把本轮 Windows 本地结果
  当作跨平台、真实 Provider 或真实外部 MCP server 的验证。

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

- Phase 5 依赖安装与 manifest/lock 变更已经获批并完成，不再是阻塞项；
  当前等待 verified-stdio 补救的控制器复核。Phase 6 的新依赖仍须另行批准。

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
