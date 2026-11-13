"""Evaluation harness: is the adapter's output actually right?

The validation contract can only check what is checkable in production without
knowing the answers: safety, FHIR validity, conventions and record counts. A
wrong mapping that still yields valid FHIR (day and month swapped, a status code
mapped to the wrong value) passes all of that.

Because this estate is synthetic we do know the answers. This module compares
every value an adapter emits with the ground truth written by the mock builder
and reports, per field, how many values are correct, wrong or absent.

Two rules keep the measurement honest:
  * it is independent: no agent, prompt or repair round ever sees the ground
    truth or this module's output;
  * it never blocks or repairs anything by itself: it is evidence, shown to the
    human reviewer at the promotion gate and printed in the report.

In production the equivalent is a small set of golden records agreed with the
system owner.
"""
from __future__ import annotations

import json
import re

from .config import ROOT

TRUTH_PATH = ROOT / "mock_systems" / "_data" / "ground_truth.json"


def load_truth(system: dict) -> dict | None:
    key = system.get("truth")
    if not key or not TRUTH_PATH.exists():
        return None
    return json.loads(TRUTH_PATH.read_text(encoding="utf-8")).get(key)


def _get(obj, *path):
    for step in path:
        if isinstance(step, int):
            obj = obj[step] if isinstance(obj, list) and len(obj) > step else None
        else:
            obj = obj.get(step) if isinstance(obj, dict) else None
        if obj is None:
            return None
    return obj


def _phone(res: dict):
    for t in res.get("telecom") or []:
        if isinstance(t, dict) and t.get("system", "phone") == "phone" and t.get("value"):
            return t["value"]
    return None


def _subject_mrn(res: dict):
    return _get(res, "subject", "identifier", "value")


def _patient_mrn(mrn_system: str):
    def inner(res: dict):
        for ident in res.get("identifier") or []:
            if isinstance(ident, dict) and ident.get("system") == mrn_system:
                return ident.get("value")
        return None
    return inner


def extractors(mrn_system: str) -> dict:
    """How to read each compared value out of a FHIR R4 resource."""
    code = lambda element: lambda r: _get(r, element, "coding", 0, "code")  # noqa: E731
    return {
        "Patient": {"mrn": _patient_mrn(mrn_system), "family": lambda r: _get(r, "name", 0, "family"),
                    "given": lambda r: _get(r, "name", 0, "given", 0), "birthDate": lambda r: r.get("birthDate"),
                    "gender": lambda r: r.get("gender"), "phone": _phone,
                    "city": lambda r: _get(r, "address", 0, "city"),
                    "postalCode": lambda r: _get(r, "address", 0, "postalCode")},
        "Encounter": {"mrn": _subject_mrn, "class": lambda r: _get(r, "class", "code"),
                      "status": lambda r: r.get("status"), "start": lambda r: _get(r, "period", "start"),
                      "end": lambda r: _get(r, "period", "end")},
        "Observation": {"mrn": _subject_mrn, "code": code("code"), "value": lambda r: _get(r, "valueQuantity", "value"),
                        "unit": lambda r: _get(r, "valueQuantity", "unit") or _get(r, "valueQuantity", "code"),
                        "effective": lambda r: r.get("effectiveDateTime"), "status": lambda r: r.get("status"),
                        "interpretation": lambda r: _get(r, "interpretation", 0, "coding", 0, "code")},
        "Condition": {"mrn": _subject_mrn, "code": code("code"), "clinicalStatus": code("clinicalStatus"),
                      "onset": lambda r: r.get("onsetDateTime")},
        "MedicationRequest": {"mrn": _subject_mrn, "code": code("medicationCodeableConcept"),
                              "status": lambda r: r.get("status"), "authoredOn": lambda r: r.get("authoredOn"),
                              "quantity": lambda r: _get(r, "dispenseRequest", "quantity", "value"),
                              "dosage": lambda r: _get(r, "dosageInstruction", 0, "text")},
    }


DATES = {"birthDate", "onset", "authoredOn"}
DATETIMES = {"start", "end", "effective"}
NUMBERS = {"value", "quantity"}
TEXT = {"family", "given", "city", "dosage", "unit"}
COMPACT = {"phone", "postalCode"}
NATURAL_KEY = {"Encounter": ("mrn", "start"), "Observation": ("mrn", "code", "effective"),
               "Condition": ("mrn", "code"), "MedicationRequest": ("mrn", "code", "authoredOn")}


def _norm(field: str, value):
    """Normalise away differences that are not errors: letter case of names, a UTC marker, 30 vs 30.0."""
    if value is None or value == "":
        return None
    if field in DATES:
        return str(value)[:10]
    if field in DATETIMES:
        return re.sub(r"(Z|\+00:00)$", "", str(value))[:19]
    if field in NUMBERS:
        try:
            return round(float(value), 6)
        except (TypeError, ValueError):
            return str(value)
    if field in TEXT:
        return " ".join(str(value).casefold().split())
    if field in COMPACT:
        return re.sub(r"\s+", "", str(value))
    return str(value)


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9\-.]", "-", str(value))


def evaluate(system_id: str, resources: dict[str, list[dict]], truth: dict, mrn_system: str) -> dict:
    """Compare emitted resources with ground truth. Returns counts only: no values leave this function."""
    getters = extractors(mrn_system)
    fields: dict[str, dict] = {}
    records = {"expected": 0, "matched": 0, "missing": 0, "unexpected": 0}

    for rt, expected in truth.items():
        emitted = resources.get(rt, [])
        records["expected"] += len(expected)
        by_id = {_safe_id(k): k for k in expected}
        by_natural = {}
        if rt in NATURAL_KEY:
            for key, rec in expected.items():
                by_natural.setdefault(tuple(_norm(f, rec.get(f)) for f in NATURAL_KEY[rt]), key)
        matched: set[str] = set()

        for res in emitted:
            got = {f: fn(res) for f, fn in getters[rt].items()}
            if rt == "Patient":
                key = got["mrn"] if got["mrn"] in expected else None
            else:
                rid = str(res.get("id", ""))
                suffix = rid[len(system_id) + 1:] if rid.startswith(f"{system_id}-") else rid
                key = by_id.get(suffix) or by_natural.get(tuple(_norm(f, got.get(f)) for f in NATURAL_KEY[rt]))
            if key is None or key in matched:
                records["unexpected"] += 1
                continue
            matched.add(key)
            for field, want in expected[key].items():
                tally = fields.setdefault(f"{rt}.{field}", {"correct": 0, "wrong": 0, "absent": 0})
                have, want = _norm(field, got.get(field)), _norm(field, want)
                if want is None:
                    tally["correct" if have is None else "wrong"] += 1
                elif have is None:
                    tally["absent"] += 1
                else:
                    tally["correct" if have == want else "wrong"] += 1
        records["matched"] += len(matched)
        records["missing"] += len(expected) - len(matched)

    correct = sum(f["correct"] for f in fields.values())
    wrong = sum(f["wrong"] for f in fields.values())
    absent = sum(f["absent"] for f in fields.values())
    return {
        "records": records, "fields": fields,
        "values_correct": correct, "values_wrong": wrong, "values_absent": absent,
        "accuracy": round(correct / (correct + wrong), 4) if correct + wrong else None,
        "coverage": round((correct + wrong) / (correct + wrong + absent), 4) if correct + wrong + absent else None,
        "clean": records["missing"] == 0 and records["unexpected"] == 0 and wrong == 0 and absent == 0,
    }


def summary_lines(ev: dict) -> list[str]:
    """Human-readable evidence for the promotion gate and the console."""
    r = ev["records"]
    acc = f"{ev['accuracy'] * 100:.1f}%" if ev["accuracy"] is not None else "n/a"
    cov = f"{ev['coverage'] * 100:.1f}%" if ev["coverage"] is not None else "n/a"
    lines = [f"records matched {r['matched']}/{r['expected']}"
             + (f", {r['unexpected']} unexpected" if r["unexpected"] else ""),
             f"values correct {ev['values_correct']:,} of {ev['values_correct'] + ev['values_wrong']:,} mapped "
             f"({acc}); coverage of source values {cov}"]
    for name, t in ev["fields"].items():
        if t["wrong"] or t["absent"]:
            parts = ([f"{t['wrong']} wrong"] if t["wrong"] else []) + ([f"{t['absent']} not mapped"] if t["absent"] else [])
            lines.append(f"{name}: {', '.join(parts)} (of {t['correct'] + t['wrong'] + t['absent']})")
    return lines
