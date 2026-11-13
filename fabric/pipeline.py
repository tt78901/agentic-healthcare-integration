"""Orchestrator: Discover -> Design -> Generate -> Validate (with self-healing) ->
Promote, with a human gate after each agent stage and an audit trail throughout.
"""
from __future__ import annotations

import datetime as dt
import json
import shutil
import time
import traceback
from pathlib import Path

from . import agents
from .audit import Audit, now
from .config import GENERATED, ROOT, RUNS
from .emit import openapi_for, test_file_for
from .evaluate import evaluate, extractors, load_truth, summary_lines
from .gates import Gates
from .llm import LLMError
from .operate import skeleton
from .privacy import Masker
from .probes import ProbeError, count_distinct, probe
from .validate import validate

MAX_STAGE_RETRIES = 2  # times a human (or the count-basis check) can send a stage back


def _salt() -> str:
    """Stable per-checkout masking salt, so masked prompts (and the LLM cache) repeat across runs."""
    path = RUNS / ".mask_salt"
    if not path.exists():
        import secrets

        RUNS.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_hex(16), encoding="utf-8")
    return path.read_text(encoding="utf-8").strip()


class Timer:
    def __init__(self):
        self.stages: dict[str, float] = {}

    def time(self, stage: str):
        timer = self

        class _Ctx:
            def __enter__(self):
                self.t0 = time.perf_counter()

            def __exit__(self, *exc):
                timer.stages[stage] = round(timer.stages.get(stage, 0) + time.perf_counter() - self.t0, 2)

        return _Ctx()


def _d(value) -> dict:
    return value if isinstance(value, dict) else {}


def _l(value) -> list:
    return value if isinstance(value, list) else []


def _profile_summary(p: dict) -> str:
    lines = [f"  {p.get('summary', '')}"]
    for e in _l(p.get("entities")):
        fields = [f for f in _l(_d(e).get("fields")) if isinstance(f, dict)]
        phi = sum(1 for f in fields if f.get("phi"))
        lines.append(f"  * {_d(e).get('name')}  <- {_d(e).get('source')}  ({len(fields)} fields, {phi} PHI; "
                     f"key {_d(e).get('primary_key')}, patient link {_d(e).get('patient_key')})")
    for note in _l(p.get("data_quality_notes"))[:5]:
        lines.append(f"  ! {note}")
    for q in _l(p.get("open_questions"))[:4]:
        lines.append(f"  ? {q}")
    return "\n".join(lines)


def _design_summary(d: dict) -> str:
    sync = d.get("sync")
    lines = [f"  Pattern: {d['pattern']}. {d.get('pattern_rationale', '')}",
             f"  Sync: {_d(sync).get('mode', sync)} ({_d(sync).get('detail', '')})"]
    for r in d["resources"]:
        lines.append(f"  * {r['resource_type']}  <- {r.get('source_entity')}  ({len(r['field_map'])} mapped fields; "
                     f"source records expected: {r.get('expected_count')}; merge: {r.get('merge_rule')})")
        for m in [m for m in r["field_map"] if isinstance(m, dict)][:12]:
            lines.append(f"      {m.get('fhir')}  <-  {m.get('source')}   [{m.get('transform')}]")
    for point in _l(d.get("human_review_points"))[:5]:
        lines.append(f"  ? {point}")
    return "\n".join(lines)


def _validation_summary(v: dict, rounds: int, evaluation: dict | None) -> str:
    lines = [f"  {'PASS' if c['status'] == 'pass' else c['status'].upper():4}  {c['title']}"
             + (f"  ({c['detail']})" if c["detail"] and c["status"] != "pass" else "") for c in v["checks"]]
    lines.append(f"  Repair rounds used: {rounds}")
    if evaluation:
        lines.append("  Evidence: values compared with the synthetic ground truth (the agents never see it)")
        lines += [f"    {line}" for line in summary_lines(evaluation)]
    return "\n".join(lines)


def _attach_expected_counts(system: dict, design: dict) -> list[str]:
    problems = []
    for r in design["resources"]:
        basis = r["count_basis"]
        try:
            r["expected_count"] = count_distinct(system, basis.get("entity", ""), basis["distinct_key"])
        except ProbeError as exc:
            r["expected_count"] = None
            problems.append(f"count_basis for {r['resource_type']} cannot be computed: {exc}")
    return problems


def _mrns(resources: dict[str, list[dict]], mrn_system: str) -> set[str]:
    getters = extractors(mrn_system)
    return {m for rt, items in resources.items() for m in (getters[rt]["mrn"](r) for r in items) if m}


def integrate_system(system: dict, settings: dict, llm, gates: Gates, audit: Audit, run_dir: Path,
                     masker: Masker) -> dict:
    sid = system["id"]
    audit.system = sid
    work = run_dir / sid
    work.mkdir(parents=True, exist_ok=True)
    timer = Timer()
    mrn_system = settings["mrn_system"]
    max_repairs = settings.get("max_repairs", 3)
    record = {"id": sid, "name": system.get("name", sid), "kind": system["kind"], "status": "failed",
              "attempts": [], "repair_rounds": 0, "review_rounds": 0}
    print(f"\n================ {sid}: {record['name']} ================")

    def save(name: str, data) -> None:
        text = data if isinstance(data, str) else json.dumps(data, indent=1, default=str)
        (work / name).write_text(text, encoding="utf-8")

    try:
        # ---- 1. Discover -------------------------------------------------------
        print("[1/4] Discover: probing the system (read-only) ...")
        with timer.time("discover_tools"):
            probe_out = probe(system, masker)
        save("probe.json", probe_out)
        audit.event("probe", system_kind=system["kind"], chars=len(json.dumps(probe_out, default=str)))
        profile, feedback = None, None
        for _ in range(MAX_STAGE_RETRIES + 1):
            print("      Discover agent is writing the system profile ...")
            with timer.time("discover_agent"):
                profile = agents.discover(llm, system, probe_out, feedback, profile)
            save("profile.json", profile)
            approved, feedback = gates.ask(sid, "system profile", _profile_summary(profile))
            if approved:
                break
        else:
            record["status"] = "rejected at profile gate"
            return record

        # ---- 2. Design ---------------------------------------------------------
        design, feedback, problems = None, None, []
        for _ in range(MAX_STAGE_RETRIES + 1):
            print("[2/4] Design agent is choosing the pattern and FHIR mapping ...")
            with timer.time("design_agent"):
                design = agents.design(llm, system, profile, mrn_system, feedback, design)
            with timer.time("design_tools"):
                problems = _attach_expected_counts(system, design)
            if problems:  # the agent proposed a count basis the tools cannot measure: send it back
                audit.event("design_check_failed", problems=problems)
                feedback = "; ".join(problems) + ". Use exact names from the discovery output."
                continue
            save("design.json", design)
            save("openapi.yaml", openapi_for(system, design))
            approved, feedback = gates.ask(sid, "integration design", _design_summary(design))
            if approved:
                break
        else:
            record["status"] = ("stopped: " + "; ".join(problems)) if problems else "rejected at design gate"
            return record
        record.update(pattern=design["pattern"], pattern_rationale=design.get("pattern_rationale", ""),
                      sync=design.get("sync"), open_questions=_l(profile.get("open_questions")),
                      human_review_points=_l(design.get("human_review_points")))

        # ---- 3. Generate, 4. Validate with self-healing, then the promotion gate
        print("[3/4] Generate agent is writing the adapter ...")
        with timer.time("generate_agent"):
            code = agents.generate(llm, system, profile, design, probe_out, mrn_system)
        attempt_no = 0
        while True:
            result = None
            for round_in_loop in range(max_repairs + 1):
                attempt_no += 1
                print(f"[4/4] Validate: running the contract (attempt {attempt_no}) ...")
                with timer.time("validate_tools"):
                    result = validate(code, system, design, mrn_system, work / f"attempt_{attempt_no}")
                failures = [c for c in result["checks"] if c["status"] == "fail"]
                record["attempts"].append({"attempt": attempt_no, "passed": result["passed"], "failed": [
                    {"id": f["id"], "title": f["title"], "detail": f["detail"]} for f in failures]})
                audit.event("validation", attempt=attempt_no, passed=result["passed"],
                            failed=[f["id"] for f in failures])
                for f in failures:
                    print(f"      FAIL {f['id']}: {f['detail'][:200]}")
                if result["passed"] or round_in_loop == max_repairs:
                    break
                print(f"      Self-healing: sending {len(failures)} failed check(s) back to the Generate agent ...")
                record["repair_rounds"] += 1
                with timer.time("repair_agent"):
                    code = agents.repair(llm, system, profile, design, probe_out, mrn_system, code, failures,
                                         record["repair_rounds"])

            save("adapter.py", code)
            record.update(checks=result["checks"], counts=result["counts"], adapter_lines=len(code.splitlines()))
            if not result["passed"]:
                record["status"] = "failed validation (needs an engineer)"
                print("      Not promoted: validation still failing after the repair budget.")
                return record

            truth = load_truth(system)
            evaluation = evaluate(sid, result["resources"], truth, mrn_system) if truth else None
            if evaluation:
                save("evaluation.json", evaluation)
                record["evaluation"] = evaluation
                audit.event("evaluation", accuracy=evaluation["accuracy"], coverage=evaluation["coverage"],
                            clean=evaluation["clean"])

            approved, feedback = gates.ask(sid, "promote adapter",
                                           _validation_summary(result, record["repair_rounds"], evaluation))
            if approved:
                break
            record["review_rounds"] += 1
            if record["review_rounds"] > MAX_STAGE_RETRIES:
                record["status"] = "rejected at promotion gate"
                return record
            print("      Reviewer feedback goes back to the Generate agent ...")
            record["repair_rounds"] += 1
            review = [{"id": "human.review", "title": "A human reviewer rejected the adapter", "detail": feedback}]
            with timer.time("repair_agent"):
                code = agents.repair(llm, system, profile, design, probe_out, mrn_system, code, review,
                                     record["repair_rounds"])

        # ---- Promote -----------------------------------------------------------
        target = GENERATED / sid
        target.mkdir(parents=True, exist_ok=True)
        for name in ("adapter.py", "design.json", "profile.json", "openapi.yaml"):
            shutil.copy(work / name, target / name)
        test_name, test_code = test_file_for(sid)
        (target / test_name).write_text(test_code, encoding="utf-8")
        (target / "fingerprint.json").write_text(json.dumps(skeleton(probe_out), indent=1), encoding="utf-8")
        record["status"] = "promoted"
        record["_mrns"] = _mrns(result["resources"], mrn_system)
        audit.event("promoted", path=str(target))
        print(f"      Promoted to generated/{sid}/")
        return record
    except (agents.AgentError, ProbeError, LLMError) as exc:
        record["status"] = f"stopped: {exc}"
        audit.event("stopped", reason=str(exc))
        print(f"      Stopped: {exc}")
        return record
    except Exception as exc:  # noqa: BLE001  (finish the run and the report; show the traceback)
        traceback.print_exc()
        record["status"] = f"stopped: unexpected {type(exc).__name__}"
        audit.event("stopped", reason=f"unexpected {type(exc).__name__}")
        return record
    finally:
        calls = audit.calls_for(sid)
        record["stage_seconds"] = timer.stages
        record["llm"] = {"calls": len(calls), "from_cache": sum(1 for c in calls if c["cached"]),
                         "prompt_tokens": sum(c["prompt_tokens"] for c in calls),
                         "completion_tokens": sum(c["completion_tokens"] for c in calls),
                         "model_seconds": round(sum(c["ms"] for c in calls) / 1000, 1)}
        record["gates"] = gates.for_system(sid)
        record["tool_seconds"] = round(sum(v for k, v in timer.stages.items() if k.endswith("_tools")), 2)
        record["human_seconds"] = round(sum(g["seconds"] for g in record["gates"]), 1)


def _estate_map(records: list[dict], mrn_system: str) -> dict:
    """Dependencies across the estate: which systems publish what, and how they join on the shared key."""
    promoted = [r for r in records if r["status"] == "promoted"]
    master = next((r for r in promoted if "Patient" in r.get("counts", {})), None)
    master_mrns = master["_mrns"] if master else set()
    systems = {}
    for r in promoted:
        mrns = r["_mrns"]
        systems[r["id"]] = {"publishes": sorted(r.get("counts", {})), "patients": len(mrns),
                            "also_in_master": len(mrns & master_mrns), "not_in_master": len(mrns - master_mrns)}
    return {"shared_key": f"MRN ({mrn_system})", "master": master["id"] if master else None, "systems": systems}


def run(cfg: dict, llm, system_ids: list[str] | None, auto_approve: bool, reviewer: str = "reviewer") -> Path:
    run_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = RUNS / run_id
    audit = Audit(run_dir)
    llm.audit = audit
    gates = Gates(auto_approve, audit, reviewer)
    masker = Masker(_salt())
    systems = [s for s in cfg["systems"] if not system_ids or s["id"] in system_ids]
    if not systems:
        raise SystemExit(f"No system matches {system_ids}. Known: {[s['id'] for s in cfg['systems']]}")
    source_file = ROOT / "mock_systems" / "_data" / "SOURCE.txt"
    summary = {"run_id": run_id, "started": now(), "model": llm.describe(), "stub": llm.is_stub,
               "auto_approved": auto_approve, "settings": cfg["settings"],
               "data_source": source_file.read_text(encoding="utf-8").strip() if source_file.exists() else "unknown",
               "systems": []}
    audit.event("run_started", model=summary["model"], stub=llm.is_stub, systems=[s["id"] for s in systems])
    t0 = time.perf_counter()
    for system in systems:
        summary["systems"].append(integrate_system(system, cfg["settings"], llm, gates, audit, run_dir, masker))
    audit.system = None
    summary["estate"] = _estate_map(summary["systems"], cfg["settings"]["mrn_system"])
    for record in summary["systems"]:
        record.pop("_mrns", None)
    summary["finished"] = now()
    summary["wall_seconds"] = round(time.perf_counter() - t0, 1)
    audit.event("run_finished", promoted=[s["id"] for s in summary["systems"] if s["status"] == "promoted"])
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")
    _update_registry(summary)
    (RUNS / "latest.txt").write_text(run_id, encoding="utf-8")
    return run_dir


def _update_registry(summary: dict) -> None:
    path = GENERATED / "registry.json"
    registry = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    for s in summary["systems"]:
        if s["status"] == "promoted":
            ev = s.get("evaluation") or {}
            registry[s["id"]] = {"run_id": summary["run_id"], "promoted_at": summary["finished"],
                                 "model": summary["model"], "stub": summary["stub"], "pattern": s.get("pattern"),
                                 "resource_types": sorted(s.get("counts", {}).keys()),
                                 "counts": {rt: c["emitted"] for rt, c in s.get("counts", {}).items()},
                                 "repair_rounds": s["repair_rounds"],
                                 "accuracy": ev.get("accuracy"), "coverage": ev.get("coverage"),
                                 "approved_by": (s.get("gates") or [{}])[-1].get("by")}
    GENERATED.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(registry, indent=1), encoding="utf-8")
