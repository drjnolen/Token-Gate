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
        for name in ("api_verify",):
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
        self.assertIn("claim_id = %s", claim)
        self.assertIn("VERIFICATION_PROCESSING_LEASE_SECONDS", claim)
        finalize = FUNCTIONS["finalize_verified_wallet"]
        self.assertIn("_save_wallet_for_user_with_cursor", finalize)
        self.assertIn("status = 'completed'", finalize)
        self.assertIn("FOR UPDATE", finalize)
        self.assertIn("AND claim_id = %s", finalize)
        self.assertIn("DELETE FROM pending_verifications", finalize)
        self.assertIn("holdings_summary = %s::jsonb", finalize)
        self.assertNotIn("expires_at > NOW()", finalize)
        release = FUNCTIONS["release_verification_session"]
        self.assertIn("AND claim_id = %s", release)
        self.assertIn("attempt_count = attempt_count + CASE", claim)
        self.assertIn("attempt_count < %s", claim)
        self.assertIn("status = 'processing' AND claim_id = %s", claim)
        self.assertIn("WHEN claim_id = %s THEN 0 ELSE 1", claim)
        self.assertNotIn("@db_retry", claim)
        self.assertNotIn("@db_retry", finalize)
        self.assertNotIn("@db_retry", release)

    def test_runtime_and_browser_have_no_json_rpc_transport(self):
        runtime = Path("main.py").read_text(encoding="utf-8")
        browser = (
            Path("templates/verify.html").read_text(encoding="utf-8")
            + Path("static/verify.js").read_text(encoding="utf-8")
        )
        for forbidden in (
            "SUI_RPC_URL",
            "sui_rpc_request",
            "suix_",
            "sui_getObject",
            '"jsonrpc"',
            "'jsonrpc'",
        ):
            self.assertNotIn(forbidden, runtime)
            self.assertNotIn(forbidden, browser)

    def test_browser_submits_only_session_wallet_and_signature(self):
        browser = Path("static/verify.js").read_text(encoding="utf-8")
        self.assertIn("verification_session: VERIFICATION_SESSION", browser)
        self.assertIn("wallet_address: selectedAddress", browser)
        self.assertIn("wallet_signature: selectedSignature", browser)
        self.assertNotIn("balance_verified", browser)
        self.assertNotIn("token_balance:", browser)
        self.assertNotIn("nft_count:", browser)

    def test_verification_ux_auto_submits_after_explicit_message_signature(self):
        template = Path("templates/verify.html").read_text(encoding="utf-8")
        browser = Path("static/verify.js").read_text(encoding="utf-8")
        connector = Path("static/wallet-connector.js").read_text(encoding="utf-8")
        self.assertIn('id="connectWalletButton"', template)
        self.assertIn('id="ownershipMessage"', template)
        self.assertIn('id="signButton"', template)
        self.assertNotIn('id="submitButton"', template)
        self.assertIn(
            "Confirm wallet and holdings via non-transactional signature. "
            "Zero risk to your holdings",
            template,
        )
        self.assertIn("AlphaCityWalletConnector.create", browser)
        self.assertIn("alwaysPrompt: true", browser)
        self.assertIn("autoReconnect: false", browser)
        self.assertIn("persistSession: false", browser)
        self.assertIn("requirePersonalMessage: true", browser)
        connect_index = browser.index("initWalletConnector()")
        sign_index = browser.index("signButton.addEventListener")
        self.assertLess(connect_index, sign_index)
        signature_index = browser.index("selectedSignature = await signOwnership()")
        submit_index = browser.index("await submitVerification()")
        self.assertLess(signature_index, submit_index)
        self.assertIn("Wallet registered — requirements not met", browser)
        self.assertIn("verification_completed", browser)
        self.assertIn("submissionInFlight", browser)
        self.assertIn("fetchJsonWithTimeout", browser)
        self.assertIn("new CustomEvent('wallet-standard:app-ready'", connector)
        self.assertIn("'sui:signMessage'", connector)
        self.assertNotIn("'standard:signMessage'", connector)
        self.assertIn("root.slush?.sui", connector)
        self.assertIn("signPersonalMessage", connector)
        self.assertNotIn("wallet-standard:register-wallet", browser)

    def test_signature_and_holdings_share_one_bounded_deadline(self):
        endpoint = FUNCTIONS["api_verify"]
        gateway = Path("sui_gateway.py").read_text(encoding="utf-8")
        self.assertIn("operation_deadline = time.monotonic()", endpoint)
        self.assertIn("deadline_monotonic=operation_deadline", endpoint)
        self.assertIn("deadline_monotonic: float | None = None", gateway)
        self.assertIn("WAITRESS_THREADS", Path("main.py").read_text(encoding="utf-8"))
        self.assertIn("_verification_work_slots.acquire", endpoint)
        self.assertIn("_verification_work_slots.release", endpoint)

    def test_readiness_covers_public_wallet_registration_configuration(self):
        readiness = FUNCTIONS["readiness_check"]
        self.assertIn("wallet_registration", readiness)
        self.assertIn("get_public_api_base_url()", readiness)
        self.assertIn("wallet_connect_url", readiness)

    def test_verification_page_uses_fragment_tokens_and_external_assets(self):
        runtime = Path("main.py").read_text(encoding="utf-8")
        template = Path("templates/verify.html").read_text(encoding="utf-8")
        browser = Path("static/verify.js").read_text(encoding="utf-8")
        styles = Path("static/verify.css").read_text(encoding="utf-8")
        connector = Path("static/wallet-connector.js").read_text(encoding="utf-8")
        runtime_config = Path("verification_config.py").read_text(encoding="utf-8")
        self.assertIn("https://alphacity.tech/verify/", runtime_config)
        self.assertIn("build_hosted_verification_url", FUNCTIONS["build_wallet_connect_url"])
        self.assertNotIn("FALLBACK_VERIFY_URL", runtime)
        self.assertIn("window.location.hash", browser)
        self.assertIn("window.history.replaceState", browser)
        self.assertIn('/static/verify.css', template)
        self.assertIn('/static/verify.js', template)
        self.assertIn('/static/wallet-connector.js', template)
        self.assertIn('data-wallet-connector-styles="external"', template)
        self.assertIn("dataset.walletConnectorStyles === 'external'", connector)
        self.assertNotIn("<style>", template)
        self.assertNotIn("'unsafe-inline'", template)
        self.assertNotIn("'unsafe-inline'", runtime)
        self.assertIn("[hidden] { display: none !important; }", styles)
        self.assertIn(".ac-wallet-overlay", styles)

    def test_completed_verification_is_replayable_and_post_commit_work_is_safe(self):
        endpoint = FUNCTIONS["api_verify"]
        self.assertIn("get_completed_verification_result", endpoint)
        self.assertIn("verification_result_replays", endpoint)
        self.assertIn("finally:", endpoint)
        finalize = FUNCTIONS["finalize_verified_wallet"]
        self.assertIn("eligibility_status = %s", finalize)
        self.assertIn("wallet_address = %s", finalize)
        self.assertIn("holdings_summary = %s::jsonb", finalize)
        self.assertIn("DELETE FROM pending_verifications", finalize)
        completed = FUNCTIONS["get_completed_verification_result"]
        self.assertIn("holdings_summary", completed)
        payload = FUNCTIONS["_completed_verification_payload"]
        self.assertIn("verification_success_message(holdings_summary)", payload)
        self.assertIn("'qualifying_holdings': holdings_summary", payload)
        # Pending-context cleanup must be part of the same transaction that
        # completes the session, never an uncaught post-commit operation.
        self.assertNotIn("DELETE FROM pending_verifications", endpoint)

    def test_first_wallet_insert_is_locked_before_merging_addresses(self):
        source = FUNCTIONS["_save_wallet_for_user_with_cursor"]
        insert_index = source.index("INSERT INTO user_wallets")
        lock_index = source.index("SELECT wallets")
        self.assertLess(insert_index, lock_index)
        self.assertIn("ON CONFLICT (group_id, user_id) DO NOTHING", source)

    def test_admission_checks_are_tri_state(self):
        source = FUNCTIONS["evaluate_wallet_requirements"]
        self.assertIn("evaluate_gate(", source)
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

    def test_enforcement_unknown_flags_are_reset_for_each_user(self):
        source = FUNCTIONS["check_user_wallets"]
        loop_index = source.index("for reg in user_regs:")
        nft_reset = source.index("nft_indeterminate = False", loop_index)
        trait_reset = source.index("trait_indeterminate = False", loop_index)
        first_read = source.index(
            "nft_indeterminate=nft_indeterminate",
            loop_index,
        )
        self.assertLess(nft_reset, first_read)
        self.assertLess(trait_reset, first_read)

    def test_auto_removal_has_grace_fresh_recheck_and_unban(self):
        source = FUNCTIONS["check_user_wallets"]
        self.assertIn("decide_auto_removal", source)
        self.assertIn("use_cache=False", source)
        self.assertIn("unban_chat_member", source)
        self.assertIn("only_if_banned=True", source)

    def test_vote_weight_failure_is_retryable_and_first_weight_is_immutable(self):
        handler = FUNCTIONS["handle_poll_vote"]
        calculator = FUNCTIONS["calculate_user_vote_weight"]
        self.assertIn("HoldingsUnavailableError", handler)
        self.assertIn("holdings are temporarily unavailable", handler)
        self.assertIn("HoldingsUnavailableError", calculator)
        self.assertNotIn("balances.get(addr, 0)", calculator)
        self.assertNotIn("vote_weight=EXCLUDED.vote_weight", handler)

    def test_poller_lease_thread_binds_its_stop_event(self):
        source = FUNCTIONS["start_polling"]
        self.assertIn("def renew_poller_lease(stop_event=lease_stop)", source)

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
