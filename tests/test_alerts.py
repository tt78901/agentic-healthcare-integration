"""Operate stage alerts: raised once per change of state, resolved when the condition
ends, kept across restarts, and never blocked by a failing webhook."""
import json

from fabric.alerts import AlertManager


def test_raise_is_deduplicated_and_resolve_closes_it(tmp_path):
    am = AlertManager(tmp_path / "alerts.jsonl", webhook="")
    assert am.raise_alert("lab-sql", "circuit", "down") is not None
    assert am.raise_alert("lab-sql", "circuit", "still down") is None      # same condition: no second alert
    assert [a["kind"] for a in am.active()] == ["circuit"]
    assert am.resolve("lab-sql", "circuit", "back") is not None
    assert am.resolve("lab-sql", "circuit", "back") is None                # nothing open any more
    assert am.active() == []
    events = [json.loads(line) for line in (tmp_path / "alerts.jsonl").read_text().splitlines()]
    assert [e["status"] for e in events] == ["open", "resolved"]


def test_active_alerts_survive_a_restart(tmp_path):
    path = tmp_path / "alerts.jsonl"
    AlertManager(path, webhook="").raise_alert("ehr-rest", "drift", "field removed")
    again = AlertManager(path, webhook="")
    assert [(a["system"], a["kind"]) for a in again.active()] == [("ehr-rest", "drift")]


def test_estate_check_results_raise_and_clear_alerts(tmp_path):
    am = AlertManager(tmp_path / "alerts.jsonl", webhook="")
    bad = {"ehr-rest": {"healthy": False, "contract": "fail", "drift": {"removed": ["Patient.dob"], "added": [], "changed": []},
                        "failed": [{"id": "reconcile.counts"}]},
           "lab-sql": {"healthy": True, "contract": "pass", "drift": None, "failed": []}}
    am.from_statuses(bad)
    assert sorted((a["system"], a["kind"]) for a in am.active()) == [("ehr-rest", "contract"), ("ehr-rest", "drift")]
    assert "Patient.dob" in next(a for a in am.active() if a["kind"] == "drift")["message"]
    good = {"ehr-rest": {"healthy": True, "contract": "pass", "drift": None, "failed": []}}
    am.from_statuses(good)
    assert am.active() == []


def test_unreachable_webhook_never_breaks_alerting(tmp_path):
    am = AlertManager(tmp_path / "alerts.jsonl", webhook="http://127.0.0.1:9/nowhere")
    assert am.raise_alert("adt-feed", "circuit", "down") is not None
    assert len(am.active()) == 1


def test_source_recovery_clears_an_outage_contract_alert(tmp_path, monkeypatch):
    """A contract alert raised only because the source was unreachable clears itself when the
    source answers again, without waiting for the next scheduled or manual check."""
    import time

    from fabric import gateway, operate

    am = AlertManager(tmp_path / "alerts.jsonl", webhook="")
    am.raise_alert("ehr-rest", "contract", "ProbeError: source or artefacts unavailable")
    am.raise_alert("lab-sql", "contract", "fhir.schema")                   # a real failure: must stay open
    monkeypatch.setattr(gateway, "_alerts", am)
    monkeypatch.setattr(operate, "check", lambda cfg, generated: {
        "ehr-rest": {"healthy": True, "contract": "pass", "drift": None, "failed": []},
        "lab-sql": {"healthy": False, "contract": "fail", "drift": None, "failed": [{"id": "fhir.schema"}]}})
    gateway._recheck_after_recovery("ehr-rest")
    for _ in range(50):
        if all(a["system"] != "ehr-rest" for a in am.active()):
            break
        time.sleep(0.05)
    assert [(a["system"], a["kind"]) for a in am.active()] == [("lab-sql", "contract")]
    gateway._recheck_after_recovery("lab-sql")                              # not an outage alert: no re-check
    assert [(a["system"], a["kind"]) for a in am.active()] == [("lab-sql", "contract")]
