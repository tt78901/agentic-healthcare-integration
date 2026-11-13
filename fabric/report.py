"""Run report: a self-contained HTML page built from runs/<run_id>/summary.json.

Everything on it is measured in that run, except the hand-built baseline, which
is an assumption from systems.yaml and is labelled as one.
"""
from __future__ import annotations

import html
import json
from pathlib import Path

from . import handbuilt
from .validate import CHECK_TITLES

CSS = """
:root{--ink:#16202e;--muted:#5b6777;--line:#dfe4ea;--bg:#f6f7f9;--card:#fff;--ok:#1a7f4b;--bad:#b3261e;--warn:#8a5a00;--accent:#1f4e79}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:1080px;margin:0 auto;padding:32px 24px 64px}h1{font-size:26px;margin:0 0 4px}h2{font-size:18px;margin:36px 0 10px}
.meta{color:var(--muted);font-size:13.5px}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:18px 20px;margin:14px 0}
table{border-collapse:collapse;width:100%;font-size:14px}th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);font-weight:600}tr:last-child td{border-bottom:0}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}.ok{color:var(--ok);font-weight:600}.bad{color:var(--bad);font-weight:600}
.skip{color:var(--muted)}.banner{background:#fff4e5;border:1px solid #f0c98a;color:var(--warn);border-radius:10px;padding:12px 16px;margin:16px 0;font-weight:600}
.note{background:#eef3f8;border-left:4px solid var(--accent);padding:10px 14px;border-radius:4px;margin:10px 0;font-size:14px}
code{font:13px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:#eef0f3;padding:1px 5px;border-radius:4px}
ul{margin:6px 0 0;padding-left:20px}li{margin:3px 0}.small{font-size:13px;color:var(--muted)}.scroll{overflow-x:auto}
"""


def _e(value) -> str:
    return html.escape(str(value if value is not None else ""))


def _pct(value) -> str:
    return "–" if value is None else f"{value * 100:.1f}%"


def _fmt_seconds(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.1f} s"
    return f"{seconds / 60:.1f} min"


def build(run_dir: Path) -> Path:
    s = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    systems = s["systems"]
    promoted = [x for x in systems if x["status"] == "promoted"]
    resources = sum(c["emitted"] for x in promoted for c in x.get("counts", {}).values())
    model_s = sum(x["llm"]["model_seconds"] for x in systems)
    tool_s = sum(x["tool_seconds"] for x in systems)
    human_s = sum(x["human_seconds"] for x in systems)
    calls = sum(x["llm"]["calls"] for x in systems)
    cached = sum(x["llm"]["from_cache"] for x in systems)
    tokens = sum(x["llm"]["prompt_tokens"] + x["llm"]["completion_tokens"] for x in systems)
    baseline_h = s["settings"].get("baseline_hours_per_integration")

    out = [f"<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
           f"<title>Integration Fabric run {_e(s['run_id'])}</title><style>{CSS}</style></head><body><main>",
           "<h1>Agentic healthcare integration: run report</h1>",
           f"<div class='meta'>Run {_e(s['run_id'])} · started {_e(s['started'])} · model: {_e(s['model'])} · "
           f"data: {_e(s['data_source'])} (synthetic) · gates: "
           f"{'auto-approved, no human review' if s['auto_approved'] else 'reviewed by a human'}</div>"]
    if s["stub"]:
        out.append("<div class='banner'>STUB RUN. No model was called. Profiles, designs and adapters in this run are "
                   "hand-written test fixtures, and two of them start deliberately broken to exercise the repair "
                   "loop. This run shows that the pipeline works, not what an agent produced.</div>")

    out.append("<div class='card'><table><tr><th>Systems integrated</th><th>FHIR resources served</th>"
               "<th>Repair rounds</th><th>Model calls</th><th>Tokens</th><th>Wall clock</th></tr><tr>"
               f"<td>{len(promoted)} of {len(systems)}</td><td>{resources}</td>"
               f"<td>{sum(x['repair_rounds'] for x in systems)}</td>"
               f"<td>{calls}{f' ({cached} from cache)' if cached else ''}</td><td>{tokens:,}</td>"
               f"<td>{_fmt_seconds(s['wall_seconds'])}</td></tr></table></div>")

    # ---- per system
    out.append("<h2>Systems</h2><div class='card scroll'><table><tr><th>System</th><th>Pattern chosen</th><th>Resources (emitted / in source)</th>"
               "<th class='num'>Repairs</th><th class='num'>Checks</th><th class='num'>Values correct</th><th class='num'>Model</th><th class='num'>Tools</th>"
               "<th class='num'>Human</th><th>Status</th></tr>")
    for x in systems:
        counts = "<br>".join(f"{_e(rt)} {c['emitted']} / {_e(c['expected'])}" for rt, c in x.get("counts", {}).items()) or "–"
        checks = x.get("checks", [])
        passed = sum(1 for c in checks if c["status"] == "pass")
        cls = "ok" if x["status"] == "promoted" else "bad"
        out.append(f"<tr><td><strong>{_e(x['name'])}</strong><br><span class='small'>{_e(x['id'])} · {_e(x['kind'])}</span></td>"
                   f"<td>{_e(x.get('pattern', '–'))}</td><td>{counts}</td><td class='num'>{x['repair_rounds']}</td>"
                   f"<td class='num'>{passed}/{len(checks)}</td><td class='num'>{_pct((x.get('evaluation') or {}).get('accuracy'))}</td>"
                   f"<td class='num'>{_fmt_seconds(x['llm']['model_seconds'])}</td>"
                   f"<td class='num'>{_fmt_seconds(x['tool_seconds'])}</td><td class='num'>{_fmt_seconds(x['human_seconds'])}</td>"
                   f"<td class='{cls}'>{_e(x['status'])}</td></tr>")
    out.append("</table></div>")
    for x in systems:
        if x.get("pattern_rationale"):
            out.append(f"<div class='note'><strong>{_e(x['id'])}: why {_e(x['pattern'])}.</strong> {_e(x['pattern_rationale'])}</div>")

    # ---- time
    out.append("<h2>Time and consistency against hand-built integrations</h2><div class='card'>")
    if s["stub"] or model_s == 0:
        out.append("<p>No model was called in this run, so there is no agent time to report. Tool time (probes and "
                   f"validation) was {_fmt_seconds(tool_s)}.</p>")
    else:
        total = model_s + tool_s + human_s
        out.append(f"<p><strong>Agent pipeline, measured in this run:</strong> {_fmt_seconds(total)} of work for "
                   f"{len(systems)} systems: {_fmt_seconds(model_s)} model time, {_fmt_seconds(tool_s)} probes and "
                   f"validation, {_fmt_seconds(human_s)} human review at the gates.</p>")
        if s["auto_approved"]:
            out.append("<p class='small'>Gates were auto-approved, so human review time is not included.</p>")
        if cached:
            out.append(f"<p class='small'>{cached} of {calls} model calls were replayed from the local cache; their "
                       "originally measured latency is used.</p>")
    hand = {k: v for k, v in handbuilt.load_all().items() if v.get("finished")}
    if hand:
        out.append("<table><tr><th>System</th><th>Built by</th><th class='num'>Time to a validated adapter</th>"
                   "<th class='num'>Validation runs</th><th class='num'>Checks failed on run 1</th>"
                   "<th class='num'>Values correct</th><th class='num'>Coverage</th></tr>")
        by_id = {x["id"]: x for x in systems}
        for sid, log in hand.items():
            agent = by_id.get(sid)
            if agent and agent["status"] == "promoted" and not s["stub"]:
                a_time = agent["llm"]["model_seconds"] + agent["tool_seconds"] + agent["human_seconds"]
                a_first = len(agent["attempts"][0]["failed"]) if agent["attempts"] else 0
                ev = agent.get("evaluation") or {}
                out.append(f"<tr><td>{_e(sid)}</td><td>Agent pipeline</td><td class='num'>{_fmt_seconds(a_time)}</td>"
                           f"<td class='num'>{len(agent['attempts'])}</td><td class='num'>{a_first}</td>"
                           f"<td class='num'>{_pct(ev.get('accuracy'))}</td><td class='num'>{_pct(ev.get('coverage'))}</td></tr>")
            hev = log.get("evaluation") or {}
            first = log["runs"][0] if log["runs"] else {"failed": [], "not_run": []}
            out.append(f"<tr><td>{_e(sid)}</td><td>Hand-built ({_e(log['developer'])})</td>"
                       f"<td class='num'>{log['minutes']} min</td><td class='num'>{len(log['runs'])}</td>"
                       f"<td class='num'>{len(first['failed'])}</td><td class='num'>{_pct(hev.get('accuracy'))}</td>"
                       f"<td class='num'>{_pct(hev.get('coverage'))}</td></tr>")
        out.append("</table><p class='small'>Hand-built adapters were written without AI assistance against the same "
                   "contract and checked by the same 14 checks and the same ground truth. Time is "
                   + _e("; ".join(sorted({log['minutes_source'] for log in hand.values()})))
                   + ". The developer could see the real data; the agents saw only masked samples.</p>")
    elif baseline_h:
        out.append(f"<p><strong>No hand-built baseline measured yet.</strong> The placeholder is {baseline_h} hours per "
                   "integration (an assumption, not measured). Measure one with "
                   "<code>python -m fabric handbuilt start &lt;system&gt;</code>.</p>")
    out.append("</div>")

    # ---- correctness
    out.append("<h2>Correctness: every value checked against ground truth</h2><div class='card scroll'>"
               "<p class='small'>The validation contract cannot catch a mapping that is valid FHIR but wrong (day and "
               "month swapped, a status mapped to the wrong code). Because the estate is synthetic, every emitted value "
               "is compared with the known right answer. The agents never see this comparison.</p>"
               "<table><tr><th>System</th><th class='num'>Records matched</th><th class='num'>Values correct</th>"
               "<th class='num'>Coverage</th><th>Fields with errors</th></tr>")
    for x in systems:
        ev = x.get("evaluation")
        if not ev:
            continue
        r = ev["records"]
        bad = [f"{_e(k)}: {t['wrong']} wrong, {t['absent']} not mapped" for k, t in ev["fields"].items()
               if t["wrong"] or t["absent"]]
        out.append(f"<tr><td>{_e(x['id'])}</td><td class='num'>{r['matched']}/{r['expected']}</td>"
                   f"<td class='num'>{ev['values_correct']:,} ({_pct(ev['accuracy'])})</td>"
                   f"<td class='num'>{_pct(ev['coverage'])}</td><td>{'<br>'.join(bad) or '<span class=ok>none</span>'}</td></tr>")
    out.append("</table></div>")

    # ---- estate
    estate = s.get("estate") or {}
    if estate.get("systems"):
        out.append(f"<h2>Estate map: how the systems join</h2><div class='card scroll'><p class='small'>Shared key: "
                   f"{_e(estate['shared_key'])}. Patient master: {_e(estate.get('master'))}.</p><table><tr><th>System</th>"
                   "<th>Publishes</th><th class='num'>Patients</th><th class='num'>Linked to master</th>"
                   "<th class='num'>Not in master</th></tr>")
        for sid, e in estate["systems"].items():
            cls = "bad" if e["not_in_master"] else ""
            out.append(f"<tr><td>{_e(sid)}</td><td>{_e(', '.join(e['publishes']))}</td><td class='num'>{e['patients']}</td>"
                       f"<td class='num'>{e['also_in_master']}</td><td class='num {cls}'>{e['not_in_master']}</td></tr>")
        out.append("</table></div>")

    # ---- consistency
    out.append("<h2>Consistency: one contract for every adapter</h2><div class='card scroll'><table><tr><th>Check</th>"
               + "".join(f"<th>{_e(x['id'])}</th>" for x in systems) + "</tr>")
    for cid, title in CHECK_TITLES.items():
        row = f"<tr><td>{_e(title)}<br><span class='small'>{_e(cid)}</span></td>"
        for x in systems:
            status = next((c["status"] for c in x.get("checks", []) if c["id"] == cid), None)
            row += {"pass": "<td class='ok'>pass</td>", "fail": "<td class='bad'>FAIL</td>",
                    "skip": "<td class='skip'>not run</td>"}.get(status, "<td class='skip'>–</td>")
        out.append(row + "</tr>")
    out.append("</table></div>")

    # ---- self-healing
    out.append("<h2>Self-healing log</h2>")
    healed = False
    for x in systems:
        failed_attempts = [a for a in x.get("attempts", []) if not a["passed"]]
        if not failed_attempts:
            continue
        healed = True
        out.append(f"<div class='card'><strong>{_e(x['id'])}</strong>: {len(x.get('attempts', []))} validation attempt(s), "
                   f"final status <span class='{'ok' if x['status'] == 'promoted' else 'bad'}'>{_e(x['status'])}</span><ul>")
        for a in failed_attempts:
            for f in a["failed"]:
                out.append(f"<li>Attempt {a['attempt']}: <strong>{_e(f['title'])}</strong> failed. {_e(f['detail'])}</li>")
        out.append("</ul></div>")
    if not healed:
        out.append("<div class='card'>Every adapter passed validation on its first attempt.</div>")

    # ---- what the agents flagged for humans
    out.append("<h2>Flagged for human review</h2>")
    for x in systems:
        points = (x.get("human_review_points") or []) + (x.get("open_questions") or [])
        if points:
            out.append(f"<div class='card'><strong>{_e(x['id'])}</strong><ul>"
                       + "".join(f"<li>{_e(p)}</li>" for p in points) + "</ul></div>")

    out.append(f"<p class='small'>Audit trail: <code>runs/{_e(s['run_id'])}/audit.jsonl</code> (every model call, tool run, "
               "validation result and gate decision). Artefacts per system: probe.json, profile.json, design.json, "
               "openapi.yaml, adapter.py and each validation attempt.</p></main></body></html>")
    path = run_dir / "report.html"
    path.write_text("\n".join(out), encoding="utf-8")
    return path
