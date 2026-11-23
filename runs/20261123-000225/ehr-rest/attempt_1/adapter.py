import re
from datetime import datetime
import httpx

SYSTEM_ID = "ehr-rest"
SUPPORTED = ["Patient", "Condition"]


def sanitize_id(raw_id: str) -> str:
    s = re.sub(r"[^A-Za-z0-9.-]", "-", str(raw_id))
    return s[:64]


def clean_dict(val):
    if isinstance(val, dict):
        cleaned = {k: clean_dict(v) for k, v in val.items()}
        return {k: v for k, v in cleaned.items() if v not in (None, "", [], {})}
    elif isinstance(val, list):
        cleaned = [clean_dict(v) for v in val]
        return [v for v in cleaned if v not in (None, "", [], {})}
    else:
        return val


def format_dob(dob_str: str):
    if not dob_str:
        return None
    try:
        dt = datetime.strptime(dob_str, "%m/%d/%Y")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return dob_str


def fetch_records(endpoint: str, config: dict) -> list[dict]:
    base_url = config.get("base_url", "")
    headers = config.get("headers", {})
    records = []
    page = 1
    page_size = 100

    with httpx.Client(base_url=base_url, headers=headers, timeout=30) as client:
        while True:
            response = client.get(endpoint, params={"page": page, "pageSize": page_size})
            response.raise_for_status()
            payload = response.json()
            data = payload.get("data", [])
            if not data:
                break
            records.extend(data)
            paging = payload.get("paging", {})
            total = paging.get("total")
            if total is not None and len(records) >= total:
                break
            if len(data) < page_size:
                break
            page += 1

    return records


def transform_patient(item: dict) -> dict:
    patient_id = item.get("patientId")
    fhir_id = sanitize_id(f"ehr-rest-{patient_id}")
    mrn = item.get("mrn")

    name_dict = item.get("name") or {}
    first_name = name_dict.get("first")
    last_name = name_dict.get("last")
    given = [first_name] if first_name else []

    sex = item.get("sex")
    gender_map = {"F": "female", "M": "male"}
    gender = gender_map.get(sex, "unknown") if sex else "unknown"

    contact = item.get("contact") or {}
    phone = contact.get("phone")

    address_dict = item.get("address") or {}
    line1 = address_dict.get("line1")

    res = {
        "resourceType": "Patient",
        "id": fhir_id,
        "meta": {"source": "urn:system:ehr-rest"},
        "identifier": [
            {
                "system": "http://hospital.example.org/mrn",
                "value": str(mrn),
            }
        ]
        if mrn is not None
        else [],
        "active": item.get("active"),
        "name": [
            {
                "family": last_name,
                "given": given,
            }
        ],
        "gender": gender,
        "birthDate": format_dob(item.get("dob")),
        "telecom": [
            {
                "system": "phone",
                "value": str(phone),
                "use": "home",
            }
        ]
        if phone
        else [],
        "address": [
            {
                "line": [line1] if line1 else [],
                "city": address_dict.get("city"),
                "postalCode": address_dict.get("postCode"),
                "country": address_dict.get("country"),
            }
        ],
    }

    return clean_dict(res)


def transform_condition(item: dict) -> dict:
    condition_id = item.get("conditionId")
    fhir_id = sanitize_id(f"ehr-rest-{condition_id}")
    mrn = item.get("mrn")

    state = item.get("state")
    state_map = {"A": "active", "R": "resolved"}
    clinical_status_code = state_map.get(state)
    clinical_status = (
        {
            "coding": [
                {
                    "system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
                    "code": clinical_status_code,
                }
            ]
        }
        if clinical_status_code
        else None
    )

    code_obj = item.get("code") or {}
    code_val = code_obj.get("value")
    code_text = code_obj.get("text")

    res = {
        "resourceType": "Condition",
        "id": fhir_id,
        "meta": {"source": "urn:system:ehr-rest"},
        "clinicalStatus": clinical_status,
        "code": {
            "coding": [
                {
                    "system": "http://snomed.info/sct",
                    "code": str(code_val) if code_val is not None else None,
                    "display": code_text,
                }
            ]
        },
        "subject": {
            "identifier": {
                "system": "http://hospital.example.org/mrn",
                "value": str(mrn),
            }
        }
        if mrn is not None
        else None,
        "onsetDateTime": item.get("onsetDate"),
    }

    return clean_dict(res)


def fetch(resource_type: str, config: dict) -> list[dict]:
    if resource_type not in SUPPORTED:
        raise ValueError(f"Unsupported resource type: {resource_type}")

    if resource_type == "Patient":
        raw_records = fetch_records("/api/v2/patients", config)
        by_id = {}
        for rec in raw_records:
            transformed = transform_patient(rec)
            by_id[transformed["id"]] = transformed
        return sorted(by_id.values(), key=lambda x: x["id"])

    elif resource_type == "Condition":
        raw_records = fetch_records("/api/v2/conditions", config)
        by_id = {}
        for rec in raw_records:
            transformed = transform_condition(rec)
            by_id[transformed["id"]] = transformed
        return sorted(by_id.values(), key=lambda x: x["id"])

    return []
