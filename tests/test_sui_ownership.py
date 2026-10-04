import unittest
from unittest.mock import Mock

from sui_ownership import (
    MAINNET_PERSONAL_CAP, canonical_move_type, collection_matches,
    normalize_collection, personal_cap_types, trusted_personal_cap,
)
from test_multi_wallet_verification import load


class OwnershipTests(unittest.TestCase):
    def test_addresses_normalize_but_identifiers_remain_case_sensitive(self):
        self.assertTrue(collection_matches("0x01::nft::NFT", "0x1::nft::NFT"))
        self.assertFalse(collection_matches("0x1::nft::Nft", "0x1::nft::NFT"))
        self.assertFalse(collection_matches("0x1::NFT::NFT", "0x1::nft::NFT"))

    def test_nested_types_suffixes_and_partial_names_are_not_collection_members(self):
        for candidate in ("0x9::wrapper::Fake<0x1::nft::NFT>", "0x1::nft::NFTFake",
                          "0x1::nft::NFT<u8>"):
            with self.subTest(candidate=candidate):
                self.assertFalse(collection_matches(candidate, "0x1::nft::NFT"))
        with self.assertRaises(ValueError):
            normalize_collection("nft")

    def test_package_matches_only_outer_package_and_excludes_coins(self):
        self.assertTrue(collection_matches("0x1::nft::NFT", "0x01"))
        self.assertFalse(collection_matches("0x9::wrap::NFT<0x1::nft::NFT>", "0x1"))
        self.assertFalse(collection_matches("0x2::coin::Coin<0x1::nft::NFT>", "0x2"))

    def test_complete_generic_type_matches_all_arguments(self):
        actual = "0x1::nft::NFT<0x2::sui::SUI,vector<u8>>"
        self.assertTrue(collection_matches(actual, "0x01::nft::NFT<0x02::sui::SUI, vector<u8>>"))
        self.assertFalse(collection_matches(actual, "0x1::nft::NFT<0x3::sui::SUI,vector<u8>>"))

    def test_malformed_type_cannot_fall_back_to_substring(self):
        for value in ("0x1::nft", "0x1::nft::NFT<>", "0x1::nft::NFT<u8", "0x1::nft::NFT!",
                      "0x" + "1" * 65 + "::nft::NFT"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_collection(value)

    def test_caps_require_reviewed_package_not_just_struct_name(self):
        trusted = personal_cap_types()
        self.assertTrue(trusted_personal_cap(MAINNET_PERSONAL_CAP, trusted))
        self.assertFalse(trusted_personal_cap("0x9::personal_kiosk::PersonalKioskCap", trusted))
        self.assertFalse(trusted_personal_cap("0x9::fake::Wrap<" + MAINNET_PERSONAL_CAP + ">", trusted))
        self.assertTrue(trusted_personal_cap("0x9::personal_kiosk::PersonalKioskCap",
                        personal_cap_types("0x9::personal_kiosk::PersonalKioskCap")))

    def test_forged_cap_never_provides_a_victim_kiosk(self):
        gateway = Mock()
        gateway.iter_owned_objects.return_value = [
            {"type": "0x9::personal_kiosk::PersonalKioskCap",
             "content": {"fields": {"cap": {"vec": [{"for": "0xabc"}]}}}},
            {"type": MAINNET_PERSONAL_CAP,
             "content": {"fields": {"cap": {"vec": [{"for": "0xdef"}]}}}},
        ]
        ns = load("_fetch_personal_kiosk_ids", "_extract_kiosk_id_from_personal_cap",
                  sui_gateway=gateway, SUI_MAX_PAGES=10, SUI_MAX_OBJECTS=100,
                  trusted_personal_cap=trusted_personal_cap, _PERSONAL_KIOSK_CAP_TYPES=personal_cap_types())
        self.assertEqual(ns["_fetch_personal_kiosk_ids"]("0x1"), ["0xdef"])

    def test_kiosk_and_direct_reads_share_exact_type_policy(self):
        gateway = Mock()
        owner_cap = canonical_move_type("0x2::kiosk::KioskOwnerCap")
        gateway.iter_owned_objects.return_value = [{"type": owner_cap, "content": {"fields": {"for": "0x3"}}}]
        gateway.iter_dynamic_fields.return_value = [
            {"name": {"type": {"repr": "0x2::kiosk::Item"}}, "value": {
                "address": "0x4", "contents": {"type": {"repr": "0x9::wrapper::Fake<0x1::nft::NFT>"}}}},
            {"name": {"type": {"repr": "0x9::kiosk::Item"}}, "value": {
                "address": "0x5", "contents": {"type": {"repr": "0x1::nft::NFT"}}}},
            {"name": {"type": {"repr": "0x2::kiosk::Item"}}, "value": {
                "address": "0x6", "contents": {"type": {"repr": "0x1::nft::NFT"}}}},
        ]
        ns = load("_fetch_kiosk_nfts", sui_gateway=gateway,
                  _normalize_collection_id=normalize_collection, collection_matches=collection_matches,
                  trusted_personal_cap=trusted_personal_cap, _KIOSK_OWNER_CAP_TYPE=owner_cap,
                  _KIOSK_ITEM_TYPE=canonical_move_type("0x2::kiosk::Item"),
                  SUI_MAX_PAGES=10, SUI_MAX_OBJECTS=100, _fetch_personal_kiosk_ids=Mock(return_value=[]))
        self.assertEqual([n["objectId"] for n in ns["_fetch_kiosk_nfts"](["0x1"], "0x1::nft::NFT")], ["0x6"])


if __name__ == "__main__":
    unittest.main()
