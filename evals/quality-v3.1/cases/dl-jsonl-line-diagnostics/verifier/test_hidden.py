import unittest


class Behavior(unittest.TestCase):
    def test_normal(self):
        from jsonl_reader import parse_lines
        self.assertEqual(([{'a':1},{'b':2}],[]),parse_lines('{"a":1}\n{"b":2}\n'))

    def test_boundary(self):
        from jsonl_reader import parse_lines
        self.assertEqual(([],[]),parse_lines(' \n\t'))
        self.assertEqual(([{'x':1}],[2,4,5]),parse_lines('\nBAD\n{"x":1}\n[]\nnull\n'))

    def test_regression(self):
        import subprocess,sys
        import json,tempfile
        from pathlib import Path
        from cli import main
        from contextlib import redirect_stdout
        from io import StringIO
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'input.jsonl'
            for text,code,expected in (('{"x":"汉"}\n',0,{'records':[{'x':'汉'}],'error_lines':[]}),('\nBAD\n{"x":2}\n',1,{'records':[{'x':2}],'error_lines':[2]})):
                path.write_text(text,encoding='utf8'); before=path.read_bytes()
                p=subprocess.run([sys.executable,'-X','utf8','cli.py',str(path)],capture_output=True,text=True,encoding='utf8')
                self.assertEqual(code,p.returncode); self.assertEqual(1,len(p.stdout.splitlines())); self.assertEqual(expected,json.loads(p.stdout)); self.assertEqual(before,path.read_bytes())
                out=StringIO()
                with redirect_stdout(out): self.assertEqual(code,main([str(path)]))
                self.assertEqual(expected,json.loads(out.getvalue()))

