"""Provider-native web links on the Coding sessions endpoint (issue #879)."""

from app.webapp.routers.sessions import _provider_web_url


def test_claude_web_url_is_recovered_from_wrapped_terminal_output(tmp_path):
    transcript = tmp_path / "claude.transcript"
    transcript.write_text(
        "before\x1b[1Bhttps://claude.ai/code/session_\r\n"
        "\x1b[3G011QSPhSiZdi9GB8skTjx16P\x1b[K after",
        encoding="utf-8",
    )

    assert _provider_web_url("claude", transcript) == (
        "https://claude.ai/code/session_011QSPhSiZdi9GB8skTjx16P"
    )


def test_codex_has_no_web_session_url(tmp_path):
    transcript = tmp_path / "codex.transcript"
    transcript.write_text(
        "thread id 01a0819b-c4f9-70a3-83df-72445d31a231",
        encoding="utf-8",
    )

    assert _provider_web_url("codex", transcript) == ""
