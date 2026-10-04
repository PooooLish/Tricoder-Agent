import unittest
from renderer import render

class HiddenTest(unittest.TestCase):
    def test_constraint(self): self.assertEqual('[red]notice[/red]', render('notice', 'red'))
    def test_other(self): self.assertEqual('[green]ok[/green]', render('ok', 'green'))
