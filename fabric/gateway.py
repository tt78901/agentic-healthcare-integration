"""Operate: the unified FHIR API over every promoted adapter.

Only adapters that passed validation and the promotion gate are loaded (see
generated/registry.json). One patient's record can now be read across all
source systems by MRN:

    GET /fhir/Patient/{mrn}/everything

Run:  python -m fabric gateway   (http://127.0.0.1:8000, docs at /docs)
"""
from __future__ import annotations

import importlib.util
import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from .audit import now
from .config import GENERATED, RUNS, load_config
from .resilience import CircuitBreaker, SingleFlight, TTLCache
from .standards import RESOURCE_TYPES

# Fault-tolerance settings for the fan-out to source systems
CACHE_SECONDS = 30          # serve a fresh copy within this window without calling the source
STALE_MAX_SECONDS = 300     # if a source fails, serve a copy up to this old, marked as stale
SOURCE_TIMEOUT = 10.0       # per-source deadline for one request; a slow source cannot stall the rest
RETRIES = 1                 # extra attempts on error (adapters are read-only, so retries are safe)
BACKOFF_SECONDS = 0.2       # wait before a retry, doubled each time
BREAKER_THRESHOLD = 3       # consecutive failures that open a source's circuit
BREAKER_COOLDOWN = 30.0     # seconds an open circuit waits before one probe

log = logging.getLogger("fabric.gateway")
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)

app = FastAPI(title="Integration Fabric: unified FHIR API", version="0.2.0",
              description="FHIR R4 facade over the hospital estate. Every source below was integrated by the "
                          "agent pipeline, validated against the estate contract and approved at a human gate. "
                          "Sources are queried in parallel with per-source timeouts, retries and circuit breakers; "
                          "a partial answer carries an OperationOutcome naming the sources that were missing or stale.")

_cfg = load_config()
_mrn_system = _cfg["settings"]["mrn_system"]
_systems = {s["id"]: s for s in _cfg["systems"]}
_adapters: dict[str, object] = {}
_adapter_lock = threading.Lock()
_cache = TTLCache(CACHE_SECONDS, STALE_MAX_SECONDS)
_flights = SingleFlight()
_breakers: dict[str, CircuitBreaker] = {}
_health: dict[str, dict] = {}
_health_lock = threading.Lock()
# Two pools so a request waiting on its sources never starves those sources of threads
_scatter = ThreadPoolExecutor(max_workers=32, thread_name_prefix="scatter")
_pool = ThreadPoolExecutor(max_workers=32, thread_name_prefix="source")


@dataclass
class SourceResult:
    system_id: str
    resource_type: str
    items: list = field(default_factory=list)
    state: str = "fresh"        # fresh | cached | stale | failed | timeout | circuit-open
    detail: str = ""
    ms: int = 0

    @property
    def degraded(self) -> bool:
        return self.state in ("stale", "failed", "timeout", "circuit-open")


def _registry() -> dict:
    path = GENERATED / "registry.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _breaker(system_id: str) -> CircuitBreaker:
    with _adapter_lock:
        if system_id not in _breakers:
            _breakers[system_id] = CircuitBreaker(BREAKER_THRESHOLD, BREAKER_COOLDOWN)
        return _breakers[system_id]


def _adapter(system_id: str):
    with _adapter_lock:
        if system_id not in _adapters:
            spec = importlib.util.spec_from_file_location(f"adapter_{system_id}", GENERATED / system_id / "adapter.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _adapters[system_id] = module
        return _adapters[system_id]


def _set_health(system_id: str, **data) -> None:
    with _health_lock:
        _health[system_id] = data


def _call_source(system_id: str, resource_type: str) -> list:
    """One source call with retry and exponential backoff. Raises on final failure."""
    system = _systems[system_id]
    config = {k: v for k, v in system.items() if k in ("path", "pattern", "base_url", "headers")}
    delay, last = BACKOFF_SECONDS, None
    for attempt in range(RETRIES + 1):
        try:
            return _adapter(system_id).fetch(resource_type, dict(config))
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < RETRIES:
                time.sleep(delay)
                delay *= 2
    raise last


def _fetch(system_id: str, resource_type: str) -> SourceResult:
    """Read one resource type from one source, never raising: fresh cache, then the
    source (behind its circuit breaker, coalesced), then a stale copy, then nothing."""
    key = (system_id, resource_type)
    hit = _cache.fresh(key)
    if hit is not None:
        return SourceResult(system_id, resource_type, hit, "cached")
    with _flights.lock_for(key):           # single-flight: one caller fetches, the others reuse it
        hit = _cache.fresh(key)
        if hit is not None:
            return SourceResult(system_id, resource_type, hit, "cached")
        breaker = _breaker(system_id)
        if not breaker.allow():
            return _fallback(key, "circuit-open", "circuit open after repeated failures; not called")
        started = time.perf_counter()
        future = _pool.submit(_call_source, system_id, resource_type)
        try:
            items = future.result(timeout=SOURCE_TIMEOUT)
        except FutureTimeout:
            breaker.failure()
            _set_health(system_id, status="down", last_error="Timeout", at=now())
            return _fallback(key, "timeout", f"no answer within {SOURCE_TIMEOUT:g}s")
        except Exception as exc:  # noqa: BLE001  (a source being down must not take the gateway down)
            breaker.failure()
            _set_health(system_id, status="down", last_error=type(exc).__name__, at=now())
            return _fallback(key, "failed", f"{type(exc).__name__} after {RETRIES + 1} attempt(s)")
        ms = int((time.perf_counter() - started) * 1000)
        breaker.success()
        _cache.put(key, items)
        _set_health(system_id, status="up", last_ok=now(), last_ms=ms)
        return SourceResult(system_id, resource_type, items, "fresh", ms=ms)


def _fallback(key: tuple, state: str, detail: str) -> SourceResult:
    system_id, resource_type = key
    stale = _cache.stale(key)
    if stale is not None:
        items, age = stale
        return SourceResult(system_id, resource_type, items, "stale", f"{detail}; served a copy {int(age)}s old")
    return SourceResult(system_id, resource_type, [], state, detail)


def _mrn_of(resource: dict) -> str | None:
    if resource.get("resourceType") == "Patient":
        for ident in resource.get("identifier", []):
            if ident.get("system") == _mrn_system:
                return ident.get("value")
        return None
    return ((resource.get("subject") or {}).get("identifier") or {}).get("value")


def _gather(resource_types: list[str], mrn: str | None = None,
            source: str | None = None) -> tuple[list[dict], list[SourceResult]]:
    """Scatter-gather: query every relevant source in parallel, then merge in systems.yaml
    order so the result does not depend on which source answered first."""
    registry = _registry()
    tasks = [(rt, sid) for rt in resource_types for sid in _systems
             if (entry := registry.get(sid)) and rt in entry["resource_types"] and (not source or source == sid)]
    futures = {task: _scatter.submit(_fetch, task[1], task[0]) for task in tasks}
    results = {task: fut.result() for task, fut in futures.items()}  # _fetch enforces its own deadline
    out: list[dict] = []
    for rt in resource_types:
        seen_patients: set[str] = set()
        for sid in _systems:  # for Patient, the first source listed in systems.yaml wins
            result = results.get((rt, sid))
            if result is None:
                continue
            for res in result.items:
                res_mrn = _mrn_of(res)
                if mrn and res_mrn != mrn:
                    continue
                if rt == "Patient" and not source:  # one Patient per MRN across the estate
                    if res_mrn in seen_patients:
                        continue
                    seen_patients.add(res_mrn)
                out.append(res)
    return out, list(results.values())


def _collect(resource_type: str, mrn: str | None = None, source: str | None = None) -> list[dict]:
    return _gather([resource_type], mrn, source)[0]


_ISSUE = {"stale": ("warning", "transient"), "failed": ("error", "transient"),
          "timeout": ("error", "timeout"), "circuit-open": ("error", "incomplete")}


def _outcome(results: list[SourceResult]) -> dict | None:
    """FHIR OperationOutcome naming every source that was missing or stale."""
    issues = []
    for r in results:
        if r.degraded:
            severity, code = _ISSUE[r.state]
            what = "stale data" if r.state == "stale" else "missing"
            issues.append({"severity": severity, "code": code,
                           "diagnostics": f"{r.system_id} {r.resource_type}: {what} ({r.detail})"})
    if not issues:
        return None
    return {"resourceType": "OperationOutcome", "id": "partial-" + uuid.uuid4().hex[:12], "issue": issues}


def _bundle(resources: list[dict], results: list[SourceResult] | None = None, response: Response | None = None,
            count: int | None = None) -> dict:
    shown = resources[:count] if count else resources
    entries = [{"fullUrl": f"{r['resourceType']}/{r['id']}", "resource": r, "search": {"mode": "match"}}
               for r in shown]
    outcome = _outcome(results or [])
    if outcome:
        entries.append({"fullUrl": f"OperationOutcome/{outcome['id']}", "resource": outcome,
                        "search": {"mode": "outcome"}})
    if response is not None:
        response.headers["X-Fabric-Partial"] = "true" if outcome else "false"
        response.headers["X-Fabric-Sources"] = ", ".join(sorted({f"{r.system_id}={r.state}" for r in results or []}))
    return {"resourceType": "Bundle", "type": "searchset", "total": len(resources), "entry": entries}


@app.middleware("http")
async def request_id(request: Request, call_next):
    """Correlation id on every request and one structured log line per request."""
    rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
    started = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Request-ID"] = rid
    if request.url.path.startswith(("/fhir", "/systems", "/check")):
        log.info(json.dumps({"ts": now(), "request_id": rid, "method": request.method, "path": request.url.path,
                             "status": response.status_code, "ms": int((time.perf_counter() - started) * 1000),
                             "partial": response.headers.get("X-Fabric-Partial"),
                             "sources": response.headers.get("X-Fabric-Sources")}))
    return response


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "promoted_systems": sorted(_registry())}


def _status() -> dict:
    path = GENERATED / "status.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/console")


@app.get("/console", include_in_schema=False)
def console() -> FileResponse:
    return FileResponse(Path(__file__).with_name("console.html"), media_type="text/html")


@app.get("/report", include_in_schema=False)
def latest_report():
    latest = RUNS / "latest.txt"
    if latest.exists():
        path = RUNS / latest.read_text(encoding="utf-8").strip() / "report.html"
        if path.exists():
            return FileResponse(path, media_type="text/html")
    return HTMLResponse("<p>No run report yet. Run: python -m fabric demo</p>", status_code=404)


@app.get("/systems", summary="Promoted source systems, their live status and their last health check")
def systems() -> dict:
    out, health = {}, _status()
    registry = _registry()
    for system_id in _systems:
        if system_id not in registry:
            continue
        with _health_lock:
            live = dict(_health.get(system_id, {"status": "not called yet"}))
        out[system_id] = {**registry[system_id], "name": _systems[system_id].get("name"),
                          "kind": _systems[system_id].get("kind"), "live": live,
                          "circuit": _breaker(system_id).snapshot(), "health": health.get(system_id)}
    return out


@app.post("/check", summary="Re-check every promoted integration for drift and contract failures")
def run_check() -> dict:
    from .operate import check

    result = check(_cfg, GENERATED)
    with _adapter_lock:
        _adapters.clear()
        _breakers.clear()
    _cache.clear()
    return result


# ---------------------------------------------------------------- demo uploads (switch off with --no-demo-uploads)

ACCEPTS = {"http": "JSON patients or conditions", "sqlite": "CSV lab results",
           "hl7v2_files": "HL7 v2 ADT messages", "delimited_files": "pipe-delimited export"}


_last_upload: dict[str, dict] = {}   # system id -> outcome of its most recent upload (for the console badge)


def _remember(system_id: str, outcome: dict) -> dict:
    _last_upload[system_id] = {"at": now(), "system": system_id, **outcome}
    return _last_upload[system_id]


def _uploads_on() -> None:
    from . import uploads

    if not uploads.enabled():
        raise HTTPException(status_code=403, detail="Demo uploads are switched off (gateway started with --no-demo-uploads)")


@app.get("/sources", summary="Source systems that accept demo uploads, and what each accepts")
def sources() -> dict:
    from . import uploads

    registry = _registry()
    return {"uploads": uploads.enabled(), "demo_mrn": uploads.demo_mrn(), "data_source": uploads.data_source(),
            "systems": {sid: {"name": s.get("name"), "kind": s["kind"], "accepts": ACCEPTS.get(s["kind"], ""),
                              "last_upload": _last_upload.get(sid)}
                        for sid, s in _systems.items() if sid in registry}}


@app.get("/sources/{system_id}/template", summary="A ready-to-upload example for one source system")
def source_template(system_id: str, mrn: str | None = None) -> Response:
    from . import uploads

    _uploads_on()
    if system_id not in _systems:
        raise HTTPException(status_code=404, detail="Unknown system")
    name, content = uploads.template(_systems[system_id], mrn)
    return Response(content, media_type="text/plain; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.post("/sources/{system_id}/upload", summary="Demo: new data arrives in one source system, is validated, bad records quarantined")
async def source_upload(system_id: str, request: Request) -> Response:
    """Write the upload, re-run drift detection and the 14-check contract on that system's
    promoted connector, and keep the data only if everything passes. If a multi-record file
    fails, each record is validated on its own: passing records are kept, failing ones are
    quarantined (never written) and reported."""
    from starlette.concurrency import run_in_threadpool

    from . import uploads
    from .operate import check_system

    _uploads_on()
    if system_id not in _systems or system_id not in _registry():
        raise HTTPException(status_code=404, detail="Unknown or unpromoted system")
    body = await request.body()

    def verify(system: dict) -> dict:
        return check_system(system, _mrn_system, GENERATED / system["id"], with_truth=False)

    filename = request.headers.get("X-Filename", "")[:200]
    try:
        result = await run_in_threadpool(uploads.ingest_with_quarantine, _systems[system_id], body, verify)
    except uploads.ValidationFailed as exc:
        _cache.clear()
        log.info(json.dumps({"ts": now(), "upload": system_id, "result": "rolled_back",
                             "failed": [f["id"] for f in exc.validation["failed"]],
                             "drift": bool(exc.validation["drift"])}))
        out = _remember(system_id, {"outcome": "rolled_back", "file": filename, "detail": str(exc),
                                    "validation": exc.validation, "records": getattr(exc, "records", None)})
        return JSONResponse(status_code=422, content=out)
    except uploads.UploadError as exc:
        out = _remember(system_id, {"outcome": "format_rejected", "file": filename, "detail": str(exc),
                                    "validation": None, "records": getattr(exc, "records", None)})
        return JSONResponse(status_code=422, content=out)
    _cache.clear()   # the next read goes to the source and sees the new data
    v = result["validation"]
    log.info(json.dumps({"ts": now(), "upload": system_id, "result": result["outcome"], "added": result["added"],
                         "checks": f"{v['checks_passed']}/{v['checks_total']}",
                         "quarantined": len((result.get("records") or {}).get("quarantined", []))}))
    return JSONResponse(content=_remember(system_id, {"file": filename, **result}))


@app.post("/sources/reset", summary="Demo: rebuild all four source systems as they were (removes uploads)")
def source_reset() -> dict:
    from . import uploads

    _uploads_on()
    message = uploads.reset()
    _last_upload.clear()
    _cache.clear()
    return {"reset": message}


@app.get("/fhir/Patient/{mrn}/everything", summary="One patient's record across every source system")
def everything(mrn: str, response: Response) -> dict:
    resources, results = _gather(RESOURCE_TYPES, mrn=mrn)
    if not resources and not any(r.degraded for r in results):
        raise HTTPException(status_code=404, detail="No resources for that MRN")
    return _bundle(resources, results, response)


@app.get("/fhir/{resource_type}", summary="Search one resource type across the estate")
def search(resource_type: str, response: Response, mrn: str | None = Query(None), source: str | None = Query(None),
           count: int = Query(100, ge=1, le=1000, alias="_count")) -> dict:
    if resource_type not in RESOURCE_TYPES:
        raise HTTPException(status_code=404, detail=f"Unknown resource type. Supported: {RESOURCE_TYPES}")
    resources, results = _gather([resource_type], mrn=mrn, source=source)
    return _bundle(resources, results, response, count)
