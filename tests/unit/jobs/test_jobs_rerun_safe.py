"""`rerun_safe` job flag: owner-set, strict boolean, never with confirm/elevated.

The fleet chief re-runs a failed job unattended only when this is a literal
``true``, so the loader must refuse anything looser and any combination that
needs a human.
"""

from __future__ import annotations

import json

import pytest

from src.jobs import config as jc
from src.jobs.config import Job, JobsConfig, job_from_dict, load_jobs, save_jobs, update_job

_ROW = {"id": "j", "name": "J", "script_path": "C:\\x\\s.py"}


def test_default_false_and_omitted_from_file():
    job = job_from_dict(dict(_ROW))
    assert job.rerun_safe is False
    assert "rerun_safe" not in job.to_dict()


def test_true_round_trips_through_save_and_load(tmp_path, monkeypatch):
    monkeypatch.setattr(jc, "DEFAULT_JOBS_PATH", tmp_path / "jobs.json")
    save_jobs(JobsConfig(jobs=[Job(id="j", name="J", script_path="C:\\x\\s.py", rerun_safe=True)]))
    on_disk = json.loads((tmp_path / "jobs.json").read_text(encoding="utf-8"))
    assert on_disk["jobs"][0]["rerun_safe"] is True
    assert load_jobs().jobs[0].rerun_safe is True


@pytest.mark.parametrize("bad", ["true", "yes", 1, 0, [], {}])
def test_non_bool_is_refused(bad):
    with pytest.raises(ValueError, match="rerun_safe must be true or false"):
        job_from_dict({**_ROW, "rerun_safe": bad})


@pytest.mark.parametrize("other", ["confirm", "elevated"])
def test_refused_with_confirm_or_elevated(other):
    with pytest.raises(ValueError, match=f"rerun_safe cannot be combined with {other}"):
        job_from_dict({**_ROW, "rerun_safe": True, other: True})


def test_false_alongside_confirm_is_fine():
    assert job_from_dict({**_ROW, "rerun_safe": False, "confirm": True}).confirm is True


def test_update_job_checks_the_effective_post_edit_shape(tmp_path, monkeypatch):
    monkeypatch.setattr(jc, "DEFAULT_JOBS_PATH", tmp_path / "jobs.json")
    cfg = JobsConfig(jobs=[Job(id="j", name="J", script_path="C:\\x\\s.py", confirm=True)])
    save_jobs(cfg)
    with pytest.raises(ValueError, match="confirm"):
        update_job(cfg, "j", rerun_safe=True)
    update_job(cfg, "j", confirm=False, rerun_safe=True)
    assert jc.get_by_id(cfg, "j").rerun_safe is True
    with pytest.raises(ValueError, match="confirm"):
        update_job(cfg, "j", confirm=True)
    assert load_jobs().jobs[0].confirm is False
    with pytest.raises(ValueError, match="rerun_safe must be"):
        update_job(cfg, "j", rerun_safe="true")
    assert load_jobs().jobs[0].rerun_safe is True


def test_sample_registry_loads_and_documents_the_field():
    sample = jc.PROJECT_ROOT / "config" / "jobs.sample.json"
    cfg = jc.JobsConfig(
        jobs=[job_from_dict(r) for r in json.loads(sample.read_text(encoding="utf-8"))["jobs"]]
    )
    flagged = [j.id for j in cfg.jobs if j.rerun_safe]
    assert flagged == ["grokbot-ensure"]
