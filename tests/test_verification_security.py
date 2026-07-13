import base64
import hashlib
import unittest

from nacl.signing import SigningKey

from verification_security import (
    SUI_ED25519_SCHEME_FLAG,
    build_wallet_ownership_message,
    canonical_sui_address,
    sui_personal_message_digest,
    verify_sui_personal_message_signature,
)


class SuiWalletSignatureTests(unittest.TestCase):
    def setUp(self):
        self.key = SigningKey.generate()
        self.public_key = bytes(self.key.verify_key)
        self.address = "0x" + hashlib.blake2b(
            bytes([SUI_ED25519_SCHEME_FLAG]) + self.public_key, digest_size=32
        ).hexdigest()
        self.message = build_wallet_ownership_message("session-abc", -100123, 12345, self.address)
        signature = self.key.sign(sui_personal_message_digest(self.message)).signature
        self.serialized_signature = base64.b64encode(
            bytes([SUI_ED25519_SCHEME_FLAG]) + signature + self.public_key
        ).decode("ascii")

    def test_accepts_valid_signature_for_matching_wallet_and_session(self):
        self.assertTrue(
            verify_sui_personal_message_signature(
                self.address, self.message, self.serialized_signature
            )
        )

    def test_rejects_replayed_signature_for_another_session(self):
        other_message = build_wallet_ownership_message("session-other", -100123, 12345, self.address)
        self.assertFalse(
            verify_sui_personal_message_signature(
                self.address, other_message, self.serialized_signature
            )
        )

    def test_rejects_signature_when_public_key_does_not_match_claimed_wallet(self):
        self.assertFalse(
            verify_sui_personal_message_signature(
                "0x1", self.message, self.serialized_signature
            )
        )

    def test_normalizes_short_sui_addresses(self):
        self.assertEqual(canonical_sui_address("0x1"), "0x" + "0" * 63 + "1")


if __name__ == "__main__":
    unittest.main()
