"""Stub "LLM" for smoke tests and CI. It calls no model.

It returns hand-written fixtures from fabric/stub/fixtures/<system>/ so the whole
pipeline (probes, masking, gates, validation, self-healing, promotion, gateway,
report) can be exercised without an API key. Two fixtures deliberately start
with a broken first attempt (adapter_first_attempt.py) so the repair loop runs.

A stub run proves the plumbing, not the agents. Its outputs are labelled STUB in
the audit trail, the registry and the report, and must not be presented as
model output.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from ..llm import LLMError, LLMResult

FIXTURES = Path(__file__).parent / "fixtures"


class StubLLM:
    is_stub = True
    model = "stub"

    def __init__(self):
        self.audit = None
        self.prompts: list[tuple[str, str, str]] = []  # (tag, system prompt, user prompt), for tests

    def describe(self) -> str:
        return "STUB (no model called; hand-written fixtures)"

    def complete(self, *, system: str, user: str, tag: str, json_mode: bool = False) -> LLMResult:
        self.prompts.append((tag, system, user))
        stage, _, rest = tag.partition(":")
        folder = FIXTURES / rest.split(":")[0]
        if not folder.is_dir():
            raise LLMError(f"the stub has no fixtures for system '{rest.split(':')[0]}'; use a real model")
        if stage == "discover":
            text = (folder / "profile.json").read_text(encoding="utf-8")
        elif stage == "design":
            text = (folder / "design.json").read_text(encoding="utf-8")
        else:
            first = folder / "adapter_first_attempt.py"
            source = first if stage == "generate" and first.exists() else folder / "adapter.py"
            text = "```python\n" + source.read_text(encoding="utf-8") + "```"
        result = LLMResult(text, "stub", 0, 0, 0, cached=False)
        if self.audit:
            sha = hashlib.sha256((system + user).encode()).hexdigest()[:16]
            self.audit.llm_call(tag, result, prompt_sha=sha, prompt_chars=len(system) + len(user))
        return result
