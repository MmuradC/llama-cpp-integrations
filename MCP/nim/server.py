#!/usr/bin/env python3
"""NVIDIA NIM as an MCP tool, so a local model can consult a hosted one.

llama.cpp cannot call out to a remote API — it only runs local GGUF models.
This exposes NVIDIA's hosted NIM endpoint (OpenAI-compatible) as tools, so the
model running in llama-server can hand a question to a much larger model when
the local one is not up to it.

Credentials, in order of preference:
    NVIDIA_API_KEY_FILE   path to a file containing only the key (chmod 600)
    NVIDIA_API_KEY        the key itself

Prefer the file: the MCP config is a plain JSON file that is easy to copy,
back up or paste into a chat without noticing what is inside it.

Other configuration:
    NIM_BASE_URL      default https://integrate.api.nvidia.com/v1
    NIM_MODEL         default model when a call does not name one
    NIM_TIMEOUT       seconds per request, default 300

Calls are billed by NVIDIA against your account.
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

BASE_URL = os.environ.get("NIM_BASE_URL", "https://integrate.api.nvidia.com/v1").rstrip("/")
DEFAULT_MODEL = os.environ.get("NIM_MODEL", "nvidia/llama-3.3-nemotron-super-49b-v1.5")
TIMEOUT = int(os.environ.get("NIM_TIMEOUT", "300"))

mcp = FastMCP("nim")


def _api_key() -> str | None:
    """Read the key, preferring a file over an environment variable."""
    path = os.environ.get("NVIDIA_API_KEY_FILE", "").strip()
    if path:
        try:
            key = Path(path).expanduser().read_text(encoding="utf-8").strip()
            if key:
                return key
        except OSError as exc:
            print(f"[nim] cannot read NVIDIA_API_KEY_FILE: {exc}", file=sys.stderr)
    key = os.environ.get("NVIDIA_API_KEY", "").strip()
    return key or None


def _no_key_message() -> str:
    return (
        "No NVIDIA API key configured.\n"
        "Get one at https://build.nvidia.com, then store it in a file only you can read:\n"
        "    install -m 600 /dev/null ~/.config/nvidia-nim.key\n"
        "    printf '%s' 'nvapi-...' > ~/.config/nvidia-nim.key\n"
        "and set NVIDIA_API_KEY_FILE=/home/murad/.config/nvidia-nim.key in this "
        "server's env block in llama-mcp-servers.json."
    )


def _request(path: str, payload: dict | None = None, timeout: int | None = None) -> dict:
    key = _api_key()
    headers = {"Accept": "application/json"}
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
        return f"NIM rejected the key ({exc.code}). Check it is current.\n{body}"
    if exc.code == 404:
        return f"Model not found (404). Use list_nim_models to see valid ids.\n{body}"
    if exc.code == 429:
        return f"Rate limited or out of credits (429).\n{body}"
    return f"NIM returned {exc.code}: {body}"


@mcp.tool()
def ask_nim(
    prompt: str,
    model: str = "",
    system: str = "",
    max_tokens: int = 2048,
    temperature: float = 0.7,
) -> str:
    """Ask a hosted NVIDIA NIM model a question and return its answer.

    Use this for questions the local model cannot handle well — very long
    context, specialist knowledge, or when a substantially larger model would
    give a better answer. Each call is billed to the account behind the key,
    so prefer answering locally when that is good enough. Call list_nim_models
    to see what is available.
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
        return f"Request to NIM failed: {exc}"

    try:
        choice = result["choices"][0]["message"]
    except (KeyError, IndexError):
        return f"Unexpected response shape. Keys: {list(result)}"

    # reasoning models return their chain separately; report only the answer
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
def list_nim_models(filter: str = "") -> str:
    """List models available on NVIDIA NIM, optionally filtered by substring.

    There are over a hundred, so pass a filter such as "nemotron", "deepseek",
    "gpt-oss" or "vision" to narrow it down.
    """
    try:
        result = _request("/models", timeout=30)
    except urllib.error.HTTPError as exc:
        return _http_error(exc)
    except Exception as exc:
        return f"Could not list models: {exc}"

    ids = sorted(m.get("id", "") for m in result.get("data", []) if isinstance(m, dict))
    needle = filter.strip().lower()
    if needle:
        ids = [i for i in ids if needle in i.lower()]
    if not ids:
        return f"No models matching {filter!r}."

    head = f"{len(ids)} model(s)" + (f" matching {filter!r}" if needle else "")
    shown = ids[:60]
    body = "\n".join(f"  {i}" for i in shown)
    if len(ids) > len(shown):
        body += f"\n  ... and {len(ids) - len(shown)} more; narrow with a filter"
    return f"{head}:\n{body}\n\ndefault when unspecified: {DEFAULT_MODEL}"


if __name__ == "__main__":
    state = "key configured" if _api_key() else "NO KEY — see _no_key_message"
    print(f"[nim] {BASE_URL} | {state}", file=sys.stderr)
    mcp.run("stdio")
