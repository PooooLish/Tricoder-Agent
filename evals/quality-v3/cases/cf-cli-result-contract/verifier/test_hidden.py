import unittest


class Behavior(unittest.TestCase):
    def test_normal(self):
        import subprocess,sys
        p=subprocess.run([sys.executable,'cli.py','2','3'],capture_output=True,text=True)
        self.assertEqual((0,'5\n',''),(p.returncode,p.stdout,p.stderr))

    def test_boundary(self):
        import subprocess,sys
        for args in ([],['1'],['x','2'],['1','2','3'],['1','2','3','4']):
            with self.subTest(args=args):
                p=subprocess.run([sys.executable,'cli.py',*args],capture_output=True,text=True)
                self.assertEqual(2,p.returncode); self.assertEqual('',p.stdout); self.assertTrue(p.stderr.strip())

    def test_regression(self):
        import subprocess,sys
        from cli import main
        from contextlib import redirect_stdout,redirect_stderr
        from io import StringIO
        out=StringIO(); err=StringIO()
        with redirect_stdout(out),redirect_stderr(err): self.assertEqual(0,main(['-7','2']))
        self.assertEqual(('-5\n',''),(out.getvalue(),err.getvalue()))
        for args in ([],['1'],['bad','2'],['1','2','3'],['1','2','3','4']):
            with self.subTest(args=args):
                out=StringIO(); err=StringIO()
                with redirect_stdout(out),redirect_stderr(err):
                    code = main(args)
                self.assertIsInstance(code, int)
                self.assertEqual(2, code)
                self.assertEqual('', out.getvalue())
                self.assertTrue(err.getvalue().strip())
        p=subprocess.run([sys.executable,'cli.py','-7','2'],capture_output=True,text=True); self.assertEqual((0,'-5\n',''),(p.returncode,p.stdout,p.stderr))
