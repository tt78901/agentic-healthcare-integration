"""Terminology check: are the clinical codes a connector emits real, and do their names match?

The validation contract proves an adapter's output is well-formed FHIR with the right record
counts. It cannot tell that a drug code is real or that the name printed next to it belongs
to it. In the measured run the Design agent invented RxNorm drug names (6 of 10 wrong); every
automatic check passed and only the human reviewer caught it. This check closes that gap.

Every coding from a clinical code system is validated against a FHIR terminology server with
the standard CodeSystem/$validate-code operation:

  LOINC      lab tests        (Observation.code)
  RxNorm     medicines        (MedicationRequest.medicationCodeableConcept)
  SNOMED CT  diagnoses        (Condition.code)

Each unique (system, code, display) gets one of four outcomes:

  valid             the code exists and the display matches it
  display-mismatch  the code exists but the name next to it is wrong   (the drug-name error)
  unknown-code      the code does not exist in that code system
  unverified        the server could not be reached; nothing is claimed either way

Results are cached in generated/terminology-cache.json, so a check that ran once online also
works offline (for example at a venue without Wi-Fi). The server defaults to the public HL7
terminology server; set FABRIC_TX_SERVER to use another (in production: the organisation's own
terminology service, e.g. Azure Health Data Services).

This check is advisory: it reports, it never blocks promotion, and the 14-check contract is
unchanged. Making it a blocking check is a deliberate next step once a production terminology
server is in place.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

from .config import GENERATED
from .validate import run_adapter

CLINICAL = {
    "http://loinc.org": "LOINC",
    "http://www.nlm.nih.gov/research/umls/rxnorm": "RxNorm",
    "http://snomed.info/sct": "SNOMED CT",
}
DEFAULT_SERVER = "https://tx.fhir.org/r4"


def collect(resources: dict[str, list[dict]]) -> dict[tuple[str, str, str], int]:
    """Every (system, code, display) from a clinical code system, with how often it appears."""
    found: dict[tuple[str, str, str], int] = {}

    def walk(node) -> None:
        if isinstance(node, dict):
            system, code = node.get("system"), node.get("code")
            if system in CLINICAL and isinstance(code, str) and code:
                key = (system, code, str(node.get("display") or ""))
                found[key] = found.get(key, 0) + 1
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for items in resources.values():
        walk(items)
    return found


def _param(params: dict, name: str):
    for p in params.get("parameter", []):
        if p.get("name") == name:
            for k, v in p.items():
                if k.startswith("value"):
                    return v
    return None


class Terminology:
    """Validates codes against a FHIR terminology server, with an on-disk cache."""

    def __init__(self, server: str | None = None, cache_path: Path | None = None,
                 client: httpx.Client | None = None, offline: bool = False):
        self.server = (server or os.environ.get("FABRIC_TX_SERVER") or DEFAULT_SERVER).rstrip("/")
        self.cache_path = cache_path or (GENERATED / "terminology-cache.json")
        self.offline = offline
        self.client = client or httpx.Client(timeout=20, headers={"Accept": "application/fhir+json"})
        self.cache: dict[str, dict] = {}
        if self.cache_path.exists():
            try:
                self.cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self.cache = {}
        self.calls = 0

    def save(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(self.cache, indent=1, sort_keys=True), encoding="utf-8")

    def validate(self, system: str, code: str, display: str = "") -> dict:
        key = f"{system}|{code}|{display}"
        if key in self.cache:
            return self.cache[key]
        if self.offline:
            return {"status": "unverified", "message": "offline and not in the cache"}
        params = {"url": system, "code": code}
        if display:
            params["display"] = display
        try:
            self.calls += 1
            resp = self.client.get(f"{self.server}/CodeSystem/$validate-code", params=params)
            body = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            return {"status": "unverified", "message": f"terminology server unreachable ({type(exc).__name__})"}
        if body.get("resourceType") == "OperationOutcome" or resp.status_code >= 500:
            text = "; ".join(i.get("diagnostics") or (i.get("details") or {}).get("text", "") for i in body.get("issue", []))
            return {"status": "unverified", "message": f"server error: {text or resp.status_code}"}
        result = _param(body, "result")
        message = str(_param(body, "message") or "")
        expected = str(_param(body, "display") or "")
        if result is True:
            mismatch = bool(display and expected and display.strip().lower() != expected.strip().lower()
                            and "display" in message.lower())
            outcome = {"status": "display-mismatch" if mismatch else "valid", "message": message,
                       "expected_display": expected}
        elif result is False:
            status = "display-mismatch" if "display" in message.lower() else "unknown-code"
            outcome = {"status": status, "message": message, "expected_display": expected}
        else:
            return {"status": "unverified", "message": "unexpected response from the terminology server"}
        self.cache[key] = outcome
        return outcome


def check_system(system: dict, generated: Path, term: Terminology) -> dict:
    """Run one promoted adapter (isolated, as the contract does) and validate its clinical codes."""
    out, _, failure = run_adapter(generated / system["id"] / "adapter.py", system)
    if out is None:
        return {"error": failure or "adapter did not run", "codes": 0, "valid": 0, "issues": [], "unverified": 0}
    codes = collect(out.get("resources") or {})
    summary = {"codes": len(codes), "occurrences": sum(codes.values()), "valid": 0, "unverified": 0,
               "by_system": {}, "issues": []}
    for (cs, code, display), count in sorted(codes.items()):
        res = term.validate(cs, code, display)
        name = CLINICAL[cs]
        summary["by_system"].setdefault(name, {"codes": 0, "valid": 0})
        summary["by_system"][name]["codes"] += 1
        if res["status"] == "valid":
            summary["valid"] += 1
            summary["by_system"][name]["valid"] += 1
        elif res["status"] == "unverified":
            summary["unverified"] += 1
        else:
            summary["issues"].append({"code_system": name, "code": code, "display": display, "records": count,
                                      "status": res["status"], "expected_display": res.get("expected_display", ""),
                                      "message": res.get("message", "")[:300]})
    return summary


def check(cfg: dict, generated: Path = GENERATED, term: Terminology | None = None) -> dict:
    """Terminology check for every promoted integration. Writes generated/terminology.json."""
    term = term or Terminology()
    registry_path = generated / "registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8")) if registry_path.exists() else {}
    systems = {s["id"]: s for s in cfg["systems"]}
    report = {"server": term.server, "systems": {}}
    for sid in registry:
        if sid in systems:
            report["systems"][sid] = check_system(systems[sid], generated, term)
    term.save()
    (generated / "terminology.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    return report


def describe(report: dict) -> str:
    lines = [f"Terminology server: {report['server']}"]
    for sid, s in report["systems"].items():
        if s.get("error"):
            lines.append(f"ERROR  {sid}: {s['error']}")
            continue
        if not s["codes"]:
            lines.append(f"-      {sid}: no LOINC / RxNorm / SNOMED CT codes emitted")
            continue
        flag = "ISSUE " if s["issues"] else ("CHECK " if s["unverified"] else "OK    ")
        per = ", ".join(f"{k} {v['valid']}/{v['codes']}" for k, v in s["by_system"].items())
        extra = f", {s['unverified']} unverified (server unreachable)" if s["unverified"] else ""
        lines.append(f"{flag} {sid}: {s['valid']}/{s['codes']} distinct codes valid ({per}){extra}")
        for i in s["issues"]:
            want = f" -> expected '{i['expected_display']}'" if i["expected_display"] else ""
            lines.append(f"        {i['status']}: {i['code_system']} {i['code']} '{i['display']}'{want} ({i['records']} records)")
    return "\n".join(lines)
