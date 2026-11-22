"""Terminology check: real codes pass, a wrong name next to a real code is caught (the
drug-name error from the measured run), unknown codes are caught, and an unreachable server
never produces a false verdict."""
import json

import httpx
import pytest

from fabric import pipeline, terminology
from fabric.stub import StubLLM

LOINC, RXNORM = "http://loinc.org", "http://www.nlm.nih.gov/research/umls/rxnorm"

# A tiny stand-in for a FHIR terminology server's CodeSystem/$validate-code
KNOWN = {(LOINC, "2345-7"): "Glucose [Mass/volume] in Serum or Plasma",
         (RXNORM, "860975"): "24 HR metformin hydrochloride 500 MG Extended Release Oral Tablet"}


def _server(request: httpx.Request) -> httpx.Response:
    q = request.url.params
    system, code, display = q.get("url"), q.get("code"), q.get("display") or ""
    expected = KNOWN.get((system, code))
    if expected is None:
        params = [{"name": "result", "valueBoolean": False},
                  {"name": "message", "valueString": f"Unknown code '{code}' in the CodeSystem '{system}'"}]
    elif display and display.lower() != expected.lower():
        params = [{"name": "result", "valueBoolean": False}, {"name": "display", "valueString": expected},
                  {"name": "message", "valueString": f"Wrong Display Name '{display}' for {system}#{code}. "
                                                     f"Valid display is '{expected}'"}]
    else:
        params = [{"name": "result", "valueBoolean": True}, {"name": "display", "valueString": expected}]
    return httpx.Response(200, json={"resourceType": "Parameters", "parameter": params})


def _term(tmp_path, handler=_server, **kw):
    return terminology.Terminology(server="https://tx.example/r4", cache_path=tmp_path / "cache.json",
                                   client=httpx.Client(transport=httpx.MockTransport(handler)), **kw)


def test_real_code_with_matching_name_is_valid(tmp_path):
    assert _term(tmp_path).validate(LOINC, "2345-7", "Glucose [Mass/volume] in Serum or Plasma")["status"] == "valid"


def test_real_code_with_invented_name_is_caught(tmp_path):
    res = _term(tmp_path).validate(RXNORM, "860975", "Metformin 850 mg tablet")
    assert res["status"] == "display-mismatch" and "metformin hydrochloride 500 MG" in res["expected_display"]


def test_code_that_does_not_exist_is_caught(tmp_path):
    assert _term(tmp_path).validate(LOINC, "99999-9", "Made up test")["status"] == "unknown-code"


def test_unreachable_server_claims_nothing_and_is_not_cached(tmp_path):
    def down(request):
        raise httpx.ConnectError("no network")
    term = _term(tmp_path, handler=down)
    assert term.validate(LOINC, "2345-7")["status"] == "unverified"
    assert term.cache == {}


def test_results_are_cached_and_work_offline(tmp_path):
    term = _term(tmp_path)
    term.validate(LOINC, "2345-7", "Glucose [Mass/volume] in Serum or Plasma")
    term.save()
    offline = _term(tmp_path, offline=True)
    assert offline.validate(LOINC, "2345-7", "Glucose [Mass/volume] in Serum or Plasma")["status"] == "valid"
    assert offline.calls == 0
    assert offline.validate(LOINC, "718-7", "Hemoglobin")["status"] == "unverified"


def test_only_clinical_code_systems_are_collected():
    resources = {"Observation": [{"code": {"coding": [{"system": LOINC, "code": "2345-7", "display": "Glucose"}]},
                                  "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/"
                                                            "observation-category", "code": "laboratory"}]}],
                                  "valueQuantity": {"system": "http://unitsofmeasure.org", "code": "mg/dL"}},
                                 {"code": {"coding": [{"system": LOINC, "code": "2345-7", "display": "Glucose"}]}}]}
    assert terminology.collect(resources) == {(LOINC, "2345-7", "Glucose"): 2}


@pytest.fixture(scope="module")
def promoted(cfg, tmp_path_factory):
    tmp = tmp_path_factory.mktemp("tx")
    mp = pytest.MonkeyPatch()
    mp.setattr(pipeline, "RUNS", tmp / "runs")
    mp.setattr(pipeline, "GENERATED", tmp / "generated")
    pipeline.run(cfg, StubLLM(), None, auto_approve=True)
    mp.undo()
    return tmp / "generated"


def test_estate_check_runs_every_adapter_and_reports(cfg, promoted, tmp_path):
    report = terminology.check(cfg, promoted, _term(tmp_path))
    assert set(report["systems"]) == {"ehr-rest", "lab-sql", "adt-feed", "pharmacy-csv"}
    lab = report["systems"]["lab-sql"]
    assert lab["codes"] > 0 and "LOINC" in lab["by_system"]                  # every LOINC code was looked up
    assert lab["valid"] + len(lab["issues"]) + lab["unverified"] == lab["codes"]
    assert json.loads((promoted / "terminology.json").read_text())["systems"]["lab-sql"]["codes"] == lab["codes"]
    assert "Terminology server" in terminology.describe(report)
