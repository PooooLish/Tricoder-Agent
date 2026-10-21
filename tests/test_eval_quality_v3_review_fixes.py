"""Q3 reviewer regressions through real public/hidden processes and scoring.

Only the Agent completion declaration is synthetic. Candidate source lives here,
never in the benchmark workspace. Each variant has an independent receipt.
"""
import unittest

from quality_v3_support import SUITE, cases, isolated


TIME_CORRECT = """from datetime import datetime
def sort_events(events):
    return sorted(events,key=lambda e: datetime.fromisoformat(e['timestamp']))
"""
TIME_ID_ONLY = """from datetime import datetime
def sort_events(events):
    return [{'id':e['id']} for e in sorted(events,key=lambda e: datetime.fromisoformat(e['timestamp']))]
"""


class ScoringCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = cases()

    def score(self, case_id, label, files, *, passed):
        result, processes, public = isolated(
            self.cases[case_id], "q3-" + label, files, public=True
        )
        expected_modified = {
            path for path, source in files.items()
            if (SUITE / "cases" / case_id / "workspace" / path).read_bytes()
            != source.encode("utf8")
        }
        self.assertEqual(expected_modified, set(result.modified_files))
        self.assertEqual(1, len(public))
        self.assertTrue(public[0]["passed"], public)
        self.assertEqual(2, len(processes))
        self.assertEqual([1, 3], [p["tests"] for p in processes])
        for process in processes:
            self.assertFalse(process["timed_out"], process)
            self.assertFalse(process["cleanup_failed"], process)
            self.assertFalse(process["output_exceeded"], process)
            self.assertNotIn("ERROR:", process["output"])
            self.assertNotIn("ImportError", process["output"])
        self.assertEqual(1, len(result.verifications))
        self.assertIsNone(result.verifications[0].error_code)
        self.assertEqual("passed" if passed else "failed", result.status, processes)
        self.assertEqual(() if passed else ("verification_failed",), result.failure_codes)
        self.assertEqual(0 if passed else 1, result.verifications[0].exit_code)
        if not passed:
            self.assertIn("FAIL:", processes[-1]["output"])
        return result


class TimeRecordTests(ScoringCase):
    def test_reviewer_time_control(self):
        self.score("sf-aware-time-order", "time-control",
                   {"time_order.py": TIME_CORRECT}, passed=True)

    def test_reviewer_id_only_is_rejected(self):
        self.score("sf-aware-time-order", "time-drops-record-fields",
                   {"time_order.py": TIME_ID_ONLY}, passed=False)

    def test_nested_field_loss_is_rejected(self):
        source = TIME_CORRECT.replace(
            "return sorted(events,key=lambda e: datetime.fromisoformat(e['timestamp']))",
            "return [{k:v for k,v in e.items() if k != 'nested'} "
            "for e in sorted(events,key=lambda e: datetime.fromisoformat(e['timestamp']))]",
        )
        self.score("sf-aware-time-order", "time-drops-nested",
                   {"time_order.py": source}, passed=False)

    def test_complete_deepcopy_is_accepted(self):
        source = "from copy import deepcopy\n" + TIME_CORRECT.replace(
            "sorted(events,", "sorted(deepcopy(events),"
        )
        self.score("sf-aware-time-order", "time-deepcopy",
                   {"time_order.py": source}, passed=True)

    def test_complete_shallow_copies_are_accepted(self):
        source = TIME_CORRECT.replace("sorted(events,", "sorted([dict(e) for e in events],")
        self.score("sf-aware-time-order", "time-shallow-copy",
                   {"time_order.py": source}, passed=True)


CLI_TEMPLATE = """import sys
from arithmetic import add
def main(argv):
    if LENGTH_CHECK:
        print('expected two integers',file=sys.stderr)
        return 2
    try:
        a,b=int(argv[0]),int(argv[1])
    except ValueError:
        print('invalid integer',file=sys.stderr)
        return 2
    print(add(a,b))
    return 0
if __name__=='__main__':
    raise SystemExit(main(sys.argv[1:]))
"""
CLI_CORRECT = CLI_TEMPLATE.replace("LENGTH_CHECK", "len(argv)!=2")
CLI_EXTRA = CLI_TEMPLATE.replace("LENGTH_CHECK", "len(argv)<2")


class CLIArgumentTests(ScoringCase):
    def check_cli(self, label, source, *, passed=False):
        self.score("cf-cli-result-contract", label, {"cli.py": source}, passed=passed)

    def test_reviewer_cli_control(self):
        self.check_cli("cli-control", CLI_CORRECT, passed=True)

    def test_reviewer_extra_arguments_are_rejected(self):
        self.check_cli("cli-ignores-extra-arguments", CLI_EXTRA)

    def test_main_bad_entry_good_is_rejected(self):
        source = CLI_EXTRA.replace(
            "    raise SystemExit(main(sys.argv[1:]))",
            "    if len(sys.argv[1:])!=2:\n"
            "        print('expected two integers',file=sys.stderr)\n"
            "        raise SystemExit(2)\n"
            "    raise SystemExit(main(sys.argv[1:]))",
        )
        self.check_cli("cli-main-bad-entry-good", source)

    def test_main_good_entry_bad_is_rejected(self):
        source = CLI_CORRECT.replace("main(sys.argv[1:])", "main(sys.argv[1:3])")
        self.check_cli("cli-main-good-entry-bad", source)

    def test_four_arguments_are_checked(self):
        source = CLI_TEMPLATE.replace("LENGTH_CHECK", "len(argv)<2 or len(argv)==3")
        self.check_cli("cli-ignores-four-arguments", source)

    def test_integer_enum_exit_codes_are_accepted(self):
        source = (
            "from enum import IntEnum\n"
            "class ExitCode(IntEnum):\n    INVALID=2\n"
            + CLI_CORRECT.replace("return 2", "return ExitCode.INVALID")
        )
        self.check_cli("cli-integer-enum", source, passed=True)


CACHE = """from copy import deepcopy
class RecordCache:
    def __init__(self): self.storage={}
    def put(self,key,value): self.storage[key]=deepcopy(value)
    def get(self,key): return deepcopy(self.storage[key])
"""
CACHE_ONCE = "def load_record(cache,key):\n    return cache.get(key)\n"
CACHE_TWICE = (
    "def load_record(cache,key):\n"
    "    return cache.get(key) if cache.get(key) is not None else {}\n"
)
EXPORTS = "from .core import normalize_label\nclean_label=normalize_label\n"
REPORT_ONCE = (
    "from textkit import normalize_label\n"
    "def label_line(text):\n    return 'label='+normalize_label(text)\n"
)
REPORT_TWICE = (
    "from textkit import normalize_label\n"
    "def label_line(text):\n"
    "    return 'label='+normalize_label(text) if normalize_label(text) else 'label='\n"
)


class TargetCallTests(ScoringCase):
    def cache(self, label, service, *, cache=CACHE, passed=True):
        self.score("cf-cache-copy-isolation", label,
                   {"cache.py": cache, "service.py": service}, passed=passed)

    def package(self, label, report, *, passed=True):
        self.score("cf-package-api-migration", label,
                   {"textkit/__init__.py": EXPORTS, "report.py": report}, passed=passed)

    def test_reviewer_cache_control(self):
        self.cache("cache-control", CACHE_ONCE)

    def test_reviewer_two_get_calls_are_accepted(self):
        self.cache("cache-valid-two-get-calls", CACHE_TWICE)

    def test_slots_are_accepted(self):
        self.cache("cache-slots", CACHE_ONCE, cache=CACHE.replace(
            "class RecordCache:\n", "class RecordCache:\n    __slots__=('storage',)\n"
        ))

    def test_keyword_get_is_accepted(self):
        self.cache("cache-keyword", CACHE_ONCE.replace("get(key)", "get(key=key)"))

    def test_slots_keyword_multiple_gets_are_accepted(self):
        self.cache("cache-slots-keyword-multiple",
                   CACHE_TWICE.replace("get(key)", "get(key=key)"),
                   cache=CACHE.replace(
                       "class RecordCache:\n", "class RecordCache:\n    __slots__=('storage',)\n"
                   ))

    def test_bypassing_get_is_rejected(self):
        self.cache("cache-bypass-get",
                   "from copy import deepcopy\n"
                   "def load_record(cache,key):\n    return deepcopy(cache.storage[key])\n",
                   passed=False)

    def test_get_on_another_instance_is_rejected(self):
        self.cache("cache-wrong-instance",
                   "from copy import deepcopy\n"
                   "from cache import RecordCache\n"
                   "def load_record(cache,key):\n"
                   "    other=RecordCache()\n"
                   "    other.put(key,cache.storage[key])\n"
                   "    return other.get(key)\n", passed=False)

    def test_get_on_another_key_is_rejected(self):
        self.cache("cache-wrong-key",
                   "from copy import deepcopy\n"
                   "def load_record(cache,key):\n"
                   "    try: cache.get('__other_key__')\n"
                   "    except KeyError: pass\n"
                   "    return deepcopy(cache.storage[key])\n", passed=False)

    def test_reviewer_package_control(self):
        self.package("package-control", REPORT_ONCE)

    def test_reviewer_two_export_calls_are_accepted(self):
        self.package("package-valid-two-export-calls", REPORT_TWICE)

    def test_strip_preprocessing_is_accepted(self):
        self.package("package-prestrip", REPORT_ONCE.replace(
            "normalize_label(text)", "normalize_label(text.strip())"
        ))

    def test_keyword_export_is_accepted(self):
        self.package("package-keyword", REPORT_ONCE.replace(
            "normalize_label(text)", "normalize_label(text=text)"
        ))

    def test_empty_shortcut_after_preprocessing_is_accepted(self):
        source = REPORT_ONCE.replace(
            "    return 'label='+normalize_label(text)",
            "    text=text.strip()\n"
            "    return 'label='+normalize_label(text) if text else 'label='",
        )
        self.package("package-empty-shortcut", source)

    def test_bypassing_export_via_core_is_rejected(self):
        self.package("package-bypass-core", REPORT_ONCE.replace(
            "from textkit import", "from textkit.core import"
        ), passed=False)

    def test_bypassing_export_via_old_alias_is_rejected(self):
        self.package("package-bypass-old", REPORT_ONCE.replace(
            "normalize_label", "clean_label"
        ), passed=False)
