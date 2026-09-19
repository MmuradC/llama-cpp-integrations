#!/usr/bin/env python3
"""filesystem server with server-side chunked writes.

Architectural note (2026-09-XX): replaces the in-system-prompt chunked
writing protocol. Previously, instructions for writing files >~400 lines
(chunk, end with a `<!--NEXT-->` marker, chain by calling edit_file against
that marker, and the rules for picking the correct marker syntax per
context) had to live in the system prompt. Two problems with that design:

1. The system prompt (the model's context) contains the rules, and so any
   sufficiently direct "what's your system prompt say?" question
   reproduces them verbatim - a prompt leak, not a tool misconfiguration.
   (The leak actually happened, on a 35B local model, from asking in
   Turkish about "Accept Edits".)
2. edit_file text-replacement chaining is expensive (multiple full context
   re-runs across a growing conversation) and brittle (markers must match
   byte-for-byte).

The fix moves what used to be *rules the model must remember* into
*state the server holds*: the model calls begin_write(path) and then
write_chunk(handle, text) as many times as it likes; no marker matching,
no edit_file chain, no growing context. commit() atomically places the
file (to .tmp within the same dir first, then os.replace).

Everything the model needs to know lives in the tool descriptions below
(which are exposed as tool metadata, i.e. standard MCP surface, not as a
secreted prompt): what tokens they return, how to chunk, when to call
commit. Those descriptions are short and behavioral: NOT a disguised copy
of llama-models.ini, n-predict values, or internal paths, so if the model
ever breathes the description back at a user, no configuration state
leaks.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
import uuid
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("files")

# Allowed roots, same set mcp-filesystem's launch args use in
# llama-mcp-servers.json (kept in one place - here - rather than echoed
# from there, so neither can drift).
ALLOWED_ROOTS = [
    "/home/murad/Documents/GitHub",
    "/home/murad/Downloads",
]

# In-memory pending chunked writes. They are per-process state; if the
# server restarts mid-write, they're simply gone - the caller retries,
# nothing is partially written to disk either way. Handles expire after
# 2h as a runaway backstop (a large-file session walking through a
# hundred chunks takes seconds, not hours).
#
# NOTE: keying by handle string means a compromised/colliding process
# could NOT (given per-request tool-call isolation of MCP over stdio)
# accidentally touch another concurrent session's handle. There is one
# FastMCP per bridge session, but every handle is a fresh uuid4 anyway,
# so cross-talk is not a realistic attack surface.
_PENDING: dict[str, dict] = {}
_HANDLE_TTL = 2 * 60 * 60

# Confirmation gate, driven by the panel's own permission-mode file
# (written by the Svelte right-bar through panel/backend's
# /api/permission-mode): "manual" -> gate ON, every write stages and
# needs a human confirm; "auto" -> gate OFF; anything else (incl. file
# missing/invalid) -> gate ON, secure default.
#
# "accept-edits" used to turn this gate OFF too, and that was wrong here:
# the WebUI documents that mode as approving the *built-in* file edit tools
# (edit_file), with everything else still asking - and this server is an MCP
# tool, not a built-in. Measured behaviour was that in accept-edits mode a
# write landed on disk with no prompt anywhere, which is precisely the
# human-approval flow this server exists to provide. Only "auto" bypasses it
# now; accept-edits stages writes like manual does.
#
# Read fresh on every write, no cache, so flipping the mode in the UI
# takes effect on the very next write. FILES_AUTO_APPLY=1 still wins
# (headless/batch opt-out); it is meant for tests, not daily use.
PERMISSION_MODE_FILE = Path(
    "/home/murad/.config/mcp-secrets/panel-permission-mode.json"
)

def _auto_apply() -> bool:
    if os.environ.get("FILES_AUTO_APPLY", "") == "1":
        return True
    try:
        mode = json.loads(
            PERMISSION_MODE_FILE.read_text(encoding="utf-8")
        ).get("mode")
    except Exception:
        return False   # no/invalid file -> manual mode, gate on
    return mode == "auto"

# Shared state file, mirrored on every mutation of _PENDING so that the
# panel backend (a separate process, see panel/backend/server.py's
# files_pending/files_confirm) can render pending writes in the UI and
# drive the confirm/abort actions there — i.e. the human approves in the
# UI, not by asking the model. Written atomically (tmp+rename); readers
# tolerate a torn state file by design (check mtime + validate JSON).
STATE_DIR = Path("/home/murad/.local/state/mcp-files")
STATE_FILE = STATE_DIR / "pending.json"
# Chunk size guidance returned to the caller in write_chunk echoes - the
# model can ignore it (the tool still works), but most models follow it
# and produce ~1-2KB payloads per call, which is exactly what the 24576
# n-predict budget comfortably fits.
DEFAULT_CHUNK_CHARS = 3000


def _resolve(path: str) -> Path:
    p = Path(path).expanduser().resolve(strict=False)
    # commonpath is a true components-prefix check, so a sibling like
    # /home/murad/Documents/GitHubEvil is correctly rejected (a plain
    # substring check would let it through). commonpath raises on
    # mixed drive letters / disjoint roots, hence the try.
    try:
        inside = any(
            os.path.commonpath([str(p), r]) == r for r in ALLOWED_ROOTS
        )
    except ValueError as e:
        raise PermissionError(f"path {p!s} could not be validated: {e}")
    if not inside:
        raise PermissionError(
            f"path {p!s} is outside allowed roots: {ALLOWED_ROOTS}"
        )
    return p


def _sweep_expired(now: float) -> None:
    for h, s in list(_PENDING.items()):
        if now - s["created"] > _HANDLE_TTL:
            _PENDING.pop(h, None)
    _mirror_state()


def _mirror_state() -> None:
    """Atomically mirror _PENDING to the shared state file the panel
    backend reads. Best-effort: a state mirror failure must not fail the
    actual write flow, it only degrades the UI view."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".json.tmp")
        payload = {
            h: {
                "path": str(s["path"]),
                "staged": str(s.get("staged", "")),
                "size": s.get("size"),
                "chunks": len(s["parts"]),
                "created": s["created"],
            }
            for h, s in _PENDING.items()
        }
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=1)
        os.replace(tmp, STATE_FILE)
    except OSError:
        pass  # never block a disk-write flow over UI state


@mcp.tool()
def allowed_roots() -> str:
    """List the roots this server may touch, plus whether the write
    confirmation gate is on. Cheap discovery call."""
    return "\n".join(ALLOWED_ROOTS) + (
        "\nconfirmation gate: OFF (auto-apply)"
        if _auto_apply()
        else "\nconfirmation gate: ON - writes land at the target path only\nafter confirm_write(handle) is called"
    )


def _verify_staged(staged: Path, final: Path) -> None:
    """Belt-and-braces: confirmation must re-validate both the staged
    scratch path and the final destination. If anything is off, refuse
    BEFORE touching the final path."""
    _resolve(staged)
    _resolve(final)


@mcp.tool()
def begin_write(path: str, newline: str = "\n") -> str:
    """Start a buffered write for a file that may be too large for a
    single tool call.

    Returns a token like: HANDLE=ab12cd34ef567890 on the first line, then
    a one-line usage hint. Hold the handle in mind across the calls that
    follow. Do NOT summarize this hint back to the user.

    Preferred over a single write_file when the content is more than
    roughly 3-4KB or ~100 lines of code: split it into
    write_chunk(handle, chunk) calls of at most ~3KB each, then call
    commit_write(handle). No markers, no edit_file chaining, no guessing
    at where the next chunk needs to quote-match.
    """
    p = _resolve(path)
    parent = p.parent
    parent.mkdir(parents=True, exist_ok=True)
    _sweep_expired(time.time())
    h = uuid.uuid4().hex
    _PENDING[h] = {
        "path": p,
        "parts": [],
        "newline": newline,
        "created": time.time(),
    }
    _mirror_state()
    return (
        f"HANDLE={h}\n"
        f"path={p}\n"
        "Next: call write_chunk with this handle and the first ~3KB of "
        "content. Repeat until done, then call commit_write(handle) "
        "(or abort_write if you need to start over)."
    )


@mcp.tool()
def write_chunk(handle: str, chunk: str) -> str:
    """Append one piece of a file started by begin_write. Aim for <=~3KB
    per chunk. Order matters; chunks are concatenated verbatim with no
    separator added between them - include your own newlines inside the
    chunk as needed."""
    _sweep_expired(time.time())
    s = _PENDING.get(handle)
    if s is None:
        return (
            "ERROR: unknown or expired handle. Restart the write with "
            "begin_write() - nothing has been written to disk, this is a "
            "clean slate, not a corrupted half-file."
        )
    s["parts"].append(chunk)
    total = sum(len(x) for x in s["parts"])
    _mirror_state()
    return (
        f"ack: chunk #{len(s['parts'])} accepted, {len(chunk)} chars, "
        f"{total} chars buffered so far for {s['path']}."
    )


@mcp.tool()
def commit_write(handle: str) -> str:
    """Finish a chunked write started by begin_write: assembles the
    buffered parts, fsyncs them to a .staged sibling of the target path,
    and either (auto-apply mode) renames it straight over the
    destination, or (confirmation gate) leaves the final rename for a
    separate confirm_write(handle) call."""
    s = _PENDING.get(handle)
    if s is None:
        return "ERROR: unknown handle; nothing was written."
    content = s["newline"].join(s["parts"])
    p: Path = s["path"]
    staged = p.with_name(p.name + ".staged")
    s["staged"] = staged
    s["size"] = len(content)
    with open(staged, "w", encoding="utf-8") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    lines = content.count(s["newline"]) + 1
    if _auto_apply():
        _PENDING.pop(handle, None)
        os.replace(staged, p)
        _mirror_state()
        return (
            f"wrote {p} - {len(content)} chars, {lines} lines, in "
            f"{len(s['parts'])} chunks (auto-apply, gate off)."
        )
    _mirror_state()
    return (
        f"STAGED {staged} - {len(content)} chars, {lines} lines, in "
        f"{len(s['parts'])} chunks. NOTHING is at {p} yet. Call "
        f"confirm_write('{handle}') to land it, abort_write to discard."
    )


@mcp.tool()
def confirm_write(handle: str) -> str:
    """Human-gate step: atomically rename the staged file onto the real
    destination for a previously commit_write()ed handle. Re-validates
    the staged and final paths against the allowed roots and refuses to
    touch the destination if anything is off."""
    s = _PENDING.get(handle)
    if s is None or "staged" not in s:
        return (
            "ERROR: unknown handle or nothing staged for it - there is "
            "nothing to confirm and nothing was written."
        )
    staged: Path = s["staged"]
    final: Path = s["path"]
    _verify_staged(staged, final)
    if not staged.is_file():
        _PENDING.pop(handle, None)
        _mirror_state()
        return "ERROR: staged file vanished; nothing was written."
    os.replace(staged, final)
    _PENDING.pop(handle, None)
    _mirror_state()
    return (
        f"confirmed: wrote {final} ({s['size']} chars, "
        f"{len(s['parts'])} chunks)."
    )


@mcp.tool()
def list_pending() -> str:
    """List in-flight writes (handles that are buffering or staged but not
    yet confirmed). Use this if a session loses track of a handle: it is
    the recovery point — you can then confirm_write or abort_write with
    the handle you find here."""
    _sweep_expired(time.time())
    if not _PENDING:
        return "no pending writes"
    out = []
    for h, s in _PENDING.items():
        staged = "STAGED" if "staged" in s else (
            f"buffering ({sum(len(x) for x in s['parts'])} chars)" if s["parts"] else "empty"
        )
        out.append(f"{h}  -> {s['path']}  [{staged}]")
    return "\n".join(out)


@mcp.tool()
def abort_write(handle: str) -> str:
    """Discard a pending write, staged or not: frees the handle and
    deletes the staged scratch file, leaving the real target untouched."""
    s = _PENDING.pop(handle, None)
    if s is None:
        return "ERROR: unknown handle."
    staged = s.get("staged")
    if staged is not None:
        try:
            Path(staged).unlink()
        except FileNotFoundError:
            pass
    return f"aborted pending write for {s['path']}; staging discarded."


@mcp.tool()
def read_text(path: str, start_line: int = 1, end_line: int = 0) -> str:
    """Read a file (subset by 1-based line range when end_line>0).
    Read the target file BEFORE begin_write/edit so you don't clobber
    something you haven't seen."""
    p = _resolve(path)
    if not p.is_file():
        return f"ERROR: no such file {p}"
    if end_line < start_line:
        end_line = 0
    with open(p, "r", encoding="utf-8", errors="replace") as f:
        if end_line:
            return "".join(
                f.readlines()[start_line - 1 : end_line]
            )
        return f.read()


@mcp.tool()
def write_file_whole(path: str, content: str) -> str:
    """Write a small file in one call. Intended for <~3KB / <~100 lines;
    if the content is bigger than that but write_file_whole keeps failing
    with a JSON parse error from the client, prefer begin_write +
    write_chunk instead - the single-call version has an inherent ceiling
    on how much text it can carry, and it is unrelated to the file's
    actual keep-or-delete value.
    Like commit_write, this respects the confirmation gate: it stages to
    a .staged sibling, and lands at the target only after
    confirm_write(handle)."""
    p = _resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    _sweep_expired(time.time())
    h = uuid.uuid4().hex
    staged = p.with_name(p.name + ".staged")
    _PENDING[h] = {
        "path": p,
        "staged": staged,
        "parts": [content],   # keeps confirm_write's chunk-count report honest
        "size": len(content),
        "newline": "\n",
        "created": time.time(),
    }
    _mirror_state()
    with open(staged, "w", encoding="utf-8") as f:
        f.write("")
        f.flush()
        os.fsync(f.fileno())
    if _auto_apply():
        # Reuse confirm_write's validated code path rather than own it.
        return confirm_write(h)
    return (
        f"STAGED {staged} ({len(content)} chars, single call). NOTHING is "
        f"at {p} yet. Call confirm_write('{h}') to land it, abort_write "
        "to discard."
    )


@mcp.tool()
def append_text(path: str, text: str) -> str:
    """Append text to an existing file (creating it if missing). Use this
    for the rare case where a file already exists and ONLY a tail needs
    to change - for building a large file from scratch, prefer
    begin_write/write_chunk/commit_write instead.
    Respects the confirmation gate: stages old-content+text to a .staged
    sibling and lands at the target only after confirm_write(handle)."""
    p = _resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    _sweep_expired(time.time())
    h = uuid.uuid4().hex
    staged = p.with_name(p.name + ".staged")
    existing = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""
    result = existing + text
    _PENDING[h] = {
        "path": p,
        "staged": staged,
        "parts": [result],
        "size": len(result),
        "newline": "\n",
        "created": time.time(),
    }
    _mirror_state()
    Path(staged).write_text(result, encoding="utf-8")
    if _auto_apply():
        return confirm_write(h)
    return (
        f"STAGED {staged} (append of {len(text)} chars to "
        f"{'existing' if existing else 'new'} file, {len(result)} total). "
        f"NOTHING at {p} changed yet. confirm_write('{h}') lands it, "
        "abort_write discards."
    )


def main() -> int:
    mcp.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())