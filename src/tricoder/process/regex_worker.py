"""固定隔离正则 helper：只消费有界 JSON 文本，不打开用户路径。"""
import json
import re
import sys


def main():
    raw = sys.stdin.buffer.read(32 * 1024 * 1024 + 1)
    if len(raw) > 32 * 1024 * 1024:
        return 2
    try:
        data = json.loads(raw)
        pattern = re.compile(data["query"])
        matches = []
        for path, text in data["files"]:
            for number, line in enumerate(text.splitlines(), 1):
                if len(line) <= 4096 and pattern.search(line) is not None:
                    matches.append(f"{path}:{number}: {line.strip()}")
                    if len(matches) == 200:
                        print(json.dumps({"matches": matches, "truncated": True}, ensure_ascii=True))
                        return 0
        print(json.dumps({"matches": matches, "truncated": False}, ensure_ascii=True))
        return 0
    except (ValueError, TypeError, KeyError, re.error):
        return 2


if __name__ == "__main__":
    sys.exit(main())
