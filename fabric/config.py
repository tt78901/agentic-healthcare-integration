"""Configuration: systems.yaml plus a minimal .env loader (no extra dependency)."""
from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
GENERATED = ROOT / "generated"
RUNS = ROOT / "runs"


def load_env(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _expand(value):
    """Expand ${VAR} and ${VAR:-default} so secrets stay out of systems.yaml."""
    if isinstance(value, str):
        return re.sub(r"\$\{(\w+)(?::-([^}]*))?\}", lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def load_config(path: Path = ROOT / "systems.yaml") -> dict:
    load_env()
    cfg = _expand(yaml.safe_load(path.read_text(encoding="utf-8")))
    for system in cfg["systems"]:
        if not re.fullmatch(r"[A-Za-z0-9\-.]{1,24}", system["id"]):  # ids prefix FHIR resource ids
            raise SystemExit(f"systems.yaml: id '{system['id']}' must be 1-24 characters of A-Z a-z 0-9 '-' '.'")
        if "path" in system and not Path(system["path"]).is_absolute():
            system["path"] = str(ROOT / system["path"])
    return cfg


def secrets_of(system: dict) -> list[str]:
    """Secret values the generated code must never contain."""
    return [v for v in (system.get("headers") or {}).values() if v]


def public_view(system: dict) -> dict:
    """What an agent may see about a system's connection: never the secret values."""
    view = {k: v for k, v in system.items() if k not in ("headers", "autostart", "path", "truth")}
    if system.get("headers"):
        view["headers"] = {k: "<secret supplied at runtime in config['headers']>" for k in system["headers"]}
    if "path" in system:
        view["path"] = "<supplied at runtime in config['path']>"
    return view
