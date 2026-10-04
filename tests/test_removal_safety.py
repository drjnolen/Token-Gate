from contextlib import contextmanager
import datetime as dt
from types import SimpleNamespace as NS
import threading
import unittest
from unittest.mock import Mock

from removal_safety import RemovalSnapshot, SafeRemoval
from verification_security import canonical_wallets


CONFIG = {"token": "0x2::sui::SUI", "minimum_holding": 10, "decimals": 9,
          "registration_mode": "both", "auto_remove": True, "auto_remove_grace_seconds": 0,
          "nft_collection_id": "0x1::nft::NFT"}
WALLETS = canonical_wallets(["0x1"])


class RemovalTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = RemovalSnapshot(CONFIG.copy(), tuple(WALLETS), dt.datetime(2026, 1, 1))
        self.repository = Mock()
        self.repository.snapshot.return_value = self.snapshot
        self.repository._snapshot.return_value = self.snapshot
        self.cur = Mock()
        self.current = self.cur
        self.job = (-100, 10, "token", 1900000000)
        self.repository.prepare.return_value = self.job

        @contextmanager
        def authorized(*args):
            yield self.current
        self.repository.authorized.side_effect = authorized
        self.bot = Mock()
        self.bot.get_chat.return_value = NS(type="supergroup")
        self.bot.get_chat_member.side_effect = [NS(status="member"), NS(status="kicked", until_date=self.job[3])]
        self.bot.ban_chat_member.return_value = True
        self.bot.unban_chat_member.return_value = True
        self.evaluate = Mock(return_value={"status": "fail"})
        self.event = Mock()
        self.lost = threading.Event()
        self.service = SafeRemoval(self.repository, self.bot, evaluate=self.evaluate, event=self.event)

    def run_removal(self, wallets=WALLETS):
        return self.service.remove(-100, 10, CONFIG, wallets, self.lost)

    def test_fresh_complete_gate_precedes_durable_intent_ban_and_unban(self):
        order = Mock()
        order.attach_mock(self.evaluate, "evaluate")
        order.attach_mock(self.repository.prepare, "prepare")
        order.attach_mock(self.bot.ban_chat_member, "ban")
        order.attach_mock(self.bot.unban_chat_member, "unban")
        self.assertEqual(self.run_removal(), "removed")
        self.assertEqual([call[0] for call in order.mock_calls], ["evaluate", "prepare", "ban", "unban"])
        self.evaluate.assert_called_once_with(WALLETS, CONFIG, user_id=10, force_fresh=True)
        self.bot.ban_chat_member.assert_called_once_with(-100, 10, until_date=self.job[3])
        self.bot.unban_chat_member.assert_called_once_with(-100, 10, only_if_banned=True)
        self.repository.finish.assert_called_once_with(self.job)

    def test_recovered_nft_branch_prevents_removal(self):
        self.evaluate.return_value = {"status": "pass", "token_balance": 0, "nft_count": 1}
        self.assertEqual(self.run_removal(), "recovered")
        self.bot.ban_chat_member.assert_not_called()

    def test_indeterminate_provider_never_removes(self):
        self.evaluate.return_value = {"status": "indeterminate"}
        self.assertEqual(self.run_removal(), "deferred")
        self.repository.prepare.assert_not_called()

    def test_empty_registration_reaches_removal_without_provider(self):
        empty = RemovalSnapshot(CONFIG.copy(), (), self.snapshot.first_failed_at)
        self.repository.snapshot.return_value = empty
        self.repository._snapshot.return_value = empty
        self.assertEqual(self.run_removal([]), "removed")
        self.evaluate.assert_not_called()

    def test_changed_scan_configuration_or_wallets_defer(self):
        for snapshot in (None, RemovalSnapshot({**CONFIG, "auto_remove": False}, tuple(WALLETS), None),
                         RemovalSnapshot(CONFIG, tuple(canonical_wallets(["0x2"])), None)):
            self.repository.snapshot.return_value = snapshot
            self.assertEqual(self.run_removal(), "deferred")
        self.bot.ban_chat_member.assert_not_called()

    def test_database_state_changes_during_provider_call_defer(self):
        def changed(*args, **kwargs):
            self.current = None  # repository rejects changed wallet/config/exemption/lease/subscription/grace
            return {"status": "fail"}
        self.evaluate.side_effect = changed
        self.assertEqual(self.run_removal(), "deferred")
        self.bot.ban_chat_member.assert_not_called()
        self.repository.finish.assert_called_once_with(self.job)

    def test_lease_loss_during_provider_or_membership_call_defers(self):
        def lost(*args, **kwargs):
            self.lost.set()
            return {"status": "fail"}
        self.evaluate.side_effect = lost
        self.assertEqual(self.run_removal(), "deferred")
        self.lost.clear()
        self.evaluate.side_effect = None
        self.bot.get_chat_member.side_effect = lambda *args: (self.lost.set() or NS(status="member"))
        self.assertEqual(self.run_removal(), "deferred")
        self.bot.ban_chat_member.assert_not_called()

    def test_expired_lease_after_membership_request_defers(self):
        self.repository._snapshot.return_value = None
        self.assertEqual(self.run_removal(), "deferred")
        self.bot.ban_chat_member.assert_not_called()

    def test_admin_left_and_inactive_restricted_members_are_not_banned(self):
        for member in (NS(status="administrator"), NS(status="creator"), NS(status="left"),
                       NS(status="kicked"), NS(status="restricted", is_member=False)):
            self.bot.get_chat_member.side_effect = None
            self.bot.get_chat_member.return_value = member
            self.assertEqual(self.run_removal(), "deferred")
        self.bot.ban_chat_member.assert_not_called()

    def test_basic_groups_defer_because_expiring_ban_is_not_supported(self):
        self.bot.get_chat.return_value.type = "group"
        self.assertEqual(self.run_removal(), "deferred")
        self.bot.ban_chat_member.assert_not_called()

    def test_failed_unban_keeps_job_and_restart_retries_only_unban(self):
        self.bot.unban_chat_member.side_effect = TimeoutError()
        self.assertEqual(self.run_removal(), "pending")
        self.repository.finish.assert_not_called()
        self.repository.retry.assert_called_once()
        self.bot.unban_chat_member.side_effect = None
        self.bot.get_chat_member.side_effect = None
        self.bot.get_chat_member.return_value = NS(status="kicked", until_date=self.job[3])
        restarted = SafeRemoval(self.repository, self.bot, evaluate=Mock(), event=self.event)
        self.assertTrue(restarted.recover(self.job))
        self.assertEqual(self.bot.ban_chat_member.call_count, 1)
        self.assertEqual(self.bot.unban_chat_member.call_count, 2)

    def test_ambiguous_ban_timeout_retains_durable_recovery(self):
        self.bot.ban_chat_member.side_effect = TimeoutError()
        self.assertEqual(self.run_removal(), "pending")
        self.repository.finish.assert_not_called()
        self.repository.removed.assert_not_called()
        self.assertEqual(self.bot.ban_chat_member.call_count, 1)

    def test_crash_after_ban_leaves_precommitted_recovery(self):
        self.bot.ban_chat_member.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.run_removal()
        self.repository.prepare.assert_called_once()
        self.repository.finish.assert_not_called()

    def test_later_manual_ban_is_preserved_and_already_unbanned_is_idempotent(self):
        self.bot.get_chat_member.side_effect = None
        self.bot.get_chat_member.return_value = NS(status="kicked", until_date=0)
        self.assertFalse(self.service.recover(self.job))
        self.bot.unban_chat_member.assert_not_called()
        self.bot.get_chat_member.return_value = NS(status="left")
        self.assertTrue(self.service.recover(self.job))
        self.bot.unban_chat_member.assert_not_called()


if __name__ == "__main__":
    unittest.main()
