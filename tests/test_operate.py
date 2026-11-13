"""Operate: drift detection on a loud and a silent source change, and the hand-built harness."""
import json
import shutil

import pytest

from fabric import handbuilt, operate, pipeline
from fabric.stub import FIXTURES, StubLLM
from mock_systems import drift
from mock_systems.build import DATA_DIR


@pytest.fixture()
def promoted(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(pipeline, "GENERATED", tmp_path / "generated")
    pipeline.run(cfg, StubLLM(), ["ehr-rest", "lab-sql"], auto_approve=True)
    yield tmp_path / "generated"
    drift.reset()


def test_healthy_estate_has_no_alerts(cfg, promoted):
    statuses = operate.check(cfg, promoted)
    assert set(statuses) == {"ehr-rest", "lab-sql"}
    assert all(s["healthy"] for s in statuses.values())


def test_loud_and_silent_drift_are_both_detected(cfg, promoted):
    drift.drift_lab()
    drift.drift_ehr()
    statuses = operate.check(cfg, promoted)

    lab = statuses["lab-sql"]                      # loud: the adapter crashes
    assert not lab["healthy"] and lab["contract"] == "fail"
    assert "LAB_RSLT.RSLT_VAL" in lab["drift"]["removed"] and "LAB_RSLT.RESULT_VALUE" in lab["drift"]["added"]

    ehr = statuses["ehr-rest"]                     # silent: the contract still passes ...
    assert ehr["contract"] == "pass" and not ehr["healthy"]
    assert "/api/v2/patients dob" in ehr["drift"]["removed"]          # ... the fingerprint catches it
    assert ehr["evaluation"]["fields"]["Patient.birthDate"]["absent"] == 40   # ... and so does the data check
    assert "DRIFTED" in operate.describe(statuses)


def test_handbuilt_baseline_is_timed_and_checked(cfg, promoted, tmp_path, monkeypatch):
    monkeypatch.setattr(handbuilt, "BASELINE", tmp_path / "baseline")
    monkeypatch.setattr(handbuilt, "GENERATED", promoted)
    folder = handbuilt.start(cfg, "lab-sql", "Tester")
    assert "Resources to publish" in (folder / "BRIEF.md").read_text()
    log = handbuilt.check(cfg, "lab-sql")                    # the skeleton fails
    assert not log["finished"] and len(log["runs"]) == 1
    shutil.copy(FIXTURES / "lab-sql" / "adapter.py", folder / "adapter.py")
    log = handbuilt.check(cfg, "lab-sql", minutes=120)
    assert log["finished"] and log["minutes"] == 120 and len(log["runs"]) == 2
    assert log["evaluation"]["clean"]
    assert handbuilt.load_all()["lab-sql"]["developer"] == "Tester"
