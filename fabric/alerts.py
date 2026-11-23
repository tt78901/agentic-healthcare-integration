"""Operate stage: alerts. Turns what the fabric detects into something a person is told about.

Three conditions raise an alert, and each clears itself when the condition ends:

  circuit   a source failed repeatedly and the gateway stopped calling it (critical)
  drift     a source's structure no longer matches what was approved (warning)
  contract  a promoted adapter fails the validation contract (critical)

Alerts are raised on a change of state only, so a source that stays down raises one
alert, not one per request. Every raise and resolve is appended to
generated/alerts.jsonl (the alert history). If FABRIC_ALERT_WEBHOOK is set, each one
is also posted as {"text": ...}, the payload Slack and Microsoft Teams incoming
webhooks accept. Posting happens on a background thread and never blocks or fails a
request. In production this is where Azure Monitor / an on-call tool takes over.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path

import httpx

from .audit import now
from .config import GENERATED

SEVERITY = {"circuit": "critical", "contract": "critical", "drift": "warning"}
TITLES = {"circuit": "Source offline: circuit open", "contract": "Contract failing", "drift": "Source structure drifted"}


class AlertManager:
    def __init__(self, log_path=None, webhook: str | None = None):
        # log_path may be a Path or a zero-argument callable returning one (resolved at each write)
        self._log_path = log_path or (lambda: GENERATED / "alerts.jsonl")
        self.webhook = webhook if webhook is not None else os.environ.get("FABRIC_ALERT_WEBHOOK", "")
        self._lock = threading.Lock()
        self._active: dict[tuple[str, str], dict] = {}
        self._history: list[dict] = []
        self._load()

    @property
    def log_path(self) -> Path:
        return Path(self._log_path() if callable(self._log_path) else self._log_path)

    # ------------------------------------------------------------------ state
    def _load(self) -> None:
        """Rebuild the active set from the history, so alerts survive a restart."""
        if not self.log_path.exists():
            return
        for line in self.log_path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            self._history.append(event)
            key = (event["system"], event["kind"])
            if event["status"] == "open":
                self._active[key] = event
            else:
                self._active.pop(key, None)

    def _write(self, event: dict) -> None:
        self._history.append(event)
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event) + "\n")
        except OSError:
            pass  # the alert is still held in memory and shown in the console
        if self.webhook:
            threading.Thread(target=self._post, args=(event,), daemon=True).start()

    def _post(self, event: dict) -> None:
        icon = "RESOLVED" if event["status"] == "resolved" else event["severity"].upper()
        text = f"[Fabric {icon}] {event['system']}: {event['title']}. {event['message']}"
        try:
            httpx.post(self.webhook, json={"text": text}, timeout=5)
        except httpx.HTTPError:
            pass  # notification is best effort; the alert history is the record

    # ------------------------------------------------------------------ API
    def raise_alert(self, system: str, kind: str, message: str) -> dict | None:
        """Open an alert for (system, kind) unless one is already open. Returns the new alert."""
        with self._lock:
            key = (system, kind)
            if key in self._active:
                return None
            event = {"id": uuid.uuid4().hex[:10], "at": now(), "system": system, "kind": kind,
                     "severity": SEVERITY.get(kind, "warning"), "title": TITLES.get(kind, kind),
                     "message": message, "status": "open"}
            self._active[key] = event
            self._write(event)
            return event

    def resolve(self, system: str, kind: str, message: str) -> dict | None:
        """Close the open alert for (system, kind), if there is one."""
        with self._lock:
            opened = self._active.pop((system, kind), None)
            if opened is None:
                return None
            event = {"id": opened["id"], "at": now(), "system": system, "kind": kind,
                     "severity": opened["severity"], "title": opened["title"], "message": message,
                     "status": "resolved", "opened_at": opened["at"]}
            self._write(event)
            return event

    def from_statuses(self, statuses: dict[str, dict]) -> None:
        """Apply the result of an estate check (fabric.operate.check)."""
        for sid, st in statuses.items():
            drift = st.get("drift")
            if drift:
                parts = [f"removed: {', '.join(drift.get('removed', []))}" if drift.get("removed") else "",
                         f"new: {', '.join(drift.get('added', []))}" if drift.get("added") else "",
                         "changed: " + ", ".join(c["field"] for c in drift.get("changed", [])) if drift.get("changed") else ""]
                self.raise_alert(sid, "drift", "; ".join(p for p in parts if p) + ". Heal: python -m fabric check --heal")
            else:
                self.resolve(sid, "drift", "Source structure matches the approved fingerprint again")
            if st.get("contract") != "pass":
                why = st.get("error") or ", ".join(f["id"] for f in st.get("failed", [])) or "contract not passing"
                self.raise_alert(sid, "contract", why)
            else:
                self.resolve(sid, "contract", "Adapter passes all 14 checks again")

    def active(self) -> list[dict]:
        with self._lock:
            return sorted(self._active.values(), key=lambda e: e["at"], reverse=True)

    def recent(self, limit: int = 50) -> list[dict]:
        with self._lock:
            return list(reversed(self._history[-limit:]))
