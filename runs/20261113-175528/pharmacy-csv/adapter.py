import csv
import glob
import os
import re
from datetime import datetime

SYSTEM_ID = "pharmacy-csv"
SUPPORTED = ["MedicationRequest"]


def _clean_id(raw_str: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9\-\.]", "-", raw_str)
    return cleaned[:64]


def _parse_date(date_str: str) -> str:
    if not date_str:
        return ""
    try:
        dt = datetime.strptime(date_str.strip(), "%d/%m/%Y")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _transform_row(row: dict) -> dict:
    rx_no = (row.get("RX_NO") or "").strip()
    if not rx_no:
        return None

    res_id = _clean_id(f"{SYSTEM_ID}-{rx_no}")

    res = {
        "resourceType": "MedicationRequest",
        "id": res_id,
        "meta": {"source": f"urn:system:{SYSTEM_ID}"},
        "intent": "order",
    }

    stat = (row.get("STAT") or "").strip()
    stat_map = {
        "A": "active",
        "C": "completed",
        "S": "stopped",
    }
    if stat in stat_map:
        res["status"] = stat_map[stat]

    mrn = (row.get("MRN") or "").strip()
    if mrn:
        res["subject"] = {
            "identifier": {
                "system": "http://hospital.example.org/mrn",
                "value": mrn,
            }
        }

    code = (row.get("RXNORM_CD") or "").strip()
    display = (row.get("DRUG_DESC") or "").strip()

    coding_obj = {"system": "http://www.nlm.nih.gov/research/umls/rxnorm"}
    if code:
        coding_obj["code"] = code
    if display:
        coding_obj["display"] = display

    if "code" in coding_obj or "display" in coding_obj:
        res["medicationCodeableConcept"] = {"coding": [coding_obj]}

    sig = (row.get("SIG_TXT") or "").strip()
    if sig:
        res["dosageInstruction"] = [{"text": sig}]

    qty_str = (row.get("QTY") or "").strip()
    if qty_str:
        try:
            qty_val = int(qty_str)
            res["dispenseRequest"] = {"quantity": {"value": qty_val}}
        except ValueError:
            try:
                qty_val = float(qty_str)
                res["dispenseRequest"] = {"quantity": {"value": qty_val}}
            except ValueError:
                pass

    start_dt = (row.get("START_DT") or "").strip()
    authored_on = _parse_date(start_dt)
    if authored_on:
        res["authoredOn"] = authored_on

    return res


def fetch(resource_type: str, config: dict) -> list[dict]:
    if resource_type not in SUPPORTED:
        raise ValueError(f"Resource type '{resource_type}' is not supported.")

    path = config.get("path", "")
    pattern = config.get("pattern", "rx_export_*.csv")

    search_pattern = os.path.join(path, pattern)
    file_paths = sorted(glob.glob(search_pattern))

    records = {}

    for file_path in file_paths:
        if not os.path.isfile(file_path):
            continue
        with open(file_path, mode="r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f, delimiter="|")
            for row in reader:
                resource = _transform_row(row)
                if resource:
                    records[resource["id"]] = resource

    return [records[k] for k in sorted(records.keys())]
