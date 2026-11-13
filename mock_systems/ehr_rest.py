"""MediCore EHR: a mock REST API. It is one of the "systems with a usable API" in
the estate, but it is not FHIR: custom JSON, US-style dates, single-letter codes,
page-based pagination and an API key header.

Run:  python -m mock_systems.ehr_rest   (http://127.0.0.1:8101, docs at /docs)
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query

DATA = Path(__file__).parent / "_data" / "ehr"
API_KEY = os.environ.get("EHR_API_KEY", "demo-key")

app = FastAPI(title="MediCore EHR API", version="2.3.1",
              description="Patient demographics and problem list. Requires the X-API-Key header.")


def _load(name: str) -> list[dict]:
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def _auth(x_api_key: str = Header(default="")) -> None:
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Missing or invalid X-API-Key")


def _page(items: list[dict], page: int, page_size: int) -> dict:
    start = (page - 1) * page_size
    return {"data": items[start:start + page_size],
            "paging": {"page": page, "pageSize": page_size, "total": len(items)}}


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/v2/patients", dependencies=[Depends(_auth)], summary="List patients (paged)")
def list_patients(page: int = Query(1, ge=1), pageSize: int = Query(25, ge=1, le=100)) -> dict:
    return _page(_load("patients.json"), page, pageSize)


@app.get("/api/v2/patients/{patientId}", dependencies=[Depends(_auth)], summary="Get one patient")
def get_patient(patientId: str) -> dict:
    for p in _load("patients.json"):
        if p["patientId"] == patientId:
            return p
    raise HTTPException(status_code=404, detail="Patient not found")


@app.get("/api/v2/conditions", dependencies=[Depends(_auth)], summary="List problem-list conditions (paged)")
def list_conditions(page: int = Query(1, ge=1), pageSize: int = Query(25, ge=1, le=100)) -> dict:
    return _page(_load("conditions.json"), page, pageSize)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("EHR_PORT", "8101")), log_level="warning")
