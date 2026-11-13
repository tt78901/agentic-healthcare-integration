"""Append-only audit trail. Every agent call, tool run, validation result and human
decision in a run is written to runs/<run_id>/audit.jsonl."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class Audit:
    def __init__(self, run_dir: Path):
        self.path = run_dir / "audit.jsonl"
        self.system: str | None = None
        self.llm_calls: list[dict] = []
        run_dir.mkdir(parents=True, exist_ok=True)

    def event(self, kind: str, **data) -> None:
        record = {"ts": now(), "system": self.system, "event": kind, **data}
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")

    def llm_call(self, tag: str, result, prompt_sha: str, prompt_chars: int) -> None:
        entry = {"tag": tag, "model": result.model, "prompt_sha256_16": prompt_sha, "prompt_chars": prompt_chars,
                 "prompt_tokens": result.prompt_tokens, "completion_tokens": result.completion_tokens,
                 "ms": result.ms, "cached": result.cached, "system": self.system}
        self.llm_calls.append(entry)
        self.event("llm_call", **{k: v for k, v in entry.items() if k != "system"})

    def calls_for(self, system_id: str) -> list[dict]:
        return [c for c in self.llm_calls if c["system"] == system_id]
