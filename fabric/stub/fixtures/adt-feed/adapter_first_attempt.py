"""Adapter for adt-feed (patient administration HL7 v2 feed). Pattern: message_feed_ingest.

STUB FIXTURE, DELIBERATELY BROKEN: the first attempt the stub returns, so the
validation contract fails and the self-healing loop is exercised. Not model output.
Bug: admission and discharge messages for one visit are not merged.
"""
import glob
import os
import re

SYSTEM_ID = "adt-feed"
SUPPORTED = ["Patient", "Encounter"]

MRN_SYSTEM = "http://hospital.example.org/mrn"
SEX = {"F": "female", "M": "male"}
CLASS = {"O": "AMB", "E": "EMER", "I": "IMP"}


def _messages(config):
    """Parsed messages in file-name order (file names start with the event timestamp)."""
    out = []
    for path in sorted(glob.glob(os.path.join(config["path"], config.get("pattern", "*.hl7")))):
        with open(path, encoding="utf-8", newline="") as fh:
            segments = {}
            for seg in re.split(r"[\r\n]+", fh.read()):
                if seg:
                    fields = seg.split("|")
                    segments.setdefault(fields[0], fields)
        if "PID" in segments and "PV1" in segments:
            out.append(segments)
    return out


def _field(fields, index, component=None):
    if index >= len(fields) or fields[index] == "":
        return ""
    if component is None:
        return fields[index]
    parts = fields[index].split("^")
    return parts[component - 1] if component <= len(parts) else ""


def _date(value):
    return f"{value[0:4]}-{value[4:6]}-{value[6:8]}"


def _datetime(ts):
    # Source timestamps carry no timezone; estate convention treats them as UTC.
    return f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}T{ts[8:10]}:{ts[10:12]}:{ts[12:14]}Z"


def _safe(value):
    return re.sub(r"[^A-Za-z0-9\-.]", "-", value)


def _meta():
    return {"source": f"urn:system:{SYSTEM_ID}"}


def _patients(messages):
    latest = {}
    for m in messages:  # later messages overwrite earlier demographics
        mrn = _field(m["PID"], 3, 1)
        if mrn:
            latest[mrn] = m["PID"]
    out = []
    for mrn, pid in latest.items():
        res = {"resourceType": "Patient", "id": f"{SYSTEM_ID}-{_safe(mrn)}", "meta": _meta(),
               "identifier": [{"system": MRN_SYSTEM, "value": mrn}],
               "gender": SEX.get(_field(pid, 8), "unknown")}
        name = {}
        if _field(pid, 5, 1):
            name["family"] = _field(pid, 5, 1).title()
        if _field(pid, 5, 2):
            name["given"] = [_field(pid, 5, 2).title()]
        if name:
            res["name"] = [name]
        if _field(pid, 7):
            res["birthDate"] = _date(_field(pid, 7))
        address = {}
        if _field(pid, 11, 1):
            address["line"] = [_field(pid, 11, 1)]
        if _field(pid, 11, 3):
            address["city"] = _field(pid, 11, 3)
        if _field(pid, 11, 5):
            address["postalCode"] = _field(pid, 11, 5)
        if _field(pid, 11, 6):
            address["country"] = _field(pid, 11, 6)
        if address:
            res["address"] = [address]
        if _field(pid, 13):
            res["telecom"] = [{"system": "phone", "value": _field(pid, 13)}]
        out.append(res)
    return out


def _encounters(messages):
    out = []
    for m in messages:
        pv1 = m["PV1"]
        visit_no = _field(pv1, 19)
        if not visit_no:
            continue
        res = {"resourceType": "Encounter", "id": f"{SYSTEM_ID}-{_safe(visit_no)}", "meta": _meta(),
               "status": "finished" if _field(pv1, 45) else "in-progress",
               "class": {"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode", "code": CLASS.get(_field(pv1, 2), "AMB")},
               "subject": {"identifier": {"system": MRN_SYSTEM, "value": _field(m["PID"], 3, 1)}}}
        period = {}
        if _field(pv1, 44):
            period["start"] = _datetime(_field(pv1, 44))
        if _field(pv1, 45):
            period["end"] = _datetime(_field(pv1, 45))
        if period:
            res["period"] = period
        out.append(res)
    return out


def fetch(resource_type, config):
    if resource_type not in SUPPORTED:
        raise ValueError("unsupported resource type")
    messages = _messages(config)
    items = _patients(messages) if resource_type == "Patient" else _encounters(messages)
    return sorted(items, key=lambda r: r["id"])
