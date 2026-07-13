"""Regression guards for security-critical paths in the monolithic bot module.

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
            self.assertIn("verify_sui_personal_message_signature", source)
            self.assertIn("consume_verification_session", source)
            self.assertNotIn("_apply_verify_token_fallback", source)

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
