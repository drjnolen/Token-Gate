"""Verification handlers for Token-Gate bot.

Handles wallet verification callbacks and the /register command flow.
"""

import logging
import time

from telebot import types

from db import get_db_cursor, save_wallet_for_user


def register(bot, deps):
    """Register verification-related handlers on the bot instance."""
    pass
