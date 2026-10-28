import unittest


class Behavior(unittest.TestCase):
    def test_normal(self):
        from renderer import render
        self.assertEqual('[green]Hi[/green]',render('Hi','green'))

    def test_boundary(self):
        from renderer import render
        self.assertEqual('[x][/x]',render('','x')); self.assertEqual('[]a[/]',render('a',''))

    def test_regression(self):
        from renderer import render
        self.assertEqual('[red] A \n[/red]',render(' A \n','red')); self.assertEqual('[blue]B[/blue]',render('B','blue'))

