# slash-commands

**Applies to:** llama.cpp v0.4.0 — extracted and verified applying (`git apply
--check` clean). This is the module that was `panel-commands.patch`.

```sh
git apply panel/patches/modules/slash-commands/slash-commands.patch
```

## What it does

The WebUI already ships a `/` command picker with three commands of its own
(`/model`, `/cwd`, `/prompt`). This module adds the panel's commands to that
same picker without reimplementing any of its machinery (filtering, keyboard
navigation, dismiss handling). The command *bodies* live outside llama.cpp's
tree, in `panel/frontend/chat-commands/`.

| file | change |
|---|---|
| `lib/enums/chat.enums.ts` | one enum member: `ChatFormCommandAction.PANEL` |
| `lib/types/chat.d.ts` | optional `icon` on `ChatFormCommand`; an `extraCommands` option |
| `lib/utils/chat-commands.ts` | appends `options.extraCommands?.()` to the registry |
| `lib/hooks/use-chat-form-pickers.svelte.ts` | passes the getter in; one dispatch case |
| `ChatFormPickerCommand.svelte` | honours a per-command icon, plus the fallback icon |
| `lib/utils/panel-command-runtime.ts` | **new** — the panel's single import surface for node_modules |

## The one thing to know before editing it

Anything a panel command needs from node_modules must go through
`panel-command-runtime.ts`. A bare package specifier (`@lucide/svelte`,
`svelte-sonner`) in a file under `panel/frontend` resolves node_modules relative
to *its own* path, never reaching `tools/ui/node_modules`, and fails the build
with `vite:load-fallback`. That file is the same workaround `RightBar.svelte`
documents for its two icons, and it is why adding a new icon to a panel feature
means editing this module rather than the feature's own files.

It is deliberately not exported through the `$lib/utils` barrel, so that no two
modules ever edit the same barrel file.