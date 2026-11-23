import csv
from datetime import datetime
import glob
import os
import re

SYSTEM_ID = "pharmacy-csv"
SUPPORTED = ["MedicationRequest"]

STAT_MAP = {
    "A": "active",
    "C": "completed",
    "S": "stopped",
}


def sanitize_id(raw_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9\-\.]", "-", raw_id)
    return cleaned[:64]


def parse_date(dt_str: str) -> str | None:
    if not dt_str:
        return None
    dt_str = dt_str.strip()
    if not dt_str:
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(dt_str, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def parse_qty(qty_str: str):
    if not qty_str:
        return None
    qty_str = qty_str.strip()
    if not qty_str:
        return None
    try:
        return int(qty_str)
    except ValueError:
        try:
            return float(qty_str)
        except ValueError:
            return None


def fetch(resource_type: str, config: dict) -> list[dict]:
    if resource_type not in SUPPORTED:
        raise ValueError(f"Unsupported resource type: {resource_type}")

    path = config.get("path", "")
    pattern = config.get("pattern", "rx_export_*.csv")

    search_path = os.path.join(path, pattern)
    matching_files = sorted(glob.glob(search_path))

    records_by_rx = {}

    for filepath in matching_files:
        with open(filepath, mode="r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f, delimiter="|")
            for row in reader:
                rx_no = (row.get("RX_NO") or "").strip()
                if not rx_no:
                    continue
                records_by_rx[rx_no] = row

    resources = []
    for rx_no, row in records_by_rx.items():
        res_id = sanitize_id(f"{SYSTEM_ID}-{rx_no}")

        stat = (row.get("STAT") or "").strip()
        status_val = STAT_MAP.get(stat)

        mrn = (row.get("MRN") or "").strip()
        drug_desc = (row.get("DRUG_DESC") or "").strip()
        rxnorm_cd = (row.get("RXNORM_CD") or "").strip()
        sig_txt = (row.get("SIG_TXT") or "").strip()
        qty = (row.get("QTY") or "").strip()
        start_dt = (row.get("START_DT") or "").strip()

        res = {
            "resourceType": "MedicationRequest",
            "id": res_id,
            "meta": {"source": f"urn:system:{SYSTEM_ID}"},
            "intent": "order",
        }

        if status_val:
            res["status"] = status_val

        coding = {"system": "http://www.nlm.nih.gov/research/umls/rxnorm"}
        if rxnorm_cd:
            coding["code"] = rxnorm_cd
        if drug_desc:
            coding["display"] = drug_desc
        if "code" in coding or "display" in coding:
            res["medicationCodeableConcept"] = {"coding": [coding]}

        if mrn:
            res["subject"] = {
                "identifier": {
                    "system": "http://hospital.example.org/mrn",
                    "value": mrn,
                }
            }

        authored_on = parse_date(start_dt)
        if authored_on:
            res["authoredOn"] = authored_on

        if sig_txt:
            res["dosageInstruction"] = [{"text": sig_txt}]

        qty_val = parse_qty(qty)
        if qty_val is not None:
            res["dispenseRequest"] = {"quantity": {"value": qty_val}}

        resources.append(res)

    resources.sort(key=lambda r: r["id"])
    return resources
