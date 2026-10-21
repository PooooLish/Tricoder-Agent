def find_prefix(values: list[str], prefix: str) -> list[str]:
    return [value for value in values if value.startswith(prefix)]
