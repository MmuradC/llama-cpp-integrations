# rag-pages

**Applies to:** NOT YET EXTRACTED — the patch file for this module still has to be generated against the tree a given build uses (see ../README.md). What follows describes the change it must contain.

```sh
git apply panel/patches/modules/rag-pages/rag-pages.patch   # once generated
```

## What it does

Routes surfacing the RAG collection browser and editor.

## The change, precisely

Two thin route files. The pages themselves live in panel/frontend and are reached through the @mcp-panel alias, which is why these are four-line files rather than real implementations.
