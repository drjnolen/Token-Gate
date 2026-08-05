"""Regression guards for security-critical bot paths.

The application initializes its production database and Telegram client at
import time, so these tests intentionally inspect the parsed function bodies
instead of importing ``main``.  They lock in the control-flow guarantees that
would otherwise be easy to accidentally remove during a later refactor.
"""

import ast
from pathlib import Path
import unittest


TREE = ast.parse(Path("main.py").read_text(encoding="utf-8"))
FUNCTIONS = {
    node.name: ast.get_source_segment(Path("main.py").read_text(encoding="utf-8"), node)
    for node in ast.walk(TREE)
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
}


class EnforcementRegressionGuards(unittest.TestCase):
    def test_verification_paths_require_server_session_and_wallet_signature(self):
        for name in ("api_verify", "handle_wallet_webapp_data"):
            source = FUNCTIONS[name]
            self.assertIn("get_active_verification_session", source)
            self.assertIn("sui_gateway.verify_personal_message", source)
            self.assertIn("claim_verification_session", source)
            self.assertIn("finalize_verified_wallet", source)
            self.assertIn("release_verification_session", source)
            self.assertNotIn("_apply_verify_token_fallback", source)

    def test_sessions_are_retryable_and_wallet_completion_is_atomic(self):
        claim = FUNCTIONS["claim_verification_session"]
        self.assertIn("status = 'processing'", claim)
        self.assertIn("INTERVAL '2 minutes'", claim)
        finalize = FUNCTIONS["finalize_verified_wallet"]
        self.assertIn("_save_wallet_for_user_with_cursor", finalize)
        self.assertIn("status = 'completed'", finalize)
        self.assertIn("FOR UPDATE", finalize)

    def test_runtime_and_browser_have_no_json_rpc_transport(self):
        runtime = Path("main.py").read_text(encoding="utf-8")
        template = Path("templates/verify.html").read_text(encoding="utf-8")
        for forbidden in (
            "SUI_RPC_URL",
            "sui_rpc_request",
            "suix_",
            "sui_getObject",
            '"jsonrpc"',
            "'jsonrpc'",
        ):
            self.assertNotIn(forbidden, runtime)
            self.assertNotIn(forbidden, template)

    def test_browser_submits_only_session_wallet_and_signature(self):
        template = Path("templates/verify.html").read_text(encoding="utf-8")
        self.assertIn("verification_session: VERIFICATION_SESSION", template)
        self.assertIn("wallet_address: selectedAddress", template)
        self.assertIn("wallet_signature: selectedSignature", template)
        self.assertNotIn("balance_verified", template)
        self.assertNotIn("token_balance:", template)
        self.assertNotIn("nft_count:", template)

    def test_admission_checks_are_tri_state(self):
        source = FUNCTIONS["evaluate_wallet_requirements"]
        self.assertIn('"indeterminate"', source)
        self.assertIn('"pass"', source)
        self.assertIn('"fail"', source)
        self.assertIn("trait_indeterminate = True", source)
        self.assertIn('"status": status', source)

    def test_wallet_uniqueness_uses_normalized_database_table(self):
        source = Path("main.py").read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE IF NOT EXISTS user_wallet_addresses", source)
        self.assertIn("UNIQUE (group_id, wallet_address)", source)
        self.assertIn("FROM user_wallet_addresses", FUNCTIONS["wallet_already_registered"])

    def test_graphql_move_json_trait_shapes_are_supported(self):
        namespace = {}
        exec(FUNCTIONS["_extract_traits"], namespace)
        extract_traits = namespace["_extract_traits"]
        obj = {
            "content": {
                "fields": {
                    "attributes": {
                        "contents": [
                            {"key": "Rarity", "value": "Legendary"},
                        ]
                    }
                }
            }
        }
        self.assertEqual(extract_traits(obj), {"rarity": "legendary"})
        direct = {
            "content": {
                "fields": {
                    "attributes": {"Faction": "Civic"},
                }
            }
        }
        self.assertEqual(extract_traits(direct), {"faction": "civic"})

    def test_graphql_personal_kiosk_cap_shape_is_supported(self):
        namespace = {}
        exec(FUNCTIONS["_extract_kiosk_id_from_personal_cap"], namespace)
        extract_kiosk = namespace["_extract_kiosk_id_from_personal_cap"]
        obj = {
            "content": {
                "fields": {
                    "cap": {
                        "vec": [
                            {"for": "0xkiosk"},
                        ]
                    }
                }
            }
        }
        self.assertEqual(extract_kiosk(obj), "0xkiosk")

    def test_returning_member_trait_gate_is_checked(self):
        source = FUNCTIONS["handle_chat_member_update"]
        self.assertIn("get_user_nft_trait_count", source)
        self.assertIn("get_user_nft_category_count", source)
        self.assertIn("trait_valid = trait_count >= nft_trait_threshold", source)

    def test_expired_subscriptions_skip_all_gate_enforcement(self):
        self.assertIn("Skipping periodic gate enforcement", FUNCTIONS["check_user_wallets"])
        self.assertIn("Skipping join-time gate enforcement", FUNCTIONS["handle_chat_member_update"])
        self.assertIn("group_has_active_subscription(group_id)", FUNCTIONS["check_user_wallets"])

    def test_scheduler_uses_a_single_worker_lease_and_continues_after_group_failure(self):
        source = FUNCTIONS["check_user_wallets"]
        self.assertIn("refresh_wallet_scheduler_lease", source)
        self.assertIn("retry_group_items", source)
        self.assertIn("Continuing periodic wallet scan", source)

    def test_stripe_session_claim_and_expiry_extension_share_one_transaction(self):
        source = FUNCTIONS["activate_subscription_from_stripe"]
        self.assertIn("INSERT INTO stripe_processed_events", source)
        self.assertIn("ON CONFLICT DO NOTHING RETURNING", source)
        self.assertIn("FOR UPDATE", source)
        self.assertIn("activate_subscription_from_stripe", FUNCTIONS["stripe_webhook"])

    def test_alert_cooldown_is_written_only_after_delivery(self):
        source = FUNCTIONS["check_user_wallets"]
        send_index = source.index("if send_low_holdings_alerts_to_admins")
        insert_index = source.index("INSERT INTO low_balance_alerts")
        self.assertLess(send_index, insert_index)

    def test_database_connection_logging_does_not_include_url_user_info(self):
        source = Path("main.py").read_text(encoding="utf-8")
        self.assertIn("credentials redacted", source)
        self.assertNotIn("connection_string.split('@')[0]", source)


if __name__ == "__main__":
    unittest.main()
