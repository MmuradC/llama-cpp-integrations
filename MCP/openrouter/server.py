#!/usr/bin/env python3
"""OpenRouter as an MCP tool, so a local model can pick and call any hosted model.

llama.cpp's router only manages local GGUF instances — there is no native way to
add a remote OpenAI-compatible endpoint as a selectable model in its UI. This
gets the same practical effect a different way: list_openrouter_models lets you
browse what is available, and ask_openrouter's `model` argument IS the selection
— the caller names any OpenRouter model id per call.

Credentials, in order of preference:
    OPENROUTER_API_KEY_FILE   path to a file containing only the key (chmod 600)
    OPENROUTER_API_KEY        the key itself

Prefer the file: the MCP config is a plain JSON file, easy to copy or paste
without noticing what is inside it.

Other configuration:
    OPENROUTER_MODEL     default model when a call does not name one
    OPENROUTER_TIMEOUT   seconds per request, default 300
    OPENROUTER_REFERER   optional, sent as HTTP-Referer (OpenRouter attribution)
    OPENROUTER_TITLE     optional, sent as X-Title (OpenRouter attribution)

Calls are billed by OpenRouter against your account, at each model's own rate —
which varies enormously across the catalog.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from mcp.server.fastmcp import FastMCP

BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = os.environ.get("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct")
TIMEOUT = int(os.environ.get("OPENROUTER_TIMEOUT", "300"))

mcp = FastMCP("openrouter")


def _api_key() -> str | None:
    path = os.environ.get("OPENROUTER_API_KEY_FILE", "").strip()
    if path:
        try:
            key = Path(path).expanduser().read_text(encoding="utf-8").strip()
            if key:
                return key
        except OSError as exc:
            print(f"[openrouter] cannot read OPENROUTER_API_KEY_FILE: {exc}", file=sys.stderr)
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    return key or None


def _no_key_message() -> str:
    return (
        "No OpenRouter API key configured.\n"
        "Get one at https://openrouter.ai/keys, then store it in a file only you can read:\n"
        "    install -m 600 /dev/null ~/.config/openrouter.key\n"
        "    printf '%s' 'sk-or-v1-...' > ~/.config/openrouter.key\n"
        "and set OPENROUTER_API_KEY_FILE=/home/murad/.config/openrouter.key in this "
        "server's env block in llama-mcp-servers.json."
    )


def _request(path: str, payload: dict | None = None, timeout: int | None = None) -> dict:
    key = _api_key()
    headers = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    referer = os.environ.get("OPENROUTER_REFERER", "").strip()
    title = os.environ.get("OPENROUTER_TITLE", "").strip()
    if referer:
        headers["HTTP-Referer"] = referer
    if title:
        headers["X-Title"] = title

    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(
        f"{BASE_URL}{path}", data=data, headers=headers,
        method="POST" if data else "GET",
    )
    with urllib.request.urlopen(req, timeout=timeout or TIMEOUT) as resp:
        return json.loads(resp.read())


def _http_error(exc: urllib.error.HTTPError) -> str:
    body = exc.read().decode(errors="replace")[:400]
    if exc.code in (401, 403):
        return f"OpenRouter rejected the key ({exc.code}). Check it is current.\n{body}"
    if exc.code == 404:
        return f"Model not found (404). Use list_openrouter_models to see valid ids.\n{body}"
    if exc.code == 429:
        return f"Rate limited or out of credits (429).\n{body}"
    return f"OpenRouter returned {exc.code}: {body}"


@mcp.tool()
def ask_openrouter(
    prompt: str,
    model: str = "",
    system: str = "",
    max_tokens: int = 2048,
    temperature: float = 0.7,
) -> str:
    """Ask a model hosted on OpenRouter a question and return its answer.

    The `model` argument is the model selection — OpenRouter hosts hundreds under
    one API (Claude, GPT, Gemini, Llama, DeepSeek, Qwen, Mistral and more). Call
    list_openrouter_models first to find an exact id, e.g.
    "anthropic/claude-opus-5" or "openai/gpt-5". Each provider bills at its own
    rate, so prefer answering locally when that is good enough.
    """
    if not prompt.strip():
        return "Prompt is empty; nothing to ask."
    if not _api_key():
        return _no_key_message()

    messages = []
    if system.strip():
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": model.strip() or DEFAULT_MODEL,
        "messages": messages,
        "max_tokens": max(1, min(int(max_tokens), 32768)),
        "temperature": max(0.0, min(float(temperature), 2.0)),
        "stream": False,
    }

    started = time.monotonic()
    try:
        result = _request("/chat/completions", payload)
    except urllib.error.HTTPError as exc:
        return _http_error(exc)
    except Exception as exc:
        return f"Request to OpenRouter failed: {exc}"

    if "error" in result:
        return f"OpenRouter error: {result['error']}"

    try:
        choice = result["choices"][0]["message"]
    except (KeyError, IndexError):
        return f"Unexpected response shape. Keys: {list(result)}"

    answer = (choice.get("content") or "").strip()
    if not answer:
        answer = "(model returned no content)"

    usage = result.get("usage") or {}
    footer = (
        f"\n\n[{payload['model']} · {time.monotonic() - started:.1f}s"
        + (f" · {usage.get('total_tokens')} tokens" if usage.get("total_tokens") else "")
        + "]"
    )
    return answer + footer


@mcp.tool()
def list_openrouter_models(filter: str = "") -> str:
    """List models available on OpenRouter, optionally filtered by substring.

    There are hundreds across every major provider, so pass a filter such as
    "claude", "gpt-5", "deepseek", "free" or "gemini" to narrow it down. Pass an
    empty filter to see the default model and get a sense of scale.
    """
    try:
        result = _request("/models", timeout=30)
    except urllib.error.HTTPError as exc:
        return _http_error(exc)
    except Exception as exc:
        return f"Could not list models: {exc}"

    rows = result.get("data", [])
    ids = sorted(m.get("id", "") for m in rows if isinstance(m, dict))
    needle = filter.strip().lower()
    if needle:
        ids = [i for i in ids if needle in i.lower()]
    if not ids:
        return f"No models matching {filter!r}."

    head = f"{len(ids)} model(s)" + (f" matching {filter!r}" if needle else " total")
    shown = ids[:60]
    body = "\n".join(f"  {i}" for i in shown)
    if len(ids) > len(shown):
        body += f"\n  ... and {len(ids) - len(shown)} more; narrow with a filter"
    return f"{head}:\n{body}\n\ndefault when unspecified: {DEFAULT_MODEL}"


if __name__ == "__main__":
    state = "key configured" if _api_key() else "NO KEY — see _no_key_message"
    print(f"[openrouter] {BASE_URL} | {state}", file=sys.stderr)
    mcp.run("stdio")
