# Panel patches

Split from a single `right-bar.patch` on 2026-09-05 (targeting llama.cpp
v0.4.0) so a future version bump doesn't require re-resolving one large diff —
each patch only touches files for one concern, so a conflict in one doesn't
block the others from applying.

- `server-hooks.patch` — C++ server changes (`tools/server/server-models.*`,
  `server-tools.cpp`) that expose model listings for the nim/openrouter/rag tabs.
- `panel-constants.patch` — routes, sidebar icon-strip entries, and UI
  constants. Smallest, but the most likely to drift: upstream reshuffles its
  own constants/sidebar layout often — both conflicts in the 0.2.0 -> 0.3.0
  bump and two of the five in 0.3.0 -> 0.4.0 landed here.
- `panel-ui.patch` — everything else: new routes (nim/openrouter/rag/
  rag-collections), stores, services, chat components (image/media upload,
  compaction summaries, tool-call rendering).
- `panel-commands.patch` — the panel's slash commands: extends the WebUI's own
  `/` command picker (which ships `/model`, `/cwd`, `/prompt`) with the panel's
  own set. Touches five upstream files and adds one, none of which any other
  patch touches:

  | file | change |
  | --- | --- |
  | `lib/enums/chat.enums.ts` | one enum member: `ChatFormCommandAction.PANEL` |
  | `lib/types/chat.d.ts` | optional `icon` on `ChatFormCommand`; `extraCommands` option |
  | `lib/utils/chat-commands.ts` | appends `options.extraCommands?.()` to the registry |
  | `lib/hooks/use-chat-form-pickers.svelte.ts` | passes the getter in; one dispatch case |
  | `ChatFormPickerCommand.svelte` | `command.icon ?? commandIcon[command.action]`, plus the fallback icon |
  | `lib/utils/panel-command-runtime.ts` | **new** — re-exports the icons and `toast` |

Apply in any order — they don't touch the same files. `fork/pkg-build/PKGBUILD`
applies all four in `prepare()`.

## The slash commands (panel-commands.patch)

All fifteen command *bodies* live outside the tree, in
`panel/frontend/chat-commands/index.ts`. The patch is only the seam: the tree
supplies the picker, the panel supplies the commands. Two consequences worth
knowing before touching either side.

**To add or change a command, no patch work is needed.** Add an entry to
`COMMANDS` in `panel/frontend/chat-commands/index.ts` and rebuild — the picker,
the filtering and the keyboard navigation all come from upstream. `run` gets
the text typed after the command name; `disabled` is evaluated on every picker
open, so it can read stores directly.

**Anything a command needs from node_modules must go through
`lib/utils/panel-command-runtime.ts`.** A bare package specifier
(`@lucide/svelte`, `svelte-sonner`) in a file under `panel/frontend` resolves
node_modules relative to *its own* path, never reaching `tools/ui/node_modules`,
and fails the build with `vite:load-fallback`. That file is the same workaround
`RightBar.svelte` documents for its two icons. It is deliberately not exported
through the `$lib/utils` barrel, because `panel-ui.patch` already edits that
barrel.

Verification, since `npm run build` is what the PKGBUILD runs: apply all four
patches to a fresh `--depth 1 --branch v0.4.0` clone, then `npm run build` in
`tools/ui`.

### Known non-issue: a 7th `svelte-check` error

`npm run check` reports `Cannot find module '@mcp-panel/chat-commands'` for the
hook, alongside the six identical errors the existing patch already produces
for `@mcp-panel/RightBar.svelte` and the panel's `+page.svelte` route files.
Cause is shared: the alias is a Vite-only alias and `tsconfig.json` has no
`paths` entry. Adding one was deliberately avoided — it would make svelte-check
try to typecheck all of `panel/frontend` for the first time, turning one known
cosmetic error into an unknown pile in files this patch shouldn't be touching.
`npm run build` does not typecheck, so this affects neither the package nor the
dev server.

## Regenerating after a version bump

1. Shallow-clone the new tag: `git clone --depth 1 --branch v<ver> https://github.com/ggml-org/llama.cpp.git /tmp/test`
2. `cd /tmp/test && git apply --reject --whitespace=nowarn <path-to-old-full-patch>`
   — anything that fails leaves a `.rej` file.
3. Read each `.rej` against the *current* upstream file (not the old one) and
   hand-apply the equivalent change — these are usually upstream renaming or
   moving a neighboring line, not real semantic conflicts with the panel
   feature. Watch specifically for: unrelated prop-reordering (prettier/eslint
   drift breaks context matching even though nothing changed) vs. an actually
   removed anchor (e.g. the MCP-servers sidebar entry was deleted upstream in
   0.4.0, so the RAG-collections entry had to move to a different anchor
   point, and its `ROUTES`/icon imports had to be re-added since the file no
   longer imported them for any other reason).
4. `git add -A && git diff --cached > full.patch` (staging first is required —
   plain `git diff` silently omits new untracked files added by the patch).
5. Re-split: `git diff --cached -- <paths for one concern> > name.patch` per
   group above.
6. Verify: apply all three, in order, to a *second* fresh clone of the same
   tag and confirm `git diff --cached --stat` matches step 4's total exactly.

## Known landmine: absolute paths inside the panel itself

`panel/frontend` lives entirely outside the llama.cpp tree by design — the
whole integration is one Vite resolve alias (`@mcp-panel`) plus one
`server.fs.allow` entry in `vite.config.ts`, both pointing at this repo's
absolute path on disk. `git apply` succeeding proves nothing about this —
the patch applies fine either way; it only breaks at `npm run build`
(`vite:load-fallback ... ENOENT`), or at dev-server runtime. If this repo
ever moves again, grep the 3 patches for the repo's old absolute path
before assuming a successful `makepkg` build. Bit us once already
(2026-09-06, right after the 2026-09-01 move) — see git blame on
`panel-ui.patch`'s `vite.config.ts` hunk.

## History

- 2026-09-17: added `panel-commands.patch` (slash commands), split from nothing
  — it is new, not carved out of an existing patch, and was verified applying
  fourth and last against a fresh v0.4.0 clone (46 files changed in total).
- 0.2.0 baseline (28 files) -> 0.3.0: 2 conflicts, both in
  `panel-constants.patch` (upstream dropped a `NEW_CHAT` route and changed an
  enum import path — pure context drift, not semantic).
- 0.3.0 -> 0.4.0: 5 conflicts — the same 2 constants files again, plus 3 in
  `panel-ui.patch` (`ChatMessageSynthetic.svelte`,
  `ChatMessageToolCallBlockReadMedia.svelte` — both just prop-reorder drift —
  and `database.d.ts`, where upstream inserted two new fields ahead of the
  insertion point, pure line-shift). `server-hooks.patch` has needed zero
  fixes across both bumps.
