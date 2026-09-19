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
import hashlib
import json
import os
import re
import stat
import sys
import time
import traceback
from html import escape
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
sys.path.insert(0, "/home/murad/Documents/GitHub/llama.cpp/MCP/rag")
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

# Must list every server in MCP/llama-mcp-servers.json, or the panel silently
# reports the bridge as healthy while ignoring the rest - rag/vision/browser
# were missing here, which is why ad-hoc rag/vision calls looked "broken"
# while the dashboard showed ten green badges (see MEMORY: llama-cpp-mcp-setup).
SERVER_NAMES = ["filesystem", "files", "git", "fetch", "search", "markitdown", "pandoc", "imagegen", "nim", "openrouter", "opencode", "rag", "vision", "browser"]

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
    "opencode": Path("/home/murad/.config/mcp-secrets/opencode.key"),
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
    "opencode": {
        # Go's own endpoint (bills the flat $10/month allowance), not Zen's
        # pay-as-you-go https://opencode.ai/zen/v1 — same key works on both,
        # see MCP/opencode/server.py's docstring for the distinction.
        "chat_url": "https://opencode.ai/zen/go/v1/chat/completions",
        "key_file": SECRET_FILES["opencode"],
        "pinned_file": Path("/home/murad/.config/mcp-secrets/opencode-pinned-models.json"),
        "no_key_message": "No OpenCode key saved yet — set one in the Secrets section first.",
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


_opencode_cache: dict = {"data": None, "at": 0.0}
OPENCODE_MODELS_URL = "https://opencode.ai/zen/go/v1/models"
OPENCODE_CACHE_TTL = 300


async def opencode_models(request: Request) -> JSONResponse:
    """Proxy OpenCode Go's model catalog. Same "no key -> empty list, not
    error" reasoning as nim_models above: not having saved a key yet is a
    normal state for this page, not a broken one."""
    key_path = SECRET_FILES["opencode"]
    if not key_path.exists() or key_path.stat().st_size == 0:
        return JSONResponse({"data": [], "error": "No OpenCode key saved yet — set one in the Secrets section first."})

    now = time.monotonic()
    if _opencode_cache["data"] is None or now - _opencode_cache["at"] > OPENCODE_CACHE_TTL:
        try:
            key = key_path.read_text(encoding="utf-8").strip()
            async with httpx.AsyncClient() as client:
                resp = await client.get(OPENCODE_MODELS_URL, headers={"Authorization": f"Bearer {key}"}, timeout=15)
                resp.raise_for_status()
                _opencode_cache["data"] = resp.json()
                _opencode_cache["at"] = now
        except Exception as exc:
            if _opencode_cache["data"] is None:
                return JSONResponse({"error": str(exc)[:200]}, status_code=502)
            # serve the stale cache rather than a hard failure
    return JSONResponse(_opencode_cache["data"])


# Read verbatim off the "Usage" table in OpenCode's own console (the user
# pasted it 2026-09-13) — this is real account data, not a guess, but it is
# a snapshot: OpenCode can reshuffle these numbers (pricing/catalog changes)
# without notice, and we have no API to re-fetch it (opencode.ai/docs/go
# says usage is console-only, no /usage endpoint or rate-limit headers
# exist). Keys are lowercased/despaced for loose matching in
# _opencode_5h_limit below, since the real API model id format (e.g.
# whether it's "kimi-k3" or something with a vendor prefix) has not been
# confirmed against a live /models response yet. Value is "Est. requests /
# 5 hr" from that table — the closest available proxy for the documented
# $12/5h cap, not an exact figure (it assumes exclusive use of one model).
OPENCODE_5H_REQUEST_ESTIMATES = {
    "kimik3": 110, "qwen3.8max": 160, "grok4.6": 169, "qwen3.7max": 170,
    "glm5.3": 220, "glm5.2": 880, "glm5.1": 880, "deepseekv4pro": 1050,
    "kimik2.6": 1150, "kimik2.7code": 1350, "hy4preview": 1350,
    "gpt5.6luna": 2050, "minimaxm3": 3200, "mimov2.5pro": 3250,
    "qwen3.6plus": 3300, "minimaxm2.7": 3400, "qwen3.7plus": 4300,
    "hy3": 4300, "qwen3.8flash": 5400, "glm5.3flash": 6320,
    "deepseekv4flashvisionexp": 6500, "longcat2.0": 11400,
    "deepseekv4flash": 13000, "deepseekv4.1flash": 6500,
    "mimov2.5": 30100, "musespark1.3contributor": 45300,
    "musespark1.2contributor": 45300,
}


def _normalize_model_key(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum() or ch == ".")


def _opencode_5h_limit(real_model_id: str) -> int | None:
    needle = _normalize_model_key(real_model_id)
    for key, limit in OPENCODE_5H_REQUEST_ESTIMATES.items():
        if key in needle or needle in key:
            return limit
    return None


OPENCODE_USAGE_FILE = Path("/home/murad/.config/mcp-secrets/opencode-usage.json")
OPENCODE_USAGE_WINDOW_S = 5 * 3600


def _record_provider_usage(usage_file: Path, tag: str, registered_model_id: str, retention_s: float) -> None:
    """Appends one timestamp for this registered model id, dropping anything
    older than retention_s. Best-effort by design: a write failure here must
    never break the actual chat relay it is attached to, so every error is
    swallowed and only logged."""
    try:
        data: dict[str, list[float]] = {}
        if usage_file.exists():
            data = json.loads(usage_file.read_text(encoding="utf-8"))
        now = time.time()
        cutoff = now - retention_s
        times = [t for t in data.get(registered_model_id, []) if t > cutoff]
        times.append(now)
        data[registered_model_id] = times
        usage_file.parent.mkdir(parents=True, exist_ok=True)
        usage_file.write_text(json.dumps(data), encoding="utf-8")
        usage_file.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except Exception as exc:
        print(f"[{tag}] failed to record usage: {exc}", file=sys.stderr)


def _record_opencode_request(registered_model_id: str) -> None:
    _record_provider_usage(
        OPENCODE_USAGE_FILE, "opencode usage", registered_model_id, OPENCODE_USAGE_WINDOW_S * 2
    )
    _record_daily_usage("opencode")


# NIM gets a much longer retention than OpenCode on purpose: there is no
# documented NIM window to mirror (OpenCode has its 5h cap, so keeping more
# than that was pointless), and with no official counter available at all
# (see nim_usage's docstring) a longer local history is the entire value of
# tracking this. 30 days is plenty to answer "how much have I used lately".
NIM_USAGE_FILE = Path("/home/murad/.config/mcp-secrets/nim-usage.json")
NIM_USAGE_RETENTION_S = 30 * 24 * 3600


def _record_nim_request(registered_model_id: str) -> None:
    _record_provider_usage(NIM_USAGE_FILE, "nim usage", registered_model_id, NIM_USAGE_RETENTION_S)
    _record_daily_usage("nim")


# One recorder per provider that tracks usage locally. OpenRouter is absent
# deliberately: it is the one provider that does expose real account usage
# over its API (GET /api/v1/credits -> total_credits/total_usage), so there is
# nothing to reconstruct locally there.
_USAGE_RECORDERS = {
    "opencode": _record_opencode_request,
    "nim": _record_nim_request,
}


# --- daily rollup, for the dashboard's calendar -----------------------------
#
# The per-provider files above answer "how much right now" and cannot answer
# "how much on a given day" beyond their own retention: OpenCode keeps ten
# hours, NIM thirty days, and OpenRouter keeps no timestamps at all. A
# GitHub-style calendar needs one counter per day per provider, so every
# recorded request also bumps a day bucket here.
#
# Metric-agnostic on purpose: requests are the one unit all three providers
# share (that is what the calendar is coloured by), while cost and tokens ride
# along where the provider reports them, so a spend view needs no new data.
USAGE_HISTORY_FILE = Path("/home/murad/.config/mcp-secrets/usage-history.json")
USAGE_HISTORY_DAYS = 400
# One-shot guard for the startup backfill (see usage_history).
_history_backfilled = False


def _day_key(offset_days: int = 0) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(time.time() - offset_days * 86400))


def _record_daily_usage(provider: str, *, cost: float | None = None, tokens: int | None = None) -> None:
    """Bump today's bucket for one provider - best-effort, like the recorders
    above: bookkeeping must never break the chat it is attached to."""
    try:
        data: dict = {}
        if USAGE_HISTORY_FILE.exists():
            data = json.loads(USAGE_HISTORY_FILE.read_text(encoding="utf-8"))
        bucket = data.setdefault(_day_key(), {}).setdefault(
            provider, {"requests": 0, "cost": 0.0, "tokens": 0}
        )
        bucket["requests"] = int(bucket.get("requests", 0)) + 1
        if cost:
            bucket["cost"] = round(float(bucket.get("cost", 0.0)) + float(cost), 8)
        if tokens:
            bucket["tokens"] = int(bucket.get("tokens", 0)) + int(tokens)
        cutoff = _day_key(USAGE_HISTORY_DAYS)
        data = {day: value for day, value in data.items() if day >= cutoff}
        USAGE_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        USAGE_HISTORY_FILE.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
        USAGE_HISTORY_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except Exception as exc:
        print(f"[usage history] failed to record: {exc}", file=sys.stderr)


def _backfill_history_from_timestamps(usage_file: Path, provider: str) -> None:
    """One-shot, at startup: turn an existing timestamp list into day buckets
    so the calendar is not empty on the day this feature shipped.

    Counts are merged with `max`, not added: the file's history and the live
    rollup describe the same requests, so adding them would double-count every
    day that has both. Requests only - the timestamp files carry nothing else.
    """
    try:
        if not usage_file.exists():
            return
        stamps = json.loads(usage_file.read_text(encoding="utf-8"))
        per_day: dict[str, int] = {}
        for times in stamps.values():
            for stamp in times if isinstance(times, list) else []:
                per_day[_day_key(int((time.time() - float(stamp)) // 86400))] = (
                    per_day.get(_day_key(int((time.time() - float(stamp)) // 86400)), 0) + 1
                )
        if not per_day:
            return
        data: dict = {}
        if USAGE_HISTORY_FILE.exists():
            data = json.loads(USAGE_HISTORY_FILE.read_text(encoding="utf-8"))
        for day, count in per_day.items():
            bucket = data.setdefault(day, {}).setdefault(
                provider, {"requests": 0, "cost": 0.0, "tokens": 0}
            )
            bucket["requests"] = max(int(bucket.get("requests", 0)), count)
        USAGE_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        USAGE_HISTORY_FILE.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
        USAGE_HISTORY_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)
        print(f"[usage history] backfilled {provider}: {len(per_day)} day(s)", file=sys.stderr)
    except Exception as exc:
        print(f"[usage history] backfill failed: {exc}", file=sys.stderr)


async def usage_history(request: Request) -> JSONResponse:
    """Per-day usage for the dashboard calendar, oldest first, one entry per
    day whether or not anything was recorded that day (the calendar needs the
    gaps: an empty square is information)."""
    global _history_backfilled
    if not _history_backfilled:
        _history_backfilled = True
        await asyncio.to_thread(_backfill_history_from_timestamps, NIM_USAGE_FILE, "nim")
        await asyncio.to_thread(_backfill_history_from_timestamps, OPENCODE_USAGE_FILE, "opencode")
    try:
        days = max(1, min(int(request.query_params.get("days", "90")), USAGE_HISTORY_DAYS))
    except ValueError:
        days = 90
    data: dict = {}
    if USAGE_HISTORY_FILE.exists():
        try:
            data = json.loads(USAGE_HISTORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    out = []
    for i in range(days - 1, -1, -1):
        day = _day_key(i)
        per = data.get(day, {}) or {}
        out.append(
            {
                "date": day,
                "providers": {
                    name: {
                        "cost": float(value.get("cost", 0.0)),
                        "requests": int(value.get("requests", 0)),
                        "tokens": int(value.get("tokens", 0)),
                    }
                    for name, value in per.items()
                },
                "total": sum(int(value.get("requests", 0)) for value in per.values()),
            }
        )
    return JSONResponse({"days": out, "unit": "requests"})


def _usage_counts(usage_file: Path, registered: str) -> tuple[int, int, int]:
    """(count_5h, count_24h, count_total) for one registered model id. All
    zeroes when the file is missing or unreadable — an untracked model is not
    an error, it is just a model that has not been used since this existed."""
    try:
        data = json.loads(usage_file.read_text(encoding="utf-8")) if usage_file.exists() else {}
        times = data.get(registered, [])
        now = time.time()
        return (
            sum(1 for t in times if t > now - OPENCODE_USAGE_WINDOW_S),
            sum(1 for t in times if t > now - 24 * 3600),
            len(times),
        )
    except Exception:
        return 0, 0, 0


async def opencode_usage(request: Request) -> JSONResponse:
    """Local, estimated usage for one pinned OpenCode model — NOT official
    OpenCode data (see OPENCODE_5H_REQUEST_ESTIMATES's docstring for why
    none exists). `model` query param is the router-registered id
    ("opencode/<sanitized-real-id>"), matching what the web UI's context
    gauge already has as gauge.activeModelId."""
    registered = request.query_params.get("model", "")
    resolved = _resolve_registered_model(registered)
    if resolved is None or resolved[0] != "opencode":
        return JSONResponse({"error": "not a pinned opencode model"}, status_code=404)
    _, real_id = resolved

    count_5h, _, _ = _usage_counts(OPENCODE_USAGE_FILE, registered)
    limit_5h = _opencode_5h_limit(real_id)
    return JSONResponse({
        "real_id": real_id,
        "count_5h": count_5h,
        "limit_5h": limit_5h,
        "limit_is_estimate": True,
    })


async def nim_usage(request: Request) -> JSONResponse:
    """Local NIM usage, per pinned model, over three windows.

    NOT official NVIDIA data, and unlike OpenCode there is not even an
    *estimate* of a remaining allowance to compare against. NVIDIA's NIM API
    exposes no usage or credit figure at all (checked live 2026-09-14): all of
    /v1/usage, /v1/credits, /v1/quota, /v1/users/me and /v1/billing/usage
    answer `404 page not found`, and neither /v1/models nor a real chat
    completion carry any rate-limit, credit or quota header. Free-tier credits
    are visible only inside the build.nvidia.com console, behind a browser
    session. So what this counts is what THIS box actually sent — the only
    figure obtainable programmatically — and it says so in the response
    itself (`limit_known: false`) rather than inventing a denominator."""
    now = time.time()
    models = []
    for entry in _read_pinned("nim"):
        registered = _registered_id("nim", entry["id"])
        c5, c24, total = _usage_counts(NIM_USAGE_FILE, registered)
        models.append({
            "registered_id": registered,
            "real_id": entry["id"],
            "name": entry.get("name") or entry["id"],
            "count_5h": c5,
            "count_24h": c24,
            "count_total": total,
        })
    models.sort(key=lambda m: m["count_total"], reverse=True)
    return JSONResponse({
        "models": models,
        "count_5h_all": sum(m["count_5h"] for m in models),
        "count_24h_all": sum(m["count_24h"] for m in models),
        "count_total_all": sum(m["count_total"] for m in models),
        "limit_known": False,
        "retention_days": NIM_USAGE_RETENTION_S // 86400,
        "tracked_file": str(NIM_USAGE_FILE),
        "checked_at": now,
    })


_USAGE_PAGE_CSS = """
body { background:#111; color:#e6e6e6; font:14px ui-monospace,SFMono-Regular,Menlo,monospace;
       margin:0; padding:28px; }
h1 { font-size:16px; font-weight:600; margin:0 0 6px; }
p.sub { color:#8a8a8a; margin:0 0 20px; max-width:70ch; line-height:1.5; }
table { border-collapse:collapse; width:100%; max-width:900px; }
th, td { text-align:left; padding:7px 12px; border-bottom:1px solid #262626; }
th { color:#8a8a8a; font-weight:500; font-size:12px; text-transform:uppercase;
     letter-spacing:.04em; }
td.num { text-align:right; font-variant-numeric:tabular-nums; }
.zero { color:#555; }
.warn { background:#2a2210; border-left:3px solid #b58900; padding:10px 14px;
        margin:0 0 20px; max-width:900px; color:#d8c98a; line-height:1.5; }
.foot { color:#666; font-size:12px; margin-top:22px; max-width:900px; line-height:1.6; }
code { color:#7fb3d5; }
"""


def _usage_page_html(title: str, subtitle: str, note: str, models: list[dict],
                     totals: tuple[int, int, int], footer: str) -> str:
    """A self-contained usage page, served straight from this backend.

    Deliberately plain HTML/CSS with no dependency on the Svelte panel: the
    panel pages are compiled into llama-server's own UI bundle, so showing a
    number there means a full makepkg rebuild of llama.cpp. This file is
    Python, so it restarts in two seconds — the counter is visible long
    before any rebuild happens, and keeps working across one."""

    def row(m: dict) -> str:
        zero = ' class="zero"' if not m["count_total"] else ""
        cells = "".join(
            f'<td class="num"{zero}>{n}</td>' for n in (m["count_5h"], m["count_24h"], m["count_total"])
        )
        return (
            f'<tr><td>{escape(m["name"])}<br><span class="zero">{escape(m["real_id"])}</span></td>'
            f"{cells}</tr>"
        )

    body = "".join(row(m) for m in models) or (
        '<tr><td colspan="4" class="zero">No pinned models yet.</td></tr>'
    )
    total_row = "".join(f'<td class="num"><b>{n}</b></td>' for n in totals)
    note_html = f'<div class="warn">{escape(note)}</div>' if note else ""
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="15">
<title>{escape(title)}</title><style>{_USAGE_PAGE_CSS}</style></head><body>
<h1>{escape(title)}</h1>
<p class="sub">{escape(subtitle)}</p>
{note_html}
<table><thead><tr><th>Model</th><th class="num">5h</th><th class="num">24h</th>
<th class="num">total (30d)</th></tr></thead>
<tbody>{body}
<tr><td><b>all pinned models</b></td>{total_row}</tr>
</tbody></table>
<p class="foot">{footer}</p>
</body></html>"""


async def nim_usage_page(request: Request) -> Response:
    data = json.loads((await nim_usage(request)).body)
    note = (
        "NVIDIA exposes no usage or credit figure through its API at all — "
        "/v1/usage, /v1/credits, /v1/quota, /v1/users/me and /v1/billing/usage all "
        "answer 404, and no rate-limit or credit header comes back on a real chat "
        "completion either. These are counts of requests THIS machine sent, since "
        "that is the only number obtainable programmatically. Remaining free-tier "
        "credits are visible only in the build.nvidia.com console."
    )
    footer = (
        "Counted in <code>_record_nim_request</code> on every successful relay, "
        f"one timestamp per request, kept for {data['retention_days']} days in "
        f"<code>{escape(data['tracked_file'])}</code>. A request that NVIDIA "
        "rejects never reaches the counter, so 429s are not included. Page "
        "refreshes itself every 15s."
    )
    html_doc = _usage_page_html(
        "NVIDIA NIM usage (local count)",
        "Requests this box sent to integrate.api.nvidia.com, per pinned model.",
        note,
        data["models"],
        (data["count_5h_all"], data["count_24h_all"], data["count_total_all"]),
        footer,
    )
    return Response(content=html_doc, media_type="text/html")


async def opencode_usage_page(request: Request) -> Response:
    """The OpenCode counterpart. /api/opencode/usage already existed but had
    no consumer anywhere — it was only ever callable by hand with a model id.
    Same page, same renderer, and it does show a real denominator because
    OpenCode's 5h estimate table exists."""
    data = json.loads((await nim_usage(request)).body)  # totals shape is identical
    models = []
    for entry in _read_pinned("opencode"):
        registered = _registered_id("opencode", entry["id"])
        c5, c24, total = _usage_counts(OPENCODE_USAGE_FILE, registered)
        models.append({
            "registered_id": registered,
            "real_id": entry["id"],
            "name": f"{entry.get('name') or entry['id']}  [{c5}/{_opencode_5h_limit(entry['id']) or '?'} est. 5h cap]",
            "count_5h": c5,
            "count_24h": c24,
            "count_total": total,
        })
    models.sort(key=lambda m: m["count_5h"], reverse=True)
    note = (
        "OpenCode Go has no usage API either (console-only), so the 5h cap shown "
        "per model is a snapshot of estimates read off that console on 2026-09-13, "
        "not a live limit. Requests that OpenCode rejected (429 quota, 403 region) "
        "are not counted — only accepted ones."
    )
    footer = (
        "Counted in <code>_record_opencode_request</code>, kept for 10h in "
        f"<code>{escape(str(OPENCODE_USAGE_FILE))}</code>. Page refreshes every 15s."
    )
    html_doc = _usage_page_html(
        "OpenCode Go usage (local count)",
        "Accepted requests this box sent to opencode.ai/zen/go, per pinned model.",
        note,
        models,
        (sum(m["count_5h"] for m in models), sum(m["count_24h"] for m in models),
         sum(m["count_total"] for m in models)),
        footer,
    )
    return Response(content=html_doc, media_type="text/html")


# OpenRouter spend, accumulated per registered model from the usage block on
# each response's final chunk (see remote_chat_completions' relay). This is a
# running total this box computed, not a query of OpenRouter's own ledger:
# /api/v1/credits is account-wide only, so there is no per-model figure to
# fetch. Kept forever (spend is cumulative by nature — a 30-day trim would
# make the number go DOWN, which for money is worse than useless).
OPENROUTER_SPEND_FILE = Path("/home/murad/.config/mcp-secrets/openrouter-spend.json")


def _record_openrouter_spend(registered_model_id: str, usage: dict) -> None:
    """Best-effort, same contract as _record_provider_usage: never break the
    stream it is attached to. Only the fields actually present are trusted —
    OpenRouter's usage block varies by provider (some include cached_tokens,
    some cost as a string, some omit cost entirely for :free models)."""
    try:
        data: dict = {}
        if OPENROUTER_SPEND_FILE.exists():
            data = json.loads(OPENROUTER_SPEND_FILE.read_text(encoding="utf-8"))
        entry = data.get(registered_model_id) or {
            "cost": 0.0, "prompt_tokens": 0, "completion_tokens": 0, "requests": 0,
        }
        cost = usage.get("cost")
        if cost is not None:
            entry["cost"] = round(float(entry.get("cost", 0.0)) + float(cost), 8)
        for key in ("prompt_tokens", "completion_tokens"):
            if usage.get(key) is not None:
                entry[key] = int(entry.get(key, 0)) + int(usage[key])
        entry["requests"] = int(entry.get("requests", 0)) + 1
        entry["last_cost"] = float(cost) if cost is not None else entry.get("last_cost")
        entry["last_at"] = time.time()
        data[registered_model_id] = entry
        OPENROUTER_SPEND_FILE.parent.mkdir(parents=True, exist_ok=True)
        OPENROUTER_SPEND_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        OPENROUTER_SPEND_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)
        # OpenRouter keeps no per-request timestamps of its own here, so the
        # daily rollup is the only place its history accrues.
        _record_daily_usage(
            "openrouter",
            cost=float(cost) if cost is not None else None,
            tokens=(int(usage["prompt_tokens"]) + int(usage["completion_tokens"]))
            if usage.get("prompt_tokens") is not None and usage.get("completion_tokens") is not None
            else None,
        )
    except Exception as exc:
        print(f"[openrouter spend] failed to record: {exc}", file=sys.stderr)


async def provider_usage(request: Request) -> JSONResponse:
    """Everything the chat UI's gauge needs about ONE pinned model, shaped by
    what its provider can actually report:

    - opencode: request counts + the 5h cap estimate read off its console
      (OpenCode exposes no usage API), so the gauge can draw a real fraction.
    - nim: request counts only. There is no denominator to draw — NVIDIA
      publishes no usage figure through its API at all (see nim_usage).
    - openrouter: spend in USD plus token totals accumulated locally from the
      usage block on each response, because /api/v1/credits is account-wide.

    `model` is the router-registered id, exactly what the web UI's context
    gauge already carries as activeModelId."""
    registered = request.query_params.get("model", "").strip()
    resolved = _resolve_registered_model(registered)
    if resolved is None:
        return JSONResponse({"error": f"{registered!r} is not a pinned remote model"}, status_code=404)
    provider, real_id = resolved

    payload: dict = {"provider": provider, "registered_id": registered, "real_id": real_id}

    if provider == "openrouter":
        spend: dict = {}
        try:
            if OPENROUTER_SPEND_FILE.exists():
                spend = json.loads(OPENROUTER_SPEND_FILE.read_text(encoding="utf-8")).get(registered) or {}
        except Exception:
            spend = {}
        payload.update({
            "kind": "spend",
            "cost": round(float(spend.get("cost", 0.0)), 6),
            "last_cost": spend.get("last_cost"),
            "requests": int(spend.get("requests", 0)),
            "prompt_tokens": int(spend.get("prompt_tokens", 0)),
            "completion_tokens": int(spend.get("completion_tokens", 0)),
            "tracked_since": "first relayed call since 2026-09-14",
        })
    else:
        usage_file = OPENCODE_USAGE_FILE if provider == "opencode" else NIM_USAGE_FILE
        c5, c24, total = _usage_counts(usage_file, registered)
        limit = _opencode_5h_limit(real_id) if provider == "opencode" else None
        payload.update({
            "kind": "requests",
            "count_5h": c5,
            "count_24h": c24,
            "count_total": total,
            "limit_5h": limit,
            "limit_is_estimate": provider == "opencode",
            "limit_known": limit is not None,
        })
    return JSONResponse(payload)


async def openrouter_usage_page(request: Request) -> Response:
    """Per-model spend, the one provider where "how much is gone" means money
    rather than a request count. Account-wide totals exist in OpenRouter's own
    API (/api/v1/credits) but per-model figures do not, so these are sums of
    the usage.cost blocks this relay has seen since 2026-09-14."""
    spend: dict = {}
    try:
        if OPENROUTER_SPEND_FILE.exists():
            spend = json.loads(OPENROUTER_SPEND_FILE.read_text(encoding="utf-8"))
    except Exception:
        spend = {}

    rows = []
    for entry in _read_pinned("openrouter"):
        registered = _registered_id("openrouter", entry["id"])
        s = spend.get(registered) or {}
        rows.append((entry["id"], entry.get("name") or entry["id"], s))
    rows.sort(key=lambda r: float(r[2].get("cost", 0.0)), reverse=True)

    total = sum(float(r[2].get("cost", 0.0)) for r in rows)
    body = "".join(
        f'<tr><td>{escape(name)}<br><span class="zero">{escape(real)}</span></td>'
        f'<td class="num">${float(s.get("cost", 0.0)):.6f}</td>'
        f'<td class="num">{"$" + format(float(s["last_cost"]), ".6f") if s.get("last_cost") is not None else "—"}</td>'
        f'<td class="num">{int(s.get("requests", 0))}</td>'
        f'<td class="num">{int(s.get("prompt_tokens", 0)) + int(s.get("completion_tokens", 0))}</td></tr>'
        for real, name, s in rows
    ) or '<tr><td colspan="5" class="zero">No pinned models yet.</td></tr>'

    html_doc = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="15">
<title>OpenRouter spend (local count)</title><style>{_USAGE_PAGE_CSS}</style></head><body>
<h1>OpenRouter spend (local count)</h1>
<p class="sub">What each pinned OpenRouter model has cost, summed from the
<code>usage.cost</code> block of every response relayed by this box.</p>
<div class="warn">OpenRouter reports account-wide credit usage over its API
(<code>GET /api/v1/credits</code>, currently 11 credits total) but publishes no
per-model figure, so these are locally accumulated totals rather than a query of
its ledger. Counting starts 2026-09-14, when <code>usage: {{include: true}}</code>
was first requested on relayed calls.</div>
<table><thead><tr><th>Model</th><th class="num">spent</th><th class="num">last msg</th>
<th class="num">requests</th><th class="num">tokens</th></tr></thead>
<tbody>{body}
<tr><td><b>all pinned models</b></td><td class="num"><b>${total:.6f}</b></td>
<td colspan="3"></td></tr>
</tbody></table>
<p class="foot">Stored in <code>{escape(str(OPENROUTER_SPEND_FILE))}</code>. Total is
kept indefinitely — trimming it on a timer would make a money figure decrease.
Page refreshes every 15s.</p>
</body></html>"""
    return Response(content=html_doc, media_type="text/html")


OPENROUTER_CREDITS_URL = "https://openrouter.ai/api/v1/credits"
_openrouter_credits_cache: dict = {"data": None, "at": 0.0}
OPENROUTER_CREDITS_TTL = 60


async def _openrouter_account_usage() -> dict | None:
    """Account-wide credits straight from OpenRouter's own API — the one usage
    figure that provider actually publishes (total_credits / total_usage, in
    USD). Cached for 60s: the right-bar panel polls usage every 4s, and this
    is the one call in that fetch that leaves the box. On failure returns the
    last known good value with its cache entry kept, so a flaky response turns
    into a stale number rather than a disappearing line."""
    now = time.monotonic()
    cached = _openrouter_credits_cache["data"]
    if cached is not None and now - _openrouter_credits_cache["at"] < OPENROUTER_CREDITS_TTL:
        return cached
    key_path = SECRET_FILES["openrouter"]
    try:
        if not key_path.exists() or key_path.stat().st_size == 0:
            return cached
        key = key_path.read_text(encoding="utf-8").strip()
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                OPENROUTER_CREDITS_URL, headers={"Authorization": f"Bearer {key}"}, timeout=15
            )
            resp.raise_for_status()
        data = resp.json().get("data") or {}
        payload = {
            "total_credits": float(data.get("total_credits") or 0.0),
            "total_usage": float(data.get("total_usage") or 0.0),
            "fetched_at": time.time(),
        }
        _openrouter_credits_cache["data"] = payload
        _openrouter_credits_cache["at"] = now
        return payload
    except Exception as exc:
        if cached is not None:
            print(f"[openrouter credits] stale cache served ({exc})", file=sys.stderr)
        return cached


def _provider_usage_summary(provider: str) -> dict:
    """Aggregate over that provider's pinned models, for the right-bar rows.

    The chat-form badge is per model; this is the whole-provider picture the
    section rows want. NIM's retention (30d) and OpenCode's (10h) differ, and
    only OpenCode has any cap estimate to name — so the response carries the
    raw windows and lets the UI decide what to say."""
    ids = [(e["id"], _registered_id(provider, e["id"])) for e in _read_pinned(provider)]
    if provider == "openrouter":
        spend: dict = {}
        try:
            if OPENROUTER_SPEND_FILE.exists():
                spend = json.loads(OPENROUTER_SPEND_FILE.read_text(encoding="utf-8"))
        except Exception:
            spend = {}
        cost = 0.0
        requests = tokens = 0
        for _, reg in ids:
            s = spend.get(reg) or {}
            cost += float(s.get("cost", 0.0))
            requests += int(s.get("requests", 0))
            tokens += int(s.get("prompt_tokens", 0)) + int(s.get("completion_tokens", 0))
        return {"kind": "spend", "cost": round(cost, 6), "requests": requests, "tokens": tokens}

    usage_file = OPENCODE_USAGE_FILE if provider == "opencode" else NIM_USAGE_FILE
    c5 = c24 = total = 0
    for _, reg in ids:
        a, b, c = _usage_counts(usage_file, reg)
        c5 += a
        c24 += b
        total += c
    return {
        "kind": "requests",
        "count_5h": c5,
        "count_24h": c24,
        "count_total": total,
        "total_window": "10h" if provider == "opencode" else "30d",
        "limit_known": provider == "opencode",
    }


async def usage_summary(request: Request) -> JSONResponse:
    """Everything the right-bar's provider sections display, in one call.

    Per provider: the aggregate of that provider's own kind of usage — local
    request totals (OpenCode, NVIDIA NIM: neither publishes usage over their
    API) and local spend sums plus the account-wide credit figure OpenRouter
    does publish."""
    providers = {}
    for name in ("opencode", "nim", "openrouter"):
        summary = _provider_usage_summary(name)
        summary["pinned"] = len(_read_pinned(name))
        providers[name] = summary
    return JSONResponse({
        "providers": providers,
        "openrouter_account": await _openrouter_account_usage(),
        "note": (
            "opencode/nim figures are counts collected locally by this panel; "
            "openrouter_account is OpenRouter's own account-wide USD figure."
        ),
    })


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


# Last-used permission mode for the chat's tool-approval gate. One small JSON
# file whose shape is {"mode": "manual"|"accept-edits"|"auto"|"plan"}, so the
# llama.cpp web UI restores the previous combobox choice across reloads.
# Same file/layout pattern as the *_pinned_files above — a fixed known path;
# the endpoint never takes an arbitrary path, only mode strings.
PERMISSION_MODE_FILE = Path("/home/murad/.config/mcp-secrets/panel-permission-mode.json")
PERMISSION_MODES = ("manual", "accept-edits", "auto", "plan")


def _read_permission_mode() -> str | None:
    if not PERMISSION_MODE_FILE.exists():
        return None
    try:
        mode = json.loads(PERMISSION_MODE_FILE.read_text(encoding="utf-8")).get("mode")
    except Exception:
        return None
    return mode if mode in PERMISSION_MODES else None


async def permission_mode_get(request: Request) -> JSONResponse:
    mode = _read_permission_mode()
    # 200 even when unset — "no preference" is not an error; the UI keeps its
    # own default (Accept Edits).
    return JSONResponse({"mode": mode})


async def permission_mode_save(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "expected JSON body"}, status_code=400)

    mode = body.get("mode")
    if mode not in PERMISSION_MODES:
        return JSONResponse({"error": f"unknown mode {mode!r}"}, status_code=400)

    try:
        PERMISSION_MODE_FILE.parent.mkdir(parents=True, exist_ok=True)
        PERMISSION_MODE_FILE.write_text(
            json.dumps({"mode": mode}) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

    return JSONResponse({"saved": True, "mode": mode})


# ---------- files server: pending writes (human-approve flow) ----------

# State file mirrored by MCP/files/server.py on every pending-write
# mutation, shared across processes. The MCP server's own confirm_write
# tool exists for batch/headless use, but the intended human flow is:
# model stages a write -> user clicks in the right-bar -> this backend
# performs the atomic rename. Shape per handle:
#   {path, staged, size, chunks, created}
FILES_PENDING_STATE = Path("/home/murad/.local/state/mcp-files/pending.json")


def _read_files_pending() -> dict:
    try:
        data = json.loads(FILES_PENDING_STATE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}   # missing/torn file: report empty, never crash panel


async def files_pending(request: Request) -> JSONResponse:
    """List in-flight (staged or buffering) write handles from the shared
    state file the `files` MCP server mirrors. Read-only view for the UI
    — never performs the filesystem rename itself."""
    pending = _read_files_pending()
    rows = [
        {
            "handle": h,
            "path": e.get("path"),
            "staged": e.get("staged") or None,
            "size": e.get("size"),
            "chunks": e.get("chunks"),
            "created": e.get("created"),
        }
        for h, e in pending.items()
    ]
    rows.sort(key=lambda r: r.get("created") or 0, reverse=True)
    return JSONResponse({"pending": rows, "count": len(rows)})


async def files_write_action(request: Request) -> JSONResponse:
    """POST /api/files/{handle}/{action} — confirm or abort a pending
    write for the human's right-bar button. Only these two verbs; no
    arbitrary paths: this backend reads paths from the (mirrored) state
    file, not from the request, and re-validates against the same
    ALLOWED_ROOTS contract before touching the destination."""
    handle = request.path_params["handle"]
    action = request.path_params["action"]
    if action not in ("confirm", "abort"):
        return JSONResponse({"error": "unknown action"}, status_code=404)

    # Read action fresh per call — a mode flip lands immediately.
    pending = _read_files_pending()
    entry = pending.get(handle)
    if entry is None:
        return JSONResponse({"error": "unknown handle (restart or already done?)"}, status_code=404)

    staged = Path(entry.get("staged") or "")
    final = Path(entry.get("path") or "")
    if not str(staged) or not str(final):
        return JSONResponse({"error": "entry missing paths"}, status_code=400)

    # Same root check as the MCP server uses — no trust boundary skipped.
    # commonpath is the sibling-proof check (a plain startswith would let
    # /GitHubEvil through as if it were /GitHub).
    def inside_roots(p: Path) -> bool:
        try:
            return any(
                os.path.commonpath([str(p), root]) == root
                for root in ("/home/murad/Documents/GitHub", "/home/murad/Downloads")
            )
        except ValueError:   # disjoint paths: fail closed
            return False
    if not (inside_roots(staged) and inside_roots(final)):
        return JSONResponse({"error": "staged/final path outside allowed roots"}, status_code=403)

    if action == "abort":
        try:
            staged.unlink(missing_ok=True)
        except OSError as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)
        pending.pop(handle, None)
        _write_files_state(pending)
        return JSONResponse({"aborted": True, "handle": handle})

    # confirm
    if not staged.is_file():
        pending.pop(handle, None)
        _write_files_state(pending)
        return JSONResponse({"error": "staged file vanished"}, status_code=409)
    try:
        os.replace(staged, final)
        _write_files_state(pending)
    except OSError as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse({"confirmed": True, "handle": handle, "wrote": str(final)})


def _write_files_state(pending: dict) -> None:
    """Write the (mutated) pending state back, atomically, best-effort:
    a UI-confimr happens on the human's click, so a failed mirror write
    leaving one stale row is recoverable by the next refresh."""
    try:
        FILES_PENDING_STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = FILES_PENDING_STATE.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(pending, f, indent=1)
        os.replace(tmp, FILES_PENDING_STATE)
    except OSError:
        pass


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


async def _openrouter_catalog_ids() -> set[str] | None:
    """The set of currently-live OpenRouter model ids, from the same cache
    openrouter_models() already fills. Returns None rather than raising when
    the catalog cannot be fetched — a pin add must never hang or hard-fail on
    this, so the caller treats None as "validation not possible, allow it".
    On an empty/very-fresh cache the browse page's own fetch wouldn't have
    happened yet, so one cheap on-demand request makes the check available
    at all times instead of only right after someone opened the browse page."""
    try:
        if _openrouter_cache["data"] is None:
            async with httpx.AsyncClient() as client:
                resp = await client.get(OPENROUTER_MODELS_URL, timeout=15)
                resp.raise_for_status()
                _openrouter_cache["data"] = resp.json()
                _openrouter_cache["at"] = time.monotonic()
        rows = _openrouter_cache["data"].get("data", [])
        return {m["id"] for m in rows if isinstance(m, dict) and m.get("id")}
    except Exception:
        return None


async def _nim_catalog_ids() -> set[str] | None:
    """NIM's live model ids, from the same cache nim_models() fills. NVIDIA
    retires models on a schedule and then answers HTTP 410 Gone forever after
    (nvidia/nemotron-3-nano-30b-a3b went that way on 2026-09-01 while still
    pinned here), so membership in this list is what separates a usable pin
    from a guaranteed 410. None means "could not check", never "empty"."""
    try:
        if _nim_cache["data"] is None:
            key_path = SECRET_FILES["nim"]
            if not key_path.exists() or key_path.stat().st_size == 0:
                return None  # no key saved: cannot check, same as unreachable
            key = key_path.read_text(encoding="utf-8").strip()
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    NIM_MODELS_URL, headers={"Authorization": f"Bearer {key}"}, timeout=15
                )
                resp.raise_for_status()
                _nim_cache["data"] = resp.json()
                _nim_cache["at"] = time.monotonic()
        rows = _nim_cache["data"].get("data", [])
        return {m["id"] for m in rows if isinstance(m, dict) and m.get("id")}
    except Exception:
        return None


def _split_id_suffix(model_id: str) -> tuple[str, str]:
    """("z-ai/glm-5.2:free") -> ("z-ai/glm-5.2", ":free"); an id with no
    suffix keeps the whole string and an empty suffix."""
    if ":" not in model_id:
        return model_id, ""
    base, suffix = model_id.split(":", 1)
    return base, f":{suffix}"


async def _validate_pin_request(provider: str, model_id: str) -> tuple[bool, str]:
    """Reject a pin we already know is uncallable, warn on one that merely
    looks risky. Returns (allowed, message). Deliberately NOT the strictest
    possible check: ids with a ":free"/":batch"-style suffix are real and
    usable even when they have no row of their own in the catalog (see
    OpenRouterPage.svelte's manual-id input), so anything that cannot be
    confirmed for sure is allowed rather than blocked — this exists to stop
    the exact class of stale pin that accumulated (z-ai/glm-5.2:free,
    z-ai/glm-5.3-flash:batch), not to police ids that might be wrong."""
    base, suffix = _split_id_suffix(model_id)

    # Batch-only models are never reachable over chat/completions, always
    # answering HTTP 404 "only available through the Batch API" — there is no
    # retry or switch worth doing, so this is the one case that hard-rejects.
    if suffix == ":batch":
        return False, (
            f"{model_id} is only reachable via OpenRouter's Batch API (/api/beta/batches) "
            "and can never answer a chat request. Pin the non-batch id instead."
        )

    if provider == "openrouter":
        known = await _openrouter_catalog_ids()
        if known is None:
            pass  # catalog unreachable — skip validation rather than block a real pin
        elif model_id not in known and base not in known:
            return False, (
                f"{model_id!r} is not in OpenRouter's current model catalog (checked "
                f"{len(known)} ids). OpenRouter's own error for it would be HTTP 404."
            )
        elif suffix and model_id not in known:
            # The base is live but this exact suffixed id has no catalog row of
            # its own. Some suffixed ids are real aliases that just are not
            # listed separately, so this cannot hard-reject (see this
            # function's docstring). But it is also the exact shape of the
            # stale pin that sat in the dropdown failing on every call:
            # z-ai/glm-5.2:free answered HTTP 404 "This model is unavailable
            # for free" while its paid base stayed live in the catalog. So it
            # is allowed only with a warning that says this, not a clean one.
            return True, (
                f"Pinned, but {model_id} is not itself listed in OpenRouter's "
                f"catalog — only the base id {base} is. Suffix ids in this shape "
                "are sometimes live aliases and sometimes stale ones "
                "(z-ai/glm-5.2:free was exactly this and 404s on every chat "
                "request). Send one small test message on it before relying on it."
            )

    if provider == "nim":
        known = await _nim_catalog_ids()
        if known is not None and model_id not in known:
            return False, (
                f"{model_id!r} is not in NVIDIA's current NIM catalog (checked {len(known)} "
                "ids). NIM answers HTTP 410 Gone for retired models — that is how "
                "nvidia/nemotron-3-nano-30b-a3b became uncallable on 2026-09-01."
            )
        return True, ""

    if suffix == ":free":
        return True, (
            "Pinned. Free-tier models on OpenRouter are shared upstream capacity: "
            "expect intermittent 429 (rate-limited upstream) and 502/503 "
            "(provider overloaded) responses, especially on large models."
        )
    return True, ""


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

    allowed, note = await _validate_pin_request(provider, model_id)
    if not allowed:
        return JSONResponse({"error": note}, status_code=400)

    entries = _read_pinned(provider)
    if not any(e["id"] == model_id for e in entries):
        entries.append({"id": model_id, "name": name})
        _write_pinned(provider, entries)
    await _reload_router()
    result: dict = {"pinned": entries}
    if note:
        result["warning"] = note
    return JSONResponse(result)


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


# Learned per-remote-model context-window limits. The web UI's context gauge
# reads llama-server's /props -> default_generation_settings.n_ctx, but for a
# pinned remote model that always came back 0 (see router_props below), so the
# gauge had nothing to show while the provider enforced its own much smaller
# window — and that real limit was only ever revealed by a token-limit-sized
# chat request failing with an upstream error shaped like:
#     "exceeded model token limit: 262144 (requested: 502873)"
# (observed 2026-09-16 on Console Go). Every such rejection now records the
# provider's announced limit next to the model's *registered* id, and
# router_props() answers /props for that model with the recorded n_ctx, so
# the gauge turns red based on the real effective limit instead of staying
# silent until a request is already too big to send. Max-wins across
# sightings: if a provider ever reports two different numbers for one model
# (model upgraded, temporary config change) the larger one can only make the
# gauge more optimistic by an amount a real rejection immediately corrects,
# while the smaller number would permanently underestimate and block a
# conversation that has actually grown larger than the first, wrong limit.
CONTEXT_LIMITS_FILE = Path("/home/murad/.config/mcp-secrets/remote-context-limits.json")


def _load_context_limits() -> dict[str, int]:
    try:
        data = json.loads(CONTEXT_LIMITS_FILE.read_text())
        return {str(k): int(v) for k, v in data.items() if isinstance(v, (int, float)) and v > 0}
    except FileNotFoundError:
        return {}
    except Exception as exc:  # corrupt file is never worth losing relays over
        print(f"[context-limits] unreadable {CONTEXT_LIMITS_FILE}: {exc}", file=sys.stderr)
        return {}


def _record_context_limit(registered_id: str, limit: int) -> None:
    """Persist one learned limit for the router-registered model id. Atomic
    enough for a single-writer process; reads-then-merges rather than
    clobbering so concurrent sightings can't erase each other."""
    if limit <= 0:
        return
    limits = _load_context_limits()
    if limits.get(registered_id) == limit:
        return
    limits[registered_id] = max(limit, limits.get(registered_id, 0))
    CONTEXT_LIMITS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONTEXT_LIMITS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(limits, indent=2, sort_keys=True))
    tmp.replace(CONTEXT_LIMITS_FILE)


def _context_limit_for(registered_id: str) -> int | None:
    return _load_context_limits().get(registered_id)


def _parse_token_limit_error(error_body: bytes) -> tuple[int, int] | None:
    """OpenCode Go words an overflow as
    "exceeded model token limit: 262144 (requested: 315279)" — extract
    (limit, requested) so both the downstream-enriched message and the
    learned-limits store use the numbers upstream itself announced, rather
    than copy-pasting them into code that drifts the first time a provider
    changes its window."""
    try:
        parsed = json.loads(error_body)
        msg = str(parsed["error"].get("message", ""))
        m = re.search(r"token limit: (\d+) \(requested: (\d+)\)", msg)
        if m:
            return int(m.group(1)), int(m.group(2))
    except Exception:
        pass
    return None


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


def _stable_conversation_session_id(messages: list) -> str:
    """A stable-per-conversation id, derived rather than passed in: the web
    UI's own /v1/chat/completions body has no conversation-id field to reuse
    (that only lives in the browser URL, which never reaches this backend),
    and every message in a conversation resends the full history including
    message [0] unchanged, so hashing it gives the same id on every turn of
    the same conversation without any frontend wiring. Required by OpenCode
    Go specifically (see PROVIDERS["opencode"]'s caller) — its docs
    (opencode.ai/docs/go/#where-can-i-use-it) ask for "a stable session ID
    ... for each conversation so we can optimize routing and prompt caching"
    and reject requests missing the header outright, not just deprioritize
    them."""
    first = messages[0] if messages else {}
    return hashlib.sha256(json.dumps(first, sort_keys=True).encode()).hexdigest()[:32]


def _sanitize_history(messages: list) -> list:


    """Repairs the two history shapes Console Go's strict models hard-reject
    (kimi-k2.7-code, observed 2026-09-16 on a 569-message conversation that
    glm-5.3-flash accepted, so this is about strictness, not corruption by us):

    - an assistant message whose content is empty and carries no tool_calls —
      the residue of a generation that was cut off by an upstream error and
      saved as an empty reply (Go answers: "the message at position N with
      role 'assistant' must not be empty");
    - an assistant message holding tool_calls that were never answered by
      tool messages before the next assistant turn ("tool_call_ids did not
      have response messages: read_file:180").

    Everything else passes through byte-for-byte. An assistant left with
    nothing but unanswered calls is dropped only if its content is empty too,
    so a real assistant turn is never thrown away — its tool_calls field is
    just stripped, which is exactly what the error is asking for.
    Returns a NEW list; the caller compares against the original to decide
    whether a retry would change anything at all.
    """

    out: list = []
    i = 0
    n = len(messages)
    while i < n:
        m = messages[i]
        i += 1
        if not isinstance(m, dict):

            out.append(m)
            continue

        role = m.get("role")
        content = m.get("content")
        empty = content is None or (isinstance(content, str) and not str(content).strip())
        calls = m.get("tool_calls") if role == "assistant" else None

        # Case 1: plain empty assistant reply, nothing usable in it.
        if role == "assistant" and empty and not (isinstance(calls, list) and bool(calls)):
            continue

        # Case 2: assistant with tool_calls — pair them against the tool run
        # that immediately follows (upstream accepts nothing else).
        if role == "assistant" and isinstance(calls, list) and bool(calls):
            j = i
            answered: set[str] = set()
            while j < n and isinstance(messages[j], dict) and messages[j].get("role") == "tool":
                tid = messages[j].get("tool_call_id")
                if tid is not None:
                    answered.add(str(tid))
                j += 1

            def _cid(c: object) -> str:
                return str(c.get("id")) if isinstance(c, dict) and c.get("id") is not None else ""

            all_ids = {_cid(c) for c in calls if _cid(c)}
            kept_ids = {i_ for i_ in all_ids if i_ in answered}
            tool_block = [t for t in messages[i:j] if isinstance(t, dict) and str(t.get("tool_call_id")) in kept_ids]

            if kept_ids == all_ids:
                # untouched pair — append verbatim, tool run included
                out.append(m)
                out.extend(messages[i:j])
            elif kept_ids:
                out.append({**m, "tool_calls": [c for c in calls if _cid(c) in kept_ids]})
                out.extend(tool_block)
            elif not empty:
                # still a real assistant turn; drop the unanswered field only
                out.append({k: v for k, v in m.items() if k != "tool_calls"})
            i = j  # the tool block is either consumed or intentionally dropped
            continue

        out.append(m)
    return out


# Per-provider extra headers beyond the universal Authorization/Content-Type
# below. Only OpenCode needs anything here today (see
# _stable_conversation_session_id's docstring) — kept as a lookup rather
# than an if-provider-== branch so a future provider's own quirks don't have
# to touch this function's main body.
def _extra_headers(provider: str, messages: list) -> dict:
    if provider == "opencode":
        return {
            "x-opencode-session": _stable_conversation_session_id(messages),
            # OpenCode's docs explicitly ask clients to send a real
            # identifying user agent "rather than a generic SDK or
            # HTTP-library name" (httpx's own default UA is exactly that
            # generic case) — this is that identifying string, not
            # decorative.
            "User-Agent": "llama-cpp-panel/1.0 (+https://github.com/MmuradC/llama-cpp-integrations)",
        }
    return {}


# Upstream providers disagree about empty assistant messages: Console Go
# (kimi-k2.7-code) answers HTTP 400 "the message at position 110 with role
# 'assistant' must not be empty" while glm-5.3-flash accepts the exact same
# history. Empty assistant messages are not a caller's intent — they are the
# residue of a generation that was interrupted (the web UI saves an assistant
# message with nothing when a stream dies, e.g. the SSL mismatch that used to
# leave a truncated turn) and GLM forgiving it is why the user only saw the
# breakage the day they picked kimi. Filling with a single space instead of
# seeing which model it is is deliberate: patching the OUTGOING copy only
# (the saved history is untouched), and it is semantically a no-op — no other
# field on that message changes, only whitespace in place of whitespace. If
# a provider ever rejects an empty USER message (which would be a real
# malformed request) this does not touch it: the empty-fill is assistant-only.
def _sanitize_messages(messages: list) -> list:
    cleaned = []
    for msg in messages:
        if (
            isinstance(msg, dict)
            and msg.get("role") == "assistant"
            and isinstance(msg.get("content"), str)
            and not msg["content"].strip()
        ):
            msg = {**msg, "content": " "}
        cleaned.append(msg)
    return cleaned


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

    upstream_body: dict = {"model": real_model, "messages": _sanitize_messages(messages), "stream": True}
    for field in _PASSTHROUGH_FIELDS:
        if field in body:
            upstream_body[field] = body[field]

    # Ask OpenRouter for the usage block on the final stream chunk (see the
    # relay's _record_openrouter_spend call for why). Harmless for the other
    # two providers, but only sent to the one that documents it.
    if provider == "openrouter":
        upstream_body["usage"] = {"include": True}

    tools_in = body.get("tools")
    tool_names = [t.get("function", {}).get("name") for t in tools_in] if isinstance(tools_in, list) else None
    print(f"[{provider} relay] -> {real_model!r}: {len(messages)} messages, tools={tool_names}", file=sys.stderr)

    def _fresh_request(payload: dict | None = None) -> tuple[httpx.AsyncClient, httpx.Request]:
        client = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=15.0))
        req = client.build_request(
            "POST",
            provider_cfg["chat_url"],
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                **_extra_headers(provider, messages),
            },
            json=upstream_body if payload is None else payload,
        )
        return client, req

    # One mandatory logging point plus exactly one fresh-client retry on this
    # branch. Everything that raises before a response exists at all — DNS,
    # connect, and stray TLS alerts (observed once as
    # SSLV3_ALERT_BAD_RECORD_MAC mid-session, around a 55-message
    # opencode/deepseek-v4.1-flash conversation, at a moment the journal had
    # no code in common with the surrounding successful calls) — is specific
    # to one connection attempt, not a real server decision. This used to be
    # a bare except that returned a 502 and logged nothing, which is why such
    # a failure produced a user-visible error dialog with nothing at all to
    # correlate against in the journal. Now it is logged every time, with the
    # full traceback, and retried once on a genuinely new connection (a new
    # client, not a pooled one — a bad TLS session state on a socket is the
    # thing being retried, so reusing the connection would defeat the point).
    # After two attempts it gives up and still returns a real _openai_error,
    # so the web UI keeps showing its normal error dialog rather than a bare
    # disconnect.
    client, req = _fresh_request()
    attempt = 0
    while True:
        attempt += 1
        try:
            upstream = await client.send(req, stream=True)
            break
        except Exception as exc:
            print(
                f"[{provider} relay] {real_model!r} connection attempt {attempt} failed: "
                f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}",
                file=sys.stderr,
            )
            await client.aclose()
            if attempt >= 2:
                return _openai_error(str(exc)[:200], 502)
            client, req = _fresh_request()

    if upstream.status_code == 200 and provider in _USAGE_RECORDERS:
        _USAGE_RECORDERS[provider](model)

    # Every rejection that arrives before one byte of the stream exists is a
    # pre-flight decision. A few of them describe the request rather than the
    # service — a knob a model pins to a fixed value, a replayed history that
    # strict models audit line by line — and are repairable on the spot, so
    # they go through this single bounded loop instead of each growing its own
    # custom retry (which is how a stale error_body made consecutive repairs
    # invisible: the temperature retry fixed one thing, the loop never saw the
    # second error, and a repairable history rejection showed up as a
    # temperature error). Anything the upstream message does not name is
    # passed to the UI untouched.
    if upstream.status_code != 200:
        repair_attempts = 0
        repairs_accumulate = upstream_body  # repairs build on each other
        # Bound BEFORE the repair loop, not inside it. `error_body` used to be
        # assigned only in this loop's body, which runs for 400s alone - so the
        # log line further down ("rejected before streaming") hit an unbound
        # name for every other rejection (429 rate limit, 502/503 upstream
        # overload, 401/403 auth) and raised UnboundLocalError *inside the
        # handler*. The request died with no response at all and the web UI
        # could only report its generic "Server temporarily unavailable",
        # while the real reason - the one this line exists to record - was
        # never written to the journal. Reproduced with a stubbed 429 before
        # fixing.
        #
        # One read per rejection: here for the first one (which may not even be
        # a 400, so it never enters the loop), plus the guarded re-read at the
        # end of each retry below. Every decision and the final log therefore
        # describe the rejection actually in hand, never a previous one.
        error_body = await upstream.aread()
        while upstream.status_code == 400 and repair_attempts < 3:
            await upstream.aclose()
            await client.aclose()
            repair_attempts += 1

            repaired_body: dict | None = None
            note = ""
            if (
                provider == "opencode"
                and b"temperature" in error_body
                and "temperature" in repairs_accumulate
            ):
                # Some OpenCode Go models pin temperature to exactly 1 and
                # answer "invalid temperature: only 1 is allowed for this
                # model" for anything else (kimi-k2.7-code; confirmed
                # 2026-09-15). The web UI always attaches its own setting, so
                # without this every message fails on a knob the user never
                # turned. Retried with the field absent — the provider's error
                # is literally asking for that. Checked against the body this
                # retry will actually send, not the original request: after a
                # prior repair removed the field, reading upstream_body here
                # re-triggered this branch anyway, produced a body identical
                # to the one that had already failed, and burned the third
                # and final attempt resending a known-doomed request.
                repaired_body = {k: v for k, v in repairs_accumulate.items() if k != "temperature"}
                note = "temperature"

            if repaired_body is None and provider == "opencode" and (
                b"role 'assistant' must not be empty" in error_body
                or b"must be followed by tool messages" in error_body
            ):
                # Console Go audits the *replayed history* itself: a
                # conversation that once suffered a cut-off generation or a
                # stray tool call stays broken for strictly parsed models
                # forever, while a lenient one (glm-5.3-flash, within the same
                # hour) accepted the very same body. Repair the shape once and
                # resend. Headers are unchanged (the OpenCode session id is
                # derived from the original messages, which is what keeps the
                # conversation cacheable upstream). Sanitize the history that
                # is about to go out, not the caller's original: after any
                # earlier repair the outgoing list is already different, and
                # re-sanitizing the untouched original would resurrect the
                # first attempt's shape.
                candidate = repairs_accumulate.get("messages", messages)
                sanitized = _sanitize_history(candidate)
                if sanitized != candidate:
                    repaired_body = {**repairs_accumulate, "messages": sanitized}
                    note = f"history ({len(candidate)} -> {len(sanitized)} messages)"
                else:
                    print(
                        f"[{provider} relay] {real_model!r} rejected the history shape "
                        f"but nothing repairable was found in the outgoing body",
                        file=sys.stderr,
                    )

            if repaired_body is None:
                break
            repairs_accumulate = repaired_body  # next repair builds on this
            print(
                f"[{provider} relay] {real_model!r} rejected {note}; retrying ({repair_attempts})",
                file=sys.stderr,
            )
            client, req = _fresh_request(repaired_body)
            try:
                upstream = await client.send(req, stream=True)
            except Exception as exc:
                print(
                    f"[{provider} relay] repair retry failed to connect: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
                break
            if upstream.status_code == 200 and provider in _USAGE_RECORDERS:
                # the recording block above looked at the first attempt's
                # status, which was the 400 that triggered the repair — a
                # repaired success would otherwise go uncounted
                _USAGE_RECORDERS[provider](model)
            if upstream.status_code != 200:
                # This attempt's own body, for the next decision and for the
                # log at the end. Guarded deliberately: a 200 here is the SSE
                # stream itself, and reading it would swallow the answer.
                error_body = await upstream.aread()

        if upstream.status_code != 200:
            await client.aclose()
            # used to be the one failure path with no logging at all —
            # the request never reached the streaming loop, so neither of the
            # other two log lines fired and a pre-stream rejection was
            # invisible no matter what the web UI's error dialog showed.
            print(f"[{provider} relay] {real_model!r} rejected before streaming: HTTP {upstream.status_code}: {error_body[:500]!r}", file=sys.stderr)
            error_body = _enrich_error_message(error_body)
            # A context-window overflow is not repairsable by resending: no
            # history sanitization shrinks tokens, so retrying just burns the
            # attempt while leaving the user staring at the raw upstream
            # string. The web UI's dialog only ever shows error.message, so
            # the honest, useful thing is to say what to do next in the same
            # sentence as what went wrong. Numbers parsed from upstream's own
            # message so the wording never becomes stale: OpenCode Go words
            # it "exceeded model token limit: 262144 (requested: 315279)".
            limits = _parse_token_limit_error(error_body)
            if limits:
                limit, _requested = limits
                # Teach the gauge: upstream only reveals its real window on a
                # rejection, so this is the cheapest moment it's ever known.
                try:
                    _record_context_limit(model, limit)
                except Exception:
                    pass  # recording must never change the relayed status
                try:
                    parsed = json.loads(error_body)
                    msg = str(parsed["error"].get("message", ""))
                    parsed["error"]["message"] = (
                        f"{msg}\n\n"
                        "This conversation is longer than this model's context window. "
                        "No request setting can fix that — start a new chat (or compact "
                        "this one in the UI), or pick a model with a larger context. "
                        "Tools off is NOT what saves you here: the history itself is too big."
                    )
                    error_body = json.dumps(parsed).encode()
                except Exception:
                    pass  # never turn a parse hiccup into a 500 of our own
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
                    # OpenRouter is the one provider that reports real account
                    # usage back to us: with "usage": {"include": true} in the
                    # request it puts a final chunk carrying
                    # usage.cost/prompt_tokens/completion_tokens just before
                    # [DONE]. That is the only place its per-message spend is
                    # ever available — /api/v1/credits is account-wide, not
                    # per model — so it is accumulated here for the gauge.
                    # Wrapped defensively: a shape change upstream must never
                    # take down the stream that is carrying the answer.
                    if provider == "openrouter" and isinstance(chunk, dict):
                        usage = chunk.get("usage")
                        if isinstance(usage, dict) and usage:
                            _record_openrouter_spend(model, usage)
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
    its own /props).

    n_ctx here is NOT hardcoded to 0: for a local llama-server model this
    handler never runs (the child answers its own /props), but for pinned
    remote models it is the only /props in the picture, and it is what feeds
    the web UI's context gauge via getModelContextSize(). A remote provider
    publishes its window only through "exceeded model token limit" errors,
    so if one has ever been seen for this id, replay it here (see
    CONTEXT_LIMITS_FILE) — otherwise the gauge stays empty and the user
    only learns the window exists by overflowing it at request time."""
    registered = str(request.query_params.get("model", ""))
    return JSONResponse({
        "default_generation_settings": {"params": {}, "n_ctx": _context_limit_for(registered) or 0},
        "model_path": "",
        "model_alias": registered,
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
        Route("/api/nim/usage", nim_usage),
        Route("/api/provider-usage", provider_usage),
        Route("/api/usage-summary", usage_summary),
        Route("/api/usage-history", usage_history),
        Route("/api/opencode/models", opencode_models),
        Route("/api/opencode/usage", opencode_usage),
        Route("/nim-usage", nim_usage_page),
        Route("/opencode-usage", opencode_usage_page),
        Route("/openrouter-usage", openrouter_usage_page),
        Route("/api/{provider}/pinned", pinned_list),
        Route("/api/{provider}/pin", pinned_add, methods=["POST"]),
        Route("/api/{provider}/unpin", pinned_remove, methods=["POST"]),
        Route("/v1/chat/completions", remote_chat_completions, methods=["POST"]),
        Route("/props", router_props),
        Route("/api/secrets", secrets_status),
        Route("/api/secrets/{name}", secrets_save, methods=["POST"]),
        Route("/api/permission-mode", permission_mode_get, methods=["GET"]),
        Route("/api/permission-mode", permission_mode_save, methods=["POST"]),
        Route("/api/upload-image", upload_image, methods=["POST"]),
        Route("/api/rag/collections", rag_list, methods=["GET"]),
        Route("/api/rag/collections", rag_create, methods=["POST"]),
        Route("/api/rag/collections/{id}", rag_get, methods=["GET"]),
        Route("/api/rag/collections/{id}", rag_delete, methods=["DELETE"]),
        Route("/api/rag/collections/{id}/documents", rag_upload, methods=["POST"]),
        Route("/api/rag/collections/{id}/documents/{doc_id}", rag_delete_document, methods=["DELETE"]),
        Route("/api/files/pending", files_pending, methods=["GET"]),
        Route("/api/files/{handle}/{action}", files_write_action, methods=["POST"]),
        # must stay last: matches anything not matched above, see catch_all()
        Route("/{path:path}", catch_all, methods=["GET", "POST"]),
    ],
    middleware=[Middleware(CORSMiddleware, allow_origins=ALLOW_ORIGINS, allow_methods=["GET", "POST", "DELETE"])],
)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
