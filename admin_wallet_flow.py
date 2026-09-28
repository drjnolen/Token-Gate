"""Private, explicitly confirmed admin wallet registration.

Drafts are process-local and expire; persistent registration uses the existing
transactional wallet writer. No next-step handlers intercept other commands.
"""

from dataclasses import dataclass, field
import logging
import secrets
import threading
import time

import psycopg2
from telebot import types

from verification_security import canonical_sui_address


@dataclass
class Draft:
    group_id: int
    expires_at: float
    token: str = field(default_factory=lambda: secrets.token_hex(12))
    request_id: int = field(default_factory=lambda: secrets.randbelow(2**31))
    phase: str = "select"
    member_id: int | None = None
    member_name: str = ""
    wallet: str = ""
    prompt_id: int | None = None
    busy: bool = False


class AdminWalletFlow:
    def __init__(self, bot, *, subscription_active, wallet_taken, save_wallet,
                 ttl_seconds=900, max_drafts=1000, clock=time.monotonic):
        self.bot = bot
        self.subscription_active = subscription_active
        self.wallet_taken = wallet_taken
        self.save_wallet = save_wallet
        self.ttl_seconds = ttl_seconds
        self.max_drafts = max_drafts
        self.clock = clock
        self.drafts = {}
        self.lock = threading.Lock()

    @staticmethod
    def _private(message, user_id):
        return bool(message and message.chat.type == "private"
                    and message.chat.id == user_id)

    def _expire(self):
        now = self.clock()
        for owner, draft in list(self.drafts.items()):
            if not draft.busy and draft.expires_at <= now:
                del self.drafts[owner]

    def _take(self, owner, *, token=None, request_id=None):
        with self.lock:
            self._expire()
            draft = self.drafts.get(owner)
            if (draft is None or draft.busy
                    or (token is not None and draft.token != token)
                    or (request_id is not None and draft.request_id != request_id)):
                return None
            draft.busy = True
            return draft

    def _release(self, draft):
        with self.lock:
            draft.busy = False

    def abandon(self, owner):
        """Leave this draft when the admin selects another config action."""
        with self.lock:
            draft = self.drafts.get(owner)
            if draft and not draft.busy:
                del self.drafts[owner]
            else:
                return
        if draft.phase != "done":
            self.bot.send_message(owner, "Member wallet form closed.", reply_markup=types.ReplyKeyboardRemove())

    def _authorized(self, owner, draft):
        admin = self.bot.get_chat_member(draft.group_id, owner)
        if admin.status not in {"creator", "administrator"}:
            raise ValueError("Only current group administrators can add wallets.")
        if not self.subscription_active(draft.group_id):
            raise ValueError("This group's subscription is inactive. Reopen /cwconfig.")

    def _member(self, draft, member_id):
        member = self.bot.get_chat_member(draft.group_id, member_id)
        active = member.status in {"creator", "administrator", "member"} or (
            member.status == "restricted" and getattr(member, "is_member", False)
        )
        if not active or member.user.is_bot:
            raise ValueError("Select a person who is currently a member of this group.")
        return member.user

    @staticmethod
    def _name(user):
        name = " ".join(filter(None, [user.first_name, getattr(user, "last_name", None)]))
        username = getattr(user, "username", None)
        return f"{name} (@{username})" if username else name or str(user.id)

    def _buttons(self, draft, *, confirm=False):
        markup = types.InlineKeyboardMarkup()
        if confirm:
            markup.add(types.InlineKeyboardButton(
                "Confirm and add wallet", callback_data=f"adminwallet_save_{draft.token}"))
        markup.add(types.InlineKeyboardButton(
            "Cancel", callback_data=f"adminwallet_cancel_{draft.token}"))
        return markup

    def start(self, message, owner, group_id):
        if not self._private(message, owner):
            self.bot.send_message(message.chat.id, "Open /cwconfig in private chat to add a member wallet.")
            return
        with self.lock:
            self._expire()
            previous = self.drafts.get(owner)
            if previous and previous.busy:
                unavailable = "Your previous request is still processing."
            elif previous is None and len(self.drafts) >= self.max_drafts:
                unavailable = "Please try again shortly."
            else:
                unavailable = None
                draft = Draft(group_id, self.clock() + self.ttl_seconds, busy=True)
                self.drafts[owner] = draft
        if unavailable:
            self.bot.send_message(owner, unavailable)
            return
        try:
            self._authorized(owner, draft)
            title = self.bot.get_chat(group_id).title
            # Explicitly starting this form supersedes an unfinished config
            # prompt; legacy next-step handlers run before regular handlers.
            self.bot.clear_step_handler_by_chat_id(owner)
            picker = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
            picker.add(types.KeyboardButton("Choose member", request_users=types.KeyboardButtonRequestUsers(
                request_id=draft.request_id, user_is_bot=False, max_quantity=1,
                request_name=True, request_username=True,
            )))
            self.bot.send_message(owner,
                f"Add a member wallet to {title} (group {group_id}).\n"
                "Choose a person below. Their membership will be checked.\n"
                "This adds an admin-approved wallet without a signature.\n"
                f"This form expires in {self.ttl_seconds // 60} minutes. Send /cancel to stop.", reply_markup=picker)
            self.bot.send_message(owner, "You can cancel at any point.", reply_markup=self._buttons(draft))
        except Exception as exc:
            with self.lock:
                self.drafts.pop(owner, None)
            self._error(owner, exc)
        finally:
            self._release(draft)

    def _error(self, owner, error):
        logging.warning("Admin wallet flow failed (%s)", type(error).__name__)
        text = str(error) if isinstance(error, ValueError) else (
            "Could not complete that step. Please retry, or reopen Add member wallet in /cwconfig.")
        self.bot.send_message(owner, text)

    def shared(self, message):
        owner = message.from_user.id
        if not self._private(message, owner):
            return
        draft = self._take(owner, request_id=message.users_shared.request_id)
        if not draft:
            # An old selection must not remove the keyboard of a newer draft.
            self.bot.send_message(owner, "That selection expired or is no longer active. Use the latest picker or reopen Add member wallet.")
            return
        try:
            if draft.phase != "select":
                return
            self._authorized(owner, draft)
            users = message.users_shared.users
            if len(users) != 1:
                raise ValueError("Please choose exactly one member.")
            user = self._member(draft, users[0].user_id)
            draft.member_id = user.id
            draft.member_name = self._name(user)
            self.bot.send_message(owner, f"Selected: {draft.member_name}\nTelegram ID: {user.id}",
                                  reply_markup=types.ReplyKeyboardRemove())
            prompt = self.bot.send_message(owner, "Reply to this message with the member's Sui wallet address.",
                                           reply_markup=types.ForceReply(selective=True))
            draft.prompt_id = prompt.message_id
            draft.phase = "wallet"
        except Exception as exc:
            self._error(owner, exc)
        finally:
            self._release(draft)

    def accepts_text(self, message):
        if not self._private(message, message.from_user.id):
            return False
        text = message.text or ""
        reply = message.reply_to_message
        with self.lock:
            self._expire()
            draft = self.drafts.get(message.from_user.id)
            return bool(draft and (text.split("@", 1)[0] == "/cancel" or (
                draft.phase == "wallet" and reply and reply.message_id == draft.prompt_id
                and not text.startswith("/")
            )))

    def text(self, message):
        owner = message.from_user.id
        if not self.accepts_text(message):
            return
        draft = self._take(owner)
        if not draft:
            return
        try:
            if message.text.split("@", 1)[0] == "/cancel":
                self._cancel(owner, draft)
                return
            self._authorized(owner, draft)
            wallet = canonical_sui_address(message.text)
            if not wallet:
                raise ValueError("Invalid Sui address. Reply to the wallet prompt again, or send /cancel.")
            if self.wallet_taken(wallet, draft.group_id, user_id=draft.member_id):
                raise ValueError("That wallet is already registered to another member of this group.")
            title = self.bot.get_chat(draft.group_id).title
            draft.wallet = wallet
            self.bot.send_message(owner,
                f"Confirm wallet registration\n\nGroup: {title} ({draft.group_id})\n"
                f"Member: {draft.member_name}\nTelegram ID: {draft.member_id}\nWallet: {wallet}\n\n"
                "Existing wallets will be kept. Wallet ownership has not been verified by signature.",
                reply_markup=self._buttons(draft, confirm=True))
            draft.phase = "confirm"
        except Exception as exc:
            self._error(owner, exc)
        finally:
            self._release(draft)

    def _cancel(self, owner, draft):
        with self.lock:
            self.drafts.pop(owner, None)
        self.bot.send_message(owner, "Member wallet entry cancelled.", reply_markup=types.ReplyKeyboardRemove())

    def callback(self, call):
        owner = call.from_user.id
        if not self._private(call.message, owner):
            self.bot.answer_callback_query(call.id, "Use the private configuration chat.")
            return
        parts = call.data.split("_")
        if len(parts) != 3 or parts[1] not in {"save", "cancel"}:
            self.bot.answer_callback_query(call.id, "Invalid action.")
            return
        draft = self._take(owner, token=parts[2])
        if not draft:
            self.bot.answer_callback_query(call.id, "Expired, replaced, or already processing. Reopen Add member wallet.")
            return
        try:
            self.bot.answer_callback_query(call.id)
            if draft.phase == "done":
                self.bot.send_message(owner, "This wallet was already added successfully.")
                return
            if parts[1] == "cancel":
                self._cancel(owner, draft)
                return
            if draft.phase != "confirm":
                return
            self._authorized(owner, draft)
            user = self._member(draft, draft.member_id)
            if self.wallet_taken(draft.wallet, draft.group_id, user_id=user.id):
                raise ValueError("That wallet is already registered to another member of this group.")
            if not self.save_wallet(draft.group_id, user, draft.wallet):
                raise RuntimeError("Registration was not saved")
            # Set before sending: a failed Telegram response must not allow a
            # second write on repeated confirmation clicks.
            draft.phase = "done"
            back = types.InlineKeyboardMarkup()
            back.add(types.InlineKeyboardButton("Back to configuration",
                callback_data=f"privconfig_{draft.group_id}_back"))
            self.bot.send_message(owner,
                f"Wallet added successfully for {self._name(user)} (Telegram ID {user.id})\n"
                f"Group: {draft.group_id}\nWallet: {draft.wallet}\nExisting wallets were preserved.",
                reply_markup=back)
        except psycopg2.IntegrityError:
            self._error(owner, ValueError("That wallet is already registered to another member of this group."))
        except Exception as exc:
            self._error(owner, exc)
        finally:
            self._release(draft)

    def register(self):
        self.bot.register_message_handler(self.shared, content_types=["users_shared"])
        self.bot.register_message_handler(self.text, content_types=["text"], func=self.accepts_text)
        self.bot.register_callback_query_handler(self.callback,
            func=lambda call: bool(call.data and call.data.startswith("adminwallet_")))
