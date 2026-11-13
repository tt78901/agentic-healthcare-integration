"""Ground-truth evaluation: catches mappings that are valid FHIR but wrong."""
import json

from fabric.evaluate import evaluate, load_truth
from fabric.validate import run_adapter
from fabric.stub import FIXTURES


def system(cfg, sid):
    return next(s for s in cfg["systems"] if s["id"] == sid)


def _run(cfg, sid, code, tmp_path):
    path = tmp_path / "adapter.py"
    path.write_text(code)
    out, _, failure = run_adapter(path, system(cfg, sid))
    assert out and not out["errors"], failure or out["errors"]
    return out["resources"]


def test_reference_adapters_are_fully_correct(cfg, tmp_path):
    for sid in ("ehr-rest", "lab-sql", "adt-feed", "pharmacy-csv"):
        code = (FIXTURES / sid / "adapter.py").read_text()
        res = _run(cfg, sid, code, tmp_path)
        ev = evaluate(sid, res, load_truth(system(cfg, sid)), cfg["settings"]["mrn_system"])
        assert ev["clean"] and ev["accuracy"] == 1.0 and ev["coverage"] == 1.0, (sid, ev["fields"])


def test_valid_but_wrong_mapping_is_caught(cfg, tmp_path):
    """Day and month swapped in the date of birth: valid FHIR, passes every contract check, wrong data."""
    code = (FIXTURES / "ehr-rest" / "adapter.py").read_text()
    wrong = code.replace('return f"{year}-{month}-{day}"', 'return f"{year}-{day}-{month}" if int(day) <= 12 else f"{year}-{month}-{day}"')
    assert wrong != code
    ev = evaluate("ehr-rest", _run(cfg, "ehr-rest", wrong, tmp_path), load_truth(system(cfg, "ehr-rest")),
                  cfg["settings"]["mrn_system"])
    t = ev["fields"]["Patient.birthDate"]
    assert t["wrong"] > 0 and not ev["clean"] and ev["accuracy"] < 1.0
    assert ev["fields"]["Patient.family"]["wrong"] == 0


def test_unmapped_field_lowers_coverage_not_accuracy(cfg, tmp_path):
    code = (FIXTURES / "pharmacy-csv" / "adapter.py").read_text()
    dropped = code.replace('        if row.get("SIG_TXT"):\n            res["dosageInstruction"] = [{"text": row["SIG_TXT"]}]\n', "")
    assert dropped != code
    ev = evaluate("pharmacy-csv", _run(cfg, "pharmacy-csv", dropped, tmp_path),
                  load_truth(system(cfg, "pharmacy-csv")), cfg["settings"]["mrn_system"])
    assert ev["accuracy"] == 1.0 and ev["coverage"] < 1.0
    assert ev["fields"]["MedicationRequest.dosage"]["absent"] == 60


def test_agents_never_see_the_ground_truth(cfg):
    from fabric.config import public_view
    for s in cfg["systems"]:
        assert "truth" not in json.dumps(public_view(s))
