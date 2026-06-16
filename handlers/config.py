"""Configuration handlers for Token-Gate bot.

Handles /cwconfig command and the private config callback menu.
"""

import logging
import time

from telebot import types

from db import get_db_cursor, get_user_registration, toggle_user_exemption


def register(bot, deps):
    """Register configuration-related handlers on the bot instance.

    Parameters
    ----------
    bot : telebot.TeleBot
    deps : dict
        Shared dependencies:
        - config_lock: threading.Lock
        - SUBSCRIBER_CONFIGS: dict
        - get_bot_username: callable
        - is_group_admin: callable
        - admin_required: decorator
        - group_has_active_subscription: callable
        - show_subscription_prompt: callable
        - show_config_menu_private: callable
        - display_wallet_holdings: callable
        - display_settings: callable
        - display_voting_settings: callable
        - display_exemption_manager: callable
        - create_registration_link: callable
        - process_set_token_config: callable
        - process_set_nft_collection: callable
        - process_set_nft_threshold: callable
        - process_set_trait_name: callable
        - process_set_trait_value: callable
        - process_set_trait_threshold: callable
        - process_set_votes_per_nft: callable
        - process_set_votes_per_million: callable
        - process_set_vote_duration: callable
        - process_set_votes_per_exempt: callable
    """
    config_lock = deps['config_lock']
    SUBSCRIBER_CONFIGS = deps['SUBSCRIBER_CONFIGS']
    get_bot_username = deps['get_bot_username']
    admin_required = deps['admin_required']
    group_has_active_subscription = deps['group_has_active_subscription']
    show_subscription_prompt = deps['show_subscription_prompt']
    show_config_menu_private = deps['show_config_menu_private']
    display_wallet_holdings = deps['display_wallet_holdings']
    display_settings = deps['display_settings']
    display_voting_settings = deps['display_voting_settings']
    display_exemption_manager = deps['display_exemption_manager']
    create_registration_link = deps['create_registration_link']
    ensure_config_exists = deps['ensure_config_exists']
    update_config_in_db = deps['update_config_in_db']
    process_set_token_config = deps['process_set_token_config']
    process_set_nft_collection = deps['process_set_nft_collection']
    process_set_nft_threshold = deps['process_set_nft_threshold']
    process_set_trait_name = deps['process_set_trait_name']
    process_set_trait_value = deps['process_set_trait_value']
    process_set_trait_threshold = deps['process_set_trait_threshold']
    process_set_votes_per_nft = deps['process_set_votes_per_nft']
    process_set_votes_per_million = deps['process_set_votes_per_million']
    process_set_vote_duration = deps['process_set_vote_duration']
    process_set_votes_per_exempt = deps['process_set_votes_per_exempt']

    @bot.message_handler(commands=['cwconfig'])
    @admin_required
    def config_command(message):
        markup = types.InlineKeyboardMarkup()
        deep_link = f"https://t.me/{get_bot_username()}?start=config_{message.chat.id}"
        config_btn = types.InlineKeyboardButton("⚙️ Configure in Private Chat", url=deep_link)
        markup.add(config_btn)

        message_thread_id = getattr(message, 'message_thread_id', None)
        text = "🔧 **Group Configuration**\n\nClick the button below to configure this group's settings in a private chat:"

        if message_thread_id:
            bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode="Markdown", message_thread_id=message_thread_id)
        else:
            bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode="Markdown")
        logging.info(f"Sent config redirect to private chat for group {message.chat.id}")

    @bot.callback_query_handler(func=lambda call: call.data.startswith("privconfig_") or call.data.startswith("config_") or call.data.startswith("privvote_"))
    def handle_private_config_callback(call):
        """Handles ALL callbacks from the private configuration menu."""
        try:
            if call.data.startswith("privconfig_") or call.data.startswith("privvote_"):
                parts = call.data.split("_")
                if len(parts) < 3:
                    bot.answer_callback_query(call.id, "❌ Invalid config action.")
                    return
                group_id = int(parts[1])
                action = parts[2]

            elif call.data.startswith("config_"):
                parts = call.data.split("_")
                with get_db_cursor() as (conn, cur):
                    cur.execute("SELECT group_id FROM pending_verifications WHERE user_id = %s", (call.from_user.id,))
                    result = cur.fetchone()
                    if not result:
                        bot.answer_callback_query(call.id, "❌ Group context lost. Please restart.")
                        return
                    group_id = result[0]
                action = parts[1] if len(parts) > 1 else ""

            else:
                parts = call.data.split("_")
                if len(parts) < 3:
                    bot.answer_callback_query(call.id, "❌ Invalid wallet action.")
                    return
                group_id = int(parts[1])
                action = parts[2]

            user_id = call.from_user.id

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
            elif action == "viewwallets":
                display_wallet_holdings(group_id, send_to_chat_id=call.message.chat.id)
            elif action == "viewsettings":
                if call.data.startswith("privvote_"):
                    display_voting_settings(group_id, send_to_chat_id=call.message.chat.id)
                else:
                    display_settings(group_id, send_to_chat_id=call.message.chat.id)
            elif action == "createreglink":
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
