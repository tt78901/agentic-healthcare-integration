"""Load a Synthea FHIR R4 export into the canonical dataset shape.

Usage:  python -m fabric seed --synthea path/to/synthea/output/fhir

Each Synthea patient is one ``*.json`` Bundle. Only the resource types the mock
systems need are read: Patient, Encounter, Observation (laboratory, numeric),
Condition and MedicationRequest.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from pathlib import Path

from .dataset import LAB_TESTS

_CLASS = {"AMB": "ambulatory", "EMER": "emergency", "IMP": "inpatient", "ACUTE": "inpatient",
          "NONAC": "inpatient", "OBSENC": "emergency", "UC": "emergency", "HH": "ambulatory", "VR": "ambulatory"}
_LOCAL_BY_LOINC = {t[1]: t for t in LAB_TESTS}


def _naive(ts: str | None) -> str | None:
    """Synthea timestamps carry an offset; the legacy systems store naive local time."""
    if not ts:
        return None
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).replace(tzinfo=None).isoformat(timespec="seconds")


def _clean(name: str | None) -> str:
    return re.sub(r"\d+", "", name or "").strip()  # Synthea appends digits to names


def _ref(resource: dict, key: str) -> str:
    return (resource.get(key) or {}).get("reference", "").replace("urn:uuid:", "").split("/")[-1]


def load_synthea(folder: str, max_patients: int = 40, since: str = "2024-11-01") -> dict:
    files = sorted(p for p in Path(folder).glob("*.json")
                   if not p.name.startswith(("hospitalInformation", "practitionerInformation")))
    out = {"source": f"Synthea export ({folder})", "patients": [], "encounters": [], "labs": [],
           "conditions": [], "medications": []}
    used: set[str] = set()
    seq = {"visit": 0, "result": 0, "cond": 0, "rx": 0}
    local_codes: dict[str, str] = {}

    for path in files[:max_patients]:
        bundle = json.loads(path.read_text(encoding="utf-8"))
        resources = [e["resource"] for e in bundle.get("entry", []) if "resource" in e]
        pat = next((r for r in resources if r["resourceType"] == "Patient"), None)
        if not pat:
            continue
        # Synthea MRNs are UUIDs; derive a short, stable 7-digit MRN instead
        n = int(hashlib.sha1(pat["id"].encode()).hexdigest()[:8], 16) % 1000000
        while f"7{n:06d}" in used:
            n = (n + 1) % 1000000
        mrn = f"7{n:06d}"
        used.add(mrn)
        name = (pat.get("name") or [{}])[0]
        addr = (pat.get("address") or [{}])[0]
        out["patients"].append({
            "mrn": mrn, "first": _clean((name.get("given") or [""])[0]), "last": _clean(name.get("family")),
            "dob": pat.get("birthDate"), "sex": pat.get("gender", "unknown"),
            "phone": ((pat.get("telecom") or [{}])[0]).get("value", ""),
            "line": (addr.get("line") or [""])[0], "city": addr.get("city", ""),
            "postcode": addr.get("postalCode", ""), "country": addr.get("country", ""),
        })

        for r in resources:
            rt = r["resourceType"]
            if rt == "Encounter":
                start = _naive((r.get("period") or {}).get("start"))
                if not start or start[:10] < since:
                    continue
                seq["visit"] += 1
                klass = _CLASS.get((r.get("class") or {}).get("code", "AMB"), "ambulatory")
                who = ((r.get("participant") or [{}])[0].get("individual") or {}).get("display", "Unknown Clinician")
                parts = _clean(who.replace("Dr.", "")).split()
                out["encounters"].append({
                    "visit_no": f"V{seq['visit']:07d}", "mrn": mrn, "class": klass, "start": start,
                    "end": _naive((r.get("period") or {}).get("end")),
                    "ward": {"ambulatory": "OPD1", "emergency": "ED", "inpatient": "MED1"}[klass],
                    "attending": {"id": "D" + hashlib.sha1(who.encode()).hexdigest()[:4].upper(),
                                  "last": parts[-1] if parts else "Clinician", "first": parts[0] if parts else "Unknown"},
                    "reason": (((r.get("type") or [{}])[0].get("coding") or [{}])[0]).get("display", "Encounter"),
                })
            elif rt == "Observation":
                cats = [c.get("code") for cat in r.get("category", []) for c in cat.get("coding", [])]
                vq = r.get("valueQuantity")
                when = _naive(r.get("effectiveDateTime"))
                coding = ((r.get("code") or {}).get("coding") or [{}])[0]
                if "laboratory" not in cats or not vq or not when or when[:10] < since or not coding.get("code"):
                    continue
                seq["result"] += 1
                loinc = coding["code"]
                known = _LOCAL_BY_LOINC.get(loinc)
                local = known[0] if known else local_codes.setdefault(loinc, f"T{len(local_codes) + 1:04d}")
                lo, hi = (known[4], known[5]) if known else (None, None)
                value = round(float(vq["value"]), 2)
                flag = "N" if lo is None else ("H" if value > hi else ("L" if value < lo else "N"))
                out["labs"].append({
                    "result_id": seq["result"], "mrn": mrn, "local_code": local, "loinc": loinc,
                    "name": coding.get("display", loinc), "value": value, "unit": vq.get("unit", ""),
                    "ref_low": lo, "ref_high": hi, "flag": flag, "status": r.get("status", "final"),
                    "collected": when, "resulted": _naive(r.get("issued")) or when,
                })
            elif rt == "Condition":
                coding = ((r.get("code") or {}).get("coding") or [{}])[0]
                if not coding.get("code"):
                    continue
                seq["cond"] += 1
                status = (((r.get("clinicalStatus") or {}).get("coding") or [{}])[0]).get("code", "active")
                out["conditions"].append({
                    "condition_id": f"C{seq['cond']:06d}", "mrn": mrn, "snomed": coding["code"],
                    "display": coding.get("display", ""), "onset": (r.get("onsetDateTime") or "")[:10] or None,
                    "status": "resolved" if status == "resolved" else "active",
                })
            elif rt == "MedicationRequest":
                coding = ((r.get("medicationCodeableConcept") or {}).get("coding") or [{}])[0]
                authored = (r.get("authoredOn") or "")[:10]
                if not coding.get("code") or not authored or authored < since:
                    continue
                seq["rx"] += 1
                status = r.get("status", "active")
                out["medications"].append({
                    "rx_no": f"RX{seq['rx']:07d}", "mrn": mrn, "rxnorm": coding["code"],
                    "drug": coding.get("display", ""), "qty": 30, "start": authored,
                    "sig": ((r.get("dosageInstruction") or [{}])[0]).get("text") or "Take as directed",
                    "status": status if status in ("active", "stopped", "completed") else "active",
                })
    if not out["patients"]:
        raise SystemExit(f"No Synthea patient bundles found in {folder}")
    return out
