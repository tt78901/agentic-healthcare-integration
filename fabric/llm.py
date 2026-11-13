"""LLM access for the agents: any OpenAI-compatible chat-completions endpoint.

Set three environment variables (or put them in .env):

    LLM_BASE_URL   e.g. https://<resource>.openai.azure.com/openai/v1/   (Azure AI Foundry)
                        http://localhost:11434/v1                        (Ollama, local)
    LLM_API_KEY    the key for that endpoint (any non-empty string for Ollama)
    LLM_MODEL      model or deployment name

Responses are cached on disk by prompt hash, so a completed run can be replayed
offline (useful for a live demo) and re-runs cost nothing. The cache stores the
model's real output; nothing is fabricated.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from .config import ROOT

CACHE_DIR = ROOT / ".cache" / "llm"


@dataclass
class LLMResult:
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    ms: int = 0
    cached: bool = False


class LLMError(RuntimeError):
    pass


class LLM:
    is_stub = False

    def __init__(self, base_url: str, api_key: str, model: str, use_cache: bool = True, audit=None):
        from openai import OpenAI

        self.model = model
        self.base_url = base_url
        self.use_cache = use_cache
        self.audit = audit
        self._client = OpenAI(base_url=base_url, api_key=api_key, max_retries=4, timeout=180)
        self._drop: set[str] = set()  # request options this endpoint has rejected

    @classmethod
    def from_env(cls, use_cache: bool = True, audit=None) -> "LLM":
        missing = [k for k in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL") if not os.environ.get(k)]
        if missing:
            raise LLMError("Missing " + ", ".join(missing) + ". Copy .env.example to .env and fill it in, "
                           "or run with --stub to smoke-test the pipeline without a model.")
        return cls(os.environ["LLM_BASE_URL"], os.environ["LLM_API_KEY"], os.environ["LLM_MODEL"], use_cache, audit)

    def describe(self) -> str:
        host = re.sub(r"^https?://", "", self.base_url).split("/")[0]
        return f"{self.model} via {host}"

    def complete(self, *, system: str, user: str, tag: str, json_mode: bool = False) -> LLMResult:
        key = hashlib.sha256(json.dumps([self.model, system, user, json_mode]).encode()).hexdigest()
        path = CACHE_DIR / f"{key}.json"
        if self.use_cache and path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            result = LLMResult(data["text"], data["model"], data["prompt_tokens"], data["completion_tokens"],
                               data["ms"], cached=True)
        else:
            result = self._call(system, user, json_mode)
            if self.use_cache:
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({**result.__dict__, "tag": tag}, indent=1), encoding="utf-8")
        if self.audit:
            self.audit.llm_call(tag, result, prompt_sha=key[:16], prompt_chars=len(system) + len(user))
        return result

    def _call(self, system: str, user: str, json_mode: bool) -> LLMResult:
        from openai import APIStatusError

        options = {"temperature": 0}
        if json_mode:
            options["response_format"] = {"type": "json_object"}
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        for _ in range(3):  # some endpoints reject temperature or response_format: drop and retry
            kwargs = {k: v for k, v in options.items() if k not in self._drop}
            started = time.perf_counter()
            try:
                resp = self._client.chat.completions.create(model=self.model, messages=messages, **kwargs)
            except APIStatusError as exc:
                # Endpoints differ in what they accept. On a 400, drop the option named in the error,
                # or else the next optional one (JSON mode first, then temperature), and try again.
                named = [k for k in kwargs if k in str(exc)]
                droppable = named or [k for k in ("response_format", "temperature") if k in kwargs][:1]
                if exc.status_code == 400 and droppable:
                    self._drop.update(droppable)
                    continue
                raise LLMError(f"LLM request failed: HTTP {exc.status_code} from {self.describe()}") from exc
            except Exception as exc:  # noqa: BLE001
                raise LLMError(f"LLM request failed: {type(exc).__name__} calling {self.describe()}") from exc
            usage = resp.usage
            return LLMResult(resp.choices[0].message.content or "", self.model,
                             getattr(usage, "prompt_tokens", 0) or 0, getattr(usage, "completion_tokens", 0) or 0,
                             int((time.perf_counter() - started) * 1000))
        raise LLMError(f"LLM endpoint {self.describe()} rejected the request options")


def extract_json(text: str) -> dict:
    """Parse the JSON object in a model reply, tolerating code fences and stray prose."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.M)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found in the reply")
    return json.loads(cleaned[start:end + 1])


def extract_code(text: str) -> str:
    """Return the Python module in a model reply (largest fenced block, else the whole reply)."""
    blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)```", text, flags=re.S)
    code = max(blocks, key=len) if blocks else text
    if "def fetch" not in code:
        raise ValueError("the reply does not contain a fetch() function")
    return code.strip() + "\n"
