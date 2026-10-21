from core import join


def render(name: str, prefix: str = "Hi") -> str:
    return join(name, prefix)
