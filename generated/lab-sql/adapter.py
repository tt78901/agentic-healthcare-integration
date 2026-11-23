import pathlib
import re
import sqlite3
from typing import Any, Dict, List, Optional

SYSTEM_ID = "lab-sql"
SUPPORTED = ["Patient", "Observation"]


def _clean_id(val: Any) -> str:
    s = str(val) if val is not None else ""
    cleaned = re.sub(r"[^A-Za-z0-9\-\.]", "-", s)
    return cleaned[:64]


def _format_date(val: Any) -> Optional[str]:
    if val is None:
        return None
    s = str(val).strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    return None


def _format_datetime(val: Any) -> Optional[str]:
    if val is None:
        return None
    s = str(val).strip()
    if len(s) == 14 and s.isdigit():
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}T{s[8:10]}:{s[10:12]}:{s[12:14]}Z"
    return None


def _fetch_patients(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    cur = conn.cursor()
    cur.execute("SELECT PT_ID, MRN_NO, LNAME, FNAME, DOB_DT, SEX_CD FROM PT_MSTR")
    rows = cur.fetchall()

    patients_by_id: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        pt_id = row["PT_ID"]
        if pt_id is None:
            continue

        res_id = _clean_id(f"lab-sql-{pt_id}")
        res: Dict[str, Any] = {
            "resourceType": "Patient",
            "id": res_id,
            "meta": {"source": f"urn:system:{SYSTEM_ID}"},
        }

        mrn = row["MRN_NO"]
        if mrn is not None and str(mrn).strip():
            res["identifier"] = [
                {
                    "system": "http://hospital.example.org/mrn",
                    "value": str(mrn).strip(),
                }
            ]

        lname = row["LNAME"]
        fname = row["FNAME"]
        family = str(lname).strip() if lname is not None and str(lname).strip() else None
        given = [str(fname).strip()] if fname is not None and str(fname).strip() else []

        name_obj: Dict[str, Any] = {}
        if family:
            name_obj["family"] = family
        if given:
            name_obj["given"] = given
        if name_obj:
            res["name"] = [name_obj]

        sex = row["SEX_CD"]
        if sex is not None and str(sex).strip():
            sex_str = str(sex).strip().upper()
            sex_map = {"M": "male", "F": "female"}
            res["gender"] = sex_map.get(sex_str, "unknown")
        else:
            res["gender"] = "unknown"

        birth_date = _format_date(row["DOB_DT"])
        if birth_date:
            res["birthDate"] = birth_date

        patients_by_id[res_id] = res

    results = list(patients_by_id.values())
    results.sort(key=lambda r: r["id"])
    return results


def _fetch_observations(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    cur = conn.cursor()
    sql = """
    SELECT
        r.RSLT_ID,
        r.PT_ID,
        r.TEST_CD,
        r.RSLT_VAL,
        r.ABN_FLG,
        r.RSLT_STAT,
        r.COLL_DTTM,
        r.RSLT_DTTM,
        t.LOINC_CD,
        t.TEST_NM,
        t.UOM,
        p.MRN_NO
    FROM LAB_RSLT r
    LEFT JOIN TEST_REF t ON r.TEST_CD = t.TEST_CD
    LEFT JOIN PT_MSTR p ON r.PT_ID = p.PT_ID
    """
    cur.execute(sql)
    rows = cur.fetchall()

    obs_by_id: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        rslt_id = row["RSLT_ID"]
        if rslt_id is None:
            continue

        res_id = _clean_id(f"lab-sql-{rslt_id}")
        res: Dict[str, Any] = {
            "resourceType": "Observation",
            "id": res_id,
            "meta": {"source": f"urn:system:{SYSTEM_ID}"},
        }

        stat_val = row["RSLT_STAT"]
        stat_str = str(stat_val).strip().upper() if stat_val is not None else ""
        stat_map = {"F": "final", "P": "preliminary", "C": "corrected"}
        res["status"] = stat_map.get(stat_str, "registered")

        res["category"] = [
            {
                "coding": [
                    {
                        "system": "http://terminology.hl7.org/CodeSystem/observation-category",
                        "code": "laboratory",
                    }
                ]
            }
        ]

        loinc = row["LOINC_CD"]
        test_nm = row["TEST_NM"]
        coding_obj: Dict[str, str] = {"system": "http://loinc.org"}
        if loinc is not None and str(loinc).strip():
            coding_obj["code"] = str(loinc).strip()
        if test_nm is not None and str(test_nm).strip():
            coding_obj["display"] = str(test_nm).strip()

        if "code" in coding_obj or "display" in coding_obj:
            res["code"] = {"coding": [coding_obj]}

        mrn = row["MRN_NO"]
        if mrn is not None and str(mrn).strip():
            res["subject"] = {
                "identifier": {
                    "system": "http://hospital.example.org/mrn",
                    "value": str(mrn).strip(),
                }
            }

        eff_dt = _format_datetime(row["COLL_DTTM"])
        if eff_dt:
            res["effectiveDateTime"] = eff_dt

        rslt_val = row["RSLT_VAL"]
        if rslt_val is not None:
            val_str = str(rslt_val).strip()
            try:
                val_num = float(val_str)
                vq: Dict[str, Any] = {"value": val_num}
                uom = row["UOM"]
                if uom is not None and str(uom).strip():
                    uom_str = str(uom).strip()
                    vq["unit"] = uom_str
                    vq["system"] = "http://unitsofmeasure.org"
                    vq["code"] = uom_str
                res["valueQuantity"] = vq
            except ValueError:
                pass

        abn_flg = row["ABN_FLG"]
        if abn_flg is not None:
            abn_str = str(abn_flg).strip().upper()
            if abn_str in ("H", "L", "N"):
                res["interpretation"] = [
                    {
                        "coding": [
                            {
                                "system": "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation",
                                "code": abn_str,
                            }
                        ]
                    }
                ]

        obs_by_id[res_id] = res

    results = list(obs_by_id.values())
    results.sort(key=lambda r: r["id"])
    return results


def fetch(resource_type: str, config: dict) -> list[dict]:
    """Return ALL resources of that type as FHIR R4 JSON dicts, sorted by id.
    Raise ValueError for a resource type not in SUPPORTED."""
    if resource_type not in SUPPORTED:
        raise ValueError(f"Unsupported resource type: {resource_type}")

    db_path = config["path"]
    uri = pathlib.Path(db_path).as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row

    try:
        if resource_type == "Patient":
            return _fetch_patients(conn)
        elif resource_type == "Observation":
            return _fetch_observations(conn)
        else:
            raise ValueError(f"Unsupported resource type: {resource_type}")
    finally:
        conn.close()
