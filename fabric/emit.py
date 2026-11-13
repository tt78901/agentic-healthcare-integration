"""Deterministic emitters: artefacts rendered from an approved design by template,
so every system gets the same shape of OpenAPI document and test file."""
from __future__ import annotations

import yaml

TEST_TEMPLATE = '''"""Contract test for the {system_id} adapter. Emitted by the fabric at promotion.

Re-runs the full validation contract (static safety, sandboxed run, FHIR R4,
estate conventions, reconciliation against the live source):  pytest generated/
"""
import json
from pathlib import Path

from fabric.__main__ import ensure_mocks
from fabric.config import load_config
from fabric.validate import validate

HERE = Path(__file__).parent


def test_{func_name}_adapter_meets_contract(tmp_path):
    cfg = load_config()
    ensure_mocks(cfg)
    system = next(s for s in cfg["systems"] if s["id"] == "{system_id}")
    design = json.loads((HERE / "design.json").read_text(encoding="utf-8"))
    result = validate((HERE / "adapter.py").read_text(encoding="utf-8"), system, design,
                      cfg["settings"]["mrn_system"], tmp_path)
    failed = [c for c in result["checks"] if c["status"] != "pass"]
    assert not failed, failed
'''


def openapi_for(system: dict, design: dict) -> str:
    """OpenAPI 3 description of what this system contributes to the unified FHIR API."""
    paths = {}
    for res in design.get("resources", []):
        rt = res["resource_type"]
        paths[f"/fhir/{rt}"] = {"get": {
            "summary": f"{rt} resources sourced from {system.get('name', system['id'])}",
            "parameters": [
                {"name": "mrn", "in": "query", "required": False, "schema": {"type": "string"},
                 "description": "Return only resources for the patient with this MRN."},
                {"name": "source", "in": "query", "required": False, "schema": {"type": "string"},
                 "description": f"Restrict to one source system, e.g. {system['id']}."},
            ],
            "responses": {"200": {"description": "FHIR R4 searchset Bundle", "content": {
                "application/fhir+json": {"schema": {"$ref": "#/components/schemas/Bundle"}}}}},
            "x-source-entity": res.get("source_entity"),
            "x-merge-rule": res.get("merge_rule"),
            "x-field-map": res.get("field_map", []),
        }}
    doc = {
        "openapi": "3.0.3",
        "info": {"title": f"{system.get('name', system['id'])}: FHIR facade", "version": "0.1.0",
                 "description": f"Integration pattern: {design.get('pattern')}. {design.get('pattern_rationale', '')}"},
        "paths": paths,
        "components": {"schemas": {"Bundle": {
            "type": "object", "description": "FHIR R4 Bundle (type searchset)",
            "externalDocs": {"url": "https://hl7.org/fhir/R4/bundle.html"},
            "properties": {"resourceType": {"type": "string", "enum": ["Bundle"]},
                           "type": {"type": "string", "enum": ["searchset"]},
                           "total": {"type": "integer"},
                           "entry": {"type": "array", "items": {"type": "object"}}}}}},
    }
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=110)


def test_file_for(system_id: str) -> tuple[str, str]:
    """(file name, content). The name is unique per system so pytest can collect them together."""
    func_name = system_id.replace("-", "_").replace(".", "_")
    return f"test_{func_name}_adapter.py", TEST_TEMPLATE.format(system_id=system_id, func_name=func_name)
