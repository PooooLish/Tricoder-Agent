import unittest

from discounts import percentage_discount
from pricing import final_price


class HiddenDiscountTests(unittest.TestCase):
    def test_percentage_discount_returns_discounted_amount(self) -> None:
        self.assertEqual(75, percentage_discount(100, 25))

    def test_percentage_discount_rejects_invalid_bounds(self) -> None:
        for amount, percent in ((-1, 10), (100, -1), (100, 101)):
            with self.subTest(amount=amount, percent=percent):
                with self.assertRaises(ValueError):
                    percentage_discount(amount, percent)

    def test_final_price_uses_the_cross_file_discount(self) -> None:
        self.assertEqual(80, final_price(100, 20))
        self.assertEqual(100, final_price(100))

    def test_final_price_rejects_invalid_bounds(self) -> None:
        for amount, percent in ((-1, 0), (100, -1), (100, 101)):
            with self.subTest(amount=amount, percent=percent):
                with self.assertRaises(ValueError):
                    final_price(amount, percent)


if __name__ == "__main__":
    unittest.main()
