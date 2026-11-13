"""End to end with the stub (no model): probes, masking, gates, validation,
self-healing, promotion, gateway and report."""
import json

import pytest

from fabric import pipeline, report
from fabric.config import ROOT, secrets_of
from fabric.stub import FIXTURES, StubLLM
from fabric.validate import validate


def system(cfg, system_id):
    return next(s for s in cfg["systems"] if s["id"] == system_id)


@pytest.fixture(scope="module")
def stub_run(cfg, tmp_path_factory):
    tmp = tmp_path_factory.mktemp("fabric")
    mp = pytest.MonkeyPatch()
    mp.setattr(pipeline, "RUNS", tmp / "runs")
    mp.setattr(pipeline, "GENERATED", tmp / "generated")
    mp.setattr(pipeline, "_salt", lambda: "fixed-test-salt")
    llm = StubLLM()
    run_dir = pipeline.run(cfg, llm, None, auto_approve=True)
    yield {"dir": run_dir, "llm": llm, "generated": tmp / "generated",
           "summary": json.loads((run_dir / "summary.json").read_text())}
    mp.undo()


def test_all_four_systems_are_promoted(stub_run):
    assert {s["id"]: s["status"] for s in stub_run["summary"]["systems"]} == {
        "ehr-rest": "promoted", "lab-sql": "promoted", "adt-feed": "promoted", "pharmacy-csv": "promoted"}
    for s in stub_run["summary"]["systems"]:
        assert all(c["status"] == "pass" for c in s["checks"]) and len(s["checks"]) == 14
        assert all(c["emitted"] == c["expected"] > 0 for c in s["counts"].values())
        assert (stub_run["generated"] / s["id"] / "adapter.py").exists()
        assert (stub_run["generated"] / s["id"] / "openapi.yaml").exists()


def test_broken_first_attempts_are_caught_and_repaired(stub_run):
    by_id = {s["id"]: s for s in stub_run["summary"]["systems"]}
    assert by_id["lab-sql"]["repair_rounds"] == 1
    assert [f["id"] for f in by_id["lab-sql"]["attempts"][0]["failed"]] == ["fhir.schema"]
    assert by_id["adt-feed"]["repair_rounds"] == 1
    assert {f["id"] for f in by_id["adt-feed"]["attempts"][0]["failed"]} == {"fhir.conventions", "reconcile.counts"}
    assert by_id["ehr-rest"]["repair_rounds"] == 0


def test_no_secret_or_real_identifier_reaches_a_prompt(stub_run, cfg):
    prompts = "\n".join(s + u for _, s, u in stub_run["llm"].prompts)
    assert len(stub_run["llm"].prompts) >= 14
    for sys_cfg in cfg["systems"]:
        for secret in secrets_of(sys_cfg):
            assert secret not in prompts
        if "path" in sys_cfg:
            assert sys_cfg["path"] not in prompts
    patients = json.loads((ROOT / "mock_systems" / "_data" / "ehr" / "patients.json").read_text())
    for p in patients:
        assert p["mrn"] not in prompts
        assert p["contact"]["phone"] not in prompts
        if len(p["name"]["last"]) >= 5:
            assert p["name"]["last"] not in prompts and p["name"]["last"].upper() not in prompts


def test_repair_prompt_contains_no_data_values(stub_run):
    repair = next(u for tag, _, u in stub_run["llm"].prompts if tag.startswith("repair1:lab-sql"))
    assert "FAILING CHECKS" in repair and "does not match spec regex" in repair
    assert "9999-99-99x99:99:99" in repair            # the shape of the bad value, not the value


def test_audit_trail_records_every_step(stub_run):
    events = [json.loads(line) for line in (stub_run["dir"] / "audit.jsonl").read_text().splitlines()]
    kinds = {e["event"] for e in events}
    assert {"run_started", "probe", "llm_call", "gate", "validation", "promoted", "run_finished"} <= kinds
    gates = [e for e in events if e["event"] == "gate"]
    assert len(gates) == 12 and all("no human review" in g["by"] for g in gates)


def test_report_is_labelled_as_a_stub_run(stub_run):
    html = report.build(stub_run["dir"]).read_text()
    assert "STUB RUN" in html and "assumption, not measured" in html


def test_unsafe_adapter_is_never_executed(cfg, tmp_path):
    lab = system(cfg, "lab-sql")
    design = json.loads((FIXTURES / "lab-sql" / "design.json").read_text())
    marker = tmp_path / "executed.txt"
    code = (FIXTURES / "lab-sql" / "adapter.py").read_text() + f"\nopen({str(marker)!r}, 'w').write('x')\n"
    result = validate(code, lab, design, cfg["settings"]["mrn_system"], tmp_path / "work")
    statuses = {c["id"]: c["status"] for c in result["checks"]}
    assert statuses["static.no_dangerous_calls"] == "fail" and statuses["runtime.executes"] == "skip"
    assert not marker.exists() and not result["passed"]


def test_gateway_serves_one_record_across_systems(stub_run, monkeypatch):
    from fastapi.testclient import TestClient

    from fabric import gateway

    monkeypatch.setattr(gateway, "GENERATED", stub_run["generated"])
    gateway._adapters.clear()
    gateway._cache.clear()
    client = TestClient(gateway.app)
    patients = client.get("/fhir/Patient", params={"_count": 1000}).json()
    ehr_count = next(s for s in stub_run["summary"]["systems"] if s["id"] == "ehr-rest")["counts"]["Patient"]["emitted"]
    assert patients["total"] == ehr_count                      # one Patient per MRN, EHR listed first
    sources = set()
    for entry in patients["entry"]:
        mrn = entry["resource"]["identifier"][0]["value"]
        bundle = client.get(f"/fhir/Patient/{mrn}/everything").json()
        sources |= {e["resource"]["meta"]["source"] for e in bundle["entry"]}
    assert sources == {"urn:system:ehr-rest", "urn:system:lab-sql", "urn:system:adt-feed", "urn:system:pharmacy-csv"}
    assert client.get("/fhir/Invoice").status_code == 404
