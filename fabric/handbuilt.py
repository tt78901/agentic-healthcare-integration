"""The hand-built baseline: a measured comparison instead of an assumed one.

The challenge asks for "the time and consistency gained versus hand-built
integrations". This harness measures the hand-built side. A developer writes
an adapter for one of the systems by hand, against exactly the same contract
and the same checks the agents face:

    python -m fabric handbuilt start pharmacy-csv --dev "Your name"
        ... write baseline/pharmacy-csv/adapter.py ...
    python -m fabric handbuilt check pharmacy-csv        (repeat until it passes)

`start` begins the clock and writes the brief. Every `check` runs the full
validation contract and the ground-truth comparison and logs what failed. The
first passing check stops the clock. The run report then puts the two side by
side: time to a validated adapter, validation runs needed, checks failed on the
first run and value accuracy.

Rules that keep the comparison fair (the report states them too):
  * the developer must not use an AI assistant or read generated/<system>/;
  * the developer gets what the agent gets (the contract and how to reach the
    system) plus one thing the agent does not: full sight of the real data;
  * work in one sitting, or record your own active minutes with --minutes.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from .audit import now
from .config import GENERATED, ROOT, public_view
from .evaluate import evaluate, load_truth, summary_lines
from .standards import adapter_contract
from .validate import validate

BASELINE = ROOT / "baseline"

SKELETON = '''"""Hand-built adapter for {system_id}.

Written by a developer without AI assistance, as the baseline the agent pipeline
is compared with. The contract is in BRIEF.md next to this file.
"""

SYSTEM_ID = "{system_id}"
SUPPORTED = {supported}


def fetch(resource_type, config):
    raise NotImplementedError("write me")
'''


def _system(cfg: dict, system_id: str) -> dict:
    match = [s for s in cfg["systems"] if s["id"] == system_id]
    if not match:
        raise SystemExit(f"Unknown system '{system_id}'. Known: {[s['id'] for s in cfg['systems']]}")
    return match[0]


def _design(system_id: str) -> dict:
    path = GENERATED / system_id / "design.json"
    if not path.exists():
        raise SystemExit(f"No approved design for {system_id} yet. Run the agent pipeline for it first: the "
                         "hand-built adapter is checked against the same approved resource list and record counts.")
    return json.loads(path.read_text(encoding="utf-8"))


def start(cfg: dict, system_id: str, developer: str) -> Path:
    system, design = _system(cfg, system_id), _design(system_id)
    folder = BASELINE / system_id
    if (folder / "log.json").exists():
        raise SystemExit(f"{folder} already exists. Delete it to start again (that resets the clock).")
    folder.mkdir(parents=True)
    supported = [r["resource_type"] for r in design["resources"]]
    counts = "\n".join(f"- {r['resource_type']}: one per distinct {r['count_basis'].get('distinct_key')} in "
                       f"{r['count_basis'].get('entity')} ({r.get('expected_count')} today)" for r in design["resources"])
    brief = (f"# Hand-built baseline: {system.get('name', system_id)}\n\n"
             f"Write `adapter.py` in this folder by hand. No AI assistant, and do not open `generated/{system_id}/`.\n"
             f"You may look at the source system as much as you like (see `systems.yaml` for where it lives).\n"
             f"Check your work with `python -m fabric handbuilt check {system_id}`. The clock started when this "
             f"file was written and stops at the first check that passes.\n\n"
             f"## System\n\n```json\n{json.dumps(public_view(system), indent=1)}\n```\n\n"
             f"## Resources to publish\n\n{counts}\n\n"
             f"## Contract\n\n```\n{adapter_contract(system, cfg['settings']['mrn_system'])}\n```\n")
    (folder / "BRIEF.md").write_text(brief, encoding="utf-8")
    (folder / "adapter.py").write_text(SKELETON.format(system_id=system_id, supported=json.dumps(supported)),
                                       encoding="utf-8")
    log = {"system": system_id, "developer": developer, "started": now(), "finished": None, "minutes": None,
           "minutes_source": None, "runs": []}
    (folder / "log.json").write_text(json.dumps(log, indent=1), encoding="utf-8")
    return folder


def check(cfg: dict, system_id: str, minutes: float | None = None) -> dict:
    system, design = _system(cfg, system_id), _design(system_id)
    folder = BASELINE / system_id
    log_path = folder / "log.json"
    if not log_path.exists():
        raise SystemExit(f"Not started. Run: python -m fabric handbuilt start {system_id} --dev \"Your name\"")
    log = json.loads(log_path.read_text(encoding="utf-8"))
    mrn_system = cfg["settings"]["mrn_system"]
    code = (folder / "adapter.py").read_text(encoding="utf-8")
    result = validate(code, system, design, mrn_system, folder / "_work")
    elapsed = (dt.datetime.fromisoformat(now()) - dt.datetime.fromisoformat(log["started"])).total_seconds() / 60
    failed = [{"id": c["id"], "title": c["title"], "detail": c["detail"]} for c in result["checks"] if c["status"] == "fail"]
    run = {"at": now(), "elapsed_minutes": round(elapsed, 1), "passed": result["passed"], "failed": failed,
           "not_run": [c["id"] for c in result["checks"] if c["status"] == "skip"]}
    truth = load_truth(system)
    evaluation = evaluate(system_id, result["resources"], truth, mrn_system) if truth and result["passed"] else None

    for c in result["checks"]:
        print(f"  {c['status'].upper():4}  {c['title']}" + (f"  ({c['detail'][:220]})" if c["status"] == "fail" else ""))
    if log["finished"]:
        print("\nAlready finished; this run is not added to the log.")
        return log
    log["runs"].append(run)
    if result["passed"]:
        log["finished"] = run["at"]
        log["minutes"] = round(minutes, 1) if minutes else run["elapsed_minutes"]
        log["minutes_source"] = "self-reported active minutes" if minutes else "wall clock from start to first pass"
        log["adapter_lines"] = len(code.splitlines())
        log["evaluation"] = evaluation
        print(f"\nPASSED on validation run {len(log['runs'])} after {log['minutes']} minutes ({log['minutes_source']}).")
        if evaluation:
            print("Values compared with ground truth:")
            for line in summary_lines(evaluation):
                print(f"  {line}")
    else:
        print(f"\nNot passing yet: {len(failed)} check(s) failed on run {len(log['runs'])}, "
              f"{run['elapsed_minutes']} minutes in. Fix and run the check again.")
    log_path.write_text(json.dumps(log, indent=1), encoding="utf-8")
    return log


def load_all() -> dict[str, dict]:
    """Every hand-built log, finished or not, keyed by system id."""
    if not BASELINE.exists():
        return {}
    return {p.parent.name: json.loads(p.read_text(encoding="utf-8")) for p in sorted(BASELINE.glob("*/log.json"))}
