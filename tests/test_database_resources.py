"""Execute database lifecycle functions without starting the production bot."""

import ast
from contextlib import contextmanager
import logging
from pathlib import Path
import unittest
from unittest.mock import Mock


def load_functions(**dependencies):
    tree = ast.parse(Path("main.py").read_text(encoding="utf-8"))
    tree.body = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name in {"get_connection_pool", "get_db_cursor"}]
    namespace = {"contextmanager": contextmanager, "logging": logging, **dependencies}
    exec(compile(tree, "main.py", "exec"), namespace)
    return namespace


class DatabaseResourceTests(unittest.TestCase):
    def setUp(self):
        self.pool = Mock()
        self.connection = self.pool.getconn.return_value
        self.connection.closed = False
        self.ns = load_functions(connection_pool=self.pool)

    def test_reusing_pool_does_not_borrow_or_probe_a_connection(self):
        for _ in range(100):
            self.assertIs(self.ns["get_connection_pool"](), self.pool)
        self.pool.getconn.assert_not_called()
        self.pool.closeall.assert_not_called()

    def test_pool_exhaustion_does_not_close_other_transactions(self):
        self.pool.getconn.side_effect = RuntimeError("connection pool exhausted")
        with self.assertRaisesRegex(RuntimeError, "exhausted"):
            with self.ns["get_db_cursor"]():
                self.fail("unreachable")
        self.pool.closeall.assert_not_called()
        self.assertIs(self.ns["connection_pool"], self.pool)

    def test_success_commits_and_returns_the_connection(self):
        with self.ns["get_db_cursor"]() as (connection, cursor):
            self.assertIs(connection, self.connection)
            cursor.execute("SELECT 1")
        self.connection.commit.assert_called_once()
        self.pool.putconn.assert_called_once_with(self.connection, close=False)

    def test_rollback_failure_discards_only_failed_connection(self):
        self.connection.rollback.side_effect = RuntimeError("connection lost")
        with self.assertLogs(level="ERROR"), self.assertRaisesRegex(ValueError, "query failed"):
            with self.ns["get_db_cursor"]():
                raise ValueError("query failed")
        self.pool.putconn.assert_called_once_with(self.connection, close=True)
        self.pool.closeall.assert_not_called()

    def test_recoverable_query_error_keeps_connection(self):
        with self.assertRaises(ValueError):
            with self.ns["get_db_cursor"]():
                raise ValueError("bad query")
        self.connection.rollback.assert_called_once()
        self.pool.putconn.assert_called_once_with(self.connection, close=False)

    def test_failed_return_closes_connection_without_abandoning_pool(self):
        self.pool.putconn.side_effect = RuntimeError("closed")
        with self.assertLogs(level="ERROR"):
            with self.ns["get_db_cursor"]():
                pass
        self.connection.close.assert_called_once()
        self.assertIs(self.ns["connection_pool"], self.pool)
