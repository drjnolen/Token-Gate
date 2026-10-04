"""Final authorization and restart-safe recovery for temporary removals.

Chain reads happen outside transactions. Only the last authorization and single
Telegram ban call hold row locks, so all database writers serialize with that
decision, including other application processes. Never retry a ban on timeout.
"""

from contextlib import contextmanager
from dataclasses import dataclass
import datetime as dt
from decimal import Decimal
import json
import logging
import secrets
import time

from verification_security import canonical_sui_address, canonical_wallets


CONFIG_DEFAULTS = {
    "token": "", "minimum_holding": Decimal(0), "decimals": 6,
    "auto_remove": False, "auto_remove_grace_seconds": 86400,
    "nft_collection_id": "", "nft_threshold": 1, "registration_mode": "token",
    "nft_trait_name": "", "nft_trait_value": "", "nft_trait_threshold": 1,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS removal_recovery (
    group_id BIGINT NOT NULL, user_id BIGINT NOT NULL,
    token TEXT NOT NULL, until_date BIGINT NOT NULL,
    next_attempt_at TIMESTAMPTZ NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (group_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_removal_recovery_due ON removal_recovery(next_attempt_at);
"""


def config_signature(config):
    values = dict(CONFIG_DEFAULTS)
    values.update({k: v for k, v in config.items() if k in values and v is not None})
    values["minimum_holding"] = Decimal(str(values["minimum_holding"]))
    return tuple(values[key] for key in CONFIG_DEFAULTS)


@dataclass(frozen=True)
class RemovalSnapshot:
    config: dict
    wallets: tuple
    first_failed_at: object

    def matches(self, config, wallets):
        return (config_signature(self.config) == config_signature(config)
                and self.wallets == tuple(sorted(canonical_wallets(wallets))))


class RemovalRepository:
    def __init__(self, db_cursor, *, instance_id, whitelisted):
        self.db_cursor = db_cursor
        self.instance_id = instance_id
        self.whitelisted = whitelisted

    def _snapshot(self, cur, group_id, user_id, *, lock=False):
        shared = " FOR SHARE" if lock else ""
        cur.execute("SELECT " + ", ".join(CONFIG_DEFAULTS)
                    + " FROM subscriber_configs WHERE chat_id = %s" + shared, (group_id,))
        row = cur.fetchone()
        if not row:
            return None
        config = {key: default if value is None else value
                  for (key, default), value in zip(CONFIG_DEFAULTS.items(), row)}
        if (not config["auto_remove"] or not config["token"]
                or config["registration_mode"] not in {"token", "both"}):
            return None
        cur.execute("SELECT wallets, is_exempt FROM user_wallets "
                    "WHERE group_id = %s AND user_id = %s" + (" FOR UPDATE" if lock else ""),
                    (group_id, user_id))
        registration = cur.fetchone()
        if not registration or registration[1]:
            return None
        wallets = json.loads(registration[0]) if registration[0] else []
        if not isinstance(wallets, list) or any(not isinstance(w, str) for w in wallets):
            return None
        normalized = canonical_wallets(wallets)
        # Malformed stored addresses are not an authoritative empty registration.
        if any(canonical_sui_address(wallet) is None for wallet in wallets):
            return None
        if not self.whitelisted(group_id):
            cur.execute("SELECT expires_at FROM subscriptions WHERE group_id = %s" + shared,
                        (group_id,))
            subscription = cur.fetchone()
            if not subscription or not subscription[0]:
                return None
            expiry = subscription[0]
            expiry = (expiry.replace(tzinfo=dt.timezone.utc) if expiry.tzinfo is None
                      else expiry.astimezone(dt.timezone.utc))
            if expiry <= dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=120):
                return None
        cur.execute("SELECT holder_id, expires_at > clock_timestamp() + INTERVAL '120 seconds' "
                    "FROM scheduler_leases WHERE lease_name = 'wallet_checks'" + shared)
        lease = cur.fetchone()
        if not lease or lease[0] != self.instance_id or not lease[1]:
            return None
        cur.execute("SELECT first_failed_at, "
                    "first_failed_at + (%s * INTERVAL '1 second') <= clock_timestamp() "
                    "FROM enforcement_states WHERE group_id = %s AND user_id = %s" + shared,
                    (max(0, config["auto_remove_grace_seconds"]), group_id, user_id))
        state = cur.fetchone()
        if not state or not state[1]:
            return None
        return RemovalSnapshot(config, tuple(sorted(normalized)), state[0])

    def snapshot(self, group_id, user_id):
        with self.db_cursor() as (_, cur):
            return self._snapshot(cur, group_id, user_id)

    @contextmanager
    def authorized(self, group_id, user_id, expected):
        with self.db_cursor() as (_, cur):
            # Fail closed on competing writers instead of waiting indefinitely.
            cur.execute("SET LOCAL lock_timeout = '3s'")
            current = self._snapshot(cur, group_id, user_id, lock=True)
            yield cur if current == expected else None

    def prepare(self, group_id, user_id):
        token = secrets.token_hex(16)
        # A finite Telegram ban is a backstop even if the service never restarts.
        until_date = int(time.time()) + 600
        with self.db_cursor() as (_, cur):
            cur.execute("INSERT INTO removal_recovery "
                        "(group_id, user_id, token, until_date, next_attempt_at) "
                        "VALUES (%s, %s, %s, %s, NOW() + INTERVAL '3 minutes') "
                        "ON CONFLICT DO NOTHING RETURNING token",
                        (group_id, user_id, token, until_date))
            if not cur.fetchone():
                return None
        return (group_id, user_id, token, until_date)

    def finish(self, job):
        with self.db_cursor() as (_, cur):
            cur.execute("DELETE FROM removal_recovery "
                        "WHERE group_id = %s AND user_id = %s AND token = %s", job[:3])

    def retry(self, job, error):
        with self.db_cursor() as (_, cur):
            cur.execute("UPDATE removal_recovery SET attempts = attempts + 1, "
                        "last_error = %s, next_attempt_at = NOW() + INTERVAL '60 seconds' "
                        "WHERE group_id = %s AND user_id = %s AND token = %s",
                        (type(error).__name__, *job[:3]))

    def claim_due(self):
        with self.db_cursor() as (_, cur):
            cur.execute("SELECT group_id, user_id, token, until_date FROM removal_recovery "
                        "WHERE next_attempt_at <= NOW() ORDER BY next_attempt_at "
                        "LIMIT 1 FOR UPDATE SKIP LOCKED")
            job = cur.fetchone()
            if job:
                # One recovery request can run per member across all workers.
                cur.execute("UPDATE removal_recovery SET next_attempt_at = NOW() + INTERVAL '3 minutes' "
                            "WHERE group_id = %s AND user_id = %s AND token = %s", job[:3])
            return job

    @staticmethod
    def removed(cur, group_id, user_id):
        # Under the registration row lock, before a concurrent new registration.
        for table in ("user_wallets", "low_balance_alerts", "enforcement_states"):
            cur.execute(f"DELETE FROM {table} WHERE group_id = %s AND user_id = %s",
                        (group_id, user_id))


class SafeRemoval:
    def __init__(self, repository, bot, *, evaluate, event):
        self.repository = repository
        self.bot = bot
        self.evaluate = evaluate
        self.event = event

    def recover(self, job):
        group, user, _, until_date = job
        try:
            member = self.bot.get_chat_member(group, user)
            if member.status == "kicked":
                # Preserve a later manual administrator ban.
                if getattr(member, "until_date", None) != until_date:
                    self.event(group, user, "auto_remove", "recovery_superseded", {})
                    self.repository.finish(job)
                    return False
                if not self.bot.unban_chat_member(group, user, only_if_banned=True):
                    raise RuntimeError("Telegram did not confirm unban")
            self.event(group, user, "auto_remove", "unban_confirmed", {})
            self.repository.finish(job)
            return True
        except Exception as exc:
            self.repository.retry(job, exc)
            self.event(group, user, "auto_remove", "unban_pending", {"error": type(exc).__name__})
            return False

    def remove(self, group, user, config, wallets, lease_lost):
        snapshot = self.repository.snapshot(group, user)
        if not snapshot or not snapshot.matches(config, wallets) or lease_lost.is_set():
            return "deferred"
        result = ({"status": "fail"} if not snapshot.wallets else self.evaluate(
            list(snapshot.wallets), snapshot.config, user_id=user, force_fresh=True))
        if result["status"] == "pass":
            return "recovered"
        if result["status"] != "fail" or lease_lost.is_set():
            return "deferred"
        # until_date is supported for supergroups. Do not risk an indefinite ban
        # in a legacy basic group, where Telegram ignores this expiry.
        if self.bot.get_chat(group).type != "supergroup":
            return "deferred"
        job = self.repository.prepare(group, user)
        if not job:
            return "deferred"
        ban_attempted = False
        try:
            with self.repository.authorized(group, user, snapshot) as cur:
                if cur is None or lease_lost.is_set():
                    return "deferred"
                member = self.bot.get_chat_member(group, user)
                active = member.status == "member" or (
                    member.status == "restricted" and getattr(member, "is_member", False))
                if not active or lease_lost.is_set():
                    return "deferred"
                # Recheck clocks after the membership request, while row locks
                # still prevent wallet/config/exemption changes or lease takeover.
                if self.repository._snapshot(cur, group, user, lock=True) != snapshot:
                    return "deferred"
                # Never submit an old expiry: Telegram treats near/past expiry
                # as an indefinite ban rather than a short temporary one.
                if job[3] <= int(time.time()) + 120:
                    return "deferred"
                ban_attempted = True
                if not self.bot.ban_chat_member(group, user, until_date=job[3]):
                    raise RuntimeError("Telegram did not confirm removal")
                self.repository.removed(cur, group, user)
        except Exception as exc:
            # An ambiguous timeout may have succeeded. Keep the durable intent
            # and let recovery inspect Telegram; never issue another ban here.
            self.event(group, user, "auto_remove", "removal_uncertain", {"error": type(exc).__name__})
            return "pending"
        finally:
            if not ban_attempted:
                self.repository.finish(job)
        return "removed" if self.recover(job) else "pending"

    def run_recovery(self, stop):
        while not stop.is_set():
            try:
                job = self.repository.claim_due()
                if job:
                    self.recover(job)
                    continue
            except Exception:
                logging.exception("Temporary-removal recovery failed; will retry")
            stop.wait(30)
