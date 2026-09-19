# archive — the original v0.4.0 patch set

Kept as history. **Not maintained, and not applied by anything.**

| file | what it was |
|---|---|
| `panel-constants.patch` | routes, sidebar icon-strip entries and UI constants |
| `panel-ui.patch` | everything else: 34 files, mixing the right-bar, compaction, media, markdown, RAG pages and more |

## Why they are here and not in use

Both target llama.cpp **v0.4.0**, whose UI is an older generation than the one
this project actually runs. That was verified rather than assumed: `git apply
--check` against the live tree conflicts for both of these, and for the two
patches that were in use (`server-hooks`, `panel-commands`) as well.

The consequence is worth stating plainly, because it is the reason this
directory was reorganised: **a package built from these patches would not
reproduce the UI that runs.** The live tree carries a different UI generation
(hash routing, a rewritten settings architecture) plus this project's own work,
so the patch set and the live tree were two competing strategies for the same
goal, with only one of them actually running.

`panel-ui.patch` is the clearest case for the split into modules: a conflict in
its markdown changes blocked its compaction changes, which blocked its media
changes — none of which have anything to do with each other.

## If you want a v0.4.0 build again

Regenerate the module patches against a v0.4.0 checkout rather than reviving
these files: the modules describe the current intent (per-concern, owner-
documented), while these two are a snapshot of a layout that no longer matches
how the project is organised.

## History, from the original README

- 0.2.0 baseline (28 files) -> 0.3.0: 2 conflicts, both context drift in the
  constants patch (upstream dropped a `NEW_CHAT` route and changed an enum
  import path).
- 0.3.0 -> 0.4.0: 5 conflicts — the same 2 constants files again, plus 3 in
  `panel-ui.patch`, all pure drift (prop reordering from a prettier run, and a
  line shift where upstream inserted two fields ahead of an insertion point).
- The prop-reordering drift is the one to remember when regenerating: it breaks
  context matching while changing nothing semantically, so a failed hunk there
  is usually not a real conflict.