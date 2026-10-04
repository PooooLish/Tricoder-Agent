"""兼容入口；Textual 界面已迁至 :mod:`tricoder.presentation.tui`。"""

from tricoder.presentation.tui import (
    ApprovalScreen,
    OptionChoice,
    OptionListScreen,
    TextInputScreen,
    TricoderApp,
    TuiObserver,
)

__all__ = [
    "ApprovalScreen",
    "OptionChoice",
    "OptionListScreen",
    "TextInputScreen",
    "TricoderApp",
    "TuiObserver",
]
