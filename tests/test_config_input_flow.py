from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock

from config_input_flow import ConfigInputFlow


def message(owner=10, prompt=100, text="new value", private=True):
    return NS(from_user=NS(id=owner), chat=NS(id=owner if private else -100,
              type="private" if private else "supergroup"), text=text,
              reply_to_message=NS(message_id=prompt) if prompt else None, message_id=100)


class ConfigInputTests(unittest.TestCase):
    def setUp(self):
        self.bot = Mock()
        self.bot.get_chat_member.return_value = NS(status="administrator")
        self.subscription = Mock(return_value=True)
        self.clock = Mock(return_value=0)
        self.flow = ConfigInputFlow(self.bot, subscription_active=self.subscription, clock=self.clock)
        self.save = Mock()
        self.flow.expect(message(), self.save, -100)

    def test_owner_bound_reply_saves_once_with_current_authorization(self):
        reply = message()
        self.assertTrue(self.flow.accepts(reply))
        self.flow.handle(reply)
        self.flow.handle(reply)
        self.save.assert_called_once_with(reply, -100)
        self.bot.get_chat_member.assert_called_once_with(-100, 10)
        self.subscription.assert_called_once_with(-100)

    def test_nonreply_wrong_prompt_other_user_and_group_cannot_save(self):
        for reply in (message(prompt=None), message(prompt=99), message(owner=11), message(private=False)):
            self.assertFalse(self.flow.accepts(reply))
            self.flow.handle(reply)
        self.save.assert_not_called()
        self.assertIn(10, self.flow.prompts)

    def test_revoked_admin_and_expired_subscription_cannot_save(self):
        self.bot.get_chat_member.return_value.status = "member"
        self.flow.handle(message())
        self.save.assert_not_called()
        self.flow.expect(message(), self.save, -100)
        self.bot.get_chat_member.return_value.status = "administrator"
        self.subscription.return_value = False
        self.flow.handle(message())
        self.save.assert_not_called()

    def test_permission_lookup_failure_cannot_write(self):
        self.bot.get_chat_member.side_effect = RuntimeError("unavailable")
        with self.assertLogs(level="ERROR"):
            self.flow.handle(message())
        self.save.assert_not_called()

    def test_expired_cancelled_and_abandoned_prompts_cannot_write(self):
        self.clock.return_value = 901
        self.flow.handle(message())
        self.save.assert_not_called()
        self.flow.expect(message(), self.save, -100)
        self.flow.handle(message(text="/cancel", prompt=None))
        self.flow.handle(message())
        self.save.assert_not_called()
        self.flow.expect(message(), self.save, -100)
        self.flow.abandon(10)
        self.flow.handle(message())
        self.save.assert_not_called()

    def test_new_group_prompt_invalidates_old_group_reply(self):
        prompt = message()
        prompt.message_id = 101
        self.flow.expect(prompt, self.save, -200)
        self.flow.handle(message(prompt=100))
        self.save.assert_not_called()
        reply = message(prompt=101)
        self.flow.handle(reply)
        self.save.assert_called_once_with(reply, -200)

    def test_commands_do_not_become_setting_values(self):
        self.flow.handle(message(text="/help"))
        self.save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
