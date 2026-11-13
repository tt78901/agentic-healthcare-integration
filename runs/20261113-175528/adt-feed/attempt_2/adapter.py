import glob
import os
import re
from typing import Any

SYSTEM_ID = "adt-feed"
SUPPORTED = ["Patient", "Encounter"]

LOCATION_MAP = {
    "CARD3": "Cardiology Ward 3",
    "CLINIC3": "Outpatient Clinic 3",
    "ED": "Emergency Department",
    "MED1": "Medical Ward 1",
    "OPD1": "Outpatient Department 1",
    "OPD2": "Outpatient Department 2",
    "SURG2": "Surgical Ward 2",
}

CLASS_MAP = {
    "E": "EMER",
    "I": "IMP",
    "O": "AMB",
}


def clean_id(raw_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9\-\.]", "-", raw_id)
    return cleaned[:64]


def parse_hl7_date(val: str) -> str:
    val = val.strip()
    if len(val) >= 8:
        return f"{val[0:4]}-{val[4:6]}-{val[6:8]}"
    return val


def parse_hl7_datetime(val: str) -> str:
    val = val.strip()
    if len(val) >= 14:
        return f"{val[0:4]}-{val[4:6]}-{val[6:8]}T{val[8:10]}:{val[10:12]}:{val[12:14]}Z"
    elif len(val) == 8:
        return f"{val[0:4]}-{val[4:6]}-{val[6:8]}T00:00:00Z"
    return val


def get_field(seg: list[str], field_idx: int, comp_idx: int | None = None) -> str:
    if not seg or field_idx >= len(seg):
        return ""
    val = seg[field_idx].strip()
    if comp_idx is not None:
        comps = val.split("^")
        if comp_idx < len(comps):
            return comps[comp_idx].strip()
        return ""
    return val


def build_patient(mrn: str, data: dict[str, Any]) -> dict[str, Any]:
    patient_id = clean_id(f"{SYSTEM_ID}-{mrn}")
    res: dict[str, Any] = {
        "resourceType": "Patient",
        "id": patient_id,
        "meta": {"source": f"urn:system:{SYSTEM_ID}"},
        "identifier": [
            {
                "system": "http://hospital.example.org/mrn",
                "value": mrn,
            }
        ],
    }

    family = data.get("family", "")
    given = data.get("given", "")
    name_obj = {}
    if family:
        name_obj["family"] = family
    if given:
        name_obj["given"] = [given]
    if name_obj:
        res["name"] = [name_obj]

    raw_gender = data.get("gender", "")
    if raw_gender == "M":
        res["gender"] = "male"
    elif raw_gender == "F":
        res["gender"] = "female"
    else:
        res["gender"] = "unknown"

    raw_dob = data.get("birth_date", "")
    if raw_dob:
        dob_fmt = parse_hl7_date(raw_dob)
        if dob_fmt:
            res["birthDate"] = dob_fmt

    phone = data.get("telecom", "")
    if phone:
        res["telecom"] = [{"system": "phone", "value": phone}]

    line = data.get("addr_line", "")
    city = data.get("addr_city", "")
    postal = data.get("addr_postal", "")
    country = data.get("addr_country", "")
    addr_obj = {}
    if line:
        addr_obj["line"] = [line]
    if city:
        addr_obj["city"] = city
    if postal:
        addr_obj["postalCode"] = postal
    if country:
        addr_obj["country"] = country
    if addr_obj:
        res["address"] = [addr_obj]

    return res


def build_encounter(visit_num: str, data: dict[str, Any]) -> dict[str, Any]:
    enc_id = clean_id(f"{SYSTEM_ID}-{visit_num}")

    end_raw = data.get("end_time", "")
    status = "finished" if end_raw else "in-progress"

    res: dict[str, Any] = {
        "resourceType": "Encounter",
        "id": enc_id,
        "meta": {"source": f"urn:system:{SYSTEM_ID}"},
        "identifier": [{"value": visit_num}],
        "status": status,
    }

    raw_class = data.get("class", "")
    class_code = CLASS_MAP.get(raw_class, raw_class)
    if class_code:
        res["class"] = {
            "system": "http://terminology.hl7.org/CodeSystem/v3-ActCode",
            "code": class_code,
        }

    mrn = data.get("mrn", "")
    if mrn:
        res["subject"] = {
            "identifier": {
                "system": "http://hospital.example.org/mrn",
                "value": mrn,
            }
        }

    start_raw = data.get("start_time", "")
    period = {}
    if start_raw:
        period["start"] = parse_hl7_datetime(start_raw)
    if end_raw:
        period["end"] = parse_hl7_datetime(end_raw)
    if period:
        res["period"] = period

    raw_loc = data.get("location", "")
    if raw_loc:
        display_loc = LOCATION_MAP.get(raw_loc, raw_loc)
        res["location"] = [{"location": {"display": display_loc}}]

    provider = data.get("service_provider", "")
    if provider:
        res["serviceProvider"] = {"display": provider}

    doc_id = data.get("doc_id", "")
    doc_fam = data.get("doc_family", "")
    doc_giv = data.get("doc_given", "")

    indiv: dict[str, Any] = {}
    if doc_id:
        indiv["identifier"] = {"value": doc_id}
    doc_name_parts = [p for p in [doc_giv, doc_fam] if p]
    if doc_name_parts:
        indiv["display"] = " ".join(doc_name_parts)

    if indiv:
        res["participant"] = [{"individual": indiv}]

    reason = data.get("reason", "")
    if reason:
        res["reasonCode"] = [{"text": reason}]

    return res


def fetch(resource_type: str, config: dict) -> list[dict]:
    if resource_type not in SUPPORTED:
        raise ValueError("Unsupported resource type")

    folder = config.get("path", "")
    pattern = config.get("pattern", "*")

    file_paths = glob.glob(os.path.join(folder, pattern))
    if not file_paths and "pattern" in config:
        file_paths = glob.glob(os.path.join(folder, "*"))

    file_paths.sort()

    parsed_messages = []
    for fp in file_paths:
        if not os.path.isfile(fp):
            continue
        with open(fp, encoding="utf-8", newline="") as f:
            content = f.read()

        lines = [
            l.strip()
            for l in content.replace("\r\n", "\r").replace("\n", "\r").split("\r")
            if l.strip()
        ]
        if not lines:
            continue

        segs: dict[str, list[str]] = {}
        for line in lines:
            parts = line.split("|")
            if parts[0] and parts[0] not in segs:
                segs[parts[0]] = parts

        msh = segs.get("MSH", [])
        evn = segs.get("EVN", [])
        msg_ts = get_field(msh, 6) or get_field(evn, 2) or ""
        parsed_messages.append((msg_ts, fp, segs))

    parsed_messages.sort(key=lambda x: (x[0], x[1]))

    if resource_type == "Patient":
        patients_map: dict[str, dict[str, Any]] = {}
        for msg_ts, fp, segs in parsed_messages:
            pid = segs.get("PID", [])
            mrn = get_field(pid, 3, 0)
            if not mrn:
                continue

            pdata = patients_map.setdefault(mrn, {})

            fam = get_field(pid, 5, 0)
            if fam:
                pdata["family"] = fam
            giv = get_field(pid, 5, 1)
            if giv:
                pdata["given"] = giv
            gen = get_field(pid, 8)
            if gen:
                pdata["gender"] = gen
            dob = get_field(pid, 7)
            if dob:
                pdata["birth_date"] = dob
            phone = get_field(pid, 13)
            if phone:
                pdata["telecom"] = phone
            line1 = get_field(pid, 11, 0)
            if line1:
                pdata["addr_line"] = line1
            city = get_field(pid, 11, 2)
            if city:
                pdata["addr_city"] = city
            postal = get_field(pid, 11, 4)
            if postal:
                pdata["addr_postal"] = postal
            country = get_field(pid, 11, 5)
            if country:
                pdata["addr_country"] = country

        resources = [build_patient(mrn, pdata) for mrn, pdata in patients_map.items()]
        resources.sort(key=lambda r: r["id"])
        return resources

    elif resource_type == "Encounter":
        encounters_map: dict[str, dict[str, Any]] = {}
        for msg_ts, fp, segs in parsed_messages:
            pv1 = segs.get("PV1", [])
            visit_num = get_field(pv1, 19)
            if not visit_num:
                continue

            edata = encounters_map.setdefault(visit_num, {})

            pid = segs.get("PID", [])
            mrn = get_field(pid, 3, 0)
            if mrn:
                edata["mrn"] = mrn

            pclass = get_field(pv1, 2)
            if pclass:
                edata["class"] = pclass

            loc = get_field(pv1, 3, 0)
            if loc:
                edata["location"] = loc

            provider = get_field(pv1, 3, 3)
            if provider:
                edata["service_provider"] = provider

            doc_id = get_field(pv1, 7, 0)
            if doc_id:
                edata["doc_id"] = doc_id
            doc_fam = get_field(pv1, 7, 1)
            if doc_fam:
                edata["doc_family"] = doc_fam
            doc_giv = get_field(pv1, 7, 2)
            if doc_giv:
                edata["doc_given"] = doc_giv

            t_start = get_field(pv1, 44)
            if t_start:
                edata["start_time"] = t_start

            t_end = get_field(pv1, 45)
            if t_end:
                edata["end_time"] = t_end

            pv2 = segs.get("PV2", [])
            reason = get_field(pv2, 3, 1)
            if reason:
                edata["reason"] = reason

        resources = [
            build_encounter(vnum, edata) for vnum, edata in encounters_map.items()
        ]
        resources.sort(key=lambda r: r["id"])
        return resources

    return []
