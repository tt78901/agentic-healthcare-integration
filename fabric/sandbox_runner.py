"""Runs one generated adapter in a separate Python process.

Invoked by fabric.validate as:  python -I sandbox_runner.py <adapter.py> <config.json> <out.json>

Standalone on purpose (imports nothing from the fabric). The adapter's stdout and
stderr are captured by the parent: an adapter that prints anything fails the
"silent" check, because printed records are a PHI leak.

This is process isolation with a timeout, plus the static checks that run before
it. It is not a security boundary: a production fabric would run adapters in a
container with no network route except to the source system.
"""
import importlib.util
import json
import sys
import traceback


def main(adapter_path: str, config_path: str, out_path: str) -> None:
    out = {"system_id": None, "supported": None, "resources": {}, "errors": {}, "deterministic": True}
    try:
        spec = importlib.util.spec_from_file_location("generated_adapter", adapter_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        out["system_id"] = getattr(module, "SYSTEM_ID", None)
        out["supported"] = list(getattr(module, "SUPPORTED", []) or [])
        with open(config_path, "r", encoding="utf-8") as fh:
            config = json.load(fh)
        for rt in out["supported"]:
            try:
                first = module.fetch(rt, dict(config))
                second = module.fetch(rt, dict(config))
                if not isinstance(first, list) or not all(isinstance(r, dict) for r in first):
                    raise TypeError("fetch() must return a list of dicts")
                text = json.dumps(first)
                if text != json.dumps(second):
                    out["deterministic"] = False
                out["resources"][rt] = json.loads(text)
            except Exception as exc:  # noqa: BLE001
                out["errors"][rt] = _describe(exc, adapter_path)
    except Exception as exc:  # noqa: BLE001
        out["errors"]["<module>"] = _describe(exc, adapter_path)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(out, fh)


def _describe(exc: Exception, adapter_path: str) -> dict:
    """Exception type, message and the adapter line that raised. The parent scrubs the message."""
    line_no, line = None, None
    for frame in reversed(traceback.extract_tb(exc.__traceback__)):
        if frame.filename == adapter_path:
            line_no, line = frame.lineno, frame.line
            break
    if isinstance(exc, SyntaxError):
        line_no, line = exc.lineno, (exc.text or "").strip()
    return {"type": type(exc).__name__, "message": str(exc), "line_no": line_no, "line": line}


if __name__ == "__main__":
    main(*sys.argv[1:4])
