def apply_change(current: str, requested: str, approved: bool) -> str:
    return requested if approved else current
