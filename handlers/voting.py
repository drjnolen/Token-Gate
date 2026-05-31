"""Voting handlers for Token-Gate bot.

Handles /votesetup, /vote commands, poll creation, and vote callbacks.
"""

import json
import logging
import time
import uuid
import datetime

from telebot import types

from db import get_db_cursor, db_retry, get_user_registration


def register(bot, deps):
    """Register voting-related handlers on the bot instance.

    Parameters
    ----------
    bot : telebot.TeleBot
    deps : dict
        Shared dependencies:
        - config_lock: threading.Lock
        - SUBSCRIBER_CONFIGS: dict
        - get_bot_username: callable
        - admin_required: decorator
        - poll_creation_context: dict
        - fetch_wallet_balances: callable
        - get_user_nft_count: callable
        - update_config_in_db: callable
    """
    config_lock = deps['config_lock']
    SUBSCRIBER_CONFIGS = deps['SUBSCRIBER_CONFIGS']
    get_bot_username = deps['get_bot_username']
    admin_required = deps['admin_required']
    poll_creation_context = deps['poll_creation_context']
    fetch_wallet_balances = deps['fetch_wallet_balances']
    get_user_nft_count = deps['get_user_nft_count']
    update_config_in_db = deps['update_config_in_db']

    @bot.message_handler(commands=['votesetup'])
    @admin_required
    def votesetup_command(message):
        markup = types.InlineKeyboardMarkup()
        deep_link = f"https://t.me/{get_bot_username()}?start=votesetup_{message.chat.id}"
        votesetup_btn = types.InlineKeyboardButton("🗳️ Configure Voting in Private Chat", url=deep_link)
        markup.add(votesetup_btn)

        message_thread_id = getattr(message, 'message_thread_id', None)
        text = "🗳️ **Voting Configuration**\n\nClick the button below to configure this group's voting settings in a private chat:"

        if message_thread_id:
            bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode="Markdown", message_thread_id=message_thread_id)
        else:
            bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode="Markdown")
        logging.info(f"Sent voting setup redirect to private chat for group {message.chat.id}")

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

        final_text = (
            f"{help_text}\n"
            "------\n"
            "**To create the poll, reply directly to this message with the details in the format above.**"
        )

        message_thread_id = getattr(message, 'message_thread_id', None)
        user_id = message.from_user.id
        poll_creation_context[user_id] = {
            'chat_id': message.chat.id,
            'message_thread_id': message_thread_id,
            'timestamp': time.time()
        }

        if message_thread_id:
            bot.send_message(message.chat.id, final_text, reply_markup=types.ForceReply(selective=True), parse_mode="Markdown", message_thread_id=message_thread_id)
        else:
            bot.send_message(message.chat.id, final_text, reply_markup=types.ForceReply(selective=True), parse_mode="Markdown")

    @bot.callback_query_handler(func=lambda call: call.data.startswith("poll_vote_"))
    def handle_poll_callback(call):
        """Handle poll voting callbacks specifically."""
        try:
            parts = call.data.split("_")
            if len(parts) == 4:
                poll_id = parts[2]
                option_index = int(parts[3])
                _handle_poll_vote(call, poll_id, option_index)
                return
            else:
                bot.answer_callback_query(call.id, "❌ Invalid poll action.")
                return
        except Exception as e:
            logging.error(f"Error in poll callback handler: {e}")
            bot.answer_callback_query(call.id, "❌ An error occurred.")

    @db_retry
    def _handle_poll_vote(call, poll_id, option_index):
        try:
            chat_id = call.message.chat.id
            user_id = call.from_user.id

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

                with config_lock:
                    config = SUBSCRIBER_CONFIGS.get(group_id, {})
                vote_duration = config.get("vote_duration", 3600)

                if created_at:
                    if isinstance(created_at, str):
                        created_at = datetime.datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                    elif hasattr(created_at, 'timestamp'):
                        pass
                    else:
                        created_at = datetime.datetime.now()

                    elapsed = (datetime.datetime.now(datetime.timezone.utc) - created_at.astimezone(datetime.timezone.utc)).total_seconds()
                    if elapsed > vote_duration:
                        cur.execute("UPDATE voting_polls SET is_active=FALSE WHERE poll_id=%s", (poll_id,))
                        bot.answer_callback_query(call.id, "❌ This poll has expired")
                        return

            vote_weight = _calculate_user_vote_weight(group_id, user_id)

            if vote_weight <= 0:
                bot.answer_callback_query(call.id, "❌ You need registered tokens/NFTs to vote or be exempt")
                return

            with get_db_cursor() as (conn, cur):
                cur.execute("""
                    INSERT INTO poll_votes (poll_id, user_id, option_index, vote_weight)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (poll_id, user_id) DO UPDATE SET
                        option_index=EXCLUDED.option_index,
                        vote_weight=EXCLUDED.vote_weight
                """, (poll_id, user_id, option_index, vote_weight))

            _update_poll_display(call.message, poll_id, title, options)
            bot.answer_callback_query(call.id, f"✅ Vote recorded! Your voting power: {vote_weight:.2f}")

        except Exception as e:
            logging.error(f"Error handling poll vote: {e}")
            bot.answer_callback_query(call.id, "❌ Error recording vote")

    @db_retry
    def _calculate_user_vote_weight(group_id, user_id):
        try:
            with config_lock:
                config = SUBSCRIBER_CONFIGS.get(group_id, {})
            token = config.get("token", "")
            decimals = config.get("decimals", 6)
            nft_collection_id = config.get("nft_collection_id", "")
            votes_per_nft = config.get("votes_per_nft", 1)
            votes_per_million = config.get("votes_per_million_tokens", 1)
            votes_per_exempt = config.get("votes_per_exempt", 1)

            user_reg = get_user_registration(group_id, user_id)

            if not user_reg:
                return 0

            if user_reg["is_exempt"]:
                return votes_per_exempt

            total_weight = 0
            wallets = user_reg["wallets"]
            if not wallets:
                return 0

            wallet_addresses = [w.lower() for w in wallets]

            if token and votes_per_million > 0:
                balances = fetch_wallet_balances(wallet_addresses, token, decimals)
                total_tokens = sum(balances.get(addr, 0) or 0 for addr in wallet_addresses)
                token_votes = (total_tokens / 1_000_000) * votes_per_million
                total_weight += token_votes

            if nft_collection_id and votes_per_nft > 0:
                nft_count = get_user_nft_count(wallet_addresses, nft_collection_id)
                if nft_count is None:
                    logging.warning(f"NFT count RPC failed for user {user_id} in vote weight calc, treating as 0")
                    nft_count = 0
                nft_votes = nft_count * votes_per_nft
                total_weight += nft_votes

            return total_weight

        except Exception as e:
            logging.error(f"Error calculating vote weight for user {user_id}: {e}")
            return 0

    @db_retry
    def _update_poll_display(message, poll_id, title, options):
        """Update the poll message with current vote tallies."""
        try:
            with get_db_cursor() as (conn, cur):
                cur.execute("SELECT option_index, SUM(vote_weight) FROM poll_votes WHERE poll_id=%s GROUP BY option_index", (poll_id,))
                vote_results = cur.fetchall()

            vote_totals = {i: 0 for i in range(len(options))}
            for option_idx, total_weight in vote_results:
                if option_idx in vote_totals:
                    vote_totals[option_idx] = total_weight or 0

            markup = types.InlineKeyboardMarkup()
            for i, option in enumerate(options):
                weight = vote_totals.get(i, 0)
                btn = types.InlineKeyboardButton(
                    f"{option} ({weight:.1f} votes)",
                    callback_data=f"poll_vote_{poll_id}_{i}"
                )
                markup.add(btn)

            poll_text = f"🗳️ *{title}*\n\n_Votes are weighted by token and NFT holdings_"

            bot.edit_message_text(
                poll_text,
                chat_id=message.chat.id,
                message_id=message.message_id,
                reply_markup=markup,
                parse_mode="Markdown"
            )
        except Exception as e:
            logging.error(f"Error updating poll display: {e}")

    # Expose functions needed by other modules
    deps['_voting_calculate_user_vote_weight'] = _calculate_user_vote_weight
    deps['_voting_update_poll_display'] = _update_poll_display
