# TriCoder Eval Framework Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建默认调用真实 Provider、使用隔离工作副本和隐藏 verifier 的本地 Coding Agent Eval MVP。

**Architecture:** Eval 以 `tricoder.evals` 独立包承载定义加载、工作区隔离、执行评分和报告；生产 CLI 只负责参数解析与真实 Agent 装配。每个 case 的 Agent 运行在 `runtime/evals/<run-id>/workspaces/<case-id>`，Agent 返回后才注入隐藏 verifier，并以确定性规则评分。

**Tech Stack:** Python 3.11+ 标准库（`tomllib`、`dataclasses`、`subprocess`、`hashlib`、`json`、`shutil`）、现有 TriCoder `CodingAgent`/Provider/ToolRegistry/CommandPolicy、`unittest`。

**Spec:** `docs/superpowers/specs/2026-08-25-eval-framework-design.md`

## Global Constraints

- 默认 `tricoder eval` 使用真实 OpenAI Provider；`--dry-run` 不加载 Key、不构建 Provider、不调用网络。
- 不新增第三方依赖，不读取、打印、复制或持久化 API Key。
- Agent 运行期间不得看到 `verifier/`；隐藏验证仅在 Agent 返回并完成修改快照后注入。
- 所有可变状态和报告只能写入 `runtime/evals/<run-id>/`；原始 suite、workspace fixture、verifier 不得修改。
- fullaccess 仅表示自动批准 TriCoder 策略允许的工具，不是 OS 沙盒；`CommandPolicy`、工作区边界和敏感环境过滤继续生效。
- 报告只保存结构化状态、路径、退出码和用量，不保存任务正文、Provider 输出、源码、补丁或完整验证输出。
- 当前仓库已有未提交修改。新增文件可按任务精确提交；修改 `cli.py`、`README.md`、`project.md` 等重叠文件时必须先审查原差异，并使用 `git add -p -- <path>` 只暂存 Eval hunks。

---

### Task 1: Eval 领域模型与安全 TOML Loader

**Files:**
- Create: `src/tricoder/evals/__init__.py`
- Create: `src/tricoder/evals/models.py`
- Create: `src/tricoder/evals/loader.py`
- Test: `tests/test_eval_loader.py`

**Interfaces:**
- Produces: `EvalDefinitionError(ValueError)`。
- Produces: `VerificationSpec(name: str, command: str, timeout: float)`。
- Produces: `EvalCase(id, title, task, source_dir, workspace_dir, verifier_dir, allowed_changes, required_changes, max_rounds, max_context_chars, verifications)`。
- Produces: `EvalSuite(id: str, title: str, source_dir: Path, cases: tuple[EvalCase, ...])`。
- Produces: `load_suite(path: Path, *, case_id: str | None = None) -> EvalSuite`。

- [ ] **Step 1: 写 Loader 的失败测试**

在 `tests/test_eval_loader.py` 建立临时 suite 帮助函数，并覆盖：合法 suite、`case_id`
过滤、重复 case ID、缺失 verifier、绝对/`..` glob、保留目录 glob、危险验证命令。

```python
def test_load_suite_parses_and_filters_a_valid_case(self) -> None:
    suite_dir = self._write_suite(case_ids=("fix-one", "fix-two"))

    suite = load_suite(suite_dir, case_id="fix-two")

    self.assertEqual("smoke", suite.id)
    self.assertEqual(("fix-two",), tuple(case.id for case in suite.cases))
    self.assertEqual("unit", suite.cases[0].verifications[0].name)

def test_load_suite_rejects_verifier_command_with_shell_chaining(self) -> None:
    suite_dir = self._write_suite(
        verification_command="python -m unittest -q && type .env.local",
    )

    with self.assertRaisesRegex(EvalDefinitionError, "验证命令"):
        load_suite(suite_dir)

def test_load_suite_rejects_reserved_verifier_change_pattern(self) -> None:
    suite_dir = self._write_suite(
        allowed_changes=(".tricoder_eval_verifier/**",),
    )

    with self.assertRaisesRegex(EvalDefinitionError, "保留目录"):
        load_suite(suite_dir)
```

- [ ] **Step 2: 运行红灯测试**

Run: `$env:PYTHONPATH='src'; python -m unittest tests.test_eval_loader -v`

Expected: FAIL，原因是 `tricoder.evals.loader` 尚不存在。

- [ ] **Step 3: 实现不可变模型和严格 Loader**

`models.py` 使用 frozen/slots dataclass；`loader.py` 使用 `tomllib.loads()`，只接受
明确字段。ID 使用 `^[a-z0-9][a-z0-9_-]{0,63}$`；字符串必须非空；轮数、上下文和
timeout 必须为正数；模式必须是 POSIX 相对形式且不能触及
`.tricoder_eval_verifier`；`CommandPolicy().validate(command)` 做无工作区的语法
预检。

```python
class EvalDefinitionError(ValueError):
    """评测定义不完整、越界或包含不安全命令。"""


@dataclass(frozen=True, slots=True)
class VerificationSpec:
    name: str
    command: str
    timeout: float


@dataclass(frozen=True, slots=True)
class EvalCase:
    id: str
    title: str
    task: str
    source_dir: Path
    workspace_dir: Path
    verifier_dir: Path
    allowed_changes: tuple[str, ...]
    required_changes: tuple[str, ...]
    max_rounds: int
    max_context_chars: int
    verifications: tuple[VerificationSpec, ...]
```

`suite.toml` 的精确 schema：

```toml
id = "smoke"
title = "TriCoder Smoke Eval"
cases = ["fix-subtract", "add-validation", "cross-file-feature"]
```

未知字段一律拒绝；case 目录必须精确解析到 `<suite>/cases/<case-id>`，并同时包含
普通目录 `workspace/` 与 `verifier/`。

- [ ] **Step 4: 运行 Loader 测试转绿**

Run: `$env:PYTHONPATH='src'; python -m unittest tests.test_eval_loader -v`

Expected: 全部 PASS，且没有网络或 Provider 构建。

- [ ] **Step 5: 精确提交新文件**

```powershell
git add -- src/tricoder/evals/__init__.py src/tricoder/evals/models.py src/tricoder/evals/loader.py tests/test_eval_loader.py
git diff --cached --check
git commit -m "feat: load deterministic eval suites"
```

---

### Task 2: 工作副本、重解析点防御与修改快照

**Files:**
- Create: `src/tricoder/evals/workspace.py`
- Test: `tests/test_eval_workspace.py`

**Interfaces:**
- Consumes: `EvalCase` from Task 1。
- Produces: `RESERVED_VERIFIER_DIR = ".tricoder_eval_verifier"`。
- Produces: `FileFingerprint(size: int, sha256: str)`。
- Produces: `prepare_workspace(case: EvalCase, workspaces_root: Path) -> Path`。
- Produces: `capture_snapshot(workspace: Path) -> dict[str, FileFingerprint]`。
- Produces: `changed_paths(before, after) -> tuple[str, ...]`。
- Produces: `install_verifier(case: EvalCase, workspace: Path) -> Path` 与 `remove_verifier(workspace: Path) -> None`。

- [ ] **Step 1: 写隔离与快照失败测试**

```python
def test_prepare_workspace_copies_fixture_without_verifier(self) -> None:
    workspace = prepare_workspace(self.case, self.run_root / "workspaces")

    self.assertEqual("value = 1\n", (workspace / "app.py").read_text("utf-8"))
    self.assertFalse((workspace / RESERVED_VERIFIER_DIR).exists())
    self.assertFalse((workspace / "hidden_test.py").exists())

def test_verifier_is_installed_after_snapshot_and_removed(self) -> None:
    workspace = prepare_workspace(self.case, self.run_root / "workspaces")
    before = capture_snapshot(workspace)
    (workspace / "app.py").write_text("value = 2\n", encoding="utf-8")
    after = capture_snapshot(workspace)

    verifier = install_verifier(self.case, workspace)
    self.assertTrue((verifier / "test_hidden.py").is_file())
    self.assertEqual(("app.py",), changed_paths(before, after))
    remove_verifier(workspace)
    self.assertFalse(verifier.exists())
```

另加符号链接/Windows reparse point 拒绝测试；当前账户不能创建时只 skip 对应平台
用例，不能跳过普通隔离测试。

- [ ] **Step 2: 运行红灯测试**

Run: `$env:PYTHONPATH='src'; python -m unittest tests.test_eval_workspace -v`

Expected: FAIL，原因是 workspace API 尚不存在。

- [ ] **Step 3: 实现安全复制与快照**

复制前用 `os.scandir()` 逐项检查：拒绝 `entry.is_symlink()`；Windows 上拒绝
`stat.FILE_ATTRIBUTE_REPARSE_POINT`。每次解析目标后确认其位于
`workspaces_root.resolve()` 内。快照只包含普通文件，以 SHA-256 和 size 判断创建、
删除、内容变化；路径统一为排序后的 POSIX 相对路径。

```python
def changed_paths(
    before: Mapping[str, FileFingerprint],
    after: Mapping[str, FileFingerprint],
) -> tuple[str, ...]:
    return tuple(
        path
        for path in sorted(set(before) | set(after))
        if before.get(path) != after.get(path)
    )
```

`install_verifier()` 必须拒绝已存在的保留目录，并复用同一安全复制函数；
`remove_verifier()` 只删除经校验位于 workspace 内且名称精确匹配的保留目录。

- [ ] **Step 4: 运行工作区测试转绿**

Run: `$env:PYTHONPATH='src'; python -m unittest tests.test_eval_workspace -v`

Expected: 全部 PASS，平台不允许创建链接时仅对应链接用例 skip。

- [ ] **Step 5: 精确提交新文件**

```powershell
git add -- src/tricoder/evals/workspace.py tests/test_eval_workspace.py
git diff --cached --check
git commit -m "feat: isolate eval task workspaces"
```

---

### Task 3: 隐藏验证执行、确定性评分与 Suite Runner

**Files:**
- Create: `src/tricoder/subprocess_env.py`
- Create: `src/tricoder/evals/runner.py`
- Test: `tests/test_subprocess_env.py`
- Test: `tests/test_eval_runner.py`
- Modify: `src/tricoder/tools/command.py`（仅将现有环境过滤实现改为导入共享函数）

**Interfaces:**
- Consumes: Task 1 models and Task 2 workspace APIs。
- Produces: `filtered_subprocess_env(source: Mapping[str, str] | None = None) -> dict[str, str]`。
- Produces: `VerificationResult(name: str, exit_code: int | None, passed: bool, error_code: str | None)`。
- Produces: `EvalCaseResult(case_id, status, failure_codes, duration_ms, rounds, tool_calls, modified_files, verification, usage)`。
- Produces: `EvalRunReport(run_id, suite_id, provider, model, started_at, duration_ms, cases)`。
- Produces: `AgentExecutor = Callable[[EvalCase, Path, Path], RunResult]`，参数依次为 case、工作副本、审计路径。
- Produces: `run_suite(suite, run_dir, provider, model, agent_executor) -> EvalRunReport`。

- [ ] **Step 1: 写环境过滤和 Runner 失败测试**

```python
def test_filtered_subprocess_env_removes_credentials(self) -> None:
    env = filtered_subprocess_env({
        "PATH": "safe",
        "OPENAI_API_KEY": "secret",
        "CUSTOM_TOKEN": "secret",
    })
    self.assertEqual("safe", env["PATH"])
    self.assertNotIn("OPENAI_API_KEY", env)
    self.assertNotIn("CUSTOM_TOKEN", env)

def test_run_suite_hides_verifier_until_agent_returns(self) -> None:
    seen: list[bool] = []

    def executor(case, workspace, audit_path):  # type: ignore[no-untyped-def]
        seen.append((workspace / RESERVED_VERIFIER_DIR).exists())
        (workspace / "app.py").write_text("value = 2\n", encoding="utf-8")
        return RunResult(True, "done", 2, 1, ("app.py",), "通过")

    report = run_suite(self.suite, self.run_dir, "openai", "test-model", executor)

    self.assertEqual([False], seen)
    self.assertEqual("passed", report.cases[0].status)
    self.assertFalse(
        (self.run_dir / "workspaces" / self.case.id / RESERVED_VERIFIER_DIR).exists()
    )
```

增加以下独立测试：Agent false、Agent verification 非“通过”、hidden verifier 失败、
越界修改、required pattern 未命中、executor 抛异常后继续下一个 case、未知 Token
保持 None、所有 case 的 Token 使用 `TokenUsage.merge()` 汇总。

- [ ] **Step 2: 运行红灯测试**

Run: `$env:PYTHONPATH='src'; python -m unittest tests.test_subprocess_env tests.test_eval_runner -v`

Expected: FAIL，原因是共享环境函数和 Runner 尚不存在。

- [ ] **Step 3: 提取共享环境过滤函数**

把 `tools/command.py` 当前 `_filtered_env()` 的规则原样移动到
`tricoder.subprocess_env.filtered_subprocess_env()`；`command.py` 导入该函数并保留
兼容别名 `_filtered_env = filtered_subprocess_env`，避免改变现有工具行为和测试契约。

- [ ] **Step 4: 实现 verifier 和评分**

Runner 在 Agent 返回后先捕获 after snapshot，再安装 verifier。每条验证命令必须用
`CommandPolicy(workspace).validate()` 得到 argv，并通过以下方式运行：

```python
completed = subprocess.run(
    args,
    cwd=workspace,
    env=filtered_subprocess_env(),
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    timeout=spec.timeout,
    check=False,
)
```

不能使用 shell。timeout 映射为 `exit_code=None`、`error_code="verification_timeout"`；
策略拒绝映射为 `verification_policy_rejected`；其他受控异常映射为固定 error code，
不保存异常原文。`finally` 必须调用 `remove_verifier()`。

路径匹配使用 `PurePosixPath(path).match(pattern)`；case 通过要求严格实现 spec 的六个
条件。`failure_codes` 只允许固定集合：`agent_failed`、`agent_unverified`、
`verification_failed`、`change_out_of_scope`、`required_change_missing`、
`executor_error`、`workspace_error`、`verification_error`。

- [ ] **Step 5: 运行 Runner 测试转绿**

Run: `$env:PYTHONPATH='src'; python -m unittest tests.test_subprocess_env tests.test_eval_runner tests.test_tools -v`

Expected: 新测试与既有工具环境过滤测试全部 PASS。

- [ ] **Step 6: 审查重叠文件并提交**

先运行：

```powershell
git diff -- src/tricoder/tools/command.py
```

只暂存共享环境函数的 import/兼容别名 hunk：

```powershell
git add -- src/tricoder/subprocess_env.py src/tricoder/evals/runner.py tests/test_subprocess_env.py tests/test_eval_runner.py
git add -p -- src/tricoder/tools/command.py
git diff --cached --check
git commit -m "feat: run isolated deterministic eval cases"
```

---

### Task 4: 安全 JSON/Markdown 报告

**Files:**
- Create: `src/tricoder/evals/report.py`
- Test: `tests/test_eval_report.py`

**Interfaces:**
- Consumes: `EvalRunReport` from Task 3。
- Produces: `report_as_dict(report: EvalRunReport) -> dict[str, object]`。
- Produces: `render_markdown(report: EvalRunReport) -> str`。
- Produces: `write_reports(report: EvalRunReport, run_dir: Path) -> tuple[Path, Path]`。

- [ ] **Step 1: 写报告失败测试**

```python
def test_write_reports_persists_metrics_without_free_text(self) -> None:
    report = self._report_with_unknown_usage_and_failure()

    json_path, markdown_path = write_reports(report, self.run_dir)

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    markdown = markdown_path.read_text(encoding="utf-8")
    self.assertIsNone(payload["usage"]["input_tokens"])
    self.assertEqual("verification_failed", payload["cases"][0]["failure_codes"][0])
    self.assertNotIn("PROVIDER-SECRET-SENTINEL", json_path.read_text("utf-8"))
    self.assertNotIn("PROVIDER-SECRET-SENTINEL", markdown)
```

再测试：通过率、Token 合并、Markdown 表格换行转义、原子替换后不存在 `.tmp` 文件，
以及输出路径逃逸被拒绝。

- [ ] **Step 2: 运行红灯测试**

Run: `$env:PYTHONPATH='src'; python -m unittest tests.test_eval_report -v`

Expected: FAIL，原因是 report API 尚不存在。

- [ ] **Step 3: 实现结构化序列化与原子写入**

JSON 固定 `schema_version = 1`；case 不包含 task/title/summary/异常原文。Markdown
只展示 case ID、状态、耗时、轮数、工具数、修改文件数、验证结果和 failure codes。
用同目录临时文件加 `os.replace()` 写入 `result.json` 和 `report.md`。

- [ ] **Step 4: 运行报告测试转绿**

Run: `$env:PYTHONPATH='src'; python -m unittest tests.test_eval_report -v`

Expected: 全部 PASS。

- [ ] **Step 5: 精确提交新文件**

```powershell
git add -- src/tricoder/evals/report.py tests/test_eval_report.py
git diff --cached --check
git commit -m "feat: write safe eval reports"
```

---

### Task 5: 真实 Provider 装配与 `tricoder eval` CLI

**Files:**
- Create: `src/tricoder/evals/service.py`
- Create: `tests/test_eval_service.py`
- Create: `tests/test_eval_cli.py`
- Modify: `src/tricoder/cli.py:51-232`

**Interfaces:**
- Consumes: `load_suite()`、`run_suite()`、`write_reports()`。
- Produces: `run_eval_command(args: argparse.Namespace, *, environ, provider_factory, output) -> int`。
- Produces: CLI 子命令 `tricoder eval SUITE [--provider ...] [--model ...] [--base-url ...] [--env-file ...] [--case ...] [--dry-run] [--no-color]`。

- [ ] **Step 1: 写 CLI 解析和 dry-run 失败测试**

```python
def test_eval_parser_defaults_to_real_openai(self) -> None:
    args = build_parser().parse_args(["eval", "evals/smoke"])
    self.assertEqual("eval", args.command)
    self.assertEqual("openai", args.provider)
    self.assertFalse(args.dry_run)

def test_eval_dry_run_does_not_build_provider(self) -> None:
    calls: list[str] = []
    exit_code = main(
        ["eval", str(self.suite_dir), "--dry-run", "--no-color"],
        environ={},
        provider_factory=lambda *_args: calls.append("called"),  # type: ignore[arg-type]
        output=self.output,
    )
    self.assertEqual(0, exit_code)
    self.assertEqual([], calls)
```

`tests/test_eval_service.py` 再覆盖真实模式缺 Key 返回 2、单 case 失败返回 1、全通过
返回 0、`--case` 只执行目标、报告位于 `runtime/evals/<run-id>`。

- [ ] **Step 2: 运行红灯测试**

Run: `$env:PYTHONPATH='src'; python -m unittest tests.test_eval_cli tests.test_eval_service -v`

Expected: FAIL，因为 parser 和 service 尚无 Eval 入口。

- [ ] **Step 3: 增加 CLI 参数和路由**

在 `build_parser()` 添加 `eval` parser；不要给它添加 `--workspace`、`--read-only` 或
`--audit-dir`。在 `main()` 创建 Console 后、chat/tui 分支前路由：

```python
if args.command == "eval":
    return run_eval_command(
        args,
        environ=env,
        provider_factory=provider_factory,
        output=output,
    )
```

- [ ] **Step 4: 实现真实 Agent executor**

`run_eval_command()` 先 `load_suite()`。dry-run 成功后立即输出受控摘要并返回 0；真实
模式以 `Path.cwd()` 调用现有 `load_config()`，因此沿用当前项目 `.env.local` 和
`.tricoder.toml` 边界，但不读取或显示 Key 内容。

为每个 case 构造 executor：

```python
case_config = replace(
    base_config,
    workspace=workspace,
    max_rounds=case.max_rounds,
    max_context_chars=case.max_context_chars,
    read_only=False,
    audit_dir=audit_path.parent,
)
tools = ToolRegistry(ToolContext(
    WorkspacePolicy(workspace),
    CommandPolicy(workspace),
    approver=lambda _action, _detail: True,
    timeout=case_config.timeout,
))
agent = CodingAgent(
    provider_factory(case_config.provider, case_config.timeout),
    tools,
    max_rounds=case_config.max_rounds,
    max_context_chars=case_config.max_context_chars,
    audit=audit,
    tool_protocol=case_config.tool_protocol,
    plan_enabled=case_config.plan_enabled,
)
return agent.run(case.task)
```

先准备 audit，再运行 Agent。只向终端输出 run ID、case ID、状态、指标和报告路径；
Provider/Agent 异常使用固定安全错误，不回显异常文本。

- [ ] **Step 5: 运行 CLI 与服务测试转绿**

Run: `$env:PYTHONPATH='src'; python -m unittest tests.test_eval_cli tests.test_eval_service tests.test_cli -v`

Expected: 新旧 CLI 测试全部 PASS，测试 Provider 均为注入对象，无网络请求。

- [ ] **Step 6: 只暂存 Eval CLI hunks并提交**

```powershell
git diff -- src/tricoder/cli.py
git add -- src/tricoder/evals/service.py tests/test_eval_service.py tests/test_eval_cli.py
git add -p -- src/tricoder/cli.py
git diff --cached --check
git commit -m "feat: add real-provider eval command"
```

---

### Task 6: 三个 Smoke Case 与用户文档

**Files:**
- Create: `evals/smoke/suite.toml`
- Create: `evals/smoke/cases/fix-subtract/case.toml`
- Create: `evals/smoke/cases/fix-subtract/workspace/calculator.py`
- Create: `evals/smoke/cases/fix-subtract/workspace/tests/test_calculator.py`
- Create: `evals/smoke/cases/fix-subtract/verifier/test_hidden.py`
- Create: `evals/smoke/cases/add-validation/case.toml`
- Create: `evals/smoke/cases/add-validation/workspace/usernames.py`
- Create: `evals/smoke/cases/add-validation/workspace/tests/test_usernames.py`
- Create: `evals/smoke/cases/add-validation/verifier/test_hidden.py`
- Create: `evals/smoke/cases/cross-file-feature/case.toml`
- Create: `evals/smoke/cases/cross-file-feature/workspace/pricing.py`
- Create: `evals/smoke/cases/cross-file-feature/workspace/tests/test_pricing.py`
- Create: `evals/smoke/cases/cross-file-feature/verifier/test_hidden.py`
- Test: `tests/test_eval_smoke_suite.py`
- Modify: `README.md`
- Modify: `project.md`

**Interfaces:**
- Consumes: public `tricoder eval` schema and dry-run path。
- Produces: tracked `evals/smoke` benchmark suite。

- [ ] **Step 1: 写 smoke suite 结构失败测试**

```python
def test_builtin_smoke_suite_loads_three_cases(self) -> None:
    suite = load_suite(Path("evals/smoke"))
    self.assertEqual(
        ("fix-subtract", "add-validation", "cross-file-feature"),
        tuple(case.id for case in suite.cases),
    )

def test_builtin_smoke_verifiers_fail_before_agent_changes(self) -> None:
    suite = load_suite(Path("evals/smoke"))
    for case in suite.cases:
        with self.subTest(case=case.id):
            result = run_case_with_noop_agent(case, self.temp_root)
            self.assertNotEqual("passed", result.status)
```

第二个测试使用测试内的 no-op `AgentExecutor` 和 Task 3 的真实隐藏验证路径，证明每个
case 不是预先通过的空评测。

- [ ] **Step 2: 运行红灯测试**

Run: `$env:PYTHONPATH='src'; python -m unittest tests.test_eval_smoke_suite -v`

Expected: FAIL，因为内置 suite 尚不存在。

- [ ] **Step 3: 创建三个具体 fixture**

`fix-subtract/workspace/calculator.py`：

```python
def subtract(a: int, b: int) -> int:
    """返回 a 减去 b 的结果。"""
    return a + b
```

隐藏测试至少验证 `subtract(7, 2) == 5`、`subtract(-3, -2) == -1`。

`add-validation/workspace/usernames.py`：

```python
def normalize_username(value: str) -> str:
    """规范化用户名。"""
    return value.strip().lower()
```

任务要求：非字符串抛 `TypeError`，空白字符串抛 `ValueError`，合法值 strip/lower；
隐藏测试覆盖三类输入。

`cross-file-feature/workspace/pricing.py`：

```python
def final_price(amount: int) -> int:
    """返回订单最终价格。"""
    return amount
```

任务要求创建 `discounts.py` 的 `percentage_discount(amount, percent)`，并让
`pricing.final_price(amount, percent=0)` 使用它；负数 amount/percent 抛
`ValueError`，percent 大于 100 抛 `ValueError`。`required_changes` 精确包含
`pricing.py`、`discounts.py`，隐藏测试覆盖跨文件导入与边界。

三个 case 的验证命令统一为：

```toml
[[verification]]
name = "hidden-tests"
command = "python -m unittest discover -s .tricoder_eval_verifier -q"
timeout = 30
```

- [ ] **Step 4: 更新 README 与项目状态**

README 增加“Eval”章节，给出默认真实运行、Provider 选择、单 case、省费用 dry-run、
报告目录和“fullaccess 非 OS 沙盒”警告。`project.md` 记录架构、隐藏 verifier 决策、
验证结果和下一步（真实三 Provider 手动运行，不在自动测试执行）。

- [ ] **Step 5: 运行 smoke/dry-run 测试转绿**

Run: `$env:PYTHONPATH='src'; python -m unittest tests.test_eval_smoke_suite -v`

Run: `$env:PYTHONPATH='src'; python -m tricoder eval evals/smoke --dry-run --no-color`

Expected: 测试 PASS；dry-run 输出 3 个 case 校验成功，不请求 Key。

- [ ] **Step 6: 精确暂存 fixture，并交互暂存文档 hunks**

```powershell
git add -- evals/smoke tests/test_eval_smoke_suite.py
git add -p -- README.md project.md
git diff --cached --check
git commit -m "test: add hidden-verifier smoke eval suite"
```

---

### Task 7: 全量验证、安全自审与交付

**Files:**
- Review: `src/tricoder/evals/`
- Review: `src/tricoder/subprocess_env.py`
- Review: `src/tricoder/cli.py`
- Review: `src/tricoder/tools/command.py`
- Review: `evals/smoke/`
- Review: `README.md`
- Review: `project.md`

**Interfaces:**
- Consumes: Tasks 1–6 的全部公开行为。
- Produces: 可交付的 Eval MVP 与当前验证证据。

- [ ] **Step 1: 运行 Eval 聚焦测试**

Run:

```powershell
$env:PYTHONPATH='src'
python -m unittest tests.test_eval_loader tests.test_eval_workspace tests.test_subprocess_env tests.test_eval_runner tests.test_eval_report tests.test_eval_cli tests.test_eval_service tests.test_eval_smoke_suite -v
```

Expected: 全部 PASS；仅平台无法创建链接时允许明确的链接测试 skip。

- [ ] **Step 2: 运行全量离线测试与编译检查**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -m unittest discover -s tests -v
& '.\.venv\Scripts\python.exe' -m compileall -q src tests
```

Expected: exit code 0；记录准确测试总数和 skips。

- [ ] **Step 3: 运行离线 dry-run 验证**

Run:

```powershell
$env:PYTHONPATH='src'
python -m tricoder eval evals/smoke --dry-run --no-color
```

Expected: exit code 0，不要求 API Key，不创建 case 工作副本，不调用网络。

- [ ] **Step 4: 安全与差异自审**

逐项确认：Agent 回调时 verifier 不存在；报告无 task/summary/source/output 字段；
subprocess 无 shell；修改路径在 verifier 注入前捕获；reserved dir 始终清理；suite
加载失败发生在 Provider 构建前；所有输出路径位于 `runtime/evals`。

Run:

```powershell
git diff --check
git status --short
git diff --stat
```

Expected: Eval 相关差异无 whitespace error；现有非 Eval 脏文件被准确列为保留项。

- [ ] **Step 5: 不自动运行真实 API Eval**

真实运行会产生费用，所以只向用户提供命令，不在验证阶段自动执行：

```powershell
python -m tricoder eval evals/smoke --case fix-subtract --provider openai --no-color
python -m tricoder eval evals/smoke --case fix-subtract --provider deepseek --no-color
python -m tricoder eval evals/smoke --case fix-subtract --provider glm --no-color
```

最终报告必须明确：自动测试覆盖框架与离线 dry-run；真实 Provider 质量尚未运行，需
用户明确授权费用后再验证。
