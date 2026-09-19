# permission-modes

**Applies to:** NOT YET EXTRACTED — the patch file for this module still has to be generated against the tree a given build uses (see ../README.md). What follows describes the change it must contain.

```sh
git apply panel/patches/modules/permission-modes/permission-modes.patch   # once generated
```

## What it does

Permission mode per chat, and a gate that asks before anything that can modify state.

## The change, precisely

permissionModeStore keeps per-conversation modes (populated by the panel) and falls back to one global value; the gate resolves the mode for the conversation the tool call belongs to. The always-allow list is consulted only for tools toolCanWrite() says cannot modify anything, EDIT_APPROVED_TOOLS is empty (Accept Edits approves nothing), and the default mode is manual. Two bugs of the same shape are recorded here: a remembered approval outranking the mode picker, and accept-edits approving whole-file writes.
