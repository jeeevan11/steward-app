"""Control layer: the human-in-the-loop surface.

This package owns everything that talks to *you* (the operator):

  * ``notifier``      — stdlib Telegram sender (notifications, approvals, asks).
  * ``briefs``        — morning/evening digest generation.
  * ``commands``      — free-text natural-language command parsing/dispatch.
  * ``telegram_bot``  — the python-telegram-bot polling app (slash commands,
                        inline-button callbacks, plain-text handling).

The action layer (sending, archiving, labelling) is dispatched *from* here but
lives under :mod:`assistant.action`; this package only decides *what* to surface
and *how* to react to your replies.
"""
