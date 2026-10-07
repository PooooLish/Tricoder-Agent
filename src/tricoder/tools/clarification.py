"""只读需求澄清工具：等待宿主回答并核对等待期间的工作区状态。"""

from __future__ import annotations

import asyncio
import inspect
import json
import secrets
from typing import Any

from tricoder.core.cancellation import CancellationError, CancellationToken
from tricoder.core.clarification import (
    ClarificationRequest,
    ClarificationResult,
    ClarificationStatus,
)
from tricoder.execution_state import EffectState, ErrorCode, FileEffects
from tricoder.models import ToolResult, tool_failure
from tricoder.task_cleanup import run_in_cleanup_thread
from tricoder.tools.handlers import InvalidToolArgument, ToolHandler
from tricoder.workspace.verification import stable_snapshots


class AskUserTool(ToolHandler):
    """向当前交互宿主提出一个有界问题；回答不具备审批能力。"""

    name = "ask_user"
    description = (
        "仅在缺少必要用户信息时提问并等待回答。回答只是信息，不批准写入、命令、"
        "扩展工具或权限变更；取得回答后必须在下一轮重新决定动作。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "question": {"type": "string", "minLength": 1, "maxLength": 1000},
            "options": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 120},
                "minItems": 2,
                "maxItems": 4,
            },
        },
        "required": ["question"],
        "additionalProperties": False,
    }

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        """同步兼容入口；生产 Agent 使用异步入口。"""

        return self.run_with_cancellation(arguments, None)

    def run_with_cancellation(
        self,
        arguments: dict[str, Any],
        cancellation: CancellationToken | None,
    ) -> ToolResult:
        """同步执行时也把注册表的取消令牌传入真实等待。"""

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self.run_async(
                    arguments,
                    cancellation=cancellation or CancellationToken(),
                )
            )
        return self._failure(
            ClarificationResult.unavailable("host_unavailable"),
            "当前同步入口无法安全等待用户回答",
        )

    async def run_async(
        self,
        arguments: dict[str, Any],
        *,
        cancellation: CancellationToken | None = None,
    ) -> ToolResult:
        token = cancellation or CancellationToken()
        token.raise_if_cancelled()
        try:
            request = self._request(arguments)
        except ValueError as exc:
            raise InvalidToolArgument("澄清参数无效") from exc

        clarifier = self.context.clarifier
        if clarifier is None:
            return self._failure(
                ClarificationResult.unavailable("noninteractive"),
                "当前入口不支持交互回答，任务需要用户补充信息",
            )

        try:
            before = await run_in_cleanup_thread(
                self.context.verification_scope.capture,
                self.context.workspace_policy,
            )
        except CancellationError:
            raise
        except Exception:
            return self._failure(
                ClarificationResult.unavailable("workspace_scan_failed"),
                "等待回答前无法完成工作区扫描，任务已停止",
            )

        token.raise_if_cancelled()
        try:
            if inspect.iscoroutinefunction(clarifier):
                response = await clarifier(
                    request, token, self.context.clarification_timeout
                )
            else:
                response = await run_in_cleanup_thread(
                    clarifier,
                    request,
                    token,
                    self.context.clarification_timeout,
                )
                if inspect.isawaitable(response):
                    response = await response
        except CancellationError:
            raise
        except Exception:
            response = ClarificationResult.unavailable("host_failed")

        # 回答完成后重新读取令牌；取消与退出不能被一个同时到达的答案覆盖。
        if token.is_cancelled:
            response = ClarificationResult.cancelled()
        elif type(response) is not ClarificationResult:
            response = ClarificationResult.unavailable("invalid_host_result")

        try:
            after = await run_in_cleanup_thread(
                self.context.verification_scope.capture,
                self.context.workspace_policy,
            )
        except CancellationError:
            raise
        except Exception:
            if token.is_cancelled:
                response = ClarificationResult.cancelled()
            else:
                return self._failure(
                    ClarificationResult.unavailable("workspace_scan_failed"),
                    "等待回答后无法完成工作区扫描，任务已停止",
                )

        if token.is_cancelled or response.status is ClarificationStatus.CANCELLED:
            return self._failure(
                ClarificationResult.cancelled(),
                "用户取消等待；任务未完成",
                code=ErrorCode.CANCELLED,
            )
        if not stable_snapshots(before, after):
            return self._failure(
                ClarificationResult.unavailable("workspace_changed"),
                "等待回答期间工作区发生变化；本次回答未采用，任务已停止",
            )
        if response.status is ClarificationStatus.ANSWERED:
            assert response.answer is not None
            return ToolResult(
                True,
                json.dumps(
                    {
                        "status": response.status.value,
                        "request_id": request.request_id,
                        "answer": response.answer,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                file_effects=FileEffects(EffectState.NONE),
                clarification=response,
            )
        if response.status is ClarificationStatus.TIMED_OUT:
            return self._failure(response, "等待用户回答超时；任务未完成")
        return self._failure(response, "当前入口无法取得用户回答；任务未完成")

    @staticmethod
    def _request(arguments: dict[str, Any]) -> ClarificationRequest:
        question = arguments.get("question")
        options = arguments.get("options", [])
        if not isinstance(question, str) or not isinstance(options, list):
            raise ValueError("澄清参数类型无效")
        return ClarificationRequest(
            secrets.token_hex(16),
            question,
            tuple(options),
        )

    @staticmethod
    def _failure(
        result: ClarificationResult,
        output: str,
        *,
        code: ErrorCode = ErrorCode.NEEDS_INPUT,
    ) -> ToolResult:
        return tool_failure(
            code,
            output,
            file_effects=FileEffects(EffectState.NONE),
            clarification=result,
        )
