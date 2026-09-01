#!/usr/bin/env python3
"""Dashboard data for the llama.cpp right-bar panel.

Aggregates, on every request:
  - each of the 9 MCP servers: a real handshake through the bridge (SSE) to
    get a live tool count and round-trip time, not just "configured"
  - GPU: total/used/free VRAM and utilisation, per device
  - llama-server: which model(s) are currently loaded
  - sd-server: reachable or not, and which diffusion model is loaded

This is intentionally separate from llama.cpp's own source tree: the Svelte
panel in ../frontend imports nothing from here except this HTTP endpoint's
JSON, so the panel's data logic can change without touching the llama.cpp
fork at all.

Config:
    PANEL_PORT           default 9010
    MCP_BRIDGE_URL        default http://127.0.0.1:9000
    LLAMA_SERVER_URL      default http://127.0.0.1:8080
    SD_SERVER_URL         default http://127.0.0.1:1234
    ALLOW_ORIGIN          default http://127.0.0.1:8080
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import time
from pathlib import Path

import httpx
import uvicorn
from mcp import ClientSession
from mcp.client.sse import sse_client
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

# rag_core.py lives in MCP/rag/, shared with the rag MCP server (see
# MCP/rag/server.py) — same reasoning as RightBar.svelte's own cross-boundary
# workaround: this file lives in a different directory, so the import needs
# an explicit path rather than a package install.
sys.path.insert(0, "/home/murad/Documents/llama.cpp/MCP/rag")
import rag_core  # noqa: E402

PORT = int(os.environ.get("PANEL_PORT", "9010"))
BRIDGE_URL = os.environ.get("MCP_BRIDGE_URL", "http://127.0.0.1:9000").rstrip("/")
LLAMA_URL = os.environ.get("LLAMA_SERVER_URL", "http://127.0.0.1:8080").rstrip("/")
SD_URL = os.environ.get("SD_SERVER_URL", "http://127.0.0.1:1234").rstrip("/")
# comma-separated; includes the vite dev-server port (5173) so the frontend
# can be iterated on with `npm run dev` without a full C++ rebuild each time
ALLOW_ORIGINS = os.environ.get(
    "ALLOW_ORIGINS", "http://127.0.0.1:8080,http://127.0.0.1:5173"
).split(",")

SERVER_NAMES = ["filesystem", "git", "fetch", "search", "markitdown", "pandoc", "imagegen", "nim", "openrouter"]

# Where the "Secrets" section in the panel writes API keys, so the servers
# that read *_API_KEY_FILE pick them up. Fixed, known filenames only — this
# endpoint must never accept an arbitrary path, only pick from this map. Paths
# match each server's actual *_API_KEY_FILE in llama-mcp-servers.json exactly;
# writing here does not go through the filesystem MCP tool or its sandbox —
# this process has the user's own normal filesystem access, so the path does
# not need to sit inside an MCP-allowed root.
SECRET_FILES = {
    "openrouter": Path("/home/murad/.config/mcp-secrets/openrouter.key"),
    "nim": Path("/home/murad/.config/nvidia-nim.key"),
}

# Every remote provider a model can be pinned from. The router
# (server-models.cpp's load_remote_model_presets(), see the fork patch)
# reads each pinned_file at startup/reload and registers one static,
# already-"loaded" model entry per pin — id "{provider}/{sanitized real id}"
# — all pointing back at THIS backend's own port, so they show up for real
# in llama.cpp's normal chat model dropdown. Adding a new provider here is
# the whole frontend-facing half of supporting it; the matching C++ change
# is one more file path read the same way (see load_remote_model_presets()).
PROVIDERS = {
    "openrouter": {
        "chat_url": "https://openrouter.ai/api/v1/chat/completions",
        "key_file": SECRET_FILES["openrouter"],
        "pinned_file": Path("/home/murad/.config/mcp-secrets/openrouter-pinned-models.json"),
        "no_key_message": "No OpenRouter key saved yet — set one in the Secrets section first.",
    },
    "nim": {
        "chat_url": "https://integrate.api.nvidia.com/v1/chat/completions",
        "key_file": SECRET_FILES["nim"],
        "pinned_file": Path("/home/murad/.config/mcp-secrets/nim-pinned-models.json"),
        "no_key_message": "No NVIDIA NIM key saved yet — set one in the Secrets section first.",
    },
}


# The right-bar panel polls /api/dashboard every 4s (RightBar.svelte's
# POLL_MS) so the "connected" badges feel live, but a full MCP handshake
# (SSE connect + initialize + list_tools) per server on every single poll —
# with no reuse and no overlap guard — meant up to 9 fresh
# connect/teardown cycles every ~4s, compounding further whenever a check
# ran long enough to overlap the next timer tick (observed as 2-4s in
# practice, ~100+ reconnects/server in a few minutes). A short TTL cache
# keeps checks "real" (per the module docstring — this still catches a
# server that's configured but actually down) while capping how often that
# handshake actually happens, the same tradeoff _openrouter_cache below
# already makes for the catalog fetch.
_mcp_check_cache: dict[str, dict] = {}
MCP_CHECK_CACHE_TTL = 20


async def _check_mcp_server(name: str) -> dict:
    cached = _mcp_check_cache.get(name)
    if cached is not None and time.monotonic() - cached["at"] < MCP_CHECK_CACHE_TTL:
        return cached["result"]

    url = f"{BRIDGE_URL}/servers/{name}/sse"
    started = time.monotonic()
    try:
        async with asyncio.timeout(8):
            async with sse_client(url) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
        result = {
            "name": name,
            "ok": True,
            "tool_count": len(tools.tools),
            "ms": round((time.monotonic() - started) * 1000),
        }
    except Exception as exc:
        result = {"name": name, "ok": False, "error": str(exc)[:150]}

    _mcp_check_cache[name] = {"result": result, "at": time.monotonic()}
    return result


async def _gpu_stats() -> list[dict]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "nvidia-smi",
            "--query-gpu=index,name,memory.total,memory.used,memory.free,utilization.gpu",
            "--format=csv,noheader,nounits",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
    except Exception:
        return []

    gpus = []
    for line in out.decode(errors="replace").strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 6:
            continue
        idx, name, total, used, free, util = parts
        gpus.append({
            "index": int(idx), "name": name,
            "total_mib": int(total), "used_mib": int(used),
            "free_mib": int(free), "util_pct": int(util),
        })
    return gpus


async def _llama_models(client: httpx.AsyncClient) -> dict:
    try:
        resp = await client.get(f"{LLAMA_URL}/models", timeout=5)
        data = resp.json()
        entries = data.get("data", data) if isinstance(data, dict) else data
        loaded = []
        for e in entries if isinstance(entries, list) else []:
            if not isinstance(e, dict):
                continue
            raw = e.get("status", "")
            state = str(raw.get("value", "") if isinstance(raw, dict) else raw).lower()
            if state and state != "unloaded":
                loaded.append(e.get("id") or e.get("name"))
        return {"reachable": True, "loaded": loaded}
    except Exception as exc:
        return {"reachable": False, "error": str(exc)[:150]}


async def _sd_status(client: httpx.AsyncClient) -> dict:
    try:
        resp = await client.get(f"{SD_URL}/sdcpp/v1/capabilities", timeout=5)
        data = resp.json()
        model = data.get("model", {}) if isinstance(data, dict) else {}
        return {"reachable": True, "model": model.get("name") if isinstance(model, dict) else None}
    except Exception:
        return {"reachable": False}


async def dashboard(request):
    async with httpx.AsyncClient() as client:
        mcp_results, gpus, llama, sd = await asyncio.gather(
            asyncio.gather(*(_check_mcp_server(n) for n in SERVER_NAMES)),
            _gpu_stats(),
            _llama_models(client),
            _sd_status(client),
        )
    return JSONResponse({
        "mcp_servers": list(mcp_results),
        "gpus": gpus,
        "llama_server": llama,
        "sd_server": sd,
        "generated_at": time.time(),
    })


_openrouter_cache: dict = {"data": None, "at": 0.0}
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
OPENROUTER_CACHE_TTL = 300  # OpenRouter's catalog is 300+ entries and barely
# changes minute to minute; refetching every dashboard-style poll would be
# wasteful, so the browse page's own poll (see OpenRouterPage.svelte) is slow
# (60s) and this cache absorbs repeat loads within that window regardless.


async def openrouter_models(request: Request) -> JSONResponse:
    """Proxy OpenRouter's public model catalog — no key needed, browser CORS
    would block a direct openrouter.ai fetch from the panel origin anyway."""
    now = time.monotonic()
    if _openrouter_cache["data"] is None or now - _openrouter_cache["at"] > OPENROUTER_CACHE_TTL:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(OPENROUTER_MODELS_URL, timeout=15)
                resp.raise_for_status()
                _openrouter_cache["data"] = resp.json()
                _openrouter_cache["at"] = now
        except Exception as exc:
            if _openrouter_cache["data"] is None:
                return JSONResponse({"error": str(exc)[:200]}, status_code=502)
            # serve the stale cache rather than a hard failure
    return JSONResponse(_openrouter_cache["data"])


_nim_cache: dict = {"data": None, "at": 0.0}
NIM_MODELS_URL = "https://integrate.api.nvidia.com/v1/models"
NIM_CACHE_TTL = 300


async def nim_models(request: Request) -> JSONResponse:
    """Proxy NVIDIA NIM's model catalog. Unlike OpenRouter's, this endpoint
    requires the API key — no key means an empty (not error) list, since
    "no key saved yet" is a normal state for this page to render (a bare
    404/502 would just look broken instead of guiding to Secrets)."""
    key_path = SECRET_FILES["nim"]
    if not key_path.exists() or key_path.stat().st_size == 0:
        return JSONResponse({"data": [], "error": "No NVIDIA NIM key saved yet — set one in the Secrets section first."})

    now = time.monotonic()
    if _nim_cache["data"] is None or now - _nim_cache["at"] > NIM_CACHE_TTL:
        try:
            key = key_path.read_text(encoding="utf-8").strip()
            async with httpx.AsyncClient() as client:
                resp = await client.get(NIM_MODELS_URL, headers={"Authorization": f"Bearer {key}"}, timeout=15)
                resp.raise_for_status()
                _nim_cache["data"] = resp.json()
                _nim_cache["at"] = now
        except Exception as exc:
            if _nim_cache["data"] is None:
                return JSONResponse({"error": str(exc)[:200]}, status_code=502)
            # serve the stale cache rather than a hard failure
    return JSONResponse(_nim_cache["data"])


async def secrets_status(request: Request) -> JSONResponse:
    """Whether each known key is set — never the value itself."""
    return JSONResponse({name: path.exists() and path.stat().st_size > 0 for name, path in SECRET_FILES.items()})


async def secrets_save(request: Request) -> JSONResponse:
    name = request.path_params.get("name", "")
    path = SECRET_FILES.get(name)
    if path is None:
        return JSONResponse({"error": f"unknown secret {name!r}"}, status_code=404)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "expected JSON body"}, status_code=400)

    value = str(body.get("value", "")).strip()
    if not value:
        return JSONResponse({"error": "value is empty"}, status_code=400)

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 600 — owner read/write only
    except OSError as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

    return JSONResponse({"saved": True})


def _read_pinned(provider: str) -> list[dict]:
    path = PROVIDERS[provider]["pinned_file"]
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [e for e in data if isinstance(e, dict) and e.get("id")] if isinstance(data, list) else []
    except Exception:
        return []


def _write_pinned(provider: str, entries: list[dict]) -> None:
    path = PROVIDERS[provider]["pinned_file"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


async def _reload_router() -> None:
    """Best-effort: ask the router to re-read its model sources (including
    this pinned file) without a full restart. Fine to fail silently — a
    `systemctl --user restart llama-server` is always the guaranteed
    fallback, same as every other change to this project."""
    try:
        async with httpx.AsyncClient() as client:
            await client.get(f"{LLAMA_URL}/models", params={"reload": "true"}, timeout=10)
    except Exception:
        pass


def _provider_or_404(request: Request) -> str | None:
    provider = request.path_params.get("provider", "")
    return provider if provider in PROVIDERS else None


async def pinned_list(request: Request) -> JSONResponse:
    provider = _provider_or_404(request)
    if provider is None:
        return JSONResponse({"error": "unknown provider"}, status_code=404)
    return JSONResponse(_read_pinned(provider))


async def pinned_add(request: Request) -> JSONResponse:
    provider = _provider_or_404(request)
    if provider is None:
        return JSONResponse({"error": "unknown provider"}, status_code=404)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "expected JSON body"}, status_code=400)

    model_id = str(body.get("id", "")).strip()
    if not model_id:
        return JSONResponse({"error": "id is required"}, status_code=400)
    name = str(body.get("name", "")).strip() or model_id

    entries = _read_pinned(provider)
    if not any(e["id"] == model_id for e in entries):
        entries.append({"id": model_id, "name": name})
        _write_pinned(provider, entries)
    await _reload_router()
    return JSONResponse({"pinned": entries})


async def pinned_remove(request: Request) -> JSONResponse:
    provider = _provider_or_404(request)
    if provider is None:
        return JSONResponse({"error": "unknown provider"}, status_code=404)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "expected JSON body"}, status_code=400)

    model_id = str(body.get("id", "")).strip()
    entries = [e for e in _read_pinned(provider) if e["id"] != model_id]
    _write_pinned(provider, entries)
    await _reload_router()
    return JSONResponse({"pinned": entries})


def _openai_error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": {"message": message, "type": "invalid_request_error", "code": None}}, status_code=status)


def _enrich_error_message(error_body: bytes) -> bytes:
    """OpenRouter's own top-level error.message is frequently a useless
    boilerplate string ("Provider returned error") while the actual reason
    sits one level down in error.metadata.raw — and the web UI's error
    dialog only ever displays error.message. Promote the real detail up so
    the dialog shows something a human can act on. Falls back to the
    original bytes untouched on any parse surprise (unknown shape, NIM's
    already-direct error.message, non-JSON body, etc.)."""
    try:
        parsed = json.loads(error_body)
        err = parsed["error"]
        raw = err.get("metadata", {}).get("raw")
        if isinstance(raw, str) and raw.strip():
            message = raw.strip()
            retry_after = err.get("metadata", {}).get("retry_after_seconds")
            if isinstance(retry_after, (int, float)):
                message += f" (retry in {retry_after:g}s)"
            err["message"] = message
            return json.dumps(parsed).encode()
    except Exception:
        pass
    return error_body


def _registered_id(provider: str, real_id: str) -> str:
    """The id a pinned model is actually registered under in the router —
    must match server-models.cpp's load_remote_model_presets_for() exactly.
    Real provider ids look like "vendor/model" (sometimes more segments),
    but llama.cpp's own normalizeModelName() — used whenever it persists
    which model produced a reply — only preserves an id with EXACTLY one
    slash; two or more collapses to just the last segment, silently
    destroying the provider/router prefix. So every inner "/" gets replaced
    with "__" here, leaving exactly one real slash (after the provider name)."""
    return f"{provider}/" + real_id.replace("/", "__")


def _resolve_registered_model(registered: str) -> tuple[str, str] | None:
    """Reverse of _registered_id(), via the pinned registries rather than
    undoing the "__" encoding textually — real ids can contain underscores
    too, so a straight string-reverse would be ambiguous. Returns
    (provider, real_id)."""
    for provider in PROVIDERS:
        for entry in _read_pinned(provider):
            if _registered_id(provider, entry["id"]) == registered:
                return provider, entry["id"]
    return None


# Forwarded 1:1 when the client supplies them — everything else in the
# incoming body (llama.cpp-specific fields like timings_per_token) is
# dropped rather than passed through blind to OpenRouter. "tools" is what
# lets an OpenRouter model actually call the same MCP tools (filesystem,
# git, fetch, ...) a local model can — the web UI already attaches it to
# every request when tools are enabled for the conversation (messages
# already carry their own tool_calls/tool_call_id and are forwarded as-is
# elsewhere in this function, unrelated to this list).
_PASSTHROUGH_FIELDS = ("temperature", "top_p", "max_tokens", "presence_penalty", "frequency_penalty", "stop", "tools")


async def remote_chat_completions(request: Request) -> Response:
    """The HTTP target every pinned remote "model" (any provider) in the
    router actually points at (see server-models.cpp's remote-source patch —
    those entries are registered with this process's port, no spawned child
    at all). Must live at exactly this path: the router proxies the incoming
    request path verbatim, and the web UI always calls /v1/chat/completions
    with stream:true, so this both matches that path and speaks real SSE —
    a single buffered JSON reply is never reached by that code path."""
    try:
        body = await request.json()
    except Exception:
        return _openai_error("expected JSON body", 400)

    model = str(body.get("model", "")).strip()
    messages = body.get("messages")
    if not model or not isinstance(messages, list) or not messages:
        return _openai_error("expected {model, messages}", 400)

    resolved = _resolve_registered_model(model)
    if resolved is None:
        return _openai_error(f"model {model!r} is not pinned", 404)
    provider, real_model = resolved
    provider_cfg = PROVIDERS[provider]

    key_path = provider_cfg["key_file"]
    if not key_path.exists() or key_path.stat().st_size == 0:
        return _openai_error(provider_cfg["no_key_message"], 400)
    key = key_path.read_text(encoding="utf-8").strip()

    upstream_body: dict = {"model": real_model, "messages": messages, "stream": True}
    for field in _PASSTHROUGH_FIELDS:
        if field in body:
            upstream_body[field] = body[field]

    tools_in = body.get("tools")
    tool_names = [t.get("function", {}).get("name") for t in tools_in] if isinstance(tools_in, list) else None
    print(f"[{provider} relay] -> {real_model!r}: {len(messages)} messages, tools={tool_names}", file=sys.stderr)

    client = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=15.0))
    req = client.build_request(
        "POST",
        provider_cfg["chat_url"],
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=upstream_body,
    )
    try:
        upstream = await client.send(req, stream=True)
    except Exception as exc:
        await client.aclose()
        return _openai_error(str(exc)[:200], 502)

    if upstream.status_code != 200:
        error_body = await upstream.aread()
        await upstream.aclose()
        await client.aclose()
        # this is the one failure path that had no logging at all — the
        # request never even reached the streaming loop below, so neither
        # of the other two log lines fire; without this, a same-request
        # (pre-stream) rejection from upstream was completely invisible.
        print(f"[{provider} relay] {real_model!r} rejected before streaming: HTTP {upstream.status_code}: {error_body[:500]!r}", file=sys.stderr)
        error_body = _enrich_error_message(error_body)
        return Response(content=error_body, status_code=upstream.status_code, media_type="application/json")

    async def relay():
        # Rewrite each chunk's own "model" field from the provider's real id
        # back to the router-registered one (e.g. "openrouter/nvidia__...").
        # Not cosmetic: the web UI persists this exact field as the saved
        # message's model and reads it back via getConversationModel() to
        # keep using "the same model" when a conversation continues —
        # relaying the provider's raw id verbatim would save an id the
        # router has never heard of, and the next message in that
        # conversation would fail with "model not found".
        # Deliberately do NOT synthesize a "data: [DONE]\n\n" on any path
        # where OpenRouter didn't send one itself (including a real
        # exception here). The web UI's SSE parser has no handling for an
        # "error" field in a chunk at all — it only reacts to "choices"
        # content and the literal [DONE] marker — so a synthesized [DONE]
        # would make an interrupted, truncated generation look like a
        # completed one to the user, silently, with no indication anything
        # went wrong. Leaving the stream to end without [DONE] instead lets
        # the web UI's own existing "lost connection, try to resume, then
        # show an error" path run — visible, if generic, beats silent data
        # loss. See resolve_child_for_conv's SERVER_MODEL_SOURCE_REMOTE
        # exclusion for why that resume attempt fails fast instead of
        # hanging.
        saw_done = False
        try:
            async for line in upstream.aiter_lines():
                if not line:
                    continue
                if not line.startswith("data: "):
                    yield (line + "\n\n").encode()
                    continue
                payload = line[len("data: "):]
                if payload.strip() == "[DONE]":
                    saw_done = True
                    yield b"data: [DONE]\n\n"
                    continue
                try:
                    chunk = json.loads(payload)
                    if isinstance(chunk, dict) and isinstance(chunk.get("error"), dict):
                        # A provider failure (rate limit, overloaded, etc.)
                        # arrives as an in-stream chunk with "choices": [] and
                        # an "error" object — not an HTTP error status, so the
                        # pre-flight status check above never sees it. Worse,
                        # this is usually followed by a clean [DONE] (the
                        # provider isn't dropping the connection, it's ending
                        # the turn on purpose) — so the "let it end without
                        # [DONE] so the web UI's own lost-connection path
                        # shows a dialog" trick below never triggers either.
                        # The SSE parser has no handling for "error" at all
                        # (only choices[].delta.content and [DONE]), so left
                        # alone this is completely invisible: no content, no
                        # dialog, just a silently empty reply. Turn it into
                        # real message content instead — guaranteed visible.
                        err_msg = chunk["error"].get("message", "unknown upstream error")
                        print(f"[{provider} relay] {real_model!r} reported an in-stream error: {chunk['error']}", file=sys.stderr)
                        visible = {
                            "id": chunk.get("id", ""),
                            "object": "chat.completion.chunk",
                            "model": model,
                            "choices": [{
                                "index": 0,
                                "delta": {"role": "assistant", "content": f"\n\n⚠️ **Upstream error:** {err_msg}\n"},
                                "finish_reason": None
                            }],
                        }
                        yield f"data: {json.dumps(visible)}\n\n".encode()
                        continue
                    if isinstance(chunk, dict) and "model" in chunk:
                        chunk["model"] = model
                    yield f"data: {json.dumps(chunk)}\n\n".encode()
                except Exception:
                    yield (line + "\n\n").encode()
        except Exception as exc:
            # uvicorn's log_level="warning" would otherwise hide this
            # entirely — print() still reaches the systemd journal
            # regardless, so `journalctl --user -u mcp-panel-backend` can
            # show the actual reason a generation was cut off instead of
            # only ever seeing the web UI's generic "connection lost".
            print(f"[{provider} relay] stream to {model!r} ended abnormally: {type(exc).__name__}: {exc}", file=sys.stderr)
            saw_done = True  # already logged above; the plain "no [DONE]" log below is for the other, silent case
        finally:
            await upstream.aclose()
            await client.aclose()

        if not saw_done:
            # Upstream closed the connection cleanly — no exception at
            # all — without ever sending [DONE] or a finish_reason. httpx
            # treats that as a normal end of stream, so the except block
            # above never runs and this would otherwise go completely
            # unlogged.
            print(f"[{provider} relay] stream to {model!r} ended with no [DONE]/finish_reason (upstream closed early)", file=sys.stderr)

    return StreamingResponse(relay(), media_type="text/event-stream")


async def router_props(request: Request) -> JSONResponse:
    """The web UI probes GET /props?model=<id> for every model it thinks is
    loaded (capabilities/modalities), proxied straight through by the router
    exactly like /v1/chat/completions. A minimal real response here, not the
    framework's bare 404: a 404 from this path was observed to crash the
    router process (something in its proxy/streaming layer for a small,
    connection-closing error response — never fully root-caused, but a real
    200 with a body sidesteps it entirely, and implementing this properly is
    the right fix regardless since a real llama-server child always answers
    its own /props)."""
    return JSONResponse({
        "default_generation_settings": {"params": {}, "n_ctx": 0},
        "model_path": "",
        "model_alias": str(request.query_params.get("model", "")),
        "build_info": "",
    })


async def catch_all(request: Request) -> JSONResponse:
    """Last-resort fallback for any other path the router's per-model proxy
    might probe on a pinned OpenRouter "model" (health checks, /slots, etc.)
    that this backend does not explicitly implement — same reasoning as
    router_props above: always answer with a real, small 200 JSON body,
    never the framework's default 404."""
    return JSONResponse({})


UPLOAD_DIR = Path("/tmp/llama-pasted-images")
_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")


async def upload_image(request: Request) -> JSONResponse:
    """Persists a pasted/dropped image to disk so a text-only model's
    filesystem/vision tools can reach it by path — the chat UI falls back to
    this instead of blocking the attachment when the active model has no
    vision support (see pending-image-path.svelte.ts on the frontend)."""
    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        return JSONResponse({"error": "expected a multipart 'file' field"}, status_code=400)

    ext = Path(upload.filename or "").suffix.lower()
    if ext not in _IMAGE_EXTENSIONS:
        ext = ".png"

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    dest = UPLOAD_DIR / f"pasted-{int(time.time() * 1000)}{ext}"
    dest.write_bytes(await upload.read())

    return JSONResponse({"path": str(dest)})


async def rag_list(request: Request) -> JSONResponse:
    return JSONResponse(await asyncio.to_thread(rag_core.list_collections))


async def rag_create(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "expected JSON body"}, status_code=400)

    name = str(body.get("name", "")).strip()
    description = str(body.get("description", "")).strip()
    if not name:
        return JSONResponse({"error": "name is required"}, status_code=400)

    try:
        manifest = await asyncio.to_thread(rag_core.create_collection, name, description)
    except rag_core.RagError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(manifest)


async def rag_get(request: Request) -> JSONResponse:
    collection_id = request.path_params["id"]
    try:
        manifest = await asyncio.to_thread(rag_core.get_collection, collection_id)
    except rag_core.RagError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    return JSONResponse(manifest)


async def rag_delete(request: Request) -> JSONResponse:
    collection_id = request.path_params["id"]
    try:
        await asyncio.to_thread(rag_core.delete_collection, collection_id)
    except rag_core.RagError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    return JSONResponse({"deleted": True})


async def _run_ingestion(collection_id: str, tmp_path: Path, filename: str) -> None:
    try:
        await asyncio.to_thread(rag_core.ingest_document, collection_id, tmp_path, filename)
    except Exception as exc:
        # ingest_document already wrote status="error" into the manifest
        # before re-raising — this is just so the failure isn't silent in
        # the journal too.
        print(f"[rag] ingestion of {filename!r} into {collection_id!r} failed: {exc}", file=sys.stderr)
    finally:
        tmp_path.unlink(missing_ok=True)


async def rag_upload(request: Request) -> JSONResponse:
    """Mirrors upload_image's shape (multipart "file" field, extension-based
    handling) but ingestion runs as a background task instead of blocking —
    an embed call can take real time, especially on a cold model-swap — with
    status tracked in the collection's own manifest.json rather than an
    in-memory job registry, so it survives a panel-backend restart mid-run."""
    collection_id = request.path_params["id"]
    try:
        rag_core.get_collection(collection_id)
    except rag_core.RagError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)

    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        return JSONResponse({"error": "expected a multipart 'file' field"}, status_code=400)

    filename = upload.filename or "document"
    tmp_dir = Path("/tmp/llama-rag-uploads")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_dir / f"{int(time.time() * 1000)}-{Path(filename).name}"
    tmp_path.write_bytes(await upload.read())

    asyncio.create_task(_run_ingestion(collection_id, tmp_path, filename))
    return JSONResponse({"filename": filename, "status": "processing"})


async def rag_delete_document(request: Request) -> JSONResponse:
    collection_id = request.path_params["id"]
    doc_id = request.path_params["doc_id"]
    try:
        manifest = await asyncio.to_thread(rag_core.delete_document, collection_id, doc_id)
    except rag_core.RagError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    return JSONResponse(manifest)


app = Starlette(
    routes=[
        Route("/api/dashboard", dashboard),
        Route("/api/openrouter/models", openrouter_models),
        Route("/api/nim/models", nim_models),
        Route("/api/{provider}/pinned", pinned_list),
        Route("/api/{provider}/pin", pinned_add, methods=["POST"]),
        Route("/api/{provider}/unpin", pinned_remove, methods=["POST"]),
        Route("/v1/chat/completions", remote_chat_completions, methods=["POST"]),
        Route("/props", router_props),
        Route("/api/secrets", secrets_status),
        Route("/api/secrets/{name}", secrets_save, methods=["POST"]),
        Route("/api/upload-image", upload_image, methods=["POST"]),
        Route("/api/rag/collections", rag_list, methods=["GET"]),
        Route("/api/rag/collections", rag_create, methods=["POST"]),
        Route("/api/rag/collections/{id}", rag_get, methods=["GET"]),
        Route("/api/rag/collections/{id}", rag_delete, methods=["DELETE"]),
        Route("/api/rag/collections/{id}/documents", rag_upload, methods=["POST"]),
        Route("/api/rag/collections/{id}/documents/{doc_id}", rag_delete_document, methods=["DELETE"]),
        # must stay last: matches anything not matched above, see catch_all()
        Route("/{path:path}", catch_all, methods=["GET", "POST"]),
    ],
    middleware=[Middleware(CORSMiddleware, allow_origins=ALLOW_ORIGINS, allow_methods=["GET", "POST", "DELETE"])],
)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
