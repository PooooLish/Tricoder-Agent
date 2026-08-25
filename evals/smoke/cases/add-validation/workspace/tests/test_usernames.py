import unittest

from usernames import normalize_username


class NormalizeUsernameTests(unittest.TestCase):
    def test_normalizes_a_valid_username(self) -> None:
        self.assertEqual("alice", normalize_username("  Alice  "))

    def test_rejects_blank_username(self) -> None:
        with self.assertRaises(ValueError):
            normalize_username("   ")


if __name__ == "__main__":
    unittest.main()
