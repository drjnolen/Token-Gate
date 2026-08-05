import unittest

from verification_security import (
    build_wallet_ownership_message,
    canonical_sui_address,
)


class SuiWalletVerificationInputTests(unittest.TestCase):
    def test_normalizes_short_sui_addresses(self):
        self.assertEqual(canonical_sui_address("0x1"), "0x" + "0" * 63 + "1")

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
