from contextlib import contextmanager
import datetime
from decimal import Decimal
import math
from types import SimpleNamespace as NS
import threading
import unittest
from unittest.mock import Mock

from enforcement_policy import EnforcementDecision, GateStatus, decide_auto_removal
from test_multi_wallet_verification import load


class ScanComplete(BaseException):
    pass


class EmptyWalletEnforcementTests(unittest.TestCase):
    def scan(self, *, exempt=False, auto_remove=True, warned=True, delivered=True):
        cur = Mock()
        cur.fetchall.return_value = []

        @contextmanager
        def db_cursor():
            yield Mock(), cur

        def finished(*args):
            raise ScanComplete()

        config = {"token": "0x2::sui::SUI", "minimum_holding": Decimal(10),
                  "auto_remove": auto_remove, "auto_remove_grace_seconds": 0}
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        ns = load(
            "check_user_wallets", GateStatus=GateStatus, EnforcementDecision=EnforcementDecision,
            decide_auto_removal=decide_auto_removal, datetime=datetime, math=math,
            refresh_wallet_scheduler_lease=Mock(return_value=True),
            renew_wallet_scheduler_lease=Mock(), threading=NS(Event=threading.Event, Thread=Mock()),
            load_configs_from_db=Mock(return_value={-100: config}),
            _wallet_scan_state_lock=threading.Lock(), _wallet_scan_state={}, runtime_metrics=Mock(),
            group_has_active_subscription=Mock(return_value=True),
            DEFAULT_AUTO_REMOVE_GRACE_SECONDS=86400, ALERT_COOLDOWN_DAYS=2, ALERT_DELIVERY_VERSION=1,
            get_user_registrations_for_group=Mock(return_value=[
                {"user_id": 10, "wallets": [], "is_exempt": exempt, "username": "test"}]),
            get_db_cursor=db_cursor, fetch_wallet_balances=Mock(return_value={}),
            bot=Mock(get_chat_member=Mock(return_value=NS(status="member"))),
            get_enforcement_first_failed_at=Mock(return_value=now if warned else None),
            record_enforcement_failure=Mock(), record_enforcement_event=Mock(),
            clear_enforcement_state=Mock(), _safe_removal=Mock(remove=Mock(return_value="removed")),
            send_low_holdings_alerts_to_admins=Mock(return_value=delivered),
            record_delivered_low_balance_alerts=Mock(), GROUP_CHECK_DELAY=0,
            time=Mock(monotonic=Mock(return_value=1), time=Mock(return_value=1)),
            SLEEP_BETWEEN_TASKS=1, TASK_JITTER_PERCENT=0, random=Mock(random=Mock(return_value=0)),
            sleep_while_holding_wallet_scheduler_lease=finished,
        )
        with self.assertRaises(ScanComplete):
            ns["check_user_wallets"]()
        self.assertEqual(ns["_wallet_scan_state"]["status"], "completed")
        return ns

    def test_group_containing_only_empty_registration_reaches_final_removal(self):
        ns = self.scan()
        ns["_safe_removal"].remove.assert_called_once()
        self.assertEqual(ns["_safe_removal"].remove.call_args.args[3], [])

    def test_empty_registration_starts_grace_before_any_removal(self):
        ns = self.scan(warned=False)
        ns["record_enforcement_failure"].assert_called_once()
        ns["_safe_removal"].remove.assert_not_called()
        ns["send_low_holdings_alerts_to_admins"].assert_called_once()

    def test_empty_exempt_registration_is_skipped(self):
        ns = self.scan(exempt=True)
        ns["_safe_removal"].remove.assert_not_called()
        ns["record_enforcement_failure"].assert_not_called()

    def test_alert_only_policy_is_preserved_and_cooldown_requires_delivery(self):
        for delivered in (True, False):
            with self.subTest(delivered=delivered):
                ns = self.scan(auto_remove=False, delivered=delivered)
                ns["_safe_removal"].remove.assert_not_called()
                ns["send_low_holdings_alerts_to_admins"].assert_called_once()
                self.assertEqual(ns["record_delivered_low_balance_alerts"].call_count, int(delivered))


if __name__ == "__main__":
    unittest.main()
