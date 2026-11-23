"""The Synthea loader, against a miniature bundle in Synthea's FHIR R4 shape, and the
committed Synthea estate the demo runs on."""
import json
import sqlite3

from mock_systems.build import build_all
from mock_systems.synthea import load_synthea

PID = "0b1c2d3e-0000-4000-8000-000000000001"
SUBJECT = {"reference": f"urn:uuid:{PID}"}
BUNDLE = {"resourceType": "Bundle", "type": "transaction", "entry": [{"resource": r} for r in [
    {"resourceType": "Patient", "id": PID, "name": [{"family": "Smith123", "given": ["Jane45"]}],
     "gender": "female", "birthDate": "1970-02-03", "telecom": [{"system": "phone", "value": "555-100-2000"}],
     "address": [{"line": ["1 Main St"], "city": "Boston", "state": "Massachusetts", "postalCode": "02101",
                  "country": "US"}]},
    {"resourceType": "Encounter", "id": "e1", "status": "finished", "class": {"code": "IMP"}, "subject": SUBJECT,
     "type": [{"coding": [{"display": "Hospital admission"}]}],
     "participant": [{"individual": {"display": "Dr. Ann67 Lee89"}}],
     "period": {"start": "2026-03-01T08:00:00-05:00", "end": "2026-03-04T10:00:00-05:00"}},
    {"resourceType": "Encounter", "id": "old", "status": "finished", "class": {"code": "AMB"}, "subject": SUBJECT,
     "period": {"start": "2001-03-01T08:00:00-05:00", "end": "2001-03-01T08:30:00-05:00"}},
    {"resourceType": "Observation", "id": "o1", "status": "final", "subject": SUBJECT,
     "category": [{"coding": [{"code": "laboratory"}]}],
     "code": {"coding": [{"system": "http://loinc.org", "code": "2345-7", "display": "Glucose"}]},
     "effectiveDateTime": "2026-03-01T09:00:00-05:00", "issued": "2026-03-01T11:00:00.000-05:00",
     "valueQuantity": {"value": 131.2, "unit": "mg/dL"}},
    {"resourceType": "Observation", "id": "vital", "status": "final", "subject": SUBJECT,
     "category": [{"coding": [{"code": "vital-signs"}]}], "code": {"coding": [{"code": "8302-2"}]},
     "effectiveDateTime": "2026-03-01T09:00:00-05:00", "valueQuantity": {"value": 170, "unit": "cm"}},
    {"resourceType": "Condition", "id": "c1", "subject": SUBJECT, "onsetDateTime": "2015-06-07T00:00:00-04:00",
     "clinicalStatus": {"coding": [{"code": "active"}]},
     "code": {"coding": [{"system": "http://snomed.info/sct", "code": "44054006",
                          "display": "Diabetes mellitus type 2 (disorder)"}]}},
    {"resourceType": "Condition", "id": "c2", "subject": SUBJECT, "onsetDateTime": "2015-06-07T00:00:00-04:00",
     "clinicalStatus": {"coding": [{"code": "active"}]},
     "code": {"coding": [{"system": "http://snomed.info/sct", "code": "160903007",
                          "display": "Full-time employment (finding)"}]}},
    {"resourceType": "MedicationRequest", "id": "m1", "status": "active", "intent": "order", "subject": SUBJECT,
     "authoredOn": "2026-03-04T10:00:00-05:00",
     "medicationCodeableConcept": {"coding": [{"code": "860975", "display": "Metformin 500 MG"}]}},
]]}


def test_synthea_bundle_becomes_four_mock_systems(tmp_path):
    src = tmp_path / "fhir"
    src.mkdir()
    (src / "Jane45_Smith123.json").write_text(json.dumps(BUNDLE))
    (src / "hospitalInformation1.json").write_text(json.dumps({"resourceType": "Bundle", "entry": []}))
    ds = load_synthea(str(src))
    assert ds["patients"][0]["first"] == "Jane" and ds["patients"][0]["last"] == "Smith"
    assert len(ds["patients"][0]["mrn"]) == 7
    assert [e["class"] for e in ds["encounters"]] == ["inpatient"]            # the 2001 visit is outside the window
    assert ds["encounters"][0]["start"] == "2026-03-01T08:00:00"
    assert [(lab["loinc"], lab["local_code"], lab["flag"]) for lab in ds["labs"]] == [("2345-7", "GLU", "H")]
    assert len(ds["conditions"]) == 1 and len(ds["medications"]) == 1     # the social "finding" is not a diagnosis
    assert ds["conditions"][0]["snomed"] == "44054006"
    assert ds["patients"][0]["postcode"] == "MA 02101"                       # text, as the hospital systems store it

    out = tmp_path / "estate"
    counts = build_all(ds, out)
    assert counts["patients"] == 1 and counts["lab_results"] == 1
    assert sqlite3.connect(out / "lab" / "lab.db").execute("SELECT COUNT(*) FROM LAB_RSLT").fetchone()[0] == 1
    assert len(list((out / "adt").glob("*.hl7"))) == 2                         # admission + discharge
    assert len(json.loads((out / "ehr" / "conditions.json").read_text())) == 1


def test_committed_synthea_estate_is_complete_and_puts_the_richest_record_first():
    from mock_systems.synthea import ESTATE

    ds = json.loads(ESTATE.read_text(encoding="utf-8"))
    assert ds["source"].startswith("Synthea")
    assert len(ds["patients"]) == 40
    demo = ds["patients"][0]["mrn"]
    for key in ("encounters", "labs", "conditions", "medications"):
        assert any(x["mrn"] == demo for x in ds[key])                        # the demo patient is in all four systems
    assert all(c["display"].endswith("(disorder)") for c in ds["conditions"])
    units = {}
    for lab in ds["labs"]:
        units.setdefault(lab["local_code"], set()).add(lab["unit"])
    assert all(len(u) == 1 for u in units.values())                          # one unit per test, as TEST_REF stores it
