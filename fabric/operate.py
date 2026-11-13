"""Operate stage: notice when a promoted integration stops matching its source.

Source systems change underneath their integrations: a vendor upgrade renames a
column, an API version renames a field. Two kinds of damage follow:

  loud    the adapter crashes or fails the contract (caught by re-running the contract)
  silent  the adapter keeps running and emits valid FHIR with a value missing
          (only caught by comparing the source's structure with what was approved)

`python -m fabric check` does both for every promoted system. It re-probes the
source, diffs its structural fingerprint against the one stored at promotion,
and re-runs the validation contract on the promoted adapter. With --heal the
drifted systems go back through the agent pipeline, gates included.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from .audit import now
from .config import GENERATED
from .evaluate import evaluate, load_truth
from .privacy import Masker
from .probes import probe
from .validate import validate


def _kind(label: str) -> str:
    """Coarse field type. Coarse on purpose: text getting longer is not drift, a date format changing is."""
    if label.startswith("date"):
        return label.replace(" stored as integer", "")
    if label.startswith(("integer", "decimal")):
        return "number as text" if "text" in label else "number"
    if label.startswith("text"):
        return "text"
    if label.startswith("boolean"):
        return "boolean"
    return "empty"


def skeleton(probe_out: dict) -> dict[str, str]:
    """Structural fingerprint of a system: every field the probe saw and its coarse type. No values."""
    out: dict[str, str] = {}
    kind = probe_out["kind"]
    if kind == "sqlite":
        for t in probe_out["tables"]:
            for col in t["columns"]:
                out[f"{t['table']}.{col['name']}"] = "empty"
            for col, st in t["field_statistics"].items():
                out[f"{t['table']}.{col}"] = _kind(st["format"])
    elif kind == "http":
        for e in probe_out["list_endpoints"]:
            for field, st in e["field_statistics"].items():
                out[f"{e['path']} {field}"] = _kind(st["format"])
    elif kind == "hl7v2_files":
        for message_type in probe_out["message_type_counts"]:
            out[f"message type {message_type}"] = "present"
        for path, st in probe_out["field_statistics_by_hl7_path"].items():
            out[path] = _kind(st["format"])
    elif kind == "delimited_files":
        out["(delimiter)"] = probe_out["delimiter"]
        for col, st in probe_out["field_statistics"].items():
            out[col] = _kind(st["format"])
    return out


def diff(approved: dict[str, str], current: dict[str, str]) -> dict:
    """What changed in the source's structure since the integration was approved."""
    changed = [{"field": k, "was": approved[k], "now": current[k]} for k in approved
               if k in current and approved[k] != current[k] and "empty" not in (approved[k], current[k])]
    return {"added": sorted(k for k in current if k not in approved),
            "removed": sorted(k for k in approved if k not in current), "changed": changed}


def check(cfg: dict, generated: Path = GENERATED) -> dict[str, dict]:
    """Health of every promoted integration. Writes generated/status.json and returns it."""
    registry_path = generated / "registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8")) if registry_path.exists() else {}
    systems = {s["id"]: s for s in cfg["systems"]}
    mrn_system = cfg["settings"]["mrn_system"]
    statuses: dict[str, dict] = {}

    for sid in registry:
        system = systems.get(sid)
        if not system:
            continue
        status = {"checked_at": now(), "drift": None, "contract": "error", "failed": [], "evaluation": None,
                  "healthy": False}
        folder = generated / sid
        try:
            current = skeleton(probe(system, Masker("check")))  # samples are discarded; only structure is kept
            approved = json.loads((folder / "fingerprint.json").read_text(encoding="utf-8"))
            delta = diff(approved, current)
            status["drift"] = delta if any(delta.values()) else None
            design = json.loads((folder / "design.json").read_text(encoding="utf-8"))
            code = (folder / "adapter.py").read_text(encoding="utf-8")
            with tempfile.TemporaryDirectory() as tmp:
                result = validate(code, system, design, mrn_system, Path(tmp) / "work")
            status["contract"] = "pass" if result["passed"] else "fail"
            status["failed"] = [{"id": c["id"], "title": c["title"], "detail": c["detail"]}
                                for c in result["checks"] if c["status"] == "fail"]
            truth = load_truth(system)
            if truth and result["resources"]:
                status["evaluation"] = evaluate(sid, result["resources"], truth, mrn_system)
        except Exception as exc:  # noqa: BLE001  (an unreachable source is itself a finding)
            status["error"] = f"{type(exc).__name__}: source or artefacts unavailable"
        status["healthy"] = status["contract"] == "pass" and status["drift"] is None
        statuses[sid] = status

    generated.mkdir(parents=True, exist_ok=True)
    (generated / "status.json").write_text(json.dumps(statuses, indent=1), encoding="utf-8")
    return statuses


def describe(statuses: dict[str, dict]) -> str:
    lines = []
    for sid, st in statuses.items():
        flag = "OK   " if st["healthy"] else "ALERT"
        lines.append(f"{flag} {sid}: contract {st['contract']}, structure {'unchanged' if not st['drift'] else 'DRIFTED'}")
        if st.get("error"):
            lines.append(f"        {st['error']}")
        d = st["drift"] or {}
        for name in d.get("removed", []):
            lines.append(f"        - removed from source: {name}")
        for name in d.get("added", []):
            lines.append(f"        + new in source:       {name}")
        for c in d.get("changed", []):
            lines.append(f"        ~ changed:             {c['field']} ({c['was']} -> {c['now']})")
        for f in st["failed"]:
            lines.append(f"        FAIL {f['id']}: {f['detail'][:160]}")
        ev = st.get("evaluation")
        if ev and not ev["clean"]:
            lost = [f"{k} ({t['wrong']} wrong, {t['absent']} missing)" for k, t in ev["fields"].items()
                    if t["wrong"] or t["absent"]]
            lines.append("        data check: " + ("; ".join(lost) if lost else "records do not match the source"))
    return "\n".join(lines) if lines else "No promoted systems yet. Run the pipeline first."
