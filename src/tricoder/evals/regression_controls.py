"""Regression V1 的固定离线 Provider 与持久基线控制场景。

控制器只接受注册 case ID，只在调用方提供的临时目录、临时 SQLite 和合成
工作区内运行。它不解析 TOML 中的 SQL、绝对路径或脚本。
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import sqlite3

from tricoder.models import (
    AppConfig,
    MemoryConfig,
    Message,
    ProviderConfig,
    ProviderResponse,
    RunResult,
    ToolCall,
)
from tricoder.session.runtime import RuntimeOptions, SessionRuntime, SessionRuntimeError
from tricoder.session.store import SessionStore

from .assertions import AssertionObservation, SubcaseResult


_SPECIAL_CONTROLS = frozenset({"c-restart-changed", "c-baseline-corrupted"})


class RegressionScriptProvider:
    """通过真实 Agent 协议触发固定契约，不接收题目答案。"""

    def __init__(self, case_id: str) -> None:
        if case_id not in {
            "c-text-termination",
            "c-repeat-failure",
            "c-approval-denied",
            "c-restart-unchanged",
        }:
            raise ValueError("unregistered regression script provider")
        self.case_id = case_id
        self.calls = 0

    def complete(self, _messages, _tools=()):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.case_id == "c-text-termination":
            return ProviderResponse(content="plain response", finish_reason="stop")
        if self.case_id == "c-repeat-failure":
            return ProviderResponse(
                tool_calls=(
                    ToolCall(f"read-{self.calls}", "read_file", {"path": "missing.txt"}),
                ),
                finish_reason="tool_calls",
            )
        if self.case_id == "c-approval-denied":
            return ProviderResponse(
                tool_calls=(
                    ToolCall(
                        f"write-{self.calls}",
                        "edit_file",
                        {
                            "path": "state.py",
                            "old_text": "value = 1\n",
                            "new_text": "value = 2\n",
                        },
                    ),
                ),
                finish_reason="tool_calls",
            )
        return ProviderResponse(
            tool_calls=(
                ToolCall(f"read-{self.calls}", "read_file", {"path": "app.py"}),
                ToolCall(
                    f"finish-{self.calls}",
                    "finish",
                    {"summary": "只读检查完成", "outcome": "completed"},
                ),
            ),
            finish_reason="tool_calls",
        )


@dataclass(frozen=True, slots=True)
class ContractControlResult:
    result: RunResult
    observation: AssertionObservation


class _FinishProvider:
    def __init__(self) -> None:
        self.calls = 0
        self.inputs: list[tuple[Message, ...]] = []

    def complete(self, messages, _tools=()):  # type: ignore[no-untyped-def]
        self.calls += 1
        self.inputs.append(tuple(messages))
        return ProviderResponse(
            tool_calls=(
                ToolCall(
                    f"finish-{self.calls}",
                    "finish",
                    {"summary": "只读契约任务完成", "outcome": "completed"},
                ),
            ),
            finish_reason="tool_calls",
        )


def is_special_contract(case_id: str) -> bool:
    return case_id in _SPECIAL_CONTROLS


def build_script_provider(case_id: str) -> RegressionScriptProvider:
    return RegressionScriptProvider(case_id)


def run_special_contract(
    case_id: str,
    source_workspace: Path,
    control_root: Path,
) -> ContractControlResult:
    """Run a registered multi-subcase contract through real SessionRuntime gates."""

    if case_id not in _SPECIAL_CONTROLS:
        raise ValueError("unregistered special contract")
    control_root.mkdir(parents=True, exist_ok=False)
    if case_id == "c-restart-changed":
        return _run_restart_changed(source_workspace, control_root)
    return _run_corrupted_baselines(source_workspace, control_root)


def _config(workspace: Path, root: Path) -> AppConfig:
    return AppConfig(
        workspace=workspace,
        provider=ProviderConfig(
            "fake", "offline-placeholder", "https://example.invalid/v1", "offline"
        ),
        audit_dir=root / "audit",
        plan_enabled=False,
        memory=MemoryConfig(compaction="off", persistence="off"),
    )


def _runtime(
    database: Path,
    workspace: Path,
    root: Path,
    provider: _FinishProvider,
    *,
    session_id: str | None = None,
    confirmer=None,  # type: ignore[no-untyped-def]
) -> SessionRuntime:
    return SessionRuntime(
        SessionStore(database),
        workspace,
        options=RuntimeOptions(environ={}),
        config_loader=lambda **_kwargs: _config(workspace, root),
        provider_factory=lambda _config, _timeout: provider,
        initial_session_id=session_id,
        approver=lambda _action, _detail: True,
        workspace_confirmer=confirmer,
    )


def _seed(
    source_workspace: Path,
    root: Path,
) -> tuple[Path, Path, str]:
    workspace = root / "workspace"
    shutil.copytree(source_workspace, workspace)
    database = root / "sessions.db"
    provider = _FinishProvider()
    runtime = _runtime(
        database,
        workspace,
        root,
        provider,
        confirmer=lambda _preview: (_ for _ in ()).throw(
            AssertionError("initial baseline must not require confirmation")
        ),
    )
    result = runtime.run_task("建立合成基线")
    if not result.ok or runtime.current is None:
        runtime.close()
        raise RuntimeError("could not seed regression baseline")
    session_id = runtime.current.record.id
    if not runtime.close():
        raise RuntimeError("could not close baseline seed runtime")
    return workspace, database, session_id


def _run_restart_changed(
    source_workspace: Path,
    root: Path,
) -> ContractControlResult:
    subcases: list[SubcaseResult] = []
    total_provider_calls = 0
    total_notices = 0
    for decision_name, decision in (("accept", True), ("deny", False)):
        subroot = root / decision_name
        subroot.mkdir()
        workspace, database, session_id = _seed(source_workspace, subroot)
        stored_before = SessionStore(database).load_workspace_baseline(session_id)
        (workspace / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        confirmations: list[object] = []
        provider = _FinishProvider()
        runtime = _runtime(
            database,
            workspace,
            subroot,
            provider,
            session_id=session_id,
            confirmer=lambda preview: confirmations.append(preview) or decision,
        )
        try:
            if decision:
                result = runtime.run_task("读取变化后的 app.py")
                notice_count = sum(
                    message.kind == "workspace_change"
                    for request in provider.inputs
                    for message in request
                )
                stored_after = runtime.store.load_workspace_baseline(session_id)
                checks = {
                    "agent_completed": result.ok,
                    "confirmed_once": len(confirmations) == 1,
                    "provider_called": provider.calls > 0,
                    "notice_received": notice_count == 1,
                    "baseline_records_present": stored_before is not None and stored_after is not None,
                    "revision_advanced": (
                        stored_before is not None
                        and stored_after is not None
                        and stored_after.revision == stored_before.revision + 1
                    ),
                }
                met = all(checks.values())
                reason = next((key for key, value in checks.items() if not value), "ok")
                subcases.append(SubcaseResult("accept", met, result.ok, reason))
                total_notices += notice_count
            else:
                # 拒绝发生在恢复激活门禁；本子场景不再发起第二个用户操作，
                # 否则那会按产品语义形成一次新的确认请求。
                blocked = runtime.status().workspace_baseline_state == "rejected"
                stored_after = runtime.store.load_workspace_baseline(session_id)
                checks = {
                    "blocked": blocked,
                    "confirmed_once": len(confirmations) == 1,
                    "provider_not_called": provider.calls == 0,
                    "baseline_preserved": stored_after == stored_before,
                    "session_preserved": (
                        runtime.current is not None
                        and runtime.current.record.id == session_id
                    ),
                }
                met = all(checks.values())
                reason = next((key for key, value in checks.items() if not value), "ok")
                subcases.append(SubcaseResult("deny", met, False, reason))
            total_provider_calls += provider.calls
        finally:
            if not runtime.close():
                raise RuntimeError("special contract cleanup incomplete")
    passed = all(item.expectation_met for item in subcases)
    return ContractControlResult(
        RunResult(passed, "基线变化子场景已执行", 2),
        AssertionObservation(
            provider_requests=total_provider_calls,
            provider_change_notices=total_notices,
            subcases=tuple(subcases),
        ),
    )


def _run_corrupted_baselines(
    source_workspace: Path,
    root: Path,
) -> ContractControlResult:
    passed_ids: list[str] = []
    subcases: list[SubcaseResult] = []
    variants = (
        ("kind-array", []),
        ("kind-object", {}),
        ("invalid-json", None),
    )
    for variant_id, value in variants:
        subroot = root / variant_id
        subroot.mkdir()
        workspace, database, session_id = _seed(source_workspace, subroot)
        with closing(sqlite3.connect(database)) as connection, connection:
            row = connection.execute(
                "SELECT payload_json, revision FROM session_workspace_baselines "
                "WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("baseline row missing")
            if variant_id == "invalid-json":
                encoded = "{"
            else:
                payload = json.loads(row[0])
                payload["entries"][0]["kind"] = value
                encoded = json.dumps(payload, sort_keys=True)
            connection.execute(
                "UPDATE session_workspace_baselines SET payload_json = ? WHERE session_id = ?",
                (encoded, session_id),
            )
            before = (encoded, row[1])
        provider = _FinishProvider()
        runtime = _runtime(
            database,
            workspace,
            subroot,
            provider,
            session_id=session_id,
            confirmer=lambda _preview: True,
        )
        try:
            blocked = False
            try:
                runtime.run_task("不得进入 Provider")
            except SessionRuntimeError:
                blocked = True
            with closing(sqlite3.connect(database)) as connection:
                after = connection.execute(
                    "SELECT payload_json, revision FROM session_workspace_baselines "
                    "WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
            checks = {
                "task_blocked": blocked,
                "provider_not_called": provider.calls == 0,
                "record_preserved": after == before,
                "session_preserved": (
                    runtime.current is not None
                    and runtime.current.record.id == session_id
                ),
                "state_corrupted": (
                    runtime.status().workspace_baseline_state == "corrupted"
                ),
            }
            met = all(checks.values())
            reason = next((key for key, value in checks.items() if not value), "ok")
            subcases.append(SubcaseResult(variant_id, met, False, reason))
            if met:
                passed_ids.append(variant_id)
        finally:
            if not runtime.close():
                raise RuntimeError("corrupted contract cleanup incomplete")
    passed = len(passed_ids) == len(variants)
    return ContractControlResult(
        RunResult(False, "损坏基线已安全阻止" if passed else "损坏基线契约失败", 0),
        AssertionObservation(
            provider_requests=0,
            corrupted_subcases=tuple(passed_ids),
            subcases=tuple(subcases),
        ),
    )
