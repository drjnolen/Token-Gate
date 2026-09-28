import ast
import json
from pathlib import Path
from types import SimpleNamespace as NS
import threading
import unittest
from unittest.mock import Mock

import psycopg2
import telebot

from admin_wallet_flow import AdminWalletFlow
from verification_security import canonical_sui_address


def user(uid, name="Member"):
    return NS(id=uid, first_name=name, last_name=None, username=None, is_bot=False)


class AdminWalletFlowTests(unittest.TestCase):
    def setUp(self):
        self.owner = user(10, "Admin")
        self.member = user(20)
        self.bot = Mock()
        self.bot.get_chat.return_value = NS(title="Group A")
        self.bot.get_chat_member.side_effect = lambda group, uid: NS(
            status="administrator" if uid == 10 else "member", user=user(uid))
        self.bot.send_message.return_value = NS(message_id=111)
        self.save = Mock(return_value=True)
        self.taken = Mock(return_value=False)
        self.subscription = Mock(return_value=True)
        self.now = 0
        self.flow = AdminWalletFlow(self.bot, subscription_active=self.subscription,
            wallet_taken=self.taken, save_wallet=self.save, clock=lambda: self.now)
        self.message = NS(chat=NS(id=10, type="private"), from_user=self.owner,
                          text="", reply_to_message=None)

    def start(self, group=-100):
        self.flow.start(self.message, 10, group)
        return self.flow.drafts[10]

    def share(self, draft, ids=(20,), request_id=None):
        self.flow.shared(NS(chat=self.message.chat, from_user=self.owner,
            users_shared=NS(request_id=draft.request_id if request_id is None else request_id,
                            users=[NS(user_id=uid) for uid in ids])))

    def address(self, value="0x123", prompt_id=111):
        self.flow.text(NS(chat=self.message.chat, from_user=self.owner, text=value,
                          reply_to_message=NS(message_id=prompt_id)))

    def click(self, draft, action="save", owner=None):
        self.flow.callback(NS(id="callback", from_user=owner or self.owner,
            message=self.message, data=f"adminwallet_{action}_{draft.token}"))

    def ready(self):
        draft = self.start()
        self.share(draft)
        self.address()
        self.assertEqual(draft.phase, "confirm")
        self.save.assert_not_called()
        return draft

    def test_picker_requests_one_non_bot_and_clears_legacy_prompt(self):
        draft = self.start()
        markup = json.loads(self.bot.send_message.call_args_list[0].kwargs["reply_markup"].to_json())
        request = markup["keyboard"][0][0]["request_users"]
        self.assertEqual(request["request_id"], draft.request_id)
        self.assertEqual(request["max_quantity"], 1)
        self.assertIs(request["user_is_bot"], False)
        self.bot.clear_step_handler_by_chat_id.assert_called_once_with(10)

    def test_full_flow_waits_for_confirmation_and_preserves_group_identity(self):
        draft = self.ready()
        confirmation = self.bot.send_message.call_args.args[1]
        for value in ("Group A (-100)", "Telegram ID: 20", canonical_sui_address("0x123")):
            self.assertIn(value, confirmation)
        self.click(draft)
        self.assertEqual(draft.phase, "done")
        args = self.save.call_args.args
        self.assertEqual((args[0], args[1].id, args[2]), (-100, 20, canonical_sui_address("0x123")))
        self.click(draft)
        self.save.assert_called_once()

    def test_old_picker_cannot_change_replacement_group(self):
        old = self.start(-100)
        new = self.start(-200)
        self.share(old)
        self.assertEqual(new.phase, "select")
        self.assertIsNone(new.member_id)

    def test_old_confirmation_cannot_save_replacement_draft(self):
        old = self.ready()
        self.start(-200)
        self.click(old)
        self.save.assert_not_called()

    def test_other_user_and_group_chat_cannot_use_confirmation(self):
        draft = self.ready()
        self.click(draft, owner=user(99))
        self.message.chat.type = "supergroup"
        self.click(draft)
        self.save.assert_not_called()

    def test_expired_confirmation_cannot_write(self):
        draft = self.ready()
        self.now = 901
        self.click(draft)
        self.save.assert_not_called()

    def test_selection_rejects_nonmembers_bots_and_multiple_users(self):
        for status, is_member, bot_user in [("left", False, False), ("kicked", False, False),
                                           ("restricted", False, False), ("member", True, True)]:
            with self.subTest(status=status, is_bot=bot_user):
                draft = self.start()
                target = user(20)
                target.is_bot = bot_user
                self.bot.get_chat_member.side_effect = lambda group, uid, status=status, is_member=is_member, target=target: (
                    NS(status="administrator", user=self.owner) if uid == 10 else
                    NS(status=status, is_member=is_member, user=target))
                self.share(draft)
                self.assertEqual(draft.phase, "select")
        draft = self.start()
        self.share(draft, ids=(20, 21))
        self.assertEqual(draft.phase, "select")

    def test_restricted_current_member_can_be_selected(self):
        draft = self.start()
        self.bot.get_chat_member.side_effect = lambda group, uid: NS(
            status="administrator" if uid == 10 else "restricted", is_member=True, user=user(uid))
        self.share(draft)
        self.assertEqual(draft.phase, "wallet")

    def test_rechecks_admin_and_target_at_save(self):
        for denied_id in (10, 20):
            with self.subTest(denied_id=denied_id):
                self.setUp()
                draft = self.ready()
                self.bot.get_chat_member.side_effect = lambda group, uid, denied_id=denied_id: NS(
                    status="left" if uid == denied_id else "administrator", user=user(uid))
                self.click(draft)
                self.save.assert_not_called()

    def test_subscription_expiry_or_telegram_failure_prevents_save(self):
        draft = self.ready()
        self.subscription.return_value = False
        self.click(draft)
        self.save.assert_not_called()
        self.subscription.return_value = True
        self.bot.get_chat_member.side_effect = RuntimeError("Telegram unavailable")
        self.click(draft)
        self.save.assert_not_called()

    def test_invalid_wallet_and_other_prompt_are_not_saved(self):
        draft = self.start()
        self.share(draft)
        self.address("0x1-2")
        self.assertEqual(draft.phase, "wallet")
        self.address(prompt_id=999)
        self.assertEqual(draft.phase, "wallet")
        self.address()
        self.assertEqual(draft.phase, "confirm")

    def test_duplicate_at_entry_or_confirmation_blocks_save(self):
        draft = self.start()
        self.share(draft)
        self.taken.return_value = True
        self.address()
        self.assertEqual(draft.phase, "wallet")
        self.taken.return_value = False
        self.address()
        self.taken.return_value = True
        self.click(draft)
        self.save.assert_not_called()

    def test_database_failure_is_retryable_and_unique_race_is_reported(self):
        draft = self.ready()
        self.save.side_effect = psycopg2.IntegrityError("unique race")
        self.click(draft)
        self.assertIn("another member", self.bot.send_message.call_args.args[1])
        self.assertEqual(draft.phase, "confirm")
        self.save.side_effect = RuntimeError("database unavailable")
        self.click(draft)
        self.assertEqual(draft.phase, "confirm")
        self.save.side_effect = None
        self.click(draft)
        self.assertEqual(draft.phase, "done")

    def test_cancel_and_navigation_invalidate_drafts(self):
        draft = self.ready()
        self.click(draft, "cancel")
        self.click(draft)
        self.save.assert_not_called()
        self.start()
        self.flow.abandon(10)
        self.assertNotIn(10, self.flow.drafts)

    def test_save_is_single_flight(self):
        draft = self.ready()
        entered, release = threading.Event(), threading.Event()
        def save(*args):
            entered.set()
            release.wait(3)
            return True
        self.save.side_effect = save
        thread = threading.Thread(target=self.click, args=(draft,))
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            self.click(draft)
            self.click(draft, "cancel")
            self.save.assert_called_once()
        finally:
            release.set()
            thread.join(3)
        self.assertEqual(draft.phase, "done")

    def test_lost_success_message_does_not_repeat_save(self):
        draft = self.ready()
        self.bot.send_message.side_effect = [RuntimeError("delivery failed"), NS(message_id=112)]
        self.click(draft)
        self.bot.send_message.side_effect = None
        self.click(draft)
        self.save.assert_called_once()

    def test_real_telegram_dispatch_does_not_capture_other_commands(self):
        bot = telebot.TeleBot("1234:test", threaded=False)
        flow = AdminWalletFlow(bot, subscription_active=self.subscription,
                              wallet_taken=self.taken, save_wallet=self.save)
        flow.register()
        flow.drafts[10] = self.ready()
        other_handler = Mock()
        bot.register_message_handler(other_handler, commands=["mywallets"])
        payload = {"message_id": 2, "date": 1, "chat": {"id": 10, "type": "private"},
                   "from": {"id": 10, "is_bot": False, "first_name": "Admin"}, "text": "/mywallets"}
        bot.process_new_messages([telebot.types.Message.de_json(payload)])
        other_handler.assert_called_once()

    def test_cancel_command_does_not_save(self):
        self.ready()
        self.flow.text(NS(chat=self.message.chat, from_user=self.owner,
                          text="/cancel", reply_to_message=None))
        self.assertNotIn(10, self.flow.drafts)
        self.save.assert_not_called()

    def test_real_user_shared_dispatch_replaces_legacy_next_step(self):
        bot = telebot.TeleBot("1234:test", threaded=False)
        bot.send_message = self.bot.send_message
        bot.get_chat = self.bot.get_chat
        bot.get_chat_member = self.bot.get_chat_member
        flow = AdminWalletFlow(bot, subscription_active=self.subscription,
                              wallet_taken=self.taken, save_wallet=self.save)
        flow.register()
        old_prompt = Mock()
        bot.register_next_step_handler_by_chat_id(10, old_prompt)
        flow.start(self.message, 10, -100)
        draft = flow.drafts[10]
        payload = {"message_id": 2, "date": 1, "chat": {"id": 10, "type": "private"},
                   "from": {"id": 10, "is_bot": False, "first_name": "Admin"},
                   "users_shared": {"request_id": draft.request_id,
                                    "users": [{"user_id": 20, "first_name": "Untrusted name"}]}}
        bot.process_new_messages([telebot.types.Message.de_json(payload)])
        old_prompt.assert_not_called()
        self.assertEqual(draft.phase, "wallet")
        self.assertEqual(draft.member_id, 20)
        self.assertEqual(draft.member_name, "Member")

    def test_failed_initial_authorization_leaves_no_draft(self):
        self.subscription.return_value = False
        self.flow.start(self.message, 10, -100)
        self.assertNotIn(10, self.flow.drafts)
        self.bot.clear_step_handler_by_chat_id.assert_not_called()

    def test_draft_capacity_and_expiration(self):
        self.flow.max_drafts = 1
        self.start()
        second = NS(chat=NS(id=11, type="private"))
        self.flow.start(second, 11, -100)
        self.assertNotIn(11, self.flow.drafts)
        self.now = 901
        self.bot.get_chat_member.side_effect = lambda group, uid: NS(status="administrator", user=user(uid))
        self.flow.start(second, 11, -100)
        self.assertNotIn(10, self.flow.drafts)
        self.assertIn(11, self.flow.drafts)

    def test_existing_private_config_callback_routes_new_action(self):
        tree = ast.parse(Path("main.py").read_text(encoding="utf-8"))
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                        and node.name == "handle_private_config_callback")
        function.decorator_list = []
        flow = Mock()
        namespace = {"bot": self.bot, "group_has_active_subscription": self.subscription,
                     "_admin_wallet_flow": flow}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "main.py", "exec"), namespace)
        call = NS(id="callback", from_user=self.owner, message=self.message,
                  data="privconfig_-100_addmemberwallet")
        namespace["handle_private_config_callback"](call)
        flow.start.assert_called_once_with(self.message, 10, -100)
        flow.abandon.assert_not_called()

    def test_main_adapter_appends_and_preserves_registration_mode(self):
        tree = ast.parse(Path("main.py").read_text(encoding="utf-8"))
        tree.body = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                     and node.name == "_save_admin_selected_wallet"]
        writer = Mock(return_value=True)
        namespace = {"config_lock": threading.Lock(), "SUBSCRIBER_CONFIGS": {-100: {"registration_mode": "both"}},
                     "save_wallet_for_user": writer, "get_telegram_user_display_name": lambda u: u.first_name}
        exec(compile(tree, "main.py", "exec"), namespace)
        namespace["_save_admin_selected_wallet"](-100, self.member, "0x123")
        writer.assert_called_once_with(-100, 20, "Member", ["0x123"],
                                       replace_existing=False, registration_type="both")
