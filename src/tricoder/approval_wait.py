"""兼容入口；审批等待实现已迁至 :mod:`tricoder.presentation.approval_wait`。"""

from tricoder.presentation.approval_wait import ApprovalWait

__all__ = ["ApprovalWait"]
