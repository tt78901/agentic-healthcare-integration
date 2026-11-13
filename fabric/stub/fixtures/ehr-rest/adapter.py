"""Adapter for ehr-rest (MediCore EHR REST API). Pattern: api_facade.

STUB FIXTURE: hand-written for smoke tests. Not model output.
"""
import re

import httpx

SYSTEM_ID = "ehr-rest"
SUPPORTED = ["Patient", "Condition"]

MRN_SYSTEM = "http://hospital.example.org/mrn"
SEX = {"F": "female", "M": "male"}
STATE = {"A": "active", "R": "resolved"}
PAGE_SIZE = 100


def _all(config, path):
    """Every item of a paged list endpoint."""
    items = []
    with httpx.Client(base_url=config["base_url"], headers=config.get("headers") or {}, timeout=30) as client:
        page = 1
        while True:
            resp = client.get(path, params={"page": page, "pageSize": PAGE_SIZE})
            resp.raise_for_status()
            body = resp.json()
            batch = body.get("data") or []
            items.extend(batch)
            if not batch or len(items) >= body.get("paging", {}).get("total", 0):
                return items
            page += 1


def _safe(value):
    return re.sub(r"[^A-Za-z0-9\-.]", "-", str(value))


def _meta():
    return {"source": f"urn:system:{SYSTEM_ID}"}


def _us_date(value):
    month, day, year = value.split("/")
    return f"{year}-{month}-{day}"


def _patients(config):
    out = []
    for p in _all(config, "/api/v2/patients"):
        res = {"resourceType": "Patient", "id": f"{SYSTEM_ID}-{_safe(p['patientId'])}", "meta": _meta(),
               "identifier": [{"system": MRN_SYSTEM, "value": p["mrn"]}],
               "gender": SEX.get(p.get("sex"), "unknown")}
        if isinstance(p.get("active"), bool):
            res["active"] = p["active"]
        name = {}
        if (p.get("name") or {}).get("last"):
            name["family"] = p["name"]["last"]
        if (p.get("name") or {}).get("first"):
            name["given"] = [p["name"]["first"]]
        if name:
            res["name"] = [name]
        if p.get("dob"):
            res["birthDate"] = _us_date(p["dob"])
        telecom = []
        if (p.get("contact") or {}).get("phone"):
            telecom.append({"system": "phone", "value": p["contact"]["phone"]})
        if (p.get("contact") or {}).get("email"):
            telecom.append({"system": "email", "value": p["contact"]["email"]})
        if telecom:
            res["telecom"] = telecom
        src = p.get("address") or {}
        address = {}
        if src.get("line1"):
            address["line"] = [src["line1"]]
        if src.get("city"):
            address["city"] = src["city"]
        if src.get("postCode"):
            address["postalCode"] = src["postCode"]
        if src.get("country"):
            address["country"] = src["country"]
        if address:
            res["address"] = [address]
        out.append(res)
    return out


def _conditions(config):
    out = []
    for c in _all(config, "/api/v2/conditions"):
        coding = {"system": "http://snomed.info/sct", "code": c["code"]["value"]}
        if c["code"].get("text"):
            coding["display"] = c["code"]["text"]
        res = {"resourceType": "Condition", "id": f"{SYSTEM_ID}-{_safe(c['conditionId'])}", "meta": _meta(),
               "clinicalStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
                                              "code": STATE.get(c.get("state"), "active")}]},
               "code": {"coding": [coding]},
               "subject": {"identifier": {"system": MRN_SYSTEM, "value": c["mrn"]}}}
        if c.get("onsetDate"):
            res["onsetDateTime"] = c["onsetDate"]
        out.append(res)
    return out


def fetch(resource_type, config):
    if resource_type not in SUPPORTED:
        raise ValueError("unsupported resource type")
    items = _patients(config) if resource_type == "Patient" else _conditions(config)
    return sorted(items, key=lambda r: r["id"])
