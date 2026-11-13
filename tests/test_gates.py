"""Human gates: a rejection sends the reviewer's feedback back to the agent."""
import builtins
import json
import sys

from fabric import pipeline
from fabric.stub import StubLLM


def test_rejection_feedback_reaches_the_agent(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(pipeline, "GENERATED", tmp_path / "generated")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    answers = iter(["STAT code S means suspended, not stopped", "y", "y", "y"])
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(answers))
    llm = StubLLM()
    run_dir = pipeline.run(cfg, llm, ["pharmacy-csv"], auto_approve=False, reviewer="tarun")

    discover_prompts = [u for tag, _, u in llm.prompts if tag.startswith("discover:")]
    assert len(discover_prompts) == 2
    assert "STAT code S means suspended" in discover_prompts[1] and "Your previous answer" in discover_prompts[1]

    summary = json.loads((run_dir / "summary.json").read_text())
    gates = summary["systems"][0]["gates"]
    assert [g["approved"] for g in gates] == [False, True, True, True]
    assert all(g["by"] == "tarun" for g in gates) and summary["systems"][0]["status"] == "promoted"
    assert summary["auto_approved"] is False
