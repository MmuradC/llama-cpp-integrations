# panel-shell

**Applies to:** NOT YET EXTRACTED — the patch file for this module still has to be generated against the tree a given build uses (see ../README.md). What follows describes the change it must contain.

```sh
git apply panel/patches/modules/panel-shell/panel-shell.patch   # once generated
```

## What it does

The two in-tree edits that make the whole panel reachable: a Vite alias and one render line.

## The change, precisely

tools/ui/vite.config.ts adds resolve.alias { '@mcp-panel': '<abs>/panel/frontend' } and adds that path to server.fs.allow. tools/ui/src/routes/+layout.svelte imports and renders RightBar (from the alias). That is the entire in-tree footprint of the panel — everything else lives in panel/frontend and is never subject to a llama.cpp version bump. NOTE: the alias is an absolute path; if this repo moves, these entries must be updated, which bit this project once already (2026-09-06).
