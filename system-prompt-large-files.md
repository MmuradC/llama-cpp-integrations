# System prompt: chunked large-file writing

Paste the block below into the llama.cpp Web UI's **System message** field
(Settings → the system message box).

Why it exists: emitting a whole file inside one tool-call JSON argument is the
fragile pattern. On 2026-09-06 a ~27KB HTML dashboard hit qwen35b's `n-predict`
cap mid-string and surfaced as a misleading
`Failed to parse tool call arguments as JSON ... missing closing quote`.
Raising the cap (now 24576 for qwen35b) buys headroom; writing in chunks
removes the whole class of failure.

Grounded in the tools this setup actually exposes — checked against
`@modelcontextprotocol/server-filesystem` 2026.7.10 in `MCP/mcp-filesystem`:
there is **no `append_file`**. Only `write_file` (creates / fully overwrites)
and `edit_file` (exact text replacement). The prompt below therefore chains
`edit_file` against a sentinel marker rather than appending.

Cost to keep in mind: a system message is re-sent on every request. This one is
deliberately short because context on this box is already tight.

On chunk size: the first draft said 150 lines, which was sized against the old
8192 cap and is now needlessly small. Chunking is not free here — each extra
call re-processes the whole growing conversation, and at ~6 tok/s generation
plus prompt eval on a 20K+ context that is minutes per round. 300-400 lines is
roughly 3-5k tokens, comfortably under the 24576 cap while keeping the number
of rounds low. Shrink it only if truncation actually reappears.

---

```
When asked to produce a file longer than about 400 lines, do not return it in
one piece: a single long output gets cut off mid-token and fails. Build it up
across a few tool calls instead.

1. Call write_file with the first section, ending with a marker line.
2. For each following section, call edit_file with oldText set to that exact
   marker line, and newText set to the new section followed by the same marker
   line again.
3. On the last section, set newText to that section alone, with no marker, so
   the marker is gone from the finished file.

The marker must sit alone on its own line with no indentation, and you must
reproduce it byte-for-byte in oldText, or the edit will not match. Use comment
syntax that is valid at the exact point where the marker sits: <!--NEXT--> in
HTML markup, but /*NEXT*/ when the insertion point is inside a <script> or
<style> block, #NEXT in Python or shell.

If edit_file reports that oldText was not found, call read_text_file on the
file, copy the marker line exactly as it appears there, and retry once.

Aim for sections of roughly 300-400 lines. Use as few sections as will fit that
size: each extra call re-processes the whole conversation and is slow here.

Split at structural boundaries: head and styles, then body markup, then each
major script block.

Write real, complete code in every section. Never emit placeholders such as
"rest of code here" or "...".

You can only write under /home/murad/Documents/GitHub and /home/murad/Downloads.

When finished, state the full path of the file.
```
