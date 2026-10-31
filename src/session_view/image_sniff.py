"""The one place that decides whether a file's bytes prove it's an image
(#1627): shared by ``src.session_view.transcript``'s inline transcript images and
``src.session_view.outbox``'s sent-file outbox, so the two never disagree on what a
served file may claim to be.
"""

from __future__ import annotations

from typing import Optional

IMAGE_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def sniff_image(data: bytes) -> Optional[str]:
    """The media type of an allowlisted image format, from its own bytes."""
    for magic, media in IMAGE_MAGIC:
        if data.startswith(magic):
            return media
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None
