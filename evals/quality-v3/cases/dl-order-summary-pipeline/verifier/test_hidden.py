import unittest


class Behavior(unittest.TestCase):
    def test_normal(self):
        from reader import read_orders
        from totals import summarize
        from pipeline import build_report
        text='customer,amount,status\na,10,paid\na,5,paid\nb,9,pending\n'
        self.assertEqual([{'customer':'a','amount':'10','status':'paid'},{'customer':'a','amount':'5','status':'paid'},{'customer':'b','amount':'9','status':'pending'}],read_orders(text)); self.assertEqual({'a':15},summarize(read_orders(text))); self.assertEqual({'a':15},build_report(text))

    def test_boundary(self):
        from pipeline import build_report
        from totals import summarize
        self.assertEqual({},build_report('customer,amount,status\n')); self.assertEqual({},summarize([]))
        self.assertEqual({'a':-3},build_report('customer,amount,status\na,-3,paid\nb,20,Paid\n'))

    def test_regression(self):
        from reader import read_orders
        from totals import summarize
        from pipeline import build_report
        text='customer,amount,status\n"a,b",10,paid\n"a,b",-4,paid\nz,0,paid\n'
        self.assertEqual('a,b',read_orders(text)[0]['customer']); self.assertEqual({'a,b':6,'z':0},summarize(read_orders(text))); self.assertEqual({'a,b':6,'z':0},build_report(text))

