"""Simulate a source system changing underneath its integration.

    python -m mock_systems.drift lab      vendor upgrade renames LAB_RSLT.RSLT_VAL to RESULT_VALUE
                                          (loud: the promoted adapter crashes)
    python -m mock_systems.drift ehr      API release renames dob to dateOfBirth and switches it to ISO dates
                                          (silent: the adapter still runs and emits valid FHIR, minus birth dates)
    python -m mock_systems.drift reset    rebuild all four systems as they were

Then:  python -m fabric check           detects both
       python -m fabric check --heal    sends the drifted systems back through the agent pipeline
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

from .build import DATA_DIR, build_all


def drift_lab(data_dir: Path = DATA_DIR) -> str:
    db = sqlite3.connect(data_dir / "lab" / "lab.db")
    columns = [c[1] for c in db.execute('PRAGMA table_info("LAB_RSLT")')]
    if "RSLT_VAL" not in columns:
        db.close()
        return "lab: already drifted"
    db.execute('ALTER TABLE LAB_RSLT RENAME COLUMN RSLT_VAL TO RESULT_VALUE')
    db.commit()
    db.close()
    return "lab: LAB_RSLT.RSLT_VAL renamed to RESULT_VALUE"


def drift_ehr(data_dir: Path = DATA_DIR) -> str:
    path = data_dir / "ehr" / "patients.json"
    patients = json.loads(path.read_text(encoding="utf-8"))
    if patients and "dob" not in patients[0]:
        return "ehr: already drifted"
    for p in patients:
        p["dateOfBirth"] = dt.datetime.strptime(p.pop("dob"), "%m/%d/%Y").strftime("%Y-%m-%d")
    path.write_text(json.dumps(patients, indent=1), encoding="utf-8")
    return "ehr: patient field dob (MM/DD/YYYY) replaced by dateOfBirth (YYYY-MM-DD)"


def reset(data_dir: Path = DATA_DIR) -> str:
    snapshot = data_dir / "dataset.json"
    if not snapshot.exists():
        raise SystemExit("No dataset snapshot found. Run: python -m fabric seed")
    build_all(json.loads(snapshot.read_text(encoding="utf-8")), data_dir)
    return "all four systems rebuilt from the dataset snapshot"


if __name__ == "__main__":
    actions = {"lab": drift_lab, "ehr": drift_ehr, "reset": reset}
    if len(sys.argv) != 2 or sys.argv[1] not in actions:
        raise SystemExit(__doc__)
    print(actions[sys.argv[1]]())
