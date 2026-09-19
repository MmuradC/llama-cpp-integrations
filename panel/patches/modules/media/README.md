# media

**Applies to:** NOT YET EXTRACTED — the patch file for this module still has to be generated against the tree a given build uses (see ../README.md). What follows describes the change it must contain.

```sh
git apply panel/patches/modules/media/media.patch   # once generated
```

## What it does

Images work for models that cannot see.

## The change, precisely

On paste/drop, an image the active model cannot take is posted to the panel backend (/api/upload-image), which saves it and returns a path. The image is still attached so the composer and transcript show the preview, but what goes to a text-only model is the bare path (the model then reads it with a vision MCP tool such as vision_inspect/vision_ocr). read_media's own error, when a model does have it but the file is the wrong kind, points at those tools instead of dead-ending.
