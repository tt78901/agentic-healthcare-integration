"""Adapter for lab-sql (LabTrak LIS). Pattern: database_polling.

STUB FIXTURE, DELIBERATELY BROKEN: the first attempt the stub returns, so the
validation contract fails and the self-healing loop is exercised. Not model output.
Bug: dateTime values have no timezone.
"""
import pathlib
import sqlite3

SYSTEM_ID = "lab-sql"
SUPPORTED = ["Patient", "Observation"]

MRN_SYSTEM = "http://hospital.example.org/mrn"
SEX = {"F": "female", "M": "male"}
STATUS = {"F": "final", "P": "preliminary", "C": "corrected"}


def _date(value):
    s = str(value)
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"


def _datetime(ts):
    return f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}T{ts[8:10]}:{ts[10:12]}:{ts[12:14]}"


def _meta():
    return {"source": f"urn:system:{SYSTEM_ID}"}


def _patients(db):
    out = []
    for r in db.execute("SELECT PT_ID, MRN_NO, LNAME, FNAME, DOB_DT, SEX_CD FROM PT_MSTR ORDER BY PT_ID"):
        res = {"resourceType": "Patient", "id": f"{SYSTEM_ID}-{r['PT_ID']}", "meta": _meta(),
               "identifier": [{"system": MRN_SYSTEM, "value": r["MRN_NO"]}],
               "gender": SEX.get(r["SEX_CD"], "unknown")}
        name = {}
        if r["LNAME"]:
            name["family"] = r["LNAME"].title()
        if r["FNAME"]:
            name["given"] = [r["FNAME"].title()]
        if name:
            res["name"] = [name]
        if r["DOB_DT"]:
            res["birthDate"] = _date(r["DOB_DT"])
        out.append(res)
    return out


def _observations(db):
    sql = """SELECT r.RSLT_ID, r.RSLT_VAL, r.ABN_FLG, r.RSLT_STAT, r.COLL_DTTM, p.MRN_NO,
                    t.LOINC_CD, t.TEST_NM, t.UOM
             FROM LAB_RSLT r
             JOIN PT_MSTR p ON p.PT_ID = r.PT_ID
             JOIN TEST_REF t ON t.TEST_CD = r.TEST_CD
             ORDER BY r.RSLT_ID"""
    out = []
    for r in db.execute(sql):
        res = {"resourceType": "Observation", "id": f"{SYSTEM_ID}-{r['RSLT_ID']}", "meta": _meta(),
               "status": STATUS.get(r["RSLT_STAT"], "unknown"),
               "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category",
                                         "code": "laboratory"}]}],
               "code": {"coding": [{"system": "http://loinc.org", "code": r["LOINC_CD"], "display": r["TEST_NM"]}]},
               "subject": {"identifier": {"system": MRN_SYSTEM, "value": r["MRN_NO"]}}}
        if r["COLL_DTTM"]:
            res["effectiveDateTime"] = _datetime(r["COLL_DTTM"])
        if r["RSLT_VAL"] not in (None, ""):
            quantity = {"value": float(r["RSLT_VAL"])}
            if r["UOM"]:
                quantity.update({"unit": r["UOM"], "system": "http://unitsofmeasure.org", "code": r["UOM"]})
            res["valueQuantity"] = quantity
        if r["ABN_FLG"] in ("H", "L", "N"):
            res["interpretation"] = [{"coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation", "code": r["ABN_FLG"]}]}]
        out.append(res)
    return out


def fetch(resource_type, config):
    if resource_type not in SUPPORTED:
        raise ValueError("unsupported resource type")
    db = sqlite3.connect(pathlib.Path(config["path"]).as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        items = _patients(db) if resource_type == "Patient" else _observations(db)
    finally:
        db.close()
    return sorted(items, key=lambda r: r["id"])
