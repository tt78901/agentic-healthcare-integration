"""Validate stage: the same deterministic contract for every generated adapter.

No LLM is involved here. An adapter is promoted only if every check passes:

  static    safe to run: import allow-list, no dangerous calls, read-only SQL,
            no data logging, no hard-coded secrets, correct interface
  runtime   runs in a separate process, is deterministic and prints nothing
  fhir      every resource validates against FHIR R4 and the estate conventions
  reconcile resource counts equal an independent count of the source records

Failed checks are fed back to the Generate agent for repair (self-healing), with
data values scrubbed from the error text first.
"""
from __future__ import annotations

import ast
import importlib
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import secrets_of
from .privacy import scrub_error
from .probes import ProbeError, count_distinct
from .standards import ALLOWED_IMPORTS, RESOURCE_TYPES

RUNNER = Path(__file__).with_name("sandbox_runner.py")
ID_RX = re.compile(r"^[A-Za-z0-9\-.]{1,64}$")
WRITE_SQL = re.compile(r"\b(INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|DROP\s+(TABLE|INDEX|VIEW)|ALTER\s+TABLE|"
                       r"CREATE\s+(TABLE|INDEX|VIEW|TRIGGER)|REPLACE\s+INTO|ATTACH\s+DATABASE|VACUUM)\b", re.I)
BAD_CALLS = {"eval", "exec", "compile", "__import__", "input", "breakpoint"}
BAD_ATTRS = {"write_text", "write_bytes", "unlink", "rmtree", "rmdir", "executescript", "system", "popen",
             "mkdir", "makedirs", "touch", "remove", "rename", "chmod", "kill", "fork", "putenv"}
LOG_CALLS = {"print", "pprint"}
SECRET_ASSIGN = re.compile(r"(api[_-]?key|token|secret|password|passwd)\w*\s*=\s*['\"][^'\"]{4,}['\"]", re.I)

CHECK_TITLES = {
    "static.syntax": "Module parses",
    "static.imports": "Imports are on the allow-list",
    "static.no_dangerous_calls": "No dynamic execution, shell or file-write calls",
    "static.read_only_sql": "SQL is read-only",
    "static.no_data_logging": "No print or logging of records",
    "static.no_hardcoded_secrets": "No credentials in the code",
    "contract.interface": "SYSTEM_ID, SUPPORTED and fetch() match the contract",
    "contract.coverage": "Publishes exactly the resource types in the design",
    "runtime.executes": "Runs to completion in a separate process",
    "runtime.deterministic": "Two runs give identical output",
    "runtime.silent": "Writes nothing to stdout or stderr",
    "fhir.schema": "Every resource is valid FHIR R4",
    "fhir.conventions": "Estate conventions: ids, provenance, MRN linkage, no empty elements",
    "reconcile.counts": "Resource counts match the source",
}


@dataclass
class Check:
    id: str
    status: str  # pass | fail | skip
    detail: str = ""

    @property
    def title(self) -> str:
        return CHECK_TITLES[self.id]

    def as_dict(self) -> dict:
        return {**asdict(self), "title": self.title}


def _ok(check_id: str, detail: str = "") -> Check:
    return Check(check_id, "pass", detail)


def _fail(check_id: str, detail: str) -> Check:
    return Check(check_id, "fail", detail)


# ------------------------------------------------------------------ static checks

def static_checks(code: str, system: dict) -> list[Check]:
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return [_fail("static.syntax", f"SyntaxError at line {exc.lineno}: {exc.msg}")]
    checks = [_ok("static.syntax")]

    bad_imports, bad_calls, log_calls, write_sql = [], [], [], []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            bad_imports += [a.name for a in node.names if a.name not in ALLOWED_IMPORTS]
        elif isinstance(node, ast.ImportFrom):
            if node.level or (node.module or "") not in ALLOWED_IMPORTS:
                bad_imports.append(("." * node.level) + (node.module or ""))
        elif isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name):
                if fn.id in BAD_CALLS:
                    bad_calls.append(f"{fn.id}() line {node.lineno}")
                if fn.id in LOG_CALLS:
                    log_calls.append(f"{fn.id}() line {node.lineno}")
                if fn.id == "open" and _opens_for_write(node):
                    bad_calls.append(f"open() for writing, line {node.lineno}")
            elif isinstance(fn, ast.Attribute):
                owner = fn.value.id if isinstance(fn.value, ast.Name) else ""
                if fn.attr in BAD_ATTRS and not (fn.attr in ("remove", "rename") and owner not in ("os", "shutil")):
                    bad_calls.append(f".{fn.attr}() line {node.lineno}")
                if owner in ("logging", "logger", "warnings", "sys") or fn.attr in LOG_CALLS:
                    log_calls.append(f"{owner}.{fn.attr}() line {node.lineno}")
                if fn.attr == "open" and _opens_for_write(node):
                    bad_calls.append(f".open() for writing, line {node.lineno}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and WRITE_SQL.search(node.value):
            write_sql.append(f"line {node.lineno}")

    checks.append(_fail("static.imports", "not allowed: " + ", ".join(sorted(set(bad_imports)))) if bad_imports
                  else _ok("static.imports"))
    checks.append(_fail("static.no_dangerous_calls", "; ".join(bad_calls)) if bad_calls
                  else _ok("static.no_dangerous_calls"))
    checks.append(_fail("static.read_only_sql", "write statement in a string at " + ", ".join(write_sql))
                  if write_sql else _ok("static.read_only_sql"))
    checks.append(_fail("static.no_data_logging", "; ".join(log_calls) + ". Remove all output.") if log_calls
                  else _ok("static.no_data_logging"))

    leaked = any(s in code for s in secrets_of(system)) or bool(SECRET_ASSIGN.search(code))
    checks.append(_fail("static.no_hardcoded_secrets", "a credential literal appears in the code; read it from "
                        "config['headers'] instead") if leaked else _ok("static.no_hardcoded_secrets"))

    checks.append(_interface(tree, system))
    return checks


def _opens_for_write(call: ast.Call) -> bool:
    mode = None
    if len(call.args) >= 2 and isinstance(call.args[1], ast.Constant):
        mode = call.args[1].value
    for kw in call.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            mode = kw.value.value
    return isinstance(mode, str) and any(c in mode for c in "wax+")


def _interface(tree: ast.Module, system: dict) -> Check:
    consts, fetch = {}, None
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            try:
                consts[node.targets[0].id] = ast.literal_eval(node.value)
            except (ValueError, SyntaxError):
                pass
        elif isinstance(node, ast.FunctionDef) and node.name == "fetch":
            fetch = node
    problems = []
    if consts.get("SYSTEM_ID") != system["id"]:
        problems.append(f'SYSTEM_ID must be the literal "{system["id"]}"')
    supported = consts.get("SUPPORTED")
    if not isinstance(supported, list) or not supported or any(s not in RESOURCE_TYPES for s in supported):
        problems.append(f"SUPPORTED must be a literal list drawn from {RESOURCE_TYPES}")
    if fetch is None or [a.arg for a in fetch.args.args] != ["resource_type", "config"]:
        problems.append("define fetch(resource_type, config) at module level")
    return _fail("contract.interface", "; ".join(problems)) if problems else _ok("contract.interface")


# ----------------------------------------------------------------- sandboxed run

def run_adapter(adapter_path: Path, system: dict, timeout: int = 120) -> tuple[dict | None, str, str]:
    """Run the adapter in a child process. Returns (runner output, captured stdio, failure reason)."""
    config = {k: v for k, v in system.items() if k in ("path", "pattern", "base_url", "headers")}
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path, out_path = Path(tmp) / "config.json", Path(tmp) / "out.json"
        cfg_path.write_text(json.dumps(config), encoding="utf-8")
        try:
            proc = subprocess.run([sys.executable, "-I", str(RUNNER), str(adapter_path), str(cfg_path), str(out_path)],
                                  capture_output=True, text=True, timeout=timeout, cwd=tmp)
        except subprocess.TimeoutExpired:
            return None, "", f"timed out after {timeout}s"
        if not out_path.exists():
            return None, proc.stdout + proc.stderr, f"runner exited with code {proc.returncode}"
        return json.loads(out_path.read_text(encoding="utf-8")), proc.stdout + proc.stderr, ""


# ------------------------------------------------------------------ FHIR checks

def _model(resource_type: str):
    return getattr(importlib.import_module(f"fhir.resources.R4B.{resource_type.lower()}"), resource_type)


def _shape(value) -> str:
    if isinstance(value, (dict, list)):
        return type(value).__name__
    return re.sub(r"[A-Za-z]", "x", re.sub(r"\d", "9", str(value)))[:40]


def fhir_schema_check(resources: dict[str, list[dict]]) -> Check:
    from pydantic import ValidationError

    problems: dict[tuple, dict] = {}
    invalid = total = 0
    for rt, items in resources.items():
        model = _model(rt)
        for res in items:
            total += 1
            try:
                model.model_validate(res)
            except ValidationError as exc:
                invalid += 1
                for err in exc.errors():
                    key = (rt, ".".join(str(p) for p in err["loc"]), err["msg"])
                    entry = problems.setdefault(key, {"n": 0, "shape": _shape(err.get("input"))})
                    entry["n"] += 1
    if not invalid:
        return _ok("fhir.schema", f"{total} resources valid")
    top = sorted(problems.items(), key=lambda kv: -kv[1]["n"])[:6]
    lines = [f"{rt}.{loc}: {msg} ({v['n']} resources; value shape: {v['shape']})" for (rt, loc, msg), v in top]
    return _fail("fhir.schema", f"{invalid} of {total} resources invalid. " + " | ".join(lines))


def _empties(node, path: str = "") -> list[str]:
    found = []
    if node is None or node == "" or node == [] or node == {}:
        return [path or "<root>"]
    if isinstance(node, dict):
        for k, v in node.items():
            found += _empties(v, f"{path}.{k}" if path else k)
    elif isinstance(node, list):
        for v in node:
            found += _empties(v, f"{path}[]")
    return found


def conventions_check(resources: dict[str, list[dict]], system_id: str, mrn_system: str) -> Check:
    problems: dict[str, int] = {}

    def note(msg: str) -> None:
        problems[msg] = problems.get(msg, 0) + 1

    for rt, items in resources.items():
        seen: set[str] = set()
        for res in items:
            rid = res.get("id")
            if res.get("resourceType") != rt:
                note(f"{rt}: resourceType is wrong or missing")
            if not isinstance(rid, str) or not ID_RX.match(rid):
                note(f"{rt}: id missing or not [A-Za-z0-9-.]{{1,64}}")
            elif not rid.startswith(f"{system_id}-"):
                note(f'{rt}: id does not start with "{system_id}-"')
            if rid in seen:
                note(f"{rt}: duplicate id (records for the same source key were not merged)")
            seen.add(rid)
            if (res.get("meta") or {}).get("source") != f"urn:system:{system_id}":
                note(f'{rt}: meta.source is not "urn:system:{system_id}"')
            if rt == "Patient":
                idents = res.get("identifier") or []
                if not any(isinstance(i, dict) and i.get("system") == mrn_system and i.get("value") for i in idents):
                    note("Patient: no identifier with the MRN system")
            else:
                ident = ((res.get("subject") or {}).get("identifier") or {})
                if ident.get("system") != mrn_system or not ident.get("value"):
                    note(f"{rt}: subject.identifier does not carry the MRN")
            for path in set(_empties(res)):
                note(f"{rt}: empty element at {path}")
    if not problems:
        return _ok("fhir.conventions")
    top = sorted(problems.items(), key=lambda kv: -kv[1])[:8]
    return _fail("fhir.conventions", " | ".join(f"{msg} ({n} resources)" for msg, n in top))


def reconcile_check(resources: dict[str, list[dict]], system: dict, design: dict) -> tuple[Check, dict]:
    counts, problems = {}, []
    for spec in design.get("resources", []):
        rt = spec["resource_type"]
        basis = spec.get("count_basis") or {}
        emitted = len(resources.get(rt, []))
        try:
            expected = count_distinct(system, basis.get("entity", ""), basis.get("distinct_key", ""))
        except ProbeError as exc:
            expected = None
            problems.append(f"{rt}: source count not computable ({exc})")
        counts[rt] = {"emitted": emitted, "expected": expected,
                      "basis": f"distinct {basis.get('distinct_key')} in {basis.get('entity')}"}
        if expected is not None and emitted != expected:
            problems.append(f"{rt}: adapter returned {emitted} resources but the source has {expected} "
                            f"distinct {basis.get('distinct_key')} in {basis.get('entity')}")
    if problems:
        return _fail("reconcile.counts", " | ".join(problems)), counts
    return _ok("reconcile.counts", ", ".join(f"{rt} {c['emitted']}/{c['expected']}" for rt, c in counts.items())), counts


# ------------------------------------------------------------------ entry point

def validate(code: str, system: dict, design: dict, mrn_system: str, workdir: Path) -> dict:
    """Run every check. Returns {"passed", "checks", "counts", "resources"}."""
    workdir.mkdir(parents=True, exist_ok=True)
    adapter_path = workdir / "adapter.py"
    adapter_path.write_text(code, encoding="utf-8")
    checks = static_checks(code, system)
    resources: dict[str, list[dict]] = {}
    counts: dict = {}
    runtime_ids = ["runtime.executes", "runtime.deterministic", "runtime.silent", "contract.coverage",
                   "fhir.schema", "fhir.conventions", "reconcile.counts"]

    if any(c.status == "fail" for c in checks):  # never execute code that failed a safety check
        checks += [Check(i, "skip", "not run: static checks failed") for i in runtime_ids]
    else:
        out, stdio, failure = run_adapter(adapter_path, system)
        if out is None or out["errors"]:
            errors = (out or {}).get("errors") or {"<process>": {"type": "RunnerError", "message": failure,
                                                                  "line_no": None, "line": None}}
            parts = []
            for rt, e in errors.items():
                where = f" at line {e['line_no']} `{e['line']}`" if e.get("line_no") else ""
                parts.append(f"{rt}: {e['type']}: {scrub_error(e['message'], code)}{where}")
            checks.append(_fail("runtime.executes", " | ".join(parts)))
            checks += [Check(i, "skip", "not run: adapter did not complete") for i in runtime_ids[1:]]
        else:
            resources = out["resources"]
            checks.append(_ok("runtime.executes", f"{sum(len(v) for v in resources.values())} resources returned"))
            checks.append(_ok("runtime.deterministic") if out["deterministic"] else
                          _fail("runtime.deterministic", "two consecutive runs returned different output"))
            checks.append(_ok("runtime.silent") if not stdio.strip() else
                          _fail("runtime.silent", f"adapter wrote {len(stdio)} characters to stdout/stderr"))
            wanted = sorted(r["resource_type"] for r in design.get("resources", []))
            got = sorted(out["supported"])
            checks.append(_ok("contract.coverage") if wanted == got else
                          _fail("contract.coverage", f"design specifies {wanted} but SUPPORTED is {got}"))
            checks.append(fhir_schema_check(resources))
            checks.append(conventions_check(resources, system["id"], mrn_system))
            recon, counts = reconcile_check(resources, system, design)
            checks.append(recon)

    (workdir / "resources.json").write_text(json.dumps(resources), encoding="utf-8")
    result = {"passed": all(c.status == "pass" for c in checks), "checks": [c.as_dict() for c in checks],
              "counts": counts}
    (workdir / "validation.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    return {**result, "resources": resources}
