"""Load a Synthea FHIR R4 export into the canonical dataset shape.

The demo estate is Synthea data. `synthea_estate.json` (next to this file) is the
canonical dataset loaded from a Synthea run (seed 20261113, Massachusetts, reference
date 2026-11-22), so the estate can be rebuilt without Java or the 200 MB export:

    python -m fabric seed                                  # the committed Synthea estate (default)
    python -m fabric seed --synthea path/to/output/fhir    # from a fresh Synthea export
    python -m fabric seed --synthea DIR --save-estate      # ...and make it the new default

Each Synthea patient is one ``*.json`` Bundle. Only the resource types the mock
systems need are read: Patient, Encounter, Observation (laboratory, numeric),
Condition and MedicationRequest.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from collections import Counter
from pathlib import Path


ESTATE = Path(__file__).with_name("synthea_estate.json")
_CLASS = {"AMB": "ambulatory", "EMER": "emergency", "IMP": "inpatient", "ACUTE": "inpatient",
          "NONAC": "inpatient", "OBSENC": "emergency", "UC": "emergency", "HH": "ambulatory", "VR": "ambulatory"}
# The lab's own codes and reference ranges for the tests it has always run:
# (local code, LOINC, name, UCUM unit, ref low, ref high). Other LOINC codes get local codes T0001...
LAB_TESTS = [
    ("HBA1C", "4548-4", "Hemoglobin A1c/Hemoglobin.total in Blood", "%", 4.0, 5.6),
    ("GLU", "2345-7", "Glucose [Mass/volume] in Serum or Plasma", "mg/dL", 70, 99),
    ("CREA", "2160-0", "Creatinine [Mass/volume] in Serum or Plasma", "mg/dL", 0.6, 1.3),
    ("HGB", "718-7", "Hemoglobin [Mass/volume] in Blood", "g/dL", 12.0, 17.5),
    ("CHOL", "2093-3", "Cholesterol [Mass/volume] in Serum or Plasma", "mg/dL", 0, 200),
    ("HDL", "2085-9", "Cholesterol in HDL [Mass/volume] in Serum or Plasma", "mg/dL", 40, 100),
    ("TRIG", "2571-8", "Triglyceride [Mass/volume] in Serum or Plasma", "mg/dL", 0, 150),
    ("WBC", "6690-2", "Leukocytes [#/volume] in Blood by Automated count", "10*3/uL", 4.0, 11.0),
    ("PLT", "777-3", "Platelets [#/volume] in Blood by Automated count", "10*3/uL", 150, 400),
    ("NA", "2951-2", "Sodium [Moles/volume] in Serum or Plasma", "mmol/L", 136, 145),
    ("K", "2823-3", "Potassium [Moles/volume] in Serum or Plasma", "mmol/L", 3.5, 5.1),
    ("ALT", "1742-6", "Alanine aminotransferase [Enzymatic activity/volume] in Serum or Plasma", "U/L", 7, 56),
    ("TSH", "3016-3", "Thyrotropin [Units/volume] in Serum or Plasma", "m[IU]/L", 0.4, 4.0),
]
_LOCAL_BY_LOINC = {t[1]: t for t in LAB_TESTS}


def _naive(ts: str | None) -> str | None:
    """Synthea timestamps carry an offset; the legacy systems store naive local time."""
    if not ts:
        return None
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).replace(tzinfo=None).isoformat(timespec="seconds")


def _clean(name: str | None) -> str:
    return re.sub(r"\d+", "", name or "").strip()  # Synthea appends digits to names


_STATES = {"Massachusetts": "MA"}


def _postcode(addr: dict) -> str:
    """US address as written on a letter: state + ZIP ("MA 01085"). Kept as text, which is
    how the hospital systems store postcodes; a bare all-digit ZIP would be a format change."""
    state = addr.get("state", "")
    abbr = _STATES.get(state, state[:2].upper())
    return f"{abbr} {addr.get('postalCode', '')}".strip()


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
            "postcode": _postcode(addr), "country": addr.get("country", ""),
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
                # Synthea also records social and lifestyle "findings" (employment, education...) as
                # Conditions; a hospital problem list holds disorders.
                if not coding.get("code") or not coding.get("display", "").endswith("(disorder)"):
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
    # The lab stores one unit per test (TEST_REF). Synthea sometimes spells the same unit two
    # ways for one LOINC code (e.g. eGFR), so each test keeps its most common spelling.
    units: dict[str, Counter] = {}
    for lab in out["labs"]:
        units.setdefault(lab["local_code"], Counter())[lab["unit"]] += 1
    for lab in out["labs"]:
        lab["unit"] = units[lab["local_code"]].most_common(1)[0][0]
    out["patients"].sort(key=lambda p: -_richness(p["mrn"], out))   # the most complete record first: the demo patient
    return out


def _richness(mrn: str, ds: dict) -> int:
    """How much of a joined-up record a patient has across all four systems."""
    n = {k: sum(1 for x in ds[k] if x["mrn"] == mrn) for k in ("encounters", "labs", "conditions", "medications")}
    if not all(n.values()):
        return 0
    return min(n["labs"], 40) + 3 * n["conditions"] + 3 * n["medications"] + 2 * n["encounters"]


def load_estate() -> dict:
    """The committed Synthea estate (canonical dataset shape)."""
    if not ESTATE.exists():
        raise SystemExit(f"{ESTATE.name} is missing. Seed from an export: python -m fabric seed --synthea DIR")
    return json.loads(ESTATE.read_text(encoding="utf-8"))


def save_estate(dataset: dict) -> Path:
    ESTATE.write_text(json.dumps(dataset, indent=0) + "\n", encoding="utf-8")
    return ESTATE
