"""Demo data uploads: new data arriving in a source system, live.

This simulates the hospital systems themselves receiving new data (a new admission
message, a new lab result, tonight's pharmacy export, a new patient in the EHR), so a
demo can show the fabric picking it up with no code change and no re-integration.

It writes to the mock source systems, never through a connector: the connectors stay
read-only. It is on for the demo console and can be switched off with
`python -m fabric gateway --no-demo-uploads` (environment variable FABRIC_DEMO_UPLOADS=0).
In production there is no such endpoint; data arrives through the systems' own feeds.

Accepted per system (every upload is validated before anything is written):

  ehr-rest      JSON: one object or a list of patients and/or problem-list conditions,
                in the EHR's own format (patientId/mrn/name/dob... or conditionId/code...)
  lab-sql       CSV (comma or pipe): MRN, TEST_CD, RSLT_VAL, ABN_FLG, RSLT_STAT,
                COLL_DTTM, RSLT_DTTM; inserted into LAB_RSLT for an existing patient
  adt-feed      HL7 v2 ADT message(s), saved as a new message file in the feed folder
  pharmacy-csv  pipe-delimited nightly export with the standard header, saved as a new
                export file (later files win for a re-exported RX_NO)

Validated uploads (`ingest_validated`): after the data lands, the fabric re-checks that
system exactly as `python -m fabric check` does: the source's structure is compared with
the fingerprint approved at promotion, and the promoted connector is re-run through the
full validation contract (safety, runtime, FHIR R4 schema and conventions, and a
reconciliation of source rows against FHIR resources). Only if every check passes is the
upload kept; otherwise it is rolled back and the failed checks are reported. These checks
are deterministic code: no AI model is called.

Quarantine (`ingest_with_quarantine`): if a multi-record file fails, the fabric does not
throw the good records away. It splits the file into its records (EHR items, lab rows, HL7
messages, pharmacy rows) and validates each one on its own with the same drift check and
14-check contract. Records that pass are kept; records that fail are quarantined (never
written) and reported with their line and reason. A file that is not in the system's format
at all (wrong header, not JSON, not HL7) is still refused whole.

`template(system_id)` returns a ready-to-upload example for each system, and `reset()`
rebuilds all four systems from the dataset snapshot, removing every upload.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable

from .config import ROOT

DATA_DIR = ROOT / "mock_systems" / "_data"
MAX_BYTES = 2_000_000
RX_HEADER = ["RX_NO", "MRN", "DRUG_DESC", "RXNORM_CD", "SIG_TXT", "QTY", "START_DT", "STAT"]
LAB_COLUMNS = ["MRN", "TEST_CD", "RSLT_VAL", "ABN_FLG", "RSLT_STAT", "COLL_DTTM", "RSLT_DTTM"]


class UploadError(ValueError):
    """The upload was rejected; nothing was written."""


class ValidationFailed(UploadError):
    """The data was written, failed the post-upload checks, and was rolled back."""

    def __init__(self, message: str, validation: dict):
        super().__init__(message)
        self.validation = validation


_lock = threading.Lock()   # one upload (write, check, keep or roll back) at a time


def enabled() -> bool:
    return os.environ.get("FABRIC_DEMO_UPLOADS", "1").strip().lower() not in ("0", "false", "no", "off")


def _stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d%H%M%S")


def _new_file(folder: Path, name: str) -> Path:
    """A path for a new upload that never overwrites an existing file and always sorts after
    every file already in the folder, so the feed processes uploads in arrival order. `name`
    contains {ts}; the timestamp is now, or just after the newest existing file if that is later."""
    prefix = name.split("{ts}")[0]
    newest = max((p.name for p in folder.glob(prefix + "*")), default="")
    when = dt.datetime.now().replace(microsecond=0)
    digits = re.match(r"\d+", newest[len(prefix):]) if newest else None
    if digits and len(digits.group()) in (8, 14):
        try:
            last = dt.datetime.strptime(digits.group(), "%Y%m%d%H%M%S" if len(digits.group()) == 14 else "%Y%m%d")
            when = max(when, last + dt.timedelta(seconds=1))
        except ValueError:
            pass
    for _ in range(1000):
        path = folder / name.format(ts=when.strftime("%Y%m%d%H%M%S"))
        if path.name > newest and not path.exists():
            return path
        when += dt.timedelta(seconds=1)
    raise UploadError("Could not choose a file name for the upload.")


def _text(content: bytes) -> str:
    if not content or not content.strip():
        raise UploadError("The file is empty.")
    if len(content) > MAX_BYTES:
        raise UploadError(f"The file is larger than {MAX_BYTES // 1_000_000} MB.")
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise UploadError("The file is not UTF-8 text.") from exc


def _system_path(system: dict) -> Path:
    path = Path(system["path"])
    return path if path.is_absolute() else ROOT / path


# ------------------------------------------------------------------ per-system ingest

def _ingest_ehr(text: str) -> dict:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise UploadError(f"Not valid JSON: {exc.msg} (line {exc.lineno}).") from exc
    items = payload if isinstance(payload, list) else [payload]
    if not items or not all(isinstance(i, dict) for i in items):
        raise UploadError("Expected a JSON object or a list of objects.")
    pat_path, con_path = DATA_DIR / "ehr" / "patients.json", DATA_DIR / "ehr" / "conditions.json"
    patients = json.loads(pat_path.read_text(encoding="utf-8"))
    conditions = json.loads(con_path.read_text(encoding="utf-8"))
    by_mrn = {p["mrn"]: p for p in patients}
    added_p = added_c = 0
    for n, item in enumerate(items, 1):
        if "conditionId" in item:
            for key in ("conditionId", "mrn", "code", "onsetDate", "state"):
                if key not in item:
                    raise UploadError(f"Item {n}: condition is missing '{key}'.")
            if item["mrn"] not in by_mrn:
                raise UploadError(f"Item {n}: no EHR patient with MRN {item['mrn']}.")
            code = item["code"]
            if not isinstance(code, dict) or not code.get("value"):
                raise UploadError(f"Item {n}: 'code' needs at least a 'value' (SNOMED code).")
            item.setdefault("patientId", by_mrn[item["mrn"]]["patientId"])
            conditions = [c for c in conditions if c["conditionId"] != item["conditionId"]] + [item]
            added_c += 1
        elif "mrn" in item and "name" in item:
            for key in ("mrn", "name", "dob", "sex"):
                if key not in item:
                    raise UploadError(f"Item {n}: patient is missing '{key}'.")
            if not re.fullmatch(r"\d{2}/\d{2}/\d{4}", str(item["dob"])):
                raise UploadError(f"Item {n}: 'dob' must be MM/DD/YYYY, as this EHR stores it.")
            existing = by_mrn.get(item["mrn"])
            item.setdefault("patientId", existing["patientId"] if existing else f"P-U{_stamp()}{n}")
            item.setdefault("active", True)
            patients = [p for p in patients if p["mrn"] != item["mrn"]] + [item]
            by_mrn[item["mrn"]] = item
            added_p += 1
        else:
            raise UploadError(f"Item {n}: not recognised as an EHR patient (mrn, name, dob, sex) "
                              "or condition (conditionId, mrn, code, onsetDate, state).")
    for path, rows in ((pat_path, patients), (con_path, conditions)):
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rows, indent=1), encoding="utf-8")
        tmp.replace(path)
    return {"added": added_p + added_c, "detail": f"{added_p} patient(s), {added_c} condition(s) added to the EHR"}


def _sniff_rows(text: str) -> list[dict]:
    first = text.splitlines()[0]
    delim = "|" if first.count("|") > first.count(",") else ","
    return list(csv.DictReader(io.StringIO(text), delimiter=delim))


def _ingest_lab(text: str, system: dict) -> dict:
    rows = _sniff_rows(text)
    if not rows:
        raise UploadError("No rows found under the header.")
    missing = [c for c in LAB_COLUMNS if c not in rows[0]]
    if missing:
        raise UploadError(f"Missing column(s): {', '.join(missing)}. Expected: {', '.join(LAB_COLUMNS)}.")
    db = sqlite3.connect(_system_path(system))
    try:
        pt_ids = dict(db.execute("SELECT MRN_NO, PT_ID FROM PT_MSTR"))
        tests = {r[0] for r in db.execute("SELECT TEST_CD FROM TEST_REF")}
        next_id = (db.execute("SELECT COALESCE(MAX(RSLT_ID), 0) FROM LAB_RSLT").fetchone()[0] or 0) + 1
        new = []
        for n, r in enumerate(rows, 2):
            mrn, test = (r["MRN"] or "").strip(), (r["TEST_CD"] or "").strip()
            if mrn not in pt_ids:
                raise UploadError(f"Line {n}: no lab patient with MRN {mrn}.")
            if test not in tests:
                raise UploadError(f"Line {n}: unknown test code {test}. Known: {', '.join(sorted(tests))}.")
            for col in ("COLL_DTTM", "RSLT_DTTM"):
                if not re.fullmatch(r"\d{14}", (r[col] or "").strip()):
                    raise UploadError(f"Line {n}: {col} must be YYYYMMDDHHMMSS, as this lab stores it.")
            new.append((next_id + len(new), pt_ids[mrn], test, r["RSLT_VAL"].strip(), (r["ABN_FLG"] or "N").strip(),
                        (r["RSLT_STAT"] or "F").strip(), r["COLL_DTTM"].strip(), r["RSLT_DTTM"].strip()))
        db.executemany("INSERT INTO LAB_RSLT (RSLT_ID, PT_ID, TEST_CD, RSLT_VAL, ABN_FLG, RSLT_STAT, COLL_DTTM, "
                       "RSLT_DTTM) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", new)
        db.commit()
    finally:
        db.close()
    return {"added": len(new), "detail": f"{len(new)} lab result(s) inserted into LAB_RSLT"}


def _ingest_adt(text: str, system: dict) -> dict:
    msg = text.replace("\r\n", "\r").replace("\n", "\r").strip("\r")
    segments = [s for s in msg.split("\r") if s]
    if not segments or not segments[0].startswith("MSH|"):
        raise UploadError("Not an HL7 v2 message: the first segment must be MSH.")
    messages = sum(1 for s in segments if s.startswith("MSH|"))
    if not any(s.startswith("PID|") for s in segments):
        raise UploadError("The message has no PID (patient) segment.")
    if "ADT^" not in segments[0]:
        raise UploadError("Only ADT messages are accepted (MSH-9 must be ADT^A01, ADT^A03, ADT^A04...).")
    folder = _system_path(system)
    path = _new_file(folder, "adt_{ts}_upload.hl7")
    path.write_text("\r".join(segments) + "\r", encoding="utf-8")
    return {"added": messages, "detail": f"{messages} HL7 message(s) delivered to the feed as {path.name}"}


def _ingest_pharmacy(text: str, system: dict) -> dict:
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines or lines[0].strip().split("|") != RX_HEADER:
        raise UploadError("The header must be: " + "|".join(RX_HEADER))
    rows = list(csv.DictReader(io.StringIO("\n".join(lines)), delimiter="|"))
    if not rows:
        raise UploadError("No prescriptions found under the header.")
    for n, r in enumerate(rows, 2):
        if not (r["RX_NO"] or "").strip() or not (r["MRN"] or "").strip():
            raise UploadError(f"Line {n}: RX_NO and MRN are required.")
        try:
            dt.datetime.strptime((r["START_DT"] or "").strip(), "%d/%m/%Y")
        except ValueError:
            raise UploadError(f"Line {n}: START_DT must be a real date in DD/MM/YYYY, as this export uses.") from None
    path = _new_file(_system_path(system), "rx_export_{ts}.csv")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"added": len(rows), "detail": f"{len(rows)} prescription(s) delivered as tonight's export {path.name}"}


def ingest(system: dict, content: bytes) -> dict:
    """Validate and add uploaded data to one mock source system."""
    text = _text(content)
    kind = system["kind"]
    if kind == "http":
        result = _ingest_ehr(text)
    elif kind == "sqlite":
        result = _ingest_lab(text, system)
    elif kind == "hl7v2_files":
        result = _ingest_adt(text, system)
    elif kind == "delimited_files":
        result = _ingest_pharmacy(text, system)
    else:
        raise UploadError(f"Uploads are not supported for {kind} systems.")
    return {"system": system["id"], **result}


# ------------------------------------------------------------------ validate, then keep or roll back

def _undo_point(system: dict) -> Callable[[], None]:
    """Record the system's state now; the returned function puts it back exactly."""
    kind = system["kind"]
    if kind == "http":
        files = [DATA_DIR / "ehr" / "patients.json", DATA_DIR / "ehr" / "conditions.json"]
        saved = {f: f.read_bytes() for f in files}

        def undo() -> None:
            for f, data in saved.items():
                tmp = f.with_suffix(".tmp")
                tmp.write_bytes(data)
                tmp.replace(f)
        return undo
    if kind == "sqlite":
        db_path = _system_path(system)
        saved_db = db_path.read_bytes()   # small (tens of KB): restore it byte for byte

        def undo() -> None:
            tmp = db_path.with_suffix(".restore")
            tmp.write_bytes(saved_db)
            tmp.replace(db_path)
        return undo
    if kind in ("hl7v2_files", "delimited_files"):
        folder = _system_path(system)
        before = {p.name for p in folder.iterdir() if p.is_file()}

        def undo() -> None:
            for p in folder.iterdir():
                if p.is_file() and p.name not in before:
                    p.unlink()
        return undo
    raise UploadError(f"Uploads are not supported for {kind} systems.")


def _summary(status: dict, seconds: float) -> dict:
    drift = status.get("drift") or None
    return {"passed": bool(status.get("healthy")), "checks_passed": status.get("checks_passed", 0),
            "checks_total": status.get("checks_total", 0), "resources": status.get("resources", 0),
            "drift": drift, "failed": status.get("failed", []), "checks": status.get("checks", []),
            "error": status.get("error"), "seconds": round(seconds, 2)}


def _reason(v: dict) -> str:
    if v.get("error"):
        return "The connector could not be re-checked: " + v["error"] + "."
    parts = []
    if v["failed"]:
        parts.append("failed " + ", ".join(f["id"] for f in v["failed"]))
    if v["drift"]:
        d = v["drift"]
        bits = [f"{len(d[k])} field(s) {k}" for k in ("added", "removed", "changed") if d.get(k)]
        parts.append("the source structure no longer matches what was approved (" + ", ".join(bits) + ")")
    return "Rolled back: " + "; ".join(parts) + "."


def ingest_validated(system: dict, content: bytes, verify: Callable[[dict], dict]) -> dict:
    """Write the upload, re-check the system with `verify`, keep it only if every check passes.

    `verify(system)` returns a status from operate.check_system. If any check fails, the
    source drifts, or the check itself errors, the upload is rolled back and
    ValidationFailed is raised with the details.
    """
    with _lock:
        undo = _undo_point(system)
        result = ingest(system, content)          # format errors raise here, before anything is written
        started = time.monotonic()
        try:
            status = verify(system)
        except Exception as exc:  # noqa: BLE001  (a check that cannot run is a failed check)
            status = {"healthy": False, "error": f"{type(exc).__name__}"}
        summary = _summary(status, time.monotonic() - started)
        if not summary["passed"]:
            undo()
            raise ValidationFailed(_reason(summary), summary)
    return {**result, "validation": summary}


# ------------------------------------------------------------------ quarantine: keep the good records

MAX_RECORDS_TO_ISOLATE = 50


def split_records(system: dict, content: bytes) -> list[tuple[str, bytes]]:
    """Split an upload into single-record uploads in the same format, each with a label that
    says where it came from. Raises UploadError if the file itself is not in the format."""
    text = _text(content)
    kind = system["kind"]
    if kind == "http":
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise UploadError(f"Not valid JSON: {exc.msg} (line {exc.lineno}).") from exc
        items = payload if isinstance(payload, list) else [payload]
        out = []
        for n, item in enumerate(items, 1):
            if isinstance(item, dict) and "conditionId" in item:
                label = f"Item {n}: condition {item.get('conditionId')} (MRN {item.get('mrn')})"
            elif isinstance(item, dict):
                label = f"Item {n}: patient MRN {item.get('mrn')}"
            else:
                label = f"Item {n}"
            out.append((label, json.dumps([item]).encode()))
        return out
    if kind in ("sqlite", "delimited_files"):
        lines = [l for l in text.splitlines() if l.strip()]
        if len(lines) < 2:
            raise UploadError("No rows found under the header.")
        header, rows = lines[0], lines[1:]
        delim = "|" if header.count("|") > header.count(",") else ","
        names = header.split(delim)
        out = []
        for n, row in enumerate(rows, 2):
            cells = dict(zip(names, row.split(delim)))
            if kind == "sqlite":
                label = f"Line {n}: MRN {cells.get('MRN', '?')}, test {cells.get('TEST_CD', '?')}"
            else:
                label = f"Line {n}: prescription {cells.get('RX_NO', '?')}"
            out.append((label, (header + "\n" + row + "\n").encode()))
        return out
    if kind == "hl7v2_files":
        segments = [x for x in text.replace("\r\n", "\r").replace("\n", "\r").split("\r") if x]
        if not segments or not segments[0].startswith("MSH|"):
            raise UploadError("Not an HL7 v2 message: the first segment must be MSH.")
        messages: list[list[str]] = []
        for seg in segments:
            if seg.startswith("MSH|"):
                messages.append([])
            messages[-1].append(seg)
        out = []
        for n, msg in enumerate(messages, 1):
            msh = msg[0].split("|")
            label = f"Message {n}: {msh[8] if len(msh) > 8 else '?'} {msh[9] if len(msh) > 9 else ''}".strip()
            out.append((label, ("\r".join(msg) + "\r").encode()))
        return out
    raise UploadError(f"Uploads are not supported for {kind} systems.")


def _why(summary: dict) -> dict:
    """The reasons a record failed, in the shape the console shows."""
    return {"failed": summary.get("failed", []), "drift": summary.get("drift"), "error": summary.get("error"),
            "checks_passed": summary.get("checks_passed", 0), "checks_total": summary.get("checks_total", 0)}


def ingest_with_quarantine(system: dict, content: bytes, verify: Callable[[dict], dict]) -> dict:
    """Validate the whole upload; if it fails, keep the records that pass on their own and
    quarantine the rest. Returns the result with outcome "accepted" or "partial"; raises
    ValidationFailed (nothing kept) or UploadError (not this system's format)."""
    with _lock:
        # 1. The whole file, as one transaction (the common case: everything is fine)
        undo = _undo_point(system)
        whole_format_error = None
        try:
            result = ingest(system, content)
        except UploadError as exc:          # a row may be wrong; the file may still be in the format
            whole_format_error = exc
        else:
            started = time.monotonic()
            try:
                status = verify(system)
            except Exception as exc:  # noqa: BLE001
                status = {"healthy": False, "error": type(exc).__name__}
            summary = _summary(status, time.monotonic() - started)
            if summary["passed"]:
                return {**result, "outcome": "accepted", "validation": summary}
            undo()

        # 2. Split into records; a file that is not in the format at all is refused whole
        records = split_records(system, content)
        if len(records) == 1:
            if whole_format_error:
                raise whole_format_error
            raise ValidationFailed(_reason(summary), summary)
        if len(records) > MAX_RECORDS_TO_ISOLATE:
            if whole_format_error:
                raise whole_format_error
            raise ValidationFailed(_reason(summary) + f" (more than {MAX_RECORDS_TO_ISOLATE} records: fix the "
                                   "file and upload it again)", summary)

        # 3. Each record on its own: kept only if it passes everything
        started_all = time.monotonic()
        accepted, quarantined, added, last_ok, last_fail = [], [], 0, None, None
        for label, record in records:
            undo = _undo_point(system)
            try:
                res = ingest(system, record)
            except UploadError as exc:
                # the label already says which line/item; drop the single-record file's own numbering
                quarantined.append({"record": label, "format": re.sub(r"^(Line|Item) \d+: ", "", str(exc))})
                continue
            started = time.monotonic()
            try:
                status = verify(system)
            except Exception as exc:  # noqa: BLE001
                status = {"healthy": False, "error": type(exc).__name__}
            rs = _summary(status, time.monotonic() - started)
            if rs["passed"]:
                accepted.append(label)
                added += res["added"]
                last_ok = rs
            else:
                undo()
                last_fail = rs
                quarantined.append({"record": label, **_why(rs)})

        records_info = {"total": len(records), "accepted": accepted, "quarantined": quarantined,
                        "seconds": round(time.monotonic() - started_all, 2)}
        if not accepted:
            if all("format" in q for q in quarantined) and whole_format_error:
                exc = UploadError(str(whole_format_error))
                exc.records = records_info  # type: ignore[attr-defined]
                raise exc
            evidence = last_fail or (None if whole_format_error else summary) or {
                "passed": False, "checks_passed": 0, "checks_total": 14, "resources": 0, "drift": None,
                "failed": [], "checks": [], "error": None, "seconds": 0}
            failed = ValidationFailed(f"Rolled back: none of the {len(records)} records passed on its own.", evidence)
            failed.records = records_info  # type: ignore[attr-defined]
            raise failed
        return {"system": system["id"], "added": added, "outcome": "partial",
                "detail": f"{len(accepted)} of {len(records)} records accepted, {len(quarantined)} quarantined",
                "validation": last_ok, "records": records_info}


# ------------------------------------------------------------------ templates and reset

def demo_mrn() -> str:
    """The demo patient: the first patient in the EHR (the most complete record in the estate)."""
    path = DATA_DIR / "ehr" / "patients.json"
    patients = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    return patients[0]["mrn"] if patients else ""


def data_source() -> str:
    """Short label for where the estate's data came from (e.g. "Synthea · 40 patients")."""
    path = DATA_DIR / "SOURCE.txt"
    text = path.read_text(encoding="utf-8").strip() if path.exists() else ""
    if text.startswith("Synthea"):
        m = re.search(r"(\d+) patients", text)
        return "Synthea" + (f" · {m.group(1)} patients" if m else "")
    return text[:40]


def _lab_tests(system: dict) -> list[tuple]:
    """Tests the lab knows, those with a reference range first: (code, unit, low, high)."""
    db = sqlite3.connect(_system_path(system))
    try:
        rows = db.execute("SELECT TEST_CD, UOM, REF_LO, REF_HI FROM TEST_REF").fetchall()
    finally:
        db.close()
    return sorted(rows, key=lambda r: (r[2] is None, r[0]))


def template(system: dict, mrn: str | None = None) -> tuple[str, str]:
    """A valid example upload for this system (filename, content), for the given patient."""
    mrn = mrn or demo_mrn()
    now = dt.datetime.now()
    kind = system["kind"]
    if kind == "http":
        body = [{"conditionId": f"C-U{now:%H%M%S}", "mrn": mrn,
                 "code": {"system": "SNOMED", "value": "38341003", "text": "Hypertensive disorder"},
                 "onsetDate": f"{now:%Y-%m-%d}", "state": "A"}]
        return "ehr-new-condition.json", json.dumps(body, indent=2) + "\n"
    if kind == "sqlite":
        stamp = f"{now:%Y%m%d%H%M%S}"
        rows = ["MRN,TEST_CD,RSLT_VAL,ABN_FLG,RSLT_STAT,COLL_DTTM,RSLT_DTTM"]
        for i, (code, _unit, lo, hi) in enumerate(_lab_tests(system)[:2]):
            if lo is None or hi is None:
                value, flag = "12.0", "N"
            elif i == 0:
                value, flag = f"{round(hi * 1.2, 1)}", "H"        # one result above the reference range
            else:
                value, flag = f"{round((lo + hi) / 2, 1)}", "N"
            rows.append(f"{mrn},{code},{value},{flag},F,{stamp},{stamp}")
        return "lab-new-results.csv", "\n".join(rows) + "\n"
    if kind == "hl7v2_files":
        start, end = now - dt.timedelta(hours=2), now
        names = _patient_names(mrn)
        seg = [f"MSH|^~\\&|PAS|GENHOSP|INTEGRATION|GENHOSP|{end:%Y%m%d%H%M%S}||ADT^A04|MSGU{end:%H%M%S}|P|2.5",
               f"EVN|A04|{end:%Y%m%d%H%M%S}",
               f"PID|1||{mrn}^^^GENHOSP^MR||{names}",
               f"PV1|1|E|ED1^^^GENHOSP||||D1159^ROSSI^PAOLO^^^DR||||||||||||VU{end:%H%M%S}|||||||||||||||||||||||||"
               f"{start:%Y%m%d%H%M%S}|{end:%Y%m%d%H%M%S}",
               "PV2|||^Shortness of breath"]
        return "adt-new-emergency-visit.hl7", "\r".join(seg) + "\r"
    if kind == "delimited_files":
        rows = ["|".join(RX_HEADER),
                f"RXU{now:%H%M%S}|{mrn}|Amlodipine 5 MG Oral Tablet|197361|Take 1 tablet by mouth once daily|28|"
                f"{now:%d/%m/%Y}|A"]
        return "pharmacy-tonights-export.csv", "\n".join(rows) + "\n"
    raise UploadError(f"No template for {kind} systems.")


def _patient_names(mrn: str) -> str:
    path = DATA_DIR / "ehr" / "patients.json"
    for p in json.loads(path.read_text(encoding="utf-8")) if path.exists() else []:
        if p["mrn"] == mrn:
            return f"{p['name']['last'].upper()}^{p['name']['first'].upper()}"
    return "DOE^JANE"


def reset() -> str:
    """Rebuild all four mock systems from the dataset snapshot (removes every upload)."""
    from mock_systems.drift import reset as rebuild

    return rebuild(DATA_DIR)
