#!/usr/bin/env python3
"""extract.py - regenerate every patch module from the manifest and verify it.

Reads panel/patches/manifest.json, writes one patch per module as

    git diff <base> -- <that module's files>

so each patch applies to a pristine checkout of the base revision, then proves
it: creates a detached worktree at the base, applies the patches in manifest
order, and compares every covered file byte-for-byte against the tree the
patches were taken from.  Any mismatch is an error, not a warning.

    python3 panel/patches/extract.py                 # dry run, report only
    python3 panel/patches/extract.py --write         # write patches + verify
    python3 panel/patches/extract.py --write --no-verify

Options: --tree DIR (llama.cpp tree, default fork/pkg-build/src/llama.cpp),
--base REV (default from the manifest), --only NAME (repeatable).

Note on untracked files: files that exist in the working tree but not in git
(the tree was carrying 329 uncommitted edits, 21 of them brand new) are added
with `git add -N` for the duration of the diff and unstaged again afterwards, so
they appear in the patches as proper new files and your index ends up as it was.
"""
import argparse, hashlib, json, os, subprocess, sys, tempfile, shutil

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def run(args, cwd=None, check=True, capture=True):
    r = subprocess.run(args, cwd=cwd, capture_output=capture, text=True)
    if check and r.returncode != 0:
        sys.stderr.write(f"! {' '.join(args)}\n{r.stdout}{r.stderr}\n")
        raise SystemExit(1)
    return r


def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tree", default=os.path.join(ROOT, "fork/pkg-build/src/llama.cpp"))
    ap.add_argument("--base", default=None)
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--no-verify", action="store_true")
    ap.add_argument("--only", action="append")
    args = ap.parse_args()

    manifest_path = os.path.join(ROOT, "panel/patches/manifest.json")
    m = json.load(open(manifest_path))
    base = args.base or m["base"]["commit"]
    tree = args.tree

    if not os.path.isdir(os.path.join(tree, ".git")) and not os.path.isfile(os.path.join(tree, ".git")):
        raise SystemExit(f"! {tree} is not a git checkout - patches cannot be generated from it")

    head = run(["git", "-C", tree, "rev-parse", "--short", "HEAD"]).stdout.strip()
    print(f"tree      {tree}")
    print(f"base      {base}  (HEAD is {head})")
    print()

    modules = [mod for mod in m["modules"] if mod.get("files")]
    if args.only:
        modules = [mod for mod in modules if mod["name"] in args.only]

    # ---- intent-to-add for brand new files ---------------------------------- #
    all_files = [f for mod in modules for f in mod["files"]]
    untracked = [f for f in run(["git", "-C", tree, "ls-files", "--others", "--exclude-standard", "--"] + all_files, check=False).stdout.split("\n") if f]
    if untracked:
        print(f"adding {len(untracked)} untracked file(s) with git add -N (restored at the end)")
        run(["git", "-C", tree, "add", "-N", "--"] + untracked)

    written, problems = [], []
    try:
        for mod in modules:
            name, files = mod["name"], mod["files"]
            patch = run(["git", "-C", tree, "diff", base, "--"] + files).stdout
            dest = os.path.join(ROOT, "panel/patches", mod["patch"])
            n_files = len([l for l in patch.splitlines() if l.startswith("diff --git ")])
            empty = "yes" if not patch.strip() else "no"
            print(f"  {name:20s} {len(files):3d} listed, {n_files:3d} in patch, {len(patch):7d} bytes, empty={empty}")
            if len(files) != n_files:
                problems.append(f"{name}: listed {len(files)} files but the patch touches {n_files}")
            if not patch.strip():
                problems.append(f"{name}: empty patch")
                continue
            if args.write:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "w") as fh:
                    fh.write(patch)
                written.append(dest)
    finally:
        if untracked:
            run(["git", "-C", tree, "reset", "-q", "--"] + untracked, check=False)
            gone = run(["git", "-C", tree, "ls-files", "--others", "--exclude-standard", "--"] + all_files, check=False).stdout.split()
            print(f"index restored ({len(gone)} file(s) untracked again)")

    print()
    if not args.write:
        print("dry run: nothing written (use --write)")
        return 0 if not problems else 1

    if args.no_verify:
        print(f"wrote {len(written)} patch file(s), verification skipped")
        return 0 if not problems else 1

    # ---- verification ------------------------------------------------------- #
    print("verifying: apply all patches to a pristine checkout of the base and compare")
    wt = tempfile.mkdtemp(prefix="extract-verify-")
    shutil.rmtree(wt)
    run(["git", "-C", tree, "worktree", "add", "--detach", wt, base])
    try:
        for mod in modules:
            path = os.path.join(ROOT, "panel/patches", mod["patch"])
            r = run(["git", "-C", wt, "apply", "--whitespace=nowarn", path], check=False)
            if r.returncode != 0:
                problems.append(f"{mod['name']}: patch does not apply to a pristine {base}")
                print(f"  {mod['name']:20s} APPLY FAILED\n{r.stderr[:400]}")
            else:
                print(f"  {mod['name']:20s} applied")

        # compare every covered file: content must match, deletions must be gone
        bad = 0
        checked = 0
        for mod in modules:
            for f in mod["files"]:
                src, dst = os.path.join(tree, f), os.path.join(wt, f)
                if not os.path.exists(src):
                    if os.path.exists(dst):
                        bad += 1
                        print(f"    ! {f} should be deleted but exists after applying")
                    continue
                checked += 1
                if not os.path.exists(dst):
                    bad += 1
                    print(f"    ! {f} missing after applying")
                elif md5(src) != md5(dst):
                    bad += 1
                    print(f"    ! {f} differs after applying")
        print(f"  compared {checked} file(s), {bad} mismatch(es)")
        if bad:
            problems.append(f"{bad} file(s) did not round-trip")

        changed = [l[3:] for l in run(["git", "-C", wt, "status", "--porcelain", "-uall"]).stdout.splitlines()]
        expected = sorted({f for mod in modules for f in mod["files"]})
        got = sorted(changed)
        print(f"  applied worktree changed {len(got)} file(s); patch set covers {len(expected)}")
        if got != expected:
            extra = sorted(set(got) - set(expected))
            missing = sorted(set(expected) - set(got))
            problems.append(f"changed-file set differs (extra={extra[:5]}, missing={missing[:5]})")
    finally:
        run(["git", "-C", tree, "worktree", "remove", "--force", wt], check=False)

    print()
    if problems:
        print("PROBLEMS:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"OK: {len(written)} patch(es) written and verified against {base}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
