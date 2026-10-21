import unittest


class Behavior(unittest.TestCase):
    def test_normal(self):
        import textkit
        from report import label_line
        self.assertEqual('hello',textkit.normalize_label(' Hello ')); self.assertEqual('hello',textkit.clean_label(' Hello ')); self.assertEqual('label=hello',label_line(' Hello '))

    def test_boundary(self):
        import textkit
        from report import label_line
        for f in (textkit.clean_label,textkit.normalize_label): self.assertEqual('',f(' \t'))
        self.assertEqual('label=',label_line(''))

    def test_regression(self):
        import importlib,textkit
        from unittest.mock import patch
        import report
        try:
            with patch.object(textkit,'normalize_label',wraps=textkit.normalize_label) as called:
                importlib.reload(report)
                for text in (' A ', '', ' Mixed Case '):
                    self.assertEqual('label='+text.strip().lower(), report.label_line(text))
                called.assert_called()
        finally:
            # Restore from-import bindings even when a candidate assertion fails.
            importlib.reload(report)
