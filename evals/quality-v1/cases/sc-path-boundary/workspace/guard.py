from pathlib import PurePosixPath

def is_safe(path: str) -> bool:
    candidate = PurePosixPath(path)
    return not candidate.is_absolute() and '..' not in candidate.parts
