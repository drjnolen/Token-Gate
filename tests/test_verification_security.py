import unittest

from verification_security import (
    build_wallet_ownership_message,
    canonical_sui_address,
    is_valid_verification_session_id,
)


class SuiWalletVerificationInputTests(unittest.TestCase):
    def test_verification_session_ids_are_bounded_url_safe_secrets(self):
        self.assertTrue(is_valid_verification_session_id("a" * 43))
        self.assertTrue(is_valid_verification_session_id("A_b-9" * 8))
        self.assertFalse(is_valid_verification_session_id("short"))
        self.assertFalse(is_valid_verification_session_id("a" * 129))
        self.assertFalse(is_valid_verification_session_id("a" * 40 + "?"))

    def test_normalizes_short_sui_addresses(self):
        self.assertEqual(canonical_sui_address("0x1"), "0x" + "0" * 63 + "1")

    def test_rejects_punctuation_whitespace_unicode_and_oversized_addresses(self):
        invalid = (
            "0x1-2",
            "0x1 2",
            "0x１２",
            "0x" + "a" * 65,
            123,
        )
        for address in invalid:
            with self.subTest(address=address):
                self.assertIsNone(canonical_sui_address(address))

    def test_rejects_invalid_and_overlong_addresses(self):
        self.assertIsNone(canonical_sui_address("1"))
        self.assertIsNone(canonical_sui_address("0xzz"))
        self.assertIsNone(canonical_sui_address("0x" + "1" * 65))

    def test_ownership_message_binds_session_user_group_and_canonical_wallet(self):
        message = build_wallet_ownership_message(
            "session-abc",
            -100123,
            12345,
            "0x1",
        )
        self.assertEqual(
            message,
            "Token Gate wallet ownership verification\n"
            "Session: session-abc\n"
            "Telegram user: 12345\n"
            "Group: -100123\n"
            f"Wallet: 0x{'0' * 63}1",
        )


if __name__ == "__main__":
    unittest.main()
