from pathlib import PurePosixPath

def may_read(path: str) -> bool:
    name = PurePosixPath(path).name.lower()
    return name not in {'.env', '.env.local', 'credentials.json'}
