"""Verification handlers for Token-Gate bot.

Handles wallet verification callbacks and the /register command flow.
"""

import logging
import time

from telebot import types

from db import get_db_cursor, save_wallet_for_user


def register(bot, deps):
    """Register verification-related handlers on the bot instance.

    Parameters
    ----------
    bot : telebot.TeleBot
    deps : dict
        Shared dependencies:
        - config_lock: threading.Lock
        - SUBSCRIBER_CONFIGS: dict
        - VERIFICATION_TIMEOUT: int
        - evaluate_wallet_requirements: callable
        - create_single_use_invite_link: callable
    """
    config_lock = deps['config_lock']
    SUBSCRIBER_CONFIGS = deps['SUBSCRIBER_CONFIGS']
    VERIFICATION_TIMEOUT = deps['VERIFICATION_TIMEOUT']
    evaluate_wallet_requirements = deps['evaluate_wallet_requirements']
    create_single_use_invite_link = deps['create_single_use_invite_link']

    @bot.callback_query_handler(func=lambda call: call.data.startswith("verify_wallet_"))
    def handle_verify_wallet_callback(call):
        """Handle wallet verification confirmation via inline button."""
        try:
            user_id = call.from_user.id

            parts = call.data.split("_")
            if len(parts) != 3:
                bot.answer_callback_query(call.id, "❌ Invalid verification request.")
                return

            expected_user_id = int(parts[2])
            if user_id != expected_user_id:
                bot.answer_callback_query(call.id, "❌ This verification is not for you.")
                return

            with get_db_cursor() as (conn, cur):
                cur.execute("SELECT group_id, wallet_address, created_at FROM pending_verifications WHERE user_id = %s", (user_id,))
                verification_data = cur.fetchone()

            if not verification_data:
                bot.answer_callback_query(call.id, "❌ No pending verification found.")
                bot.edit_message_text(
                    "❌ No pending wallet verification found.\n\n"
                    "Please use /register to start the verification process.",
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id
                )
                return

            group_id, wallet_address, timestamp = verification_data

            if not wallet_address or not isinstance(wallet_address, str):
                bot.answer_callback_query(call.id, "❌ Invalid wallet data.")
                with get_db_cursor() as (conn, cur):
                    cur.execute("DELETE FROM pending_verifications WHERE user_id = %s", (user_id,))
                bot.edit_message_text(
                    "❌ Invalid wallet address in verification data.\n\n"
                    "Please use /register to start the verification process again.",
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id
                )
                return

            if not timestamp or time.time() - timestamp.timestamp() > VERIFICATION_TIMEOUT:
                with get_db_cursor() as (conn, cur):
                    cur.execute("DELETE FROM pending_verifications WHERE user_id = %s", (user_id,))
                bot.answer_callback_query(call.id, "⏰ Verification timed out.")
                bot.edit_message_text(
                    "❌ Verification timed out.\n\n"
                    "Please use /register to start the verification process again.",
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id
                )
                return

            bot.answer_callback_query(call.id, "Re-checking on-chain holdings...")
            bot.edit_message_text(
                "⏳ Re-validating your on-chain token/NFT holdings...",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id
            )

            try:
                with config_lock:
                    cfg = SUBSCRIBER_CONFIGS.get(group_id)
                if not cfg:
                    bot.edit_message_text(
                        "❌ This group isn't set up yet. Ask an admin to run /cwconfig first.",
                        chat_id=call.message.chat.id,
                        message_id=call.message.message_id
                    )
                    return

                requirement_eval = evaluate_wallet_requirements(wallet_address, cfg, user_id=user_id, force_fresh=True)
                if not requirement_eval["requirements_met"]:
                    error_text = "❌ *Wallet Requirements Not Met*\n\n" + "\n".join(
                        requirement_eval["errors"] or ["Please retry after updating your holdings."]
                    )
                    if requirement_eval["details"]:
                        error_text += "\n\n📋 *Current Check Details:*\n" + "\n".join(requirement_eval["details"])
                    bot.edit_message_text(
                        error_text,
                        chat_id=call.message.chat.id,
                        message_id=call.message.message_id,
                        parse_mode="Markdown"
                    )
                    return

                verification_details = requirement_eval["details"]

                success = save_wallet_for_user(
                    group_id,
                    user_id,
                    call.from_user.username or call.from_user.first_name,
                    [wallet_address.lower()],
                    replace_existing=False,
                    registration_type=cfg.get("registration_mode", "token")
                )

                if not success:
                    bot.edit_message_text(
                        "❌ Failed to save your wallet. Please try again later.",
                        chat_id=call.message.chat.id,
                        message_id=call.message.message_id
                    )
                    return

                try:
                    chat_obj = bot.get_chat(group_id)
                    group_name = chat_obj.title
                except Exception as e:
                    logging.warning(f"Could not resolve group title for {group_id}: {e}")
                    group_name = f"Group {group_id}"

                text_lines = [
                    "✅ *Wallet Verification Successful!*",
                    "",
                    f"*Group:* {group_name}",
                    f"*Wallet:* `{wallet_address}`",
                ]

                if verification_details:
                    text_lines.append("")
                    text_lines.append("📋 *Verification Details:*")
                    text_lines.extend(verification_details)

                text_lines.append("")
                text_lines.append("Your wallet has been registered. You can now participate in group activities!")

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
                    logging.error(f"Error fetching invite link for group {group_id}: {e}")

                bot.edit_message_text(
                    "\n".join(text_lines),
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    parse_mode="Markdown"
                )

                with get_db_cursor() as (conn, cur):
                    cur.execute("""
                        UPDATE pending_verifications 
                        SET wallet_address = NULL, created_at = NOW()
                        WHERE user_id = %s
                    """, (user_id,))

                logging.info(f"Wallet verification successful for user {user_id}, wallet {wallet_address}")

            except Exception as e:
                logging.error(f"Error during callback verification: {e}")
                try:
                    bot.edit_message_text(
                        "❌ Error confirming verification. Please try again later.",
                        chat_id=call.message.chat.id,
                        message_id=call.message.message_id
                    )
                except Exception:
                    pass

        except Exception as e:
            logging.error(f"Error in handle_verify_wallet_callback: {e}")
            bot.answer_callback_query(call.id, "❌ An error occurred.")
