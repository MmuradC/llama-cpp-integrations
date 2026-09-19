# tailwind-source

**Applies to:** NOT YET EXTRACTED — the patch file for this module still has to be generated against the tree a given build uses (see ../README.md). What follows describes the change it must contain.

```sh
git apply panel/patches/modules/tailwind-source/tailwind-source.patch   # once generated
```

## What it does

Makes the panel's Tailwind classes exist at all.

## The change, precisely

Tailwind generates a class only if it is found in a scanned source file, and tools/ui/src/app.css scanned only '.'. Every utility used exclusively by panel components was therefore never generated: the dashboard's grid-cols-4 collapsed to a single column, its colour ramp and spacing vanished, and the panel rendered unstyled. One @source line for panel/frontend fixes it for every panel component.
