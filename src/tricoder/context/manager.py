"""按 token/字符预算保留固定消息和完整工具回合。"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Iterable, Literal, Sequence

from tricoder.models import Message, TokenUsage
from tricoder.protocols import ActionProtocol


CONTEXT_COMPACTION_NOTICE = "较早的消息已被压缩，以保留最新上下文。"


@dataclass(frozen=True, slots=True)
class ContextBudget:
    """一次 Provider 请求允许使用的上下文预算。

    ``max_chars`` 保留旧版精确字符语义；``max_tokens`` 使用 Provider usage
    锚点和本地保守估算。两者同时存在时必须同时满足。
    """

    max_tokens: int | None = None
    max_chars: int | None = None

    def __post_init__(self) -> None:
        if self.max_tokens is None and self.max_chars is None:
            raise ValueError("上下文预算至少需要 max_tokens 或 max_chars")
        for name, value in (("max_tokens", self.max_tokens), ("max_chars", self.max_chars)):
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value <= 0
            ):
                raise ValueError(f"{name} 必须是正整数或 None")


@dataclass(frozen=True, slots=True)
class ContextSnapshot:
    """一次上下文准备的不可变结果，不携带 Provider 原始响应。"""

    messages: tuple[Message, ...]
    estimated_tokens: int
    character_count: int
    compacted: bool
    token_source: Literal["provider", "estimate"]


class ContextManager:
    """对消息进行完整回合归一化，并维护最近一次真实用量锚点。"""

    def __init__(
        self,
        budget: ContextBudget,
        protocol: ActionProtocol | Sequence[ActionProtocol],
    ) -> None:
        self.budget = budget
        self._protocols = (
            tuple(protocol)
            if isinstance(protocol, Sequence)
            else (protocol,)
        )
        if not self._protocols:
            raise ValueError("至少需要一个 ActionProtocol")
        self._anchor_messages: tuple[Message, ...] = ()
        self._anchor_tokens: int | None = None

    def prepare(self, messages: Iterable[Message]) -> ContextSnapshot:
        """返回满足预算的请求消息；固定消息可能安全地超过预算。"""

        copied = list(messages)
        if self.budget.max_tokens is None:
            prepared = self._prepare_character_compatible(copied)
        else:
            prepared = self._prepare_token_budget(copied)
        prepared_tuple = tuple(prepared)
        estimated, source = self._estimate_with_anchor(prepared_tuple)
        return ContextSnapshot(
            messages=prepared_tuple,
            estimated_tokens=estimated,
            character_count=sum(message.character_budget() for message in prepared_tuple),
            compacted=prepared_tuple != tuple(copied),
            token_source=source,
        )

    def prepare_generic(self, messages: Iterable[Message]) -> ContextSnapshot:
        """兼容旧 ``compact_messages``：固定前两条，其余按完整回合处理。"""

        copied = list(messages)
        prepared = self._compact_generic_chars(copied)
        prepared_tuple = tuple(prepared)
        estimated, source = self._estimate_with_anchor(prepared_tuple)
        return ContextSnapshot(
            prepared_tuple,
            estimated,
            sum(message.character_budget() for message in prepared_tuple),
            prepared_tuple != tuple(copied),
            source,
        )

    def record_usage(
        self,
        usage: TokenUsage | None,
        messages: Iterable[Message],
    ) -> None:
        """把 Provider 的真实输入用量绑定到它所描述的精确消息前缀。

        缺少 ``input_tokens`` 时无法证明 prompt 大小，因而清除锚点并继续
        使用本地估算；未知字段绝不按零处理。
        """

        anchored = tuple(messages)
        if usage is None or usage.input_tokens is None:
            self._anchor_messages = ()
            self._anchor_tokens = None
            return
        if usage.output_tokens is None:
            total = max(usage.input_tokens, self._estimate_tokens(anchored))
        else:
            total = usage.input_tokens + usage.output_tokens
        self._anchor_messages = anchored
        self._anchor_tokens = total

    def _prepare_character_compatible(self, messages: list[Message]) -> list[Message]:
        max_chars = self.budget.max_chars
        if max_chars is None:
            return messages
        if any(message.kind == "task" for message in messages):
            return self._compact_session_chars(messages, max_chars)
        return self._compact_generic_chars(messages)

    def _compact_generic_chars(self, messages: list[Message]) -> list[Message]:
        max_chars = self.budget.max_chars
        if max_chars is None:
            return list(messages)
        fixed = list(messages[:2])
        later = list(messages[2:])
        rounds = self._collect_rounds(later, loose=True)
        if (
            sum(len(round_messages) for round_messages in rounds) == len(later)
            and self._chars(messages) <= max_chars
        ):
            return list(messages)
        if not later:
            return fixed
        notice = Message("system", CONTEXT_COMPACTION_NOTICE)
        remaining = max_chars - self._chars([*fixed, notice])
        retained: list[list[Message]] = []
        for round_messages in reversed(rounds):
            cost = self._chars(round_messages)
            if cost > remaining:
                break
            retained.append(round_messages)
            remaining -= cost
        return [
            *fixed,
            notice,
            *(message for group in reversed(retained) for message in group),
        ]

    def _compact_session_chars(
        self,
        messages: list[Message],
        max_chars: int,
    ) -> list[Message]:
        task_indexes = [
            index for index, message in enumerate(messages) if message.kind == "task"
        ]
        if not task_indexes:
            return list(messages)
        fixed = messages[: task_indexes[0]]
        blocks = [
            messages[start:end]
            for start, end in zip(task_indexes, [*task_indexes[1:], len(messages)])
        ]
        latest = blocks[-1]
        history = [
            normalized
            for block in blocks[:-1]
            if (normalized := self._normalize_task_block(block))
        ]
        normalized = [
            *fixed,
            *(message for block in history for message in block),
            *latest,
        ]
        if self._chars(normalized) <= max_chars:
            return normalized
        notice = Message("system", CONTEXT_COMPACTION_NOTICE)
        remaining = max_chars - self._chars([*fixed, notice, *latest])
        retained: list[list[Message]] = []
        for block in reversed(history):
            cost = self._chars(block)
            if cost > remaining:
                break
            retained.append(block)
            remaining -= cost
        return [
            *fixed,
            notice,
            *(message for block in reversed(retained) for message in block),
            *latest,
        ]

    def _prepare_token_budget(self, messages: list[Message]) -> list[Message]:
        task_indexes = [
            index for index, message in enumerate(messages) if message.kind == "task"
        ]
        if not task_indexes:
            return self._prepare_token_generic(messages)

        prefix = messages[: task_indexes[0]]
        blocks = [
            messages[start:end]
            for start, end in zip(task_indexes, [*task_indexes[1:], len(messages)])
        ]
        history = [
            normalized
            for block in blocks[:-1]
            if (normalized := self._normalize_task_block(block))
        ]
        latest = blocks[-1]
        latest_fixed = [latest[0], *(m for m in latest[1:] if m.role == "system")]
        latest_rounds = self._collect_current_groups(
            [m for m in latest[1:] if m.role != "system"]
        )
        normalized = [
            *prefix,
            *(message for block in history for message in block),
            *latest_fixed,
            *(message for group in latest_rounds for message in group),
        ]
        normalized_count = len(prefix) + sum(len(block) for block in history)
        normalized_count += len(latest_fixed) + sum(len(group) for group in latest_rounds)
        invalid_removed = normalized_count != len(messages)
        # 只有固定 system/task 时没有任何可安全删除的回合；沿用旧行为，
        # 允许这些不可丢消息自身超过预算，也不凭空插入压缩说明。
        if not history and not latest_rounds and not invalid_removed:
            return normalized
        if self._fits(normalized):
            return normalized

        normalized_tokens, normalized_source = self._estimate_with_anchor(tuple(normalized))
        provider_forces_drop = (
            normalized_source == "provider"
            and self.budget.max_tokens is not None
            and normalized_tokens > self.budget.max_tokens
        )

        notice = Message("system", CONTEXT_COMPACTION_NOTICE)
        base = [
            *prefix,
            notice,
            *latest_fixed,
            *(message for group in latest_rounds for message in group),
        ]
        candidates = list(history)
        retained_indexes: set[int] = set()
        current = list(base)
        for index in range(len(candidates) - 1, -1, -1):
            group = candidates[index]
            if not self._fits([*current, *group]):
                break
            retained_indexes.add(index)
            current.extend(group)
        # Provider 已证明原上下文超出 token 上限时，插入 notice 会使前缀变化，
        # 本地估算不能因此把完全相同的历史重新全部放行；至少删除最旧一组。
        if (
            provider_forces_drop
            and candidates
            and len(retained_indexes) == len(candidates)
        ):
            retained_indexes.remove(0)
        retained_history = [
            message
            for index, group in enumerate(candidates)
            if index in retained_indexes
            for message in group
        ]
        return [
            *prefix,
            notice,
            *retained_history,
            *latest_fixed,
            *(message for group in latest_rounds for message in group),
        ]

    def _prepare_token_generic(self, messages: list[Message]) -> list[Message]:
        fixed = list(messages[:2])
        later = list(messages[2:])
        rounds = self._collect_rounds(later)
        complete = sum(len(group) for group in rounds) == len(later)
        if complete and self._fits(messages):
            return list(messages)
        if not later:
            return fixed
        notice = Message("system", CONTEXT_COMPACTION_NOTICE)
        retained: list[list[Message]] = []
        current = [*fixed, notice]
        for group in reversed(rounds):
            if not self._fits([*current, *group]):
                break
            retained.append(group)
            current.extend(group)
        return [
            *fixed,
            notice,
            *(message for group in reversed(retained) for message in group),
        ]

    def _normalize_task_block(self, block: list[Message]) -> list[Message]:
        if not block or block[0].kind != "task":
            return []
        rounds = self._collect_rounds(block[1:])
        retained = [message for group in rounds for message in group]
        return [block[0], *retained] if retained else []

    def _collect_rounds(
        self,
        messages: list[Message],
        *,
        loose: bool = False,
    ) -> list[list[Message]]:
        rounds: list[list[Message]] = []
        index = 0
        while index < len(messages):
            end = self._complete_round_tail(messages, index, loose=loose)
            if end is None:
                index += 1
                continue
            rounds.append(messages[index:end])
            index = end
        return rounds

    def _collect_current_groups(self, messages: list[Message]) -> list[list[Message]]:
        """保留当前任务的完整工具回合与可恢复的协议纠错反馈。"""

        groups: list[list[Message]] = []
        index = 0
        while index < len(messages):
            end = self._complete_round_tail(messages, index)
            if end is not None:
                groups.append(messages[index:end])
                index = end
                continue
            message = messages[index]
            if (
                message.role == "assistant"
                and not message.tool_calls
                and index + 1 < len(messages)
                and messages[index + 1].kind == "protocol_feedback"
            ):
                groups.append(messages[index : index + 2])
                index += 2
                continue
            if message.kind == "protocol_feedback":
                groups.append([message])
            index += 1
        return groups

    def _complete_round_tail(
        self,
        messages: list[Message],
        index: int,
        *,
        loose: bool = False,
    ) -> int | None:
        assistant = messages[index]
        if assistant.role != "assistant":
            return None
        result_count = len(assistant.tool_calls) or 1
        end = index + 1 + result_count
        if end > len(messages):
            return None
        results = messages[index + 1 : end]
        for protocol in self._protocols:
            complete_round = (
                protocol.complete_round_loose if loose else protocol.complete_round
            )
            if assistant.tool_calls:
                complete = all(
                    complete_round(
                        replace(assistant, tool_calls=(call,)),
                        result,
                    )
                    for call, result in zip(assistant.tool_calls, results)
                )
            else:
                complete = complete_round(assistant, results[0])
            if complete:
                return end
        return None

    def _fits(self, messages: Iterable[Message]) -> bool:
        copied = tuple(messages)
        if self.budget.max_chars is not None and self._chars(copied) > self.budget.max_chars:
            return False
        if self.budget.max_tokens is not None:
            estimated, _source = self._estimate_with_anchor(copied)
            if estimated > self.budget.max_tokens:
                return False
        return True

    def _estimate_with_anchor(
        self,
        messages: tuple[Message, ...],
    ) -> tuple[int, Literal["provider", "estimate"]]:
        if (
            self._anchor_tokens is not None
            and len(messages) >= len(self._anchor_messages)
            and messages[: len(self._anchor_messages)] == self._anchor_messages
        ):
            tail = messages[len(self._anchor_messages) :]
            return self._anchor_tokens + self._estimate_tokens(tail), "provider"
        # 压缩说明只存在于发给 Provider 的视图，不写回 SessionContext。
        # 比较锚点时忽略该系统生成标记；真实 baseline 已包含它的成本，
        # 因而仍是安全的轻微高估。
        anchor_without_notice = tuple(
            message
            for message in self._anchor_messages
            if message.content != CONTEXT_COMPACTION_NOTICE
        )
        messages_without_notice = tuple(
            message
            for message in messages
            if message.content != CONTEXT_COMPACTION_NOTICE
        )
        if (
            self._anchor_tokens is not None
            and len(messages_without_notice) >= len(anchor_without_notice)
            and messages_without_notice[: len(anchor_without_notice)]
            == anchor_without_notice
        ):
            tail = messages_without_notice[len(anchor_without_notice) :]
            return self._anchor_tokens + self._estimate_tokens(tail), "provider"
        return self._estimate_tokens(messages), "estimate"

    @staticmethod
    def _chars(messages: Iterable[Message]) -> int:
        return sum(message.character_budget() for message in messages)

    @staticmethod
    def _estimate_tokens(messages: Iterable[Message]) -> int:
        """无 tokenizer 时按 UTF-8 字节保守估算，并计入每条消息开销。"""

        total = 0
        for message in messages:
            parts = [message.role, message.content or "", message.tool_call_id or ""]
            for call in message.tool_calls:
                parts.extend(
                    (
                        call.id,
                        call.name,
                        json.dumps(call.arguments, ensure_ascii=False, sort_keys=True),
                    )
                )
            byte_count = len("".join(parts).encode("utf-8"))
            total += 4 + (byte_count + 2) // 3
        return total
