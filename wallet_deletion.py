"""Stable, expiring wallet actions and atomic deletion from the current list."""

import json
import secrets

from verification_security import canonical_wallets


SCHEMA = """
CREATE TABLE IF NOT EXISTS wallet_delete_actions (
    token TEXT PRIMARY KEY, group_id BIGINT NOT NULL, user_id BIGINT NOT NULL,
    wallet_address TEXT NOT NULL, expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wallet_delete_actions_expiry ON wallet_delete_actions(expires_at);
"""


class WalletDeletion:
    def __init__(self, db_cursor):
        self.db_cursor = db_cursor

    def create(self, group_id, user_id, wallets):
        actions = []
        with self.db_cursor() as (_, cur):
            cur.execute("DELETE FROM wallet_delete_actions WHERE expires_at <= NOW()")
            for wallet in canonical_wallets(wallets):
                token = secrets.token_hex(12)
                cur.execute("INSERT INTO wallet_delete_actions "
                            "(token, group_id, user_id, wallet_address, expires_at) "
                            "VALUES (%s, %s, %s, %s, NOW() + INTERVAL '15 minutes')",
                            (token, group_id, user_id, wallet))
                actions.append((wallet, token))
        return actions

    def lookup(self, group_id, user_id, token):
        with self.db_cursor() as (_, cur):
            cur.execute("SELECT wallet_address FROM wallet_delete_actions "
                        "WHERE token = %s AND group_id = %s AND user_id = %s AND expires_at > NOW()",
                        (token, group_id, user_id))
            row = cur.fetchone()
            return row[0] if row else None

    def remove(self, group_id, user_id, token):
        with self.db_cursor() as (_, cur):
            # Same lock used by verified and admin wallet additions. Read the
            # current list only after acquiring it, so additions are preserved.
            cur.execute("SELECT wallets FROM user_wallets WHERE group_id = %s AND user_id = %s FOR UPDATE",
                        (group_id, user_id))
            row = cur.fetchone()
            if not row:
                return False
            cur.execute("DELETE FROM wallet_delete_actions "
                        "WHERE token = %s AND group_id = %s AND user_id = %s AND expires_at > NOW() "
                        "RETURNING wallet_address", (token, group_id, user_id))
            action = cur.fetchone()
            if not action:
                return False
            wallets = canonical_wallets(json.loads(row[0]) if row[0] else [])
            if action[0] not in wallets:
                return False
            wallets.remove(action[0])
            cur.execute("UPDATE user_wallets SET wallets = %s, last_token_balance = NULL, "
                        "last_nft_count = NULL, last_trait_count = NULL, holdings_updated_at = NULL "
                        "WHERE group_id = %s AND user_id = %s", (json.dumps(wallets), group_id, user_id))
            cur.execute("DELETE FROM user_wallet_addresses "
                        "WHERE group_id = %s AND user_id = %s AND wallet_address = %s",
                        (group_id, user_id, action[0]))
            return True
