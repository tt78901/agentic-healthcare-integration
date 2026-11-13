"""Adapter for pharmacy-csv (pharmacy nightly export). Pattern: file_drop_ingest.

STUB FIXTURE: hand-written for smoke tests. Not model output.
"""
import csv
import glob
import os
import re

SYSTEM_ID = "pharmacy-csv"
SUPPORTED = ["MedicationRequest"]

MRN_SYSTEM = "http://hospital.example.org/mrn"
STATUS = {"A": "active", "S": "stopped", "C": "completed"}


def _rows(config):
    """Latest row per prescription: files are read in name (date) order and later files win."""
    latest = {}
    for path in sorted(glob.glob(os.path.join(config["path"], config.get("pattern", "*.csv")))):
        with open(path, encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh, delimiter="|"):
                if row.get("RX_NO"):
                    latest[row["RX_NO"]] = row
    return latest


def _date(value):
    day, month, year = value.split("/")
    return f"{year}-{month}-{day}"


def fetch(resource_type, config):
    if resource_type not in SUPPORTED:
        raise ValueError("unsupported resource type")
    out = []
    for rx_no, row in _rows(config).items():
        coding = {"system": "http://www.nlm.nih.gov/research/umls/rxnorm", "code": row["RXNORM_CD"]}
        if row.get("DRUG_DESC"):
            coding["display"] = row["DRUG_DESC"]
        res = {"resourceType": "MedicationRequest",
               "id": f"{SYSTEM_ID}-{re.sub(r'[^A-Za-z0-9.-]', '-', rx_no)}",
               "meta": {"source": f"urn:system:{SYSTEM_ID}"},
               "status": STATUS.get(row.get("STAT"), "unknown"), "intent": "order",
               "medicationCodeableConcept": {"coding": [coding]},
               "subject": {"identifier": {"system": MRN_SYSTEM, "value": row["MRN"]}}}
        if row.get("START_DT"):
            res["authoredOn"] = _date(row["START_DT"])
        if row.get("SIG_TXT"):
            res["dosageInstruction"] = [{"text": row["SIG_TXT"]}]
        if row.get("QTY"):
            res["dispenseRequest"] = {"quantity": {"value": float(row["QTY"])}}
        out.append(res)
    return sorted(out, key=lambda r: r["id"])
