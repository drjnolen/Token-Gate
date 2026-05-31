"""Subscription handlers for Token-Gate bot.

Handles subscription tier selection, Stripe checkout callbacks, and
subscription prompt display.
"""

import logging

from telebot import types

from db import activate_subscription


def register(bot, deps):
    """Register subscription-related handlers on the bot instance.

    Parameters
    ----------
    bot : telebot.TeleBot
    deps : dict
        Shared dependencies:
        - SUBSCRIPTION_TIERS: dict
        - STRIPE_SECRET_KEY: str
        - create_stripe_checkout_session: callable
        - show_subscription_prompt: callable
    """
    SUBSCRIPTION_TIERS = deps['SUBSCRIPTION_TIERS']
    STRIPE_SECRET_KEY = deps['STRIPE_SECRET_KEY']
    create_stripe_checkout_session = deps['create_stripe_checkout_session']
    show_subscription_prompt = deps['show_subscription_prompt']

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
