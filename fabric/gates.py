"""Human-in-the-loop gates. Nothing moves to the next stage without a recorded decision.

Interactive by default: the reviewer sees a summary and approves, or rejects with
feedback that is sent back to the agent. With --auto-approve the decision is
recorded as an automatic approval so the audit trail never implies a human looked.
"""
from __future__ import annotations

import sys
import time


class GateRejected(Exception):
    pass


class Gates:
    def __init__(self, auto: bool, audit, reviewer: str = "reviewer"):
        self.auto = auto
        self.audit = audit
        self.reviewer = reviewer
        self.decisions: list[dict] = []

    def ask(self, system_id: str, gate: str, summary: str) -> tuple[bool, str]:
        """Return (approved, feedback)."""
        print(f"\n--- GATE: {gate} [{system_id}] " + "-" * max(1, 50 - len(gate) - len(system_id)))
        print(summary)
        started = time.perf_counter()
        if self.auto:
            approved, feedback, by = True, "", "auto-approve flag (no human review)"
            print("  -> auto-approved (--auto-approve)")
        else:
            if not sys.stdin.isatty():
                raise SystemExit("A human gate needs an interactive terminal. Re-run with --auto-approve "
                                 "to record automatic approvals instead.")
            answer = input("  Approve? [y] yes / or type feedback to reject: ").strip()
            approved = answer.lower() in ("y", "yes", "")
            feedback, by = ("" if approved else answer), self.reviewer
        decision = {"system": system_id, "gate": gate, "approved": approved, "by": by, "feedback": feedback,
                    "seconds": round(time.perf_counter() - started, 1)}
        self.decisions.append(decision)
        self.audit.event("gate", **{k: v for k, v in decision.items() if k != "system"})
        return approved, feedback

    def for_system(self, system_id: str) -> list[dict]:
        return [d for d in self.decisions if d["system"] == system_id]
