#!/usr/bin/env python3
"""Local RAG (retrieval-augmented generation) as an MCP tool.

Lets any model in chat - even a text-only one - ground answers in documents
uploaded through the right-sidebar RAG panel (see panel/frontend/RagPage.svelte
and panel/backend/server.py's /api/rag/* routes, which write into the same
collections this reads from). All storage/embedding/search logic lives in
rag_core.py, shared with the panel backend.

Configuration:
    LLAMA_SERVER_URL   the router, default http://127.0.0.1:8080
    RAG_EMBED_MODEL    embedding model alias, default "embed"
                        (see llama-models.ini's [gpustack/bge-m3-GGUF] preset)
    RAG_EMBED_TIMEOUT  seconds per embedding call, default 180 - generous
                        because the first call after the chat model has been
                        active can coincide with the router swapping the
                        embed model in, which takes real time
"""

from __future__ import annotations

import sys

from mcp.server.fastmcp import FastMCP

import rag_core

mcp = FastMCP("rag")


@mcp.tool()
def rag_list_collections() -> str:
    """List every RAG document collection available to search.

    Call this first if you don't already know which collection to use - the
    exact name shown here is what to pass as rag_query's `collection`
    argument.
    """
    collections = rag_core.list_collections()
    if not collections:
        return "No RAG collections exist yet. The user needs to upload documents via the RAG panel first."

    lines = [f"{len(collections)} collection(s):"]
    for c in collections:
        desc = f" - {c['description']}" if c.get("description") else ""
        lines.append(f'  "{c["name"]}": {c["chunk_count"]} chunks from {len(c["documents"])} document(s){desc}')
    return "\n".join(lines)


@mcp.tool()
def rag_query(collection: str, question: str, top_k: int = 5) -> str:
    """Search a RAG document collection and return the most relevant excerpts
    for a question, each attributed to its source file.

    `collection` should be a name from rag_list_collections() - call that
    first if unsure which collections exist. Returns up to top_k excerpts, or
    a plain message if the collection is unknown, empty, or the search itself
    fails (e.g. the embedding model is still loading after a swap - safe to
    retry in that case).
    """
    try:
        hits = rag_core.search(collection, question, top_k=top_k)
    except rag_core.RagError as exc:
        known = rag_core.list_collections()
        if known:
            names = ", ".join(f'"{c["name"]}"' for c in known)
            return f"{exc}. Available collections: {names}"
        return f"{exc}. No collections exist yet."

    if not hits:
        return f'Collection "{collection}" has no documents yet - nothing to search.'

    lines = [f'{len(hits)} excerpt(s) from "{collection}" for: {question}\n']
    for h in hits:
        lines.append(f"[{h['filename']} chunk {h['chunk_index']}, score {h['score']:.2f}]\n{h['text']}\n")
    return "\n".join(lines)


if __name__ == "__main__":
    print(f"[rag] data dir: {rag_core.RAG_DATA_DIR}", file=sys.stderr)
    print(f"[rag] embed model: {rag_core.EMBED_MODEL} @ {rag_core.LLAMA_SERVER_URL}", file=sys.stderr)
    mcp.run("stdio")
