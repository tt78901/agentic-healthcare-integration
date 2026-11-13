"""Write the canonical dataset into four mock source systems, each in its own
legacy shape. These are the systems the agents have to discover and integrate.

  ehr/        JSON served by the MediCore EHR REST API (mock_systems.ehr_rest)
  lab/lab.db  LabTrak LIS: SQLite database with terse legacy column names
  adt/*.hl7   Patient administration feed: HL7 v2.5 ADT messages, one per file
  pharmacy/   Nightly pipe-delimited prescription exports
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import shutil
import sqlite3
from pathlib import Path

DATA_DIR = Path(__file__).parent / "_data"


def _d(iso: str) -> dt.datetime:
    return dt.datetime.fromisoformat(iso)


def build_ehr(ds: dict, out: Path) -> None:
    out.mkdir(parents=True)
    pid = {p["mrn"]: f"P-{i + 1:06d}" for i, p in enumerate(ds["patients"])}
    patients = [{
        "patientId": pid[p["mrn"]], "mrn": p["mrn"],
        "name": {"first": p["first"], "last": p["last"]},
        "dob": _d(p["dob"]).strftime("%m/%d/%Y"),  # US-style dates
        "sex": {"female": "F", "male": "M"}.get(p["sex"], "U"),
        "contact": {"phone": p["phone"], "email": None},
        "address": {"line1": p["line"], "city": p["city"], "postCode": p["postcode"], "country": p["country"]},
        "active": True,
    } for p in ds["patients"]]
    conditions = [{
        "conditionId": c["condition_id"], "patientId": pid[c["mrn"]], "mrn": c["mrn"],
        "code": {"system": "SNOMED", "value": c["snomed"], "text": c["display"]},
        "onsetDate": c["onset"], "state": "A" if c["status"] == "active" else "R",
    } for c in ds["conditions"]]
    (out / "patients.json").write_text(json.dumps(patients, indent=1), encoding="utf-8")
    (out / "conditions.json").write_text(json.dumps(conditions, indent=1), encoding="utf-8")


def build_lab(ds: dict, out: Path) -> None:
    out.mkdir(parents=True)
    db = sqlite3.connect(out / "lab.db")
    db.executescript("""
        CREATE TABLE PT_MSTR (PT_ID INTEGER PRIMARY KEY, MRN_NO TEXT NOT NULL, LNAME TEXT, FNAME TEXT,
                              DOB_DT INTEGER, SEX_CD TEXT);
        CREATE TABLE TEST_REF (TEST_CD TEXT PRIMARY KEY, LOINC_CD TEXT, TEST_NM TEXT, UOM TEXT,
                               REF_LO REAL, REF_HI REAL);
        CREATE TABLE LAB_RSLT (RSLT_ID INTEGER PRIMARY KEY, PT_ID INTEGER REFERENCES PT_MSTR(PT_ID),
                               TEST_CD TEXT REFERENCES TEST_REF(TEST_CD), RSLT_VAL TEXT, ABN_FLG TEXT,
                               RSLT_STAT TEXT, COLL_DTTM TEXT, RSLT_DTTM TEXT);
    """)
    by_mrn = {p["mrn"]: p for p in ds["patients"]}
    pt_id: dict[str, int] = {}
    for lab in ds["labs"]:  # only patients who have had bloods exist in the lab system
        if lab["mrn"] not in pt_id:
            pt_id[lab["mrn"]] = 5000 + len(pt_id) * 3
            p = by_mrn[lab["mrn"]]
            db.execute("INSERT INTO PT_MSTR VALUES (?,?,?,?,?,?)",
                       (pt_id[lab["mrn"]], p["mrn"], p["last"].upper(), p["first"].upper(),
                        int(p["dob"].replace("-", "")), {"female": "F", "male": "M"}.get(p["sex"], "U")))
    tests = {}
    for lab in ds["labs"]:
        tests[lab["local_code"]] = (lab["local_code"], lab["loinc"], lab["name"], lab["unit"], lab["ref_low"], lab["ref_high"])
    db.executemany("INSERT INTO TEST_REF VALUES (?,?,?,?,?,?)", list(tests.values()))
    stat = {"final": "F", "preliminary": "P", "corrected": "C"}
    for lab in ds["labs"]:
        db.execute("INSERT INTO LAB_RSLT VALUES (?,?,?,?,?,?,?,?)",
                   (lab["result_id"], pt_id[lab["mrn"]], lab["local_code"], str(lab["value"]), lab["flag"],
                    stat.get(lab["status"], "F"), _d(lab["collected"]).strftime("%Y%m%d%H%M%S"),
                    _d(lab["resulted"]).strftime("%Y%m%d%H%M%S")))
    db.commit()
    db.close()


def _hl7_message(event: str, when: dt.datetime, seq: int, p: dict, e: dict) -> str:
    ts = "%Y%m%d%H%M%S"
    klass = {"ambulatory": "O", "emergency": "E", "inpatient": "I"}[e["class"]]
    doc = e["attending"]
    end = _d(e["end"]).strftime(ts) if e["end"] and event != "A01" else ""
    pv1 = [""] * 46
    pv1[0], pv1[1], pv1[2], pv1[3] = "PV1", "1", klass, f"{e['ward']}^^^GENHOSP"
    pv1[7] = f"{doc['id']}^{doc['last'].upper()}^{doc['first'].upper()}^^^DR"
    pv1[19], pv1[44], pv1[45] = e["visit_no"], _d(e["start"]).strftime(ts), end
    segments = [
        f"MSH|^~\\&|PAS|GENHOSP|INTEGRATION|GENHOSP|{when.strftime(ts)}||ADT^{event}|MSG{seq:08d}|P|2.5",
        f"EVN|{event}|{when.strftime(ts)}",
        f"PID|1||{p['mrn']}^^^GENHOSP^MR||{p['last'].upper()}^{p['first'].upper()}||{p['dob'].replace('-', '')}|"
        f"{ {'female': 'F', 'male': 'M'}.get(p['sex'], 'U') }|||{p['line']}^^{p['city']}^^{p['postcode']}^{p['country']}||{p['phone']}",
        "|".join(pv1),
        f"PV2|||^{e['reason']}",
    ]
    return "\r".join(segments) + "\r"


def build_adt(ds: dict, out: Path) -> None:
    """A04 = outpatient/emergency registration (start and end known), A01 = inpatient
    admission, A03 = inpatient discharge. An inpatient stay is therefore two messages."""
    out.mkdir(parents=True)
    by_mrn = {p["mrn"]: p for p in ds["patients"]}
    events = []
    for e in ds["encounters"]:
        if e["class"] == "inpatient":
            events.append((_d(e["start"]), "A01", e))
            if e["end"]:
                events.append((_d(e["end"]), "A03", e))
        else:
            events.append((_d(e["end"] or e["start"]), "A04", e))
    for seq, (when, event, e) in enumerate(sorted(events, key=lambda x: (x[0], x[1], x[2]["visit_no"])), start=1):
        name = f"adt_{when.strftime('%Y%m%d%H%M%S')}_{seq:05d}.hl7"
        (out / name).write_bytes(_hl7_message(event, when, seq, by_mrn[e["mrn"]], e).encode("utf-8"))


def build_pharmacy(ds: dict, out: Path) -> None:
    """Three nightly exports. A prescription whose status changed is re-exported in a
    later file, so the same RX_NO can appear more than once: the latest file wins."""
    out.mkdir(parents=True)
    header = ["RX_NO", "MRN", "DRUG_DESC", "RXNORM_CD", "SIG_TXT", "QTY", "START_DT", "STAT"]
    code = {"active": "A", "stopped": "S", "completed": "C"}

    def row(m: dict, status: str) -> list:
        return [m["rx_no"], m["mrn"], m["drug"], m["rxnorm"], m["sig"], m["qty"],
                _d(m["start"]).strftime("%d/%m/%Y"), code[status]]

    meds = sorted(ds["medications"], key=lambda m: m["rx_no"])
    third = max(1, len(meds) // 3)
    batches = {"20261029": [row(m, m["status"]) for m in meds[:third]],
               "20261030": [row(m, m["status"]) for m in meds[third:2 * third]],
               "20261031": [row(m, m["status"]) for m in meds[2 * third:]]}
    # Re-exports: first shown as active, then again in the final file with the true status
    changed = [m for m in meds[:2 * third] if m["status"] != "active"][:5]
    for m in changed:
        day = "20261029" if m in meds[:third] else "20261030"
        batches[day] = [row(m, "active") if r[0] == m["rx_no"] else r for r in batches[day]]
        batches["20261031"].append(row(m, m["status"]))
    for day, rows in batches.items():
        with open(out / f"rx_export_{day}.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh, delimiter="|")
            w.writerow(header)
            w.writerows(rows)


def build_truth(ds: dict) -> dict:
    """What a correct FHIR mapping of each system contains, keyed by source record.

    Evaluation only. No agent, prompt or adapter ever reads this file: it is the
    independent yardstick that an adapter's output is measured against, and it
    exists only because this estate is synthetic.
    """
    def person(p: dict) -> dict:
        return {"mrn": p["mrn"], "family": p["last"], "given": p["first"], "birthDate": p["dob"],
                "gender": p["sex"] if p["sex"] in ("female", "male") else "unknown"}

    def contact(p: dict) -> dict:
        return {"phone": p["phone"] or None, "city": p["city"] or None, "postalCode": p["postcode"] or None}

    by_mrn = {p["mrn"]: p for p in ds["patients"]}
    klass = {"ambulatory": "AMB", "emergency": "EMER", "inpatient": "IMP"}
    lab_mrns = sorted({lab["mrn"] for lab in ds["labs"]})
    adt_mrns = sorted({e["mrn"] for e in ds["encounters"]})
    return {
        "ehr": {
            "Patient": {p["mrn"]: {**person(p), **contact(p)} for p in ds["patients"]},
            "Condition": {c["condition_id"]: {"mrn": c["mrn"], "code": c["snomed"], "clinicalStatus": c["status"],
                                              "onset": c["onset"]} for c in ds["conditions"]},
        },
        "lab": {
            "Patient": {m: person(by_mrn[m]) for m in lab_mrns},
            "Observation": {str(lab["result_id"]): {
                "mrn": lab["mrn"], "code": lab["loinc"], "value": lab["value"], "unit": lab["unit"],
                "effective": lab["collected"], "status": lab["status"], "interpretation": lab["flag"]}
                for lab in ds["labs"]},
        },
        "adt": {
            "Patient": {m: {**person(by_mrn[m]), **contact(by_mrn[m])} for m in adt_mrns},
            "Encounter": {e["visit_no"]: {"mrn": e["mrn"], "class": klass[e["class"]],
                                          "status": "finished" if e["end"] else "in-progress",
                                          "start": e["start"], "end": e["end"]} for e in ds["encounters"]},
        },
        "pharmacy": {
            "MedicationRequest": {m["rx_no"]: {"mrn": m["mrn"], "code": m["rxnorm"], "status": m["status"],
                                               "authoredOn": m["start"], "quantity": m["qty"], "dosage": m["sig"]}
                                  for m in ds["medications"]},
        },
    }


def build_all(ds: dict, data_dir: Path = DATA_DIR) -> dict:
    if data_dir.exists():
        shutil.rmtree(data_dir)
    build_ehr(ds, data_dir / "ehr")
    build_lab(ds, data_dir / "lab")
    build_adt(ds, data_dir / "adt")
    build_pharmacy(ds, data_dir / "pharmacy")
    (data_dir / "SOURCE.txt").write_text(ds["source"] + "\n", encoding="utf-8")
    (data_dir / "dataset.json").write_text(json.dumps(ds), encoding="utf-8")       # lets drift scenarios be reset
    (data_dir / "ground_truth.json").write_text(json.dumps(build_truth(ds)), encoding="utf-8")
    return {"source": ds["source"], "patients": len(ds["patients"]), "encounters": len(ds["encounters"]),
            "lab_results": len(ds["labs"]), "conditions": len(ds["conditions"]),
            "prescriptions": len(ds["medications"])}
