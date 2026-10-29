import json,sys
from pathlib import Path
from jsonl_reader import parse_lines

def main(argv):
    records,errors=parse_lines(Path(argv[0]).read_text(encoding='utf8'))
    print(json.dumps({'records':records,'error_lines':errors},ensure_ascii=False))
    return 0

if __name__=='__main__':
    raise SystemExit(main(sys.argv[1:]))
