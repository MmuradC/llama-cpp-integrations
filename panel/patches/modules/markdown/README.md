# markdown

**Applies to:** NOT YET EXTRACTED — the patch file for this module still has to be generated against the tree a given build uses (see ../README.md). What follows describes the change it must contain.

```sh
git apply panel/patches/modules/markdown/markdown.patch   # once generated
```

## What it does

Rendered output: enhanced code blocks and their styling.

## The change, precisely

Touches MarkdownContent plus the code-block, markdown and icon constants. Self-contained: no other module depends on it and it depends on none.
