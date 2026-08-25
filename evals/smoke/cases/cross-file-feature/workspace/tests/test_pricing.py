import unittest

from pricing import final_price


class FinalPriceTests(unittest.TestCase):
    def test_applies_a_percentage_discount(self) -> None:
        self.assertEqual(80, final_price(100, 20))


if __name__ == "__main__":
    unittest.main()
