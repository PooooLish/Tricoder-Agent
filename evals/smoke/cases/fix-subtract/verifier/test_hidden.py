import unittest

from calculator import subtract


class HiddenSubtractTests(unittest.TestCase):
    def test_subtracts_positive_numbers(self) -> None:
        self.assertEqual(5, subtract(7, 2))

    def test_subtracts_negative_numbers(self) -> None:
        self.assertEqual(-1, subtract(-3, -2))


if __name__ == "__main__":
    unittest.main()
