def value_for(values: dict[str, str], session_id: str) -> str:
    return next(iter(values.values()))
