"""上下文预算、压缩与大型工具结果的受控运行组件。"""

from tricoder.context.manager import (
    CONTEXT_COMPACTION_NOTICE,
    CompactionPlan,
    ContextBudget,
    ContextManager,
    ContextSnapshot,
    SaveCandidatePlan,
)
from tricoder.context.memory import (
    ArchivedMemoryItem,
    ConversationMemory,
    MemoryCapacityError,
    MemoryItem,
    MemoryValidationError,
    assign_message_sequences,
    conversation_memory_message,
    memory_from_json,
    memory_to_prompt_json,
    memory_to_json,
    memory_source_ids,
    merge_candidate,
    merge_review_candidate,
    validate_candidate,
)
from tricoder.context.spill import SpillError, SpillRecord, ToolResultSpillStore

__all__ = [
    "CONTEXT_COMPACTION_NOTICE",
    "CompactionPlan",
    "ContextBudget",
    "ContextManager",
    "ContextSnapshot",
    "SaveCandidatePlan",
    "ArchivedMemoryItem",
    "ConversationMemory",
    "MemoryCapacityError",
    "MemoryItem",
    "MemoryValidationError",
    "assign_message_sequences",
    "conversation_memory_message",
    "memory_from_json",
    "memory_to_prompt_json",
    "memory_to_json",
    "memory_source_ids",
    "merge_candidate",
    "merge_review_candidate",
    "validate_candidate",
    "SpillError",
    "SpillRecord",
    "ToolResultSpillStore",
]
