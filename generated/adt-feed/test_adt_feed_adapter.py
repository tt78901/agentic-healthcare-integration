"""Contract test for the adt-feed adapter. Emitted by the fabric at promotion.

Re-runs the full validation contract (static safety, sandboxed run, FHIR R4,
estate conventions, reconciliation against the live source):  pytest generated/
"""
import json
from pathlib import Path

from fabric.__main__ import ensure_mocks
from fabric.config import load_config
from fabric.validate import validate

HERE = Path(__file__).parent


def test_adt_feed_adapter_meets_contract(tmp_path):
    cfg = load_config()
    ensure_mocks(cfg)
    system = next(s for s in cfg["systems"] if s["id"] == "adt-feed")
    design = json.loads((HERE / "design.json").read_text(encoding="utf-8"))
    result = validate((HERE / "adapter.py").read_text(encoding="utf-8"), system, design,
                      cfg["settings"]["mrn_system"], tmp_path)
    failed = [c for c in result["checks"] if c["status"] != "pass"]
    assert not failed, failed
