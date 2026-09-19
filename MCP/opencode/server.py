#!/usr/bin/env python3
"""OpenCode Go/Zen as an MCP tool, so a local model can pick and call any hosted model.

Mirrors MCP/openrouter/server.py's structure and reasoning exactly — same
"local model picks the remote model per call" shape — against OpenCode's
gateway instead. OpenCode exposes an OpenAI-compatible API
(package @ai-sdk/openai-compatible upstream) at two separate base URLs that
bill against two separate balances on the same account:

    https://opencode.ai/zen/v1      pay-as-you-go, no markup over vendor price
    https://opencode.ai/zen/go/v1   the flat $10/month Go subscription's own
                                     allowance — same key, different path

Defaults to the Go endpoint since that is the plan actually being used here;
override OPENCODE_BASE_URL to hit Zen's pay-as-you-go endpoint instead with
the same key.

Credentials, in order of preference:
    OPENCODE_API_KEY_FILE   path to a file containing only the key (chmod 600)
    OPENCODE_API_KEY        the key itself

Prefer the file: the MCP config is a plain JSON file, easy to copy or paste
without noticing what is inside it.

Other configuration:
    OPENCODE_BASE_URL    default https://opencode.ai/zen/go/v1
    OPENCODE_MODEL        default model when a call does not name one
    OPENCODE_TIMEOUT       seconds per request, default 300

Calls against the Go endpoint draw down the $10/month allowance
($12/5h, $30/week, $60/month caps per OpenCode's own docs); calls against
Zen bill per-token against the account's separate pay-as-you-go balance.

NOTE: written from OpenCode's public docs/source (opencode.ai/docs/go,
opencode.ai/docs/zen, sst/opencode's provider.ts) before ever calling it with
a real key — unlike the openrouter/nim servers this mirrors, it has not yet
been live-verified against an actual response. Confirm the model id format
(bare id like "kimi-k3" vs. a prefixed one) with list_opencode_models before
relying on ask_opencode.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from mcp.server.fastmcp import FastMCP

BASE_URL = os.environ.get("OPENCODE_BASE_URL", "https://opencode.ai/zen/go/v1").rstrip("/")
DEFAULT_MODEL = os.environ.get("OPENCODE_MODEL", "kimi-k3")
TIMEOUT = int(os.environ.get("OPENCODE_TIMEOUT", "300"))

# OpenCode Go's own client guidance (opencode.ai/docs/go/#where-can-i-use-it):
# identify with a real UA rather than a generic HTTP-library name, and send a
# stable per-conversation session ID so they can optimize routing/caching.
# This server has no notion of "conversation" (FastMCP tools are stateless
# calls), so one ID for the process's lifetime is the best available proxy -
# still far more stable than a fresh ID per call.
USER_AGENT = "llama-cpp-panel-opencode-mcp/1.0"
SESSION_ID = str(uuid.uuid4())

mcp = FastMCP("opencode")


def _api_key() -> str | None:
    path = os.environ.get("OPENCODE_API_KEY_FILE", "").strip()
    if path:
        try:
            key = Path(path).expanduser().read_text(encoding="utf-8").strip()
            if key:
                return key
        except OSError as exc:
            print(f"[opencode] cannot read OPENCODE_API_KEY_FILE: {exc}", file=sys.stderr)
    key = os.environ.get("OPENCODE_API_KEY", "").strip()
    return key or None


def _no_key_message() -> str:
    return (
        "No OpenCode API key configured.\n"
        "Buy/activate a plan at https://opencode.ai/auth, copy the key from the "
        "console, then store it in a file only you can read:\n"
        "    install -m 600 /dev/null ~/.config/mcp-secrets/opencode.key\n"
        "    printf '%s' '...' > ~/.config/mcp-secrets/opencode.key\n"
        "and set OPENCODE_API_KEY_FILE=/home/murad/.config/mcp-secrets/opencode.key "
        "in this server's env block in llama-mcp-servers.json."
    )


def _request(path: str, payload: dict | None = None, timeout: int | None = None) -> dict:
    key = _api_key()
    # urllib's default User-Agent (Python-urllib/3.x) gets caught by Cloudflare's
    # bot protection in front of opencode.ai - sometimes a clean 403, sometimes
    # the connection is killed mid-handshake in a way Python's ssl module
    # surfaces as a bad record mac. USER_AGENT/SESSION_ID follow OpenCode Go's
    # own client guidance to avoid both that and their MissingSessionID 400.
    headers = {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
        "x-opencode-session": SESSION_ID
    }
    if key:
        headers["Authorization"] = f"Bearer {key}"

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
        return f"OpenCode rejected the key ({exc.code}). Check it is current.\n{body}"
    if exc.code == 404:
        return f"Model not found (404). Use list_opencode_models to see valid ids.\n{body}"
    if exc.code == 429:
        return f"Rate limited or plan allowance exhausted (429).\n{body}"
    return f"OpenCode returned {exc.code}: {body}"


@mcp.tool()
def ask_opencode(
    prompt: str,
    model: str = "",
    system: str = "",
    max_tokens: int = 2048,
    temperature: float = 0.7,
) -> str:
    """Ask a model hosted on OpenCode Go/Zen a question and return its answer.

    The `model` argument is the model selection — OpenCode Go's catalog is 24
    curated open models (e.g. "kimi-k3", "glm-5.3", "deepseek-v4"). Call
    list_opencode_models first to confirm an exact id. Billed against the Go
    plan's dollar-metered allowance (or Zen's pay-as-you-go balance if
    OPENCODE_BASE_URL points there instead), so prefer answering locally when
    that is good enough.
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
        return f"Request to OpenCode failed: {exc}"

    if "error" in result:
        return f"OpenCode error: {result['error']}"

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
def list_opencode_models(filter: str = "") -> str:
    """List models available on OpenCode Go/Zen, optionally filtered by substring.

    The Go plan's catalog is 24 curated open models; pass a filter such as
    "kimi", "glm", "deepseek" or "qwen" to narrow it down. Pass an empty
    filter to see everything and the default model.
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
    print(f"[opencode] {BASE_URL} | {state}", file=sys.stderr)
    mcp.run("stdio")
