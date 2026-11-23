"""Command line.

    python -m fabric seed [--synthea DIR]     build the four mock source systems from Synthea
    python -m fabric run [--system ID ...]    run the agent pipeline (interactive gates)
    python -m fabric demo                     seed if needed, run every system, build the report
    python -m fabric report [RUN_ID]          rebuild the HTML report for a run
    python -m fabric gateway                  serve the console and unified FHIR API on :8000
    python -m fabric check [--heal]           detect source drift; --heal re-integrates drifted systems
    python -m fabric codes [--offline]        check that clinical codes are real (LOINC, RxNorm, SNOMED CT)
    python -m fabric handbuilt start|check ID time a hand-built adapter (the measured baseline)

Common flags for run/demo:  --auto-approve  --stub  --no-cache  --reviewer NAME
"""
from __future__ import annotations

import argparse
import atexit
import os
import subprocess
import sys
import time

import httpx

from .config import ROOT, RUNS, load_config


def ensure_mocks(cfg: dict) -> None:
    """Start any mock HTTP system that is configured with `autostart` and is not already up."""
    for system in cfg["systems"]:
        if system["kind"] != "http" or not system.get("autostart"):
            continue
        url = system["base_url"].rstrip("/") + "/health"
        try:
            httpx.get(url, timeout=1)
            continue
        except httpx.HTTPError:
            pass
        proc = subprocess.Popen([sys.executable, "-m", system["autostart"]], cwd=ROOT)
        atexit.register(proc.terminate)
        for _ in range(60):
            try:
                httpx.get(url, timeout=1)
                break
            except httpx.HTTPError:
                time.sleep(0.25)
        else:
            raise SystemExit(f"Could not start mock system {system['id']} ({system['autostart']})")
        print(f"Started mock system {system['id']} at {system['base_url']}")


def cmd_seed(args) -> None:
    from mock_systems.build import build_all

    if getattr(args, "synthea", None):
        from mock_systems.synthea import load_synthea, save_estate

        dataset = load_synthea(args.synthea, max_patients=args.patients)
        if getattr(args, "save_estate", False):
            print("Saved as the default estate:", save_estate(dataset))
    else:
        from mock_systems.synthea import load_estate

        dataset = load_estate()
    print("Mock systems built:", build_all(dataset))


def cmd_codes(args) -> None:
    from . import terminology

    cfg = load_config()
    ensure_mocks(cfg)
    term = terminology.Terminology(server=args.server, offline=args.offline)
    print(terminology.describe(terminology.check(cfg, term=term)))


def _llm(args):
    if args.stub:
        from .stub import StubLLM

        return StubLLM()
    from .llm import LLM, LLMError

    try:
        return LLM.from_env(use_cache=not args.no_cache)
    except LLMError as exc:
        raise SystemExit(str(exc)) from exc


def cmd_run(args) -> None:
    from . import pipeline, report

    cfg = load_config()
    if not (ROOT / "mock_systems" / "_data").exists():
        raise SystemExit("Mock systems are not built yet. Run: python -m fabric seed")
    ensure_mocks(cfg)
    run_dir = pipeline.run(cfg, _llm(args), args.system, args.auto_approve, args.reviewer)
    path = report.build(run_dir)
    print(f"\nReport: {path}\nAudit:  {run_dir / 'audit.jsonl'}\nNext:   python -m fabric gateway")


def cmd_demo(args) -> None:
    if not (ROOT / "mock_systems" / "_data").exists():
        args.synthea, args.patients = None, 40
        cmd_seed(args)
    args.system = None
    cmd_run(args)


def cmd_report(args) -> None:
    from . import report

    run_id = args.run_id or (RUNS / "latest.txt").read_text(encoding="utf-8").strip()
    print("Report:", report.build(RUNS / run_id))


def cmd_check(args) -> None:
    from . import operate

    cfg = load_config()
    ensure_mocks(cfg)
    statuses = operate.check(cfg)
    print(operate.describe(statuses))
    drifted = [sid for sid, st in statuses.items() if not st["healthy"]]
    if not drifted:
        return
    if not args.heal:
        print(f"\n{len(drifted)} integration(s) need attention. Re-integrate with: python -m fabric check --heal")
        raise SystemExit(1)
    from . import pipeline, report

    print(f"\nHealing: sending {', '.join(drifted)} back through the agent pipeline (gates included) ...")
    run_dir = pipeline.run(cfg, _llm(args), drifted, args.auto_approve, args.reviewer)
    print(f"\nReport: {report.build(run_dir)}")
    print(operate.describe(operate.check(cfg)))


def cmd_handbuilt(args) -> None:
    from . import handbuilt

    cfg = load_config()
    ensure_mocks(cfg)
    if args.action == "start":
        folder = handbuilt.start(cfg, args.system, args.dev)
        print(f"Clock started. Brief: {folder / 'BRIEF.md'}\nWrite: {folder / 'adapter.py'}\n"
              f"Check: python -m fabric handbuilt check {args.system}")
    else:
        handbuilt.check(cfg, args.system, args.minutes)


def cmd_gateway(args) -> None:
    import uvicorn

    ensure_mocks(load_config())
    if args.no_demo_uploads:
        os.environ["FABRIC_DEMO_UPLOADS"] = "0"   # read by the gateway's upload endpoints
        print("Demo uploads OFF")
    else:
        print("Demo uploads ON: Upload buttons in the Overview topology (switch off with --no-demo-uploads)")
    if args.check_every:
        os.environ["FABRIC_CHECK_EVERY"] = str(args.check_every)  # read by the gateway at startup
        print(f"Automatic estate checks every {args.check_every:g} min (alerts on drift or contract failure)")
    print(f"Console: http://127.0.0.1:{args.port}/console   FHIR API: http://127.0.0.1:{args.port}/docs")
    uvicorn.run("fabric.gateway:app", host=args.host, port=args.port, log_level="warning")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m fabric", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    codes = sub.add_parser("codes", help="check that emitted LOINC / RxNorm / SNOMED CT codes are real (advisory)")
    codes.add_argument("--server", help="FHIR terminology server base URL (default: FABRIC_TX_SERVER or tx.fhir.org)")
    codes.add_argument("--offline", action="store_true", help="use cached results only; no network")
    codes.set_defaults(func=cmd_codes)

    seed = sub.add_parser("seed", help="build the mock source systems")
    seed.add_argument("--synthea", metavar="DIR", help="folder of Synthea FHIR R4 patient bundles to seed from "
                      "(default: the committed Synthea estate)")
    seed.add_argument("--save-estate", action="store_true", help="with --synthea: make this export the default estate")
    seed.add_argument("--patients", type=int, default=40)
    seed.set_defaults(func=cmd_seed)

    for name, func in (("run", cmd_run), ("demo", cmd_demo)):
        p = sub.add_parser(name, help="run the agent pipeline" if name == "run" else "seed, run everything, report")
        if name == "run":
            p.add_argument("--system", action="append", metavar="ID", help="only this system (repeatable)")
        p.add_argument("--auto-approve", action="store_true", help="record automatic approvals instead of asking")
        p.add_argument("--stub", action="store_true", help="no model: use hand-written fixtures (smoke test)")
        p.add_argument("--no-cache", action="store_true", help="ignore and do not write the LLM response cache")
        p.add_argument("--reviewer", default="reviewer", help="name recorded for gate decisions")
        p.set_defaults(func=func)

    chk = sub.add_parser("check", help="detect drift and contract failures in promoted integrations")
    chk.add_argument("--heal", action="store_true", help="send drifted systems back through the pipeline")
    chk.add_argument("--auto-approve", action="store_true")
    chk.add_argument("--stub", action="store_true")
    chk.add_argument("--no-cache", action="store_true")
    chk.add_argument("--reviewer", default="reviewer")
    chk.set_defaults(func=cmd_check)

    hb = sub.add_parser("handbuilt", help="time a hand-built adapter against the same contract (the baseline)")
    hb.add_argument("action", choices=["start", "check"])
    hb.add_argument("system")
    hb.add_argument("--dev", default="developer", help="name recorded for the hand-built run")
    hb.add_argument("--minutes", type=float, help="your own active minutes, if you did not work in one sitting")
    hb.set_defaults(func=cmd_handbuilt)

    rep = sub.add_parser("report", help="rebuild the HTML report")
    rep.add_argument("run_id", nargs="?")
    rep.set_defaults(func=cmd_report)

    gw = sub.add_parser("gateway", help="serve the unified FHIR API")
    gw.add_argument("--port", type=int, default=8000)
    gw.add_argument("--host", default="127.0.0.1", help="interface to bind (0.0.0.0 inside a container)")
    gw.add_argument("--check-every", type=float, metavar="MIN",
                    help="re-check the estate automatically every MIN minutes and alert on changes")
    gw.add_argument("--no-demo-uploads", action="store_true",
                    help="hide the console's demo upload buttons and refuse uploads")
    gw.set_defaults(func=cmd_gateway)

    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):  # model text may contain characters a Windows console cannot print
        sys.stdout.reconfigure(errors="replace")
    args.func(args)


if __name__ == "__main__":
    main()
