# incognito

**Applies to:** NOT YET EXTRACTED — the patch file for this module still has to be generated against the tree a given build uses (see ../README.md). What follows describes the change it must contain.

```sh
git apply panel/patches/modules/incognito/incognito.patch   # once generated
```

## What it does

A chat that is never written to disk.

## The change, precisely

incognitoChatStore holds the conversation and its messages in memory; DatabaseService - the single choke point every write goes through - intercepts by conversation or message id and serves reads from memory, so no row ever reaches IndexedDB. The localStorage stream-resume write is skipped for the same reason. The chat exists for as long as the tab does and is gone on reload.
