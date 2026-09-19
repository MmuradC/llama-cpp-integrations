# server-model-hooks

**Applies to:** llama.cpp v0.4.0 and later (touches stable C++ interfaces, has
needed zero fixes across two version bumps — see the archive README).

```sh
git apply panel/patches/modules/server-model-hooks/server-model-hooks.patch
```

## What it does

Three C++ files, one concern: making the router aware of models this project
registers. Concretely it is what lets a **pinned remote model** (an OpenRouter,
NVIDIA NIM or OpenCode id) appear in the normal model dropdown and be routed to
the panel backend's relay instead of a local child process.

| file | change |
|---|---|
| `tools/server/server-models.cpp` | reads each provider's pinned-model file at startup/reload and registers one already-loaded entry per pin, id `{provider}/{sanitized real id}`, pointing back at the panel backend's port |
| `tools/server/server-models.h` | the declarations for the above |
| `tools/server/server-tools.cpp` | the tool listing the panel's tabs read |

## Why it is a separate module

It is the only part of this project that touches C++ rather than the web UI, so
it is also the only part that survives a UI rewrite. Keeping it apart means a
UI-generation change (which is exactly what broke the old patch set) cannot
block it, and it needs no coordination with any other module.