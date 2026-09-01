#!/usr/bin/env python3
"""Image generation with a GPU handover, for machines where the LLM and the
diffusion model cannot share the card.

An 8 GB GPU cannot hold a language model and a diffusion model at once, and
neither program knows the other exists. sd-server picks its backend once, at
startup, from whatever VRAM is free then — so if llama-server has a model
resident it silently falls back to CPU, and if llama-server *reloads* after
sd-server chose the GPU, sd-server dies with ErrorOutOfDeviceMemory mid-job.

This server closes that window by doing the whole cycle inside one tool call,
while the language model is idle waiting for the result:

    1. unload llama-server's models          -> VRAM freed
    2. restart sd-server if it was displaced -> it re-fits onto the free GPU
    3. generate                              -> runs on the GPU
    4. return

llama-server reloads its model by itself on your next message.

Configuration, all optional:
    SD_SERVER_URL       default http://127.0.0.1:1234
    LLAMA_SERVER_URL    default http://127.0.0.1:8080
    IMAGEGEN_OUTPUT     default ~/Downloads
    SD_SERVICE          systemd --user unit to restart, default sd-server
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from mcp.server.fastmcp import FastMCP

SD_URL = os.environ.get("SD_SERVER_URL", "http://127.0.0.1:1234").rstrip("/")
LLAMA_URL = os.environ.get("LLAMA_SERVER_URL", "http://127.0.0.1:8080").rstrip("/")
SD_SERVICE = os.environ.get("SD_SERVICE", "sd-server")
OUTPUT_DIR = Path(os.environ.get("IMAGEGEN_OUTPUT", str(Path.home() / "Downloads"))).expanduser()

# ceilings for a distilled turbo model; raise only if you swap in a normal one
MAX_STEPS = int(os.environ.get("IMAGEGEN_MAX_STEPS", "16"))
MAX_CFG = float(os.environ.get("IMAGEGEN_MAX_CFG", "2.0"))

mcp = FastMCP("imagegen")


def _request(url: str, payload: dict | None = None, timeout: int = 30) -> dict | list:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method="POST" if data else "GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    return json.loads(body) if body else {}


def _unload_llama() -> list[str]:
    """Unload every model llama-server has resident. Returns what was unloaded."""
    try:
        listing = _request(f"{LLAMA_URL}/models", timeout=10)
    except Exception:
        return []

    entries = listing.get("data", listing) if isinstance(listing, dict) else listing
    if not isinstance(entries, list):
        return []

    unloaded = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("id") or entry.get("name")
        raw = entry.get("status", "")
        # the router reports {"value": "loaded", ...}; older builds use a string
        state = str(raw.get("value", "") if isinstance(raw, dict) else raw).lower()
        if not name or not state or state == "unloaded":
            continue
        try:
            _request(f"{LLAMA_URL}/models/unload", {"model": name}, timeout=60)
            unloaded.append(name)
        except Exception:
            pass
    return unloaded


def _free_vram_mib() -> int | None:
    """Free VRAM on the first NVIDIA GPU, or None if it cannot be read."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        return int(out.stdout.strip().splitlines()[0])
    except Exception:
        return None


def _wait_for_vram(min_mib: int = 5000, timeout: int = 60) -> str:
    """Block until the GPU actually has room.

    An unload request returns as soon as llama-server accepts it, but the memory
    is not released until the model subprocess exits. Restarting sd-server in
    that gap makes it see an occupied GPU and commit to CPU for the whole
    session — a generation that then takes ten minutes instead of fifteen
    seconds. So wait for the memory to genuinely come back.
    """
    if _free_vram_mib() is None:
        time.sleep(5)  # cannot measure; give the unload a moment anyway
        return "waited 5s for vram"

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        free = _free_vram_mib()
        if free is not None and free >= min_mib:
            return f"{free} MiB free"
        time.sleep(1)
    return f"only {_free_vram_mib()} MiB free after {timeout}s"


def _sd_backend() -> str:
    """The backend sd-server chose at its last startup, from its journal."""
    try:
        out = subprocess.run(
            ["journalctl", "--user", "-u", SD_SERVICE, "--no-pager", "-n", "200"],
            capture_output=True, text=True, timeout=15, check=False,
        )
        for line in reversed(out.stdout.splitlines()):
            if "auto-fit:" in line:
                return line.split("auto-fit:", 1)[1].strip()
    except Exception:
        pass
    return ""


def _sd_ready(timeout: int = 90) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            _request(f"{SD_URL}/sdcpp/v1/capabilities", timeout=5)
            return True
        except Exception:
            time.sleep(1)
    return False


def _restart_sd() -> str:
    """Restart sd-server so it re-evaluates which backend fits."""
    try:
        subprocess.run(
            ["systemctl", "--user", "restart", SD_SERVICE],
            capture_output=True, text=True, timeout=120, check=False,
        )
    except Exception as exc:
        return f"could not restart {SD_SERVICE}: {exc}"
    return "restarted" if _sd_ready() else f"{SD_SERVICE} did not come back"


@mcp.tool()
def generate_image(
    prompt: str,
    negative_prompt: str = "",
    width: int = 768,
    height: int = 768,
    steps: int = 0,
    cfg_scale: float = 0.0,
    seed: int = -1,
    output_path: str = "",
) -> str:
    """Generate an image from a text prompt and save it as a PNG, returning the path.

    Put all the detail in the prompt — subject, style, lighting, composition.

    LEAVE steps AND cfg_scale AT 0. This is a distilled turbo model tuned for 8
    steps at cfg 1.0. The usual Stable Diffusion values (50 steps, cfg 7) are
    wrong here: they take roughly fifty times longer AND produce worse images,
    because turbo models degrade above cfg 1. Values outside the safe range are
    clamped.

    Larger sizes cost time roughly with pixel count: 512 is quickest, 768 is the
    default, 1024 is about four times 512. Only go above 768 if asked.
    """
    if not prompt.strip():
        return "Prompt is empty; nothing to generate."

    notes = []
    unloaded = _unload_llama()
    if unloaded:
        notes.append("unloaded " + ", ".join(n.split("/")[-1] for n in unloaded))
        # The unload call returns before the memory is actually released, so wait
        # for it — restarting sd-server too early makes it commit to CPU.
        notes.append(_wait_for_vram())
        # sd-server chose its backend at startup, when the GPU was still occupied,
        # so it must restart to notice the memory that just came free.
        notes.append(_restart_sd())

        # Refuse to start a CPU generation: it takes minutes rather than seconds
        # and there is no way to interrupt it once the request is in flight.
        backend = _sd_backend()
        if "diffusion=cpu" in backend:
            return (
                "Aborted — sd-server fell back to CPU, which would take many "
                "minutes per image.\n"
                f"backend: {backend}\n"
                f"free vram: {_free_vram_mib()} MiB\n"
                "Something else is holding the GPU. Check with nvidia-smi, then "
                f"retry, or restart it manually: systemctl --user restart {SD_SERVICE}"
            )
    elif not _sd_ready(timeout=10):
        return (
            f"sd-server is not responding at {SD_URL}.\n"
            f"Start it with:  systemctl --user start {SD_SERVICE}"
        )

    payload: dict = {
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "width": max(64, min(int(width), 2048)),
        "height": max(64, min(int(height), 2048)),
        "seed": int(seed),
        "batch_size": 1,
    }
    # A turbo model: 8 steps at cfg 1.0. Models reliably ask for 50/7 out of habit,
    # which is ~50x the work for a worse image, so clamp rather than obey.
    if steps > 0:
        capped = max(1, min(int(steps), MAX_STEPS))
        if capped != int(steps):
            notes.append(f"steps {int(steps)} -> {capped} (turbo model)")
        payload["steps"] = capped
    if cfg_scale > 0:
        capped_cfg = max(0.0, min(float(cfg_scale), MAX_CFG))
        if abs(capped_cfg - float(cfg_scale)) > 1e-6:
            notes.append(f"cfg {cfg_scale:g} -> {capped_cfg:g} (turbo model)")
        payload["cfg_scale"] = capped_cfg

    started = time.monotonic()
    try:
        result = _request(f"{SD_URL}/sdapi/v1/txt2img", payload, timeout=900)
    except urllib.error.HTTPError as exc:
        return f"sd-server returned {exc.code}: {exc.read().decode(errors='replace')[:400]}"
    except Exception as exc:
        return f"Generation failed: {exc}\n" + ("; ".join(notes) if notes else "")

    images = result.get("images") or []
    if not images:
        return f"sd-server returned no image. Keys: {list(result)}"

    if output_path:
        dest = Path(output_path).expanduser()
        if dest.suffix.lower() != ".png":
            dest = dest.with_suffix(".png")
    else:
        words = "".join(c if c.isalnum() or c.isspace() else " " for c in prompt).split()[:6]
        dest = OUTPUT_DIR / f"{'-'.join(w.lower() for w in words) or 'image'}-{int(time.time())}.png"

    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        dest.write_bytes(base64.b64decode(images[0]))
    except Exception as exc:
        return f"Generated the image but could not save it to {dest}: {exc}"

    used_seed = seed
    try:
        used_seed = json.loads(result.get("info", "{}")).get("seed", seed)
    except Exception:
        pass

    lines = [
        f"Saved {dest}",
        f"{payload['width']}x{payload['height']}, seed {used_seed}, "
        f"{dest.stat().st_size / 1024:.0f} KiB, {time.monotonic() - started:.1f}s",
    ]
    if notes:
        lines.append("GPU: " + "; ".join(notes) + " — llama-server reloads on your next message")
    return "\n".join(lines)


@mcp.tool()
def image_server_status() -> str:
    """Report whether the image server is up, and which GPU currently holds what."""
    lines = [f"sd-server: {SD_URL}", f"output: {OUTPUT_DIR}"]
    try:
        caps = _request(f"{SD_URL}/sdcpp/v1/capabilities", timeout=10)
        if isinstance(caps, dict) and "model" in caps:
            lines.append(f"model: {caps['model']}")
    except Exception as exc:
        lines.append(f"sd-server unreachable ({exc}) — systemctl --user start {SD_SERVICE}")

    try:
        listing = _request(f"{LLAMA_URL}/models", timeout=10)
        entries = listing.get("data", listing) if isinstance(listing, dict) else listing
        loaded = []
        for e in entries if isinstance(entries, list) else []:
            raw = e.get("status", "") if isinstance(e, dict) else ""
            state = str(raw.get("value", "") if isinstance(raw, dict) else raw).lower()
            if state and state != "unloaded":
                loaded.append(e.get("id") or e.get("name"))
        lines.append("llama-server holding: " + (", ".join(loaded) if loaded else "nothing"))
    except Exception:
        lines.append("llama-server: not reachable")

    return "\n".join(lines)


if __name__ == "__main__":
    print(f"[imagegen] sd={SD_URL} llama={LLAMA_URL} out={OUTPUT_DIR}", file=sys.stderr)
    mcp.run("stdio")
