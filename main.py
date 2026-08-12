import telebot
import os
import html as html_module
import logging
import json
import sys
import time
import threading
import re
import random
import secrets
import math
from flask import Flask, jsonify, request, render_template
from telebot import types
import psycopg2
from psycopg2 import pool
from logging.handlers import RotatingFileHandler
from contextlib import contextmanager
from io import StringIO, BytesIO
from urllib.parse import urljoin, urlsplit
import csv
import functools
import datetime
import gc
import psutil
import stripe
from decimal import Decimal, InvalidOperation
from concurrent.futures import ThreadPoolExecutor
from waitress import serve as waitress_serve
from verification_security import (
    build_wallet_ownership_message,
    canonical_sui_address,
    is_valid_verification_session_id,
)
from verification_config import (
    DEFAULT_PUBLIC_API_BASE_URL,
    DEFAULT_WALLET_CONNECT_URL,
    build_hosted_verification_url,
    normalize_public_api_base_url,
    normalize_wallet_connect_url,
)
from runtime_support import (
    DelayedTaskScheduler,
    RuntimeMetrics,
    SlidingWindowRateLimiter,
)
from enforcement_policy import (
    EnforcementDecision,
    GateStatus,
    decide_auto_removal,
    evaluate_gate,
)
from sui_gateway import (
    DEFAULT_SUI_GRAPHQL_URL,
    SuiGatewayError,
    SuiGraphQLGateway,
)

# ==================== Refactored Module Imports ====================
# HTML templates are in templates/ directory (Jinja2).

# ==================== Global Constants ===========================
CACHE_TTL = 1200      # Cache Time-To-Live in seconds (20 minutes)
VERIFICATION_CACHE_TTL = 60  # Freshness target for interactive verification checks
NFT_CACHE_TTL = 43200   # Cache Time-To-Live for NFTs in seconds (12 hours)
API_TIMEOUT = 60        # Timeout for API calls in seconds
SLEEP_BETWEEN_TASKS = int(os.getenv('WALLET_CHECK_INTERVAL_SECONDS', '172800'))
BOT_POLLING_TIMEOUT = 30  # Bot polling timeout (seconds)
BOT_LONG_POLLING_TIMEOUT = 10 # Bot long polling timeout (seconds)
REMINDER_THRESHOLD = 1200   # Reminder threshold in seconds (20 minutes)
VERIFICATION_TIMEOUT = 600  # Verification timeout in seconds (10 minute)
ALERT_COOLDOWN_DAYS = 2 # Days before re-alerting a user with low balance
# Alerts written by code before this version were recorded *before* Telegram
# delivery was attempted.  Keep those rows out of the cooldown so each current
# low-holdings user gets one retry under the delivery-safe implementation.
ALERT_DELIVERY_VERSION = 1
MAX_CACHE_SIZE = 1000  # Maximum number of entries in balance/NFT caches
NFT_PROVIDER_RETRY_DELAY = 2  # Seconds to wait before retrying an NFT provider check
DB_POOL_MIN = int(os.getenv('DB_POOL_MIN', '5'))  # Minimum database connections
DB_POOL_MAX = int(os.getenv('DB_POOL_MAX', '15'))  # Maximum database connections
TASK_JITTER_PERCENT = 0.1  # Add ±10% jitter to task intervals to prevent thundering herd
GROUP_CHECK_DELAY = 2     # Seconds between group checks to give the provider breathing room
SCHEDULER_LEASE_SECONDS = 300  # Recover quickly if a worker exits unexpectedly
SCHEDULER_LEASE_RENEW_SECONDS = 60
SCHEDULER_INSTANCE_ID = secrets.token_urlsafe(16)
DEFAULT_AUTO_REMOVE_GRACE_SECONDS = max(
    0,
    int(os.getenv("AUTO_REMOVE_GRACE_SECONDS", "86400")),
)
SUI_OPERATION_TIMEOUT_SECONDS = max(
    10,
    int(os.getenv("SUI_OPERATION_TIMEOUT_SECONDS", "90")),
)
SUI_MAX_PAGES = max(1, int(os.getenv("SUI_MAX_PAGES", "200")))
SUI_MAX_OBJECTS = max(50, int(os.getenv("SUI_MAX_OBJECTS", "10000")))
TELEGRAM_ALLOWED_UPDATES = [
    'message',
    'callback_query',
    'chat_member',
    'my_chat_member',
    'poll',
    'poll_answer',
    'pre_checkout_query',
]

# ==================== Logging Setup ==============================
console_handler = logging.StreamHandler()
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
console_handler.setFormatter(formatter)
log_handlers = [console_handler]
log_file = os.getenv("LOG_FILE", "").strip()
if log_file:
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
    )
    file_handler.setFormatter(formatter)
    log_handlers.append(file_handler)
logging.basicConfig(level=logging.INFO, handlers=log_handlers, force=True)

# ==================== Bot and Flask Configuration =================
BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
SUI_GRAPHQL_URLS = [
    value.strip()
    for value in os.getenv(
        'SUI_GRAPHQL_URLS',
        os.getenv('SUI_GRAPHQL_URL', DEFAULT_SUI_GRAPHQL_URL),
    ).split(',')
    if value.strip()
]
WALLET_CONNECT_ALLOWED_HOSTS = {
    value.strip().lower()
    for value in os.getenv(
        'WALLET_CONNECT_ALLOWED_HOSTS',
        'alphacity.tech,www.alphacity.tech',
    ).split(',')
    if value.strip()
}
try:
    WALLET_CONNECT_URL = normalize_wallet_connect_url(
        os.getenv('WALLET_CONNECT_URL', '').strip() or DEFAULT_WALLET_CONNECT_URL,
        WALLET_CONNECT_ALLOWED_HOSTS,
    )
except ValueError as exc:
    raise ValueError(f"Invalid wallet verification page configuration: {exc}") from exc
PUBLIC_WEBAPP_BASE_URL = os.getenv('PUBLIC_WEBAPP_BASE_URL', '').strip()
PUBLIC_API_BASE_URL = os.getenv('PUBLIC_API_BASE_URL', '').strip()
if PUBLIC_API_BASE_URL:
    try:
        PUBLIC_API_BASE_URL = normalize_public_api_base_url(PUBLIC_API_BASE_URL)
    except ValueError as exc:
        raise ValueError(f"Invalid public verification API configuration: {exc}") from exc
CORS_ALLOWED_ORIGINS = {
    value.strip().rstrip('/')
    for value in os.getenv(
        'CORS_ALLOWED_ORIGINS',
        'https://alphacity.tech,https://www.alphacity.tech',
    ).split(',')
    if value.strip()
}
TELEGRAM_WEBHOOK_URL = os.getenv('TELEGRAM_WEBHOOK_URL', '').strip()
TELEGRAM_WEBHOOK_SECRET = os.getenv('TELEGRAM_WEBHOOK_SECRET', '').strip()
METRICS_TOKEN = os.getenv('METRICS_TOKEN', '').strip()

# ==================== Stripe Configuration ========================
STRIPE_SECRET_KEY = os.getenv('STRIPE_SECRET_KEY', '').strip()
STRIPE_WEBHOOK_SECRET = os.getenv('STRIPE_WEBHOOK_SECRET', '').strip()
if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

# ==================== Bot Owner & Whitelist =======================
# Telegram user ID of the bot owner (for subscription management).
BOT_OWNER_ID = int(os.getenv('BOT_OWNER_ID', '0'))

# Groups that can use the bot WITHOUT an active subscription.
# Edit this set directly in code to add/remove whitelisted group IDs.
# This is intentionally kept at the code level for security, as the bot
# owner requested. Alternatively, populate from an environment variable:
#   WHITELISTED_GROUPS = set(int(g) for g in os.getenv('WHITELISTED_GROUPS', '').split(',') if g.strip())
WHITELISTED_GROUPS: set[int] = {
    -1002461611839,
    -1003393402791,
    -1002484520072,
}

# Subscription pricing tiers (amount in cents)
SUBSCRIPTION_TIERS = {
    "1month": {"label": "1 Month", "price_cents": 399, "days": 30, "display": "$3.99"},
    "3month": {"label": "3 Months", "price_cents": 1199, "days": 90, "display": "$11.99"},
    "6month": {"label": "6 Months", "price_cents": 2199, "days": 180, "display": "$21.99"},
}

BOT_NAME = "CityWatchBot"
CODE_SYNC_REV = (
    os.getenv("RENDER_GIT_COMMIT")
    or os.getenv("RAILWAY_GIT_COMMIT_SHA")
    or "post-graphql-hardening-2026-08-04"
)[:40]
ADMIN_MEMBER_STATUSES = frozenset({"creator", "administrator"})
ACTIVE_GROUP_MEMBER_STATUSES = frozenset({"creator", "administrator", "member", "restricted"})
INACTIVE_MEMBER_STATUS_MESSAGES = {
    "left": "❌ That user has left this group.",
    "kicked": "❌ That user was removed from this group."
}

# Verification sessions are server-stored, random, and single-use. Give users
# enough time to switch from Telegram to a system browser and unlock a wallet,
# while keeping abandoned links short-lived.
VERIFICATION_SESSION_TIMEOUT = max(
    300,
    int(os.getenv("VERIFY_SESSION_TIMEOUT_SECONDS", "900")),
)
VERIFICATION_SESSION_MAX_ATTEMPTS = max(
    1,
    int(os.getenv("VERIFY_SESSION_MAX_ATTEMPTS", "10")),
)
# A request claims its session while GraphQL verifies the signature and live
# holdings. The lease must exceed both bounded operations so an older worker
# can never finalize a claim that a newer worker recovered.
VERIFICATION_PROCESSING_LEASE_SECONDS = max(
    300,
    (2 * SUI_OPERATION_TIMEOUT_SECONDS) + 60,
    int(os.getenv("VERIFY_PROCESSING_LEASE_SECONDS", "300")),
)
WAITRESS_THREADS = max(8, int(os.getenv("WAITRESS_THREADS", "12")))
VERIFICATION_CONCURRENT_REQUESTS = max(
    1,
    min(
        WAITRESS_THREADS - 2,
        int(os.getenv("VERIFY_CONCURRENT_REQUESTS", "6")),
    ),
)
_verification_work_slots = threading.BoundedSemaphore(
    VERIFICATION_CONCURRENT_REQUESTS
)


class HoldingsUnavailableError(RuntimeError):
    """Voting holdings could not be authoritatively read from Sui."""


def require_canonical_sui_address(address):
    normalized = canonical_sui_address(address)
    if not normalized:
        raise ValueError("Invalid Sui address")
    return normalized


def encode_group_id_for_deeplink(group_id):
    """Encode a group ID for use in Telegram deep link start parameters.

    Telegram deep link parameters allow A-Z, a-z, 0-9, _ and - characters.
    However, some Telegram clients may strip or mishandle the '-' character
    (used for negative group IDs). This function replaces '-' with 'n' prefix
    to ensure reliable delivery of negative group IDs.
    """
    group_id = int(group_id)
    if group_id < 0:
        return f"n{abs(group_id)}"
    return str(group_id)


def decode_group_id_from_deeplink(encoded):
    """Decode a group ID from a Telegram deep link start parameter.

    Handles both the new 'n'-prefixed encoding (for negative IDs) and the
    legacy format where negative IDs used a literal '-' character.
    """
    if encoded.startswith("n"):
        return -int(encoded[1:])
    return int(encoded)


if not BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN not found in environment variables")

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = int(os.getenv('MAX_REQUEST_BYTES', '32768'))
sui_gateway = SuiGraphQLGateway(
    SUI_GRAPHQL_URLS,
    timeout_seconds=float(os.getenv('SUI_GRAPHQL_TIMEOUT_SECONDS', '20')),
    max_retries=int(os.getenv('SUI_GRAPHQL_RETRIES', '2')),
)
verification_rate_limiter = SlidingWindowRateLimiter(
    limit=int(os.getenv('VERIFY_RATE_LIMIT', '20')),
    window_seconds=int(os.getenv('VERIFY_RATE_WINDOW_SECONDS', '60')),
)
runtime_metrics = RuntimeMetrics()
_wallet_scan_state_lock = threading.Lock()
_wallet_scan_state = {
    "status": "not_started",
    "started_at": None,
    "completed_at": None,
    "last_error": None,
    "configured_groups": 0,
}

# Lazily cached bot username – populated on first call to get_bot_username().
_BOT_USERNAME = None

def get_bot_username():
    """Return the bot's Telegram username, caching it after the first API call."""
    global _BOT_USERNAME
    if _BOT_USERNAME is None:
        _BOT_USERNAME = bot.get_me().username
    return _BOT_USERNAME

# Lazily cached bot ID – populated on first call to get_bot_id().
_BOT_ID = None

def get_bot_id():
    """Return the bot's Telegram user ID, caching it after the first API call."""
    global _BOT_ID
    if _BOT_ID is None:
        _BOT_ID = bot.get_me().id
    return _BOT_ID

# Immediate work uses a bounded pool; delayed work waits on one priority queue.
_background_executor = ThreadPoolExecutor(
    max_workers=int(os.getenv('BACKGROUND_WORKERS', '4'))
)
_sui_executor = ThreadPoolExecutor(
    max_workers=int(os.getenv('SUI_FETCH_WORKERS', '8'))
)
_delayed_tasks = DelayedTaskScheduler()


def get_telegram_user_display_name(user):
    """Return the best available display name for a Telegram user."""
    full_name = " ".join(
        part for part in [user.first_name, getattr(user, "last_name", None)] if part
    ).strip()
    return getattr(user, "username", None) or full_name or f"User{user.id}"

# ==================== Database Setup =============================
db_lock = threading.Lock()
config_lock = threading.Lock()
cache_lock = threading.Lock()  # Protects balance_cache and nft_cache across threads
database_url = os.getenv('DATABASE_URL')
if not database_url:
    raise ValueError("DATABASE_URL not found in environment variables")

connection_pool = None
# Pool health-check throttle: only run SELECT 1 every N seconds (not on every call).
_POOL_CHECK_INTERVAL = 30  # seconds
_pool_last_check = [0.0]  # mutable container so we don't need a global statement


def get_public_webapp_base_url():
    """Resolve the bot's public web URL for verify and API endpoints.

    Preference order is PUBLIC_WEBAPP_BASE_URL, then Render's
    RENDER_EXTERNAL_URL, then PUBLIC_URL, then Railway's
    RAILWAY_PUBLIC_DOMAIN. Railway's domain variable is normalized to HTTPS
    because it is provided without a scheme.
    """
    public_base = (
        PUBLIC_WEBAPP_BASE_URL
        or os.getenv('RENDER_EXTERNAL_URL', '').strip()
        or os.getenv('PUBLIC_URL', '').strip()
    )
    if public_base:
        return public_base

    railway_public_domain = os.getenv('RAILWAY_PUBLIC_DOMAIN', '').strip()
    if railway_public_domain:
        if railway_public_domain.startswith(('http://', 'https://')):
            return railway_public_domain
        return f"https://{railway_public_domain}"

    return ''


def get_public_api_base_url():
    """Return the validated public origin used by the hosted verifier."""
    candidate = (
        PUBLIC_API_BASE_URL
        or get_public_webapp_base_url()
        or DEFAULT_PUBLIC_API_BASE_URL
    )
    try:
        return normalize_public_api_base_url(candidate)
    except ValueError as exc:
        raise RuntimeError(
            f"Public verification API URL is not configured safely: {exc}"
        ) from exc

def db_retry(func):
    """Decorator to handle database connection errors and retry."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        max_retries = 3
        for attempt in range(max_retries):
            try:
                return func(*args, **kwargs)
            except psycopg2.OperationalError as e:
                logging.warning(f"DB connection error in {func.__name__} (attempt {attempt + 1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)  # Exponential backoff
                    get_connection_pool() # Re-establish the pool
                else:
                    logging.error(f"DB operation failed after {max_retries} retries.")
                    raise  # Re-raise the last exception
    return wrapper

def get_connection_pool():
    global connection_pool, database_url
    # Fast path: return the existing pool if it was validated recently.
    # Only run an actual health check (SELECT 1) every _POOL_CHECK_INTERVAL
    # seconds to avoid a round-trip query on every database operation.
    if connection_pool:
        now = time.time()
        if now - _pool_last_check[0] < _POOL_CHECK_INTERVAL:
            return connection_pool
        # Time for a periodic health check
        try:
            conn = connection_pool.getconn()
            try:
                cur = conn.cursor()
                cur.execute("SELECT 1")
                cur.fetchone()
                cur.close()
                _pool_last_check[0] = now
                return connection_pool
            finally:
                connection_pool.putconn(conn)
        except Exception as e:
            logging.warning(f"Connection pool test failed, will recreate: {e}")
            try:
                if connection_pool:
                    connection_pool.closeall()
                    time.sleep(2)  # Brief pause before recreating
            except Exception as close_ex:
                logging.error(f"Error closing connections: {close_ex}")
            connection_pool = None

    # Slow path: pool is None (or was just cleared).  Acquire db_lock so that
    # two threads that simultaneously see connection_pool == None don't each
    # create a separate pool, leaking connections.
    with db_lock:
        # Re-check inside the lock — another thread may have created the pool
        # while we were waiting.
        if connection_pool:
            return connection_pool

        # Create a new connection pool with exponential back-off.
        # Use the original database_url directly without modification for production
        if not database_url:
            raise Exception("DATABASE_URL is not set")
        connection_string: str = database_url

        # Add SSL mode if not present (required for Neon)
        if 'sslmode=' not in connection_string:
            separator = '&' if '?' in connection_string else '?'
            connection_string = connection_string + f"{separator}sslmode=require"

        # Only use pooler for specific cases, not for production databases
        if '-pooler.' not in connection_string and 'neon.tech' in connection_string:
            # For Neon databases, add endpoint parameter for SNI support
            endpoint_match = re.search(r'@([^.]+)\.', connection_string)
            if endpoint_match:
                endpoint_id = endpoint_match.group(1)
                separator = '&' if '?' in connection_string else '?'
                connection_string = connection_string + f"{separator}options=endpoint%3D{endpoint_id}"
                logging.info(f"Added Neon endpoint parameter: {endpoint_id}")

        # Never log a connection URL: its user-info section contains the
        # database password.  Host metadata is sufficient for diagnostics.
        parsed_connection = urlsplit(connection_string)
        safe_target = parsed_connection.hostname or "unknown-host"
        if parsed_connection.port:
            safe_target = f"{safe_target}:{parsed_connection.port}"
        logging.info(f"Database connection target: {parsed_connection.scheme}://{safe_target} (credentials redacted)")

        tries = 0
        max_tries = 5
        backoff_time = 2
        while tries < max_tries:
            try:
                # Use the connection string directly with psycopg2 pool
                connection_pool = pool.ThreadedConnectionPool(
                    DB_POOL_MIN, DB_POOL_MAX,
                    connection_string,
                    connect_timeout=30,  # Increased timeout for production
                    application_name="wallet_alert_bot_production"
                )
                logging.info(f"Database connection pool created with {DB_POOL_MIN}-{DB_POOL_MAX} connections")
                conn = connection_pool.getconn()
                try:
                    cur = conn.cursor()
                    cur.execute("SELECT 1")
                    cur.fetchone()
                    cur.close()
                finally:
                    connection_pool.putconn(conn)
                logging.info("Created new database connection pool")
                return connection_pool
            except Exception as e:
                tries += 1
                error_msg = str(e).lower()

                # Handle specific authentication errors
                if 'password authentication failed' in error_msg:
                    logging.error(f"Authentication failed - check DATABASE_URL credentials (attempt {tries}/{max_tries})")
                    # Try refreshing the DATABASE_URL from environment
                    fresh_url = os.getenv('DATABASE_URL')
                    if fresh_url and fresh_url != database_url:
                        logging.info("Refreshing DATABASE_URL from environment")
                        database_url = fresh_url
                        connection_string = database_url
                        if 'sslmode=' not in connection_string:
                            separator = '&' if '?' in connection_string else '?'
                            connection_string += f"{separator}sslmode=require"
                else:
                    logging.error(f"Failed to create connection pool (attempt {tries}/{max_tries}): {e}")

                time.sleep(backoff_time)
                backoff_time *= 1.5
                if connection_pool:
                    try:
                        connection_pool.closeall()
                    except Exception:
                        pass
                    connection_pool = None

        logging.error("Could not create database connection pool after multiple attempts")
        raise Exception("Database connection failed")

@contextmanager
def get_db_cursor():
    pool_conn = None
    conn = None
    cur = None
    try:
        pool_conn = get_connection_pool()
        if not pool_conn:
            raise Exception("Could not establish connection pool")
        conn = pool_conn.getconn()
        if not conn:
            raise Exception("Could not get connection from pool")
        cur = conn.cursor()
        yield conn, cur
        conn.commit()  # Auto-commit successful operations
    except Exception as e:
        if conn:
            try:
                conn.rollback()
            except Exception as rollback_error:
                logging.error(f"Error during rollback: {rollback_error}")
        raise e
    finally:
        if cur:
            try:
                cur.close()
            except Exception as cur_error:
                logging.error(f"Error closing cursor: {cur_error}")
        if conn and pool_conn:
            try:
                pool_conn.putconn(conn)
            except Exception as e:
                logging.error(f"Error returning connection to pool: {e}")
                # If we can't return the connection, it might be dead - recreate pool
                if "closed" in str(e).lower() or "exhausted" in str(e).lower():
                    global connection_pool
                    connection_pool = None

@db_retry
def init_db():
    with get_db_cursor() as (conn, cur):
        cur.execute("""
            CREATE TABLE IF NOT EXISTS subscriber_configs (
                chat_id BIGINT PRIMARY KEY,
                token TEXT,
                minimum_holding NUMERIC(78, 18),
                decimals INTEGER DEFAULT 6,
                auto_remove BOOLEAN DEFAULT FALSE,
                auto_remove_grace_seconds INTEGER DEFAULT 86400,
                nft_collection_id TEXT DEFAULT '',
                nft_threshold INTEGER DEFAULT 1,
                registration_mode TEXT DEFAULT 'token'
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_wallets (
                group_id BIGINT,
                user_id BIGINT,
                username TEXT,
                wallets TEXT,
                is_exempt BOOLEAN DEFAULT FALSE,
                registration_type TEXT DEFAULT 'token',
                PRIMARY KEY (group_id, user_id)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS voting_polls (
                poll_id TEXT PRIMARY KEY,
                group_id BIGINT,
                creator_id BIGINT,
                title TEXT,
                options TEXT,
                message_id INTEGER,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS poll_votes (
                poll_id TEXT,
                user_id BIGINT,
                option_index INTEGER,
                vote_weight NUMERIC(78, 18),
                weight_checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                holdings_snapshot JSONB DEFAULT '{}'::jsonb,
                PRIMARY KEY (poll_id, user_id)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS low_balance_alerts (
                group_id BIGINT,
                user_id BIGINT,
                alert_sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (group_id, user_id)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS pending_verifications (
                user_id BIGINT PRIMARY KEY,
                group_id BIGINT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS verification_sessions (
                session_id TEXT PRIMARY KEY,
                group_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP NOT NULL,
                consumed_at TIMESTAMP DEFAULT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                processing_at TIMESTAMP DEFAULT NULL,
                completed_at TIMESTAMP DEFAULT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                last_attempt_at TIMESTAMP DEFAULT NULL,
                claim_id TEXT DEFAULT NULL,
                wallet_address TEXT DEFAULT NULL,
                eligibility_status TEXT DEFAULT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_wallet_addresses (
                group_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                wallet_address TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (group_id, user_id, wallet_address),
                UNIQUE (group_id, wallet_address),
                FOREIGN KEY (group_id, user_id)
                    REFERENCES user_wallets(group_id, user_id)
                    ON DELETE CASCADE
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                group_id BIGINT PRIMARY KEY,
                stripe_session_id TEXT,
                tier TEXT,
                activated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP NOT NULL,
                activated_by BIGINT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS stripe_processed_events (
                session_id TEXT PRIMARY KEY,
                processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS scheduler_leases (
                lease_name TEXT PRIMARY KEY,
                holder_id TEXT NOT NULL,
                expires_at TIMESTAMP NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS enforcement_states (
                group_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                first_failed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_checked_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                reason TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (group_id, user_id)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS enforcement_events (
                event_id BIGSERIAL PRIMARY KEY,
                group_id BIGINT NOT NULL,
                user_id BIGINT,
                action TEXT NOT NULL,
                status TEXT NOT NULL,
                details JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Create indexes for frequently queried columns to optimize performance
        cur.execute("CREATE INDEX IF NOT EXISTS idx_user_wallets_user ON user_wallets(user_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_voting_polls_group ON voting_polls(group_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_voting_polls_active ON voting_polls(is_active) WHERE is_active = TRUE")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_pending_verifications_created_at ON pending_verifications(created_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_verification_sessions_expiry ON verification_sessions(expires_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_wallet_addresses_user ON user_wallet_addresses(group_id, user_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_enforcement_events_group_time ON enforcement_events(group_id, created_at DESC)")
        try:
            cur.execute("ALTER TABLE subscriber_configs ADD COLUMN IF NOT EXISTS auto_remove BOOLEAN DEFAULT FALSE")
            cur.execute(
                "ALTER TABLE subscriber_configs ADD COLUMN IF NOT EXISTS "
                "auto_remove_grace_seconds INTEGER DEFAULT 86400"
            )
            cur.execute("ALTER TABLE subscriber_configs ADD COLUMN IF NOT EXISTS nft_collection_id TEXT DEFAULT ''")
            cur.execute("ALTER TABLE subscriber_configs ADD COLUMN IF NOT EXISTS nft_threshold INTEGER DEFAULT 1")
            cur.execute("ALTER TABLE subscriber_configs ADD COLUMN IF NOT EXISTS registration_mode TEXT DEFAULT 'token'")
            cur.execute("ALTER TABLE user_wallets ADD COLUMN IF NOT EXISTS registration_type TEXT DEFAULT 'token'")
            cur.execute("ALTER TABLE subscriber_configs ADD COLUMN IF NOT EXISTS votes_per_nft INTEGER DEFAULT 1")
            cur.execute("ALTER TABLE subscriber_configs ADD COLUMN IF NOT EXISTS votes_per_million_tokens INTEGER DEFAULT 1")
            cur.execute("ALTER TABLE subscriber_configs ADD COLUMN IF NOT EXISTS vote_duration INTEGER DEFAULT 3600")
            cur.execute("ALTER TABLE subscriber_configs ADD COLUMN IF NOT EXISTS votes_per_exempt INTEGER DEFAULT 1")
            cur.execute("ALTER TABLE voting_polls ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
            cur.execute("ALTER TABLE subscriber_configs ADD COLUMN IF NOT EXISTS nft_trait_name TEXT DEFAULT ''")
            cur.execute("ALTER TABLE subscriber_configs ADD COLUMN IF NOT EXISTS nft_trait_value TEXT DEFAULT ''")
            cur.execute("ALTER TABLE subscriber_configs ADD COLUMN IF NOT EXISTS nft_trait_threshold INTEGER DEFAULT 1")
            cur.execute("ALTER TABLE user_wallets ADD COLUMN IF NOT EXISTS last_nft_count INTEGER DEFAULT NULL")
            cur.execute("ALTER TABLE user_wallets ADD COLUMN IF NOT EXISTS last_trait_count INTEGER DEFAULT NULL")
            cur.execute("ALTER TABLE user_wallets ADD COLUMN IF NOT EXISTS last_token_balance NUMERIC(78, 18) DEFAULT NULL")
            cur.execute("ALTER TABLE user_wallets ADD COLUMN IF NOT EXISTS holdings_updated_at TIMESTAMP DEFAULT NULL")
            cur.execute("ALTER TABLE low_balance_alerts ADD COLUMN IF NOT EXISTS delivery_version INTEGER DEFAULT 0")
            cur.execute("ALTER TABLE verification_sessions ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'pending'")
            cur.execute("ALTER TABLE verification_sessions ADD COLUMN IF NOT EXISTS processing_at TIMESTAMP DEFAULT NULL")
            cur.execute("ALTER TABLE verification_sessions ADD COLUMN IF NOT EXISTS completed_at TIMESTAMP DEFAULT NULL")
            cur.execute(
                "ALTER TABLE verification_sessions ADD COLUMN IF NOT EXISTS "
                "attempt_count INTEGER NOT NULL DEFAULT 0"
            )
            cur.execute(
                "ALTER TABLE verification_sessions ADD COLUMN IF NOT EXISTS "
                "last_attempt_at TIMESTAMP DEFAULT NULL"
            )
            cur.execute(
                "ALTER TABLE verification_sessions ADD COLUMN IF NOT EXISTS "
                "claim_id TEXT DEFAULT NULL"
            )
            cur.execute(
                "ALTER TABLE verification_sessions ADD COLUMN IF NOT EXISTS "
                "wallet_address TEXT DEFAULT NULL"
            )
            cur.execute(
                "ALTER TABLE verification_sessions ADD COLUMN IF NOT EXISTS "
                "eligibility_status TEXT DEFAULT NULL"
            )
            cur.execute(
                "ALTER TABLE poll_votes ADD COLUMN IF NOT EXISTS "
                "weight_checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
            )
            cur.execute(
                "ALTER TABLE poll_votes ADD COLUMN IF NOT EXISTS "
                "holdings_snapshot JSONB DEFAULT '{}'::jsonb"
            )
            cur.execute("""
                UPDATE verification_sessions
                SET status = 'completed',
                    completed_at = COALESCE(completed_at, consumed_at)
                WHERE consumed_at IS NOT NULL
                  AND completed_at IS NULL
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_verification_sessions_status ON verification_sessions(status, expires_at)")

            migration_version = "20260803_graphql_verification"
            cur.execute(
                "SELECT 1 FROM schema_migrations WHERE version = %s",
                (migration_version,),
            )
            if cur.fetchone() is None:
                cur.execute("""
                    ALTER TABLE subscriber_configs
                    ALTER COLUMN minimum_holding TYPE NUMERIC(78, 18)
                    USING minimum_holding::NUMERIC
                """)
                cur.execute("""
                    ALTER TABLE poll_votes
                    ALTER COLUMN vote_weight TYPE NUMERIC(78, 18)
                    USING vote_weight::NUMERIC
                """)
                cur.execute("""
                    ALTER TABLE user_wallets
                    ALTER COLUMN last_token_balance TYPE NUMERIC(78, 18)
                    USING last_token_balance::NUMERIC
                """)
                cur.execute(
                    """
                    SELECT group_id, user_id, wallets
                    FROM user_wallets
                    ORDER BY group_id, user_id
                    """
                )
                for group_id, user_id, wallets_json in cur.fetchall():
                    try:
                        stored_wallets = json.loads(wallets_json) if wallets_json else []
                    except (TypeError, ValueError):
                        logging.warning(
                            "Skipping malformed wallet JSON during normalized-address backfill "
                            "for group=%s user=%s",
                            group_id,
                            user_id,
                        )
                        continue
                    for stored_wallet in stored_wallets:
                        try:
                            normalized_wallet = require_canonical_sui_address(stored_wallet)
                        except ValueError:
                            logging.warning(
                                "Skipping invalid wallet during normalized-address backfill "
                                "for group=%s user=%s",
                                group_id,
                                user_id,
                            )
                            continue
                        cur.execute(
                            """
                            INSERT INTO user_wallet_addresses
                                (group_id, user_id, wallet_address)
                            VALUES (%s, %s, %s)
                            ON CONFLICT DO NOTHING
                            """,
                            (group_id, user_id, normalized_wallet),
                        )
                cur.execute(
                    "INSERT INTO schema_migrations(version) VALUES (%s)",
                    (migration_version,),
                )
        except Exception as e:
            logging.exception("Database migration failed; refusing to start with a partial schema")
            raise
        logging.info("Database initialized successfully")
        return True

init_db()

@db_retry
def load_configs_from_db():
    with get_db_cursor() as (conn, cur):
        cur.execute("""SELECT chat_id, token, minimum_holding, decimals, auto_remove,
            COALESCE(auto_remove_grace_seconds, 86400),
            nft_collection_id, nft_threshold, registration_mode, votes_per_nft, 
            votes_per_million_tokens, vote_duration, votes_per_exempt,
            COALESCE(nft_trait_name, '') as nft_trait_name,
            COALESCE(nft_trait_value, '') as nft_trait_value,
            COALESCE(nft_trait_threshold, 1) as nft_trait_threshold
            FROM subscriber_configs""")
        rows = cur.fetchall()
    configs = {}
    for row in rows:
        (chat_id, token, minimum_holding, decimals, auto_remove,
         auto_remove_grace_seconds,
         nft_collection_id, nft_threshold, registration_mode, votes_per_nft, 
         votes_per_million_tokens, vote_duration, votes_per_exempt,
         nft_trait_name, nft_trait_value, nft_trait_threshold) = row
        configs[chat_id] = {
            "token": token,
            "minimum_holding": minimum_holding,
            "decimals": decimals,
            "auto_remove": auto_remove if auto_remove is not None else False,
            "auto_remove_grace_seconds": max(
                0,
                auto_remove_grace_seconds
                if auto_remove_grace_seconds is not None
                else DEFAULT_AUTO_REMOVE_GRACE_SECONDS,
            ),
            "nft_collection_id": nft_collection_id or "",
            "nft_threshold": nft_threshold or 1,
            "registration_mode": registration_mode or "token",
            "votes_per_nft": votes_per_nft or 1,
            "votes_per_million_tokens": votes_per_million_tokens or 1,
            "vote_duration": vote_duration or 3600,
            "votes_per_exempt": votes_per_exempt or 1,
            "nft_trait_name": nft_trait_name or "",
            "nft_trait_value": nft_trait_value or "",
            "nft_trait_threshold": nft_trait_threshold or 1
        }
    return configs

@db_retry
def update_config_in_db(chat_id, config):
    with get_db_cursor() as (conn, cur):
        decimals = config.get("decimals", 6)
        votes_per_nft = config.get("votes_per_nft", 1)
        votes_per_million_tokens = config.get("votes_per_million_tokens", 1)
        vote_duration = config.get("vote_duration", 3600)
        votes_per_exempt = config.get("votes_per_exempt", 1)

        nft_trait_name = config.get("nft_trait_name", "")
        nft_trait_value = config.get("nft_trait_value", "")
        nft_trait_threshold = config.get("nft_trait_threshold", 1)
        auto_remove_grace_seconds = max(
            0,
            int(
                config.get(
                    "auto_remove_grace_seconds",
                    DEFAULT_AUTO_REMOVE_GRACE_SECONDS,
                )
            ),
        )

        cur.execute("""
            INSERT INTO subscriber_configs (
                chat_id, token, minimum_holding, decimals, auto_remove,
                auto_remove_grace_seconds,
                nft_collection_id, nft_threshold, registration_mode, votes_per_nft, 
                votes_per_million_tokens, vote_duration, votes_per_exempt,
                nft_trait_name, nft_trait_value, nft_trait_threshold)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(chat_id) DO UPDATE SET
                token=EXCLUDED.token,
                minimum_holding=EXCLUDED.minimum_holding,
                decimals=EXCLUDED.decimals,
                auto_remove=EXCLUDED.auto_remove,
                auto_remove_grace_seconds=EXCLUDED.auto_remove_grace_seconds,
                nft_collection_id=EXCLUDED.nft_collection_id,
                nft_threshold=EXCLUDED.nft_threshold,
                registration_mode=EXCLUDED.registration_mode,
                votes_per_nft=EXCLUDED.votes_per_nft,
                votes_per_million_tokens=EXCLUDED.votes_per_million_tokens,
                vote_duration=EXCLUDED.vote_duration,
                votes_per_exempt=EXCLUDED.votes_per_exempt,
                nft_trait_name=EXCLUDED.nft_trait_name,
                nft_trait_value=EXCLUDED.nft_trait_value,
                nft_trait_threshold=EXCLUDED.nft_trait_threshold
        """, (chat_id, config.get("token", ""), config.get("minimum_holding", 5000000), decimals,
              config.get("auto_remove", False), auto_remove_grace_seconds, config.get("nft_collection_id", ""),
              config.get("nft_threshold", 1), config.get("registration_mode", "token"), 
              votes_per_nft, votes_per_million_tokens, vote_duration, votes_per_exempt,
              nft_trait_name, nft_trait_value, nft_trait_threshold))
        return True

@db_retry
def ensure_config_exists(group_id):
    """Ensure a group configuration exists, creating a default one if needed.
    
    This function MUST be called with config_lock already held, or outside of any lock.
    Returns the config dict for the group.
    """
    if group_id not in SUBSCRIBER_CONFIGS:
        SUBSCRIBER_CONFIGS[group_id] = {
            "token": "",
            "minimum_holding": 5000000,
            "decimals": 6,
            "auto_remove": False,
            "auto_remove_grace_seconds": DEFAULT_AUTO_REMOVE_GRACE_SECONDS,
            "nft_collection_id": "",
            "nft_threshold": 1,
            "registration_mode": "token",
            "votes_per_nft": 1,
            "votes_per_million_tokens": 1,
            "vote_duration": 3600,
            "votes_per_exempt": 1,
            "nft_trait_name": "",
            "nft_trait_value": "",
            "nft_trait_threshold": 1
        }
        update_config_in_db(group_id, SUBSCRIBER_CONFIGS[group_id])
        logging.info(f"Created default configuration for group {group_id}")
    return SUBSCRIBER_CONFIGS[group_id]

def get_registration_mode_display(mode):
    """Convert registration mode to human-readable display text."""
    mode_display = {
        "token": "Token",
        "nft": "NFT",
        "both": "NFT or Token"
    }
    return mode_display.get(mode, mode.title())

def _save_wallet_for_user_with_cursor(
    cur,
    group_id,
    user_id,
    username,
    wallet_list,
    *,
    is_exempt=None,
    replace_existing=False,
    registration_type="token",
):
    """Save JSON compatibility data and normalized addresses in one transaction."""
    normalized_incoming = [require_canonical_sui_address(wallet) for wallet in wallet_list]
    existing_wallets = []
    existing_exempt = False

    # Materialize the row before locking it. SELECT ... FOR UPDATE cannot lock
    # a row that does not exist, which allowed two first-time registrations for
    # the same user to race and overwrite one another's wallet list.
    cur.execute(
        """
        INSERT INTO user_wallets
            (group_id, user_id, username, wallets, is_exempt, registration_type)
        VALUES (%s, %s, %s, %s, FALSE, %s)
        ON CONFLICT (group_id, user_id) DO NOTHING
        """,
        (group_id, user_id, username, json.dumps([]), registration_type),
    )
    cur.execute(
        """
        SELECT wallets, is_exempt
        FROM user_wallets
        WHERE group_id = %s AND user_id = %s
        FOR UPDATE
        """,
        (group_id, user_id),
    )
    result = cur.fetchone()
    if result:
        try:
            if result[0]:
                existing_wallets = json.loads(result[0])
            existing_exempt = bool(result[1])
        except (TypeError, ValueError):
            logging.warning(
                "Ignoring malformed stored wallet list for group=%s user=%s",
                group_id,
                user_id,
            )

    if is_exempt is None:
        is_exempt = existing_exempt
    else:
        is_exempt = bool(is_exempt or existing_exempt)

    normalized_existing = []
    if existing_wallets and not replace_existing:
        for wallet in existing_wallets:
            try:
                normalized_existing.append(require_canonical_sui_address(wallet))
            except ValueError:
                logging.warning(
                    "Ignoring invalid stored wallet for group=%s user=%s",
                    group_id,
                    user_id,
                )
    combined_wallets = list(
        dict.fromkeys(
            normalized_incoming
            if replace_existing
            else normalized_incoming + normalized_existing
        )
    )
    wallets_json = json.dumps(combined_wallets)
    cur.execute(
        """
        INSERT INTO user_wallets
            (group_id, user_id, username, wallets, is_exempt, registration_type)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT(group_id, user_id) DO UPDATE SET
            username = EXCLUDED.username,
            wallets = EXCLUDED.wallets,
            is_exempt = EXCLUDED.is_exempt,
            registration_type = EXCLUDED.registration_type
        """,
        (
            group_id,
            user_id,
            username,
            wallets_json,
            is_exempt,
            registration_type,
        ),
    )
    cur.execute(
        "DELETE FROM user_wallet_addresses WHERE group_id = %s AND user_id = %s",
        (group_id, user_id),
    )
    for wallet in combined_wallets:
        cur.execute(
            """
            INSERT INTO user_wallet_addresses
                (group_id, user_id, wallet_address)
            VALUES (%s, %s, %s)
            """,
            (group_id, user_id, wallet),
        )
    logging.info(
        "Saved %s wallet(s) for group=%s user=%s",
        len(combined_wallets),
        group_id,
        user_id,
    )
    return True


@db_retry
def save_wallet_for_user(
    group_id,
    user_id,
    username,
    wallet_list,
    is_exempt=None,
    replace_existing=False,
    registration_type="token",
):
    with get_db_cursor() as (conn, cur):
        return _save_wallet_for_user_with_cursor(
            cur,
            group_id,
            user_id,
            username,
            wallet_list,
            is_exempt=is_exempt,
            replace_existing=replace_existing,
            registration_type=registration_type,
        )

@db_retry
def get_user_registration(group_id, user_id):
    """Fetches all registration details for a single user."""
    with get_db_cursor() as (conn, cur):
        cur.execute("SELECT username, wallets, is_exempt FROM user_wallets WHERE group_id=%s AND user_id=%s", (group_id, user_id))
        result = cur.fetchone()
        if not result:
            return None

        username, wallets_json, is_exempt = result
        try:
            wallets = json.loads(wallets_json) if wallets_json else []
        except Exception:
            wallets = []

        return {
            "username": username,
            "wallets": wallets,
            "is_exempt": is_exempt
        }

@db_retry
def get_user_registrations_for_group(group_id):
    with get_db_cursor() as (conn, cur):
        cur.execute("SELECT user_id, username, wallets, is_exempt FROM user_wallets WHERE group_id=%s", (group_id,))
        rows = cur.fetchall()
    registrations = []
    for row in rows:
        user_id, username, wallets_json, is_exempt = row
        try:
            wallets = json.loads(wallets_json) if wallets_json else []
        except Exception as e:
            logging.error(f"Error parsing wallets for user {username}: {e}, raw JSON: {wallets_json}")
            wallets = []
        registrations.append({
            "user_id": user_id,
            "username": username,
            "wallets": wallets,
            "is_exempt": is_exempt
        })
    return registrations

@db_retry
def update_user_cached_holdings(group_id, user_id, nft_count=None, trait_count=None, token_balance=None):
    """Persist last-known on-chain holdings so display functions can fall back
    to them when live provider lookups fail.

    Parameters
    ----------
    group_id : int
        Telegram group/chat ID.
    user_id : int
        Telegram user ID.
    nft_count : int or None
        Last successful NFT count across all user wallets.
    trait_count : int or None
        Last successful trait-matching NFT count.
    token_balance : float or None
        Last successful aggregate token balance.

    Only non-None values are written; the rest remain unchanged.  If every
    optional parameter is None the function returns without touching the DB.
    """
    updates = []
    params = []
    if nft_count is not None:
        updates.append("last_nft_count = %s")
        params.append(nft_count)
    if trait_count is not None:
        updates.append("last_trait_count = %s")
        params.append(trait_count)
    if token_balance is not None:
        updates.append("last_token_balance = %s")
        params.append(token_balance)
    if not updates:
        return
    updates.append("holdings_updated_at = NOW()")
    params.extend([group_id, user_id])
    # Safety: the SET clause is built from a fixed set of hard-coded column
    # literals above — no external input is interpolated into the SQL.
    with get_db_cursor() as (conn, cur):
        cur.execute(
            f"UPDATE user_wallets SET {', '.join(updates)} WHERE group_id = %s AND user_id = %s",
            tuple(params),
        )

@db_retry
def get_user_cached_holdings(group_id, user_id):
    """Retrieve previously cached on-chain holdings for a user.

    Parameters
    ----------
    group_id : int
        Telegram group/chat ID.
    user_id : int
        Telegram user ID.

    Returns
    -------
    dict or None
        ``{"nft_count": int|None, "trait_count": int|None, "token_balance": float|None}``
        if a row exists, otherwise ``None``.
    """
    with get_db_cursor() as (conn, cur):
        cur.execute(
            "SELECT last_nft_count, last_trait_count, last_token_balance FROM user_wallets WHERE group_id = %s AND user_id = %s",
            (group_id, user_id),
        )
        result = cur.fetchone()
        if result:
            return {"nft_count": result[0], "trait_count": result[1], "token_balance": result[2]}
    return None

# ==================== Subscription Helpers ========================

def is_group_whitelisted(group_id):
    """Check if a group is in the owner-controlled whitelist."""
    return int(group_id) in WHITELISTED_GROUPS

@db_retry
def get_group_subscription(group_id):
    """Return the subscription row for a group, or None."""
    with get_db_cursor() as (conn, cur):
        cur.execute(
            "SELECT group_id, stripe_session_id, tier, activated_at, expires_at, activated_by "
            "FROM subscriptions WHERE group_id = %s",
            (group_id,),
        )
        row = cur.fetchone()
        if row:
            return {
                "group_id": row[0],
                "stripe_session_id": row[1],
                "tier": row[2],
                "activated_at": row[3],
                "expires_at": row[4],
                "activated_by": row[5],
            }
    return None

def group_has_active_subscription(group_id):
    """Return True if the group has a non-expired subscription or is whitelisted."""
    if is_group_whitelisted(group_id):
        logging.debug(f"Group {group_id} is whitelisted, bypassing subscription check.")
        return True
    sub = get_group_subscription(group_id)
    if sub and sub["expires_at"]:
        now = datetime.datetime.now(datetime.timezone.utc)
        expires = sub["expires_at"]
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=datetime.timezone.utc)
        if now < expires:
            return True
        logging.info(f"Group {group_id} subscription expired at {expires}.")
    return False

@db_retry
def activate_subscription_from_stripe(group_id, stripe_session_id, tier, activated_by):
    """Atomically claim a Stripe checkout session and extend a subscription.

    Stripe can deliver the same event concurrently.  The event claim and the
    expiry calculation must therefore happen in one database transaction;
    checking first and inserting later can grant the same purchase twice.
    Returns ``(new_expiry, processed_now)``.
    """
    tier_info = SUBSCRIPTION_TIERS.get(tier)
    if not tier_info:
        raise ValueError(f"Unknown subscription tier: {tier}")
    if not stripe_session_id:
        raise ValueError("Stripe session ID is required")

    now = datetime.datetime.now(datetime.timezone.utc)
    with get_db_cursor() as (conn, cur):
        cur.execute(
            "INSERT INTO stripe_processed_events (session_id) VALUES (%s) ON CONFLICT DO NOTHING RETURNING session_id",
            (stripe_session_id,),
        )
        if cur.fetchone() is None:
            return None, False

        # Lock the group row while computing its extension point, so two
        # different valid purchases for one group cannot overwrite each other.
        cur.execute("SELECT expires_at FROM subscriptions WHERE group_id = %s FOR UPDATE", (group_id,))
        row = cur.fetchone()
        existing_expiry = row[0] if row else None
        if existing_expiry and existing_expiry.tzinfo is None:
            existing_expiry = existing_expiry.replace(tzinfo=datetime.timezone.utc)
        new_expiry = (
            existing_expiry + datetime.timedelta(days=tier_info["days"])
            if existing_expiry and existing_expiry > now
            else now + datetime.timedelta(days=tier_info["days"])
        )
        cur.execute(
            """
            INSERT INTO subscriptions (group_id, stripe_session_id, tier, activated_at, expires_at, activated_by)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (group_id) DO UPDATE SET
                stripe_session_id = EXCLUDED.stripe_session_id,
                tier = EXCLUDED.tier,
                activated_at = EXCLUDED.activated_at,
                expires_at = EXCLUDED.expires_at,
                activated_by = EXCLUDED.activated_by
            """,
            (group_id, stripe_session_id, tier, now, new_expiry, activated_by),
        )
    return new_expiry, True

def create_stripe_checkout_session(group_id, user_id, tier):
    """Create a Stripe Checkout Session for a subscription purchase."""
    if not STRIPE_SECRET_KEY:
        raise RuntimeError("Stripe is not configured. Ensure STRIPE_SECRET_KEY is set in environment variables.")
    tier_info = SUBSCRIPTION_TIERS.get(tier)
    if not tier_info:
        raise ValueError(f"Unknown tier: {tier}")
    # Determine success/cancel URL
    base_url = get_public_webapp_base_url()
    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[{
            "price_data": {
                "currency": "usd",
                "unit_amount": tier_info["price_cents"],
                "product_data": {
                    "name": f"CityWatchBot – {tier_info['label']} Subscription",
                    "description": f"Token-gating bot subscription for {tier_info['label'].lower()}",
                },
            },
            "quantity": 1,
        }],
        mode="payment",
        success_url=f"{base_url}/subscription/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{base_url}/subscription/cancel",
        metadata={
            "group_id": str(group_id),
            "user_id": str(user_id),
            "tier": tier,
        },
    )
    return session

@db_retry
def wallet_already_registered(wallet_address, group_id, user_id=None):
    """Check if a wallet is already registered in the group.

    When *user_id* is provided the check is ownership-aware:
    • If the wallet belongs to that same user → return ``False`` (allow
      re-registration so on-chain values are refreshed).
    • If the wallet belongs to a *different* user → return ``True``.

    When *user_id* is ``None`` the behavior is unchanged (legacy callers).
    """
    wallet_address = require_canonical_sui_address(wallet_address)
    with get_db_cursor() as (conn, cur):
        cur.execute(
            """
            SELECT user_id
            FROM user_wallet_addresses
            WHERE group_id = %s AND wallet_address = %s
            """,
            (group_id, wallet_address),
        )
        row = cur.fetchone()
    if row is None:
        return False
    return not (user_id is not None and row[0] == user_id)

@db_retry
def toggle_user_exemption(group_id, user_id, exempt_status):
    # 1. First, try to find and update the existing user in one transaction.
    with get_db_cursor() as (conn, cur):
        cur.execute("SELECT is_exempt FROM user_wallets WHERE group_id=%s AND user_id=%s", (group_id, user_id))
        result = cur.fetchone()

        if result:
            logging.info(f"Updating exemption for existing user {user_id} to {exempt_status}")
            cur.execute(
                "UPDATE user_wallets SET is_exempt = %s WHERE group_id = %s AND user_id = %s",
                (exempt_status, group_id, user_id)
            )
            return True # The 'with' block handles the commit, and we are done.

    # 2. If the user was not found, we exit the first DB block. 
    # Now we can safely make the slow network call without holding a connection.
    username = f"User{user_id}"
    try:
        logging.info(f"User {user_id} not in DB. Fetching info from Telegram...")
        user_info = bot.get_chat_member(group_id, user_id)
        if user_info and user_info.user:
            if user_info.user.username:
                username = f"@{user_info.user.username}"
            elif user_info.user.first_name:
                username = user_info.user.first_name
    except Exception as e:
        logging.warning(f"Could not get user info for {user_id}: {e}, using default username")

    # 3. Now, open a new, short DB transaction just to insert the new user.
    with get_db_cursor() as (conn, cur):
        logging.info(f"Creating new exemption record for user {username} ({user_id})")
        wallets_json = json.dumps([])
        cur.execute(
            "INSERT INTO user_wallets (group_id, user_id, username, wallets, is_exempt, registration_type) VALUES (%s, %s, %s, %s, %s, %s)",
            (group_id, user_id, username, wallets_json, exempt_status, 'exempt')
        )
    return True

# ==================== Global Variables =============================
SUBSCRIBER_CONFIGS = load_configs_from_db()
balance_cache = {}
nft_cache = {}
last_registration_prompt = {}


def create_verification_session(group_id, user_id, *, update_pending_context=False):
    """Create an expiring verification session."""
    session_id = secrets.token_urlsafe(32)
    with get_db_cursor() as (conn, cur):
        if update_pending_context:
            cur.execute(
                """
                INSERT INTO pending_verifications (user_id, group_id, created_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (user_id) DO UPDATE SET
                    group_id = EXCLUDED.group_id,
                    created_at = EXCLUDED.created_at
                """,
                (user_id, group_id),
            )
        cur.execute(
            """
            INSERT INTO verification_sessions (session_id, group_id, user_id, expires_at)
            VALUES (%s, %s, %s, NOW() + (%s * INTERVAL '1 second'))
            """,
            (session_id, group_id, user_id, VERIFICATION_SESSION_TIMEOUT),
        )
    return session_id


@db_retry
def get_active_verification_session(session_id):
    """Return an unfinished, unexpired verification session, if one exists."""
    if not isinstance(session_id, str) or not session_id:
        return None
    with get_db_cursor() as (conn, cur):
        cur.execute(
            """
            SELECT group_id, user_id
            FROM verification_sessions
            WHERE session_id = %s
              AND status IN ('pending', 'processing')
              AND completed_at IS NULL
              AND consumed_at IS NULL
              AND expires_at > NOW()
            """,
            (session_id,),
        )
        row = cur.fetchone()
    if row:
        return {"group_id": row[0], "user_id": row[1]}
    return None


@db_retry
def get_verification_session_group(session_id):
    """Return the group for a known session, including recently expired ones."""
    if not isinstance(session_id, str) or not session_id:
        return None
    with get_db_cursor() as (conn, cur):
        cur.execute(
            """
            SELECT group_id
            FROM verification_sessions
            WHERE session_id = %s
            """,
            (session_id,),
        )
        row = cur.fetchone()
        return row[0] if row else None


@db_retry
def get_completed_verification_result(session_id, wallet_address=None):
    """Return the durable result for a completed session, if available."""
    if not isinstance(session_id, str) or not session_id:
        return None
    canonical_wallet = None
    if wallet_address:
        canonical_wallet = require_canonical_sui_address(wallet_address)
    with get_db_cursor() as (conn, cur):
        cur.execute(
            """
            SELECT group_id, user_id, wallet_address, eligibility_status
            FROM verification_sessions
            WHERE session_id = %s
              AND status = 'completed'
              AND completed_at IS NOT NULL
              AND consumed_at IS NOT NULL
            """,
            (session_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    if canonical_wallet and row[2] != canonical_wallet:
        return None
    return {
        "group_id": row[0],
        "user_id": row[1],
        "wallet_address": row[2],
        "eligibility_status": row[3] or "unknown",
    }


def claim_verification_session(session_id, group_id, user_id, claim_id):
    """Claim a session and consume exactly one attempt in the same transaction."""
    with get_db_cursor() as (conn, cur):
        cur.execute(
            """
            UPDATE verification_sessions
            SET status = 'processing',
                processing_at = CASE
                    WHEN claim_id = %s THEN processing_at ELSE NOW()
                END,
                attempt_count = attempt_count + CASE
                    WHEN claim_id = %s THEN 0 ELSE 1
                END,
                last_attempt_at = CASE
                    WHEN claim_id = %s THEN last_attempt_at ELSE NOW()
                END,
                claim_id = %s
            WHERE session_id = %s
              AND group_id = %s
              AND user_id = %s
              AND completed_at IS NULL
              AND consumed_at IS NULL
              AND expires_at > NOW()
              AND (
                    (status = 'processing' AND claim_id = %s)
                    OR (
                        attempt_count < %s
                        AND (
                            status = 'pending'
                            OR (
                                status = 'processing'
                                AND processing_at < NOW() - (%s * INTERVAL '1 second')
                            )
                        )
                    )
              )
            RETURNING claim_id
            """,
            (
                claim_id,
                claim_id,
                claim_id,
                claim_id,
                session_id,
                group_id,
                user_id,
                claim_id,
                VERIFICATION_SESSION_MAX_ATTEMPTS,
                VERIFICATION_PROCESSING_LEASE_SECONDS,
            ),
        )
        row = cur.fetchone()
        return row[0] if row else None


@db_retry
def get_verification_attempt_state(session_id):
    """Return attempt state used to distinguish contention from exhaustion."""
    with get_db_cursor() as (conn, cur):
        cur.execute(
            """
            SELECT attempt_count, status, completed_at, consumed_at
            FROM verification_sessions
            WHERE session_id = %s
            """,
            (session_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    return {
        "attempt_count": row[0],
        "status": row[1],
        "completed": row[2] is not None or row[3] is not None,
    }


def release_verification_session(session_id, group_id, user_id, claim_id):
    """Return a claimed session to pending after an indeterminate failure."""
    with get_db_cursor() as (conn, cur):
        cur.execute(
            """
            UPDATE verification_sessions
            SET status = 'pending', processing_at = NULL, claim_id = NULL
            WHERE session_id = %s
              AND group_id = %s
              AND user_id = %s
              AND status = 'processing'
              AND claim_id = %s
              AND completed_at IS NULL
              AND consumed_at IS NULL
            """,
            (session_id, group_id, user_id, claim_id),
        )
        return cur.rowcount == 1


def finalize_verified_wallet(
    session_id,
    group_id,
    user_id,
    username,
    wallet_address,
    registration_type,
    eligibility_status,
    claim_id,
):
    """Atomically save a verified wallet and complete its claimed session."""
    with get_db_cursor() as (conn, cur):
        cur.execute(
            """
            SELECT status
            FROM verification_sessions
            WHERE session_id = %s
              AND group_id = %s
              AND user_id = %s
              AND completed_at IS NULL
              AND consumed_at IS NULL
              AND status = 'processing'
              AND claim_id = %s
              AND processing_at >= NOW() - (%s * INTERVAL '1 second')
            FOR UPDATE
            """,
            (
                session_id,
                group_id,
                user_id,
                claim_id,
                VERIFICATION_PROCESSING_LEASE_SECONDS,
            ),
        )
        row = cur.fetchone()
        if row is None:
            cur.execute(
                """
                SELECT wallet_address, eligibility_status
                FROM verification_sessions
                WHERE session_id = %s
                  AND group_id = %s
                  AND user_id = %s
                  AND status = 'completed'
                  AND consumed_at IS NOT NULL
                """,
                (session_id, group_id, user_id),
            )
            completed = cur.fetchone()
            return bool(
                completed
                and completed[0] == wallet_address
                and completed[1] == eligibility_status
            )
        if row[0] != 'processing':
            return False
        _save_wallet_for_user_with_cursor(
            cur,
            group_id,
            user_id,
            username,
            [wallet_address],
            registration_type=registration_type,
        )
        cur.execute(
            """
            DELETE FROM pending_verifications
            WHERE user_id = %s AND group_id = %s
            """,
            (user_id, group_id),
        )
        cur.execute(
            """
            UPDATE verification_sessions
            SET status = 'completed',
                completed_at = NOW(),
                consumed_at = NOW(),
                processing_at = NULL,
                claim_id = NULL,
                wallet_address = %s,
                eligibility_status = %s
            WHERE session_id = %s
            """,
            (wallet_address, eligibility_status, session_id),
        )
        return cur.rowcount == 1

# ==================== Cleanup Functions =============================
def cleanup_expired_data():
    """Clean up expired registrations, verifications, and prompts"""
    current_time = time.time()

    # Clean up old registration prompts (older than 1 hour)
    expired_prompts = [k for k, timestamp in last_registration_prompt.items() 
                       if current_time - timestamp > 3600]
    for key in expired_prompts:
        del last_registration_prompt[key]

    # Clean up expired poll creation contexts (older than 10 minutes)
    expired_polls = [k for k, v in poll_creation_context.items() 
                     if current_time - v['timestamp'] > 600]
    for key in expired_polls:
        del poll_creation_context[key]

    # Clean up expired pending verification contexts and sessions from the database.
    try:
        with get_db_cursor() as (conn, cur):
            cur.execute(
                """
                DELETE FROM pending_verifications
                WHERE created_at < NOW() - (%s * INTERVAL '1 second')
                """,
                (VERIFICATION_TIMEOUT,),
            )
            if cur.rowcount > 0:
                logging.info(f"Cleaned up {cur.rowcount} expired pending verifications from DB.")
            cur.execute("DELETE FROM verification_sessions WHERE expires_at < NOW() - INTERVAL '1 day'")
            if cur.rowcount > 0:
                logging.info(f"Cleaned up {cur.rowcount} expired verification sessions from DB.")
            cur.execute(
                "DELETE FROM enforcement_events "
                "WHERE created_at < NOW() - INTERVAL '90 days'"
            )
            if cur.rowcount > 0:
                logging.info(
                    f"Cleaned up {cur.rowcount} expired enforcement audit events."
                )
            cur.execute(
                """
                DELETE FROM enforcement_states state
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM user_wallets wallet
                    WHERE wallet.group_id = state.group_id
                      AND wallet.user_id = state.user_id
                )
                """
            )
    except Exception as e:
        logging.error(f"Error cleaning up expired verifications from DB: {e}")

# ==================== CITY Staking Constants ==========================
# Full coin type for the $CITY token on Sui mainnet.
CITY_TOKEN_TYPE = "0x308fa16c7aead43e3a49a4ff2e76205ba2a12697234f4fe80a2da66515284060::city::CITY"
# Staking contract package that issues UserStake receipts for locked $CITY.
CITY_STAKING_PACKAGE = "0x008856d5d6d60a088f6153dbe6f7697d19f81d1d0403695c9e9fbaecdc8b29a9"
CITY_STAKING_TYPE = f"{CITY_STAKING_PACKAGE}::city_staking::UserStake<{CITY_TOKEN_TYPE}>"

# ==================== fetch_wallet_balances Function =================
def fetch_wallet_balances(
    addresses,
    monitored_token,
    decimals,
    use_cache=True,
    cache_ttl=None,
    deadline_monotonic=None,
):
    """Fetch exact token balances through Sui GraphQL.

    Values are returned as ``Decimal`` human-readable units. ``None`` means the
    provider result was indeterminate; it never means a zero balance.
    """
    results = {}
    current_time = time.time()
    effective_cache_ttl = CACHE_TTL if cache_ttl is None else cache_ttl
    monitored_token_lower = monitored_token.lower()

    # 1. Identify which addresses can be served from cache
    missing_addresses = []
    with cache_lock:
        for addr in addresses:
            addr_lower = addr.lower()
            cache_key = f"{addr_lower}|{monitored_token_lower}"
            if use_cache and cache_key in balance_cache:
                cache_time, cached_val = balance_cache[cache_key]
                if current_time - cache_time < effective_cache_ttl:
                    results[addr_lower] = cached_val
                    continue
            missing_addresses.append(addr_lower)

    # 2. Fetch missing balances concurrently with a bounded worker pool.
    if missing_addresses:
        scale = Decimal(10) ** int(decimals)

        def fetch_one(wallet):
            try:
                total_atomic = sui_gateway.get_balance_atomic(
                    wallet,
                    monitored_token,
                    deadline_monotonic=deadline_monotonic,
                )
                if monitored_token == CITY_TOKEN_TYPE:
                    staked_atomic = _fetch_staked_city_balance_atomic(
                        wallet,
                        deadline_monotonic=deadline_monotonic,
                    )
                    if staked_atomic is None:
                        return wallet, None
                    total_atomic += staked_atomic
                return wallet, Decimal(total_atomic) / scale
            except (SuiGatewayError, ValueError, TypeError) as exc:
                logging.error("Failed to fetch on-chain balance for %s: %s", wallet, exc)
                return wallet, None

        for wallet, amount in _sui_executor.map(fetch_one, missing_addresses):
            results[wallet] = amount
            if amount is not None and use_cache:
                with cache_lock:
                    if len(balance_cache) >= MAX_CACHE_SIZE:
                        sorted_keys = sorted(
                            balance_cache.keys(),
                            key=lambda key: balance_cache[key][0],
                        )
                        for old_key in sorted_keys[:MAX_CACHE_SIZE // 4]:
                            del balance_cache[old_key]
                    cache_key = f"{wallet}|{monitored_token_lower}"
                    balance_cache[cache_key] = (current_time, amount)

    return results


def _fetch_staked_city_balance_atomic(
    address: str,
    *,
    deadline_monotonic=None,
) -> int | None:
    """Return atomic staked CITY units, or ``None`` for an unavailable result."""
    total_atomic = 0
    try:
        objects = sui_gateway.list_owned_objects(
            address,
            CITY_STAKING_TYPE,
            max_pages=SUI_MAX_PAGES,
            max_items=SUI_MAX_OBJECTS,
            deadline_monotonic=deadline_monotonic,
        )
        for item in objects:
            fields = (item.get("content") or {}).get("fields") or {}
            staked_amount = fields.get("staked_amount")
            if staked_amount is None:
                principal = fields.get("principal") or {}
                if isinstance(principal, dict):
                    principal_fields = principal.get("fields") or principal
                    staked_amount = principal_fields.get("value")
            if staked_amount is not None:
                total_atomic += int(staked_amount)
        return total_atomic
    except (SuiGatewayError, ValueError, TypeError) as exc:
        logging.error("Failed to fetch staked CITY balance for %s: %s", address, exc)
        return None


# ==================== Periodic Tasks ==============================
def send_low_holdings_alerts_to_admins(group_id, alert_entries):
    """Send a low-holdings alert to a group's human administrators.

    Returns ``True`` only when at least one administrator receives the alert.
    This allows the caller to start the cooldown only after a real delivery,
    rather than suppressing retries after a failed Telegram API request.
    """
    try:
        admins = bot.get_chat_administrators(group_id)
    except Exception as e:
        logging.error(f"Could not get administrators for low-holdings alert in group {group_id}: {e}")
        return False

    try:
        group_title = bot.get_chat(group_id).title or f"Group {group_id}"
    except Exception as e:
        # A title lookup should not prevent otherwise valid alerts from being
        # delivered.  The numeric group ID is still useful to an admin.
        logging.warning(f"Could not get title for low-holdings alert in group {group_id}: {e}")
        group_title = f"Group {group_id}"

    # Stored names may be first names rather than Telegram usernames, and
    # group titles are arbitrary user input.  HTML escaping keeps a special
    # character from invalidating every DM through parse-mode errors.
    user_list = "\n".join(
        f"<b>{html_module.escape(str(username))}</b>: {html_module.escape(str(failure_desc))}"
        for username, failure_desc in alert_entries
    )
    message = (
        f"🚨 <b>Low Holdings Alert for {html_module.escape(str(group_title))}:</b>\n\n"
        f"{user_list}"
    )

    delivered_count = 0
    for admin in admins:
        if getattr(admin.user, "is_bot", False):
            continue
        try:
            bot.send_message(admin.user.id, message, parse_mode="HTML")
            delivered_count += 1
        except Exception as admin_e:
            logging.error(f"Failed to send low-holdings alert to admin {admin.user.id}: {admin_e}")

    if delivered_count:
        logging.info(
            f"Delivered low-holdings alert for group {group_id} to "
            f"{delivered_count} administrator(s)."
        )
        return True

    logging.error(f"Low-holdings alert for group {group_id} was not delivered to any administrator.")
    return False


@db_retry
def refresh_wallet_scheduler_lease():
    """Acquire or renew the cross-process periodic-check lease."""
    with get_db_cursor() as (conn, cur):
        cur.execute(
            """
            INSERT INTO scheduler_leases (lease_name, holder_id, expires_at)
            VALUES ('wallet_checks', %s, NOW() + (%s * INTERVAL '1 second'))
            ON CONFLICT (lease_name) DO UPDATE SET
                holder_id = EXCLUDED.holder_id,
                expires_at = EXCLUDED.expires_at
            WHERE scheduler_leases.expires_at < NOW()
               OR scheduler_leases.holder_id = EXCLUDED.holder_id
            RETURNING holder_id
            """,
            (SCHEDULER_INSTANCE_ID, SCHEDULER_LEASE_SECONDS),
        )
        return cur.fetchone() is not None


@db_retry
def refresh_telegram_poller_lease():
    """Acquire or renew the single-active-instance Telegram polling lease."""
    with get_db_cursor() as (conn, cur):
        cur.execute(
            """
            INSERT INTO scheduler_leases (lease_name, holder_id, expires_at)
            VALUES ('telegram_poller', %s, NOW() + INTERVAL '2 minutes')
            ON CONFLICT (lease_name) DO UPDATE SET
                holder_id = EXCLUDED.holder_id,
                expires_at = EXCLUDED.expires_at
            WHERE scheduler_leases.expires_at < NOW()
               OR scheduler_leases.holder_id = EXCLUDED.holder_id
            RETURNING holder_id
            """,
            (SCHEDULER_INSTANCE_ID,),
        )
        return cur.fetchone() is not None


@db_retry
def get_enforcement_first_failed_at(group_id, user_id):
    with get_db_cursor() as (conn, cur):
        cur.execute(
            """
            SELECT first_failed_at
            FROM enforcement_states
            WHERE group_id = %s AND user_id = %s
            """,
            (group_id, user_id),
        )
        row = cur.fetchone()
        return row[0] if row else None


@db_retry
def record_enforcement_failure(group_id, user_id, reason):
    with get_db_cursor() as (conn, cur):
        cur.execute(
            """
            INSERT INTO enforcement_states
                (group_id, user_id, first_failed_at, last_checked_at, reason)
            VALUES (%s, %s, NOW(), NOW(), %s)
            ON CONFLICT (group_id, user_id) DO UPDATE SET
                last_checked_at = NOW(),
                reason = EXCLUDED.reason
            """,
            (group_id, user_id, reason),
        )


@db_retry
def clear_enforcement_state(group_id, user_id):
    with get_db_cursor() as (conn, cur):
        cur.execute(
            "DELETE FROM enforcement_states WHERE group_id = %s AND user_id = %s",
            (group_id, user_id),
        )


@db_retry
def record_enforcement_event(group_id, user_id, action, status, details=None):
    with get_db_cursor() as (conn, cur):
        cur.execute(
            """
            INSERT INTO enforcement_events
                (group_id, user_id, action, status, details)
            VALUES (%s, %s, %s, %s, %s::jsonb)
            """,
            (
                group_id,
                user_id,
                action,
                status,
                json.dumps(details or {}, default=str),
            ),
        )
    runtime_metrics.increment(f"enforcement_{status}")


def sleep_while_holding_wallet_scheduler_lease(seconds):
    """Sleep in short intervals while renewing the scheduler lease."""
    remaining = seconds
    while remaining > 0:
        time.sleep(min(SCHEDULER_LEASE_RENEW_SECONDS, remaining))
        remaining -= SCHEDULER_LEASE_RENEW_SECONDS
        if not refresh_wallet_scheduler_lease():
            logging.warning("Lost periodic wallet-check lease while waiting; another worker may take over.")
            return False
    return True


def renew_wallet_scheduler_lease(stop_event, lost_event):
    """Keep the periodic-scan lease alive during slow provider traversals."""
    while not stop_event.wait(SCHEDULER_LEASE_RENEW_SECONDS):
        try:
            if not refresh_wallet_scheduler_lease():
                logging.error("Lost periodic wallet-check lease during scan.")
                lost_event.set()
                return
        except Exception:
            logging.exception("Could not renew periodic wallet-check lease")
            lost_event.set()
            return


def check_user_wallets():
    """
    Efficiently checks all user wallets for a group in a single batch operation
    and implements a cooldown period for alerts to avoid spamming admins.
    """
    retry_group_items = None
    while True:
        group_items = []
        group_index = -1
        scan_lease_stop = None
        scan_lease_lost = None
        scan_started_monotonic = None
        try:
            if not refresh_wallet_scheduler_lease():
                logging.info("Another worker holds the periodic wallet-check lease; waiting before retrying.")
                time.sleep(SCHEDULER_LEASE_RENEW_SECONDS)
                continue
            scan_lease_stop = threading.Event()
            scan_lease_lost = threading.Event()
            threading.Thread(
                target=renew_wallet_scheduler_lease,
                args=(scan_lease_stop, scan_lease_lost),
                name="wallet-check-lease",
                daemon=True,
            ).start()

            with config_lock:
                configs = dict(SUBSCRIBER_CONFIGS)
            scan_started_monotonic = time.monotonic()
            with _wallet_scan_state_lock:
                _wallet_scan_state.update({
                    "status": "running",
                    "started_at": time.time(),
                    "last_error": None,
                    "configured_groups": len(configs),
                })
            runtime_metrics.increment("wallet_scans_started")

            logging.info(f"Starting periodic wallet check for {len(configs)} configured group(s).")

            # If a previous group raised unexpectedly, immediately continue
            # with the later groups instead of delaying every one until the
            # next scheduled (up to 48-hour) scan.
            group_items = retry_group_items if retry_group_items is not None else list(configs.items())
            retry_group_items = None
            for group_index, (group_id, config) in enumerate(group_items):
                if scan_lease_lost.is_set() or not refresh_wallet_scheduler_lease():
                    logging.warning("Lost periodic wallet-check lease during scan; stopping this scan safely.")
                    break
                # Expired subscriptions disable every enforcement action.  Keep
                # the saved configuration so an admin can renew without having
                # to rebuild it, but do not scan, alert, or remove members.
                if not group_has_active_subscription(group_id):
                    logging.info(f"Skipping periodic gate enforcement for expired-subscription group {group_id}.")
                    continue
                token = config.get("token")
                minimum_holding = Decimal(str(config.get("minimum_holding", 5000000)))
                decimals = config.get("decimals", 6)
                auto_remove = config.get("auto_remove", False)
                auto_remove_grace_seconds = max(
                    0,
                    int(
                        config.get(
                            "auto_remove_grace_seconds",
                            DEFAULT_AUTO_REMOVE_GRACE_SECONDS,
                        )
                    ),
                )
                nft_trait_name = config.get("nft_trait_name", "")
                nft_trait_value = config.get("nft_trait_value", "")
                nft_trait_threshold = config.get("nft_trait_threshold", 1)
                nft_collection_id = config.get("nft_collection_id", "")
                nft_threshold = config.get("nft_threshold", 1)
                registration_mode = config.get("registration_mode", "token")
                
                # Skip groups that don't have any gating configured
                if registration_mode == "token" and not token:
                    continue
                if registration_mode == "nft" and not nft_collection_id:
                    continue
                if registration_mode == "both" and not token and not nft_collection_id:
                    continue

                user_regs = get_user_registrations_for_group(group_id)
                if not user_regs:
                    continue

                # 1. Fetch recent alerts for cooldown period (using UTC timezone-naive datetime to match database timestamps)
                cooldown_threshold = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - datetime.timedelta(days=ALERT_COOLDOWN_DAYS)
                recent_alerts = {}
                try:
                    with get_db_cursor() as (conn, cur):
                        cur.execute(
                            """
                            SELECT user_id, alert_sent_at
                            FROM low_balance_alerts
                            WHERE group_id = %s
                              AND alert_sent_at > %s
                              AND COALESCE(delivery_version, 0) = %s
                            """,
                            (group_id, cooldown_threshold, ALERT_DELIVERY_VERSION)
                        )
                        for user_id, alert_time in cur.fetchall():
                            recent_alerts[user_id] = alert_time
                except Exception as e:
                    logging.error(f"Could not fetch recent alerts for group {group_id}: {e}")

                # 2. Collect all wallets for a single batch API call
                all_wallets_to_check = set()
                for reg in user_regs:
                    if not reg["wallets"] or reg["is_exempt"]:
                        continue
                    for wallet in reg["wallets"]:
                        all_wallets_to_check.add(wallet.lower())

                if not all_wallets_to_check:
                    continue

                # 3. Make batched API calls for the entire group
                logging.info(f"Starting batch balance check for {len(all_wallets_to_check)} unique wallets in group {group_id}.")
                
                # Fetch token balances if needed
                all_balances = {}
                if token and registration_mode in ["token", "both"]:
                    all_balances = fetch_wallet_balances(
                        list(all_wallets_to_check),
                        token,
                        decimals,
                        deadline_monotonic=(
                            time.monotonic() + SUI_OPERATION_TIMEOUT_SECONDS
                        ),
                    )

                # 4. Process users with the fetched data
                below_users_to_alert = []
                valid_user_ids = []
                for reg in user_regs:
                    if scan_lease_lost.is_set():
                        logging.warning(
                            "Stopping periodic scan before another user because "
                            "the scheduler lease was lost."
                        )
                        break
                    user_id = reg["user_id"]

                    if reg["is_exempt"] or not reg["wallets"]:
                        continue

                    user_wallets_lower = [w.lower() for w in reg["wallets"]]
                    
                    # Check token holdings
                    token_valid = False
                    token_indeterminate = False
                    total_balance = Decimal(0)
                    if registration_mode in ["token", "both"] and token:
                        wallet_values = [all_balances.get(w) for w in user_wallets_lower]
                        if any(v is None for v in wallet_values):
                            logging.warning(f"Skipping user {user_id} in group {group_id} due to incomplete token balance data from API.")
                            token_indeterminate = True
                        else:
                            total_balance = sum(v for v in wallet_values if v is not None)
                            token_valid = total_balance >= minimum_holding
                    
                    # Check NFT holdings (collection + optional traits)
                    nft_valid = False
                    trait_valid = True
                    nft_indeterminate = False
                    trait_indeterminate = False
                    user_nft_count = None
                    user_trait_count = None
                    operation_deadline = (
                        time.monotonic() + SUI_OPERATION_TIMEOUT_SECONDS
                    )
                    if registration_mode in ["nft", "both"] and nft_collection_id:
                        fetched_nfts = None
                        try:
                            if nft_trait_name:
                                # Fetch once with content so traits can be extracted from the
                                # same result set, avoiding a second provider round-trip.
                                fetched_nfts = _fetch_owned_nfts(
                                    user_wallets_lower,
                                    nft_collection_id,
                                    deadline_monotonic=operation_deadline,
                                )
                                user_nft_count = len(fetched_nfts)
                            else:
                                # Periodic enforcement must not reuse a value
                                # that was cached by a prior display request.
                                # A 48-hour scan is infrequent enough to make a
                                # fresh on-chain count the safer trade-off.
                                user_nft_count = get_user_nft_count(
                                    user_wallets_lower,
                                    nft_collection_id,
                                    use_cache=False,
                                    deadline_monotonic=operation_deadline,
                                )

                            if user_nft_count is None:
                                logging.warning(f"Skipping NFT check for user {user_id} in group {group_id} due to API failure.")
                                nft_indeterminate = True
                            else:
                                nft_valid = user_nft_count >= nft_threshold
                                logging.info(
                                    f"NFT check for user {user_id} in group {group_id}: "
                                    f"{user_nft_count} / {nft_threshold} NFTs"
                                )
                        except Exception as nft_e:
                            logging.warning(f"NFT check API error for user {user_id}, deferring enforcement: {nft_e}")
                            nft_indeterminate = True
                        
                        # Check trait requirements if configured and NFT collection check passed
                        if nft_valid and nft_trait_name:
                            try:
                                if fetched_nfts is not None:
                                    # Re-use already-fetched NFT objects; no second provider call.
                                    target_key = nft_trait_name.strip().lower()
                                    if nft_trait_value:
                                        target_val = nft_trait_value.strip().lower()
                                        user_trait_count = sum(
                                            1 for obj in fetched_nfts
                                            if _extract_traits(obj).get(target_key) == target_val
                                        )
                                    else:
                                        user_trait_count = sum(
                                            1 for obj in fetched_nfts
                                            if target_key in _extract_traits(obj)
                                        )
                                else:
                                    # Fetch failed earlier; fall back to individual helpers.
                                    if nft_trait_value:
                                        user_trait_count = get_user_nft_trait_count(
                                            user_wallets_lower,
                                            nft_collection_id,
                                            nft_trait_name,
                                            nft_trait_value,
                                            deadline_monotonic=operation_deadline,
                                        )
                                    else:
                                        user_trait_count = get_user_nft_category_count(
                                            user_wallets_lower,
                                            nft_collection_id,
                                            nft_trait_name,
                                            deadline_monotonic=operation_deadline,
                                        )
                                if user_trait_count is None:
                                    trait_valid = False
                                    trait_indeterminate = True
                                elif user_trait_count < nft_trait_threshold:
                                    trait_valid = False
                                    logging.info(f"User {user_id} fails trait check: {user_trait_count} < {nft_trait_threshold}")
                            except Exception as trait_e:
                                logging.warning(f"Trait check API error for user {user_id}, deferring enforcement: {trait_e}")
                                trait_valid = False
                                trait_indeterminate = True

                        # Persist successful on-chain results so display
                        # functions can fall back to them on provider failure.
                        if user_nft_count is not None or user_trait_count is not None:
                            try:
                                update_user_cached_holdings(
                                    group_id, user_id,
                                    nft_count=user_nft_count,
                                    trait_count=user_trait_count,
                                )
                            except Exception as e:
                                logging.debug(f"Could not update cached holdings for user {user_id}: {e}")
                    
                    gate_status = evaluate_gate(
                        registration_mode,
                        token_valid=token_valid,
                        nft_valid=nft_valid,
                        trait_valid=trait_valid,
                        token_indeterminate=token_indeterminate,
                        nft_indeterminate=nft_indeterminate,
                        trait_indeterminate=trait_indeterminate,
                    )
                    user_meets_requirements = gate_status == GateStatus.PASS

                    if gate_status == GateStatus.INDETERMINATE:
                        logging.warning(
                            "Deferring periodic enforcement for user %s in group %s "
                            "because on-chain holdings were indeterminate",
                            user_id,
                            group_id,
                        )
                        record_enforcement_event(
                            group_id,
                            user_id,
                            "holdings_check",
                            "deferred",
                            {"registration_mode": registration_mode},
                        )
                        continue
                    
                    if not user_meets_requirements:
                        # LAZY MEMBERSHIP CHECK: verify if user is still in the group
                        is_active_member = True
                        try:
                            member = bot.get_chat_member(group_id, user_id)
                            if member.status in ["left", "kicked"]:
                                logging.info(f"User {user_id} has left or was kicked from group {group_id}. Cleaning up database registration lazily.")
                                with get_db_cursor() as (conn, cur):
                                    cur.execute("DELETE FROM user_wallets WHERE group_id = %s AND user_id = %s", (group_id, user_id))
                                    cur.execute("DELETE FROM low_balance_alerts WHERE group_id = %s AND user_id = %s", (group_id, user_id))
                                    cur.execute("DELETE FROM pending_verifications WHERE group_id = %s AND user_id = %s", (group_id, user_id))
                                    cur.execute("DELETE FROM enforcement_states WHERE group_id = %s AND user_id = %s", (group_id, user_id))
                                is_active_member = False
                        except Exception as e:
                            # Never remove a user when Telegram could not
                            # authoritatively confirm their current status.
                            logging.warning(
                                f"Could not verify membership status for user {user_id} "
                                f"in group {group_id}; deferring enforcement: {e}"
                            )
                            record_enforcement_event(
                                group_id,
                                user_id,
                                "membership_check",
                                "deferred",
                                {"error": type(e).__name__},
                            )
                            is_active_member = False

                        if not is_active_member:
                            continue

                        # Auto-remove applies only to a definitive token
                        # violation. It starts with a grace period and always
                        # performs one final uncached balance check.
                        if (
                            auto_remove
                            and registration_mode in ["token", "both"]
                            and token
                            and not token_valid
                        ):
                            first_failed_at = get_enforcement_first_failed_at(
                                group_id,
                                user_id,
                            )
                            now_utc = datetime.datetime.now(
                                datetime.timezone.utc
                            ).replace(tzinfo=None)
                            removal_decision = decide_auto_removal(
                                first_failed_at=first_failed_at,
                                now=now_utc,
                                grace_seconds=auto_remove_grace_seconds,
                            )
                            reason = (
                                f"{total_balance:,.2f} / "
                                f"{minimum_holding:,.2f} tokens"
                            )
                            record_enforcement_failure(
                                group_id,
                                user_id,
                                reason,
                            )

                            if removal_decision.action == EnforcementDecision.WARN:
                                try:
                                    bot.send_message(
                                        user_id,
                                        "⚠️ Your registered token balance is below "
                                        "this group's requirement.\n\n"
                                        f"Current: {total_balance:,.2f}\n"
                                        f"Required: {minimum_holding:,.2f}\n\n"
                                        "Your access has not changed. Please restore "
                                        "your balance before the grace period ends.",
                                    )
                                except Exception as dm_e:
                                    logging.debug(
                                        f"Could not send grace warning to user "
                                        f"{user_id}: {dm_e}"
                                    )
                                record_enforcement_event(
                                    group_id,
                                    user_id,
                                    "auto_remove",
                                    "grace_started",
                                    {
                                        "remaining_seconds":
                                            removal_decision.remaining_seconds,
                                        "balance": str(total_balance),
                                        "threshold": str(minimum_holding),
                                    },
                                )

                            if removal_decision.action in (
                                EnforcementDecision.WARN,
                                EnforcementDecision.WAIT,
                            ):
                                if user_id not in recent_alerts:
                                    hours_left = math.ceil(
                                        removal_decision.remaining_seconds / 3600
                                    )
                                    below_users_to_alert.append(
                                        (
                                            user_id,
                                            f"{reason} | auto-remove grace: "
                                            f"{hours_left}h remaining",
                                        )
                                    )
                                continue

                            fresh_balances = fetch_wallet_balances(
                                user_wallets_lower,
                                token,
                                decimals,
                                use_cache=False,
                                deadline_monotonic=(
                                    time.monotonic()
                                    + SUI_OPERATION_TIMEOUT_SECONDS
                                ),
                            )
                            fresh_values = [
                                fresh_balances.get(wallet)
                                for wallet in user_wallets_lower
                            ]
                            if any(value is None for value in fresh_values):
                                record_enforcement_event(
                                    group_id,
                                    user_id,
                                    "auto_remove",
                                    "deferred",
                                    {"reason": "final_recheck_unavailable"},
                                )
                                continue

                            fresh_total = sum(
                                fresh_values,
                                Decimal(0),
                            )
                            if fresh_total >= minimum_holding:
                                clear_enforcement_state(group_id, user_id)
                                valid_user_ids.append(user_id)
                                record_enforcement_event(
                                    group_id,
                                    user_id,
                                    "auto_remove",
                                    "recovered",
                                    {"balance": str(fresh_total)},
                                )
                                continue

                            try:
                                bot.ban_chat_member(group_id, user_id)
                                bot.unban_chat_member(
                                    group_id,
                                    user_id,
                                    only_if_banned=True,
                                )
                                try:
                                    bot.send_message(
                                        user_id,
                                        "⚠️ You were removed from the group after "
                                        "the balance grace period expired.\n\n"
                                        f"Current: {fresh_total:,.2f}\n"
                                        f"Required: {minimum_holding:,.2f}\n\n"
                                        "You are not banned. Once your balance meets "
                                        "the requirement, you can re-register and rejoin.",
                                    )
                                except Exception as dm_e:
                                    logging.debug(
                                        f"Could not DM user {user_id} after "
                                        f"removal: {dm_e}"
                                    )
                                with get_db_cursor() as (conn, cur):
                                    cur.execute(
                                        "DELETE FROM user_wallets "
                                        "WHERE group_id = %s AND user_id = %s",
                                        (group_id, user_id),
                                    )
                                    cur.execute(
                                        "DELETE FROM low_balance_alerts "
                                        "WHERE group_id = %s AND user_id = %s",
                                        (group_id, user_id),
                                    )
                                    cur.execute(
                                        "DELETE FROM enforcement_states "
                                        "WHERE group_id = %s AND user_id = %s",
                                        (group_id, user_id),
                                    )
                                record_enforcement_event(
                                    group_id,
                                    user_id,
                                    "auto_remove",
                                    "removed",
                                    {"balance": str(fresh_total)},
                                )
                                logging.info(
                                    f"Removed and unbanned user {user_id} from "
                                    f"group {group_id} for holdings of "
                                    f"{fresh_total:,.2f} tokens."
                                )
                            except Exception as e:
                                record_enforcement_event(
                                    group_id,
                                    user_id,
                                    "auto_remove",
                                    "failed",
                                    {"error": type(e).__name__},
                                )
                                logging.error(
                                    f"Error removing user {user_id} from "
                                    f"group {group_id}: {e}"
                                )
                            continue

                        # Check if user is in alert cooldown period
                        if user_id in recent_alerts:
                            logging.info(
                                f"User {user_id} in group {group_id} is in the "
                                "delivered-alert cooldown period. Skipping alert."
                            )
                            continue

                        # Build a mode-appropriate description of what the user is missing
                        if registration_mode == "token":
                            failure_desc = f"{total_balance:,.2f} / {minimum_holding:,.2f} tokens"
                        elif registration_mode == "nft":
                            if user_nft_count is None:
                                failure_desc = "NFT check unavailable"
                            elif user_nft_count < nft_threshold:
                                failure_desc = f"{user_nft_count} / {nft_threshold} NFTs"
                            elif nft_trait_name and not trait_valid:
                                if nft_trait_value:
                                    failure_desc = f"{user_trait_count or 0} / {nft_trait_threshold} NFTs with trait '{nft_trait_name}={nft_trait_value}'"
                                else:
                                    failure_desc = f"{user_trait_count or 0} / {nft_trait_threshold} NFTs with trait '{nft_trait_name}'"
                            else:
                                failure_desc = f"{user_nft_count} / {nft_threshold} NFTs"
                        else:  # "both"
                            token_part = f"{total_balance:,.2f} / {minimum_holding:,.2f} tokens"
                            if user_nft_count is None:
                                nft_part = "NFT check unavailable"
                            elif user_nft_count < nft_threshold:
                                nft_part = f"{user_nft_count} / {nft_threshold} NFTs"
                            elif nft_trait_name and not trait_valid:
                                if nft_trait_value:
                                    nft_part = f"{user_trait_count or 0} / {nft_trait_threshold} '{nft_trait_name}={nft_trait_value}' NFTs"
                                else:
                                    nft_part = f"{user_trait_count or 0} / {nft_trait_threshold} '{nft_trait_name}' NFTs"
                            else:
                                nft_part = f"{user_nft_count} / {nft_threshold} NFTs"
                            failure_desc = f"{token_part} | {nft_part}"
                        below_users_to_alert.append((user_id, failure_desc))
                    else:
                        valid_user_ids.append(user_id)

                # 4.5 Clear low balance alerts for users who now meet requirements
                if valid_user_ids:
                    try:
                        with get_db_cursor() as (conn, cur):
                            placeholders = ",".join(["%s"] * len(valid_user_ids))
                            cur.execute(
                                f"DELETE FROM low_balance_alerts WHERE group_id = %s AND user_id IN ({placeholders})",
                                [group_id] + valid_user_ids
                            )
                            cur.execute(
                                f"DELETE FROM enforcement_states WHERE group_id = %s AND user_id IN ({placeholders})",
                                [group_id] + valid_user_ids,
                            )
                            logging.info(f"Cleared {len(valid_user_ids)} stale low balance alerts for group {group_id}")
                    except Exception as e:
                        logging.error(f"Error clearing low balance alerts for group {group_id}: {e}")

                # 5. Send one consolidated alert for all users not on cooldown (always alert when below_users_to_alert is not empty)
                if below_users_to_alert:
                    alert_entries = []
                    for user_id, failure_desc in below_users_to_alert:
                        # Use the username already stored in the DB to avoid
                        # expensive per-user Telegram API calls.
                        reg_match = next((r for r in user_regs if r["user_id"] == user_id), None)
                        if reg_match and reg_match.get("username"):
                            username = reg_match["username"]
                        else:
                            username = f"User{user_id}"
                        alert_entries.append((username, failure_desc))

                    # Do not begin the alert cooldown until Telegram accepted
                    # a DM for at least one administrator.  Previously this
                    # happened before delivery, causing failed alerts to be
                    # suppressed for the cooldown window.
                    if send_low_holdings_alerts_to_admins(group_id, alert_entries):
                        try:
                            with get_db_cursor() as (conn, cur):
                                # Single batch INSERT with all users instead of N individual queries
                                values_list = [
                                    (group_id, user_id, ALERT_DELIVERY_VERSION)
                                    for user_id, _ in below_users_to_alert
                                ]
                                placeholders = ",".join([f"(%s, %s, %s)"] * len(values_list))
                                flat_values = [item for pair in values_list for item in pair]
                                cur.execute(f"""
                                    INSERT INTO low_balance_alerts (group_id, user_id, alert_sent_at, delivery_version)
                                    VALUES {placeholders}
                                    ON CONFLICT (group_id, user_id) DO UPDATE SET
                                        alert_sent_at = NOW(),
                                        delivery_version = EXCLUDED.delivery_version
                                """, flat_values)
                                logging.info(f"Batch inserted {len(below_users_to_alert)} low balance alerts for group {group_id}")
                        except Exception as e:
                            # A later duplicate is preferable to claiming an
                            # alert was delivered when its cooldown was never
                            # persisted.  Keep the failure visible in logs.
                            logging.error(f"Error tracking delivered alerts for group {group_id}: {e}")
                    else:
                        logging.warning(
                            f"Will retry low-holdings alert for group {group_id} on the next periodic check; "
                            "no administrator DM was delivered."
                        )

                # Brief pause between groups to give the provider breathing room.
                time.sleep(GROUP_CHECK_DELAY)

            if scan_lease_lost.is_set():
                raise RuntimeError("periodic wallet-check lease was lost")
            logging.info("Completed periodic wallet check for all configured groups.")
            with _wallet_scan_state_lock:
                _wallet_scan_state.update({
                    "status": "completed",
                    "completed_at": time.time(),
                    "last_error": None,
                })
            runtime_metrics.increment("wallet_scans_completed")
            runtime_metrics.observe(
                "wallet_scan_seconds",
                time.monotonic() - scan_started_monotonic,
            )

        except Exception as e:
            group_label = group_items[group_index][0] if 0 <= group_index < len(group_items) else "setup"
            logging.exception(f"Error in periodic wallet check for group {group_label}: {e}")
            if group_index >= 0:
                retry_group_items = group_items[group_index + 1:] or None
            with _wallet_scan_state_lock:
                _wallet_scan_state.update({
                    "status": "failed",
                    "last_error": type(e).__name__,
                })
            runtime_metrics.increment("wallet_scans_failed")
            if scan_started_monotonic is not None:
                runtime_metrics.observe(
                    "wallet_scan_seconds",
                    time.monotonic() - scan_started_monotonic,
                )

        if scan_lease_stop is not None:
            scan_lease_stop.set()

        if retry_group_items is not None:
            logging.info(
                f"Continuing periodic wallet scan with {len(retry_group_items)} unaffected group(s) "
                "after a group-specific failure."
            )
            time.sleep(GROUP_CHECK_DELAY)
            continue
        # Add jitter to prevent all groups checking at the same time
        jitter = SLEEP_BETWEEN_TASKS * TASK_JITTER_PERCENT * (2 * random.random() - 1)
        sleep_time = SLEEP_BETWEEN_TASKS + jitter
        logging.info(f"Next user wallets check in {sleep_time:.0f}s (base: {SLEEP_BETWEEN_TASKS}s, jitter: {jitter:+.0f}s)")
        sleep_while_holding_wallet_scheduler_lease(sleep_time)

# ==================== Bot Handlers ================================
def is_group_admin(message):
    """Check if the user is an admin in the group."""
    try:
        if message.chat.type in ["group","supergroup"]:
            user_id = message.from_user.id
            member = bot.get_chat_member(message.chat.id, user_id)
            return member.status in ADMIN_MEMBER_STATUSES
        return False
    except Exception as e:
        logging.error(f"Error checking admin status: {e}")
        return False

def admin_required(func):
    """Decorator to check for group admin privileges."""
    @functools.wraps(func)
    def wrapper(message):
        if message.chat.type not in ["group", "supergroup"]:
            bot.reply_to(message, "❌ This command is only available in groups.")
            return
        if not is_group_admin(message):
            bot.reply_to(message, "❌ Only group admins can use this command.")
            return
        return func(message)
    return wrapper

@bot.message_handler(
    content_types=['text'],
    func=lambda message: message.reply_to_message and \
                         message.reply_to_message.from_user.is_bot and \
                         "reply directly to this message with the details" in message.reply_to_message.text.lower()
)
def handle_poll_reply(message):
    """
    Handles a reply to the bot's poll creation prompt to prevent chat interference.
    """
    # Check if the user who is replying is an admin
    try:
        member = bot.get_chat_member(message.chat.id, message.from_user.id)
        if member.status not in ["creator", "administrator"]:
            # Silently ignore if a non-admin replies
            return
    except Exception:
        # Silently ignore if status check fails
        return

    # Call the existing function to process the poll details
    process_create_poll(message, message.chat.id)

@bot.message_handler(commands=['reminder'])
@admin_required
def reminder_command(message):
    """Send a registration reminder with deep link to the group"""
    try:
        group_id = message.chat.id
        bot_username = get_bot_username()
        reg_link = f"https://t.me/{bot_username}?start=register_{encode_group_id_for_deeplink(group_id)}"

        # Create inline keyboard with registration button
        markup = types.InlineKeyboardMarkup()
        register_btn = types.InlineKeyboardButton("✅ Verify Wallet", url=reg_link)
        markup.add(register_btn)

        reminder_text = (
            "📢 Friendly Reminder! 📢\n"
            "If you haven't already registered your wallet, run /register in this group and complete wallet verification."
        )

        bot.send_message(
            message.chat.id,
            reminder_text,
            reply_markup=markup
        )

        logging.info(f"Sent registration reminder in group {group_id}")

    except Exception as e:
        logging.error(f"Error sending reminder: {e}")
        bot.reply_to(message, "❌ Error sending reminder. Please try again.")

@bot.message_handler(commands=['help'])
def help_command(message):
    help_text = (
        "🤖 *Welcome to CityWatch!* 🤖\n\n"
        "This bot helps manage group access based on token or NFT holdings.\n\n"
        "--- *For All Users* ---\n\n"
        "🔑 *How to Register:*\n"
        "1. In your target group, run `/register` (optionally with a wallet address).\n"
        "2. Follow the wallet verification button/WebApp flow.\n"
        "3. Complete wallet sign-in and on-chain holdings verification (no transfer required).\n\n"
        "💼 *Manage Your Wallets:*\n"
        "`/mywallets` - View your registered wallets, check balances, and add or remove them securely in our private chat.\n\n"
        "--- *For Group Admins* ---\n\n"
        "⚙️ *Configuration:*\n"
        "`/cwconfig` - Opens the main group configuration menu in a private chat.\n"
        "`/cwstatus` - Shows wallet-scan, verification, enforcement, and Sui provider status.\n"
        "`/votesetup` - Configures the rules for weighted voting.\n\n"
        "🗳️ *Voting:*\n"
        "`/vote` - Creates a new weighted poll in the group.\n\n"
        "👤 *User Management:*\n"
        "`/reminder` - Posts a public registration reminder in the chat.\n"
        "`/exempt` - Reply to a user's **recent message** to toggle their exemption from wallet rules.\n"
        "`/addwallet` - Add a wallet for a user by replying to their message or by user ID. (e.g., `/addwallet 123456789 0x...`)\n\n"
        "❓ For more assistance, please contact your group admin."
    )
    try:
        bot.send_message(message.chat.id, help_text, parse_mode="Markdown")
        logging.info(f"Help message sent successfully to chat {message.chat.id}")
    except Exception as e:
        logging.error(f"Error sending help message: {e}")
        # Fallback to plain text if markdown fails for any reason
        plain_text = help_text.replace("*", "").replace("`", "")
        try:
            bot.send_message(message.chat.id, plain_text)
        except Exception as e2:
            logging.error(f"Failed to send plain help message: {e2}")

@bot.message_handler(commands=['cwconfig'])
@admin_required
def config_command(message):
    # Create inline keyboard with private chat button
    markup = types.InlineKeyboardMarkup()
    deep_link = f"https://t.me/{get_bot_username()}?start=config_{encode_group_id_for_deeplink(message.chat.id)}"
    config_btn = types.InlineKeyboardButton("⚙️ Configure in Private Chat", url=deep_link)
    markup.add(config_btn)

    # Get the message thread ID if this is a topic
    message_thread_id = getattr(message, 'message_thread_id', None)

    # Send with topic context preserved
    if message_thread_id:
        bot.send_message(
            message.chat.id, 
            "🔧 **Group Configuration**\n\nClick the button below to configure this group's settings in a private chat:",
            reply_markup=markup,
            parse_mode="Markdown",
            message_thread_id=message_thread_id
        )
    else:
        bot.send_message(
            message.chat.id, 
            "🔧 **Group Configuration**\n\nClick the button below to configure this group's settings in a private chat:",
            reply_markup=markup,
            parse_mode="Markdown"
        )
    logging.info(f"Sent config redirect to private chat for group {message.chat.id}")


@bot.message_handler(commands=['cwstatus'])
@admin_required
def status_command(message):
    """Show safe operational and enforcement status to group admins."""
    group_id = message.chat.id
    try:
        with get_db_cursor() as (conn, cur):
            cur.execute(
                "SELECT COUNT(*) FROM verification_sessions "
                "WHERE group_id = %s AND status IN ('pending', 'processing') "
                "AND expires_at > NOW()",
                (group_id,),
            )
            active_sessions = cur.fetchone()[0]
            cur.execute(
                "SELECT COUNT(*) FROM enforcement_states WHERE group_id = %s",
                (group_id,),
            )
            grace_members = cur.fetchone()[0]
            cur.execute(
                """
                SELECT action, status, created_at
                FROM enforcement_events
                WHERE group_id = %s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (group_id,),
            )
            last_event = cur.fetchone()

        with _wallet_scan_state_lock:
            scan_state = dict(_wallet_scan_state)
        provider_lines = []
        for provider in sui_gateway.provider_status():
            circuit = (
                f"open ({provider['retry_after_seconds']:.0f}s)"
                if provider["circuit_open"]
                else "closed"
            )
            provider_lines.append(
                f"- {provider['provider']}: {circuit}, "
                f"failures={provider['failures']}"
            )
        if last_event:
            last_event_text = (
                f"{last_event[0]} / {last_event[1]} at "
                f"{last_event[2].isoformat(timespec='seconds')}"
            )
        else:
            last_event_text = "none"
        started_at = scan_state.get("started_at")
        scan_started_text = (
            datetime.datetime.fromtimestamp(
                started_at,
                datetime.timezone.utc,
            ).isoformat(timespec="seconds")
            if started_at
            else "never"
        )
        response = (
            "CityWatch operational status\n\n"
            f"Revision: {CODE_SYNC_REV}\n"
            f"Subscription active: "
            f"{'yes' if group_has_active_subscription(group_id) else 'no'}\n"
            f"Wallet scan: {scan_state.get('status')} "
            f"(started {scan_started_text})\n"
            f"Active verification sessions: {active_sessions}\n"
            f"Members in auto-remove grace: {grace_members}\n"
            f"Last enforcement event: {last_event_text}\n\n"
            "Sui GraphQL providers\n"
            + "\n".join(provider_lines)
        )
        bot.reply_to(message, response)
    except Exception as exc:
        logging.exception("Could not build /cwstatus response")
        bot.reply_to(
            message,
            f"❌ Status is temporarily unavailable ({type(exc).__name__}).",
        )


@bot.message_handler(commands=['votesetup'])
@admin_required
def votesetup_command(message):
    # Create inline keyboard with private chat button
    markup = types.InlineKeyboardMarkup()
    deep_link = f"https://t.me/{get_bot_username()}?start=votesetup_{encode_group_id_for_deeplink(message.chat.id)}"
    votesetup_btn = types.InlineKeyboardButton("🗳️ Configure Voting in Private Chat", url=deep_link)
    markup.add(votesetup_btn)

    # Get the message thread ID if this is a topic
    message_thread_id = getattr(message, 'message_thread_id', None)

    # Send with topic context preserved
    if message_thread_id:
        bot.send_message(
            message.chat.id, 
            "🗳️ **Voting Configuration**\n\nClick the button below to configure this group's voting settings in a private chat:",
            reply_markup=markup,
            parse_mode="Markdown",
            message_thread_id=message_thread_id
        )
    else:
        bot.send_message(
            message.chat.id, 
            "🗳️ **Voting Configuration**\n\nClick the button below to configure this group's voting settings in a private chat:",
            reply_markup=markup,
            parse_mode="Markdown"
        )
    logging.info(f"Sent voting setup redirect to private chat for group {message.chat.id}")

# Global dictionary to store poll creation context
poll_creation_context = {}

# Tracks which group an admin is currently configuring in private chat,
# decoupling config sessions from the pending_verifications table.
admin_config_context = {}

@bot.message_handler(commands=['vote'])
@admin_required
def vote_command(message):
    help_text = (
        "🗳️ *Create a Poll:*\n\n"
        "To create a poll, use the following format:\n\n"
        "`Title: Your poll question`\n"
        "`Option 1: Choice one`\n"
        "`Option 2: Choice two`\n"
        "... up to 10 options\n"
    )

    # MODIFIED: Combine the help text and the prompt into a single message
    final_text = (
        f"{help_text}\n"
        "------\n"
        "*To create the poll, reply directly to this message with the details in the format above.*"
    )

    # Store the original message thread ID for this poll creation
    message_thread_id = getattr(message, 'message_thread_id', None)
    user_id = message.from_user.id
    poll_creation_context[user_id] = {
        'chat_id': message.chat.id,
        'message_thread_id': message_thread_id,
        'timestamp': time.time()
    }

    # MODIFIED: Send the single, combined message
    if message_thread_id:
        bot.send_message(
            message.chat.id,
            final_text,
            reply_markup=types.ForceReply(selective=True),
            parse_mode="Markdown",
            message_thread_id=message_thread_id
        )
    else:
        bot.send_message(
            message.chat.id,
            final_text,
            reply_markup=types.ForceReply(selective=True),
            parse_mode="Markdown"
        )

@bot.callback_query_handler(func=lambda call: call.data.startswith("subscribe_"))
def handle_subscription_callback(call):
    """Handle subscription tier selection callbacks."""
    try:
        parts = call.data.split("_")
        if len(parts) < 3:
            bot.answer_callback_query(call.id, "❌ Invalid subscription action.")
            return
        group_id = int(parts[1])
        tier = parts[2]

        # Handle "back" action — re-show tier selection
        if tier == "back":
            bot.answer_callback_query(call.id)
            try:
                chat_obj = bot.get_chat(group_id)
                group_name = chat_obj.title
            except Exception:
                group_name = f"Group {group_id}"
            try:
                bot.delete_message(call.message.chat.id, call.message.message_id)
            except Exception:
                pass
            show_subscription_prompt(call.message.chat.id, group_id, group_name)
            return

        if tier not in SUBSCRIPTION_TIERS:
            bot.answer_callback_query(call.id, "❌ Unknown subscription tier.")
            return

        user_id = call.from_user.id

        # Verify user is admin of the group
        try:
            member = bot.get_chat_member(group_id, user_id)
            if member.status not in ["creator", "administrator"]:
                bot.answer_callback_query(call.id, "❌ Only group administrators can purchase subscriptions.")
                return
        except Exception:
            bot.answer_callback_query(call.id, "❌ Could not verify admin status.")
            return

        bot.answer_callback_query(call.id, "⏳ Creating payment link…")

        if not STRIPE_SECRET_KEY:
            bot.edit_message_text(
                "❌ Payment system is not configured. Please contact the bot administrator.",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id
            )
            return

        try:
            session = create_stripe_checkout_session(group_id, user_id, tier)
            tier_info = SUBSCRIPTION_TIERS[tier]
            try:
                chat_obj = bot.get_chat(group_id)
                group_name = chat_obj.title
            except Exception:
                group_name = f"Group {group_id}"
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("💳 Pay Now", url=session.url))
            markup.add(types.InlineKeyboardButton("« Back", callback_data=f"subscribe_{group_id}_back"))
            bot.edit_message_text(
                f"💳 **Complete Your Payment**\n\n"
                f"Plan: *{tier_info['label']}* — {tier_info['display']}\n"
                f"Group: *{group_name}*\n\n"
                f"Click the button below to complete your payment via Stripe.\n"
                f"Once payment is confirmed, you'll be able to configure the bot.",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=markup,
                parse_mode="Markdown"
            )
        except Exception as e:
            logging.error(f"Stripe checkout creation failed for group {group_id}: {e}")
            bot.edit_message_text(
                "❌ Error creating payment session. Please try again later.",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id
            )
    except Exception as e:
        logging.error(f"Error in subscription callback: {e}")
        try:
            bot.answer_callback_query(call.id, "❌ An error occurred.")
        except Exception:
            pass

@bot.callback_query_handler(func=lambda call: call.data.startswith("privconfig_") or call.data.startswith("config_") or call.data.startswith("privvote_"))
def handle_private_config_callback(call):
    """Handles ALL callbacks from the private configuration menu."""
    try:
        # We need more robust parsing to handle both 'privconfig_ID_action' and 'config_action'
        if call.data.startswith("privconfig_") or call.data.startswith("privvote_"):
            parts = call.data.split("_")
            if len(parts) < 3:
                bot.answer_callback_query(call.id, "❌ Invalid config action.")
                return
            group_id = int(parts[1])
            action = parts[2]

        elif call.data.startswith("config_"): # Handling for the old prefix
             parts = call.data.split("_")
             # Retrieve group context from admin_config_context (preferred) or
             # fall back to pending_verifications for backward compatibility.
             group_id = admin_config_context.get(call.from_user.id)
             if not group_id:
                 with get_db_cursor() as (conn, cur):
                    cur.execute("SELECT group_id FROM pending_verifications WHERE user_id = %s", (call.from_user.id,))
                    result = cur.fetchone()
                    if not result:
                        bot.answer_callback_query(call.id, "❌ Group context lost. Please restart.")
                        return
                    group_id = result[0]
             action = parts[1] if len(parts) > 1 else ""

        else:
            # mywallet_ callbacks are handled by handle_mywallets_callback;
            # this branch should never execute.
            bot.answer_callback_query(call.id, "❌ Unrecognized action.")
            return

        user_id = call.from_user.id

        # Check for admin status again for security (for config actions)
        if call.data.startswith("privconfig_") or call.data.startswith("config_") or call.data.startswith("privvote_"):
            try:
                member = bot.get_chat_member(group_id, user_id)
                if member.status not in ["creator", "administrator"]:
                    bot.answer_callback_query(call.id, "❌ Unauthorized action.")
                    bot.edit_message_text("❌ Permission denied.", chat_id=call.message.chat.id, message_id=call.message.message_id)
                    return
            except Exception:
                bot.answer_callback_query(call.id, "❌ Could not verify admin status.")
                return

        bot.answer_callback_query(call.id, "⏳ Processing…")

        # --- Subscription gate for config actions ---
        if call.data.startswith("privconfig_") or call.data.startswith("config_") or call.data.startswith("privvote_"):
            if not group_has_active_subscription(group_id):
                try:
                    chat_obj = bot.get_chat(group_id)
                    gname = chat_obj.title
                except Exception:
                    gname = f"Group {group_id}"
                try:
                    bot.delete_message(call.message.chat.id, call.message.message_id)
                except Exception:
                    pass
                show_subscription_prompt(call.message.chat.id, group_id, gname)
                return

        # --- THE REST OF THE FUNCTION LOGIC REMAINS THE SAME ---
        # (This combines the logic from the deleted function with the new one)

        if action == "settokenconfig":
            msg = bot.send_message(call.message.chat.id, "Please provide the token address, minimum holding, and decimals, separated by spaces:", reply_markup=types.ForceReply(selective=True))
            bot.register_next_step_handler(msg, process_set_token_config, group_id)
        elif action == "toggleautoremove":
            with config_lock:
                config = ensure_config_exists(group_id)
                current_setting = config.get("auto_remove", False)
                new_setting = not current_setting
                SUBSCRIBER_CONFIGS[group_id]["auto_remove"] = new_setting
                update_config_in_db(group_id, SUBSCRIBER_CONFIGS[group_id])
            bot.delete_message(call.message.chat.id, call.message.message_id)
            show_config_menu_private(call.message.chat.id, group_id)
        elif action == "setremovegrace":
            msg = bot.send_message(
                call.message.chat.id,
                "Enter the auto-remove grace period in hours (0-720). "
                "Use 0 only if you intentionally want the final fresh "
                "recheck on the next scan.",
                reply_markup=types.ForceReply(selective=True),
            )
            bot.register_next_step_handler(
                msg,
                process_set_auto_remove_grace,
                group_id,
            )
        elif action == "viewwallets":
            display_wallet_holdings(group_id, send_to_chat_id=call.message.chat.id)
        elif action == "viewsettings":
            # Check if this is a voting settings request
            if call.data.startswith("privvote_"):
                 display_voting_settings(group_id, send_to_chat_id=call.message.chat.id)
            else:
                 display_settings(group_id, send_to_chat_id=call.message.chat.id)
        elif action == "createreglink": # Using the fixed name from before
            success = create_registration_link(group_id, send_to_chat_id=call.message.chat.id)
            if not success:
                bot.send_message(call.message.chat.id, "❌ Failed to create registration link. Please try again.")
        elif action == "exemptions":
            bot.delete_message(call.message.chat.id, call.message.message_id)
            display_exemption_manager(group_id, send_to_chat_id=call.message.chat.id)
        elif action == "toggleexempt" and len(parts) >= 4:
            target_user_id = int(parts[3])
            user_reg = get_user_registration(group_id, target_user_id)
            current_status = user_reg["is_exempt"] if user_reg else False
            new_status = not current_status
            toggle_user_exemption(group_id, target_user_id, new_status)
            bot.delete_message(call.message.chat.id, call.message.message_id)
            display_exemption_manager(group_id, call.message.chat.id)
        elif action == "setnftcollection":
            msg = bot.send_message(call.message.chat.id, "Please enter the new NFT collection ID:", reply_markup=types.ForceReply(selective=True))
            bot.register_next_step_handler(msg, process_set_nft_collection, group_id)
        elif action == "setnftthreshold":
            msg = bot.send_message(call.message.chat.id, "Please enter the new NFT threshold (e.g., 1):", reply_markup=types.ForceReply(selective=True))
            bot.register_next_step_handler(msg, process_set_nft_threshold, group_id)
        elif action == "setregmode":
            markup = types.InlineKeyboardMarkup()
            # IMPORTANT: Ensure new buttons use the 'privconfig_' prefix
            markup.add(types.InlineKeyboardButton("Token Only", callback_data=f"privconfig_{group_id}_regmode_token"))
            markup.add(types.InlineKeyboardButton("NFT Only", callback_data=f"privconfig_{group_id}_regmode_nft"))
            markup.add(types.InlineKeyboardButton("Token OR NFT", callback_data=f"privconfig_{group_id}_regmode_both"))
            bot.edit_message_text("Choose the registration mode:", chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=markup)
        elif action == "regmode" and len(parts) >= 4:
            mode = parts[3]
            with config_lock:
                ensure_config_exists(group_id)
                SUBSCRIBER_CONFIGS[group_id]['registration_mode'] = mode
                update_config_in_db(group_id, SUBSCRIBER_CONFIGS[group_id])
            bot.delete_message(call.message.chat.id, call.message.message_id)
            show_config_menu_private(call.message.chat.id, group_id)
        elif action == "back":
            bot.delete_message(call.message.chat.id, call.message.message_id)
            show_config_menu_private(call.message.chat.id, group_id)
        # --- Voting Actions Now Handled Here ---
        elif action == "setvotespernft":
            msg = bot.send_message(call.message.chat.id, "Enter votes per NFT:", reply_markup=types.ForceReply(selective=True))
            bot.register_next_step_handler(msg, process_set_votes_per_nft, group_id)
        elif action == "setvotespermillion":
            msg = bot.send_message(call.message.chat.id, "Enter votes per 1M tokens:", reply_markup=types.ForceReply(selective=True))
            bot.register_next_step_handler(msg, process_set_votes_per_million, group_id)
        elif action == "setvoteduration":
            msg = bot.send_message(call.message.chat.id, "Enter vote duration in hours:", reply_markup=types.ForceReply(selective=True))
            bot.register_next_step_handler(msg, process_set_vote_duration, group_id)
        elif action == "setvotesperexempt":
            msg = bot.send_message(call.message.chat.id, "Enter votes for exempt users:", reply_markup=types.ForceReply(selective=True))
            bot.register_next_step_handler(msg, process_set_votes_per_exempt, group_id)
        elif action == "settraitgate":
            with config_lock:
                current_config = ensure_config_exists(group_id)
            trait_name = current_config.get("nft_trait_name", "") or "Not set"
            trait_value = current_config.get("nft_trait_value", "") or "Any value"
            trait_threshold = current_config.get("nft_trait_threshold", 1)
            
            current_settings = (
                f"🎨 **Current NFT Trait Gate Settings:**\n\n"
                f"Trait Name: `{trait_name}`\n"
                f"Trait Value: `{trait_value}`\n"
                f"Threshold: `{trait_threshold}`\n\n"
                "Select an option to configure:"
            )
            
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("Set Trait Name", callback_data=f"privconfig_{group_id}_settraitname"))
            markup.add(types.InlineKeyboardButton("Set Trait Value", callback_data=f"privconfig_{group_id}_settraitvalue"))
            markup.add(types.InlineKeyboardButton("Set Trait Threshold", callback_data=f"privconfig_{group_id}_settraitthreshold"))
            markup.add(types.InlineKeyboardButton("Clear Trait Gate", callback_data=f"privconfig_{group_id}_cleartraitgate"))
            markup.add(types.InlineKeyboardButton("⬅️ Back", callback_data=f"privconfig_{group_id}_back"))
            
            bot.edit_message_text(current_settings, chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=markup, parse_mode="Markdown")
        elif action == "settraitname":
            msg = bot.send_message(
                call.message.chat.id, 
                "Enter the NFT trait name (e.g., 'Background', 'Rarity', 'Species'):\n\n"
                "_This is the attribute category you want to gate by._",
                reply_markup=types.ForceReply(selective=True),
                parse_mode="Markdown"
            )
            bot.register_next_step_handler(msg, process_set_trait_name, group_id)
        elif action == "settraitvalue":
            msg = bot.send_message(
                call.message.chat.id, 
                "Enter the NFT trait value (e.g., 'Blue', 'Legendary', 'Dragon'):\n\n"
                "_Leave empty or type 'any' to match any value in the trait category._",
                reply_markup=types.ForceReply(selective=True),
                parse_mode="Markdown"
            )
            bot.register_next_step_handler(msg, process_set_trait_value, group_id)
        elif action == "settraitthreshold":
            msg = bot.send_message(
                call.message.chat.id, 
                "Enter the minimum number of NFTs with this trait required (e.g., 1):",
                reply_markup=types.ForceReply(selective=True)
            )
            bot.register_next_step_handler(msg, process_set_trait_threshold, group_id)
        elif action == "cleartraitgate":
            with config_lock:
                if group_id in SUBSCRIBER_CONFIGS:
                    SUBSCRIBER_CONFIGS[group_id]['nft_trait_name'] = ''
                    SUBSCRIBER_CONFIGS[group_id]['nft_trait_value'] = ''
                    SUBSCRIBER_CONFIGS[group_id]['nft_trait_threshold'] = 1
                    update_config_in_db(group_id, SUBSCRIBER_CONFIGS[group_id])
            bot.send_message(call.message.chat.id, "✅ NFT trait gate has been cleared.")
            bot.delete_message(call.message.chat.id, call.message.message_id)
            show_config_menu_private(call.message.chat.id, group_id)

    except Exception as e:
        logging.error(f"Error in handle_private_config_callback: {e}")
        bot.answer_callback_query(call.id, "❌ An error occurred.")
        

@bot.callback_query_handler(func=lambda call: call.data.startswith("mywallet_"))
def handle_mywallets_callback(call):
    """Handles callbacks from the 'my wallets' private menu."""
    try:
        parts = call.data.split("_")
        if len(parts) < 3:
            bot.answer_callback_query(call.id, "❌ Invalid wallet action.")
            return

        group_id = int(parts[1])
        action = parts[2]
        user_id = call.from_user.id

        bot.answer_callback_query(call.id)

        if action == "dodelete" and len(parts) == 4:
            try:
                wallet_idx = int(parts[3])
            except (ValueError, TypeError):
                bot.answer_callback_query(call.id, "❌ Invalid wallet selection.")
                return
            user_reg = get_user_registration(group_id, user_id)
            current_wallets = user_reg.get("wallets", []) if user_reg else []
            is_exempt = user_reg.get("is_exempt", False) if user_reg else False

            if wallet_idx < 0 or wallet_idx >= len(current_wallets):
                bot.answer_callback_query(call.id, "❌ Wallet not found.")
                return

            wallet_to_remove = current_wallets[wallet_idx]
            # Case-insensitive removal
            updated_wallets = [w for w in current_wallets if w.lower() != wallet_to_remove.lower()]

            with config_lock:
                cfg = SUBSCRIBER_CONFIGS.get(group_id)
                reg_type = cfg.get("registration_mode", "token") if cfg else "token"
            save_wallet_for_user(
                group_id, 
                user_id, 
                call.from_user.username or call.from_user.first_name, 
                updated_wallets, 
                is_exempt=is_exempt,
                replace_existing=True, 
                registration_type=reg_type
            )
            bot.send_message(call.message.chat.id, "✅ Wallet removed successfully.")
            bot.delete_message(call.message.chat.id, call.message.message_id)
            show_mywallets_private(call.message.chat.id, group_id)

        elif action == "back":
            bot.delete_message(call.message.chat.id, call.message.message_id)
            show_mywallets_private(call.message.chat.id, group_id)

    except Exception as e:
        logging.error(f"Error in handle_mywallets_callback: {e}")
        bot.answer_callback_query(call.id, "❌ An error occurred.")

@bot.callback_query_handler(func=lambda call: call.data.startswith("poll_vote_"))
def handle_poll_callback(call):
    """Handle poll voting callbacks specifically."""
    try:
        if call.data.startswith("poll_vote_"):
            parts = call.data.split("_")
            if len(parts) == 4:
                poll_id = parts[2]
                option_index = int(parts[3])
                handle_poll_vote(call, poll_id, option_index)
                return
            else:
                bot.answer_callback_query(call.id, "❌ Invalid poll action.")
                return
    except Exception as e:
        logging.error(f"Error in poll callback handler: {e}")
        bot.answer_callback_query(call.id, "❌ An error occurred.")



def create_single_use_invite_link(group_id):
    """Create a short-lived single-use invite link."""
    try:
        expire_date = int(time.time()) + 3600
        invite = bot.create_chat_invite_link(group_id, expire_date=expire_date, member_limit=1)
        return getattr(invite, 'invite_link', None) or invite.get('invite_link')
    except Exception as e:
        logging.warning(f"Failed to create single-use invite link for {group_id}: {e}")
        return None

def create_registration_link(group_id, send_to_chat_id=None):
    # If send_to_chat_id is provided, send results there, otherwise send to group_id
    target_chat_id = send_to_chat_id if send_to_chat_id is not None else group_id

    logging.info(f"Creating registration link for group ID: {group_id}")
    try:
        bot_username = get_bot_username()
        reg_link = f"https://t.me/{bot_username}?start=register_{encode_group_id_for_deeplink(group_id)}"
        try:
            chat_info = bot.get_chat(group_id)
            group_name = chat_info.title
        except Exception as e:
            logging.error(f"Error getting chat info for group {group_id}: {e}")
            group_name = "this group"
        message = (
            f"📱 <b>Registration Link for {group_name}</b>\n\n"
            f"Share this link with users to register their wallets:\n"
            f"<a href='{reg_link}'>{reg_link}</a>\n\n"
            f"<i>Users will be prompted to register their wallets after clicking this link.</i>\n"
            f"<i>Registration is required to remain in the group.</i>"
        )
        sent_message = bot.send_message(target_chat_id, message, parse_mode="HTML", disable_web_page_preview=True)
        logging.info(f"Registration link message sent successfully to chat {target_chat_id}, message ID: {sent_message.message_id}")
        return True
    except Exception as e:
        logging.error(f"Error in create_registration_link for group {group_id} to chat {target_chat_id}: {e}")
        try:
            bot.send_message(target_chat_id, "❌ Error creating registration link. Please try again.")
        except Exception as fallback_e:
            logging.error(f"Failed to send error message: {fallback_e}")
        return False

def show_subscription_prompt(chat_id, group_id, group_name):
    """Show subscription tier selection when group has no active subscription."""
    # Safety-net: never show payment prompt for whitelisted groups.
    if is_group_whitelisted(group_id):
        logging.info(f"show_subscription_prompt called for whitelisted group {group_id} — redirecting to config menu.")
        show_config_menu_private(chat_id, group_id)
        return

    sub = get_group_subscription(group_id)
    if sub and sub["expires_at"]:
        exp = sub["expires_at"]
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=datetime.timezone.utc)
        status_line = f"\n⏰ Your previous subscription expired on {exp.strftime('%Y-%m-%d')}.\n"
    else:
        status_line = ""

    markup = types.InlineKeyboardMarkup()
    for tier_key, tier_info in SUBSCRIPTION_TIERS.items():
        markup.add(types.InlineKeyboardButton(
            f"💳 {tier_info['label']} — {tier_info['display']}",
            callback_data=f"subscribe_{group_id}_{tier_key}"
        ))

    bot.send_message(
        chat_id,
        f"🔒 **Subscription Required**\n\n"
        f"To configure and use CityWatchBot in *{group_name}*, "
        f"an active subscription is required.\n"
        f"{status_line}\n"
        f"Choose a plan below:\n\n"
        f"• 1 Month — $3.99\n"
        f"• 3 Months — $11.99\n"
        f"• 6 Months — $21.99\n",
        reply_markup=markup,
        parse_mode="Markdown"
    )

def show_config_menu_private(chat_id, group_id):
    """Show configuration menu in private chat for a specific group"""
    try:
        # Get group name
        try:
            chat_obj = bot.get_chat(group_id)
            group_name = chat_obj.title
        except Exception as e:
            logging.warning(f"Could not resolve group title for {group_id}: {e}")
            group_name = f"Group {group_id}"

        # --- Subscription gate ---
        if not group_has_active_subscription(group_id):
            show_subscription_prompt(chat_id, group_id, group_name)
            return

        # Store the group context for this admin's config session
        admin_config_context[chat_id] = group_id
        with get_db_cursor() as (conn, cur):
            cur.execute("""
                INSERT INTO pending_verifications (user_id, group_id, created_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (user_id) DO UPDATE SET
                    group_id = EXCLUDED.group_id,
                    created_at = EXCLUDED.created_at
            """, (chat_id, group_id))

        # Get current config, creating default if needed
        with config_lock:
            current_config = ensure_config_exists(group_id)
        auto_remove_status = "ON" if current_config.get("auto_remove", False) else "OFF"
        grace_hours = (
            int(
                current_config.get(
                    "auto_remove_grace_seconds",
                    DEFAULT_AUTO_REMOVE_GRACE_SECONDS,
                )
            )
            / 3600
        )
        reg_mode = current_config.get("registration_mode", "token")
        reg_mode_display = get_registration_mode_display(reg_mode)

        markup = types.InlineKeyboardMarkup()
        btn1 = types.InlineKeyboardButton(f"Registration Mode: {reg_mode_display}", callback_data=f"privconfig_{group_id}_setregmode")
        btn2 = types.InlineKeyboardButton(f"Toggle Auto-Remove (Status: {auto_remove_status})", callback_data=f"privconfig_{group_id}_toggleautoremove")
        grace_label = f"{grace_hours:g}h"
        btn2b = types.InlineKeyboardButton(
            f"Auto-Remove Grace: {grace_label}",
            callback_data=f"privconfig_{group_id}_setremovegrace",
        )
        btn3 = types.InlineKeyboardButton("Set Token Config", callback_data=f"privconfig_{group_id}_settokenconfig")
        btn6 = types.InlineKeyboardButton("Set NFT Collection", callback_data=f"privconfig_{group_id}_setnftcollection")
        btn7 = types.InlineKeyboardButton("Set NFT Threshold", callback_data=f"privconfig_{group_id}_setnftthreshold")
        btn8 = types.InlineKeyboardButton("View Settings", callback_data=f"privconfig_{group_id}_viewsettings")
        btn9 = types.InlineKeyboardButton("View Wallets", callback_data=f"privconfig_{group_id}_viewwallets")
        btn10 = types.InlineKeyboardButton("Manage Exemptions", callback_data=f"privconfig_{group_id}_exemptions")
        btn11 = types.InlineKeyboardButton("Create Registration Link", callback_data=f"privconfig_{group_id}_createreglink")
        btn12 = types.InlineKeyboardButton("Set NFT Trait Gate", callback_data=f"privconfig_{group_id}_settraitgate")

        markup.add(btn1)
        markup.add(btn2)
        markup.add(btn2b)
        markup.add(btn3)
        markup.add(btn6, btn7)
        markup.add(btn12)
        markup.add(btn8, btn9)
        markup.add(btn10)
        markup.add(btn11)

        bot.send_message(
            chat_id, 
            f"⚙️ **Configuration for {group_name}**\n\nSelect an option to configure:",
            reply_markup=markup,
            parse_mode="Markdown"
        )
        logging.info(f"Sent private config menu for group {group_id} to user {chat_id}")
    except Exception as e:
        logging.error(f"Error showing private config menu: {e}")
        bot.send_message(chat_id, "❌ Error loading configuration menu. Please try again.")

def show_votesetup_menu_private(chat_id, group_id):
    """Show voting setup menu in private chat for a specific group"""
    try:
        # Get group name
        try:
            chat_obj = bot.get_chat(group_id)
            group_name = chat_obj.title
        except Exception as e:
            logging.warning(f"Could not resolve group title for {group_id}: {e}")
            group_name = f"Group {group_id}"

        # --- Subscription gate ---
        if not group_has_active_subscription(group_id):
            show_subscription_prompt(chat_id, group_id, group_name)
            return

        # Store the group context for this admin's config session
        admin_config_context[chat_id] = group_id
        with get_db_cursor() as (conn, cur):
            cur.execute("""
                INSERT INTO pending_verifications (user_id, group_id, created_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (user_id) DO UPDATE SET
                    group_id = EXCLUDED.group_id,
                    created_at = EXCLUDED.created_at
            """, (chat_id, group_id))

        markup = types.InlineKeyboardMarkup()
        btn1 = types.InlineKeyboardButton("Votes per NFT", callback_data=f"privvote_{group_id}_setvotespernft")
        btn2 = types.InlineKeyboardButton("Votes per 1M Tokens", callback_data=f"privvote_{group_id}_setvotespermillion")
        btn3 = types.InlineKeyboardButton("Vote Duration", callback_data=f"privvote_{group_id}_setvoteduration")
        btn4 = types.InlineKeyboardButton("Votes per Exempt User", callback_data=f"privvote_{group_id}_setvotesperexempt")
        btn5 = types.InlineKeyboardButton("View Voting Settings", callback_data=f"privvote_{group_id}_viewsettings")

        markup.add(btn1)
        markup.add(btn2)
        markup.add(btn3)
        markup.add(btn4)
        markup.add(btn5)

        bot.send_message(
            chat_id, 
            f"🗳️ **Voting Setup for {group_name}**\n\nSelect an option to configure:",
            reply_markup=markup,
            parse_mode="Markdown"
        )
        logging.info(f"Sent private voting setup menu for group {group_id} to user {chat_id}")
    except Exception as e:
        logging.error(f"Error showing private voting setup menu: {e}")
        bot.send_message(chat_id, "❌ Error loading voting setup menu. Please try again.")

def show_mywallets_private(chat_id, group_id):
    """Show user's wallets in private chat for a specific group"""
    try:
        user_id = chat_id

        # Get group name
        try:
            chat_obj = bot.get_chat(group_id)
            group_name = chat_obj.title
        except Exception as e:
            logging.warning(f"Could not resolve group title for {group_id}: {e}")
            group_name = f"Group {group_id}"

        # Get group configuration for holdings display.
        with config_lock:
            config = SUBSCRIBER_CONFIGS.get(group_id, {})

        # Build registration URL for the website-gated flow
        verify_url = build_wallet_connect_url(group_id, user_id)

        markup = types.InlineKeyboardMarkup()
        verify_btn = types.InlineKeyboardButton("➕ Add/Verify Wallet", url=verify_url)
        markup.add(verify_btn)

        # Get user registration data
        user_reg = get_user_registration(group_id, user_id)

        if not user_reg:
            bot.send_message(chat_id, f"❌ You are not registered in {group_name}.", reply_markup=markup)
            return

        if user_reg["is_exempt"]:
            bot.send_message(chat_id, f"✅ You are exempt from wallet requirements in {group_name}.")
            return

        wallets = user_reg["wallets"]
        if not wallets:
            bot.send_message(chat_id, f"❌ You have no registered wallets for {group_name}.", reply_markup=markup)
            return

        registration_mode = config.get("registration_mode", "token")
        token = config.get("token", "")
        decimals = config.get("decimals", 6)
        minimum_holding = config.get("minimum_holding", 0)
        nft_collection_id = config.get("nft_collection_id", "")
        nft_threshold = config.get("nft_threshold", 1)
        nft_trait_name = config.get("nft_trait_name", "")
        nft_trait_value = config.get("nft_trait_value", "")
        nft_trait_threshold = config.get("nft_trait_threshold", 1)

        has_token = bool(token) and registration_mode in ["token", "both"]
        has_nft = bool(nft_collection_id) and registration_mode in ["nft", "both"]
        has_trait = has_nft and bool(nft_trait_name)

        # Show processing message
        processing_msg = bot.send_message(chat_id, f"⏳ Loading your wallet information for {group_name}...")

        try:
            # Fetch balances for all wallets
            wallet_balances = {}
            if has_token:
                balances = fetch_wallet_balances(wallets, token, decimals)
                wallet_balances = balances

            # Fetch NFT count
            user_nft_count = None
            if has_nft:
                try:
                    user_wallets_lower = [w.lower() for w in wallets]
                    user_nft_count = get_user_nft_count(user_wallets_lower, nft_collection_id)
                except Exception as e:
                    logging.error(f"Error getting NFT count in show_mywallets_private: {e}")

                # Persist successful result or fall back to cached data
                if user_nft_count is not None:
                    try:
                        update_user_cached_holdings(group_id, user_id, nft_count=user_nft_count)
                    except Exception:
                        pass
                else:
                    try:
                        cached = get_user_cached_holdings(group_id, user_id)
                        if cached and cached.get("nft_count") is not None:
                            user_nft_count = cached["nft_count"]
                    except Exception:
                        pass

            # Fetch trait count
            user_trait_count = None
            if has_trait and user_nft_count is not None and user_nft_count > 0:
                try:
                    user_wallets_lower = [w.lower() for w in wallets]
                    if nft_trait_value:
                        user_trait_count = get_user_nft_trait_count(user_wallets_lower, nft_collection_id, nft_trait_name, nft_trait_value)
                    else:
                        user_trait_count = get_user_nft_category_count(user_wallets_lower, nft_collection_id, nft_trait_name)
                except Exception as e:
                    logging.error(f"Error getting NFT trait count in show_mywallets_private: {e}")

                # Persist successful result or fall back to cached data
                if user_trait_count is not None:
                    try:
                        update_user_cached_holdings(group_id, user_id, trait_count=user_trait_count)
                    except Exception:
                        pass
                else:
                    try:
                        cached = get_user_cached_holdings(group_id, user_id)
                        if cached and cached.get("trait_count") is not None:
                            user_trait_count = cached["trait_count"]
                    except Exception:
                        pass

            # Build wallet information message
            message_lines = [
                f"💰 *Your Registered Wallets for {group_name}*\n"
            ]

            total_balance = 0
            for i, wallet in enumerate(wallets):
                # Truncate wallet address for display
                display_wallet = f"{wallet[:8]}...{wallet[-6:]}"

                if has_token:
                    balance = wallet_balances.get(wallet.lower(), 0) or 0
                    total_balance += balance
                    status_emoji = "✅" if balance >= minimum_holding else "⚠️"
                    message_lines.append(f"{status_emoji} `{display_wallet}`")
                    message_lines.append(f"    Balance: {balance:,.2f} tokens")
                else:
                    message_lines.append(f"📱 `{display_wallet}`")
                message_lines.append("")

            # Token summary
            if has_token:
                threshold_status = "✅ Above" if total_balance >= minimum_holding else "❌ Below"
                message_lines.append(f"*Total Balance:* {total_balance:,.2f} tokens")
                message_lines.append(f"*Token Status:* {threshold_status} threshold ({minimum_holding:,.2f})")

            # NFT summary
            if has_nft:
                if user_nft_count is not None:
                    nft_status = "✅ Above" if user_nft_count >= nft_threshold else "❌ Below"
                    message_lines.append(f"*NFTs in Collection:* {user_nft_count}")
                    message_lines.append(f"*NFT Status:* {nft_status} threshold ({nft_threshold})")
                else:
                    message_lines.append(f"*NFTs in Collection:* N/A")

            # Trait summary
            if has_trait:
                trait_label = f"{nft_trait_name}={nft_trait_value}" if nft_trait_value else f"{nft_trait_name} (any value)"
                if user_trait_count is not None:
                    trait_status = "✅ Above" if user_trait_count >= nft_trait_threshold else "❌ Below"
                    message_lines.append(f"*Trait ({trait_label}):* {user_trait_count}")
                    message_lines.append(f"*Trait Status:* {trait_status} threshold ({nft_trait_threshold})")
                elif user_nft_count is not None:
                    message_lines.append(f"*Trait ({trait_label}):* N/A")

            # Create inline keyboard for wallet management
            markup = types.InlineKeyboardMarkup()

            # Add inline delete buttons for each individual wallet
            for i, wallet in enumerate(wallets):
                display_wallet = f"{wallet[:8]}...{wallet[-6:]}"
                callback_data = f"mywallet_{group_id}_dodelete_{i}"
                markup.add(types.InlineKeyboardButton(f"🗑️ Remove {display_wallet}", callback_data=callback_data))

            # Add/Verify Wallet button pointing to the website-gated flow
            add_btn = types.InlineKeyboardButton("➕ Add/Verify Wallet", url=verify_url)
            markup.add(add_btn)

            wallet_message = "\n".join(message_lines)

            bot.edit_message_text(
                wallet_message,
                chat_id=chat_id,
                message_id=processing_msg.message_id,
                parse_mode="Markdown",
                reply_markup=markup
            )

        except Exception as e:
            logging.error(f"Error in show_mywallets_private processing: {e}")
            bot.edit_message_text(
                "❌ Error loading wallet information. Please try again later.",
                chat_id=chat_id,
                message_id=processing_msg.message_id
            )

    except Exception as e:
        logging.error(f"Error in show_mywallets_private: {e}")
        bot.send_message(chat_id, "❌ An error occurred while processing your request.")

def display_wallet_holdings(group_id, send_to_chat_id=None):
    target_chat_id = send_to_chat_id if send_to_chat_id is not None else group_id

    try:
        processing_msg = bot.send_message(target_chat_id, "🔍 *Fetching and compiling wallet report...*\n\nThis may take a moment, please wait.", parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Failed to send processing message: {e}")
        processing_msg = None

    with config_lock:
        cfg = SUBSCRIBER_CONFIGS.get(group_id)
    if not cfg:
        if processing_msg:
             bot.edit_message_text("No configuration found for this group.", chat_id=target_chat_id, message_id=processing_msg.message_id)
        else:
             bot.send_message(target_chat_id, "No configuration found for this group.")
        return

    registration_mode = cfg.get("registration_mode", "token")
    token = cfg.get("token", "")
    decimals = cfg.get("decimals", 6)
    threshold = cfg.get("minimum_holding", 0)
    nft_collection_id = cfg.get("nft_collection_id", "")
    nft_threshold = cfg.get("nft_threshold", 1)
    nft_trait_name = cfg.get("nft_trait_name", "")
    nft_trait_value = cfg.get("nft_trait_value", "")
    nft_trait_threshold = cfg.get("nft_trait_threshold", 1)

    # Validate that at least one gating criterion is configured
    has_token = bool(token) and registration_mode in ["token", "both"]
    has_nft = bool(nft_collection_id) and registration_mode in ["nft", "both"]
    has_trait = has_nft and bool(nft_trait_name)
    if not has_token and not has_nft:
        if processing_msg:
             bot.edit_message_text("No token or NFT gating configured for this group.", chat_id=target_chat_id, message_id=processing_msg.message_id)
        else:
             bot.send_message(target_chat_id, "No token or NFT gating configured for this group.")
        return

    regs = get_user_registrations_for_group(group_id)
    if not regs:
        if processing_msg:
             bot.edit_message_text("No users have registered yet.", chat_id=target_chat_id, message_id=processing_msg.message_id)
        else:
             bot.send_message(target_chat_id, "No users have registered yet.")
        return

    # BATCH FETCH: Step 1 - Gather all unique wallet addresses from all users
    all_wallets_to_check = set()
    for reg in regs:
        if not reg["is_exempt"] and reg["wallets"]:
            for wallet in reg["wallets"]:
                all_wallets_to_check.add(wallet.lower())

    # BATCH FETCH: Step 2 - Fetch token balances if applicable
    all_balances = {}
    if has_token and all_wallets_to_check:
        logging.info(f"Starting batch balance check for {len(all_wallets_to_check)} unique wallets in group {group_id} report.")
        all_balances = fetch_wallet_balances(list(all_wallets_to_check), token, decimals)

    # BATCH FETCH: Step 3 - Process users using the pre-fetched data
    rows = []
    for reg in regs:
        username = reg["username"]
        wallets = reg["wallets"] or []
        exempt = reg["is_exempt"]

        wallet_lines = []
        total_balance = Decimal(0)
        balance_complete = True
        user_nft_count = None
        user_trait_count = None

        if not exempt and wallets:
            # Token balance check
            if has_token:
                for w in wallets:
                    b = all_balances.get(w.lower())
                    if b is None:
                        wallet_lines.append(f"{w}: N/A")
                        balance_complete = False
                    else:
                        wallet_lines.append(f"{w}: {b:,.2f}")
                        total_balance += b

            # NFT count check (per-user, across all their wallets)
            if has_nft:
                try:
                    user_wallets_lower = [w.lower() for w in wallets]
                    user_nft_count = get_user_nft_count(user_wallets_lower, nft_collection_id)
                except Exception as e:
                    logging.error(f"Error getting NFT count for user {username}: {e}")
                    user_nft_count = None

                # Persist successful result or fall back to cached data
                if user_nft_count is not None:
                    try:
                        update_user_cached_holdings(group_id, reg["user_id"], nft_count=user_nft_count)
                    except Exception:
                        pass
                else:
                    try:
                        cached = get_user_cached_holdings(group_id, reg["user_id"])
                        if cached and cached.get("nft_count") is not None:
                            user_nft_count = cached["nft_count"]
                    except Exception:
                        pass

                # NFT trait count check
                if has_trait and user_nft_count is not None and user_nft_count > 0:
                    try:
                        user_wallets_lower = [w.lower() for w in wallets]
                        if nft_trait_value:
                            user_trait_count = get_user_nft_trait_count(user_wallets_lower, nft_collection_id, nft_trait_name, nft_trait_value)
                        else:
                            user_trait_count = get_user_nft_category_count(user_wallets_lower, nft_collection_id, nft_trait_name)
                    except Exception as e:
                        logging.error(f"Error getting NFT trait count for user {username}: {e}")
                        user_trait_count = None

                    # Persist successful result or fall back to cached data
                    if user_trait_count is not None:
                        try:
                            update_user_cached_holdings(group_id, reg["user_id"], trait_count=user_trait_count)
                        except Exception:
                            pass
                    else:
                        try:
                            cached = get_user_cached_holdings(group_id, reg["user_id"])
                            if cached and cached.get("trait_count") is not None:
                                user_trait_count = cached["trait_count"]
                        except Exception:
                            pass

        if wallet_lines:
            wallet_text = "\n".join(wallet_lines)
        elif not has_token and wallets:
            wallet_text = "\n".join(wallets)
        else:
            wallet_text = "None"

        # Build the holdings summary line
        holdings_parts = []
        if has_token:
            if not balance_complete and wallets:
                holdings_parts.append("N/A")
            else:
                holdings_parts.append(f"{total_balance:,.2f} tokens")
        if has_nft:
            if user_nft_count is not None:
                holdings_parts.append(f"{user_nft_count} NFTs")
            elif not exempt and wallets:
                holdings_parts.append("NFTs: N/A")
        if has_trait:
            trait_label = f"{nft_trait_name}={nft_trait_value}" if nft_trait_value else f"{nft_trait_name}"
            if user_trait_count is not None:
                holdings_parts.append(f"{user_trait_count} Trait ({trait_label})")
            elif not exempt and wallets and user_nft_count is not None:
                holdings_parts.append(f"Trait ({trait_label}): N/A")
        total_str = " | ".join(holdings_parts) if holdings_parts else ""

        # Determine status based on registration mode
        status = ""
        if exempt:
            status = "Exempt"
        elif not wallets:
            status = "No Wallets"
        elif registration_mode == "token":
            if not balance_complete:
                status = "No Data"
            elif total_balance >= threshold:
                status = "Above Threshold"
            else:
                status = "Below Threshold"
        elif registration_mode == "nft":
            if user_nft_count is None:
                status = "No Data"
            elif user_nft_count >= nft_threshold:
                # Check trait validity if trait gating is configured
                if has_trait and user_trait_count is not None and user_trait_count < nft_trait_threshold:
                    status = "Below Threshold"
                else:
                    status = "Above Threshold"
            else:
                status = "Below Threshold"
        elif registration_mode == "both":
            token_ok = (balance_complete and total_balance >= threshold) if has_token else False
            nft_ok = (user_nft_count is not None and user_nft_count >= nft_threshold) if has_nft else False
            # Apply trait check to NFT validity
            if nft_ok and has_trait and user_trait_count is not None and user_trait_count < nft_trait_threshold:
                nft_ok = False
            if not balance_complete and user_nft_count is None:
                status = "No Data"
            elif token_ok or nft_ok:
                status = "Above Threshold"
            else:
                status = "Below Threshold"

        rows.append({
            "Username": username,
            "Wallets": wallet_text,
            "Total Balance": total_str,
            "Status": status
        })

    # The rest of the function (generating preview or CSV) remains the same
    try:
        preview_lines = []
        for r in rows:
            block = f"*{r['Username']}* — {r['Status']}\n"
            block += "\n".join(r["Wallets"].split("\n")) + "\n"
            if r["Total Balance"]:
                block += f"_Holdings: {r['Total Balance']}_\n"
            preview_lines.append(block)
        preview = "\n".join(preview_lines)

        if len(preview) < 4000:
            if processing_msg:
                bot.edit_message_text(preview, chat_id=target_chat_id, message_id=processing_msg.message_id, parse_mode="Markdown")
            else:
                bot.send_message(target_chat_id, preview, parse_mode="Markdown")
            return

        if processing_msg:
            bot.delete_message(target_chat_id, processing_msg.message_id)

        headers = ["Username", "Wallets", "Total Balance", "Status"]
        str_buf = StringIO()
        writer = csv.DictWriter(str_buf, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)

        byte_buf = BytesIO(str_buf.getvalue().encode("utf-8"))
        byte_buf.name = "wallets_report.csv"
        bot.send_document(
            target_chat_id,
            byte_buf,
            caption="📊 Wallet Holdings Report (CSV)"
        )
    except Exception as e:
        logging.error(f"Error updating wallet holdings message: {e}")
        error_text = "❌ An error occurred while generating the report."
        try:
            if processing_msg:
                bot.edit_message_text(error_text, chat_id=target_chat_id, message_id=processing_msg.message_id)
            else:
                bot.send_message(target_chat_id, error_text)
        except Exception as final_e:
            logging.error(f"Failed to send fallback error message: {final_e}")

def display_settings(group_id, send_to_chat_id=None):
    # If send_to_chat_id is provided, send results there, otherwise send to group_id
    target_chat_id = send_to_chat_id if send_to_chat_id is not None else group_id

    with config_lock:
        config = ensure_config_exists(group_id)
    token = config.get("token", "Not set") if config.get("token") else "Not set"
    threshold = config.get("minimum_holding", 5000000)
    decimals = config.get("decimals", 6)
    auto_remove = config.get("auto_remove", False)
    auto_remove_grace_hours = (
        int(
            config.get(
                "auto_remove_grace_seconds",
                DEFAULT_AUTO_REMOVE_GRACE_SECONDS,
            )
        )
        / 3600
    )
    num_users = 0
    try:
        with get_db_cursor() as (conn, cur):
            cur.execute("SELECT COUNT(*) FROM user_wallets WHERE group_id=%s", (group_id,))
            result = cur.fetchone()
            if result:
                num_users = result[0]
    except Exception as e:
        logging.error(f"Error retrieving user count for group {group_id}: {e}")
    exempt_count = 0
    try:
        with get_db_cursor() as (conn, cur):
            cur.execute("SELECT COUNT(*) FROM user_wallets WHERE group_id=%s AND is_exempt=TRUE", (group_id,))
            result = cur.fetchone()
            if result:
                exempt_count = result[0]
    except Exception as e:
        logging.error(f"Error retrieving exempt count: {e}")
    nft_collection_id = config.get("nft_collection_id", "") or "Not set"
    nft_threshold = config.get("nft_threshold", 1)
    registration_mode = config.get("registration_mode", "token")
    registration_mode_display = get_registration_mode_display(registration_mode)
    nft_trait_name = config.get("nft_trait_name", "")
    nft_trait_value = config.get("nft_trait_value", "")
    nft_trait_threshold = config.get("nft_trait_threshold", 1)

    settings_report = (
        f"*Group Settings:*\n"
        f"Registration Mode: {registration_mode_display}\n"
        f"Token: {token}\n"
        f"Token Threshold: {threshold:,.0f} tokens\n"
        f"Decimals: {decimals}\n"
        f"Auto-Remove: {'ON' if auto_remove else 'OFF'}\n"
        f"Auto-Remove Grace: {auto_remove_grace_hours:g} hours\n"
        f"NFT Collection ID: {nft_collection_id}\n"
        f"NFT Threshold: {nft_threshold}\n"
        f"Registered Users: {num_users}\n"
        f"Exempt Users: {exempt_count}\n"
    )
    
    if nft_trait_name:
        trait_display = f"🎨 *Trait Gate:* {nft_trait_name}"
        if nft_trait_value:
            trait_display += f" = {nft_trait_value}"
        else:
            trait_display += " (any value)"
        trait_display += f" (min: {nft_trait_threshold})"
        settings_report += trait_display
    
    bot.send_message(target_chat_id, settings_report, parse_mode="Markdown")

def process_set_token_config(message, group_id):
    parts = message.text.strip().split()
    if len(parts) != 3:
        bot.send_message(message.chat.id, "❌ Invalid format. Please provide the token address, minimum holding, and decimals separated by spaces.")
        return

    token, threshold_str, decimals_str = parts
    try:
        threshold = Decimal(threshold_str)
        decimals = int(decimals_str)
        if not threshold.is_finite() or threshold < 0:
            raise ValueError("threshold must be a finite non-negative number")
        if decimals < 0 or decimals > 18:
            raise ValueError("decimals out of range")
    except (ValueError, OverflowError):
        bot.send_message(message.chat.id, "❌ Threshold must be a finite non-negative number and decimals must be between 0 and 18.")
        return

    # Validate SUI token type format: 0x<hex>::<module>::<Type>
    if not re.match(r'^0x[0-9a-fA-F]+::\w+::\w+$', token):
        bot.send_message(message.chat.id, "❌ Invalid token type format. Expected: `0x<address>::<module>::<Type>`", parse_mode="Markdown")
        return

    with config_lock:
        ensure_config_exists(group_id)
        SUBSCRIBER_CONFIGS[group_id]['token'] = token
        SUBSCRIBER_CONFIGS[group_id]['minimum_holding'] = threshold
        SUBSCRIBER_CONFIGS[group_id]['decimals'] = decimals
        update_config_in_db(group_id, SUBSCRIBER_CONFIGS[group_id])
        bot.send_message(message.chat.id, f"✅ Token configuration updated:\n- Address: {token}\n- Threshold: {threshold}\n- Decimals: {decimals}")


def process_set_auto_remove_grace(message, group_id):
    try:
        hours = Decimal(message.text.strip())
        if not hours.is_finite() or hours < 0 or hours > 720:
            raise ValueError("grace period out of range")
        seconds = int(hours * Decimal(3600))
    except (InvalidOperation, ValueError, OverflowError):
        bot.send_message(
            message.chat.id,
            "❌ Enter a finite number of hours between 0 and 720.",
        )
        return

    with config_lock:
        ensure_config_exists(group_id)
        SUBSCRIBER_CONFIGS[group_id][
            "auto_remove_grace_seconds"
        ] = seconds
        update_config_in_db(group_id, SUBSCRIBER_CONFIGS[group_id])
    bot.send_message(
        message.chat.id,
        f"✅ Auto-remove grace period updated to {hours:g} hours.",
    )
    show_config_menu_private(message.chat.id, group_id)


def process_set_nft_collection(message, group_id):
    collection_id = message.text.strip()
    with config_lock:
        ensure_config_exists(group_id)
        SUBSCRIBER_CONFIGS[group_id]['nft_collection_id'] = collection_id
        update_config_in_db(group_id, SUBSCRIBER_CONFIGS[group_id])
    bot.send_message(message.chat.id, f"✅ NFT Collection ID updated to: {collection_id}")

def process_set_nft_threshold(message, group_id):
    try:
        threshold = int(message.text.strip())
        if threshold < 1:
            raise ValueError("threshold must be positive")
    except (ValueError, OverflowError):
        bot.send_message(message.chat.id, "❌ Invalid NFT threshold. Please enter a positive integer.")
        return
    with config_lock:
        ensure_config_exists(group_id)
        SUBSCRIBER_CONFIGS[group_id]['nft_threshold'] = threshold
        update_config_in_db(group_id, SUBSCRIBER_CONFIGS[group_id])
    bot.send_message(message.chat.id, f"✅ NFT Threshold updated to: {threshold}")

def process_set_trait_name(message, group_id):
    """Set the NFT trait name for trait gating."""
    trait_name = message.text.strip()
    with config_lock:
        ensure_config_exists(group_id)
        SUBSCRIBER_CONFIGS[group_id]['nft_trait_name'] = trait_name
        update_config_in_db(group_id, SUBSCRIBER_CONFIGS[group_id])
    bot.send_message(message.chat.id, f"✅ NFT Trait Name updated to: `{trait_name}`", parse_mode="Markdown")

def process_set_trait_value(message, group_id):
    """Set the NFT trait value for trait gating."""
    trait_value = message.text.strip()
    if trait_value.lower() == 'any':
        trait_value = ''
    with config_lock:
        ensure_config_exists(group_id)
        SUBSCRIBER_CONFIGS[group_id]['nft_trait_value'] = trait_value
        update_config_in_db(group_id, SUBSCRIBER_CONFIGS[group_id])
    if trait_value:
        bot.send_message(message.chat.id, f"✅ NFT Trait Value updated to: `{trait_value}`", parse_mode="Markdown")
    else:
        bot.send_message(message.chat.id, "✅ NFT Trait Value cleared (will match any value in the trait category)")

def process_set_trait_threshold(message, group_id):
    """Set the NFT trait threshold for trait gating."""
    try:
        threshold = int(message.text.strip())
        if threshold < 1:
            raise ValueError("Threshold must be at least 1")
    except ValueError:
        bot.send_message(message.chat.id, "❌ Invalid value. Please enter a positive integer.")
        return
    with config_lock:
        ensure_config_exists(group_id)
        SUBSCRIBER_CONFIGS[group_id]['nft_trait_threshold'] = threshold
        update_config_in_db(group_id, SUBSCRIBER_CONFIGS[group_id])
    bot.send_message(message.chat.id, f"✅ NFT Trait Threshold updated to: {threshold}")

def process_set_votes_per_nft(message, group_id):
    try:
        votes = int(message.text.strip())
        if votes < 0:
            raise ValueError("Votes must be non-negative")
    except ValueError:
        bot.send_message(message.chat.id, "❌ Invalid value. Please enter a non-negative integer.")
        return
    with config_lock:
        ensure_config_exists(group_id)
        SUBSCRIBER_CONFIGS[group_id]['votes_per_nft'] = votes
        update_config_in_db(group_id, SUBSCRIBER_CONFIGS[group_id])
    bot.send_message(message.chat.id, f"✅ Votes per NFT updated to: {votes}")

def process_set_votes_per_million(message, group_id):
    try:
        votes = int(message.text.strip())
        if votes < 0:
            raise ValueError("Votes must be non-negative")
    except ValueError:
        bot.send_message(message.chat.id, "❌ Invalid value. Please enter a non-negative integer.")
        return
    with config_lock:
        ensure_config_exists(group_id)
        SUBSCRIBER_CONFIGS[group_id]['votes_per_million_tokens'] = votes
        update_config_in_db(group_id, SUBSCRIBER_CONFIGS[group_id])
    bot.send_message(message.chat.id, f"✅ Votes per 1M tokens updated to: {votes}")

def process_set_vote_duration(message, group_id):
    try:
        hours = float(message.text.strip())
        if not math.isfinite(hours) or hours <= 0:
            raise ValueError("Duration must be positive")
        # Convert hours to seconds
        duration = int(hours * 3600)
    except ValueError:
        bot.send_message(message.chat.id, "❌ Invalid value. Please enter a positive number (hours).")
        return
    with config_lock:
        ensure_config_exists(group_id)
        SUBSCRIBER_CONFIGS[group_id]['vote_duration'] = duration
        update_config_in_db(group_id, SUBSCRIBER_CONFIGS[group_id])
    bot.send_message(message.chat.id, f"✅ Vote duration updated to: {hours} hours ({duration} seconds)")

def process_set_votes_per_exempt(message, group_id):
    try:
        votes = int(message.text.strip())
        if votes < 0:
            raise ValueError("Votes must be non-negative")
    except ValueError:
        bot.send_message(message.chat.id, "❌ Invalid value. Please enter a non-negative integer.")
        return
    with config_lock:
        ensure_config_exists(group_id)
        SUBSCRIBER_CONFIGS[group_id]['votes_per_exempt'] = votes
        update_config_in_db(group_id, SUBSCRIBER_CONFIGS[group_id])
    bot.send_message(message.chat.id, f"✅ Votes per exempt user updated to: {votes}")

def display_voting_settings(group_id, send_to_chat_id=None):
    # If send_to_chat_id is provided, send results there, otherwise send to group_id
    target_chat_id = send_to_chat_id if send_to_chat_id is not None else group_id

    with config_lock:
        config = ensure_config_exists(group_id)

    votes_per_nft = config.get("votes_per_nft", 1)
    votes_per_million = config.get("votes_per_million_tokens", 1)
    vote_duration = config.get("vote_duration", 3600)
    votes_per_exempt = config.get("votes_per_exempt", 1)

    # Format duration
    hours = vote_duration // 3600
    minutes = (vote_duration % 3600) // 60
    seconds = vote_duration % 60
    time_str = f"{hours}h {minutes}m {seconds}s" if hours > 0 else f"{minutes}m {seconds}s" if minutes > 0 else f"{seconds}s"

    settings_report = (
        f"🗳️ *Voting Settings:*\n"
        f"Votes per NFT: {votes_per_nft}\n"
        f"Votes per 1M tokens: {votes_per_million}\n"
        f"Vote duration: {vote_duration} seconds ({time_str})\n"
        f"Votes per exempt user: {votes_per_exempt}\n\n"
        f"*How voting weight is calculated:*\n"
        f"• Token votes: (Total tokens ÷ 1,000,000) × {votes_per_million}\n"
        f"• NFT votes: (NFT count) × {votes_per_nft}\n"
        f"• Exempt users: {votes_per_exempt} votes\n"
        f"• Total voting power = Token votes + NFT votes (or exempt votes)"
    )
    bot.send_message(target_chat_id, settings_report, parse_mode="Markdown")

def process_create_poll(message, chat_id):
    try:
        user_id = message.from_user.id

        # Get the stored poll creation context
        context = poll_creation_context.get(user_id)
        message_thread_id = None

        if context:
            # Clean up expired contexts (older than 10 minutes)
            if time.time() - context['timestamp'] < 600:
                message_thread_id = context.get('message_thread_id')
            # Remove the context after use
            del poll_creation_context[user_id]

        lines = message.text.strip().split('\n')
        title = None
        options = []

        for line in lines:
            line = line.strip()
            if line.lower().startswith('title:'):
                title = line[6:].strip()
            elif line.lower().startswith('option'):
                # Extract option text after colon
                colon_index = line.find(':')
                if colon_index != -1:
                    option_text = line[colon_index+1:].strip()
                    if option_text:
                        options.append(option_text)

        if not title:
            bot.reply_to(message, "❌ Please include a title. Format: `Title: Your question`", parse_mode="Markdown")
            return

        if len(options) < 2:
            bot.reply_to(message, "❌ Please include at least 2 options. Format: `Option 1: Choice one`", parse_mode="Markdown")
            return

        if len(options) > 10:
            bot.reply_to(message, "❌ Maximum 10 options allowed.")
            return

        create_weighted_poll(chat_id, message.from_user.id, title, options, message_thread_id)

    except Exception as e:
        logging.error(f"Error creating poll: {e}")
        bot.reply_to(message, "❌ Error creating poll. Please check your format and try again.")

@db_retry
def create_weighted_poll(chat_id, creator_id, title, options, message_thread_id=None):
    try:
        import uuid
        poll_id = str(uuid.uuid4())[:8]

        # Create poll message with inline keyboard
        markup = types.InlineKeyboardMarkup()
        for i, option in enumerate(options):
            btn = types.InlineKeyboardButton(f"{option} (0 votes)", callback_data=f"poll_vote_{poll_id}_{i}")
            markup.add(btn)

        poll_text = (
            f"🗳️ *{title}*\n\n"
            "_Votes are weighted by token and NFT holdings. Voting power is "
            "snapshotted when you first vote._"
        )

        # Send message with topic thread preservation if applicable
        if message_thread_id:
            sent_message = bot.send_message(chat_id, poll_text, reply_markup=markup, parse_mode="Markdown", message_thread_id=message_thread_id)
        else:
            sent_message = bot.send_message(chat_id, poll_text, reply_markup=markup, parse_mode="Markdown")

        # Save poll to database
        with get_db_cursor() as (conn, cur):
            options_json = json.dumps(options)
            cur.execute("""
                INSERT INTO voting_polls (poll_id, group_id, creator_id, title, options, message_id, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
            """, (poll_id, chat_id, creator_id, title, options_json, sent_message.message_id))

        logging.info(f"Created poll {poll_id} in group {chat_id}")

    except Exception as e:
        logging.error(f"Error creating weighted poll: {e}")
        bot.send_message(chat_id, "❌ Failed to create poll. Please try again.")

@db_retry
def handle_poll_vote(call, poll_id, option_index):
    try:
        chat_id = call.message.chat.id
        user_id = call.from_user.id

        # Check if poll exists and is active
        with get_db_cursor() as (conn, cur):
            cur.execute("SELECT title, options, group_id, created_at FROM voting_polls WHERE poll_id=%s AND is_active=TRUE", (poll_id,))
            poll_data = cur.fetchone()

            if not poll_data:
                bot.answer_callback_query(call.id, "❌ Poll not found or no longer active")
                return

            title, options_json, group_id, created_at = poll_data
            options = json.loads(options_json)

            if option_index >= len(options):
                bot.answer_callback_query(call.id, "❌ Invalid option")
                return

            # Check if poll has expired
            with config_lock:
                config = SUBSCRIBER_CONFIGS.get(group_id, {})
            vote_duration = config.get("vote_duration", 3600)

            if created_at:
                # Convert created_at to datetime if it's a string
                if isinstance(created_at, str):
                    created_at = datetime.datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                elif hasattr(created_at, 'timestamp'):
                    # It's already a datetime object
                    pass
                else:
                    # Fallback - treat as current time if parsing fails
                    created_at = datetime.datetime.now()

                elapsed = (datetime.datetime.now(datetime.timezone.utc) - created_at.astimezone(datetime.timezone.utc)).total_seconds()
                if elapsed > vote_duration:
                    # Mark poll as inactive
                    cur.execute("UPDATE voting_polls SET is_active=FALSE WHERE poll_id=%s", (poll_id,))
                    bot.answer_callback_query(call.id, "❌ This poll has expired")
                    return

        # A user's first authoritative weight in a poll is the immutable
        # snapshot for that poll. Changing options never re-weights the vote.
        with get_db_cursor() as (conn, cur):
            cur.execute(
                "SELECT vote_weight FROM poll_votes WHERE poll_id=%s AND user_id=%s",
                (poll_id, user_id),
            )
            existing_vote = cur.fetchone()

        holdings_snapshot = {}
        if existing_vote:
            vote_weight = Decimal(str(existing_vote[0]))
        else:
            try:
                vote_weight, holdings_snapshot = calculate_user_vote_weight(
                    group_id,
                    user_id,
                )
            except HoldingsUnavailableError as exc:
                runtime_metrics.increment("vote_holdings_unavailable")
                logging.warning(
                    "Vote weight unavailable for user %s in group %s: %s",
                    user_id,
                    group_id,
                    exc,
                )
                bot.answer_callback_query(
                    call.id,
                    "⚠️ On-chain holdings are temporarily unavailable; your vote was not recorded. Please retry.",
                )
                return

        if vote_weight <= 0:
            bot.answer_callback_query(call.id, "❌ You need registered tokens/NFTs to vote or be exempt")
            return

        # Record or update vote
        with get_db_cursor() as (conn, cur):
            cur.execute("""
                INSERT INTO poll_votes (
                    poll_id, user_id, option_index, vote_weight,
                    weight_checked_at, holdings_snapshot
                )
                VALUES (%s, %s, %s, %s, NOW(), %s::jsonb)
                ON CONFLICT (poll_id, user_id) DO UPDATE SET
                    option_index=EXCLUDED.option_index
                RETURNING vote_weight
            """, (
                poll_id,
                user_id,
                option_index,
                vote_weight,
                json.dumps(holdings_snapshot, default=str),
            ))
            vote_weight = Decimal(str(cur.fetchone()[0]))

        # Update poll display
        update_poll_display(call.message, poll_id, title, options)

        bot.answer_callback_query(call.id, f"✅ Vote recorded! Your voting power: {vote_weight:.2f}")

    except Exception as e:
        logging.error(f"Error handling poll vote: {e}")
        bot.answer_callback_query(call.id, "❌ Error recording vote")

@db_retry
def calculate_user_vote_weight(group_id, user_id):
    with config_lock:
        config = SUBSCRIBER_CONFIGS.get(group_id, {})
    token = config.get("token", "")
    decimals = config.get("decimals", 6)
    nft_collection_id = config.get("nft_collection_id", "")
    votes_per_nft = Decimal(str(config.get("votes_per_nft", 1)))
    votes_per_million = Decimal(
        str(config.get("votes_per_million_tokens", 1))
    )
    votes_per_exempt = Decimal(str(config.get("votes_per_exempt", 1)))

    user_reg = get_user_registration(group_id, user_id)
    if not user_reg:
        return Decimal(0), {"registration": "missing"}
    if user_reg["is_exempt"]:
        return votes_per_exempt, {"registration": "exempt"}

    wallets = user_reg["wallets"]
    if not wallets:
        return Decimal(0), {"registration": "no_wallets"}

    wallet_addresses = [w.lower() for w in wallets]
    operation_deadline = time.monotonic() + SUI_OPERATION_TIMEOUT_SECONDS
    total_weight = Decimal(0)
    snapshot = {
        "wallet_count": len(wallet_addresses),
        "token_balance": None,
        "nft_count": None,
    }

    if token and votes_per_million > 0:
        balances = fetch_wallet_balances(
            wallet_addresses,
            token,
            decimals,
            use_cache=False,
            deadline_monotonic=operation_deadline,
        )
        if any(balances.get(address) is None for address in wallet_addresses):
            raise HoldingsUnavailableError("token balance provider failed")
        total_tokens = sum(
            (balances[address] for address in wallet_addresses),
            Decimal(0),
        )
        snapshot["token_balance"] = str(total_tokens)
        total_weight += (
            total_tokens / Decimal(1_000_000)
        ) * votes_per_million

    if nft_collection_id and votes_per_nft > 0:
        nft_count = get_user_nft_count(
            wallet_addresses,
            nft_collection_id,
            use_cache=False,
            deadline_monotonic=operation_deadline,
        )
        if nft_count is None:
            raise HoldingsUnavailableError("NFT ownership provider failed")
        snapshot["nft_count"] = nft_count
        total_weight += Decimal(nft_count) * votes_per_nft

    return total_weight, snapshot

def _normalize_collection_id(raw_id: str) -> str:
    """Normalise a collection identifier to a canonical on-chain form.

    Accepted inputs
    ---------------
    * Full SUI type string   ``0xPACKAGE::module::Struct``  → address portion
      lowercased / zero-padded for canonical Sui matching.
    * SUI hex address         ``0xABCD…``                   → lowercased,
      zero-padded to 64 hex characters.
    """
    cid = (raw_id or "").strip()
    if not cid:
        return ""

    # Full type string – normalise the address portion (before first ::)
    if "::" in cid:
        addr_part, rest = cid.split("::", 1)
        addr_part = addr_part.strip()
        if addr_part.startswith("0x") or addr_part.startswith("0X"):
            hex_part = addr_part[2:]
            if hex_part and all(c in "0123456789abcdefABCDEF" for c in hex_part):
                addr_part = "0x" + hex_part.lower().zfill(64)
        return addr_part + "::" + rest

    # Plain hex address
    if cid.startswith("0x") or cid.startswith("0X"):
        hex_part = cid[2:]
        if hex_part and all(c in "0123456789abcdefABCDEF" for c in hex_part):
            return "0x" + hex_part.lower().zfill(64)

    # Fallback: return as-is (will be used for substring matching)
    return cid


def _graphql_type_filter(collection_id: str) -> str | None:
    """Return a GraphQL type/package prefix filter when the hint is canonical."""
    cid = (collection_id or "").strip()
    if "::" in cid:
        return cid
    if (
        cid.startswith("0x")
        and len(cid) > 2
        and all(char in "0123456789abcdefABCDEF" for char in cid[2:])
    ):
        return cid
    return None


_KIOSK_OWNER_CAP_TYPE = (
    "0x0000000000000000000000000000000000000000000000000000000000000002"
    "::kiosk::KioskOwnerCap"
)

# Suffix used to detect Personal Kiosk Caps, which wrap a KioskOwnerCap
# inside a soulbound object.  The package address varies across deployments
# (Mysten's official extension, OriginByte's ob_kiosk, etc.), so we match
# by suffix rather than exact type to stay robust against upgrades.
_PERSONAL_KIOSK_CAP_SUFFIX = "::personal_kiosk::PersonalKioskCap"


def _extract_kiosk_id_from_personal_cap(obj: dict) -> str | None:
    """Extract the Kiosk object ID from a ``PersonalKioskCap`` object.

    ``PersonalKioskCap`` wraps a ``KioskOwnerCap`` which has a ``for`` field
    pointing to the Kiosk ID.  Different implementations store the cap
    differently:

    * **Mysten extension** – ``cap`` is a direct ``KioskOwnerCap`` value:
      ``content.fields.cap.fields.for``
    * **OriginByte ob_kiosk** – ``cap`` is ``Option<KioskOwnerCap>``
      (serialised as a Move ``vector``):
      ``content.fields.cap.fields.vec[0].fields.for``

    Returns the kiosk ID string or ``None`` if extraction fails.
    """
    content = obj.get("content") or {}
    fields = content.get("fields") or {}
    cap = fields.get("cap")
    if not isinstance(cap, dict):
        return None

    cap_fields = cap.get("fields") or cap

    # Direct KioskOwnerCap (Mysten extension style)
    kiosk_id = cap_fields.get("for")
    if kiosk_id:
        return kiosk_id

    # Option<KioskOwnerCap> (OriginByte style) – unwrap the vector
    vec = cap_fields.get("vec")
    if isinstance(vec, list) and len(vec) > 0:
        inner = vec[0]
        if isinstance(inner, dict):
            inner_fields = inner.get("fields") or inner
            kiosk_id = inner_fields.get("for")
            if kiosk_id:
                return kiosk_id

    # GraphQL returns Move JSON without the JSON-RPC ``fields`` wrappers.
    def find_for(value):
        if isinstance(value, dict):
            for key, nested in value.items():
                if key == "for" and isinstance(nested, str):
                    return nested
                result = find_for(nested)
                if result:
                    return result
        elif isinstance(value, list):
            for nested in value:
                result = find_for(nested)
                if result:
                    return result
        return None

    return find_for(cap_fields)


def _fetch_personal_kiosk_ids(owner: str, *, deadline_monotonic=None) -> list[str]:
    """Discover personal Kiosks in one paginated GraphQL owned-object scan."""
    kiosk_ids = []
    for obj in sui_gateway.iter_owned_objects(
        owner,
        max_pages=SUI_MAX_PAGES,
        max_items=SUI_MAX_OBJECTS,
        deadline_monotonic=deadline_monotonic,
    ):
        if (obj.get("type") or "").endswith(_PERSONAL_KIOSK_CAP_SUFFIX):
            kiosk_id = _extract_kiosk_id_from_personal_cap(obj)
            if kiosk_id:
                kiosk_ids.append(kiosk_id)
    return kiosk_ids


def _fetch_kiosk_nfts(addresses, collection_id, *, deadline_monotonic=None):
    """Fetch collection items held in standard and personal Sui Kiosks."""
    normalized = _normalize_collection_id(collection_id)
    if not normalized:
        return []

    hint_lower = normalized.lower()

    results = []
    for owner in [a.lower() for a in addresses if a]:
        kiosk_ids = []
        seen_kiosk_ids = set()
        for obj in sui_gateway.iter_owned_objects(
            owner,
            _KIOSK_OWNER_CAP_TYPE,
            max_pages=SUI_MAX_PAGES,
            max_items=SUI_MAX_OBJECTS,
            deadline_monotonic=deadline_monotonic,
        ):
            fields = (obj.get("content") or {}).get("fields") or {}
            kiosk_id = fields.get("for")
            if kiosk_id and kiosk_id not in seen_kiosk_ids:
                seen_kiosk_ids.add(kiosk_id)
                kiosk_ids.append(kiosk_id)

        for kiosk_id in _fetch_personal_kiosk_ids(
            owner,
            deadline_monotonic=deadline_monotonic,
        ):
            if kiosk_id not in seen_kiosk_ids:
                seen_kiosk_ids.add(kiosk_id)
                kiosk_ids.append(kiosk_id)

        for kiosk_id in kiosk_ids:
            for field in sui_gateway.iter_dynamic_fields(
                kiosk_id,
                max_pages=SUI_MAX_PAGES,
                max_items=SUI_MAX_OBJECTS,
                deadline_monotonic=deadline_monotonic,
            ):
                name_info = field.get("name") or {}
                name_type = ((name_info.get("type") or {}).get("repr") or "").lower()
                if "kiosk::item" not in name_type:
                    continue
                value = field.get("value") or {}
                contents = value.get("contents") or {}
                type_info = contents.get("type") or value.get("type") or {}
                object_type = (type_info.get("repr") or "").lower()
                if not object_type or hint_lower not in object_type:
                    continue
                nft_entry = {
                    "objectId": value.get("address") or "",
                    "type": type_info.get("repr") or "",
                    "content": {
                        "dataType": "moveObject",
                        "type": type_info.get("repr") or "",
                        "fields": contents.get("json") or value.get("json") or {},
                    },
                }
                if not nft_entry["objectId"]:
                    name_json = name_info.get("json") or {}
                    if isinstance(name_json, dict):
                        nft_entry["objectId"] = name_json.get("id") or ""
                results.append(nft_entry)

    return results


def _fetch_owned_nfts(addresses, collection_id, *, deadline_monotonic=None):
    """Fetch NFT objects owned by *addresses* that belong to *collection_id*.

    Returns normalized object dictionaries with Move JSON content.

    Checks both directly-owned objects **and** items held inside SUI Kiosks.
    """
    normalized = _normalize_collection_id(collection_id)
    type_filter = _graphql_type_filter(normalized)

    # Fallback client-side matching when no GraphQL type filter is available
    hint_lower = normalized.lower() if normalized else ""

    def matches(obj):
        otype = (obj.get("type") or "").lower()
        oid = (obj.get("objectId") or "").lower()
        if not otype or "::" not in otype:
            return False
        if "coin::" in otype:
            return False
        if not hint_lower:
            return True
        if hint_lower == oid:
            return True
        if otype.startswith(hint_lower + "::"):
            return True
        if "::" in hint_lower and otype.startswith(hint_lower):
            return True
        return hint_lower in otype

    results = []
    seen_ids = set()
    for owner in [a.lower() for a in addresses if a]:
        for obj in sui_gateway.iter_owned_objects(
            owner,
            type_filter,
            max_pages=SUI_MAX_PAGES,
            max_items=SUI_MAX_OBJECTS,
            deadline_monotonic=deadline_monotonic,
        ):
            oid = obj.get("objectId", "")
            if oid in seen_ids:
                continue
            if matches(obj):
                seen_ids.add(oid)
                results.append(obj)

    # Also check NFTs held inside SUI Kiosks
    try:
        kiosk_nfts = _fetch_kiosk_nfts(
            addresses,
            collection_id,
            deadline_monotonic=deadline_monotonic,
        )
        for obj in kiosk_nfts:
            oid = obj.get("objectId", "")
            if oid and oid not in seen_ids:
                seen_ids.add(oid)
                results.append(obj)
    except Exception as e:
        logging.error(f"Error fetching kiosk NFTs for collection {collection_id}: {e}")
        # Any Kiosk failure makes the aggregate count incomplete.
        raise

    return results


def get_user_nft_count(
    addresses,
    collection_id,
    use_cache=True,
    cache_ttl=None,
    deadline_monotonic=None,
):
    """Count NFTs for addresses via on-chain Sui owned-object queries.

    Returns the integer count on success or ``None`` when the on-chain
    lookup fails (so callers can distinguish "0 NFTs" from a provider error).
    """
    current_time = time.time()
    normalized_addresses = [addr.lower() for addr in addresses if addr]
    collection_hint = _normalize_collection_id(collection_id).lower()
    cache_key = (tuple(sorted(normalized_addresses)), collection_hint)

    effective_cache_ttl = NFT_CACHE_TTL if cache_ttl is None else cache_ttl

    with cache_lock:
        if use_cache and cache_key in nft_cache:
            cache_time, cache_result = nft_cache[cache_key]
            if current_time - cache_time < effective_cache_ttl:
                # cache_result is a list of NFT dict objects
                return len(cache_result)

    try:
        nfts = _fetch_owned_nfts(
            normalized_addresses,
            collection_id,
            deadline_monotonic=deadline_monotonic,
        )
        with cache_lock:
            if len(nft_cache) >= MAX_CACHE_SIZE:
                sorted_keys = sorted(nft_cache.keys(), key=lambda k: nft_cache[k][0])
                for old_key in sorted_keys[:MAX_CACHE_SIZE // 4]:
                    del nft_cache[old_key]

            nft_cache[cache_key] = (current_time, nfts)
        return len(nfts)

    except Exception as e:
        logging.error(f"Error getting on-chain NFT count: {e}")
        return None


def check_nft_ownership(addresses, collection_id, threshold):
    total_nft_count = get_user_nft_count(addresses, collection_id)
    if total_nft_count is None:
        return None
    return total_nft_count >= threshold


# ── NFT trait helpers ──────────────────────────────────────────────


def _extract_traits(obj: dict) -> dict:
    """Extract trait key/value pairs from an NFT object's content.

    Supports the most common SUI NFT attribute layouts:
    1. ``VecMap<String, String>`` in an ``attributes`` field
    2. Nested OriginByte-style ``attributes.fields.map.fields.contents``
    3. Flat struct fields (anything that is a plain string value)
    """
    traits: dict = {}

    content = obj.get("content") or {}
    fields = content.get("fields") or {}

    # Also check Display data for supplementary trait info
    display = obj.get("display") or {}
    display_data = display.get("data") or {}

    # Strategy 1: VecMap / list-of-key-value in ``attributes``
    attrs = fields.get("attributes") or {}
    if isinstance(attrs, dict):
        # GraphQL Move JSON omits the JSON-RPC ``fields`` wrappers, while
        # older cached values may still contain them. Support both shapes.
        inner_fields = attrs.get("fields") or attrs
        contents = inner_fields.get("contents") or []
        if not contents:
            # OriginByte style: attributes -> fields -> map -> fields -> contents
            map_field = inner_fields.get("map") or {}
            map_inner = map_field.get("fields") or map_field
            contents = map_inner.get("contents") or []
        for entry in contents:
            ef = (entry.get("fields") or entry) if isinstance(entry, dict) else {}
            key = ef.get("key") or ""
            val = ef.get("value") or ""
            # value might be a nested object with a ``value`` field
            if isinstance(val, dict):
                val = (val.get("fields") or {}).get("value", "") or val.get("value", "")
            if isinstance(key, str) and isinstance(val, str) and key:
                traits[key.lower()] = val.lower()
        # Some collections expose attributes as a direct JSON object.
        if not contents:
            for key, value in attrs.items():
                if key in ("fields", "contents", "map"):
                    continue
                if isinstance(key, str) and isinstance(value, str):
                    traits[key.lower()] = value.lower()
    elif isinstance(attrs, list):
        for entry in attrs:
            if not isinstance(entry, dict):
                continue
            key = entry.get("key")
            value = entry.get("value")
            if isinstance(key, str) and isinstance(value, str):
                traits[key.lower()] = value.lower()

    # Strategy 2: flat struct fields that are plain strings
    for k, v in fields.items():
        if k in ("id", "name", "url", "img_url", "image_url", "description", "attributes"):
            continue
        if isinstance(v, str):
            traits.setdefault(k.lower(), v.lower())

    # Strategy 3: Display data may contain trait-like fields
    for k, v in display_data.items():
        if k in ("name", "image_url", "link", "project_url", "description", "creator"):
            continue
        if isinstance(v, str):
            traits.setdefault(k.lower(), v.lower())

    return traits


def get_user_nft_trait_count(
    wallet_addresses,
    collection_id,
    trait_name,
    trait_value,
    use_cache=True,
    cache_ttl=None,
    deadline_monotonic=None,
):
    """Count NFTs owned by *wallet_addresses* in *collection_id* whose
    *trait_name* equals *trait_value*.  Returns ``None`` on error."""
    current_time = time.time()
    normalized_addresses = [addr.lower() for addr in wallet_addresses if addr]
    collection_hint = _normalize_collection_id(collection_id).lower()
    cache_key = (tuple(sorted(normalized_addresses)), collection_hint)
    effective_cache_ttl = NFT_CACHE_TTL if cache_ttl is None else cache_ttl

    nfts = None
    with cache_lock:
        if use_cache and cache_key in nft_cache:
            cache_time, cache_result = nft_cache[cache_key]
            if current_time - cache_time < effective_cache_ttl:
                nfts = cache_result

    if nfts is None:
        try:
            nfts = _fetch_owned_nfts(
                normalized_addresses,
                collection_id,
                deadline_monotonic=deadline_monotonic,
            )
            with cache_lock:
                if len(nft_cache) >= MAX_CACHE_SIZE:
                    sorted_keys = sorted(nft_cache.keys(), key=lambda k: nft_cache[k][0])
                    for old_key in sorted_keys[:MAX_CACHE_SIZE // 4]:
                        del nft_cache[old_key]
                nft_cache[cache_key] = (current_time, nfts)
        except Exception as e:
            logging.error(f"Error in get_user_nft_trait_count fetching NFTs: {e}")
            return None

    try:
        target_key = trait_name.strip().lower()
        target_val = trait_value.strip().lower()
        count = 0
        for obj in nfts:
            traits = _extract_traits(obj)
            if traits.get(target_key) == target_val:
                count += 1
        return count
    except Exception as e:
        logging.error(f"Error in get_user_nft_trait_count processing traits: {e}")
        return None


def get_user_nft_category_count(
    wallet_addresses,
    collection_id,
    trait_name,
    use_cache=True,
    cache_ttl=None,
    deadline_monotonic=None,
):
    """Count NFTs owned by *wallet_addresses* in *collection_id* that have
    any value for *trait_name*.  Returns ``None`` on error."""
    current_time = time.time()
    normalized_addresses = [addr.lower() for addr in wallet_addresses if addr]
    collection_hint = _normalize_collection_id(collection_id).lower()
    cache_key = (tuple(sorted(normalized_addresses)), collection_hint)
    effective_cache_ttl = NFT_CACHE_TTL if cache_ttl is None else cache_ttl

    nfts = None
    with cache_lock:
        if use_cache and cache_key in nft_cache:
            cache_time, cache_result = nft_cache[cache_key]
            if current_time - cache_time < effective_cache_ttl:
                nfts = cache_result

    if nfts is None:
        try:
            nfts = _fetch_owned_nfts(
                normalized_addresses,
                collection_id,
                deadline_monotonic=deadline_monotonic,
            )
            with cache_lock:
                if len(nft_cache) >= MAX_CACHE_SIZE:
                    sorted_keys = sorted(nft_cache.keys(), key=lambda k: nft_cache[k][0])
                    for old_key in sorted_keys[:MAX_CACHE_SIZE // 4]:
                        del nft_cache[old_key]
                nft_cache[cache_key] = (current_time, nfts)
        except Exception as e:
            logging.error(f"Error in get_user_nft_category_count fetching NFTs: {e}")
            return None

    try:
        target_key = trait_name.strip().lower()
        count = 0
        for obj in nfts:
            traits = _extract_traits(obj)
            if target_key in traits:
                count += 1
        return count
    except Exception as e:
        logging.error(f"Error in get_user_nft_category_count processing categories: {e}")
        return None


def evaluate_wallet_requirements(
    wallet_address,
    cfg,
    user_id=None,
    force_fresh=False,
    deadline_monotonic=None,
):
    """Evaluate requirements as PASS, FAIL, or INDETERMINATE."""
    operation_deadline = (
        deadline_monotonic
        if deadline_monotonic is not None
        else time.monotonic() + SUI_OPERATION_TIMEOUT_SECONDS
    )
    wallet_lower = wallet_address.lower()
    registration_mode = cfg.get("registration_mode", "token")
    token = cfg.get("token", "")
    decimals = cfg.get("decimals", 6)
    try:
        minimum_holding = Decimal(str(cfg.get("minimum_holding", 0)))
    except (InvalidOperation, TypeError, ValueError):
        minimum_holding = Decimal(0)
    nft_collection_id = cfg.get("nft_collection_id", "")
    nft_threshold = cfg.get("nft_threshold", 1)
    nft_trait_name = cfg.get("nft_trait_name", "")
    nft_trait_value = cfg.get("nft_trait_value", "")
    nft_trait_threshold = cfg.get("nft_trait_threshold", 1)

    details = []
    errors = []

    token_valid = False
    nft_valid = False
    trait_valid = True
    token_indeterminate = False
    nft_indeterminate = False
    trait_indeterminate = False

    token_balance = None
    # When force_fresh is True (interactive verification), bypass the in-memory
    # cache entirely so GraphQL is always queried with live data. Otherwise use
    # the normal cache with default TTL.
    use_cache_flag = not force_fresh
    if registration_mode in ["token", "both"] and token:
        balances = fetch_wallet_balances(
            [wallet_lower],
            token,
            decimals,
            use_cache=use_cache_flag,
            deadline_monotonic=operation_deadline,
        )
        token_balance = balances.get(wallet_lower)
        if token_balance is None:
            token_indeterminate = True
            errors.append("⚠️ Unable to verify token balance right now. Please retry in a moment.")
        else:
            token_valid = token_balance >= minimum_holding
            details.append(f"*Token Balance:* {token_balance:,.2f} {'✓' if token_valid else '✗'} (threshold: {minimum_holding:,.2f})")

    nft_count = None
    trait_count = None
    trait_api_failed = False

    if registration_mode in ["nft", "both"] and nft_collection_id:
        nft_count = get_user_nft_count(
            [wallet_lower],
            nft_collection_id,
            use_cache=use_cache_flag,
            deadline_monotonic=operation_deadline,
        )
        # Retry once on provider failure during interactive verification –
        # transient errors (rate limits, kiosk fetch timeouts) can cause
        # false negatives when the user's NFTs are inside SUI Kiosks.
        if nft_count is None and force_fresh:
            time.sleep(NFT_PROVIDER_RETRY_DELAY)
            logging.info(f"Retrying NFT count for wallet {wallet_lower} after initial provider failure")
            nft_count = get_user_nft_count(
                [wallet_lower],
                nft_collection_id,
                use_cache=False,
                deadline_monotonic=operation_deadline,
            )
        if nft_count is None:
            nft_indeterminate = True
            errors.append("⚠️ Unable to verify NFT ownership right now. Please retry in a moment.")
            details.append(f"*NFTs in Collection:* ⚠️ check failed (threshold: {nft_threshold})")
        else:
            nft_valid = nft_count >= nft_threshold
            details.append(f"*NFTs in Collection:* {nft_count} {'✓' if nft_valid else '✗'} (threshold: {nft_threshold})")

        if nft_trait_name and nft_valid:
            try:
                if nft_trait_value:
                    trait_count = get_user_nft_trait_count(
                        [wallet_lower],
                        nft_collection_id,
                        nft_trait_name,
                        nft_trait_value,
                        use_cache=use_cache_flag,
                        deadline_monotonic=operation_deadline,
                    )
                    trait_desc = f"{nft_trait_name} = {nft_trait_value}"
                else:
                    trait_count = get_user_nft_category_count(
                        [wallet_lower],
                        nft_collection_id,
                        nft_trait_name,
                        use_cache=use_cache_flag,
                        deadline_monotonic=operation_deadline,
                    )
                    trait_desc = f"{nft_trait_name} (any value)"

                if trait_count is None:
                    trait_api_failed = True
                    trait_valid = False
                    trait_indeterminate = True
                    errors.append("⚠️ Unable to verify NFT traits right now. Please retry in a moment.")
                    details.append(f"*Trait Verification:* ⚠️ Unavailable for `{trait_desc}`")
                else:
                    trait_valid = trait_count >= nft_trait_threshold
                    details.append(f"*Trait Verification:* {trait_count} {'✓' if trait_valid else '✗'} for `{trait_desc}` (threshold: {nft_trait_threshold})")
            except Exception as trait_e:
                trait_api_failed = True
                trait_valid = False
                trait_indeterminate = True
                errors.append("⚠️ Unable to verify NFT traits right now. Please retry in a moment.")
                details.append("*Trait Verification:* ⚠️ Check failed")
                logging.warning(f"Trait check failed for user {user_id}: {trait_e}")

    status = evaluate_gate(
        registration_mode,
        token_valid=token_valid,
        nft_valid=nft_valid,
        trait_valid=trait_valid,
        token_indeterminate=token_indeterminate,
        nft_indeterminate=nft_indeterminate,
        trait_indeterminate=trait_indeterminate,
    ).value

    requirements_met = status == "pass"

    if not requirements_met and not errors:
        if registration_mode in ["token", "both"] and token and token_balance is not None and token_balance < minimum_holding:
            errors.append(f"💰 Token balance below threshold ({token_balance:,.2f} / {minimum_holding:,.2f}).")
        if registration_mode in ["nft", "both"] and nft_collection_id and nft_count is not None and nft_count < nft_threshold:
            errors.append(f"🖼️ NFT count below threshold ({nft_count} / {nft_threshold}).")
        if registration_mode in ["nft", "both"] and nft_trait_name and not trait_api_failed and trait_count is not None and trait_count < nft_trait_threshold:
            errors.append(f"🎨 NFT trait count below threshold ({trait_count} / {nft_trait_threshold}).")

    return {
        "requirements_met": requirements_met,
        "status": status,
        "details": details,
        "errors": errors,
        "rpc_failed": status == "indeterminate",
        "nft_count": nft_count,
        "trait_count": trait_count,
        "token_balance": token_balance,
    }

@db_retry
def update_poll_display(message, poll_id, title, options):
    try:
        with get_db_cursor() as (conn, cur):
            # Check poll status and expiration
            cur.execute("SELECT created_at, is_active, group_id FROM voting_polls WHERE poll_id=%s", (poll_id,))
            poll_info = cur.fetchone()

            if not poll_info:
                return

            created_at, is_active, group_id = poll_info
            with config_lock:
                config = SUBSCRIBER_CONFIGS.get(group_id, {})
            vote_duration = config.get("vote_duration", 3600)

            # Check if poll should be expired
            if created_at and is_active:
                if isinstance(created_at, str):
                    created_at = datetime.datetime.fromisoformat(created_at.replace('Z', '+00:00'))

                elapsed = (datetime.datetime.now(datetime.timezone.utc) - created_at.astimezone(datetime.timezone.utc)).total_seconds()
                if elapsed > vote_duration:
                    # Mark poll as expired
                    cur.execute("UPDATE voting_polls SET is_active=FALSE WHERE poll_id=%s", (poll_id,))
                    is_active = False

            # Get vote counts for each option
            cur.execute("""
                SELECT option_index, SUM(vote_weight)
                FROM poll_votes 
                WHERE poll_id=%s 
                GROUP BY option_index
            """, (poll_id,))
            vote_results = dict(cur.fetchall())

        # Update markup with vote counts or show results if expired
        if is_active:
            markup = types.InlineKeyboardMarkup()
            for i, option in enumerate(options):
                vote_count = vote_results.get(i, 0)
                btn_text = f"{option} ({vote_count:.1f} votes)"
                btn = types.InlineKeyboardButton(btn_text, callback_data=f"poll_vote_{poll_id}_{i}")
                markup.add(btn)

            poll_text = f"🗳️ *{title}*\n\n_Votes are weighted by token and NFT holdings_"
        else:
                # Poll has expired - show final results
            markup = None
            results_text = []
            total_votes = sum(vote_results.values())

                    # BUG FIX: This logic must be INSIDE the else block
            for i, option in enumerate(options):
                vote_count = vote_results.get(i, 0)
                percentage = (vote_count / total_votes * 100) if total_votes > 0 else 0               
                results_text.append(f"• {option}: {vote_count:.1f} votes ({percentage:.1f}%)")

            poll_text = f"🏁 *Poll Ended: {title}*\n\n" + "\n".join(results_text) + f"\n\n_Total votes: {total_votes:.1f}_"

        bot.edit_message_text(
            poll_text,
            chat_id=message.chat.id,
            message_id=message.message_id,
            reply_markup=markup,
            parse_mode="Markdown"
        )

    except Exception as e:
        logging.error(f"Error updating poll display: {e}")

@db_retry
def display_exemption_manager(group_id, send_to_chat_id):
    """Displays the exemption manager in a private chat."""
    try:
        with get_db_cursor() as (conn, cur):
            # Fetch all registered users for the group
            cur.execute("""
                SELECT user_id, username, is_exempt 
                FROM user_wallets 
                WHERE group_id=%s
                ORDER BY username
            """, (group_id,))
            all_users = cur.fetchall()

        if not all_users:
            bot.send_message(send_to_chat_id, "*Exemption Manager*\n\nNo registered users found in this group.", parse_mode="Markdown")
            return

        # Build the message and keyboard
        message_lines = [
            "*Exemption Manager*",
            f"Total Registered Users: {len(all_users)}",
            "",
            "Click a user to toggle their exemption status:"
        ]

        markup = types.InlineKeyboardMarkup(row_width=1)

        for user_id, username, is_exempt in all_users:
            display_name = username or f"User ID: {user_id}"
            emoji = "✅" if is_exempt else "❌"
            btn_text = f"{emoji} {display_name}"
            # The callback data now includes the group_id, a specific action, and the user_id
            callback_data = f"privconfig_{group_id}_toggleexempt_{user_id}"
            markup.add(types.InlineKeyboardButton(btn_text, callback_data=callback_data))

        # Add a back button to return to the main config menu
        markup.add(types.InlineKeyboardButton("⬅️ Back to Config", callback_data=f"privconfig_{group_id}_back"))

        message = "\n".join(message_lines)
        bot.send_message(send_to_chat_id, message, reply_markup=markup, parse_mode="Markdown")

    except Exception as e:
        logging.error(f"Error in display_exemption_manager for group {group_id}: {e}")
        bot.send_message(send_to_chat_id, "❌ Error loading the exemption manager. Please try again.")

@bot.message_handler(commands=['addwallet'])
@admin_required
def add_wallet_command(message):
    try:
        command_parts = message.text.split()
        usage_text = (
            "❌ Usage:\n"
            "• Reply to a user's message with `/addwallet <wallet_address>`\n"
            "• Or run `/addwallet <user_id> <wallet_address>`"
        )

        target_user = None
        wallet_address = ""

        if message.reply_to_message:
            if len(command_parts) < 2:
                bot.reply_to(message, usage_text)
                return
            wallet_address = command_parts[1].strip()
            target_user = message.reply_to_message.from_user
        else:
            if len(command_parts) < 3:
                bot.reply_to(message, usage_text)
                return
            try:
                target_user_id = int(command_parts[1].strip())
            except ValueError:
                bot.reply_to(message, "❌ Invalid user ID format. Please provide a valid integer user ID.")
                return
            if target_user_id <= 0:
                bot.reply_to(message, "❌ Invalid user ID. Please provide a positive Telegram user ID.")
                return

            wallet_address = command_parts[2].strip()
            try:
                target_member = bot.get_chat_member(message.chat.id, target_user_id)
                if target_member.status not in ACTIVE_GROUP_MEMBER_STATUSES:
                    bot.reply_to(
                        message,
                        INACTIVE_MEMBER_STATUS_MESSAGES.get(
                            target_member.status,
                            "❌ That user is not currently a member of this group."
                        )
                    )
                    return
                target_user = target_member.user
            except telebot.apihelper.ApiTelegramException as e:
                logging.error(f"Error fetching target user {target_user_id} for addwallet: {e}")
                bot.reply_to(message, "❌ Could not find that user in this group. Please confirm the user ID.")
                return
            except Exception as e:
                logging.error(f"Telegram lookup failed for target user {target_user_id} in addwallet: {e}")
                bot.reply_to(message, "❌ Could not look up that user right now. Please try again.")
                return

        chat_id = message.chat.id
        target_user_name = get_telegram_user_display_name(target_user)

        if not is_valid_wallet_address(wallet_address):
            bot.reply_to(message, f"❌ Invalid wallet address format: '{wallet_address}'. Please check and try again.")
            return

        if wallet_already_registered(wallet_address, chat_id, user_id=target_user.id):
            bot.reply_to(message, "⚠️ This wallet address is already registered to another user in this group.")
            return

        processing_msg = bot.reply_to(message, f"⏳ Adding wallet for {target_user_name}...")

        with config_lock:
            cfg = SUBSCRIBER_CONFIGS.get(chat_id)

        success = save_wallet_for_user(
            chat_id, 
            target_user.id, 
            target_user_name, 
            [wallet_address.lower()],
            replace_existing=False,
            registration_type=cfg.get("registration_mode", "token") if cfg else "token"
        )

        if success:
            wallet_count = 0
            try:
                with get_db_cursor() as (conn, cur):
                    cur.execute("SELECT wallets FROM user_wallets WHERE group_id=%s AND user_id=%s", (chat_id, target_user.id))
                    result = cur.fetchone()
                    if result and result[0]:
                        all_wallets = json.loads(result[0])
                        wallet_count = len(all_wallets)
            except Exception as e:
                logging.error(f"Error getting wallet count: {e}")

            bot.edit_message_text(
                f"✅ Successfully added wallet for {target_user_name}.\nThey now have {wallet_count} registered wallet(s).",
                chat_id=message.chat.id,
                message_id=processing_msg.message_id
            )
        else:
            bot.edit_message_text(
                f"❌ Failed to add wallet for {target_user_name}.\nPlease try again later.",
                chat_id=message.chat.id,
                message_id=processing_msg.message_id
            )
    except Exception as e:
        logging.error(f"Error in add_wallet_command: {e}")
        bot.reply_to(message, "❌ An error occurred while processing this command.")

@bot.message_handler(commands=['mywallets'])
def mywallets_command(message):
    """Show user's registered wallets with balances and management options"""
    try:
        user_id = message.from_user.id
        chat_id = message.chat.id

        # Check if this is a group or private chat
        if message.chat.type in ["group", "supergroup"]:
            # Redirect to private chat for security
            group_id = chat_id
            markup = types.InlineKeyboardMarkup()
            deep_link = f"https://t.me/{get_bot_username()}?start=mywallets_{encode_group_id_for_deeplink(group_id)}"
            mywallets_btn = types.InlineKeyboardButton("💰 View My Wallets in Private Chat", url=deep_link)
            markup.add(mywallets_btn)

            # Get the message thread ID if this is a topic
            message_thread_id = getattr(message, 'message_thread_id', None)

            # Send with topic context preserved
            if message_thread_id:
                bot.send_message(
                    message.chat.id, 
                    "💰 **My Wallets**\n\nFor security, wallet information is only shown in private chat:",
                    reply_markup=markup,
                    parse_mode="Markdown",
                    message_thread_id=message_thread_id
                )
            else:
                bot.send_message(
                    message.chat.id, 
                    "💰 **My Wallets**\n\nFor security, wallet information is only shown in private chat:",
                    reply_markup=markup,
                    parse_mode="Markdown"
                )
            return
        else:
            # For private chats, check if user has a pending verification context
            with get_db_cursor() as (conn, cur):
                cur.execute("SELECT group_id FROM pending_verifications WHERE user_id = %s", (user_id,))
                result = cur.fetchone()
            if not result:
                bot.reply_to(message, "⚠️ No group context found. Please use this command in a group where you're registered.")
                return
            group_id = result[0]

        # Use the full private wallet display which includes NFT/trait info
        show_mywallets_private(chat_id, group_id)

    except Exception as e:
        logging.error(f"Error in mywallets command: {e}")
        bot.reply_to(message, "❌ An error occurred while processing your request.")

@bot.message_handler(commands=['exempt'])
@admin_required
def exempt_command(message):
    try:
        if not message.reply_to_message:
            bot.reply_to(message, "❌ Please use this command by replying to a user's message to exempt them.")
            return

        chat_id = message.chat.id
        target_user = message.reply_to_message.from_user
        target_id = target_user.id
        target_username = target_user.username or target_user.first_name

        try:
            processing_msg = bot.reply_to(message, f"Processing exemption for @{target_username}...")
        except Exception as e:
            # If reply fails (message might be deleted), send a regular message instead
            logging.warning(f"Could not reply to message, sending regular message: {e}")
            processing_msg = bot.send_message(chat_id, f"Processing exemption for @{target_username}...")

        # Get the user's current exemption status and toggle it
        user_reg = get_user_registration(chat_id, target_id)
        current_status = user_reg["is_exempt"] if user_reg else False
        new_status = not current_status

        if toggle_user_exemption(chat_id, target_id, new_status):
            status_text = "exempted from" if new_status else "no longer exempt from"
            bot.edit_message_text(
                f"✅ User @{target_username} is now {status_text} wallet requirements.",
                chat_id=chat_id,
                message_id=processing_msg.message_id
            )
            logging.info(f"Successfully toggled exemption for user {target_id} to {new_status} in group {chat_id}")
        else:
            bot.edit_message_text(
                f"❌ Failed to update exemption for @{target_username}. Please try again.",
                chat_id=chat_id,
                message_id=processing_msg.message_id
            )

    except Exception as e:
        logging.error(f"Error in exempt command: {e}")
        bot.reply_to(message, "❌ An error occurred while processing the exemption.")

@bot.chat_member_handler()
def handle_chat_member_update(update):
    """Handle new members joining the group and clean up database registrations when members leave."""
    try:
        group_id = update.chat.id
        user_id = update.new_chat_member.user.id
        user_name = update.new_chat_member.user.first_name or f"User {user_id}"

        # Skip if it's the bot itself
        if user_id == get_bot_id():
            return

        # Check if a member has left, was kicked, or was banned
        if update.new_chat_member.status in ['left', 'kicked']:
            logging.info(f"Member {user_name} ({user_id}) left or was removed from group {group_id}. Cleaning up database registration.")
            with get_db_cursor() as (conn, cur):
                cur.execute("DELETE FROM user_wallets WHERE group_id = %s AND user_id = %s", (group_id, user_id))
                cur.execute("DELETE FROM low_balance_alerts WHERE group_id = %s AND user_id = %s", (group_id, user_id))
                cur.execute("DELETE FROM pending_verifications WHERE group_id = %s AND user_id = %s", (group_id, user_id))
            return

        # Check if this is a new member joining
        if (update.new_chat_member.status in ['member', 'administrator'] and 
            update.old_chat_member.status in ['left', 'kicked', 'restricted']):

            logging.info(f"New member {user_name} ({user_id}) joined group {group_id}")

            # Check if user needs to register
            user_reg = get_user_registration(group_id, user_id)

            # Skip if user is exempt
            if user_reg and user_reg["is_exempt"]:
                logging.info(f"User {user_id} is exempt, skipping registration reminder")
                return

            # Get group configuration to validate against requirements
            with config_lock:
                config = SUBSCRIBER_CONFIGS.get(group_id, {})

            token = config.get("token", "")
            decimals = config.get("decimals", 6)
            minimum_holding = config.get("minimum_holding", 0)
            nft_collection_id = config.get("nft_collection_id", "")
            nft_threshold = config.get("nft_threshold", 1)
            nft_trait_name = config.get("nft_trait_name", "")
            nft_trait_value = config.get("nft_trait_value", "")
            nft_trait_threshold = config.get("nft_trait_threshold", 1)
            registration_mode = config.get("registration_mode", "token")

            # A lapsed subscription disables the gate; do not prompt or
            # evaluate existing registrations until it is renewed.
            if not group_has_active_subscription(group_id):
                logging.info(f"Skipping join-time gate enforcement for expired-subscription group {group_id}.")
                return

            # If user has wallets, validate they meet current requirements
            if user_reg and user_reg["wallets"]:
                try:
                    wallets = user_reg["wallets"]
                    wallet_addresses = [w.lower() for w in wallets]

                    token_valid = False
                    nft_valid = False
                    trait_valid = True
                    nft_indeterminate = False
                    trait_indeterminate = False
                    token_indeterminate = False
                    nft_indeterminate = False
                    trait_indeterminate = False
                    if registration_mode in ["token", "both"] and token:
                        balances = fetch_wallet_balances(wallet_addresses, token, decimals)
                        known_balances = [
                            balances.get(addr)
                            for addr in wallet_addresses
                            if balances.get(addr) is not None
                        ]
                        total_balance = sum(known_balances, Decimal(0))
                        token_valid = total_balance >= Decimal(str(minimum_holding))
                        token_indeterminate = (
                            not token_valid
                            and len(known_balances) != len(wallet_addresses)
                        )

                    # Check NFT requirements if applicable
                    if registration_mode in ["nft", "both"] and nft_collection_id:
                        nft_valid = check_nft_ownership(wallet_addresses, nft_collection_id, nft_threshold)
                        if nft_valid is None:
                            nft_indeterminate = True
                            nft_valid = False
                        if nft_valid and nft_trait_name:
                            try:
                                if nft_trait_value:
                                    trait_count = get_user_nft_trait_count(
                                        wallet_addresses, nft_collection_id,
                                        nft_trait_name, nft_trait_value,
                                        use_cache=False,
                                    )
                                else:
                                    trait_count = get_user_nft_category_count(
                                        wallet_addresses, nft_collection_id,
                                        nft_trait_name,
                                        use_cache=False,
                                    )
                                # Fail open only when the chain query was
                                # unavailable.  A real below-threshold count
                                # must prompt the returning member to verify.
                                if trait_count is not None:
                                    trait_valid = trait_count >= nft_trait_threshold
                                    if not trait_valid:
                                        logging.info(
                                            f"Returning user {user_id} fails trait gate in group {group_id}: "
                                            f"{trait_count} / {nft_trait_threshold}"
                                        )
                                else:
                                    trait_valid = False
                                    trait_indeterminate = True
                            except Exception as trait_e:
                                trait_valid = False
                                trait_indeterminate = True
                                logging.warning(
                                    f"Could not evaluate trait gate for returning user {user_id}: {trait_e}"
                                )

                    nft_branch_valid = nft_valid and trait_valid
                    requirements_indeterminate = False
                    if registration_mode == "token":
                        requirements_met = token_valid
                        requirements_indeterminate = token_indeterminate
                    elif registration_mode == "nft":
                        requirements_met = nft_branch_valid
                        requirements_indeterminate = nft_indeterminate or trait_indeterminate
                    elif registration_mode == "both":
                        requirements_met = token_valid or nft_branch_valid
                        requirements_indeterminate = (
                            not requirements_met
                            and (
                                token_indeterminate
                                or nft_indeterminate
                                or trait_indeterminate
                            )
                        )
                    else:
                        requirements_met = False

                    # If requirements are met, user is valid - no prompt needed
                    if requirements_met:
                        logging.info(f"User {user_id} already meets registration requirements")
                        return
                    if requirements_indeterminate:
                        logging.warning(
                            "Deferring returning-member gate decision for user %s in group %s "
                            "because Sui holdings were indeterminate",
                            user_id,
                            group_id,
                        )
                        return

                except Exception as e:
                    logging.error(f"Error validating user registration for {user_id}: {e}")

            # Send registration reminder
            try:
                # Store pending verification context so /api/verify can look up
                # the group even if the group_id is absent from the POST body.
                try:
                    with get_db_cursor() as (conn, cur):
                        cur.execute("""
                            INSERT INTO pending_verifications (user_id, group_id, created_at)
                            VALUES (%s, %s, NOW())
                            ON CONFLICT (user_id) DO UPDATE SET
                                group_id = EXCLUDED.group_id,
                                created_at = EXCLUDED.created_at
                        """, (user_id, group_id))
                except Exception as pv_err:
                    logging.warning(f"Could not store pending verification for new member {user_id} in group {group_id}: {pv_err}")

                # Create inline keyboard with registration button.
                # Use a generic deep link so that each user who clicks it gets
                # their own private verification session (via handle_start with
                # the register_ param), preventing accidental cross-registration
                # if the message is seen by other group members.
                _bot_username = get_bot_username()
                reg_link = f"https://t.me/{_bot_username}?start=register_{encode_group_id_for_deeplink(group_id)}"
                markup = types.InlineKeyboardMarkup()
                register_btn = types.InlineKeyboardButton("✅ Verify Wallet", url=reg_link)
                markup.add(register_btn)

                # Send welcome message with registration prompt
                welcome_text = (
                    f"👋 Welcome to the group, {user_name}!\n\n"
                    "To participate in this group, you'll need to verify your wallet. "
                    "Tap the button below to connect your SUI wallet and verify your holdings."
                )

                sent_message = bot.send_message(
                    group_id,
                    welcome_text,
                    reply_markup=markup
                )

                logging.info(f"Sent registration reminder to new member {user_id} in group {group_id}")

                # Auto-delete registration prompt after 15 minutes
                def delete_welcome(gid=group_id, mid=sent_message.message_id):
                    try:
                        bot.delete_message(gid, mid)
                    except Exception as e:
                        logging.debug(f"Could not auto-delete welcome message in group {gid}: {e}")

                _delayed_tasks.schedule(900, delete_welcome)

            except Exception as e:
                logging.error(f"Error sending registration reminder to new member: {e}")

    except Exception as e:
        logging.error(f"Error in handle_chat_member_update: {e}")


@bot.message_handler(commands=['start'])
def handle_start(message):
    parts = message.text.split()
    if len(parts) > 1:
        param = parts[1]

        if param.startswith("register_"):
            group_id_str = param[len("register_"):]
            try:
                group_id = decode_group_id_from_deeplink(group_id_str)
                with config_lock:
                    group_is_configured = group_id in SUBSCRIBER_CONFIGS
                if not group_is_configured:
                    bot.reply_to(
                        message,
                        "❌ Wallet registration is not configured for this group. "
                        "Please ask a group administrator to run /cwconfig.",
                    )
                    return
                try:
                    # Validate the public endpoint before touching registration
                    # state, then create both compatibility context and session.
                    get_public_api_base_url()
                    verify_url = build_wallet_connect_url(
                        group_id,
                        message.from_user.id,
                        update_pending_context=True,
                    )
                except Exception as exc:
                    logging.error(
                        "Wallet verification link issuance failed: %s",
                        exc,
                    )
                    bot.reply_to(
                        message,
                        "❌ Wallet verification is temporarily unavailable. "
                        "Please try again shortly or contact a group administrator.",
                    )
                    return
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("✅ Verify Wallet", url=verify_url))
                bot.reply_to(
                    message,
                    "🔐 *Wallet Registration*\n\nTap **Verify Wallet** to open the verification page and connect your SUI wallet.",
                    reply_markup=markup,
                    parse_mode="Markdown"
                )
            except ValueError:
                bot.reply_to(message, "❌ Invalid registration parameter.")

        elif param.startswith("config_"):
            group_id_str = param[len("config_"):]
            try:
                group_id = decode_group_id_from_deeplink(group_id_str)

                # Check if user is admin of the group
                try:
                    member = bot.get_chat_member(group_id, message.from_user.id)
                    if member.status not in ["creator", "administrator"]:
                        bot.reply_to(message, "❌ Only group administrators can access configuration settings.")
                        return
                except Exception as e:
                    bot.reply_to(message, "❌ Could not verify your admin status in the group.")
                    return

                # Show config menu in private chat
                show_config_menu_private(message.chat.id, group_id)

            except ValueError:
                bot.reply_to(message, "❌ Invalid configuration parameter.")

        elif param.startswith("votesetup_"):
            group_id_str = param[len("votesetup_"):]
            try:
                group_id = decode_group_id_from_deeplink(group_id_str)

                # Check if user is admin of the group
                try:
                    member = bot.get_chat_member(group_id, message.from_user.id)
                    if member.status not in ["creator", "administrator"]:
                        bot.reply_to(message, "❌ Only group administrators can access voting configuration.")
                        return
                except Exception as e:
                    bot.reply_to(message, "❌ Could not verify your admin status in the group.")
                    return

                # Show voting setup menu in private chat
                show_votesetup_menu_private(message.chat.id, group_id)

            except ValueError:
                bot.reply_to(message, "❌ Invalid voting setup parameter.")

        elif param.startswith("mywallets_"):
            group_id_str = param[len("mywallets_"):]
            try:
                group_id = decode_group_id_from_deeplink(group_id_str)

                # Store the group context for this user and then show wallets
                with get_db_cursor() as (conn, cur):
                    cur.execute("""
                        INSERT INTO pending_verifications (user_id, group_id, created_at)
                        VALUES (%s, %s, NOW())
                        ON CONFLICT (user_id) DO UPDATE SET
                            group_id = EXCLUDED.group_id,
                            created_at = EXCLUDED.created_at
                    """, (message.from_user.id, group_id))

                # Show wallets in private chat
                show_mywallets_private(message.chat.id, group_id)

            except ValueError:
                bot.reply_to(message, "❌ Invalid mywallets parameter.")
        else:
            bot.reply_to(message, "👋 Welcome to CityWatch!")
    else:
        bot.reply_to(message, "👋 Welcome to CityWatch! Use /help to see available commands.")

def build_wallet_connect_url(group_id, user_id, *, update_pending_context=False):
    """Build an Alpha City URL containing only fragment-scoped secrets."""
    api_verify_url = f"{get_public_api_base_url()}/api/verify"
    verification_session = create_verification_session(
        group_id,
        user_id,
        update_pending_context=update_pending_context,
    )
    return build_hosted_verification_url(
        WALLET_CONNECT_URL,
        verification_session,
        api_verify_url,
    )


def build_registration_restart_url(group_id):
    try:
        return (
            f"https://t.me/{get_bot_username()}?start=register_"
            f"{encode_group_id_for_deeplink(group_id)}"
        )
    except Exception as exc:
        logging.debug(
            "Could not build Telegram registration restart URL for group %s: %s",
            group_id,
            exc,
        )
        return ""


def build_telegram_return_url():
    """Return users to the bot without starting another registration."""
    try:
        return f"https://t.me/{get_bot_username()}"
    except Exception as exc:
        logging.debug("Could not build Telegram return URL: %s", exc)
        return ""


@db_retry
@bot.message_handler(commands=['register'])
def register_wallets(message):
    is_private = (message.chat.type == 'private')
    user_id = message.from_user.id

    if is_private:
        # Check if user has a pending group context — redirect them helpfully
        with get_db_cursor() as (conn, cur):
            cur.execute("SELECT group_id FROM pending_verifications WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
        if row:
            group_id = row[0]
            try:
                chat_obj = bot.get_chat(group_id)
                group_name = chat_obj.title
            except Exception:
                group_name = f"Group {group_id}"
            bot_username = get_bot_username()
            reg_link = f"https://t.me/{bot_username}?start=register_{encode_group_id_for_deeplink(group_id)}"
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("✅ Verify Wallet", url=reg_link))
            return bot.reply_to(
                message,
                f"ℹ️ To register for *{group_name}*, tap the button below:",
                reply_markup=markup,
                parse_mode="Markdown"
            )
        return bot.reply_to(
            message,
            "ℹ️ Wallet registration is done in your group chat or via an invite link.\n\n"
            "Please use /register in the group where you want to verify your wallet.",
            parse_mode="Markdown"
        )

    group_id = message.chat.id

    with get_db_cursor() as (conn, cur):
        cur.execute(
            """
            INSERT INTO pending_verifications (user_id, group_id, created_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (user_id) DO UPDATE SET
                group_id = EXCLUDED.group_id,
                created_at = EXCLUDED.created_at
            """,
            (user_id, group_id)
        )

    # Use a generic deep link so each user who clicks the button starts their
    # own private verification session under their own Telegram ID.  This
    # prevents cross-registration if the group message is seen by other members.
    bot_username = get_bot_username()
    reg_link = f"https://t.me/{bot_username}?start=register_{encode_group_id_for_deeplink(group_id)}"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("✅ Verify Wallet", url=reg_link))

    message_thread_id = getattr(message, 'message_thread_id', None)
    register_text = (
        "🔐 **Wallet Registration**\n\n"
        "Tap **Verify Wallet** to open the verification page and connect your SUI wallet.\n"
        "Your wallet is verified on-chain automatically!"
    )

    if message_thread_id:
        bot.send_message(
            message.chat.id,
            register_text,
            reply_markup=markup,
            parse_mode="Markdown",
            message_thread_id=message_thread_id
        )
    else:
        bot.send_message(
            message.chat.id,
            register_text,
            reply_markup=markup,
            parse_mode="Markdown"
        )


# ==================== Verification Helper Functions ==============

def _get_user_display_name(tg_user_id):
    """Resolve a Telegram user's display name from their user ID (best-effort)."""
    username = str(tg_user_id)
    try:
        user_info = bot.get_chat(tg_user_id)
        username = user_info.username or user_info.first_name or str(tg_user_id)
    except Exception:
        pass
    return username


def _build_verification_success_message(group_id, wallet_address, requirement_eval):
    """Build the Markdown success message lines for a completed wallet verification.

    Returns a list of message lines and the group display name.
    """
    group_name = f"Group {group_id}"
    try:
        group_name = bot.get_chat(group_id).title
    except Exception:
        pass

    text_lines = [
        "✅ *Wallet Verification Successful!*",
        "",
        f"*Group:* {group_name}",
        f"*Wallet:* `{wallet_address}`",
    ]
    if requirement_eval.get("details"):
        text_lines += ["", "📋 *Verification Details:*"] + requirement_eval["details"]
    text_lines += ["", "Your wallet has been registered! You can now participate in group activities.",
                   "Use `/mywallets` any time if you want to add an additional wallet."]

    try:
        invite = create_single_use_invite_link(group_id)
        if invite:
            text_lines += [
                "",
                "*Group Invite Link:*",
                f"[Join {group_name}]({invite})",
                "_Use this link to join or return to the group._"
            ]
    except Exception as e:
        logging.error(f"Error creating invite link for group {group_id}: {e}")

    return text_lines, group_name


def _send_group_verified_notification(group_id, display_name):
    """Send a notification to the group that a user has verified their wallet."""
    try:
        bot.send_message(group_id, f"✅ {display_name} has verified their wallet and is now registered!")
    except Exception as e:
        logging.warning(f"Could not send group verification notification for group {group_id}: {e}")


# ==================== Flask API Endpoints ========================

def _completed_verification_payload(completed, restart_url):
    """Build the stable response returned by completed-session replays."""
    eligibility_status = completed.get("eligibility_status") or "unknown"
    if eligibility_status == "fail":
        return ({
            'success': False,
            'error': 'Wallet does not meet group requirements',
            'message': (
                'Wallet ownership was verified and the address was registered, '
                'but current holdings do not meet this group’s requirements.'
            ),
            'retryable': False,
            'ownership_verified': True,
            'wallet_registered': True,
            'eligibility_status': 'fail',
            'requirements_met': False,
            'restart_url': restart_url,
            'replayed': True,
        }, 403)
    if eligibility_status == "pass":
        return ({
            'success': True,
            'message': 'Wallet verified and registered successfully',
            'ownership_verified': True,
            'wallet_registered': True,
            'eligibility_status': 'pass',
            'requirements_met': True,
            'restart_url': restart_url,
            'replayed': True,
        }, 200)
    return ({
        'success': True,
        'message': 'Wallet registration was already completed. Check Telegram for details.',
        'ownership_verified': True,
        'wallet_registered': True,
        'eligibility_status': 'unknown',
        'requirements_met': None,
        'restart_url': restart_url,
        'replayed': True,
    }, 200)

def _add_cors_headers(response):
    """Add credential-free CORS headers only for configured page origins."""
    origin = (request.headers.get('Origin') or '').rstrip('/')
    if origin and origin in CORS_ALLOWED_ORIGINS:
        response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Vary'] = 'Origin'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    response.headers['Access-Control-Max-Age'] = '600'
    return response


@app.after_request
def add_security_headers(response):
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('Referrer-Policy', 'no-referrer')
    response.headers.setdefault(
        'Permissions-Policy',
        'camera=(), microphone=(), geolocation=()',
    )
    if (
        request.path.startswith('/verify')
        or request.path.startswith('/wallet-connect')
        or request.path.startswith('/api/verify')
        or request.path.startswith('/api/verification')
    ):
        response.headers['Cache-Control'] = 'no-store, max-age=0'
    if request.path.startswith('/verify') or request.path.startswith('/wallet-connect'):
        response.headers.setdefault(
            'Content-Security-Policy',
            "default-src 'self'; "
            "script-src 'self' https://telegram.org; "
            "connect-src 'self' https: http://localhost:* http://127.0.0.1:*; "
            "img-src 'self' data: https:; "
            "style-src 'self'; "
            "object-src 'none'; base-uri 'none'; form-action 'none'; "
            "frame-ancestors 'none'",
        )
    return response


@app.route('/')
def home():
    with config_lock:
        group_count = len(SUBSCRIBER_CONFIGS)
    return jsonify({
        "status": "running",
        "bot_name": BOT_NAME,
        "configured_groups": group_count,
    })

@app.route('/wallet-connect')
@app.route('/verify')
def wallet_connect_webapp():
    """Serve the same session-bound signing flow used by the hosted page."""
    verification_session = request.args.get('verification_session', '')
    public_base = get_public_webapp_base_url()
    if public_base:
        api_verify_url = urljoin(public_base.rstrip('/') + '/', 'api/verify')
    else:
        api_verify_url = "/api/verify"
    # Render the verify page from the Jinja2 template.
    html = render_template(
        'verify.html',
        api_verify_url=api_verify_url,
        verification_session=verification_session,
    )
    return html


@app.route('/api/verification-context', methods=['GET', 'OPTIONS'])
def verification_context():
    """Return sanitized server-owned context for a hosted signing page."""
    if request.method == 'OPTIONS':
        return _add_cors_headers(jsonify({}))
    verification_session = request.args.get('verification_session', '')
    if not is_valid_verification_session_id(verification_session):
        return _add_cors_headers(jsonify({
            'success': False,
            'error': 'Verification link is invalid.',
            'restart_url': '',
        })), 400
    rate_key = f"context:{request.remote_addr}:{verification_session}"
    if not verification_rate_limiter.allow(rate_key):
        return _add_cors_headers(jsonify({
            'success': False,
            'error': 'Too many requests. Please wait and try again.',
        })), 429
    session = get_active_verification_session(verification_session)
    if not session:
        completed = get_completed_verification_result(verification_session)
        if completed:
            restart_url = build_registration_restart_url(completed['group_id'])
            completed_payload, _ = _completed_verification_payload(
                completed,
                restart_url,
            )
            return _add_cors_headers(jsonify({
                'success': True,
                'verification_completed': True,
                'verification_result': completed_payload,
                'restart_url': restart_url,
                'telegram_return_url': build_telegram_return_url(),
            }))
        expired_group_id = get_verification_session_group(
            verification_session
        )
        return _add_cors_headers(jsonify({
            'success': False,
            'error': 'Verification link is invalid, expired, or already used.',
            'restart_url': (
                build_registration_restart_url(expired_group_id)
                if expired_group_id is not None
                else ''
            ),
        })), 404
    with config_lock:
        cfg = dict(SUBSCRIBER_CONFIGS.get(session['group_id'], {}))
    if not cfg:
        return _add_cors_headers(jsonify({
            'success': False,
            'error': 'Group is not configured.',
        })), 404
    token_type = cfg.get('token') or ''
    token_label = (
        token_type.split('::')[-1]
        if '::' in token_type
        else 'Configured token'
    )
    collection_type = cfg.get('nft_collection_id') or ''
    collection_parts = collection_type.split('::')
    if len(collection_parts) >= 3:
        collection_label = '::'.join(collection_parts[-2:])
    elif collection_type:
        collection_label = (
            f"{collection_type[:8]}…{collection_type[-6:]}"
            if len(collection_type) > 18
            else collection_type
        )
    else:
        collection_label = 'Configured collection'
    response = jsonify({
        'success': True,
        'verification_session': verification_session,
        'group_id': str(session['group_id']),
        'telegram_user_id': str(session['user_id']),
        'restart_url': build_registration_restart_url(session['group_id']),
        'telegram_return_url': build_telegram_return_url(),
        'requirements': {
            'registration_mode': cfg.get('registration_mode', 'token'),
            'minimum_holding': str(cfg.get('minimum_holding', 0)),
            'decimals': int(cfg.get('decimals', 6)),
            'nft_threshold': int(cfg.get('nft_threshold', 1)),
            'has_token_requirement': bool(cfg.get('token')),
            'has_nft_requirement': bool(cfg.get('nft_collection_id')),
            'has_trait_requirement': bool(cfg.get('nft_trait_name')),
            'token_label': token_label,
            'collection_label': collection_label,
            'trait_name': cfg.get('nft_trait_name', ''),
            'trait_value': cfg.get('nft_trait_value', ''),
        },
    })
    return _add_cors_headers(response)


@app.route('/api/verify', methods=['POST', 'OPTIONS'])
def api_verify():
    """Verify ownership and holdings using only server-owned configuration."""
    if request.method == 'OPTIONS':
        return _add_cors_headers(jsonify({}))

    verification_session = ''
    group_id = None
    tg_user_id = None
    restart_url = ''
    wallet_address = ''
    claim_id = None
    claimed = False
    verification_work_acquired = False
    runtime_metrics.increment("verification_attempts")
    try:
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return _add_cors_headers(jsonify({
                'success': False,
                'error': 'JSON object required',
            })), 400

        wallet_value = data.get('wallet_address') or data.get('walletAddress') or ''
        session_value = data.get('verification_session') or data.get('verificationSession') or ''
        signature_value = data.get('wallet_signature') or data.get('walletSignature') or ''
        wallet_address = wallet_value.strip() if isinstance(wallet_value, str) else ''
        verification_session = session_value if isinstance(session_value, str) else ''
        wallet_signature = signature_value if isinstance(signature_value, str) else ''

        if not is_valid_verification_session_id(verification_session):
            return _add_cors_headers(jsonify({
                'success': False,
                'error': 'Verification link is invalid.',
            })), 400
        if not wallet_signature or len(wallet_signature) > 4096:
            return _add_cors_headers(jsonify({
                'success': False,
                'error': 'Wallet signature is missing or invalid.',
            })), 400
        rate_key = f"verify:{request.remote_addr}:{verification_session}"
        if not verification_rate_limiter.allow(rate_key):
            return _add_cors_headers(jsonify({
                'success': False,
                'error': 'Too many verification attempts. Please wait and try again.',
            })), 429
        canonical_wallet = canonical_sui_address(wallet_address)
        if canonical_wallet is None:
            return _add_cors_headers(jsonify({
                'success': False,
                'error': 'Invalid wallet address',
            })), 400
        wallet_address = canonical_wallet

        completed = get_completed_verification_result(
            verification_session,
            wallet_address,
        )
        if completed:
            restart_url = build_registration_restart_url(completed['group_id'])
            payload, status_code = _completed_verification_payload(
                completed,
                restart_url,
            )
            runtime_metrics.increment("verification_result_replays")
            return _add_cors_headers(jsonify(payload)), status_code

        session = get_active_verification_session(verification_session)
        if not session:
            return _add_cors_headers(jsonify({
                'success': False,
                'error': 'Verification link is invalid, expired, or already used. Please run /register again.',
            })), 400
        group_id = session['group_id']
        tg_user_id = session['user_id']
        restart_url = build_registration_restart_url(group_id)

        with config_lock:
            cfg = dict(SUBSCRIBER_CONFIGS.get(group_id, {}))
        if not cfg:
            return _add_cors_headers(jsonify({
                'success': False,
                'error': 'Group is not configured. Ask an admin to run /cwconfig first.',
                'restart_url': restart_url,
            })), 400

        verification_work_acquired = _verification_work_slots.acquire(
            blocking=False
        )
        if not verification_work_acquired:
            runtime_metrics.increment("verification_capacity_limited")
            response = _add_cors_headers(jsonify({
                'success': False,
                'error': 'Verification is busy right now. Please wait a moment and try again.',
                'retryable': True,
                'restart_url': restart_url,
            }))
            response.headers['Retry-After'] = '3'
            return response, 503

        claim_id = secrets.token_urlsafe(24)
        claimed = True
        claimed_id = claim_verification_session(
            verification_session,
            group_id,
            tg_user_id,
            claim_id,
        )
        if not claimed_id:
            claimed = False
            # The winning request might have committed between our initial
            # replay check and this claim attempt.
            completed = get_completed_verification_result(
                verification_session,
                wallet_address,
            )
            if completed:
                payload, status_code = _completed_verification_payload(
                    completed,
                    restart_url,
                )
                runtime_metrics.increment("verification_result_replays")
                return _add_cors_headers(jsonify(payload)), status_code
            attempt_state = get_verification_attempt_state(verification_session)
            if (
                attempt_state
                and not attempt_state["completed"]
                and attempt_state["attempt_count"] >= VERIFICATION_SESSION_MAX_ATTEMPTS
            ):
                runtime_metrics.increment("verification_session_rate_limited")
                return _add_cors_headers(jsonify({
                    'success': False,
                    'error': (
                        'This verification link has reached its attempt limit. '
                        'Please request a new link in Telegram.'
                    ),
                    'retryable': False,
                    'restart_url': restart_url,
                })), 429
            response = _add_cors_headers(jsonify({
                'success': False,
                'error': 'Verification is already being processed. Please wait a moment and try again.',
                'retryable': True,
                'restart_url': restart_url,
            }))
            response.headers['Retry-After'] = '3'
            return response, 409
        ownership_message = build_wallet_ownership_message(
            verification_session,
            group_id,
            tg_user_id,
            wallet_address,
        )
        operation_deadline = time.monotonic() + SUI_OPERATION_TIMEOUT_SECONDS
        try:
            signature_valid = sui_gateway.verify_personal_message(
                author=wallet_address,
                message=ownership_message,
                signature=wallet_signature,
                deadline_monotonic=operation_deadline,
            )
        except SuiGatewayError as exc:
            runtime_metrics.increment("verification_signature_unavailable")
            logging.warning("api_verify: Sui signature verification unavailable: %s", exc)
            return _add_cors_headers(jsonify({
                'success': False,
                'error': 'Sui verification is temporarily unavailable. Please try again.',
                'retryable': True,
                'restart_url': restart_url,
            })), 503
        if not signature_valid:
            runtime_metrics.increment("verification_signature_rejected")
            return _add_cors_headers(jsonify({
                'success': False,
                'error': 'Wallet ownership signature could not be verified.',
                'restart_url': restart_url,
            })), 403

        if wallet_already_registered(wallet_address, group_id, user_id=tg_user_id):
            def _notify_duplicate(uid=tg_user_id):
                try:
                    bot.send_message(uid, "⚠️ This wallet address is already registered to another user in this group.")
                except Exception:
                    pass
            _background_executor.submit(_notify_duplicate)
            return _add_cors_headers(jsonify({
                'success': False,
                'error': 'Wallet already registered to another user in this group',
                'ownership_verified': True,
                'wallet_registered': False,
                'restart_url': restart_url,
            })), 409

        requirement_eval = evaluate_wallet_requirements(
            wallet_address,
            cfg,
            user_id=tg_user_id,
            force_fresh=True,
            deadline_monotonic=operation_deadline,
        )
        if requirement_eval.get('status') == 'indeterminate':
            runtime_metrics.increment("verification_holdings_unavailable")
            return _add_cors_headers(jsonify({
                'success': False,
                'error': 'Unable to verify on-chain holdings right now. Please try again.',
                'details': requirement_eval.get('errors', []),
                'retryable': True,
                'ownership_verified': True,
                'wallet_registered': False,
                'restart_url': restart_url,
            })), 503

        username = _get_user_display_name(tg_user_id)
        try:
            success = finalize_verified_wallet(
                verification_session,
                group_id,
                tg_user_id,
                username,
                wallet_address,
                cfg.get("registration_mode", "token"),
                'pass' if requirement_eval['requirements_met'] else 'fail',
                claim_id,
            )
        except psycopg2.IntegrityError:
            return _add_cors_headers(jsonify({
                'success': False,
                'error': 'Wallet already registered to another user in this group',
                'ownership_verified': True,
                'wallet_registered': False,
                'restart_url': restart_url,
            })), 409
        if not success:
            return _add_cors_headers(jsonify({
                'success': False,
                'error': 'Failed to complete verification. Please try again.',
                'retryable': True,
                'restart_url': restart_url,
            })), 500
        claimed = False

        _nft = requirement_eval.get("nft_count")
        _trait = requirement_eval.get("trait_count")
        _bal = requirement_eval.get("token_balance")
        if _nft is not None or _trait is not None or _bal is not None:
            try:
                update_user_cached_holdings(group_id, tg_user_id, nft_count=_nft, trait_count=_trait, token_balance=_bal)
            except Exception as e:
                logging.debug(f"Could not update cached holdings for user {tg_user_id}: {e}")

        if not requirement_eval['requirements_met']:
            runtime_metrics.increment("verification_registered_ineligible")
            eval_errors = requirement_eval.get('errors', [])
            error_msg = "❌ *Wallet doesn't meet requirements:*\n\n" + "\n".join(
                eval_errors or ['Please retry after updating your holdings.']
            )
            if requirement_eval.get('details'):
                error_msg += "\n\n📋 *Current Check Details:*\n" + "\n".join(requirement_eval['details'])
            error_msg += "\n\n_Your wallet has been saved. You can re-verify at any time._"
            try:
                bot.send_message(tg_user_id, error_msg, parse_mode='Markdown')
            except Exception as e:
                logging.debug(f"api_verify: could not send requirement error to user {tg_user_id}: {e}")
            return _add_cors_headers(jsonify({
                'success': False,
                'error': 'Wallet does not meet group requirements',
                'message': (
                    'Wallet ownership was verified and the address was '
                    'registered, but current holdings do not meet this '
                    'group’s requirements.'
                ),
                'details': eval_errors,
                'retryable': False,
                'ownership_verified': True,
                'wallet_registered': True,
                'eligibility_status': 'fail',
                'requirements_met': False,
                'restart_url': restart_url,
            })), 403

        # Send confirmation to user and notify the group
        try:
            text_lines, _ = _build_verification_success_message(group_id, wallet_address, requirement_eval)
            bot.send_message(tg_user_id, "\n".join(text_lines), parse_mode='Markdown', disable_web_page_preview=True)
        except Exception as e:
            logging.error(f"api_verify: failed to send Telegram confirmation to user {tg_user_id}: {e}")

        display_name = f"@{username}" if (username and not username.isdigit()) else f"user {tg_user_id}"
        _send_group_verified_notification(group_id, display_name)

        logging.info(f"api_verify: wallet {wallet_address} verified for user {tg_user_id} in group {group_id}")
        runtime_metrics.increment("verification_succeeded")
        return _add_cors_headers(jsonify({
            'success': True,
            'message': 'Wallet verified and registered successfully',
            'ownership_verified': True,
            'wallet_registered': True,
            'eligibility_status': 'pass',
            'requirements_met': True,
            'restart_url': restart_url,
        }))

    except Exception as e:
        runtime_metrics.increment("verification_internal_errors")
        logging.exception("Error in api_verify")
        if verification_session and wallet_address:
            try:
                completed = get_completed_verification_result(
                    verification_session,
                    wallet_address,
                )
                if completed:
                    payload, status_code = _completed_verification_payload(
                        completed,
                        build_registration_restart_url(completed['group_id']),
                    )
                    runtime_metrics.increment("verification_result_replays")
                    return _add_cors_headers(jsonify(payload)), status_code
            except Exception:
                logging.exception("Could not reconcile an ambiguous verification commit")
        return _add_cors_headers(jsonify({
            'success': False,
            'error': 'Internal server error',
            'retryable': bool(claimed),
            'restart_url': restart_url,
        })), 500
    finally:
        if (
            claimed
            and claim_id
            and verification_session
            and group_id is not None
            and tg_user_id is not None
        ):
            try:
                release_verification_session(
                    verification_session,
                    group_id,
                    tg_user_id,
                    claim_id,
                )
            except Exception:
                logging.exception("Could not release failed verification session")
        if verification_work_acquired:
            _verification_work_slots.release()


@app.route('/telegram/webhook', methods=['POST'])
def telegram_webhook():
    """Receive authenticated Telegram updates when webhook mode is enabled."""
    if not TELEGRAM_WEBHOOK_URL or not TELEGRAM_WEBHOOK_SECRET:
        return jsonify({"error": "webhook mode is disabled"}), 404
    supplied_secret = request.headers.get(
        'X-Telegram-Bot-Api-Secret-Token',
        '',
    )
    if not secrets.compare_digest(supplied_secret, TELEGRAM_WEBHOOK_SECRET):
        return jsonify({"error": "unauthorized"}), 401
    try:
        update = types.Update.de_json(request.get_data(as_text=True))
        bot.process_new_updates([update])
        return jsonify({"ok": True})
    except Exception:
        logging.exception("Failed to process Telegram webhook update")
        return jsonify({"error": "invalid update"}), 400


# ==================== Stripe Webhook & Subscription Routes ========

@app.route('/stripe/webhook', methods=['POST'])
def stripe_webhook():
    """Handle Stripe webhook events for payment completion."""
    payload = request.get_data()
    sig_header = request.headers.get('Stripe-Signature', '')

    if not STRIPE_SECRET_KEY:
        return jsonify({"error": "Stripe not configured"}), 503

    # STRIPE_WEBHOOK_SECRET is required.  Without it we cannot verify that a
    # request genuinely originated from Stripe, so we refuse all webhook
    # traffic rather than falling back to processing unsigned payloads.
    if not STRIPE_WEBHOOK_SECRET:
        logging.error(
            "Received POST /stripe/webhook but STRIPE_WEBHOOK_SECRET is not set. "
            "All webhook requests are rejected until the secret is configured."
        )
        return jsonify({"error": "Webhook secret not configured"}), 503

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except stripe.SignatureVerificationError:
        logging.warning("Stripe webhook signature verification failed")
        return jsonify({"error": "Invalid signature"}), 400
    except Exception as e:
        logging.error(f"Stripe webhook error: {e}")
        return jsonify({"error": "Webhook error"}), 400

    if event.get("type") == "checkout.session.completed":
        session_data = event["data"]["object"]
        stripe_session_id = session_data.get("id", "")

        metadata = session_data.get("metadata", {})
        group_id_str = metadata.get("group_id")
        user_id_str = metadata.get("user_id")
        tier = metadata.get("tier")

        if group_id_str and tier:
            try:
                group_id = int(group_id_str)
                user_id = int(user_id_str) if user_id_str else 0
                new_expiry, processed_now = activate_subscription_from_stripe(
                    group_id, stripe_session_id, tier, user_id
                )
                if not processed_now:
                    logging.info(f"Stripe session {stripe_session_id} already processed, skipping")
                    return jsonify({"status": "ok"})
                logging.info(f"Stripe payment completed: group={group_id}, tier={tier}, expires={new_expiry}")

                # Notify the user via Telegram
                if user_id:
                    try:
                        tier_info = SUBSCRIPTION_TIERS.get(tier, {})
                        try:
                            chat_obj = bot.get_chat(group_id)
                            gname = chat_obj.title
                        except Exception:
                            gname = f"Group {group_id}"
                        bot.send_message(
                            user_id,
                            f"✅ **Payment Confirmed!**\n\n"
                            f"Your *{tier_info.get('label', tier)}* subscription for "
                            f"*{gname}* is now active.\n\n"
                            f"📅 Expires: {new_expiry.strftime('%Y-%m-%d %H:%M UTC')}\n\n"
                            f"You can now use /cwconfig to configure the bot.",
                            parse_mode="Markdown"
                        )
                    except Exception as notify_e:
                        logging.warning(f"Could not notify user {user_id} about subscription: {notify_e}")
            except (ValueError, TypeError) as e:
                logging.error(f"Invalid metadata in Stripe webhook: {e}")
        else:
            logging.warning(f"Stripe checkout.session.completed missing metadata: {metadata}")

    return jsonify({"status": "ok"}), 200


@app.route('/subscription/success')
def subscription_success():
    """Simple success landing page after Stripe Checkout."""
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<title>Payment Successful</title>'
        '<style>body{font-family:system-ui,sans-serif;display:flex;align-items:center;'
        'justify-content:center;min-height:100vh;margin:0;background:#f0f9f0;}'
        '.card{background:#fff;border-radius:12px;padding:2rem;text-align:center;'
        'box-shadow:0 2px 10px rgba(0,0,0,.1);max-width:420px}'
        'h1{color:#22c55e;margin:0 0 .5rem}p{color:#555;line-height:1.5}'
        '</style></head><body><div class="card">'
        '<h1>✅ Payment Successful</h1>'
        '<p>Your CityWatchBot subscription is now active.</p>'
        '<p>Return to Telegram and run <b>/cwconfig</b> to configure your group.</p>'
        '</div></body></html>'
    ), 200


@app.route('/subscription/cancel')
def subscription_cancel():
    """Simple cancel landing page if user aborts Stripe Checkout."""
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<title>Payment Cancelled</title>'
        '<style>body{font-family:system-ui,sans-serif;display:flex;align-items:center;'
        'justify-content:center;min-height:100vh;margin:0;background:#fff5f5;}'
        '.card{background:#fff;border-radius:12px;padding:2rem;text-align:center;'
        'box-shadow:0 2px 10px rgba(0,0,0,.1);max-width:420px}'
        'h1{color:#ef4444;margin:0 0 .5rem}p{color:#555;line-height:1.5}'
        '</style></head><body><div class="card">'
        '<h1>❌ Payment Cancelled</h1>'
        '<p>No charges were made. You can try again anytime by running <b>/cwconfig</b> in your Telegram group.</p>'
        '</div></body></html>'
    ), 200


@app.route('/health')
def health_check():
    """Cheap liveness probe with no database or third-party network calls."""
    return jsonify({
        "status": "healthy",
        "revision": CODE_SYNC_REV,
        "uptime_seconds": time.time() - APPLICATION_START_TIME,
        "timestamp": time.time(),
    })


@app.route('/metrics')
def metrics_check():
    """Return credential-free runtime metrics when explicitly enabled."""
    if not METRICS_TOKEN:
        return jsonify({"error": "not found"}), 404
    authorization = request.headers.get("Authorization", "")
    supplied = (
        authorization[7:]
        if authorization.startswith("Bearer ")
        else ""
    )
    if not secrets.compare_digest(supplied, METRICS_TOKEN):
        return jsonify({"error": "unauthorized"}), 401
    with _wallet_scan_state_lock:
        wallet_scan = dict(_wallet_scan_state)
    return jsonify({
        "revision": CODE_SYNC_REV,
        "uptime_seconds": time.time() - APPLICATION_START_TIME,
        "wallet_scan": wallet_scan,
        "sui_graphql_providers": sui_gateway.provider_status(),
        **runtime_metrics.snapshot(),
    })


_readiness_lock = threading.Lock()
_readiness_cache = {"checked_at": 0.0, "payload": None, "status": 503}


@app.route('/ready')
def readiness_check():
    """Cached readiness probe for PostgreSQL and the configured Sui provider."""
    now = time.monotonic()
    with _readiness_lock:
        if (
            _readiness_cache["payload"] is not None
            and now - _readiness_cache["checked_at"] < 30
        ):
            return jsonify(_readiness_cache["payload"]), _readiness_cache["status"]

    payload = {
        "status": "ready",
        "database": "healthy",
        "sui_graphql": "healthy",
        "wallet_registration": "healthy",
        "wallet_connect_url": WALLET_CONNECT_URL,
        "sui_graphql_providers": sui_gateway.provider_status(),
        "chain_identifier": None,
        "timestamp": time.time(),
    }
    status_code = 200
    try:
        with get_db_cursor() as (conn, cur):
            cur.execute("SELECT 1")
            cur.fetchone()
    except Exception as exc:
        payload["database"] = f"unhealthy: {type(exc).__name__}"
        status_code = 503
    try:
        payload["chain_identifier"] = sui_gateway.chain_identifier()
    except Exception as exc:
        payload["sui_graphql"] = f"unhealthy: {type(exc).__name__}"
        status_code = 503
    try:
        payload["public_api_base_url"] = get_public_api_base_url()
    except Exception as exc:
        payload["wallet_registration"] = f"unhealthy: {type(exc).__name__}"
        status_code = 503
    if status_code != 200:
        payload["status"] = "not_ready"
    with _readiness_lock:
        _readiness_cache.update({
            "checked_at": now,
            "payload": payload,
            "status": status_code,
        })
    return jsonify(payload), status_code

def is_valid_wallet_address(address):
    return canonical_sui_address(address) is not None

# ==================== New Functions =============================

# ==================== Main Execution =============================
# Global application start time for uptime tracking
APPLICATION_START_TIME = time.time()

if __name__ == "__main__":
    PORT = int(os.environ.get("PORT", "5000"))
    HOST = "0.0.0.0"
    print(f"{BOT_NAME} is starting as a background worker...")

    def keep_alive():
        cleanup_counter = 0
        while True:
            current_time = time.time()
            cleanup_counter += 1

            # Cleanup old cache entries and expired data
            try:
                # Clean balance cache
                with cache_lock:
                    expired_balance_keys = [k for k, (cache_time, _) in balance_cache.items() 
                                            if current_time - cache_time > CACHE_TTL * 2]
                    for key in expired_balance_keys:
                        del balance_cache[key]
                if expired_balance_keys:
                    logging.debug(f"Cleaned up {len(expired_balance_keys)} expired balance cache entries")

                # Clean NFT cache
                with cache_lock:
                    expired_nft_keys = [k for k, (cache_time, _) in nft_cache.items() 
                                        if current_time - cache_time > NFT_CACHE_TTL * 2]
                    for key in expired_nft_keys:
                        del nft_cache[key]
                if expired_nft_keys:
                    logging.debug(f"Cleaned up {len(expired_nft_keys)} expired NFT cache entries")

                # Clean up other expired data
                cleanup_expired_data()

                # Every 10 minutes, run garbage collection and log memory usage
                if cleanup_counter % 10 == 0:
                    try:
                        collected = gc.collect()
                        process = psutil.Process()
                        memory_mb = process.memory_info().rss / 1024 / 1024
                        logging.info(f"Memory usage: {memory_mb:.1f}MB, GC collected: {collected} objects")

                        # If memory usage is high, be more aggressive with cache cleanup
                        if memory_mb > 500:  # 500MB threshold
                            logging.warning(f"High memory usage detected: {memory_mb:.1f}MB")
                            # Clear older cache entries more aggressively
                            with cache_lock:
                                old_balance_keys = [k for k, (cache_time, _) in balance_cache.items() 
                                                    if current_time - cache_time > CACHE_TTL]
                                for key in old_balance_keys:
                                    del balance_cache[key]
                                old_nft_keys = [k for k, (cache_time, _) in nft_cache.items() 
                                                if current_time - cache_time > NFT_CACHE_TTL // 2]
                                for key in old_nft_keys:
                                    del nft_cache[key]
                            if old_balance_keys or old_nft_keys:
                                logging.info(f"Aggressive cleanup: removed {len(old_balance_keys)} balance, {len(old_nft_keys)} NFT cache entries")
                    except Exception as mem_e:
                        logging.error(f"Error in memory monitoring: {mem_e}")

            except Exception as e:
                logging.error(f"Error cleaning cache and expired data: {e}")

            time.sleep(60)

    keepalive_thread = threading.Thread(target=keep_alive)
    keepalive_thread.daemon = True
    keepalive_thread.start()

    flask_thread = threading.Thread(
        target=lambda: waitress_serve(
            app,
            host=HOST,
            port=PORT,
            threads=WAITRESS_THREADS,
        ),
        name="waitress-server",
    )
    flask_thread.daemon = True
    flask_thread.start()

    user_wallets_thread = threading.Thread(target=check_user_wallets)
    user_wallets_thread.daemon = True
    user_wallets_thread.start()

    def start_polling():
        print(f"{BOT_NAME} is starting polling...")
        conflict_count = 0
        max_conflict_retries = 3
        error_backoff = 60  # Initial backoff for non-409 errors (seconds)
        max_error_backoff = 600  # Cap at 10 minutes
        while True:
            if not refresh_telegram_poller_lease():
                logging.info("Another instance holds the Telegram polling lease; waiting.")
                time.sleep(45)
                continue
            lease_stop = threading.Event()

            def renew_poller_lease(stop_event=lease_stop):
                while not stop_event.wait(45):
                    try:
                        if not refresh_telegram_poller_lease():
                            logging.error("Lost Telegram polling lease; stopping this poller.")
                            bot.stop_polling()
                            return
                    except Exception:
                        logging.exception("Could not renew Telegram polling lease")
                        bot.stop_polling()
                        return

            renewal_thread = threading.Thread(
                target=renew_poller_lease,
                name="telegram-poller-lease",
                daemon=True,
            )
            renewal_thread.start()
            try:
                logging.info("Bot polling started with none_stop=True.")
                bot.infinity_polling(
                    timeout=BOT_POLLING_TIMEOUT,
                    long_polling_timeout=BOT_LONG_POLLING_TIMEOUT,
                    none_stop=True,
                    allowed_updates=TELEGRAM_ALLOWED_UPDATES,
                )
                conflict_count = 0  # Reset on successful polling
                error_backoff = 60  # Reset backoff on success
            except telebot.apihelper.ApiTelegramException as api_e:
                if "409" in str(api_e) or "Conflict" in str(api_e):
                    conflict_count += 1
                    if conflict_count >= max_conflict_retries:
                        logging.error(f"Multiple 409 conflicts detected ({conflict_count}). Another bot instance is likely running. Backing off for 5 minutes...")
                        time.sleep(300)  # 5 minute backoff to let other instance handle updates
                        conflict_count = 0
                    else:
                        logging.warning(f"409 Conflict detected (attempt {conflict_count}/{max_conflict_retries}). Retrying in 30 seconds...")
                        time.sleep(30)
                else:
                    logging.critical(f"Telegram API error in polling: {api_e}. Restarting in {error_backoff} seconds...")
                    time.sleep(error_backoff)
                    error_backoff = min(error_backoff * 2, max_error_backoff)
            except Exception as e:
                # This will only catch very critical errors that stop the polling loop entirely
                logging.critical(f"CRITICAL ERROR in polling loop: {e}. Restarting polling in {error_backoff} seconds...")
                time.sleep(error_backoff)
                error_backoff = min(error_backoff * 2, max_error_backoff)
            finally:
                lease_stop.set()


    # Register bot commands for auto-completion
    def register_bot_commands():
        try:
            # Retrieve bot username to ensure it is authenticated and fetched
            username = get_bot_username()
            logging.info(f"Registering bot commands for @{username}...")

            # 1. Default commands (for private chats)
            private_commands = [
                telebot.types.BotCommand("help", "Show help information"),
                telebot.types.BotCommand("register", "Register your wallet addresses"),
                telebot.types.BotCommand("mywallets", "View and manage your registered wallets"),
            ]
            bot.set_my_commands(private_commands, scope=telebot.types.BotCommandScopeAllPrivateChats())

            # 2. Public group commands (visible to all members in groups)
            group_commands = [
                telebot.types.BotCommand("help", "Show help information"),
                telebot.types.BotCommand("register", "Register your wallet addresses"),
                telebot.types.BotCommand("mywallets", "View and manage your registered wallets"),
            ]
            bot.set_my_commands(group_commands, scope=telebot.types.BotCommandScopeAllGroupChats())

            # 3. Admin commands (visible only to chat administrators in groups)
            admin_commands = [
                telebot.types.BotCommand("help", "Show help information"),
                telebot.types.BotCommand("register", "Register your wallet addresses"),
                telebot.types.BotCommand("mywallets", "View and manage your registered wallets"),
                telebot.types.BotCommand("cwconfig", "Configure group settings (admins only)"),
                telebot.types.BotCommand("cwstatus", "Show bot and gate status (admins only)"),
                telebot.types.BotCommand("votesetup", "Configure voting settings (admins only)"),
                telebot.types.BotCommand("vote", "Create a new poll (admins only)"),
                telebot.types.BotCommand("reminder", "Send registration reminder (admins only)"),
                telebot.types.BotCommand("exempt", "Exempt a user from wallet requirements (admins only)"),
                telebot.types.BotCommand("addwallet", "Add wallet for a user by reply or user ID (admins only)"),
            ]
            bot.set_my_commands(admin_commands, scope=telebot.types.BotCommandScopeAllChatAdministrators())

            # 4. Default commands as fallback
            bot.set_my_commands(admin_commands)  # Keep admin commands in default scope for legacy support / backward compatibility

            logging.info(f"Bot commands for @{username} registered successfully across all scopes.")
        except Exception as e:
            logging.error(f"Failed to register bot commands: {e}")

    register_bot_commands()

    if TELEGRAM_WEBHOOK_URL:
        if not TELEGRAM_WEBHOOK_SECRET:
            raise ValueError("TELEGRAM_WEBHOOK_SECRET is required in webhook mode")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", TELEGRAM_WEBHOOK_SECRET):
            raise ValueError("TELEGRAM_WEBHOOK_SECRET contains unsupported characters")
        parsed_webhook = urlsplit(TELEGRAM_WEBHOOK_URL)
        if parsed_webhook.scheme != 'https' or not parsed_webhook.netloc:
            raise ValueError("TELEGRAM_WEBHOOK_URL must be an absolute HTTPS URL")
        webhook_url = TELEGRAM_WEBHOOK_URL.rstrip('/')
        if not webhook_url.endswith('/telegram/webhook'):
            webhook_url += '/telegram/webhook'
        bot.remove_webhook()
        bot.set_webhook(
            url=webhook_url,
            secret_token=TELEGRAM_WEBHOOK_SECRET,
            allowed_updates=TELEGRAM_ALLOWED_UPDATES,
        )
        logging.info("Telegram webhook mode enabled at %s", webhook_url)
    else:
        bot.remove_webhook()
        polling_thread = threading.Thread(
            target=start_polling,
            name="telegram-poller",
            daemon=True,
        )
        polling_thread.start()

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print("Application shutting down...")
        bot.stop_polling()
        _delayed_tasks.close()
        _background_executor.shutdown(wait=False, cancel_futures=True)
        _sui_executor.shutdown(wait=False, cancel_futures=True)
        sys.exit(0)
