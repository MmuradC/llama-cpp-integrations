#!/usr/bin/env bash
#
# apply.sh - apply every patch module to a llama.cpp checkout, in manifest order.
#
#   bash panel/patches/apply.sh /path/to/llama.cpp        # apply
#   bash panel/patches/apply.sh /path/to/llama.cpp --check # verify only, no writes
#
# The checkout must be at the manifest's base revision (checked below). Patches
# are applied with `git apply`; nothing is committed and no index state is
# touched beyond the patch application itself.
#
set -uo pipefail

TREE=${1:-}
CHECK=0
[ "${2:-}" = "--check" ] && CHECK=1

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
MANIFEST="$HERE/manifest.json"

if [ -z "$TREE" ]; then
	sed -n '2,12p' "$0"
	exit 2
fi
if [ ! -d "$TREE/.git" ] && [ ! -f "$TREE/.git" ]; then
	echo "! $TREE is not a git checkout" >&2
	exit 2
fi

BASE=$(python3 -c "import json;print(json.load(open('$MANIFEST'))['base']['commit'])")
HEAD=$(git -C "$TREE" rev-parse --short=10 HEAD)

echo "tree: $TREE"
echo "base: $BASE (manifest)"
echo "head: $HEAD"
if [ "$CHECK" = 0 ] && [ "$HEAD" != "$BASE" ]; then
	echo "warn: HEAD is not the base revision - patches may not apply cleanly"
	echo "      (that is expected if you are re-targeting a newer upstream)"
fi
echo

FAILED=0
python3 - "$MANIFEST" <<'EOF' >/tmp/apply-order.txt
import json, sys
m = json.load(open(sys.argv[1]))
for mod in m["modules"]:
    if mod.get("patch") and mod.get("files"):
        print(mod["patch"])
EOF

N=0
while read -r rel; do
	[ -z "$rel" ] && continue
	N=$((N + 1))
	name=$(basename "$rel" .patch)
	patch="$HERE/$rel"
	if [ ! -s "$patch" ]; then
		printf '  %-20s MISSING (%s)\n' "$name" "$rel"
		FAILED=$((FAILED + 1))
		continue
	fi
	if [ "$CHECK" = 1 ]; then
		if git -C "$TREE" apply --check --whitespace=nowarn "$patch" 2>/dev/null; then
			printf '  %-20s would apply\n' "$name"
		else
			printf '  %-20s WOULD FAIL\n' "$name"
			FAILED=$((FAILED + 1))
		fi
	else
		if out=$(git -C "$TREE" apply --whitespace=nowarn "$patch" 2>&1); then
			printf '  %-20s applied\n' "$name"
		else
			printf '  %-20s FAILED: %s\n' "$name" "$(echo "$out" | head -1)"
			FAILED=$((FAILED + 1))
		fi
	fi
done </tmp/apply-order.txt

echo
if [ "$FAILED" = 0 ]; then
	[ "$CHECK" = 1 ] && echo "OK: all $N patch(es) would apply" || echo "OK: $N patch(es) applied"
	echo "next: build tools/ui for the dist, then cmake+build llama.cpp"
else
	echo "$FAILED of $N patch(es) failed"
	exit 1
fi
