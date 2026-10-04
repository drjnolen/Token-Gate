"""Real transaction/locking tests. CI supplies a disposable PostgreSQL service.

Locally: set TEST_DATABASE_URL to a dedicated test database, then run unittest.
Every run creates its own schema; no application schema or data is touched.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
import threading
import unittest
from unittest.mock import Mock
from types import SimpleNamespace as NS
import uuid

import psycopg2
from psycopg2 import sql

from removal_safety import RemovalRepository, SafeRemoval, SCHEMA as REMOVAL_SCHEMA
from wallet_deletion import WalletDeletion, SCHEMA as WALLET_DELETION_SCHEMA
from verification_security import canonical_wallets
from test_multi_wallet_verification import load


DSN = os.getenv("TEST_DATABASE_URL")
A, B, C = canonical_wallets(["0x1", "0x2", "0x3"])


@unittest.skipUnless(DSN, "TEST_DATABASE_URL is not set (PostgreSQL integration tests run in CI)")
class SafetyPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = "safety_test_" + uuid.uuid4().hex
        with psycopg2.connect(DSN) as conn:
            with conn.cursor() as cur:
                cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(cls.schema)))
        cls.ns = load("init_db", "_save_wallet_for_user_with_cursor", "record_delivered_low_balance_alerts",
                      get_db_cursor=cls.db_cursor, REMOVAL_SCHEMA=REMOVAL_SCHEMA,
                      WALLET_DELETION_SCHEMA=WALLET_DELETION_SCHEMA, ALERT_DELIVERY_VERSION=1)
        # Exercise the real additive startup migration twice for idempotence.
        cls.ns["init_db"]()
        cls.ns["init_db"]()

    @classmethod
    def tearDownClass(cls):
        with psycopg2.connect(DSN) as conn:
            with conn.cursor() as cur:
                cur.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(cls.schema)))

    @classmethod
    @contextmanager
    def db_cursor(cls):
        conn = psycopg2.connect(DSN)
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(cls.schema)))
                    yield conn, cur
        finally:
            conn.close()

    def setUp(self):
        self.repo = RemovalRepository(self.db_cursor, instance_id="worker", whitelisted=lambda group: False)
        self.deletion = WalletDeletion(self.db_cursor)
        with self.db_cursor() as (_, cur):
            cur.execute("TRUNCATE user_wallets, subscriber_configs, subscriptions, enforcement_states, "
                        "scheduler_leases, removal_recovery, wallet_delete_actions, low_balance_alerts CASCADE")
            cur.execute("INSERT INTO subscriber_configs (chat_id, token, minimum_holding, auto_remove, "
                        "auto_remove_grace_seconds) VALUES (-100, '0x2::sui::SUI', 10, TRUE, 0)")
            cur.execute("INSERT INTO subscriptions (group_id, expires_at) VALUES (-100, NOW() + INTERVAL '1 day')")
            cur.execute("INSERT INTO scheduler_leases VALUES ('wallet_checks', 'worker', NOW() + INTERVAL '5 minutes')")
            cur.execute("INSERT INTO enforcement_states (group_id, user_id, first_failed_at) "
                        "VALUES (-100, 10, NOW() - INTERVAL '1 day')")
            self.ns["_save_wallet_for_user_with_cursor"](cur, -100, 10, "test", [A, B])
        self.bot = Mock()
        self.bot.get_chat.return_value = NS(type="supergroup")
        self.bot.get_chat_member.return_value = NS(status="member")
        self.bot.ban_chat_member.return_value = True
        self.bot.unban_chat_member.return_value = True
        self.evaluate = Mock(return_value={"status": "fail"})
        self.event = Mock()
        self.service = SafeRemoval(self.repo, self.bot, evaluate=self.evaluate, event=self.event)

    def execute(self, query, params=()):
        with self.db_cursor() as (_, cur):
            cur.execute(query, params)
            return cur.fetchall() if cur.description else None

    def remove(self):
        original = self.repo.snapshot(-100, 10)
        return self.service.remove(-100, 10, original.config, original.wallets, threading.Event())

    def test_changes_during_fresh_check_cancel_removal(self):
        changes = [
            "UPDATE user_wallets SET is_exempt = TRUE",
            "UPDATE user_wallets SET wallets = '[]'",
            "UPDATE subscriber_configs SET auto_remove = FALSE",
            "UPDATE subscriber_configs SET minimum_holding = 100",
            "UPDATE subscriber_configs SET registration_mode = 'nft'",
            "UPDATE subscriptions SET expires_at = NOW() - INTERVAL '1 second'",
            "UPDATE scheduler_leases SET holder_id = 'other'",
            "UPDATE scheduler_leases SET expires_at = NOW() - INTERVAL '1 second'",
            "UPDATE enforcement_states SET first_failed_at = NOW() + INTERVAL '1 day'",
        ]
        for query in changes:
            with self.subTest(query=query):
                self.setUp()
                def change(*args, statement=query, **kwargs):
                    self.execute(statement)
                    return {"status": "fail"}
                self.evaluate.side_effect = change
                self.assertEqual(self.remove(), "deferred")
                self.bot.ban_chat_member.assert_not_called()
                self.assertEqual(self.execute("SELECT * FROM removal_recovery"), [])

    def test_final_authorization_locks_serialize_exemption_and_config_writers(self):
        for query in ("UPDATE user_wallets SET is_exempt = TRUE WHERE user_id = 10",
                      "UPDATE subscriber_configs SET auto_remove = FALSE WHERE chat_id = -100"):
            with self.subTest(query=query):
                snapshot = self.repo.snapshot(-100, 10)
                # Each real competing connection has a short lock timeout. It
                # must be blocked until the destructive decision releases locks.
                with self.repo.authorized(-100, 10, snapshot) as cur:
                    self.assertIsNotNone(cur)
                    with self.assertRaises(psycopg2.errors.LockNotAvailable):
                        with self.db_cursor() as (_, other):
                            other.execute("SET LOCAL lock_timeout = '100ms'")
                            other.execute(query)
                self.execute(query)
                self.setUp()

    def test_intent_committed_before_ban_and_survives_registration_cleanup(self):
        def ban(*args, **kwargs):
            jobs = self.execute("SELECT until_date FROM removal_recovery")
            self.assertEqual(jobs, [(kwargs["until_date"],)])
            self.bot.get_chat_member.return_value = NS(status="kicked", until_date=kwargs["until_date"])
            return True
        self.bot.ban_chat_member.side_effect = ban
        self.bot.unban_chat_member.side_effect = TimeoutError()
        self.assertEqual(self.remove(), "pending")
        self.assertEqual(self.execute("SELECT * FROM user_wallets"), [])
        self.assertEqual(len(self.execute("SELECT * FROM removal_recovery")), 1)
        self.execute("UPDATE removal_recovery SET next_attempt_at = NOW() - INTERVAL '1 second'")
        restarted = RemovalRepository(self.db_cursor, instance_id="new-worker", whitelisted=lambda _: False)
        job = restarted.claim_due()
        self.assertIsNotNone(job)
        self.assertIsNone(self.repo.claim_due())
        self.bot.unban_chat_member.side_effect = None
        self.assertTrue(SafeRemoval(restarted, self.bot, evaluate=Mock(), event=self.event).recover(job))
        self.assertEqual(self.execute("SELECT * FROM removal_recovery"), [])
        self.assertEqual(self.bot.ban_chat_member.call_count, 1)

    def test_ban_timeout_keeps_precommitted_job_after_transaction_rollback(self):
        self.bot.ban_chat_member.side_effect = TimeoutError()
        self.assertEqual(self.remove(), "pending")
        self.assertEqual(len(self.execute("SELECT * FROM removal_recovery")), 1)
        self.assertEqual(len(self.execute("SELECT * FROM user_wallets")), 1)

    def test_empty_wallet_row_is_still_subject_to_enforcement(self):
        for _, token in self.deletion.create(-100, 10, [A, B]):
            self.assertTrue(self.deletion.remove(-100, 10, token))
        self.assertEqual(self.repo.snapshot(-100, 10).wallets, ())
        self.assertEqual(self.remove(), "removed")
        self.evaluate.assert_not_called()

    def test_wallet_action_is_bound_to_user_group_address_and_expiry(self):
        _, token = self.deletion.create(-100, 10, [A])[0]
        self.assertIsNone(self.deletion.lookup(-100, 11, token))
        self.assertIsNone(self.deletion.lookup(-200, 10, token))
        self.assertFalse(self.deletion.remove(-100, 11, token))
        self.execute("UPDATE user_wallets SET wallets = %s", (json.dumps([B, A]),))
        self.assertTrue(self.deletion.remove(-100, 10, token))
        self.assertEqual(json.loads(self.execute("SELECT wallets FROM user_wallets")[0][0]), [B])
        self.assertFalse(self.deletion.remove(-100, 10, token))
        _, expired = self.deletion.create(-100, 10, [B])[0]
        self.execute("UPDATE wallet_delete_actions SET expires_at = NOW() - INTERVAL '1 second'")
        self.assertFalse(self.deletion.remove(-100, 10, expired))

    def test_deletion_waits_for_concurrent_addition_and_preserves_it(self):
        _, token = self.deletion.create(-100, 10, [A])[0]
        started = threading.Event()
        with ThreadPoolExecutor(max_workers=1) as executor:
            with self.db_cursor() as (_, cur):
                self.ns["_save_wallet_for_user_with_cursor"](cur, -100, 10, "test", [C])
                def delete():
                    started.set()
                    return self.deletion.remove(-100, 10, token)
                pending = executor.submit(delete)
                self.assertTrue(started.wait(2))
                self.assertFalse(pending.done())
            self.assertTrue(pending.result(timeout=5))
        self.assertEqual(set(json.loads(self.execute("SELECT wallets FROM user_wallets")[0][0])), {B, C})
        self.assertEqual(set(row[0] for row in self.execute("SELECT wallet_address FROM user_wallet_addresses")), {B, C})

    def test_delivered_alert_batch_has_valid_columns_and_cooldown_version(self):
        self.ns["record_delivered_low_balance_alerts"](-100, [10, 11, 10])
        rows = self.execute("SELECT user_id, alert_sent_at IS NOT NULL, delivery_version "
                            "FROM low_balance_alerts ORDER BY user_id")
        self.assertEqual(rows, [(10, True, 1), (11, True, 1)])
        self.execute("UPDATE low_balance_alerts SET delivery_version = 0")
        self.ns["record_delivered_low_balance_alerts"](-100, [10, 11])
        self.assertEqual(self.execute("SELECT COUNT(*) FROM low_balance_alerts WHERE delivery_version = 1"), [(2,)])


if __name__ == "__main__":
    unittest.main()
