"""The one symlink-or-junction test every path jail shares.

A link, or an NTFS junction (which ``is_symlink`` does not report), can point
out of the place it appears to be, so no jail follows or removes through one.
These are security checks: a second copy that drifts is a hole, which is why
the predicate lives here once.
"""

from __future__ import annotations

import os
from pathlib import Path


def is_link(path: Path) -> bool:
    """True for a symlink *or* a Windows junction."""
    return path.is_symlink() or bool(getattr(os.path, "isjunction", lambda _p: False)(path))
