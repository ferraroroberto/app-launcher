"""Codex full-control launch compatibility (issue #436)."""

from __future__ import annotations

import logging
from pathlib import Path

from src import launcher


def _capture_create(monkeypatch) -> dict:
    captured: dict = {}

    def fake_create_session(port, project_dir, name, flags, **kwargs):
        captured.update(
            port=port,
            project_dir=project_dir,
            name=name,
            flags=flags,
            **kwargs,
        )
        return {"session_id": "codex-test", "kind": kwargs["kind"]}

    monkeypatch.setattr(launcher.session_client, "create_session", fake_create_session)
    return captured


def test_codex_pty_disables_redundant_paste_burst_detector(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    captured = _capture_create(monkeypatch)
    caplog.set_level(logging.INFO, logger=launcher.logger.name)

    launcher.spawn_claude_session(
        tmp_path,
        "proj",
        "-c model_reasoning_effort=high",
        8446,
        kind="pty",
        agent="codex",
    )

    assert captured["flags"].endswith("-c disable_paste_burst=true")
    assert "disabled fallback paste-burst detection" in caplog.text


def test_codex_resume_pty_keeps_subcommand_before_compat_override(
    tmp_path: Path, monkeypatch
) -> None:
    captured = _capture_create(monkeypatch)

    launcher.spawn_claude_session(
        tmp_path,
        "proj",
        "resume -c model_reasoning_effort=high",
        8446,
        kind="pty",
        agent="codex",
    )

    assert captured["flags"] == (
        "resume -c model_reasoning_effort=high -c disable_paste_burst=true"
    )


def test_codex_detached_console_keeps_native_paste_detector(
    tmp_path: Path, monkeypatch
) -> None:
    captured = _capture_create(monkeypatch)

    launcher.spawn_claude_session(
        tmp_path,
        "proj",
        "-c model_reasoning_effort=high",
        8446,
        kind="remote",
        agent="codex",
    )

    assert "disable_paste_burst" not in captured["flags"]


def test_codex_pty_compatibility_wins_over_conflicting_false_override(
    tmp_path: Path, monkeypatch
) -> None:
    captured = _capture_create(monkeypatch)

    launcher.spawn_claude_session(
        tmp_path,
        "proj",
        "-c disable_paste_burst=false",
        8446,
        kind="pty",
        agent="codex",
    )

    assert captured["flags"].endswith("-c disable_paste_burst=true")
