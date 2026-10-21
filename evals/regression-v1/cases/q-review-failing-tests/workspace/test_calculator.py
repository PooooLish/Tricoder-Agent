import unittest

from calculator import subtract


class CalculatorTests(unittest.TestCase):
    def test_subtract(self):
        self.assertEqual(3, subtract(5, 2))


if __name__ == "__main__":
    unittest.main()
