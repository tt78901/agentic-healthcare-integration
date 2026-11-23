import glob
import os
import os.path
import re

SYSTEM_ID = "adt-feed"
SUPPORTED = ["Patient", "Encounter"]


def _clean_id(val: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9-.]", "-", val)
    full_id = f"{SYSTEM_ID}-{cleaned}"
    return full_id[:64]


def _get_val(fields: list[str], field_num: int, comp_num: int = 1) -> str:
    if not fields:
        return ""
    if fields[0] == "MSH":
        idx = field_num - 1
    else:
        idx = field_num
    if idx < 0 or idx >= len(fields):
        return ""
    val = fields[idx]
    if not val:
        return ""
    comps = val.split("^")
    if comp_num - 1 < len(comps):
        return comps[comp_num - 1].strip()
    return ""


def _parse_date(val: str) -> str:
    v = val.strip()
    if len(v) >= 8 and v[:8].isdigit():
        return f"{v[0:4]}-{v[4:6]}-{v[6:8]}"
    return ""


def _parse_datetime(val: str) -> str:
    v = val.strip()
    if len(v) >= 14 and v[:14].isdigit():
        return f"{v[0:4]}-{v[4:6]}-{v[6:8]}T{v[8:10]}:{v[10:12]}:{v[12:14]}Z"
    elif len(v) >= 8 and v[:8].isdigit():
        return f"{v[0:4]}-{v[4:6]}-{v[6:8]}T00:00:00Z"
    return ""


def _build_patient(mrn: str, state: dict) -> dict:
    assigner = state.get("assigner", "")
    type_code = state.get("type_code", "")

    identifier = {
        "system": "http://hospital.example.org/mrn",
        "value": mrn,
    }
    if assigner:
        identifier["assigner"] = {"display": assigner}
    if type_code:
        identifier["type"] = {"coding": [{"code": type_code}]}

    patient = {
        "resourceType": "Patient",
        "id": _clean_id(mrn),
        "meta": {"source": f"urn:system:{SYSTEM_ID}"},
        "identifier": [identifier],
    }

    family = state.get("family", "")
    given = state.get("given", "")
    name_obj = {}
    if family:
        name_obj["family"] = family
    if given:
        name_obj["given"] = [given]
    if name_obj:
        patient["name"] = [name_obj]

    gender_raw = state.get("gender", "")
    if gender_raw == "M":
        patient["gender"] = "male"
    elif gender_raw == "F":
        patient["gender"] = "female"
    else:
        patient["gender"] = "unknown"

    birth_date = state.get("birth_date", "")
    if birth_date:
        formatted_dob = _parse_date(birth_date)
        if formatted_dob:
            patient["birthDate"] = formatted_dob

    telecom_val = state.get("telecom", "")
    if telecom_val:
        patient["telecom"] = [{"system": "phone", "value": telecom_val}]

    addr_line = state.get("addr_line", "")
    city = state.get("city", "")
    postal_code = state.get("postal_code", "")
    country = state.get("country", "")

    addr_obj = {}
    if addr_line:
        addr_obj["line"] = [addr_line]
    if city:
        addr_obj["city"] = city
    if postal_code:
        addr_obj["postalCode"] = postal_code
    if country:
        addr_obj["country"] = country
    if addr_obj:
        patient["address"] = [addr_obj]

    return patient


def _build_encounter(visit_id: str, state: dict) -> dict:
    pv1_class = state.get("class", "")
    class_code_map = {"E": "EMER", "I": "IMP", "O": "AMB"}
    code = class_code_map.get(pv1_class, pv1_class)

    pv1_45 = state.get("pv1_45", "")
    msh_9_2 = state.get("msh_9_2", "")

    if pv1_45 or msh_9_2 == "A03":
        status = "finished"
    else:
        status = "in-progress"

    enc = {
        "resourceType": "Encounter",
        "id": _clean_id(visit_id),
        "meta": {"source": f"urn:system:{SYSTEM_ID}"},
        "status": status,
    }

    if code:
        enc["class"] = {
            "system": "http://terminology.hl7.org/CodeSystem/v3-ActCode",
            "code": code,
        }

    mrn = state.get("mrn", "")
    if mrn:
        enc["subject"] = {
            "identifier": {
                "system": "http://hospital.example.org/mrn",
                "value": mrn,
            }
        }

    period = {}
    pv1_44 = state.get("pv1_44", "")
    if pv1_44:
        start_dt = _parse_datetime(pv1_44)
        if start_dt:
            period["start"] = start_dt

    if pv1_45:
        end_dt = _parse_datetime(pv1_45)
        if end_dt:
            period["end"] = end_dt

    if period:
        enc["period"] = period

    location_disp = state.get("location", "")
    if location_disp:
        enc["location"] = [{"location": {"display": location_disp}}]

    prov_id = state.get("prov_id", "")
    prov_surname = state.get("prov_surname", "")
    prov_given = state.get("prov_given", "")

    disp_names = [n for n in [prov_given, prov_surname] if n]
    disp_str = " ".join(disp_names)

    indiv = {}
    if prov_id:
        indiv["identifier"] = {
            "system": "http://hospital.example.org/provider-id",
            "value": prov_id,
        }
    if disp_str:
        indiv["display"] = disp_str

    if indiv:
        enc["participant"] = [{"individual": indiv}]

    reason_text = state.get("reason_text", "")
    if reason_text:
        enc["reasonCode"] = [{"text": reason_text}]

    return enc


def fetch(resource_type: str, config: dict) -> list[dict]:
    if resource_type not in SUPPORTED:
        raise ValueError("Unsupported resource type")

    path = config.get("path", ".")
    pattern = config.get("pattern", "*.hl7")
    search_path = os.path.join(path, pattern)
    file_paths = sorted(glob.glob(search_path))
    if not file_paths:
        file_paths = sorted(glob.glob(os.path.join(path, "*")))

    messages = []
    for filepath in file_paths:
        try:
            with open(filepath, encoding="utf-8", newline="") as f:
                content = f.read()
        except Exception:
            continue

        lines = content.replace("\r\n", "\r").replace("\n", "\r").split("\r")
        segments = {}
        for line in lines:
            line = line.rstrip("\r\n")
            if not line:
                continue
            parts = line.split("|")
            seg_name = parts[0]
            segments[seg_name] = parts

        msh = segments.get("MSH", [])
        timestamp = _get_val(msh, 7)
        messages.append((timestamp, filepath, segments))

    messages.sort(key=lambda m: (m[0], m[1]))

    if resource_type == "Patient":
        patients_state = {}
        for ts, filepath, segs in messages:
            pid = segs.get("PID")
            if not pid:
                continue
            mrn = _get_val(pid, 3, 1)
            if not mrn:
                continue
            if mrn not in patients_state:
                patients_state[mrn] = {}
            st = patients_state[mrn]

            val = _get_val(pid, 3, 4)
            if val:
                st["assigner"] = val
            val = _get_val(pid, 3, 5)
            if val:
                st["type_code"] = val
            val = _get_val(pid, 5, 1)
            if val:
                st["family"] = val
            val = _get_val(pid, 5, 2)
            if val:
                st["given"] = val
            val = _get_val(pid, 7)
            if val:
                st["birth_date"] = val
            val = _get_val(pid, 8)
            if val:
                st["gender"] = val
            val = _get_val(pid, 11, 1)
            if val:
                st["addr_line"] = val
            val = _get_val(pid, 11, 3)
            if val:
                st["city"] = val
            val = _get_val(pid, 11, 5)
            if val:
                st["postal_code"] = val
            val = _get_val(pid, 11, 6)
            if val:
                st["country"] = val
            val = _get_val(pid, 13)
            if val:
                st["telecom"] = val

        results = [_build_patient(mrn, st) for mrn, st in patients_state.items()]
        results.sort(key=lambda r: r["id"])
        return results

    elif resource_type == "Encounter":
        encounters_state = {}
        for ts, filepath, segs in messages:
            pv1 = segs.get("PV1")
            if not pv1:
                continue
            visit_id = _get_val(pv1, 19)
            if not visit_id:
                continue
            if visit_id not in encounters_state:
                encounters_state[visit_id] = {}
            st = encounters_state[visit_id]

            pid = segs.get("PID", [])
            msh = segs.get("MSH", [])
            pv2 = segs.get("PV2", [])

            mrn = _get_val(pid, 3, 1)
            if mrn:
                st["mrn"] = mrn

            val = _get_val(pv1, 2)
            if val:
                st["class"] = val
            val = _get_val(pv1, 3, 1)
            if val:
                st["location"] = val
            val = _get_val(pv1, 7, 1)
            if val:
                st["prov_id"] = val
            val = _get_val(pv1, 7, 2)
            if val:
                st["prov_surname"] = val
            val = _get_val(pv1, 7, 3)
            if val:
                st["prov_given"] = val
            val = _get_val(pv1, 44)
            if val:
                st["pv1_44"] = val
            val = _get_val(pv1, 45)
            if val:
                st["pv1_45"] = val

            msh_9_2 = _get_val(msh, 9, 2)
            if msh_9_2:
                st["msh_9_2"] = msh_9_2

            reason = _get_val(pv2, 3, 2)
            if reason:
                st["reason_text"] = reason

        results = [_build_encounter(v_id, st) for v_id, st in encounters_state.items()]
        results.sort(key=lambda r: r["id"])
        return results

    return []
