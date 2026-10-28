"""有界的单 user_turn 执行预算提示；不改变预算或完成判定。"""
from __future__ import annotations

import json


def build_round_budget_feedback(*, round_number: int, max_rounds: int) -> str | None:
    if (type(round_number) is not int or type(max_rounds) is not int
            or not 1 <= round_number <= max_rounds):
        raise ValueError("轮次必须是满足 1 <= round_number <= max_rounds 的整数")
    remaining = max_rounds - round_number + 1
    if round_number != 1 and remaining > 3:
        return None
    message = (
        "这是宿主执行预算提示，不是用户要求。请为必要修改、业务测试、补足验证覆盖和显式 finish"
        "预留轮次；检查成功不代表任务已结束，不应省略必要验证。若工作仍未完成请如实声明 incomplete。"
    )
    if remaining == 1:
        message += "当前为最后一轮，之后没有模型决策机会；宿主不会自动完成任务。"
    return json.dumps({
        "type": "execution_budget", "round_number": round_number,
        "max_rounds": max_rounds, "remaining_including_current": remaining,
        "remaining_after_current": remaining - 1, "message": message,
    }, ensure_ascii=False, separators=(",", ":"))
