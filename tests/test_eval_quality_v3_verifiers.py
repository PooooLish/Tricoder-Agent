"""Each test is an independently copied workspace and actual hidden subprocess."""
import unittest

from quality_v3_support import NEW_CASES, REFERENCES, cases, isolated


class BaseCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = cases()


class OldCases(BaseCase):
    pass


class NewBatch1(BaseCase):
    pass


class NewBatch2(BaseCase):
    pass


class NewBatch3(BaseCase):
    pass


def check_variant(id, variant, files):
    def test(self):
        result, processes, public = isolated(self.cases[id], variant, files,
                                             public=variant not in ("initial","wrong_a","wrong_b"))
        self.assertTrue(processes, result)
        self.assertTrue(all(p["tests"] and not p["timed_out"] and not p["cleanup_failed"]
                            and not p["output_exceeded"] for p in processes), processes)
        self.assertNotEqual("error", result.status, result)
        hidden = result.verifications[0]
        if variant in ("initial","wrong_a","wrong_b"):
            self.assertEqual(1, hidden.exit_code, (id,variant,result))
            self.assertIn("verification_failed", result.failure_codes)
        else:
            self.assertEqual("passed", result.status, result)
            self.assertTrue(public and all(p["passed"] for p in public), processes)
            expected=tuple(sorted(p for p,content in files.items()
                                  if not (self.cases[id].source_dir/"workspace"/p).exists()
                                  or (self.cases[id].source_dir/"workspace"/p).read_bytes()!=content.encode("utf8")))
            self.assertEqual(expected, result.modified_files)
    return test


for id, refs in REFERENCES.items():
    index = list(NEW_CASES).index(id) if id in NEW_CASES else -1
    cls = OldCases if index < 0 else NewBatch1 if index < 3 else NewBatch2 if index < 7 else NewBatch3
    for variant in ("initial","correct","wrong_a","wrong_b"):
        setattr(cls, "test_"+id.replace("-","_")+"_"+variant,
                check_variant(id,variant,{} if variant=="initial" else refs[variant]))
    for label,files in refs["alternatives"].items():
        setattr(cls,"test_"+id.replace("-","_")+"_alternative_"+label,check_variant(id,"alternative_"+label,files))

# unittest discovers every class-valued module global, including loop aliases.
del cls
