import pathlib
import re
import sqlite3

SYSTEM_ID = "lab-sql"
SUPPORTED = ["Patient", "Observation"]


def _clean_id(raw_id: str) -> str:
    cleaned = re.sub(r'[^A-Za-z0-9.-]', '-', raw_id)
    return cleaned[:64]


def _fetch_patients(conn: sqlite3.Connection) -> list[dict]:
    cursor = conn.cursor()
    cursor.execute("SELECT PT_ID, MRN_NO, LNAME, FNAME, DOB_DT, SEX_CD FROM PT_MSTR")
    rows = cursor.fetchall()
    results = []

    sex_map = {"M": "male", "F": "female"}

    for row in rows:
        pt_id = row["PT_ID"]
        mrn_no = row["MRN_NO"]
        lname = row["LNAME"]
        fname = row["FNAME"]
        dob_dt = row["DOB_DT"]
        sex_cd = row["SEX_CD"]

        patient_id = _clean_id(f"{SYSTEM_ID}-{pt_id}")

        resource = {
            "resourceType": "Patient",
            "id": patient_id,
            "meta": {"source": f"urn:system:{SYSTEM_ID}"}
        }

        if mrn_no:
            resource["identifier"] = [
                {
                    "system": "http://hospital.example.org/mrn",
                    "value": str(mrn_no)
                }
            ]

        name_entry = {}
        if lname:
            name_entry["family"] = str(lname)
        if fname:
            name_entry["given"] = [str(fname)]
        if name_entry:
            resource["name"] = [name_entry]

        if sex_cd:
            resource["gender"] = sex_map.get(str(sex_cd).strip().upper(), "unknown")
        else:
            resource["gender"] = "unknown"

        if dob_dt is not None:
            dob_str = str(dob_dt).strip()
            if len(dob_str) == 8 and dob_str.isdigit():
                resource["birthDate"] = f"{dob_str[0:4]}-{dob_str[4:6]}-{dob_str[6:8]}"

        results.append(resource)

    results.sort(key=lambda x: x["id"])
    return results


def _fetch_observations(conn: sqlite3.Connection) -> list[dict]:
    cursor = conn.cursor()
    query = """
    SELECT 
        r.RSLT_ID,
        r.PT_ID,
        r.TEST_CD,
        r.RSLT_VAL,
        r.ABN_FLG,
        r.RSLT_STAT,
        r.COLL_DTTM,
        r.RSLT_DTTM,
        p.MRN_NO,
        t.LOINC_CD,
        t.TEST_NM,
        t.UOM
    FROM LAB_RSLT r
    LEFT JOIN PT_MSTR p ON r.PT_ID = p.PT_ID
    LEFT JOIN TEST_REF t ON r.TEST_CD = t.TEST_CD
    """
    cursor.execute(query)
    rows = cursor.fetchall()
    results = []

    stat_map = {
        "F": "final",
        "C": "corrected",
        "P": "preliminary"
    }

    for row in rows:
        rslt_id = row["RSLT_ID"]
        rslt_val = row["RSLT_VAL"]
        abn_flg = row["ABN_FLG"]
        rslt_stat = row["RSLT_STAT"]
        coll_dttm = row["COLL_DTTM"]
        mrn_no = row["MRN_NO"]
        loinc_cd = row["LOINC_CD"]
        test_cd = row["TEST_CD"]
        test_nm = row["TEST_NM"]
        uom = row["UOM"]

        obs_id = _clean_id(f"{SYSTEM_ID}-{rslt_id}")
        status = stat_map.get(str(rslt_stat or "").strip().upper(), "unknown")

        resource = {
            "resourceType": "Observation",
            "id": obs_id,
            "meta": {"source": f"urn:system:{SYSTEM_ID}"},
            "status": status,
            "category": [
                {
                    "coding": [
                        {
                            "system": "http://terminology.hl7.org/CodeSystem/observation-category",
                            "code": "laboratory"
                        }
                    ]
                }
            ]
        }

        coding_obj = {}
        if loinc_cd:
            coding_obj["system"] = "http://loinc.org"
            coding_obj["code"] = str(loinc_cd)
        elif test_cd:
            coding_obj["code"] = str(test_cd)

        if test_nm:
            coding_obj["display"] = str(test_nm)

        if coding_obj:
            resource["code"] = {"coding": [coding_obj]}

        if mrn_no:
            resource["subject"] = {
                "identifier": {
                    "system": "http://hospital.example.org/mrn",
                    "value": str(mrn_no)
                }
            }

        if coll_dttm:
            dttm_str = str(coll_dttm).strip()
            if len(dttm_str) == 14 and dttm_str.isdigit():
                resource["effectiveDateTime"] = f"{dttm_str[0:4]}-{dttm_str[4:6]}-{dttm_str[6:8]}T{dttm_str[8:10]}:{dttm_str[10:12]}:{dttm_str[12:14]}Z"
            elif len(dttm_str) == 8 and dttm_str.isdigit():
                resource["effectiveDateTime"] = f"{dttm_str[0:4]}-{dttm_str[4:6]}-{dttm_str[6:8]}"

        if rslt_val is not None:
            val_str = str(rslt_val).strip()
            if val_str:
                try:
                    num_val = float(val_str)
                    if num_val.is_integer():
                        num_val = int(num_val)
                    vq = {"value": num_val}
                    if uom:
                        vq["unit"] = str(uom)
                        vq["system"] = "http://unitsofmeasure.org"
                        vq["code"] = str(uom)
                    resource["valueQuantity"] = vq
                except ValueError:
                    resource["valueString"] = val_str

        if abn_flg:
            clean_flg = str(abn_flg).strip().upper()
            if clean_flg in ("H", "L", "N"):
                resource["interpretation"] = [
                    {
                        "coding": [
                            {
                                "system": "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation",
                                "code": clean_flg
                            }
                        ]
                    }
                ]

        results.append(resource)

    results.sort(key=lambda x: x["id"])
    return results


def fetch(resource_type: str, config: dict) -> list[dict]:
    if resource_type not in SUPPORTED:
        raise ValueError("Unsupported resource type")

    db_path = config.get("path")
    if not db_path:
        return []

    uri = pathlib.Path(db_path).as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row

    try:
        if resource_type == "Patient":
            return _fetch_patients(conn)
        elif resource_type == "Observation":
            return _fetch_observations(conn)
    finally:
        conn.close()
