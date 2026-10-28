"""Offline test-only references and isolation helpers; never shipped in fixtures."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUITE = ROOT / "evals/quality-v3"
OLD = ROOT / "evals/quality-v2.2"
NEW_CASES = {
    "sf-csv-records": "single_file",
    "sf-aware-time-order": "single_file",
    "cf-config-precedence": "cross_file",
    "cf-cli-result-contract": "cross_file",
    "cf-record-schema-compat": "cross_file",
    "cf-page-merge": "cross_file",
    "cf-package-api-migration": "cross_file",
    "dl-jsonl-line-diagnostics": "delivery",
    "mm-partial-revision": "memory",
    "cf-cache-copy-isolation": "cross_file",
    "dl-order-summary-pipeline": "delivery",
}

# Helpers call the production isolation, verifier process and scoring path.
from dataclasses import asdict
import json
import os
import re
import tempfile
from unittest.mock import patch

from tricoder.evals.loader import load_suite
from tricoder.evals.runner import run_isolated_case
from tricoder.models import RunResult
import tricoder.evals.runner as runner


def cases(suite=SUITE):
    return {c.id: c for c in load_suite(suite).cases if c.execution_kind == "quality"}


def write_files(workspace, files):
    for rel, content in files.items():
        path = workspace / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf8", newline="\n")


def isolated(case, label, files, *, ok=True, executor=None, public=False, workspace_prefix=None):
    """Capture real subprocess receipts, including counts discarded by score API."""
    receipts = []
    real = runner.run_bounded_process
    def record(*args, **kwargs):
        result = real(*args, **kwargs)
        output = result.stdout + "\n" + result.stderr
        found = re.search(r"Ran (\d+) tests? in ", output)
        receipts.append(dict(command=list(args[0]), exit_code=result.returncode,
                             tests=int(found[1]) if found else None,
                             timed_out=result.timed_out, cleanup_failed=result.cleanup_failed,
                             output_exceeded=result.output_exceeded, output=output))
        return result
    public_receipts = []
    executor_errors = []
    def execute(c, workspace, audit):
        if executor is not None:
            try:
                return executor(c, workspace, audit)
            except Exception as exc:
                executor_errors.append(exc)
                raise
        write_files(workspace, files)
        if public:
            from tricoder.evals.models import VerificationSpec
            spec=VerificationSpec("public", "python -m unittest discover -s tests -v", 20)
            public_receipts.append(asdict(runner._run_verification(spec, workspace)))
        return RunResult(ok, "offline variant (not a model run)", 1, 1, (), "通过")
    target = os.environ.get("TRICODER_V3_EVIDENCE")
    if target:
        root=Path(target); root.mkdir(parents=True,exist_ok=True)
        temporary=tempfile.TemporaryDirectory(
            prefix=workspace_prefix if workspace_prefix is not None else case.id+"-"+label+"-", dir=root)
    else:
        temporary=tempfile.TemporaryDirectory(prefix="tricoder-v3-",dir=ROOT/"runtime")
    with temporary as directory, patch.object(runner,"run_bounded_process",side_effect=record):
        result=run_isolated_case(case,Path(directory).resolve(),execute)
    evidence=dict(case=case.id,variant=label,result=asdict(result),processes=receipts,public=public_receipts)
    if target:
        path=Path(target)/(case.id+"-"+label+".json")
        with path.open("x",encoding="utf8",newline="\n") as stream:
            json.dump(evidence,stream,ensure_ascii=False,indent=2)
    if executor_errors:
        raise executor_errors[0]
    return result,receipts,public_receipts
# GENERATED REFERENCES (test-only)
REFERENCES = {'sf-subtract': {'correct': {'math_ops.py': 'def subtract(a, b):\n    return a - b\n'},
                 'wrong_a': {'math_ops.py': 'def subtract(a, b):\n    return abs(a-b)\n'},
                 'wrong_b': {'math_ops.py': 'def subtract(a, b):\n    return int(float(a)-float(b))\n'},
                 'alternatives': {}},
 'sf-tail-boundary': {'correct': {'sequence_ops.py': 'def tail(values, count):\n'
                                                     '    return values[-count:] if count else []\n'},
                      'wrong_a': {'sequence_ops.py': 'def tail(values, count):\n    return values[:count]\n'},
                      'wrong_b': {'sequence_ops.py': 'def tail(values, count):\n'
                                                     '    result=values[-count:] if count else []\n'
                                                     '    values.clear()\n'
                                                     '    return result\n'},
                      'alternatives': {}},
 'sf-empty-input': {'correct': {'stats_ops.py': 'def mean(values):\n'
                                                '    return sum(values)/len(values) if values else 0.0\n'},
                    'wrong_a': {'stats_ops.py': 'def mean(values):\n'
                                                '    return sum(values)//len(values) if values else 0.0\n'},
                    'wrong_b': {'stats_ops.py': 'def mean(values):\n'
                                                '    return sum(abs(v) for v in values)/len(values) if '
                                                'values else 0.0\n'},
                    'alternatives': {}},
 'sf-unique-stable': {'correct': {'collection_ops.py': 'def unique_stable(values):\n'
                                                       '    return list(dict.fromkeys(values))\n'},
                      'wrong_a': {'collection_ops.py': 'def unique_stable(values):\n'
                                                       '    return sorted(set(values))\n'},
                      'wrong_b': {'collection_ops.py': 'def unique_stable(values):\n'
                                                       '    return [v for i,v in enumerate(values) if not i '
                                                       'or values[i-1]!=v]\n'},
                      'alternatives': {'scan': {'collection_ops.py': 'def unique_stable(values):\n'
                                                                     '    result=[]\n'
                                                                     '    for value in values:\n'
                                                                     '        if value not in result: '
                                                                     'result.append(value)\n'
                                                                     '    return result\n'}}},
 'sf-negative-boundary': {'correct': {'limits.py': 'def non_negative(value):\n    return max(0,value)\n'},
                          'wrong_a': {'limits.py': 'def non_negative(value):\n    return abs(value)\n'},
                          'wrong_b': {'limits.py': 'def non_negative(value):\n'
                                                   '    return min(100,max(0,value))\n'},
                          'alternatives': {}},
 'cf-price-discount': {'correct': {'pricing.py': 'def final_price(price, discount_percent):\n'
                                                 '    return price*(1-discount_percent/100)\n'},
                       'wrong_a': {'pricing.py': 'def final_price(price, discount_percent):\n'
                                                 '    return price-discount_percent\n'},
                       'wrong_b': {'pricing.py': 'def final_price(price, discount_percent):\n'
                                                 '    return round(price*(1-discount_percent/100))\n'},
                       'alternatives': {}},
 'cf-parameter-validation': {'correct': {'validator.py': 'def valid_quantity(value):\n'
                                                         '    return 1 <= value <= 100\n'},
                             'wrong_a': {'validator.py': 'def valid_quantity(value):\n'
                                                         '    return 1 <= value < 100\n'},
                             'wrong_b': {'validator.py': 'def valid_quantity(value):\n'
                                                         '    return value >= 1\n'},
                             'alternatives': {}},
 'cf-module-interface': {'correct': {'formatter.py': 'def format_name(first,last):\n'
                                                     "    return (first+' '+last).title()\n"},
                         'wrong_a': {'formatter.py': 'def format_name(first,last):\n'
                                                     "    return (first+' '+last).upper()\n"},
                         'wrong_b': {'formatter.py': 'def format_name(first,last):\n'
                                                     "    return (last+' '+first).title()\n"},
                         'alternatives': {'caller': {'api.py': 'from formatter import format_name\n'
                                                               '\n'
                                                               'def display(first,last):\n'
                                                               "    return format_name(first+' '+last)\n"}}},
 'cf-update-caller': {'correct': {'report.py': 'from calculator import multiply\n'
                                               '\n'
                                               'def product_line(a,b):\n'
                                               "    return f'product={multiply(a,b)}'\n"},
                      'wrong_a': {'report.py': "def product_line(a,b):\n    return f'product={a+b}'\n"},
                      'wrong_b': {'report.py': 'from calculator import multiply\n'
                                               '\n'
                                               'def product_line(a,b):\n'
                                               "    return f'product={abs(multiply(a,b))}'\n"},
                      'alternatives': {}},
 'cf-backward-api': {'correct': {'converter.py': 'def to_slug(text):\n'
                                                 "    return '-'.join(text.lower().split())\n"
                                                 '\n'
                                                 'slugify = to_slug\n'},
                     'wrong_a': {'converter.py': 'def to_slug(text):\n'
                                                 "    return text.lower().replace(' ','-')\n"
                                                 '\n'
                                                 'slugify=to_slug\n'},
                     'wrong_b': {'converter.py': 'def to_slug(text):\n'
                                                 "    return '-'.join(text.lower().split())\n"
                                                 '\n'
                                                 'def slugify(text):\n'
                                                 '    return text\n'},
                     'alternatives': {'wrapper': {'converter.py': 'def to_slug(text):\n'
                                                                  "    return '-'.join(part.lower() for part "
                                                                  'in text.split())\n'
                                                                  '\n'
                                                                  'def slugify(text):\n'
                                                                  '    return to_slug(text)\n'}}},
 'rc-read-once': {'correct': {'parser.py': 'def parse_number(text):\n    return int(text)\n'},
                  'wrong_a': {'parser.py': 'def parse_number(text):\n    return abs(int(text))\n'},
                  'wrong_b': {'parser.py': 'def parse_number(text):\n    return int(float(text))\n'},
                  'alternatives': {}},
 'rc-invalid-args': {'correct': {'normalize.py': 'def normalize(text):\n    return text.strip().lower()\n'},
                     'wrong_a': {'normalize.py': 'def normalize(text):\n'
                                                 "    return ' '.join(text.lower().split())\n"},
                     'wrong_b': {'normalize.py': 'def normalize(text):\n    return text.strip().upper()\n'},
                     'alternatives': {}},
 'rc-provider-transient': {'correct': {'counter.py': 'def increment(value):\n    return value+1\n'},
                           'wrong_a': {'counter.py': 'def increment(value):\n    return abs(value)+1\n'},
                           'wrong_b': {'counter.py': 'def increment(value):\n'
                                                     '    return int(float(value)+1)\n'},
                           'alternatives': {}},
 'rc-search-truncated': {'correct': {'searcher.py': 'def find_prefix(values,prefix):\n'
                                                    '    return [v for v in values if '
                                                    'v.lower().startswith(prefix.lower())]\n'},
                         'wrong_a': {'searcher.py': 'def find_prefix(values,prefix):\n'
                                                    '    return [v.lower() for v in values if '
                                                    'v.lower().startswith(prefix.lower())]\n'},
                         'wrong_b': {'searcher.py': 'def find_prefix(values,prefix):\n'
                                                    '    return [v for v in dict.fromkeys(values) if '
                                                    'v.lower().startswith(prefix.lower())]\n'},
                         'alternatives': {}},
 'rc-patch-conflict': {'correct': {'tags.py': 'def replace_tag(text,old,new):\n'
                                              '    return text.replace(old,new)\n'},
                       'wrong_a': {'tags.py': 'def replace_tag(text,old,new):\n'
                                              '    return text.replace(old,new).strip()\n'},
                       'wrong_b': {'tags.py': 'def replace_tag(text,old,new):\n'
                                              '    return text.replace(old,new) if new else text\n'},
                       'alternatives': {}},
 'mm-early-constraint': {'correct': {'renderer.py': 'def render(text,color):\n'
                                                    "    return f'[{color}]{text}[/{color}]'\n"},
                         'wrong_a': {'renderer.py': 'def render(text,color):\n'
                                                    "    return f'[{color}]{text.strip()}[/{color}]'\n"},
                         'wrong_b': {'renderer.py': 'def render(text,color):\n'
                                                    "    return f'[red]{text}[/red]'\n"},
                         'alternatives': {}},
 'mm-latest-correction': {'correct': {'config.py': 'def select(initial,latest):\n    return latest\n'},
                          'wrong_a': {'config.py': 'def select(initial,latest):\n'
                                                   '    return latest or initial\n'},
                          'wrong_b': {'config.py': "def select(initial,latest):\n    return 'green'\n"},
                          'alternatives': {}},
 'mm-todo-once': {'correct': {'tasks.py': 'def pending_once(items,completed):\n'
                                          '    return list(dict.fromkeys(v for v in items if v not in '
                                          'completed))\n'},
                  'wrong_a': {'tasks.py': 'def pending_once(items,completed):\n'
                                          '    return sorted(set(items)-completed)\n'},
                  'wrong_b': {'tasks.py': 'def pending_once(items,completed):\n'
                                          '    return [v for v in items if v not in completed]\n'},
                  'alternatives': {}},
 'dl-regression': {'correct': {'divide.py': 'def divide(a,b):\n    return a/b if b else None\n'},
                   'wrong_a': {'divide.py': 'def divide(a,b):\n    return a//b if b else None\n'},
                   'wrong_b': {'divide.py': 'def divide(a,b):\n    return abs(a/b) if b else None\n'},
                   'alternatives': {}},
 'sf-csv-records': {'correct': {'csv_records.py': 'import csv\n'
                                                  'import io\n'
                                                  '\n'
                                                  'def parse_records(text):\n'
                                                  '    return '
                                                  "list(csv.reader(io.StringIO(text,newline='')))\n"},
                    'wrong_a': {'csv_records.py': 'def parse_records(text):\n'
                                                  "    return [line.split(',') for line in "
                                                  'text.splitlines()]\n'},
                    'wrong_b': {'csv_records.py': 'import csv\n'
                                                  'import io\n'
                                                  '\n'
                                                  'def parse_records(text):\n'
                                                  '    return [[v for v in row if v] for row in '
                                                  "csv.reader(io.StringIO(text,newline=''))]\n"},
                    'alternatives': {'dialect': {'csv_records.py': 'import csv\n'
                                                                   'from io import StringIO\n'
                                                                   '\n'
                                                                   'def parse_records(text):\n'
                                                                   "    stream=StringIO(text,newline='')\n"
                                                                   '    return [row for row in '
                                                                   "csv.reader(stream,dialect='excel')]\n"}}},
 'sf-aware-time-order': {'correct': {'time_order.py': 'from datetime import datetime\n'
                                                      '\n'
                                                      'def sort_events(events):\n'
                                                      '    return sorted(events,key=lambda e: '
                                                      "datetime.fromisoformat(e['timestamp'].replace('Z','+00:00')))\n"},
                         'wrong_a': {'time_order.py': 'def sort_events(events):\n'
                                                      '    return sorted(events,key=lambda '
                                                      "e:e['timestamp'])\n"},
                         'wrong_b': {'time_order.py': 'from datetime import datetime\n'
                                                      '\n'
                                                      'def sort_events(events):\n'
                                                      '    events.sort(key=lambda e: '
                                                      "datetime.fromisoformat(e['timestamp'].replace('Z','+00:00')))\n"
                                                      '    return events\n'},
                         'alternatives': {}},
 'cf-config-precedence': {'correct': {'settings.py': 'def resolve(defaults,file_values,cli_values):\n'
                                                     '    return {**defaults,**file_values,**cli_values}\n',
                                      'app.py': 'from settings import resolve\n'
                                                '\n'
                                                "DEFAULTS={'port':8000,'debug':False,'label':'app','token':None}\n"
                                                '\n'
                                                'def build_settings(file_values,cli_values):\n'
                                                '    return resolve(DEFAULTS,file_values,cli_values)\n'},
                          'wrong_a': {'settings.py': 'def resolve(defaults,file_values,cli_values):\n'
                                                     '    return {k:cli_values.get(k) or file_values.get(k) '
                                                     'or defaults.get(k) for k in '
                                                     'defaults.keys()|file_values.keys()|cli_values.keys()}\n',
                                      'app.py': 'from settings import resolve\n'
                                                '\n'
                                                "DEFAULTS={'port':8000,'debug':False,'label':'app','token':None}\n"
                                                '\n'
                                                'def build_settings(file_values,cli_values):\n'
                                                '    return resolve(DEFAULTS,file_values,cli_values)\n'},
                          'wrong_b': {'settings.py': 'def resolve(defaults,file_values,cli_values):\n'
                                                     '    defaults.update(file_values); '
                                                     'defaults.update(cli_values)\n'
                                                     '    return defaults\n',
                                      'app.py': 'from settings import resolve\n'
                                                '\n'
                                                "DEFAULTS={'port':8000,'debug':False,'label':'app','token':None}\n"
                                                '\n'
                                                'def build_settings(file_values,cli_values):\n'
                                                '    return resolve(DEFAULTS,file_values,cli_values)\n'},
                          'alternatives': {}},
 'cf-cli-result-contract': {'correct': {'cli.py': 'import sys\n'
                                                  'from arithmetic import add\n'
                                                  '\n'
                                                  'def main(argv):\n'
                                                  '    try:\n'
                                                  "        if len(argv)!=2: raise ValueError('two integers "
                                                  "required')\n"
                                                  '        a,b=map(int,argv)\n'
                                                  '    except ValueError:\n'
                                                  "        print('two integers required',file=sys.stderr)\n"
                                                  '        return 2\n'
                                                  '    print(add(a,b))\n'
                                                  '    return 0\n'
                                                  '\n'
                                                  "if __name__=='__main__':\n"
                                                  '    raise SystemExit(main(sys.argv[1:]))\n'},
                            'wrong_a': {'cli.py': 'import sys\n'
                                                  'from arithmetic import add\n'
                                                  '\n'
                                                  'def main(argv):\n'
                                                  '    try:\n'
                                                  "        if len(argv)!=2: raise ValueError('two integers "
                                                  "required')\n"
                                                  '        a,b=map(int,argv)\n'
                                                  '    except ValueError:\n'
                                                  "        print('two integers required',file=sys.stderr)\n"
                                                  '        return 2\n'
                                                  '    print(add(a,b))\n'
                                                  '    return 0\n'
                                                  '\n'
                                                  "if __name__=='__main__':\n"
                                                  '    main(sys.argv[1:])\n'},
                            'wrong_b': {'cli.py': 'import sys\n'
                                                  'from arithmetic import add\n'
                                                  '\n'
                                                  'def main(argv):\n'
                                                  '    try:\n'
                                                  "        if len(argv)!=2: raise ValueError('two integers "
                                                  "required')\n"
                                                  '        a,b=map(int,argv)\n'
                                                  '    except ValueError:\n'
                                                  "        print('two integers required',file=sys.stderr)\n"
                                                  '        return 2\n'
                                                  '    print(abs(add(a,b)))\n'
                                                  '    return 0\n'
                                                  '\n'
                                                  "if __name__=='__main__':\n"
                                                  '    raise SystemExit(main(sys.argv[1:]))\n'},
                            'alternatives': {}},
 'cf-record-schema-compat': {'correct': {'records.py': 'def normalize(record):\n'
                                                       "    if 'name' not in record and 'full_name' not in "
                                                       "record: raise ValueError('missing name')\n"
                                                       "    return {'id':record['id'],'name':record['name'] "
                                                       "if 'name' in record else record['full_name']}\n",
                                         'loader.py': 'import json\n'
                                                      'from records import normalize\n'
                                                      '\n'
                                                      'def load_records(text):\n'
                                                      '    return [normalize(r) for r in '
                                                      'json.loads(text)]\n'},
                             'wrong_a': {'records.py': 'def normalize(record):\n'
                                                       '    return '
                                                       "{'id':record['id'],'name':record.get('name') or "
                                                       "record['full_name']}\n",
                                         'loader.py': 'import json\n'
                                                      'from records import normalize\n'
                                                      '\n'
                                                      'def load_records(text):\n'
                                                      '    return [normalize(r) for r in '
                                                      'json.loads(text)]\n'},
                             'wrong_b': {'records.py': 'def normalize(record):\n'
                                                       "    if 'name' not in record and 'full_name' not in "
                                                       "record: raise ValueError('missing name')\n"
                                                       "    record['extra']='changed'\n"
                                                       "    return {'id':record['id'],'name':record['name'] "
                                                       "if 'name' in record else record['full_name']}\n",
                                         'loader.py': 'import json\n'
                                                      'from records import normalize\n'
                                                      '\n'
                                                      'def load_records(text):\n'
                                                      '    return [normalize(r) for r in '
                                                      'json.loads(text)]\n'},
                             'alternatives': {}},
 'cf-page-merge': {'correct': {'pagination.py': 'def merge_pages(pages):\n'
                                                '    seen=set(); result=[]\n'
                                                '    for page in pages:\n'
                                                '        for row in page:\n'
                                                "            if row['id'] not in seen:\n"
                                                "                seen.add(row['id']); result.append(row)\n"
                                                '    return result\n',
                               'service.py': 'from pagination import merge_pages\n'
                                             '\n'
                                             'def collect(pages):\n'
                                             '    return merge_pages(pages)\n'},
                   'wrong_a': {'pagination.py': 'def merge_pages(pages):\n'
                                                '    seen=set(); result=[]\n'
                                                '    for page in pages:\n'
                                                '        if not page: break\n'
                                                '        for row in page:\n'
                                                "            if row['id'] not in seen:\n"
                                                "                seen.add(row['id']); result.append(row)\n"
                                                '    return result\n',
                               'service.py': 'from pagination import merge_pages\n'
                                             '\n'
                                             'def collect(pages):\n'
                                             '    return merge_pages(pages)\n'},
                   'wrong_b': {'pagination.py': 'def merge_pages(pages):\n'
                                                "    return list({r['id']:r for page in pages for r in "
                                                'page}.values())\n',
                               'service.py': 'from pagination import merge_pages\n'
                                             '\n'
                                             'def collect(pages):\n'
                                             '    return merge_pages(pages)\n'},
                   'alternatives': {}},
 'cf-package-api-migration': {'correct': {'textkit/__init__.py': 'from .core import normalize_label\n'
                                                                 '\n'
                                                                 'clean_label=normalize_label\n',
                                          'report.py': 'from textkit import normalize_label\n'
                                                       '\n'
                                                       'def label_line(text):\n'
                                                       "    return 'label='+normalize_label(text)\n"},
                              'wrong_a': {'textkit/__init__.py': 'from .core import normalize_label\n'
                                                                 '\n'
                                                                 'def clean_label(text):\n'
                                                                 '    return text\n',
                                          'report.py': 'from textkit import normalize_label\n'
                                                       '\n'
                                                       'def label_line(text):\n'
                                                       "    return 'label='+normalize_label(text)\n"},
                              'wrong_b': {'textkit/__init__.py': 'from .core import normalize_label\n'
                                                                 '\n'
                                                                 'clean_label=normalize_label\n',
                                          'report.py': 'from textkit import clean_label\n'
                                                       '\n'
                                                       'def label_line(text):\n'
                                                       "    return 'label='+clean_label(text)\n"},
                              'alternatives': {}},
 'dl-jsonl-line-diagnostics': {'correct': {'jsonl_reader.py': 'import json\n'
                                                              '\n'
                                                              'def parse_lines(text):\n'
                                                              '    records=[]; errors=[]\n'
                                                              '    for number,line in '
                                                              'enumerate(text.splitlines(),1):\n'
                                                              '        if not line.strip(): continue\n'
                                                              '        try:\n'
                                                              '            value=json.loads(line)\n'
                                                              '            if not isinstance(value,dict): '
                                                              "raise ValueError('object required')\n"
                                                              '        except ValueError: '
                                                              'errors.append(number)\n'
                                                              '        else: records.append(value)\n'
                                                              '    return records,errors\n',
                                           'cli.py': 'import json,sys\n'
                                                     'from pathlib import Path\n'
                                                     'from jsonl_reader import parse_lines\n'
                                                     '\n'
                                                     'def main(argv):\n'
                                                     '    '
                                                     "records,errors=parse_lines(Path(argv[0]).read_text(encoding='utf8'))\n"
                                                     '    '
                                                     "print(json.dumps({'records':records,'error_lines':errors},ensure_ascii=False))\n"
                                                     '    return 1 if errors else 0\n'
                                                     '\n'
                                                     "if __name__=='__main__':\n"
                                                     '    raise SystemExit(main(sys.argv[1:]))\n'},
                               'wrong_a': {'jsonl_reader.py': 'import json\n'
                                                              '\n'
                                                              'def parse_lines(text):\n'
                                                              '    records=[]; errors=[]\n'
                                                              '    for number,line in enumerate((l for l in '
                                                              'text.splitlines() if l.strip()),1):\n'
                                                              '        if not line.strip(): continue\n'
                                                              '        try:\n'
                                                              '            value=json.loads(line)\n'
                                                              '            if not isinstance(value,dict): '
                                                              "raise ValueError('object required')\n"
                                                              '        except ValueError: '
                                                              'errors.append(number)\n'
                                                              '        else: records.append(value)\n'
                                                              '    return records,errors\n',
                                           'cli.py': 'import json,sys\n'
                                                     'from pathlib import Path\n'
                                                     'from jsonl_reader import parse_lines\n'
                                                     '\n'
                                                     'def main(argv):\n'
                                                     '    '
                                                     "records,errors=parse_lines(Path(argv[0]).read_text(encoding='utf8'))\n"
                                                     '    '
                                                     "print(json.dumps({'records':records,'error_lines':errors},ensure_ascii=False))\n"
                                                     '    return 1 if errors else 0\n'
                                                     '\n'
                                                     "if __name__=='__main__':\n"
                                                     '    raise SystemExit(main(sys.argv[1:]))\n'},
                               'wrong_b': {'jsonl_reader.py': 'import json\n'
                                                              '\n'
                                                              'def parse_lines(text):\n'
                                                              '    records=[]; errors=[]\n'
                                                              '    for number,line in '
                                                              'enumerate(text.splitlines(),1):\n'
                                                              '        if not line.strip(): continue\n'
                                                              '        try:\n'
                                                              '            value=json.loads(line)\n'
                                                              '            if not isinstance(value,dict): '
                                                              "raise ValueError('object required')\n"
                                                              '        except ValueError: '
                                                              'errors.append(number)\n'
                                                              '        else: records.append(value)\n'
                                                              '    return records,errors\n',
                                           'cli.py': 'import json,sys\n'
                                                     'from pathlib import Path\n'
                                                     'from jsonl_reader import parse_lines\n'
                                                     '\n'
                                                     'def main(argv):\n'
                                                     '    '
                                                     "records,errors=parse_lines(Path(argv[0]).read_text(encoding='utf8'))\n"
                                                     '    '
                                                     "print(json.dumps({'records':records,'error_lines':errors},ensure_ascii=False))\n"
                                                     '    return 0\n'
                                                     '\n'
                                                     "if __name__=='__main__':\n"
                                                     '    raise SystemExit(main(sys.argv[1:]))\n'},
                               'alternatives': {}},
 'mm-partial-revision': {'correct': {'formatter.py': 'def render(text,prefix):\n'
                                                     '    return text.strip().lower()\n'},
                         'wrong_a': {'formatter.py': 'def render(text,prefix):\n'
                                                     '    return prefix+text.strip().lower()\n'},
                         'wrong_b': {'formatter.py': 'def render(text,prefix):\n    return text.lower()\n'},
                         'alternatives': {}},
 'cf-cache-copy-isolation': {'correct': {'cache.py': 'from copy import deepcopy\n'
                                                     '\n'
                                                     'class RecordCache:\n'
                                                     '    def __init__(self): self._data={}\n'
                                                     '    def put(self,key,value): '
                                                     'self._data[key]=deepcopy(value)\n'
                                                     '    def get(self,key): return '
                                                     'deepcopy(self._data[key])\n',
                                         'service.py': 'def load_record(cache,key):\n'
                                                       '    return cache.get(key)\n'},
                             'wrong_a': {'cache.py': 'from copy import deepcopy\n'
                                                     '\n'
                                                     'class RecordCache:\n'
                                                     '    def __init__(self): self._data={}\n'
                                                     '    def put(self,key,value): '
                                                     'self._data[key]=value.copy()\n'
                                                     '    def get(self,key): return '
                                                     'deepcopy(self._data[key])\n',
                                         'service.py': 'def load_record(cache,key):\n'
                                                       '    return cache.get(key)\n'},
                             'wrong_b': {'cache.py': 'from copy import deepcopy\n'
                                                     '\n'
                                                     'class RecordCache:\n'
                                                     '    def __init__(self): self._data={}\n'
                                                     '    def put(self,key,value): '
                                                     'self._data[key]=deepcopy(value)\n'
                                                     '    def get(self,key): return self._data[key]\n',
                                         'service.py': 'def load_record(cache,key):\n'
                                                       '    return cache.get(key)\n'},
                             'alternatives': {'slots': {'cache.py': 'from copy import deepcopy\n'
                                                                    '\n'
                                                                    'class RecordCache:\n'
                                                                    "    __slots__=('_data',)\n"
                                                                    '    def __init__(self): self._data={}\n'
                                                                    '    def put(self,key,value): '
                                                                    'self._data[key]=deepcopy(value)\n'
                                                                    '    def get(self,key): return '
                                                                    'deepcopy(self._data[key])\n',
                                                        'service.py': 'def load_record(cache,key):\n'
                                                                      '    return cache.get(key)\n'}}},
 'dl-order-summary-pipeline': {'correct': {'reader.py': 'import csv,io\n'
                                                        '\n'
                                                        'def read_orders(text):\n'
                                                        '    return '
                                                        "list(csv.DictReader(io.StringIO(text,newline='')))\n",
                                           'totals.py': 'def summarize(rows):\n'
                                                        '    result={}\n'
                                                        '    for row in rows:\n'
                                                        "        if row['status']=='paid':\n"
                                                        "            key=row['customer']; "
                                                        "result[key]=result.get(key,0)+int(row['amount'])\n"
                                                        '    return result\n',
                                           'pipeline.py': 'from reader import read_orders\n'
                                                          'from totals import summarize\n'
                                                          '\n'
                                                          'def build_report(text):\n'
                                                          '    return summarize(read_orders(text))\n'},
                               'wrong_a': {'reader.py': 'import csv,io\n'
                                                        '\n'
                                                        'def read_orders(text):\n'
                                                        '    return '
                                                        "list(csv.DictReader(io.StringIO(text,newline='')))\n",
                                           'totals.py': 'def summarize(rows):\n'
                                                        '    result={}\n'
                                                        '    for row in rows:\n'
                                                        "        if row['status']=='paid':\n"
                                                        "            key=row['customer']; "
                                                        "result[key]=int(row['amount'])\n"
                                                        '    return result\n',
                                           'pipeline.py': 'from reader import read_orders\n'
                                                          'from totals import summarize\n'
                                                          '\n'
                                                          'def build_report(text):\n'
                                                          '    return summarize(read_orders(text))\n'},
                               'wrong_b': {'reader.py': 'def read_orders(text):\n'
                                                        '    lines=text.splitlines()\n'
                                                        '    return '
                                                        "[dict(zip(lines[0].split(','),line.split(','))) for "
                                                        'line in lines[1:]] if lines else []\n',
                                           'totals.py': 'def summarize(rows):\n'
                                                        '    result={}\n'
                                                        '    for row in rows:\n'
                                                        "        if row['status']=='paid':\n"
                                                        "            key=row['customer']; "
                                                        "result[key]=result.get(key,0)+int(row['amount'])\n"
                                                        '    return result\n',
                                           'pipeline.py': 'from reader import read_orders\n'
                                                          'from totals import summarize\n'
                                                          '\n'
                                                          'def build_report(text):\n'
                                                          '    return summarize(read_orders(text))\n'},
                               'alternatives': {}}}
