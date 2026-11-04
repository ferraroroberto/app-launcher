"""The static mount sends modules without their comments, vendored bundles as they are."""

from __future__ import annotations

import re

from app.webapp.routers._helpers import STATIC_DIR

IDENTITY = {"Accept-Encoding": "identity"}


def test_a_module_is_served_without_comments_and_with_stamped_imports(webapp_client):
    client, _, _ = webapp_client
    on_disk = (STATIC_DIR / "main.js").read_text(encoding="utf-8")
    served = client.get("/static/main.js", headers=IDENTITY)
    assert served.status_code == 200
    assert served.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert len(served.text) < len(on_disk) * 0.8
    assert not re.search(r"^\s*//", served.text, re.MULTILINE)
    assert "Entry point: wires every module together" in on_disk
    assert "Entry point: wires every module together" not in served.text
    assert re.search(r"from './tabs\.js\?v=[0-9a-f]+'", served.text)


def test_a_vendored_bundle_is_served_as_it_is_on_disk(webapp_client):
    client, _, _ = webapp_client
    on_disk = (STATIC_DIR / "vendor" / "xterm.js").read_text(encoding="utf-8")
    served = client.get("/static/vendor/xterm.js", headers=IDENTITY)
    assert served.status_code == 200
    assert served.text == on_disk


def test_an_edited_module_is_served_fresh_not_from_the_kept_copy(webapp_client, tmp_path):
    from app.webapp.server import _VersionedStatic

    root = tmp_path / "static"
    root.mkdir()
    module = root / "a.js"
    module.write_text("const a = 1; // one\n", encoding="utf-8")
    mount = _VersionedStatic(directory=str(root), asset_hashes={})
    assert mount._served_js(module, module.stat()) == "const a = 1;\n"
    module.write_text("const a = 2; // two, longer\n", encoding="utf-8")
    assert mount._served_js(module, module.stat()) == "const a = 2;\n"
