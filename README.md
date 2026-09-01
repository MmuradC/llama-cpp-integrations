# llama-cpp-integrations

Personal MCP servers and control-panel integrations built around a local
llama.cpp setup.

- `MCP/` — MCP servers: `imagegen`, `nim`, `openrouter`, `rag`, plus the
  `mcp-filesystem` server config and the combined `llama-mcp-servers.json`.
- `panel/` — Svelte frontend + FastAPI backend for the right-bar control
  panel, plus the `right-bar.patch` it's built from.
- `llama-models.ini` — per-model llama-server launch config.

Not tracked here (see `.gitignore`): `fork/` (separate llama.cpp git repos)
and `models/` (GGUF weights).
