"""Notification interface — the universal, domain-free surface.

A *notifier* delivers a short plain-text message to an out-of-band channel
(Telegram, …). The contract is intentionally tiny: one method, ``send_text``.
Anything richer (digests, formatting, persistence, retry/queue state) is the
consuming app's concern and is layered *on top* of this — never baked in here —
so the primitive stays copy-verbatim across every fleet app.

On failure a notifier raises :class:`NotifierError`; the caller decides whether
to retry, log, or swallow it. Concrete delivery (Telegram) lives in
:mod:`src.notify.telegram`.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class NotifierError(RuntimeError):
    """Raised when a notifier fails to deliver a message (so the caller can react)."""


@runtime_checkable
class Notifier(Protocol):
    def send_text(self, text: str) -> None:
        """Deliver a one-line plain-text message. Raises :class:`NotifierError` on failure."""
        ...
