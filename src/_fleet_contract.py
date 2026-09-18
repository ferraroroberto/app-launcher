"""Load a fleet-config contract module from a path (issue #1003).

``active_issue_claims`` and ``quota_usage`` each carried a byte-identical
copy of this loader — same ``lru_cache``, same
``spec_from_file_location`` / ``module_from_spec`` / ``exec_module``
sequence, same ``ImportError`` guard — differing only in the internal
module-name alias and the error message.

Both import a *contract* module out of the fleet-config checkout rather
than vendoring its logic, so the launcher and fleet-config cannot drift on
what a claim or a quota snapshot means. The alias keeps the two loaded
modules distinct in ``sys.modules`` terms.
"""

from __future__ import annotations

import importlib.util
from functools import lru_cache
from pathlib import Path
from types import ModuleType


@lru_cache(maxsize=4)
def load_fleet_contract(path_text: str, alias: str, label: str) -> ModuleType:
    """Import ``path_text`` as a module named ``alias``.

    ``label`` names the caller in the ``ImportError`` a missing or
    unloadable contract raises, so the failure still says which contract it
    was. Cached like the two copies it replaces: a missing fleet-config
    checkout is a steady state under a five-second board poll.
    """
    path = Path(path_text)
    spec = importlib.util.spec_from_file_location(alias, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"{label} contract loader unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
