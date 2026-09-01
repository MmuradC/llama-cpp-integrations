"""Shared storage/ingestion/search logic for the local RAG feature.

Used by two separate processes: MCP/rag/server.py (the rag_query/
rag_list_collections MCP tool) and panel/backend/server.py (the upload/list/
delete UI endpoints, via `sys.path.insert` + `import rag_core` since it lives
in a different directory). Kept plain and synchronous to match every sibling
MCP server's urllib-only convention (imagegen/nim/openrouter) - the async
panel backend wraps calls in asyncio.to_thread instead of this module
growing an async variant.

No vector-store dependency: at personal-scale (dozens-low hundreds of docs)
brute-force numpy cosine search is sub-millisecond, and FAISS/chromadb would
be pure overhead for a problem numpy already solves. Embeddings come from
llama-server's own /v1/embeddings (never computed in this process), using a
dedicated "embed" model preset that the router auto-swaps in/out with
whatever chat model is active (LLAMA_ARG_MODELS_MAX=1 already forces this).

Storage layout, one directory per collection under RAG_DATA_DIR:
    <collection-id>/manifest.json   collection + per-document metadata
    <collection-id>/chunks.jsonl    one {doc_id, filename, chunk_index, text}
                                     per line, in insertion order
    <collection-id>/embeddings.npy  float32 (n_chunks, dim), L2-normalized,
                                     row index == chunks.jsonl line index

RAG_DATA_DIR must never be overridden differently between the MCP server and
the panel backend - both must resolve the same default, or collections would
silently split between what each process can see. Neither process's env
block sets it; only change the DEFAULT_DATA_DIR constant below if it ever
needs to move.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import numpy as np

DEFAULT_DATA_DIR = Path.home() / ".local" / "share" / "llama-rag" / "collections"
RAG_DATA_DIR = Path(os.environ.get("RAG_DATA_DIR", "") or DEFAULT_DATA_DIR)

LLAMA_SERVER_URL = os.environ.get("LLAMA_SERVER_URL", "http://127.0.0.1:8080").rstrip("/")
EMBED_MODEL = os.environ.get("RAG_EMBED_MODEL", "embed")
EMBED_TIMEOUT = float(os.environ.get("RAG_EMBED_TIMEOUT", "180"))

_TEXT_EXTENSIONS = {".txt", ".md"}


class RagError(Exception):
    """Raised for user-facing failures (bad collection, empty upload, etc)."""


# --- collection id / manifest helpers ---------------------------------


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "collection"


def _unique_slug(name: str) -> str:
    base = _slugify(name)
    slug = base
    n = 2
    while (RAG_DATA_DIR / slug).exists():
        slug = f"{base}-{n}"
        n += 1
    return slug


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _collection_dir(collection_id: str) -> Path:
    return RAG_DATA_DIR / collection_id


def _manifest_path(collection_id: str) -> Path:
    return _collection_dir(collection_id) / "manifest.json"


def _read_manifest(collection_id: str) -> dict:
    path = _manifest_path(collection_id)
    if not path.exists():
        raise RagError(f'no such collection: "{collection_id}"')
    return json.loads(path.read_text(encoding="utf-8"))


def _write_manifest(collection_id: str, manifest: dict) -> None:
    manifest["updated_at"] = _now()
    _manifest_path(collection_id).write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def resolve_collection_id(collection: str) -> str | None:
    """Match a model-supplied name against a real collection id.

    A model calling rag_query is more likely to pass something close to the
    display name than the exact slug, so try, in order: exact id, then a
    case-insensitive slug match, then a case-insensitive match against the
    human-readable name.
    """
    needle = collection.strip()
    if not needle:
        return None
    if _manifest_path(needle).exists():
        return needle

    needle_lower = needle.lower()
    slug = _slugify(needle)
    for entry in list_collections():
        if entry["id"] == slug or entry["id"].lower() == needle_lower:
            return entry["id"]
    for entry in list_collections():
        if entry["name"].lower() == needle_lower:
            return entry["id"]
    return None


# --- collection CRUD ----------------------------------------------------


def list_collections() -> list[dict]:
    if not RAG_DATA_DIR.exists():
        return []
    out = []
    for child in sorted(RAG_DATA_DIR.iterdir()):
        manifest_path = child / "manifest.json"
        if manifest_path.exists():
            out.append(json.loads(manifest_path.read_text(encoding="utf-8")))
    return out


def get_collection(collection_id: str) -> dict:
    return _read_manifest(collection_id)


def create_collection(name: str, description: str = "") -> dict:
    if not name.strip():
        raise RagError("collection name cannot be empty")
    collection_id = _unique_slug(name)
    _collection_dir(collection_id).mkdir(parents=True, exist_ok=True)
    manifest = {
        "id": collection_id,
        "name": name.strip(),
        "description": description.strip(),
        "created_at": _now(),
        "updated_at": _now(),
        "embedding_model": EMBED_MODEL,
        "embedding_dim": None,
        "chunk_size": 800,
        "chunk_overlap": 150,
        "chunk_count": 0,
        "documents": [],
    }
    _write_manifest(collection_id, manifest)
    return manifest


def delete_collection(collection_id: str) -> None:
    manifest = _read_manifest(collection_id)  # raises if missing
    import shutil

    shutil.rmtree(_collection_dir(manifest["id"]))


def delete_document(collection_id: str, doc_id: str) -> dict:
    manifest = _read_manifest(collection_id)
    chunks_path = _collection_dir(collection_id) / "chunks.jsonl"
    emb_path = _collection_dir(collection_id) / "embeddings.npy"

    keep_rows: list[int] = []
    kept_lines: list[str] = []
    if chunks_path.exists():
        for i, line in enumerate(chunks_path.read_text(encoding="utf-8").splitlines()):
            if not line.strip():
                continue
            row = json.loads(line)
            if row["doc_id"] != doc_id:
                keep_rows.append(i)
                kept_lines.append(line)
        chunks_path.write_text(
            "\n".join(kept_lines) + ("\n" if kept_lines else ""), encoding="utf-8"
        )

    if emb_path.exists():
        embeddings = np.load(emb_path)
        embeddings = embeddings[keep_rows] if keep_rows else embeddings[:0]
        np.save(emb_path, embeddings)

    manifest["documents"] = [d for d in manifest["documents"] if d["doc_id"] != doc_id]
    manifest["chunk_count"] = len(kept_lines)
    _write_manifest(collection_id, manifest)
    return manifest


# --- document conversion + chunking -------------------------------------


def convert_to_text(file_path: Path, original_filename: str) -> str:
    ext = Path(original_filename).suffix.lower()
    if ext in _TEXT_EXTENSIONS:
        return file_path.read_text(encoding="utf-8", errors="replace")

    from markitdown import MarkItDown

    result = MarkItDown().convert(str(file_path))
    return result.text_content


_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")
_LINE_SPLIT = re.compile(r"\n")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _split_oversized(text: str, chunk_size: int) -> list[str]:
    """Break a too-long paragraph down until every piece fits chunk_size.

    Tries progressively finer splits - newlines, then sentences, then a hard
    char slice as a last resort - so nothing ever comes out longer than
    chunk_size. Needed for content with no blank-line paragraph breaks AND
    no sentence-ending punctuation, e.g. a markdown table converted from a
    CSV (one row per line, decimal points but no ". "): without this, the
    whole table came out as a single multi-KB "chunk" that then blew past
    the embedding model's context window - the real cause of a 6.5MB CSV
    upload timing out on its embedding call, not chunk *count* as first
    suspected.
    """
    if len(text) <= chunk_size:
        return [text]

    lines = [ln.strip() for ln in _LINE_SPLIT.split(text) if ln.strip()]
    if len(lines) > 1:
        out: list[str] = []
        for line in lines:
            out.extend(_split_oversized(line, chunk_size))
        return out

    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(text) if s.strip()]
    if len(sentences) > 1:
        out = []
        for s in sentences:
            out.extend(_split_oversized(s, chunk_size))
        return out

    # no natural split point at all - hard character slice
    return [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]


def chunk_text(text: str, chunk_size: int = 800, overlap: int = 150) -> list[str]:
    """Pack paragraphs (falling back to lines, then sentences, then a hard
    char slice for anything still too long - see _split_oversized) into
    chunks up to chunk_size chars, with the tail of each chunk re-seeding the
    next one for overlap continuity.

    Char-based, not token-based: chunk_size sits far under the embedding
    model's context window (see llama-models.ini's [gpustack/bge-m3-GGUF]
    section), so the ~4-chars-per-token approximation has ample headroom and
    a per-chunk /tokenize round-trip would add nothing but latency.
    """
    paragraphs = [p.strip() for p in _PARAGRAPH_SPLIT.split(text) if p.strip()]

    units: list[str] = []
    for para in paragraphs:
        units.extend(_split_oversized(para, chunk_size))

    chunks: list[str] = []
    buffer: list[str] = []
    buffer_len = 0

    def flush() -> None:
        if buffer:
            chunks.append(" ".join(buffer).strip())

    for unit in units:
        added_len = len(unit) + (1 if buffer else 0)
        if buffer and buffer_len + added_len > chunk_size:
            flush()
            # reseed with trailing units of the just-flushed chunk, up to
            # ~overlap chars, so the next chunk keeps some continuity
            tail: list[str] = []
            tail_len = 0
            for prev_unit in reversed(buffer):
                if tail_len + len(prev_unit) > overlap:
                    break
                tail.insert(0, prev_unit)
                tail_len += len(prev_unit) + 1
            buffer = tail
            buffer_len = tail_len

        buffer.append(unit)
        buffer_len += added_len

    flush()
    return [c for c in chunks if c]


# --- embeddings -----------------------------------------------------------


EMBED_BATCH_SIZE = int(os.environ.get("RAG_EMBED_BATCH_SIZE", "32"))


def _embed_batch(texts: list[str]) -> np.ndarray:
    """One /v1/embeddings call for a single batch. Retries once on a
    timeout/connection error: the very first call after the chat model has
    been idle can coincide with the router's own model-swap, which can
    briefly refuse connections - same lesson learned from the vision MCP
    tool's timeout tuning earlier this session."""
    payload = json.dumps({"model": EMBED_MODEL, "input": texts}).encode()
    req = urllib.request.Request(
        f"{LLAMA_SERVER_URL}/v1/embeddings",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=EMBED_TIMEOUT) as resp:
                result = json.loads(resp.read())
            break
        except (TimeoutError, ConnectionError, urllib.error.URLError) as exc:
            last_exc = exc
            if attempt == 0:
                time.sleep(2)
                continue
            raise RagError(
                f"embedding request timed out after {EMBED_TIMEOUT:.0f}s - the "
                "embed model may still be loading after a swap, try again in "
                "a moment"
            ) from exc
    else:  # pragma: no cover - loop always breaks or raises
        raise RagError(f"embedding request failed: {last_exc}")

    rows = [item["embedding"] for item in result["data"]]
    return np.array(rows, dtype=np.float32)


def embed_texts(texts: list[str], mode: Literal["document", "query"] = "document") -> np.ndarray:
    """Call llama-server's /v1/embeddings for the configured RAG_EMBED_MODEL.

    `mode` is accepted (not currently used) so a future embedding model that
    needs "search_document:"/"search_query:"-style prefixes (nomic-embed,
    the e5 family) is a one-line change here rather than a call-site change -
    BGE-M3, the current default, needs no prefix.

    Batches internally at EMBED_BATCH_SIZE: a large document can chunk into
    thousands of pieces, and one HTTP call embedding all of them at once
    scales its own latency with document size without bound - a 6.5MB CSV
    hit exactly this, timing out at 180s regardless of how generous that
    timeout was. Each batch instead stays a small, bounded request; the
    overall ingestion still takes longer for a bigger document, but no
    single call can time out because of size alone.
    """
    if not texts:
        return np.zeros((0, 0), dtype=np.float32)

    batches = [texts[i : i + EMBED_BATCH_SIZE] for i in range(0, len(texts), EMBED_BATCH_SIZE)]
    arr = np.vstack([_embed_batch(batch) for batch in batches])

    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return arr / norms


# --- ingestion --------------------------------------------------------


def ingest_document(collection_id: str, file_path: Path, original_filename: str) -> dict:
    manifest = _read_manifest(collection_id)
    doc_id = uuid.uuid4().hex[:8]
    doc_entry = {
        "doc_id": doc_id,
        "filename": original_filename,
        "bytes": file_path.stat().st_size,
        "added_at": _now(),
        "chunk_count": 0,
        "status": "processing",
        "error": None,
    }
    manifest["documents"].append(doc_entry)
    _write_manifest(collection_id, manifest)

    try:
        text = convert_to_text(file_path, original_filename)
        if not text.strip():
            raise RagError(f'"{original_filename}" contains no extractable text')

        chunks = chunk_text(
            text,
            chunk_size=manifest.get("chunk_size", 800),
            overlap=manifest.get("chunk_overlap", 150),
        )
        if not chunks:
            raise RagError(f'"{original_filename}" produced no chunks')

        embeddings = embed_texts(chunks, mode="document")

        existing_dim = manifest.get("embedding_dim")
        if existing_dim is not None and embeddings.shape[1] != existing_dim:
            raise RagError(
                f"embedding dimension changed ({existing_dim} -> "
                f"{embeddings.shape[1]}) - the embed model config may have "
                "changed since this collection was created"
            )

        chunks_path = _collection_dir(collection_id) / "chunks.jsonl"
        with chunks_path.open("a", encoding="utf-8") as f:
            for i, chunk in enumerate(chunks):
                f.write(
                    json.dumps(
                        {
                            "doc_id": doc_id,
                            "filename": original_filename,
                            "chunk_index": i,
                            "text": chunk,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )

        emb_path = _collection_dir(collection_id) / "embeddings.npy"
        if emb_path.exists():
            combined = np.vstack([np.load(emb_path), embeddings])
        else:
            combined = embeddings
        np.save(emb_path, combined)

        manifest = _read_manifest(collection_id)  # re-read in case of concurrent edits
        for d in manifest["documents"]:
            if d["doc_id"] == doc_id:
                d["status"] = "ready"
                d["chunk_count"] = len(chunks)
        manifest["embedding_dim"] = int(embeddings.shape[1])
        manifest["chunk_count"] = int(combined.shape[0])
        _write_manifest(collection_id, manifest)

    except Exception as exc:
        manifest = _read_manifest(collection_id)
        for d in manifest["documents"]:
            if d["doc_id"] == doc_id:
                d["status"] = "error"
                d["error"] = str(exc)
        _write_manifest(collection_id, manifest)
        raise

    return get_collection(collection_id)


# --- search -----------------------------------------------------------


def search(collection: str, question: str, top_k: int = 5) -> list[dict]:
    collection_id = resolve_collection_id(collection)
    if collection_id is None:
        raise RagError(f'no such collection: "{collection}"')

    emb_path = _collection_dir(collection_id) / "embeddings.npy"
    chunks_path = _collection_dir(collection_id) / "chunks.jsonl"
    if not emb_path.exists() or not chunks_path.exists():
        return []

    embeddings = np.load(emb_path)
    if embeddings.shape[0] == 0:
        return []

    rows = [json.loads(line) for line in chunks_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    query_vec = embed_texts([question], mode="query")[0]
    scores = embeddings @ query_vec
    top_k = max(1, min(top_k, len(rows)))
    top_indices = np.argsort(-scores)[:top_k]

    return [
        {
            "text": rows[i]["text"],
            "filename": rows[i]["filename"],
            "chunk_index": rows[i]["chunk_index"],
            "score": float(scores[i]),
        }
        for i in top_indices
    ]
