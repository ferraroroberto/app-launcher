"""Which providers the usage meter shows: the ``usage_providers`` setting.

Each provider is switched on or off by itself on the Code tab's Usage card;
an empty list hides the card. It replaced a single ``usage_shows`` choice,
which an older config file may still carry.
"""

from __future__ import annotations

from typing import Any, List

# The tuple order is the order the card draws them in.
VALID_USAGE_PROVIDERS = ("claude", "codex", "grok")
DEFAULT_USAGE_PROVIDERS = ("claude", "codex")

# The single choice this setting replaced, read once from an older file.
_LEGACY_USAGE_SHOWS = {
    "claude": ("claude",),
    "codex": ("codex",),
    "both": ("claude", "codex"),
    "none": (),
}


def load_usage_providers(raw: dict) -> List[str]:
    """The providers in card order, unknown names dropped.

    An empty list is kept (it hides the card), so this cannot ride the
    config loader's falsy-means-default rule. An older file's ``usage_shows``
    choice is carried over; the next save writes only ``usage_providers``.
    """
    authored = raw.get("usage_providers")
    if isinstance(authored, list):
        wanted = {str(item) for item in authored}
    elif "usage_shows" in raw:
        wanted = set(_LEGACY_USAGE_SHOWS.get(str(raw["usage_shows"]), DEFAULT_USAGE_PROVIDERS))
    else:
        wanted = set(DEFAULT_USAGE_PROVIDERS)
    return [name for name in VALID_USAGE_PROVIDERS if name in wanted]


def validate_usage_providers(value: Any) -> None:
    """Raise ``ValueError`` unless ``value`` lists only known providers."""
    if not isinstance(value, list) or any(name not in VALID_USAGE_PROVIDERS for name in value):
        raise ValueError(
            f"usage_providers must list only {VALID_USAGE_PROVIDERS}; got {value!r}"
        )
