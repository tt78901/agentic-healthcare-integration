import re
from typing import Any, Dict, List
import httpx

SYSTEM_ID = "ehr-rest"
SUPPORTED = ["Condition", "Patient"]


def _clean_id(raw_key: Any) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9\-.]", "-", str(raw_key))
    return f"{SYSTEM_ID}-{cleaned}"[:64]


def _format_dob(dob_str: str) -> str:
    if not dob_str:
        return ""
    parts = dob_str.strip().split("/")
    if len(parts) == 3:
        try:
            mm, dd, yyyy = parts
            return f"{int(yyyy):04d}-{int(mm):02d}-{int(dd):02d}"
        except ValueError:
            return ""
    return dob_str.strip()


def _format_datetime(val_str: str) -> str:
    if not val_str:
        return ""
    val = val_str.strip()
    if not val:
        return ""
    if re.match(r"^\d{4}-\d{2}-\d{2}$", val):
        return val
    if "T" in val:
        if val.endswith("Z") or "+" in val or re.search(r"-\d{2}:\d{2}$", val):
            return val
        return val + "Z"
    return val


def _transform_patient(record: Dict[str, Any]) -> Dict[str, Any]:
    pid = record.get("patientId")
    if pid is None or str(pid).strip() == "":
        return {}

    fhir_id = _clean_id(pid)
    res: Dict[str, Any] = {
        "resourceType": "Patient",
        "id": fhir_id,
        "meta": {"source": f"urn:system:{SYSTEM_ID}"},
    }

    if "active" in record and record["active"] is not None:
        res["active"] = bool(record["active"])

    mrn = record.get("mrn")
    if mrn is not None and str(mrn).strip() != "":
        res["identifier"] = [{
            "system": "http://hospital.example.org/mrn",
            "value": str(mrn).strip(),
        }]

    name_obj = record.get("name")
    if isinstance(name_obj, dict):
        name_entry: Dict[str, Any] = {}
        last = name_obj.get("last")
        if last is not None and str(last).strip() != "":
            name_entry["family"] = str(last).strip()
        first = name_obj.get("first")
        if first is not None and str(first).strip() != "":
            name_entry["given"] = [str(first).strip()]
        if name_entry:
            res["name"] = [name_entry]

    sex_raw = record.get("sex")
    if sex_raw is not None and str(sex_raw).strip() != "":
        s = str(sex_raw).strip().upper()
        if s == "M":
            res["gender"] = "male"
        elif s == "F":
            res["gender"] = "female"
        elif s == "O":
            res["gender"] = "other"
        else:
            res["gender"] = "unknown"

    dob = record.get("dob")
    if dob is not None and str(dob).strip() != "":
        formatted_dob = _format_dob(str(dob))
        if formatted_dob:
            res["birthDate"] = formatted_dob

    contact_obj = record.get("contact")
    if isinstance(contact_obj, dict):
        phone = contact_obj.get("phone")
        if phone is not None and str(phone).strip() != "":
            res["telecom"] = [{
                "system": "phone",
                "value": str(phone).strip(),
            }]

    addr_obj = record.get("address")
    if isinstance(addr_obj, dict):
        addr_entry: Dict[str, Any] = {}
        line1 = addr_obj.get("line1")
        if line1 is not None and str(line1).strip() != "":
            addr_entry["line"] = [str(line1).strip()]
        city = addr_obj.get("city")
        if city is not None and str(city).strip() != "":
            addr_entry["city"] = str(city).strip()
        post_code = addr_obj.get("postCode")
        if post_code is not None and str(post_code).strip() != "":
            addr_entry["postalCode"] = str(post_code).strip()
        country = addr_obj.get("country")
        if country is not None and str(country).strip() != "":
            addr_entry["country"] = str(country).strip()
        if addr_entry:
            res["address"] = [addr_entry]

    return res


def _transform_condition(record: Dict[str, Any]) -> Dict[str, Any]:
    cid = record.get("conditionId")
    if cid is None or str(cid).strip() == "":
        return {}

    fhir_id = _clean_id(cid)
    res: Dict[str, Any] = {
        "resourceType": "Condition",
        "id": fhir_id,
        "meta": {"source": f"urn:system:{SYSTEM_ID}"},
    }

    state = record.get("state")
    if state is not None and str(state).strip() != "":
        st = str(state).strip().upper()
        code_val = None
        if st == "A":
            code_val = "active"
        elif st == "R":
            code_val = "resolved"
        if code_val:
            res["clinicalStatus"] = {
                "coding": [{
                    "system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
                    "code": code_val,
                }]
            }

    code_obj = record.get("code")
    if isinstance(code_obj, dict):
        c_val = code_obj.get("value")
        c_text = code_obj.get("text")
        if c_val is not None and str(c_val).strip() != "":
            coding: Dict[str, str] = {
                "system": "http://snomed.info/sct",
                "code": str(c_val).strip(),
            }
            if c_text is not None and str(c_text).strip() != "":
                coding["display"] = str(c_text).strip()
            res["code"] = {"coding": [coding]}

    mrn = record.get("mrn")
    if mrn is not None and str(mrn).strip() != "":
        res["subject"] = {
            "identifier": {
                "system": "http://hospital.example.org/mrn",
                "value": str(mrn).strip(),
            }
        }

    onset_date = record.get("onsetDate")
    if onset_date is not None and str(onset_date).strip() != "":
        formatted_onset = _format_datetime(str(onset_date))
        if formatted_onset:
            res["onsetDateTime"] = formatted_onset

    return res


def fetch(resource_type: str, config: dict) -> List[dict]:
    if resource_type not in SUPPORTED:
        raise ValueError("Unsupported resource type")

    base_url = config["base_url"]
    headers = config.get("headers", {})

    endpoint = "/api/v2/patients" if resource_type == "Patient" else "/api/v2/conditions"

    raw_records: List[Dict[str, Any]] = []
    with httpx.Client(base_url=base_url, headers=headers, timeout=30.0) as client:
        page = 1
        page_size = 100
        while True:
            resp = client.get(endpoint, params={"page": page, "pageSize": page_size})
            resp.raise_for_status()
            payload = resp.json()

            items = payload.get("data", [])
            if not items:
                break

            raw_records.extend(items)

            paging = payload.get("paging", {})
            total = paging.get("total")
            if total is not None and len(raw_records) >= total:
                break
            if len(items) < page_size and total is None:
                break

            page += 1

    mapped_by_id: Dict[str, dict] = {}
    for rec in raw_records:
        if resource_type == "Patient":
            fhir_res = _transform_patient(rec)
        else:
            fhir_res = _transform_condition(rec)

        if fhir_res and "id" in fhir_res:
            mapped_by_id[fhir_res["id"]] = fhir_res

    return [mapped_by_id[k] for k in sorted(mapped_by_id.keys())]
