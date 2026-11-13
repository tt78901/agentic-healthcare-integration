"""The real LLM client, against a local OpenAI-compatible endpoint (no external calls)."""
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from fabric import llm as llm_module
from fabric.llm import LLM, extract_code, extract_json

PORT = 8177
calls = []
api = FastAPI()


@api.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    calls.append(body)
    if "response_format" in body:   # like endpoints that do not support JSON mode
        return JSONResponse({"error": {"message": "response_format is not supported by this model"}}, status_code=400)
    return {"id": "x", "object": "chat.completion", "created": 0, "model": body["model"],
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": 'Sure:\n```json\n{"ok": true}\n```'}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}}


@pytest.fixture(scope="module")
def endpoint():
    server = uvicorn.Server(uvicorn.Config(api, host="127.0.0.1", port=PORT, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(50):
        try:
            httpx.get(f"http://127.0.0.1:{PORT}/docs", timeout=0.5)
            break
        except httpx.HTTPError:
            time.sleep(0.1)
    yield f"http://127.0.0.1:{PORT}/v1"
    server.should_exit = True


def test_client_falls_back_caches_and_counts_tokens(endpoint, tmp_path, monkeypatch):
    monkeypatch.setattr(llm_module, "CACHE_DIR", tmp_path / "cache")
    client = LLM(endpoint, "test-key", "test-model")
    first = client.complete(system="Reply in JSON.", user="hello", tag="t", json_mode=True)
    assert extract_json(first.text) == {"ok": True}
    assert (first.prompt_tokens, first.completion_tokens, first.cached) == (11, 7, False)
    assert len(calls) == 2 and "response_format" in calls[0] and "response_format" not in calls[1]

    again = client.complete(system="Reply in JSON.", user="hello", tag="t", json_mode=True)
    assert again.cached and again.text == first.text and len(calls) == 2      # replayed, no new request

    client.complete(system="Reply in JSON.", user="different", tag="t", json_mode=True)
    assert len(calls) == 3 and "response_format" not in calls[2]               # rejected option stays dropped


def test_missing_configuration_is_explained(monkeypatch):
    for key in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(llm_module.LLMError, match="LLM_BASE_URL"):
        LLM.from_env()


def test_reply_parsing():
    assert extract_json('{"a": {"b": 1}}') == {"a": {"b": 1}}
    assert extract_json('Here you go:\n```json\n{"a": 1}\n```\nDone.') == {"a": 1}
    with pytest.raises(ValueError):
        extract_json("no json here")
    assert extract_code("x\n```python\nimport re\n\ndef fetch(resource_type, config):\n    return []\n```").startswith("import re")
    with pytest.raises(ValueError):
        extract_code("```python\nprint('hi')\n```")
