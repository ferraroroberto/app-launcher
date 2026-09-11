"""Issue #883: ``VALID_CLAUDE_MODELS`` is derived from the model catalog.

Claude used to be the one harness with two competing "which models are
valid" sources: a hand-written tuple in ``src/webapp_config.py`` and the
canonical ``CLAUDE_MODEL_SPECS`` catalog. The catalog drove the Coding
picker (``coding_model_choice``) and dispatch; the tuple drove
``_validate`` for ``claude_model``, ``--model`` in the launch flags, and the
Settings picker's ``models_available``. A routine model refresh — adding a
tier to the catalog, as ``model_catalog.py``'s docstring instructs — made the
new tier offerable in the picker while the save path threw on it.

The drift only shows up when the catalog *changes*, so the regression test
adds a tier to the catalog before ``src.webapp_config`` is first imported. It
runs in a fresh interpreter because the valid-model tuples are computed at
import time, and reloading the module in-process would leave every other
module holding the old ``WebappConfig`` class.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

from src.model_catalog import CLAUDE_MODEL_SPECS, available_values
from src.webapp_config import (
    LEGACY_CLAUDE_MODELS,
    VALID_CLAUDE_MODELS,
    VALID_CODING_MODEL_CHOICES,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent

_NEW_TIER_PROBE = textwrap.dedent(
    """
    import json
    from dataclasses import replace

    from src import model_catalog

    model_catalog.CLAUDE_MODEL_SPECS["probe-tier"] = {
        "label": "Probe", "efforts": ("low", "high"), "available": True,
    }

    from src.launch_flags import build_claude_flags
    from src.webapp_config import (
        VALID_CLAUDE_MODELS, VALID_CODING_MODEL_CHOICES, WebappConfig, _validate,
    )

    cfg = replace(
        WebappConfig(),
        coding_model_choice="claude:probe-tier",
        claude_model="probe-tier",
    )
    try:
        _validate(cfg)
        error = None
    except ValueError as exc:
        error = str(exc)
    print(json.dumps({
        "offered": "claude:probe-tier" in VALID_CODING_MODEL_CHOICES,
        "accepted": "probe-tier" in VALID_CLAUDE_MODELS,
        "validate_error": error,
        "flags": build_claude_flags(cfg),
    }))
    """
)


def test_a_new_catalog_tier_is_offered_saveable_and_launchable():
    """Adding a Claude tier to the catalog must make it pickable, savable,
    and passed as ``--model`` in one step, not just pickable."""
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", _NEW_TIER_PROBE],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        cwd=str(_REPO_ROOT),
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout.strip().splitlines()[-1])

    assert result["offered"], "the Coding picker must offer a new catalog tier"
    assert result["accepted"], "VALID_CLAUDE_MODELS must include a new catalog tier"
    assert result["validate_error"] is None, result["validate_error"]
    assert "--model probe-tier" in result["flags"]


def test_valid_claude_models_is_the_catalog_plus_legacy_haiku():
    """Haiku is accepted for existing configs and direct launches but is not
    a catalog tier, so it is never offered by the curated Coding picker."""
    assert VALID_CLAUDE_MODELS == (
        available_values(CLAUDE_MODEL_SPECS) + LEGACY_CLAUDE_MODELS
    )
    assert "haiku" in VALID_CLAUDE_MODELS
    assert "claude:haiku" not in VALID_CODING_MODEL_CHOICES
