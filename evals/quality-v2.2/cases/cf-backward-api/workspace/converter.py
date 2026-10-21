def to_slug(text: str) -> str:
    return '-'.join(text.lower().split())
