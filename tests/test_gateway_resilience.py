"""Gateway fault tolerance: one failing, slow or flapping source must not take down
or stall the unified API, and a partial answer must say so."""
import threading
import time

import pytest
from fastapi.testclient import TestClient

from fabric import gateway, pipeline
from fabric.resilience import CircuitBreaker, TTLCache
from fabric.stub import StubLLM


@pytest.fixture(scope="module")
def promoted(cfg, tmp_path_factory):
    tmp = tmp_path_factory.mktemp("gw")
    mp = pytest.MonkeyPatch()
    mp.setattr(pipeline, "RUNS", tmp / "runs")
    mp.setattr(pipeline, "GENERATED", tmp / "generated")
    pipeline.run(cfg, StubLLM(), None, auto_approve=True)
    mp.undo()
    return tmp / "generated"


@pytest.fixture()
def client(promoted, monkeypatch):
    monkeypatch.setattr(gateway, "GENERATED", promoted)
    monkeypatch.setattr(gateway, "BACKOFF_SECONDS", 0.01)
    monkeypatch.setattr(gateway, "_cache", TTLCache(gateway.CACHE_SECONDS, gateway.STALE_MAX_SECONDS))
    gateway._adapters.clear()
    gateway._breakers.clear()
    gateway._health.clear()
    yield TestClient(gateway.app)
    gateway._adapters.clear()
    gateway._breakers.clear()


def _mrn(client) -> str:
    return client.get("/fhir/Patient", params={"_count": 1}).json()["entry"][0]["resource"]["identifier"][0]["value"]


def _sources(bundle) -> set[str]:
    return {e["resource"]["meta"]["source"] for e in bundle["entry"] if e["search"]["mode"] == "match"}


def _break(system_id: str, monkeypatch, behaviour=None) -> dict:
    """Replace one source's fetch with a failing (or slow) one; returns a call counter."""
    calls = {"n": 0}

    def broken(resource_type, config):
        calls["n"] += 1
        if behaviour:
            return behaviour(resource_type, config)
        raise ConnectionError("source down")

    monkeypatch.setattr(gateway._adapter(system_id), "fetch", broken)
    return calls


def test_healthy_estate_answers_in_full(client):
    resp = client.get(f"/fhir/Patient/{_mrn(client)}/everything")
    bundle = resp.json()
    assert resp.headers["X-Fabric-Partial"] == "false" and resp.headers["X-Request-ID"]
    assert all(e["search"]["mode"] == "match" for e in bundle["entry"])
    assert len(_sources(bundle)) >= 2


def test_one_source_down_degrades_gracefully(client, monkeypatch):
    mrn = _mrn(client)
    gateway._cache.clear()
    calls = _break("pharmacy-csv", monkeypatch)
    resp = client.get(f"/fhir/Patient/{mrn}/everything")
    bundle = resp.json()
    assert resp.status_code == 200 and resp.headers["X-Fabric-Partial"] == "true"
    assert "urn:system:pharmacy-csv" not in _sources(bundle)
    assert "urn:system:ehr-rest" in _sources(bundle)                  # the other sources still answer
    outcome = [e["resource"] for e in bundle["entry"] if e["search"]["mode"] == "outcome"]
    assert len(outcome) == 1 and "pharmacy-csv" in outcome[0]["issue"][0]["diagnostics"]
    assert calls["n"] == gateway.RETRIES + 1                           # retried before giving up


def test_patient_master_fails_over_to_other_sources(client, monkeypatch):
    gateway._cache.clear()
    _break("ehr-rest", monkeypatch)
    bundle = client.get("/fhir/Patient", params={"_count": 1000}).json()
    sources = _sources(bundle)
    assert "urn:system:ehr-rest" not in sources and sources               # lab and ADT patients still served
    mrns = [e["resource"]["identifier"][0]["value"] for e in bundle["entry"] if e["search"]["mode"] == "match"]
    assert len(mrns) == len(set(mrns))                                  # still one Patient per MRN


def test_circuit_opens_and_stops_calling_a_failing_source(client, monkeypatch):
    gateway._cache.clear()
    calls = _break("lab-sql", monkeypatch)
    for _ in range(gateway.BREAKER_THRESHOLD):
        assert gateway._fetch("lab-sql", "Observation").state == "failed"
    before = calls["n"]
    result = gateway._fetch("lab-sql", "Observation")
    assert result.state == "circuit-open" and calls["n"] == before      # fail fast: source not called
    assert client.get("/systems").json()["lab-sql"]["circuit"]["state"] == "open"


def test_stale_copy_served_when_source_fails(client, monkeypatch):
    monkeypatch.setattr(gateway, "_cache", TTLCache(ttl=0, stale_max=300))  # every copy is immediately stale
    good = gateway._fetch("pharmacy-csv", "MedicationRequest")
    assert good.state == "fresh" and good.items
    _break("pharmacy-csv", monkeypatch)
    stale = gateway._fetch("pharmacy-csv", "MedicationRequest")
    assert stale.state == "stale" and stale.items == good.items and "old" in stale.detail


def test_slow_source_times_out_without_stalling_the_request(client, monkeypatch):
    gateway._cache.clear()
    monkeypatch.setattr(gateway, "SOURCE_TIMEOUT", 0.3)
    _break("pharmacy-csv", monkeypatch, behaviour=lambda rt, cfg: time.sleep(2) or [])
    started = time.perf_counter()
    resp = client.get(f"/fhir/Patient/{_mrn(client)}/everything")
    elapsed = time.perf_counter() - started
    assert elapsed < 1.5 and resp.headers["X-Fabric-Partial"] == "true"
    assert "pharmacy-csv=timeout" in resp.headers["X-Fabric-Sources"]


def test_concurrent_requests_share_one_source_call(client, monkeypatch):
    gateway._cache.clear()
    real = gateway._adapter("pharmacy-csv").fetch
    calls = _break("pharmacy-csv", monkeypatch, behaviour=lambda rt, cfg: time.sleep(0.3) or real(rt, cfg))
    threads = [threading.Thread(target=gateway._fetch, args=("pharmacy-csv", "MedicationRequest")) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert calls["n"] == 1                                               # single-flight coalescing


def test_breaker_half_open_allows_one_probe():
    clock = {"t": 0.0}
    b = CircuitBreaker(threshold=2, cooldown=10, clock=lambda: clock["t"])
    b.failure(); b.failure()
    assert b.state == "open" and not b.allow()
    clock["t"] = 11
    assert b.state == "half-open" and b.allow() and not b.allow()        # exactly one probe
    b.failure()
    assert b.state == "open"                                             # failed probe re-opens
    clock["t"] = 22
    assert b.allow()
    b.success()
    assert b.state == "closed" and b.allow()
