"""Discovery tools. Deterministic, read-only inspection of a source system.

The Discover agent does not get raw access to a system. It gets the output of
these probes: structure, aggregate field statistics and a few masked samples.
``count_distinct`` is the independent measurement the Validate stage reconciles
generated adapters against.
"""
from __future__ import annotations

import csv
import glob
import os
import sqlite3
from collections import Counter
from pathlib import Path

import httpx

from .privacy import Masker, field_stats, flatten, hl7_flatten, hl7_mask, public_stats, unflatten_masked

MAX_PROFILE_ROWS = 5000


class ProbeError(Exception):
    pass


# ----------------------------------------------------------------------- sqlite

def _connect_ro(path: str) -> sqlite3.Connection:
    if not os.path.exists(path):
        raise ProbeError(f"database not found: {path}")
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def _sqlite_tables(db: sqlite3.Connection) -> dict[str, list[str]]:
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    return {t: [c[1] for c in db.execute(f'PRAGMA table_info("{t}")')] for t in tables}


def probe_sqlite(system: dict, masker: Masker) -> dict:
    db = _connect_ro(system["path"])
    tables = []
    for table in _sqlite_tables(db):
        cols = [{"name": c[1], "declared_type": c[2], "not_null": bool(c[3]), "primary_key": bool(c[5])}
                for c in db.execute(f'PRAGMA table_info("{table}")')]
        fks = [f"{table}.{f[3]} -> {f[2]}.{f[4]}" for f in db.execute(f'PRAGMA foreign_key_list("{table}")')]
        rows = [dict(r) for r in db.execute(f'SELECT * FROM "{table}" LIMIT {MAX_PROFILE_ROWS}')]
        stats = field_stats(rows)
        tables.append({
            "table": table, "row_count": db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0],
            "columns": cols, "foreign_keys": fks, "field_statistics": public_stats(stats),
            "sample_rows_masked": [masker.mask_flat(r, stats) for r in rows[:3]],
        })
    db.close()
    return {"kind": "sqlite", "access": "read-only SQL (SQLite dialect)", "tables": tables}


# ------------------------------------------------------------------------- http

def _client(system: dict) -> httpx.Client:
    return httpx.Client(base_url=system["base_url"], headers=system.get("headers") or {}, timeout=15)


def _list_container(payload) -> tuple[str | None, list]:
    if isinstance(payload, list):
        return None, payload
    if isinstance(payload, dict):
        for key, value in payload.items():
            if isinstance(value, list) and (not value or isinstance(value[0], dict)):
                return key, value
    return None, []


def http_items(system: dict, path: str, limit: int = MAX_PROFILE_ROWS) -> list[dict]:
    """All items of a list endpoint. Follows simple page-number pagination (?page=N)."""
    items: list[dict] = []
    with _client(system) as client:
        page = 1
        while len(items) < limit:
            resp = client.get(path, params={"page": page})
            if resp.status_code != 200:
                raise ProbeError(f"GET {path} returned HTTP {resp.status_code}")
            _, batch = _list_container(resp.json())
            if not batch or (page > 1 and batch == items[:len(batch)]):  # no paging support
                break
            items.extend(batch)
            page += 1
            if page > 500:
                break
    return items


def probe_http(system: dict, masker: Masker) -> dict:
    with _client(system) as client:
        try:
            spec = client.get(system.get("docs_path", "/openapi.json")).json()
        except Exception as exc:  # noqa: BLE001
            raise ProbeError(f"cannot read API description at {system['base_url']}: {type(exc).__name__}") from exc
        endpoints, other = [], []
        for path, methods in spec.get("paths", {}).items():
            op = methods.get("get")
            if not op or path in ("/health",):
                continue
            params = [{"name": p["name"], "in": p["in"], "default": (p.get("schema") or {}).get("default"),
                       "maximum": (p.get("schema") or {}).get("maximum")} for p in op.get("parameters", [])]
            if "{" in path:
                other.append({"path": path, "summary": op.get("summary", ""), "parameters": params})
                continue
            first = client.get(path)
            if first.status_code != 200:
                other.append({"path": path, "summary": op.get("summary", ""), "note": f"HTTP {first.status_code}"})
                continue
            payload = first.json()
            key, _ = _list_container(payload)
            items = http_items(system, path)
            stats = field_stats([flatten(i) for i in items])
            envelope = {k: v for k, v in payload.items() if k != key} if isinstance(payload, dict) else {}
            endpoints.append({
                "path": path, "summary": op.get("summary", ""), "parameters": params,
                "response_shape": {"items_under_key": key, "other_keys_example": envelope},
                "item_count_all_pages": len(items), "field_statistics": public_stats(stats),
                "sample_items_masked": [unflatten_masked(i, stats, masker) for i in items[:3]],
            })
    return {"kind": "http", "api_title": spec.get("info", {}).get("title"), "api_version": spec.get("info", {}).get("version"),
            "api_description": spec.get("info", {}).get("description"),
            "auth_headers_required": sorted((system.get("headers") or {}).keys()),
            "list_endpoints": endpoints, "other_endpoints": other}


# ------------------------------------------------------------------ HL7 v2 files

def _files(system: dict) -> list[str]:
    files = sorted(glob.glob(os.path.join(system["path"], system.get("pattern", "*"))))
    if not files:
        raise ProbeError(f"no files match {system.get('pattern', '*')} in the configured folder")
    return files


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def probe_hl7(system: dict, masker: Masker) -> dict:
    files = _files(system)
    messages = [_read(f) for f in files[:MAX_PROFILE_ROWS]]
    flat = [hl7_flatten(m) for m in messages]
    stats = field_stats(flat)
    types = Counter(f"{m.get('MSH.9.1', '?')}^{m.get('MSH.9.2', '?')}" for m in flat)
    samples, seen = [], set()
    for msg, m in zip(messages, flat):
        t = f"{m.get('MSH.9.1', '?')}^{m.get('MSH.9.2', '?')}"
        if t not in seen:
            seen.add(t)
            samples.append({"message_type": t, "message_masked": hl7_mask(msg, stats, masker)})
    return {"kind": "hl7v2_files", "access": "read-only folder of files, one HL7 v2 message per file",
            "file_count": len(files), "file_name_example": masker.pseudonym(os.path.basename(files[0])),
            "segment_terminator": "carriage return (\\r)", "message_type_counts": dict(types),
            "field_statistics_by_hl7_path": public_stats(stats), "sample_messages_masked": samples}


# -------------------------------------------------------------- delimited files

def _delimited_rows(system: dict) -> tuple[str, list[str], list[dict], dict[str, int]]:
    rows: list[dict] = []
    per_file: dict[str, int] = {}
    header: list[str] = []
    delimiter = ","
    for f in _files(system):
        text = _read(f)
        first_line = text.splitlines()[0] if text else ""
        delimiter = max("|;,\t", key=first_line.count)
        reader = csv.DictReader(text.splitlines(), delimiter=delimiter)
        header = reader.fieldnames or header
        batch = [dict(r, **{"__file__": os.path.basename(f)}) for r in reader]
        per_file[os.path.basename(f)] = len(batch)
        rows.extend(batch)
    return delimiter, header, rows, per_file


def probe_delimited(system: dict, masker: Masker) -> dict:
    delimiter, header, rows, per_file = _delimited_rows(system)
    data = [{k: v for k, v in r.items() if k != "__file__"} for r in rows]
    stats = field_stats(data)
    return {"kind": "delimited_files", "access": "read-only folder of delimited text files",
            "file_pattern": system.get("pattern"), "files_and_row_counts": per_file,
            "delimiter": delimiter, "header": header, "total_rows": len(rows),
            "field_statistics": public_stats(stats),
            "sample_rows_masked": [masker.mask_flat(r, stats) for r in data[:3]]}


# -------------------------------------------------------------------- dispatch

PROBES = {"sqlite": probe_sqlite, "http": probe_http, "hl7v2_files": probe_hl7, "delimited_files": probe_delimited}


def probe(system: dict, masker: Masker) -> dict:
    if system["kind"] not in PROBES:
        raise ProbeError(f"no probe for system kind '{system['kind']}'")
    return PROBES[system["kind"]](system, masker)


def count_distinct(system: dict, entity: str, key: str) -> int:
    """Independent count of distinct source records, used to reconcile an adapter's output."""
    kind = system["kind"]
    if kind == "sqlite":
        db = _connect_ro(system["path"])
        tables = _sqlite_tables(db)
        if entity not in tables or key not in tables[entity]:
            raise ProbeError(f"no column {entity}.{key}")
        n = db.execute(f'SELECT COUNT(DISTINCT "{key}") FROM "{entity}"').fetchone()[0]
        db.close()
        return n
    if kind == "http":
        values = {flatten(i).get(key) for i in http_items(system, entity)}
    elif kind == "hl7v2_files":
        wanted = {t.strip() for t in entity.split(",")} if entity and entity.lower() not in ("messages", "all", "*") else None
        values = set()
        for f in _files(system):
            m = hl7_flatten(_read(f))
            if wanted is None or f"{m.get('MSH.9.1')}^{m.get('MSH.9.2')}" in wanted:
                values.add(m.get(key))
    elif kind == "delimited_files":
        _, header, rows, _ = _delimited_rows(system)
        if key not in header:
            raise ProbeError(f"no column {key}")
        values = {r.get(key) for r in rows}
    else:
        raise ProbeError(f"no counter for system kind '{kind}'")
    values.discard(None)
    values.discard("")
    if not values:
        raise ProbeError(f"key '{key}' not found in '{entity}'")
    return len(values)
