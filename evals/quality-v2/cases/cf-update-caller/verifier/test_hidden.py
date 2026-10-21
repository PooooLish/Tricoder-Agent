import unittest
from report import product_line

class HiddenTest(unittest.TestCase):
    def test_product(self): self.assertEqual('product=12', product_line(3, 4))
    def test_zero(self): self.assertEqual('product=0', product_line(0, 9))
