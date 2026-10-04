"""Expiring, owner-bound private replies for administrator settings."""

from dataclasses import dataclass
import logging
import threading
import time


@dataclass
class ConfigPrompt:
    group_id: int
    message_id: int
    callback: object
    expires_at: float


class ConfigInputFlow:
    def __init__(self, bot, *, subscription_active, ttl_seconds=900,
                 max_drafts=1000, clock=time.monotonic):
        self.bot = bot
        self.subscription_active = subscription_active
        self.ttl_seconds = ttl_seconds
        self.max_drafts = max_drafts
        self.clock = clock
        self.prompts = {}
        self.lock = threading.Lock()

    def abandon(self, owner):
        with self.lock:
            self.prompts.pop(owner, None)

    def expect(self, prompt, callback, group_id):
        owner = prompt.chat.id
        if prompt.chat.type != "private":
            raise ValueError("Open configuration in private chat")
        with self.lock:
            self.prompts = {k: v for k, v in self.prompts.items()
                            if v.expires_at > self.clock()}
            if owner not in self.prompts and len(self.prompts) >= self.max_drafts:
                raise ValueError("Too many active settings prompts; retry shortly")
            self.prompts[owner] = ConfigPrompt(
                group_id, prompt.message_id, callback, self.clock() + self.ttl_seconds)
        self.bot.clear_step_handler_by_chat_id(owner)

    def accepts(self, message):
        if message.chat.type != "private" or message.chat.id != message.from_user.id:
            return False
        with self.lock:
            prompt = self.prompts.get(message.from_user.id)
            reply = getattr(message, "reply_to_message", None)
            return bool(prompt and (message.text == "/cancel" or (
                reply and reply.message_id == prompt.message_id)))

    def handle(self, message):
        owner = message.from_user.id
        with self.lock:
            prompt = self.prompts.get(owner)
            reply = getattr(message, "reply_to_message", None)
            if (message.chat.type != "private" or message.chat.id != owner or not prompt
                    or (message.text != "/cancel" and (
                        not reply or reply.message_id != prompt.message_id))):
                return
            # Consume before performing external work: duplicate deliveries cannot write twice.
            self.prompts.pop(owner)
        if message.text == "/cancel":
            self.bot.send_message(owner, "Settings prompt cancelled.")
            return
        if prompt.expires_at <= self.clock():
            self.bot.send_message(owner, "That settings prompt expired. Reopen /cwconfig.")
            return
        if not message.text or message.text.startswith("/"):
            self.bot.send_message(owner, "Settings unchanged. Reopen /cwconfig to edit.")
            return
        try:
            member = self.bot.get_chat_member(prompt.group_id, owner)
            if member.status not in {"creator", "administrator"}:
                self.bot.send_message(owner, "Only current group administrators can change settings.")
                return
            if not self.subscription_active(prompt.group_id):
                self.bot.send_message(owner, "This group's subscription is inactive. Settings unchanged.")
                return
            prompt.callback(message, prompt.group_id)
        except Exception:
            logging.exception("Settings reply failed for group %s", prompt.group_id)
            self.bot.send_message(owner, "Could not save that setting. Reopen /cwconfig and retry.")

    def register_handlers(self):
        self.bot.register_message_handler(self.handle, func=self.accepts, content_types=["text"])
