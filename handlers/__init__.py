"""Handler package for Token-Gate bot.

Each sub-module exposes a ``register(bot, deps)`` function that attaches
Telegram message/callback handlers to the bot instance.  The *deps* dict
carries shared state (configs, locks, helper functions) so modules stay
decoupled from global variables in main.py.
"""
