"""The Telegram autostart's breadcrumbs reach a log file (#1546).

The tray runs the webapp under ``pythonw`` (no stderr) with only ``auth.log``
and ``slow-requests.log`` attached, so the autostart's "off" / per-profile
started-or-skipped lines (#1542) were emitted and dropped. These tests pin the
file handler that now takes them: it receives the lines, it attaches once, and
without it nothing reaches disk.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app.webapp import observability, server
from app.webapp.routers import life_os_channels
from tests.test_webapp_api_life_os import life_os_client  # noqa: F401  (fixture)


def _file_handlers(target: logging.Logger, path: Path) -> list:
    return [
        h
        for h in target.handlers
        if isinstance(h, logging.FileHandler)
        and Path(h.baseFilename).resolve() == path.resolve()
    ]


@pytest.fixture
def fresh_loggers():
    """Two throwaway loggers, handlers and level restored afterwards."""
    names = ["test1546.server.autostart", "test1546.channels"]
    loggers = [logging.getLogger(n) for n in names]
    yield loggers
    for lg in loggers:
        for h in list(lg.handlers):
            h.close()
            lg.removeHandler(h)
        lg.setLevel(logging.NOTSET)


@pytest.fixture
def log_path(tmp_path, monkeypatch):
    path = tmp_path / "webapp" / "channel-autostart.log"
    monkeypatch.setattr(observability, "_AUTOSTART_LOG_PATH", path)
    return path


def _flush(loggers) -> None:
    for lg in loggers:
        for h in lg.handlers:
            h.flush()


class TestHandler:
    def test_info_and_warning_breadcrumbs_land_in_the_file(
        self, log_path, fresh_loggers
    ):
        observability.ensure_autostart_log_handler(*fresh_loggers)
        fresh_loggers[0].info("ℹ️ Telegram autostart is off; no channel session started")
        fresh_loggers[1].info("✅ Telegram autostart: profile garden started")
        fresh_loggers[1].warning("⚠️ session-host still unreachable")
        _flush(fresh_loggers)

        text = log_path.read_text(encoding="utf-8")
        assert "Telegram autostart is off" in text
        assert "profile garden started" in text
        assert "[WARNING]" in text and "session-host still unreachable" in text

    def test_without_the_handler_nothing_reaches_the_file(
        self, log_path, fresh_loggers
    ):
        # The pre-fix state: the loggers emit, no file handler is attached.
        fresh_loggers[0].setLevel(logging.INFO)
        fresh_loggers[0].info("ℹ️ Telegram autostart is off")
        assert not log_path.exists()

    def test_attaches_once_however_often_it_is_called(
        self, log_path, fresh_loggers
    ):
        for _ in range(3):
            observability.ensure_autostart_log_handler(*fresh_loggers)
        assert [len(_file_handlers(lg, log_path)) for lg in fresh_loggers] == [1, 1]
        fresh_loggers[0].info("once")
        _flush(fresh_loggers)
        assert log_path.read_text(encoding="utf-8").count("once") == 1

    def test_an_unwritable_path_never_raises(self, tmp_path, monkeypatch, fresh_loggers):
        blocker = tmp_path / "webapp"
        blocker.write_text("a file where the folder should be", encoding="utf-8")
        monkeypatch.setattr(
            observability, "_AUTOSTART_LOG_PATH", blocker / "channel-autostart.log"
        )
        observability.ensure_autostart_log_handler(*fresh_loggers)
        assert not fresh_loggers[0].handlers


class TestWiring:
    def test_create_app_attaches_the_handler_to_the_autostart_loggers_once(
        self, life_os_client, log_path, monkeypatch  # noqa: F811
    ):
        targets = [server._autostart_log, life_os_channels.logger]
        for lg in targets:
            for h in list(lg.handlers):
                if isinstance(h, logging.FileHandler):
                    lg.removeHandler(h)
        try:
            server.create_app()
            server.create_app()
            assert [len(_file_handlers(lg, log_path)) for lg in targets] == [1, 1]
            # The real emit sites, not a stand-in: both reach the one file.
            server._autostart_log.info("ℹ️ Telegram autostart is off (wired)")
            life_os_channels.logger.info("ℹ️ Telegram autostart: wired profile line")
            _flush(targets)
            text = log_path.read_text(encoding="utf-8")
            assert "autostart is off (wired)" in text
            assert "wired profile line" in text
        finally:
            for lg in targets:
                for h in _file_handlers(lg, log_path):
                    h.close()
                    lg.removeHandler(h)
