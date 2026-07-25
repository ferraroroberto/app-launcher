"""Notification boundary — a vendored-verbatim, stdlib-only notifier primitive.

Copy this whole package into a consuming app's ``src/notify/`` unchanged; wire
your own config layer to :class:`TelegramConfig` and call ``send_text``. See
``README.md`` for the adopt-and-wire contract.
"""

from src.notify.base import Notifier, NotifierError
from src.notify.config import TelegramConfig
from src.notify.factory import build_notifier
from src.notify.telegram import TelegramNotifier

__all__ = [
    "Notifier",
    "NotifierError",
    "TelegramConfig",
    "TelegramNotifier",
    "build_notifier",
]
