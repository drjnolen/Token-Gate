"""Exercise batch/cache behavior with a fake chain gateway, without bot startup."""

import ast
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import logging
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import Mock

from runtime_support import bounded_executor_map


class BalanceResourceTests(unittest.TestCase):
    def setUp(self):
        tree = ast.parse(Path("main.py").read_text(encoding="utf-8"))
        tree.body = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                     and node.name == "fetch_wallet_balances"]
        self.executor = ThreadPoolExecutor(max_workers=2)
        self.addCleanup(self.executor.shutdown)
        self.gateway = Mock()
        self.gateway.get_balance_atomic.return_value = 10000
        self.ns = {
            "time": time, "Decimal": Decimal, "logging": logging,
            "CACHE_TTL": 1200, "MAX_CACHE_SIZE": 1000,
            "cache_lock": threading.Lock(), "balance_cache": {},
            "sui_gateway": self.gateway, "SuiGatewayError": RuntimeError,
            "CITY_TOKEN_TYPE": "city-token", "_sui_executor": self.executor,
            "SUI_FETCH_WORKERS": 2, "bounded_executor_map": bounded_executor_map,
        }
        exec(compile(tree, "main.py", "exec"), self.ns)
        self.fetch = self.ns["fetch_wallet_balances"]

    def test_duplicate_addresses_make_one_provider_request(self):
        self.assertEqual(self.fetch(["0xAB", "0xab", "0xAB"], "token", 2),
                         {"0xab": Decimal(100)})
        self.gateway.get_balance_atomic.assert_called_once()

    def test_cache_separates_case_sensitive_types_and_changed_decimals(self):
        self.assertEqual(self.fetch(["0xab"], "coin::TOKEN", 2)["0xab"], 100)
        self.assertEqual(self.fetch(["0xab"], "coin::TOKEN", 4)["0xab"], 1)
        self.fetch(["0xab"], "coin::token", 2)
        self.assertEqual(self.gateway.get_balance_atomic.call_count, 3)
        self.fetch(["0xab"], "coin::TOKEN", 2)
        self.assertEqual(self.gateway.get_balance_atomic.call_count, 3)

    def test_fresh_requests_bypass_cache_and_failure_does_not_become_zero(self):
        self.fetch(["0xab"], "token", 2)
        self.gateway.get_balance_atomic.side_effect = RuntimeError("provider unavailable")
        with self.assertLogs(level="ERROR"):
            self.assertIsNone(self.fetch(["0xab"], "token", 2, use_cache=False)["0xab"])
        self.assertEqual(self.fetch(["0xab"], "token", 2)["0xab"], 100)
