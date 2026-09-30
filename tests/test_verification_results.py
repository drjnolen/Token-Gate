import unittest
from decimal import Decimal

from verification_results import (
    qualifying_holdings_summary,
    verification_success_message,
    verification_holdings_progress,
)


class VerificationResultTests(unittest.TestCase):
    def test_multi_wallet_progress_preserves_fractional_precision(self):
        progress = verification_holdings_progress(
            {'token': 'CITY', 'minimum_holding': '1000000'},
            {'token_balance': Decimal('999999.999999999'), 'wallet_count': 2},
        )
        self.assertIn('999,999.999999999 / 1,000,000 required', progress['message'])
        self.assertIn('across 2 registered wallets', progress['message'])

    def test_trait_progress_never_confuses_collection_and_trait_counts(self):
        progress = verification_holdings_progress(
            {'registration_mode': 'nft', 'nft_collection_id': 'NFT',
             'nft_threshold': 5, 'nft_trait_name': 'Faction', 'nft_trait_threshold': 3},
            {'nft_count': 7, 'trait_count': 2, 'wallet_count': 2},
        )
        self.assertIn('NFTs: 7 / 5 required', progress['message'])
        self.assertIn('Matching-trait NFTs: 2 / 3 required', progress['message'])

    def test_token_success_reports_exact_grouped_balance(self):
        summary = qualifying_holdings_summary(
            {"registration_mode": "token", "token": "0x2::city::CITY"},
            {"requirements_met": True, "token_balance": Decimal("5819000.000000")},
        )
        self.assertEqual(summary, {"token_balance": "5819000"})
        self.assertEqual(
            verification_success_message(summary),
            "Wallet verified and registered successfully. "
            "5,819,000 qualifying tokens found.",
        )

    def test_nft_success_reports_collection_count(self):
        summary = qualifying_holdings_summary(
            {"registration_mode": "nft", "nft_collection_id": "0x2::citizen::Citizen"},
            {"requirements_met": True, "nft_count": 5},
        )
        self.assertEqual(summary, {"nft_count": 5})
        self.assertEqual(
            verification_success_message(summary),
            "Wallet verified and registered successfully. 5 qualifying NFTs found.",
        )

    def test_trait_gate_reports_only_qualifying_trait_count(self):
        summary = qualifying_holdings_summary(
            {
                "registration_mode": "nft",
                "nft_collection_id": "0x2::citizen::Citizen",
                "nft_trait_name": "Faction",
            },
            {"requirements_met": True, "nft_count": 9, "trait_count": 3},
        )
        self.assertEqual(summary, {"nft_count": 3})

    def test_both_gate_reports_tokens_and_nfts(self):
        summary = qualifying_holdings_summary(
            {
                "registration_mode": "both",
                "token": "0x2::city::CITY",
                "nft_collection_id": "0x2::citizen::Citizen",
            },
            {"requirements_met": True, "token_balance": "1250000.5", "nft_count": 1},
        )
        self.assertEqual(
            verification_success_message(summary),
            "Wallet verified and registered successfully. "
            "1,250,000.5 qualifying tokens and 1 qualifying NFT found.",
        )

    def test_failed_or_empty_gate_does_not_claim_qualifying_holdings(self):
        summary = qualifying_holdings_summary(
            {"registration_mode": "token", "token": "0x2::city::CITY"},
            {"requirements_met": False, "token_balance": "12"},
        )
        self.assertEqual(summary, {})
        self.assertEqual(
            verification_success_message(summary),
            "Wallet verified and registered successfully.",
        )


if __name__ == "__main__":
    unittest.main()
