import unittest

from usernames import normalize_username


class HiddenNormalizeUsernameTests(unittest.TestCase):
    def test_rejects_non_string_values(self) -> None:
        with self.assertRaises(TypeError):
            normalize_username(42)  # type: ignore[arg-type]

    def test_rejects_blank_values(self) -> None:
        with self.assertRaises(ValueError):
            normalize_username(" \t ")

    def test_normalizes_valid_values(self) -> None:
        self.assertEqual("alice_42", normalize_username("  Alice_42  "))


if __name__ == "__main__":
    unittest.main()
