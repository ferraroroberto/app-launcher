"""Logic layer — config, registry, scanner, bat-generator, diagnostics.

UI surfaces (`app/cli/`, `app/webapp/`, `app/tray/`) consume this package;
nothing in here imports any UI framework. See `CLAUDE.md` for the
`src/` <-> `app/` split convention shared across the monorepo.
"""

from .app_config import (
    AppConfig,
    load_app_config,
)

__all__ = [
    "AppConfig",
    "load_app_config",
]
