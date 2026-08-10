"""``schtasks`` output must decode regardless of the ambient locale (#743).

``schtasks.exe`` writes the OEM code page (cp850 on this fleet), which is not
valid UTF-8. The webapp is spawned with ``PYTHONUTF8=1`` /
``PYTHONIOENCODING=utf-8`` (``app/webapp/manager.py``), so a ``text=True``
call there decoded OEM bytes as UTF-8 and came back with *no stdout at all* —
`_bulk_records` read that as "query failed" and every schtasks-backed feature
degraded silently: blank ``next_run`` on all 20 jobs and the structural half
of missed-fire coverage pinned at ``unknown`` fleet-wide, for weeks.

The load-bearing test is
:meth:`TestDecodesUnderUtf8Mode.test_oem_output_survives_utf8_mode`, which
re-runs the read in a *real* child interpreter under ``-X utf8=1``. That is
the only shape that reproduces production: the decode happens in the calling
process, so a test running under this suite's own locale would pass against
the pre-fix code and prove nothing.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import textwrap
from types import SimpleNamespace

from src import jobs_schtasks

# cp850 bytes that are NOT valid UTF-8 — 0xE9 is 'Ú' in cp850 and an illegal
# lone continuation lead in UTF-8. This is the shape of a localised task name.
OEM_ONLY_BYTE = b"\xe9"


class TestRunSchtasksContract:
    def test_decoding_is_pinned_not_inherited_from_the_locale(self, monkeypatch):
        seen = {}

        def _spy(argv, **kwargs):
            seen.update(kwargs)
            seen["argv"] = argv
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(jobs_schtasks.subprocess, "run", _spy)
        jobs_schtasks._run_schtasks(["schtasks", "/Query"])

        assert seen["encoding"] == jobs_schtasks.SCHTASKS_ENCODING == "oem"
        assert seen["errors"] == jobs_schtasks.SCHTASKS_ENCODING_ERRORS == "replace"
        # `text=True` is what deferred to the ambient locale in the first place.
        assert "text" not in seen or seen["text"] is not True


class TestDecodesUnderUtf8Mode:
    def test_oem_output_survives_utf8_mode(self, tmp_path):
        """A child under ``-X utf8=1`` must still read OEM bytes back.

        Reproduces the production environment exactly: UTF-8 mode on, a
        command emitting OEM bytes that are invalid UTF-8. Pre-fix this
        returns 0 characters; post-fix it returns the full payload.
        """
        emitter = tmp_path / "emit_oem.py"
        emitter.write_text(
            "import sys\n"
            "sys.stdout.buffer.write(b'Folder: \\\\AppLauncher' + "
            f"{OEM_ONLY_BYTE!r} + b'\\r\\n')\n",
            encoding="utf-8",
        )
        probe = tmp_path / "probe.py"
        probe.write_text(
            textwrap.dedent(
                f"""
                import sys
                # sys.path[0] is the probe's own tmp dir, so the repo root has
                # to be named explicitly for `from src import ...` to resolve.
                sys.path.insert(0, {str(_repo_root())!r})
                from src import jobs_schtasks
                proc = jobs_schtasks._run_schtasks(
                    [sys.executable, {str(emitter)!r}]
                )
                print(len(proc.stdout or ""))
                """
            ),
            encoding="utf-8",
        )

        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        result = subprocess.run(
            [sys.executable, "-X", "utf8=1", str(probe)],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(_repo_root()),
            env=env,
        )
        assert result.returncode == 0, result.stderr
        assert int(result.stdout.strip()) > 0, (
            "schtasks output decoded to nothing under UTF-8 mode — "
            f"this is #743 regressing. stderr={result.stderr!r}"
        )


class TestBulkRecordsAnnouncesFailure:
    def test_failed_query_is_logged_not_swallowed(self, monkeypatch, caplog):
        """A dead query must say so — silence is why #743 hid for weeks."""

        def _dead(argv, **kwargs):
            return SimpleNamespace(returncode=0, stdout="", stderr="boom")

        monkeypatch.setattr(jobs_schtasks, "_run_schtasks", _dead)
        with caplog.at_level(logging.WARNING, logger=jobs_schtasks.logger.name):
            assert jobs_schtasks._bulk_records() is None
        messages = [r.getMessage() for r in caplog.records]
        assert any("schtasks bulk query failed" in m for m in messages), messages
        # The two facts needed to diagnose it must be in the line itself.
        assert any("rc=0" in m and "boom" in m for m in messages), messages


def _repo_root():
    from pathlib import Path

    return Path(jobs_schtasks.__file__).resolve().parent.parent
