"""Design-surface capture hook (issue #408; fleet-config#1383, Step 2/6).

The fleet wants every web-app surface (tabs, sheets, dialogs, menus, popovers,
empty and error states) inventoried for design consistency. The `/design-review`
walk opens only a fraction of them; an app's e2e suite already opens most of the
rest on its own disposable server. This module makes adding a surface to the
design inventory the same act as testing it: one call, once the surface is open.

    from tests.e2e._surface import surface

    def test_sort_sheet(page):
        ...open the sheet...
        surface(page, "jobs.sort-sheet")

**Inert by default.** With `DESIGN_SURFACE_CAPTURE` unset (or empty) `surface()`
returns after one environment lookup and touches neither the page nor the disk,
so the normal gate pays nothing and an invalid name is not even looked at.

**Capture mode.** `DESIGN_SURFACE_CAPTURE=<dir>` names a directory that
fleet-config's `/design-review` prepared. This module imports nothing from
fleet-config; the directory is the whole contract:

- `script.js`   — the in-page measurement, an evaluate-ready `(params) => {...}`
- `params.json` — the object passed to it
- `capture.json` — optional; `{"theme_storage_key": "<localStorage key>"}` for an
  app with an explicit theme toggle

`surface(page, name)` then, for `light` and `dark` in turn: flips the theme in
place (the surface stays open, so no test re-run), waits two animation frames,
screenshots the viewport to `<name>.<theme>.png` and writes the script's result,
with the context it was taken in, to `<name>.<theme>.json`. It restores the
page's original scheme (and, with a storage key, its `data-theme` and stored
value) afterwards. A name already captured in this process is skipped, so the
first call wins. Nothing is ever written outside the capture directory.

**Theme flip.** Colour-scheme emulation always; with a `theme_storage_key` the
hook also sets that key and the `data-theme` attribute, for apps whose theme is
an explicit toggle rather than the media query. Each JSON carries `observed`
(the attribute and the media query as the page then saw them) so a flip the app
overwrote is visible rather than silently passing.

**Failure states stay distinct.** A missing/unreadable capture directory raises
`SurfaceCaptureError`; a bad name raises `ValueError`; a measurement script that
throws is written as `{"error": ..., "metrics": null}` (never as an empty
success) and the test carries on.

**Collection.** In capture mode the suite should run only the tests that
capture. Import the hook into the conftest with one line:

    from tests.e2e._surface import pytest_collection_modifyitems  # noqa: F401

While the env var is set it deselects every test whose own function source never
mentions `surface(` — so call `surface(` directly in the test body, not from a
helper the test delegates to. A test whose source cannot be read is kept (unknown
is not "does not capture"). With the env var unset the hook changes nothing.

**Never run against a live app.** Capture a suite that boots its own disposable
server (the `tests/e2e/_e2e_live_guard.py` rule), never one pointed at the app
you use. Run one process at a time per capture directory.

Vendor-verbatim: consuming apps copy this file byte-identical into their own
`tests/e2e/` and track it in their `.fleet.toml` `[vendored]` table (manifest key
`surface_capture`). Stdlib, `pytest` and `playwright.sync_api` only.
"""

from __future__ import annotations

import inspect
import json
import os
import re
from pathlib import Path
from typing import Any, Literal

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

CAPTURE_ENV = "DESIGN_SURFACE_CAPTURE"
THEMES: tuple[Literal["light"], Literal["dark"]] = ("light", "dark")

# Lowercase, dot- or hyphen-separated, e.g. "jobs.sort-sheet". Unique per app by
# convention; the first call for a name wins.
_NAME_RE = re.compile(r"^[a-z0-9]+(?:[.-][a-z0-9]+)*$")
# `surface(` as a call, not as the tail of `test_surface(`.
_CALL_RE = re.compile(r"(?<!\w)surface\(")
_PHONE_UA_RE = re.compile(r"Mobile|iPhone|Android")

_SCRIPT_FILE = "script.js"
_PARAMS_FILE = "params.json"
_OPTIONS_FILE = "capture.json"
_SCHEMA_VERSION = 1

# (capture dir, name) pairs already captured in this process.
_captured: set[tuple[str, str]] = set()


class SurfaceCaptureError(RuntimeError):
    """The capture directory is missing or unusable (a setup fault, not a skip)."""


_READ_STATE_JS = """key => {
  let stored = null;
  if (key) { try { stored = localStorage.getItem(key); } catch (e) { stored = null; } }
  return {
    dark: matchMedia('(prefers-color-scheme: dark)').matches,
    attr: document.documentElement.getAttribute('data-theme'),
    stored: stored,
    ua: navigator.userAgent,
    width: window.innerWidth,
    height: window.innerHeight,
  };
}"""

_SET_THEME_JS = """([theme, key]) => {
  document.documentElement.setAttribute('data-theme', theme);
  try { localStorage.setItem(key, theme); } catch (e) { /* storage blocked */ }
}"""

_RESTORE_THEME_JS = """([attr, stored, key]) => {
  if (attr === null) document.documentElement.removeAttribute('data-theme');
  else document.documentElement.setAttribute('data-theme', attr);
  try {
    if (stored === null) localStorage.removeItem(key); else localStorage.setItem(key, stored);
  } catch (e) { /* storage blocked */ }
}"""

_TWO_FRAMES_JS = "() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))"


def surface(page: Page, name: str) -> None:
    """Capture the open surface *name* in light and dark (a no-op unless opted in)."""
    raw = os.environ.get(CAPTURE_ENV)
    if not raw:
        return
    _capture(page, name, Path(raw))


def _capture(page: Page, name: str, capture_dir: Path) -> None:
    if not _NAME_RE.fullmatch(name):
        raise ValueError(
            f"surface name {name!r} must be lowercase, dot- or hyphen-separated "
            "(e.g. 'jobs.sort-sheet')"
        )
    key = (str(capture_dir.resolve()), name)
    if key in _captured:
        return
    script, params, storage_key = _load_capture_dir(capture_dir)

    original = page.evaluate(_READ_STATE_JS, storage_key)
    context = _context_record(page, original)
    try:
        for theme in THEMES:
            page.emulate_media(color_scheme=theme)
            if storage_key:
                page.evaluate(_SET_THEME_JS, [theme, storage_key])
            page.evaluate(_TWO_FRAMES_JS)
            page.screenshot(
                path=capture_dir / f"{name}.{theme}.png",
                animations="disabled",
                caret="hide",
            )
            _write_json(
                capture_dir / f"{name}.{theme}.json",
                {
                    "version": _SCHEMA_VERSION,
                    "surface": name,
                    "theme": theme,
                    **context,
                    "observed": _observed(page),
                    **_measure(page, script, params),
                },
            )
    finally:
        page.emulate_media(color_scheme="dark" if original["dark"] else "light")
        if storage_key:
            page.evaluate(_RESTORE_THEME_JS, [original["attr"], original["stored"], storage_key])
    _captured.add(key)


def _load_capture_dir(capture_dir: Path) -> tuple[str, Any, str | None]:
    """`(script, params, theme_storage_key)` from the prepared directory."""
    try:
        script = (capture_dir / _SCRIPT_FILE).read_text(encoding="utf-8")
        params = json.loads((capture_dir / _PARAMS_FILE).read_text(encoding="utf-8"))
        options_path = capture_dir / _OPTIONS_FILE
        options = json.loads(options_path.read_text(encoding="utf-8")) if options_path.exists() else {}
    except (OSError, ValueError) as exc:
        raise SurfaceCaptureError(
            f"{CAPTURE_ENV}={capture_dir} is not a usable capture directory "
            f"(needs {_SCRIPT_FILE} and {_PARAMS_FILE}): {exc}"
        ) from exc
    storage_key = options.get("theme_storage_key") if isinstance(options, dict) else None
    return script, params, storage_key if isinstance(storage_key, str) and storage_key else None


def _context_record(page: Page, state: dict[str, Any]) -> dict[str, Any]:
    viewport = page.viewport_size or {"width": state["width"], "height": state["height"]}
    browser = page.context.browser
    return {
        "engine": browser.browser_type.name if browser is not None else "unknown",
        "viewport": {"width": viewport["width"], "height": viewport["height"]},
        "phone": bool(_PHONE_UA_RE.search(str(state["ua"]))),
    }


def _observed(page: Page) -> dict[str, Any]:
    state = page.evaluate(_READ_STATE_JS, None)
    return {"attr": state["attr"], "dark": state["dark"]}


def _measure(page: Page, script: str, params: Any) -> dict[str, Any]:
    """The script's result, or its error as its own state — never an empty success."""
    try:
        return {"error": None, "metrics": page.evaluate(script, params)}
    except PlaywrightError as exc:
        return {"error": str(exc).strip().splitlines()[0][:300], "metrics": None}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """Byte-stable: sorted keys, fixed indent, LF, no timestamps."""
    path.write_bytes((json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8"))


def _mentions_surface(item: pytest.Item) -> bool | None:
    """`True`/`False` for whether the test's source calls `surface(`; `None` when unreadable."""
    function = getattr(item, "function", None)
    if function is None:
        return False
    try:
        return bool(_CALL_RE.search(inspect.getsource(function)))
    except (OSError, TypeError):
        return None


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """In capture mode, deselect every test that never calls `surface(`."""
    if not os.environ.get(CAPTURE_ENV):
        return
    kept: list[pytest.Item] = []
    dropped: list[pytest.Item] = []
    for item in items:
        (dropped if _mentions_surface(item) is False else kept).append(item)
    if dropped:
        config.hook.pytest_deselected(items=dropped)
        items[:] = kept
