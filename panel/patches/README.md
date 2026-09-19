# llama-cpp-integrations — patch modules

This directory is the **llama-cpp-integrations** project's own integration layer
for llama.cpp's web UI and server. It used to be three anonymous patches
(`server-hooks`, `panel-constants`, `panel-ui`) plus a commands patch, split by
*file location*, which meant one of them (`panel-ui`) was a 34-file grab-bag
mixing seven unrelated features: markdown rendering, compaction, image
handling, the right-bar, RAG pages and more. Adding anything meant editing that
pile, and a conflict in one feature blocked every other.

It is now organised by **what it does**, one directory per module, and it is
owned by this repo rather than being an incidental patch on upstream.

```
patches/
  manifest.json          machine-readable module list (name, path, applies-to, files)
  modules/
    server-model-hooks/  C++ side: what the router advertises and proxies
    panel-shell/         the right-bar mount and the panel's own routes
    compaction/          summarize-and-trim for long conversations
    media/               images, audio, PDFs: attach, fall back, read
    markdown/            code blocks and rendered output
    rag-pages/           RAG collection browser and editor
    slash-commands/      the panel's commands in the WebUI's own picker
    permission-modes/    per-chat permission modes
    incognito/           chats that are never written to disk
    tailwind-source/     makes panel classes visible to Tailwind
  archive/               the original v0.4.0 patch set, unmaintained
```

## What each module is, and what applying it requires

Every module has a `README.md` stating the files it touches, what it does, and
the exact `git apply` line. Order matters only where a module depends on
another (noted per module); otherwise they are independent.

| module | touches | why it exists |
|---|---|---|
| `server-model-hooks` | `tools/server/server-models.*`, `server-tools.cpp` | exposes remote/pinned models to the router so they appear in the model list and proxy through the panel |
| `panel-shell` | `tools/ui/vite.config.ts`, `routes/+layout.svelte` | one Vite alias (`@mcp-panel` → `panel/frontend`) plus one render line: the whole reason the panel can live outside llama.cpp |
| `compaction` | chat store, constants, `utils/compaction.ts`, summary message component | ask the model to summarize a long conversation, then trim what is resent — nothing is deleted |
| `media` | file-upload hook, `read-media.service.ts`, message extras, image-path handling | images work for models **without** vision: the file is saved and its path is handed to the model |
| `markdown` | `MarkdownContent/*`, code-block and markdown constants | rendered output: enhanced code blocks, styling |
| `rag-pages` | `routes/rag*`, RAG frontend pages | browse and edit the RAG collections the MCP server exposes |
| `slash-commands` | command registry, enums, types, picker, `panel-command-runtime.ts` | adds the panel's commands to the WebUI's existing `/` picker |
| `permission-modes` | `stores/permission-mode`, `stores/agentic/gates`, mode picker | the mode applies **per chat**, and the always-allow list can only silence tools that cannot write |
| `incognito` | `stores/incognito-chat.svelte.ts` + guards in `DatabaseService` | a chat whose writes are intercepted at the single choke point, so it never reaches IndexedDB |
| `tailwind-source` | `src/app.css` | Tailwind only scans `src/`, so without this every class used **only** by panel components is never generated and the panel renders unstyled |

## Which llama.cpp version this targets

The modules in `modules/` were extracted from the **live tree**
(`fork/pkg-build/llama.cpp-src`, master-era UI with hash routing) and are the
ones that describe what actually runs. `archive/` holds the original
v0.4.0-targeted set, which does **not** apply to the live tree and is kept only
as history.

Because the two trees are different UI generations, a module's patch applies to
the generation it was extracted from. `manifest.json` records that per module
under `applies_to`.

## Adding or changing a module

1. Edit the live tree (`fork/pkg-build/llama.cpp-src`) until the behaviour is right.
2. Regenerate only that module's patch:
   `git diff -- <its files> > modules/<name>/<name>.patch`
3. Update `manifest.json` if the file list changed.

Keep a module to one concern. The old single-patch approach is exactly what
this replaces: a conflict in one feature must not block the others.