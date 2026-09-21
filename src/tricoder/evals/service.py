"""Production assembly for the ``tricoder eval`` command."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

from tricoder.agent import CodingAgent
from tricoder.audit import AuditLogger
from tricoder.config import load_config
from tricoder.models import ProviderConfig, RunResult
from tricoder.policy import CommandPolicy, WorkspacePolicy
from tricoder.providers import ModelProvider
from tricoder.tools import ToolContext, ToolRegistry
from tricoder.sandbox.execution import build_command_policy, build_execution_backend
from tricoder.sandbox.workspace import SandboxWorkspace
from tricoder.verification import VerificationScope

from .loader import load_suite
from .models import EvalCase
from .output import reserve_run_directory
from .report import write_reports
from .runner import run_suite


ProviderFactory = Callable[[ProviderConfig, float], ModelProvider]


def run_eval_command(
    args: argparse.Namespace,
    *,
    environ: Mapping[str, str],
    provider_factory: ProviderFactory,
    output: TextIO,
) -> int:
    """Validate and run an eval suite with production Agent assembly."""

    try:
        suite = load_suite(args.suite, case_id=args.case)
    except Exception:
        output.write("eval_error=definition\n")
        return 2

    if args.dry_run:
        output.write(f"dry_run suite={suite.id} cases={len(suite.cases)}\n")
        for case in suite.cases:
            output.write(f"case={case.id} status=validated\n")
        return 0

    try:
        project_root = Path.cwd()
        base_config = load_config(
            provider=args.provider,
            workspace=project_root,
            environ=environ,
            env_file=args.env_file,
            model=args.model,
            base_url=args.base_url,
            sandbox_mode=getattr(args, "sandbox", "local"),
            sandbox_image=getattr(args, "docker_image", None),
        )
    except Exception:
        output.write("eval_error=configuration\n")
        return 2

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%S.%fz")
    execution_environments: dict[Path, tuple[object, CommandPolicy]] = {}
    def execute_case(
        case: EvalCase,
        workspace: Path,
        audit_path: Path,
    ) -> RunResult:
        case_config = replace(
            base_config,
            workspace=workspace,
            max_rounds=case.max_rounds,
            max_context_chars=case.max_context_chars,
            read_only=False,
            audit_dir=audit_path.parent,
        )
        audit = AuditLogger(audit_path)
        audit.prepare()
        sandbox_workspace = (
            SandboxWorkspace.capture_existing(workspace, session_id=f"eval-{case.id}")
            if case_config.sandbox.mode == "docker"
            else None
        )
        execution_workspace = (
            sandbox_workspace.execution_workspace
            if sandbox_workspace is not None
            else workspace
        )
        backend = build_execution_backend(
            case_config.sandbox,
            execution_workspace,
            session_id=f"eval-{case.id}",
            generation=0,
            state_path=(
                sandbox_workspace.control_path
                if sandbox_workspace is not None
                else None
            ),
        )
        command_policy = build_command_policy(case_config.sandbox, execution_workspace)
        execution_environments[workspace.resolve()] = (backend, command_policy)
        tools = ToolRegistry(
            ToolContext(
                workspace_policy=WorkspacePolicy(execution_workspace),
                command_policy=command_policy,
                approver=lambda _action, _detail: True,
                read_only=False,
                timeout=case_config.timeout,
                execution_backend=backend,
                verification_scope=VerificationScope(
                    sandbox_session_id=(
                        sandbox_workspace.session_id
                        if sandbox_workspace is not None
                        else None
                    ),
                    sandbox_generation=(
                        sandbox_workspace.generation
                        if sandbox_workspace is not None
                        else None
                    ),
                ),
                workspace_diff=(
                    sandbox_workspace.diff_stat
                    if sandbox_workspace is not None
                    else None
                ),
                operation_lock=(
                    sandbox_workspace.operation_lock
                    if sandbox_workspace is not None
                    else None
                ),
            )
        )
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

    try:
        run_dir = reserve_run_directory(project_root, run_id)
        report = run_suite(
            suite,
            run_dir,
            base_config.provider.name,
            base_config.provider.model,
            execute_case,
            verification_environment_factory=lambda workspace: execution_environments[
                workspace.resolve()
            ],
        )
        json_path, markdown_path = write_reports(report, run_dir)
    except Exception:
        output.write("eval_error=runtime\n")
        return 2

    output.write(f"run_id={report.run_id}\n")
    for case_result in report.cases:
        output.write(
            f"case={case_result.case_id} status={case_result.status} "
            f"duration_ms={case_result.duration_ms} rounds={case_result.rounds} "
            f"tool_calls={case_result.tool_calls}\n"
        )
    output.write(f"result={json_path}\n")
    output.write(f"report={markdown_path}\n")
    return 0 if all(case.status == "passed" for case in report.cases) else 1
