import unittest


class Behavior(unittest.TestCase):
    def test_public(self):
        import subprocess,sys
        p=subprocess.run([sys.executable,'cli.py','2','3'],capture_output=True,text=True)
        self.assertEqual((0,'5\n',''),(p.returncode,p.stdout,p.stderr))

