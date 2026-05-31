"""Database operations module for Token-Gate bot.

Encapsulates connection pooling, cursor management, schema initialization,
and all data-access functions that interact with PostgreSQL.
"""

import os
import re
import time
import json
import logging
import functools
import threading
import datetime

import psycopg2
from psycopg2 import pool
from contextlib import contextmanager

# ==================== Database Constants ============================
DB_POOL_MIN = int(os.getenv('DB_POOL_MIN', '5'))
DB_POOL_MAX = int(os.getenv('DB_POOL_MAX', '15'))
_POOL_CHECK_INTERVAL = 30  # seconds

# ==================== Module-level State ============================
db_lock = threading.Lock()
connection_pool = None
_pool_last_check = [0.0]

database_url = os.getenv('DATABASE_URL')
if not database_url:
    raise ValueError("DATABASE_URL not found in environment variables")


# ==================== Retry Decorator ===============================
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
                    time.sleep(2 ** attempt)
                    get_connection_pool()
                else:
                    logging.error(f"DB operation failed after {max_retries} retries.")
                    raise
    return wrapper


# ==================== Connection Pool ===============================
def get_connection_pool():
    global connection_pool, database_url

    if connection_pool:
        now = time.time()
        if now - _pool_last_check[0] < _POOL_CHECK_INTERVAL:
            return connection_pool
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
                    time.sleep(2)
            except Exception as close_ex:
                logging.error(f"Error closing connections: {close_ex}")
            connection_pool = None

    with db_lock:
        if connection_pool:
            return connection_pool

        if not database_url:
            raise Exception("DATABASE_URL is not set")
        connection_string: str = database_url

        if 'sslmode=' not in connection_string:
            separator = '&' if '?' in connection_string else '?'
            connection_string = connection_string + f"{separator}sslmode=require"

        if '-pooler.' not in connection_string and 'neon.tech' in connection_string:
            endpoint_match = re.search(r'@([^.]+)\.', connection_string)
            if endpoint_match:
                endpoint_id = endpoint_match.group(1)
                separator = '&' if '?' in connection_string else '?'
                connection_string = connection_string + f"{separator}options=endpoint%3D{endpoint_id}"
                logging.info(f"Added Neon endpoint parameter: {endpoint_id}")

        logging.info(f"Final connection string (masked): {connection_string.split('@')[0]}@[MASKED]")

        tries = 0
        max_tries = 5
        backoff_time = 2
        while tries < max_tries:
            try:
                connection_pool = pool.ThreadedConnectionPool(
                    DB_POOL_MIN, DB_POOL_MAX,
                    connection_string,
                    connect_timeout=30,
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

                if 'password authentication failed' in error_msg:
                    logging.error(f"Authentication failed - check DATABASE_URL credentials (attempt {tries}/{max_tries})")
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
        conn.commit()
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
                if "closed" in str(e).lower() or "exhausted" in str(e).lower():
                    global connection_pool
                    connection_pool = None


# ==================== Schema Initialization =========================
@db_retry
def init_db():
    with get_db_cursor() as (conn, cur):
        cur.execute("""
            CREATE TABLE IF NOT EXISTS subscriber_configs (
                chat_id BIGINT PRIMARY KEY,
                token TEXT,
                minimum_holding REAL,
                wallets TEXT,
                decimals INTEGER DEFAULT 6,
                auto_remove BOOLEAN DEFAULT FALSE,
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
                vote_weight REAL,
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
                wallet_address TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
        try:
            cur.execute("ALTER TABLE subscriber_configs ADD COLUMN IF NOT EXISTS auto_remove BOOLEAN DEFAULT FALSE")
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
            cur.execute("ALTER TABLE user_wallets ADD COLUMN IF NOT EXISTS last_token_balance REAL DEFAULT NULL")
            cur.execute("ALTER TABLE user_wallets ADD COLUMN IF NOT EXISTS holdings_updated_at TIMESTAMP DEFAULT NULL")
        except Exception as e:
            logging.error(f"Error adding new columns: {e}")
            conn.rollback()
            logging.warning("Database initialized with migration errors (non-critical columns may be missing)")
            return True
        logging.info("Database initialized successfully")
        return True


# ==================== Config Operations =============================
@db_retry
def load_configs_from_db():
    with get_db_cursor() as (conn, cur):
        cur.execute("""SELECT chat_id, token, minimum_holding, decimals, wallets, auto_remove, 
            nft_collection_id, nft_threshold, registration_mode, votes_per_nft, 
            votes_per_million_tokens, vote_duration, votes_per_exempt,
            COALESCE(nft_trait_name, '') as nft_trait_name,
            COALESCE(nft_trait_value, '') as nft_trait_value,
            COALESCE(nft_trait_threshold, 1) as nft_trait_threshold
            FROM subscriber_configs""")
        rows = cur.fetchall()
    configs = {}
    for row in rows:
        (chat_id, token, minimum_holding, decimals, wallets_json, auto_remove,
         nft_collection_id, nft_threshold, registration_mode, votes_per_nft,
         votes_per_million_tokens, vote_duration, votes_per_exempt,
         nft_trait_name, nft_trait_value, nft_trait_threshold) = row
        try:
            wallets = json.loads(wallets_json) if wallets_json else {}
        except Exception as e:
            logging.warning(f"Error parsing wallets JSON for chat {chat_id}: {e}")
            wallets = {}
        configs[chat_id] = {
            "token": token,
            "minimum_holding": minimum_holding,
            "decimals": decimals,
            "wallets": wallets,
            "auto_remove": auto_remove if auto_remove is not None else False,
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
        wallets_json = json.dumps(config.get("wallets", {}))
        decimals = config.get("decimals", 6)
        votes_per_nft = config.get("votes_per_nft", 1)
        votes_per_million_tokens = config.get("votes_per_million_tokens", 1)
        vote_duration = config.get("vote_duration", 3600)
        votes_per_exempt = config.get("votes_per_exempt", 1)

        nft_trait_name = config.get("nft_trait_name", "")
        nft_trait_value = config.get("nft_trait_value", "")
        nft_trait_threshold = config.get("nft_trait_threshold", 1)

        cur.execute("""
            INSERT INTO subscriber_configs (chat_id, token, minimum_holding, decimals, wallets, auto_remove, 
                nft_collection_id, nft_threshold, registration_mode, votes_per_nft, 
                votes_per_million_tokens, vote_duration, votes_per_exempt,
                nft_trait_name, nft_trait_value, nft_trait_threshold)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(chat_id) DO UPDATE SET
                token=EXCLUDED.token,
                minimum_holding=EXCLUDED.minimum_holding,
                decimals=EXCLUDED.decimals,
                wallets=EXCLUDED.wallets,
                auto_remove=EXCLUDED.auto_remove,
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
              wallets_json, config.get("auto_remove", False), config.get("nft_collection_id", ""),
              config.get("nft_threshold", 1), config.get("registration_mode", "token"),
              votes_per_nft, votes_per_million_tokens, vote_duration, votes_per_exempt,
              nft_trait_name, nft_trait_value, nft_trait_threshold))
        return True


# ==================== Wallet Operations =============================
@db_retry
def save_wallet_for_user(group_id, user_id, username, wallet_list, is_exempt=False, replace_existing=False, registration_type="token"):
    with get_db_cursor() as (conn, cur):
        existing_wallets = []
        if not replace_existing:
            cur.execute("SELECT wallets, is_exempt FROM user_wallets WHERE group_id=%s AND user_id=%s", (group_id, user_id))
            result = cur.fetchone()
            if result and result[0]:
                try:
                    existing_wallets = json.loads(result[0])
                    logging.info(f"Found existing wallets for user {username}: {len(existing_wallets)} wallets")
                    is_exempt = result[1] or is_exempt
                except Exception as e:
                    logging.error(f"Error parsing existing wallets: {e}")
        if existing_wallets and not replace_existing:
            existing_lower = [w.lower() for w in existing_wallets]
            combined_wallets = []
            for wallet in wallet_list:
                if wallet.lower() not in existing_lower:
                    combined_wallets.append(wallet.lower())
                    existing_lower.append(wallet.lower())
            combined_wallets.extend(existing_wallets)
            seen = set()
            combined_wallets = [w for w in combined_wallets if not (w.lower() in seen or seen.add(w.lower()))]
        else:
            combined_wallets = [w.lower() for w in wallet_list]
        wallets_json = json.dumps(combined_wallets)
        total_wallets = len(combined_wallets)
        logging.info(f"Saving wallets for user {username}: {total_wallets} wallets total, exempt: {is_exempt}")
        cur.execute("""
            INSERT INTO user_wallets (group_id, user_id, username, wallets, is_exempt, registration_type)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT(group_id, user_id) DO UPDATE SET
                username=EXCLUDED.username,
                wallets=EXCLUDED.wallets,
                is_exempt=EXCLUDED.is_exempt,
                registration_type=EXCLUDED.registration_type
        """, (group_id, user_id, username, wallets_json, is_exempt, registration_type))
        cur.execute("SELECT wallets, is_exempt FROM user_wallets WHERE group_id=%s AND user_id=%s", (group_id, user_id))
        result = cur.fetchone()
        if result:
            logging.info(f"Verified wallets for user {username}: {result[0]}, exempt: {result[1]}")
            return True
        else:
            logging.error(f"Failed to verify wallet save for user {username}")
            return False


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
    """Persist last-known on-chain holdings for a user."""
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
    with get_db_cursor() as (conn, cur):
        cur.execute(
            f"UPDATE user_wallets SET {', '.join(updates)} WHERE group_id = %s AND user_id = %s",
            tuple(params),
        )


@db_retry
def get_user_cached_holdings(group_id, user_id):
    """Retrieve previously cached on-chain holdings for a user."""
    with get_db_cursor() as (conn, cur):
        cur.execute(
            "SELECT last_nft_count, last_trait_count, last_token_balance FROM user_wallets WHERE group_id = %s AND user_id = %s",
            (group_id, user_id),
        )
        result = cur.fetchone()
        if result:
            return {"nft_count": result[0], "trait_count": result[1], "token_balance": result[2]}
    return None


# ==================== Subscription DB Operations ====================
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


@db_retry
def activate_subscription(group_id, stripe_session_id, tier, activated_by, subscription_tiers):
    """Create or extend a subscription for a group."""
    tier_info = subscription_tiers.get(tier)
    if not tier_info:
        raise ValueError(f"Unknown subscription tier: {tier}")
    days = tier_info["days"]
    now = datetime.datetime.now(datetime.timezone.utc)
    existing = get_group_subscription(group_id)
    if existing and existing["expires_at"]:
        existing_exp = existing["expires_at"]
        if existing_exp.tzinfo is None:
            existing_exp = existing_exp.replace(tzinfo=datetime.timezone.utc)
        if existing_exp > now:
            new_expiry = existing_exp + datetime.timedelta(days=days)
        else:
            new_expiry = now + datetime.timedelta(days=days)
    else:
        new_expiry = now + datetime.timedelta(days=days)
    with get_db_cursor() as (conn, cur):
        cur.execute("""
            INSERT INTO subscriptions (group_id, stripe_session_id, tier, activated_at, expires_at, activated_by)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (group_id) DO UPDATE SET
                stripe_session_id = EXCLUDED.stripe_session_id,
                tier = EXCLUDED.tier,
                activated_at = EXCLUDED.activated_at,
                expires_at = EXCLUDED.expires_at,
                activated_by = EXCLUDED.activated_by
        """, (group_id, stripe_session_id, tier, now, new_expiry, activated_by))
    logging.info(f"Subscription activated for group {group_id}: tier={tier}, expires={new_expiry}")
    return new_expiry


@db_retry
def wallet_already_registered(wallet_address, group_id, user_id=None):
    """Check if a wallet is already registered in the group."""
    with get_db_cursor() as (conn, cur):
        cur.execute("SELECT user_id, wallets FROM user_wallets WHERE group_id=%s", (group_id,))
        rows = cur.fetchall()
    wallet_address = wallet_address.lower()
    for (row_user_id, wallets_json) in rows:
        try:
            wallets = json.loads(wallets_json) if wallets_json else []
        except Exception:
            wallets = []
        if wallet_address in [w.lower() for w in wallets]:
            if user_id is not None and row_user_id == user_id:
                return False
            return True
    return False


@db_retry
def toggle_user_exemption(group_id, user_id, exempt_status, get_username_func=None):
    """Toggle exemption status for a user.
    
    get_username_func: optional callable(group_id, user_id) -> str that fetches
    the username from Telegram when the user is not already in the DB.
    """
    with get_db_cursor() as (conn, cur):
        cur.execute("SELECT is_exempt FROM user_wallets WHERE group_id=%s AND user_id=%s", (group_id, user_id))
        result = cur.fetchone()

        if result:
            logging.info(f"Updating exemption for existing user {user_id} to {exempt_status}")
            cur.execute(
                "UPDATE user_wallets SET is_exempt = %s WHERE group_id = %s AND user_id = %s",
                (exempt_status, group_id, user_id)
            )
            return True

    # User not found - fetch username via callback if provided
    username = f"User{user_id}"
    if get_username_func:
        try:
            fetched = get_username_func(group_id, user_id)
            if fetched:
                username = fetched
        except Exception as e:
            logging.warning(f"Could not get user info for {user_id}: {e}, using default username")

    with get_db_cursor() as (conn, cur):
        logging.info(f"Creating new exemption record for user {username} ({user_id})")
        wallets_json = json.dumps([])
        cur.execute(
            "INSERT INTO user_wallets (group_id, user_id, username, wallets, is_exempt, registration_type) VALUES (%s, %s, %s, %s, %s, %s)",
            (group_id, user_id, username, wallets_json, exempt_status, 'exempt')
        )
    return True
