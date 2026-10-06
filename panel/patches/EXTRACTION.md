# Patch extraction — llama.cpp v0.4.0 fork

Every change the deployed tree carries over upstream **llama.cpp v0.4.0** is now
extracted as a patch module, so the fork can be recreated on any machine from a
pristine checkout instead of being a single tree that must be copied around.

    base      llama.cpp v0.4.0, commit 5266f24da7 ("bump version to 0.4.0 (#28386)")
    source    fork/pkg-build/src/llama.cpp  (HEAD 9d7f14d4b6, plus 329 uncommitted UI edits)
    coverage  342 of 342 changed files, each owned by exactly one module
    verified  all 16 patches apply to a pristine v0.4.0 and the result is
              byte-identical to the source tree (340 files compared, 0 mismatches;
              2 files are deletions)

## Modules

| # | module | files | patch |
|---|--------|-------|-------|
| 1 | server-model-hooks | 3 | 21 KB — router hooks, pinned remote models |
| 2 | panel-shell | 2 | 4 KB — vite `@mcp-panel` alias + the render line that mounts the panel |
| 3 | compaction | 7 | 41 KB — summarize-and-trim a long chat, incl. the n_ctx-0 fix |
| 4 | media | 4 | 8 KB — attach images for models that cannot see them |
| 5 | markdown | 8 | 20 KB — enhanced code blocks and styling |
| 6 | rag-pages | 2 | 1 KB — `/rag`, `/rag-collections` routes |
| 7 | slash-commands | 6 | 11 KB — panel commands in the WebUI `/` picker |
| 8 | permission-modes | 4 | 22 KB — per-chat permission mode, write-gates |
| 9 | incognito | 2 | 12 KB — a chat that is never written to disk |
| 10 | tailwind-source | 1 | 1 KB — `@source` for panel/frontend (without it the panel is unstyled) |
| 11 | remote-providers | 9 | 11 KB — `/nim`, `/opencode`, `/openrouter` pages + usage dial |
| 12 | mcp-servers-ui | 2 | 7 KB — `/mcp-servers` page + chat-form submenu |
| 13 | settings-ui | 6 | 7 KB — the `/settings` section |
| 14 | agentic-ui | 4 | 24 KB — agentic loop guard, compaction tool distillation |
| 15 | attachments-pwa | 7 | 9 KB — attachment menu + PWA |
| 16 | ui-generation | 275 | 500 KB — **bulk**: the newer UI generation ported onto v0.4.0 |

Modules 1–10 are the surgical ones named in the original manifest; 11–15 were
extracted as new feature modules; 16 is everything else and is the module that
will need hand-rebasing on an upstream bump.

## Applying

To an existing checkout of the base revision:

    bash panel/patches/apply.sh /path/to/llama.cpp      # applies in manifest order

or by hand, in the manifest's order:

    git -C llama.cpp apply /path/to/panel/patches/modules/server-model-hooks/server-model-hooks.patch

From scratch:

    git clone https://github.com/ggml-org/llama.cpp && cd llama.cpp
    git checkout 5266f24da7          # v0.4.0
    bash ../panel/patches/apply.sh .

Then build the UI (`tools/ui`) — Debian's cmake run does **not** build it
(`LLAMA_USE_PREBUILT_UI=ON`), and `serve`'s unit serves `tools/ui/dist` directly
via `LLAMA_ARG_STATIC_PATH`.

## Regenerating after you change the tree

    python3 panel/patches/extract.py            # dry run: what would change
    python3 panel/patches/extract.py --write    # write patches, then verify

`extract.py` reads `manifest.json`, diffs each module's files against `base`,
writes the patches, then **proves** them: it creates a detached worktree at the
base, applies all patches in order, and compares every covered file byte-for-byte
against the source tree. Files that exist but are untracked are staged with
`git add -N` for the duration of the diff and unstaged again, so they come out as
proper new-file patches and your index is left as it was.

To re-target a new upstream revision, change `base` in `manifest.json`, add any
newly-changed files to the owning module's `files` list, and re-run. Expect real
work only in `ui-generation`.

## Ownership rules

* One file, one module. The only file two concerns shared,
  `tools/ui/src/lib/services/chat.service.ts`, is owned by `compaction`;
  `incognito` carries it as a `shared_files_note`. Applying both modules still
  yields the complete file.
* Because a file can only live in one module, a module's patch also carries any
  unrelated change made to *its* files. That is why `slash-commands` is larger
  than the version extracted from the panel-only commit: its files also picked up
  UI-generation edits.

## Not covered

* **panel/frontend and panel/backend** live in this outer repo, not in the
  llama.cpp tree, so no patch here contains them. The `@mcp-panel` vite alias
  (module 2) points at `panel/frontend`; the backend stays a separate service.
  A fresh llama.cpp checkout therefore still needs this repo's `panel/` next to
  it (or the alias edited).
* The 2 deleted files in the change set are deletions, so they are absent from
  the applied result on purpose (they are not counted in the 340 compared files).
* The deployed `serve` tree also carries files that were already correct in
  v0.4.0 — those are unchanged and therefore in no patch.
