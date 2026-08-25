import unittest

from calculator import subtract


class SubtractTests(unittest.TestCase):
    def test_subtracts_two_positive_numbers(self) -> None:
        self.assertEqual(5, subtract(7, 2))


if __name__ == "__main__":
    unittest.main()
