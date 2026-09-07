"""上下文预算、压缩与大型工具结果的受控运行组件。"""

from tricoder.context.manager import (
    CONTEXT_COMPACTION_NOTICE,
    ContextBudget,
    ContextManager,
    ContextSnapshot,
)
from tricoder.context.spill import SpillError, SpillRecord, ToolResultSpillStore

__all__ = [
    "CONTEXT_COMPACTION_NOTICE",
    "ContextBudget",
    "ContextManager",
    "ContextSnapshot",
    "SpillError",
    "SpillRecord",
    "ToolResultSpillStore",
]
