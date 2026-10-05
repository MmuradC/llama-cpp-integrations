#!/usr/bin/env python3
"""Mermaid diagrams as MCP tools for the llama.cpp Web UI.

Adapted from Google-Vertex-AI/servers/mermaid-mcp (Node + mermaid-cli +
puppeteer). The llama.cpp setup cannot carry that rendering stack here — it
needs Chromium and vertex's own node_modules — so this server is code-only:
it never touches a browser or the disk. It picks the right diagram type for a
plain-language request and hands back the finished diagram as a ```mermaid
fence, which the Web UI (and any Mermaid-rendering chat client) draws inline.

Two tools, mirroring the vertex server's visualize flow in one round each:

    mermaid_pick     request -> recommended diagram type(s) + starter syntax
    mermaid_chart    type + finished code -> validated ```mermaid block

Configuration (env):
    MERMAID_SUGGESTIONS   how many types mermaid_pick suggests when the
                          request is ambiguous, default 4
    MERMAID_TYPES         comma-separated allow-list of diagram type ids from
                          this server's catalog, e.g. "flowchart,sequence".
                          Default: every type the catalog knows.
"""

from __future__ import annotations

import os
import re
from mcp.server.fastmcp import FastMCP

SUGGESTION_COUNT = int(os.environ.get("MERMAID_SUGGESTIONS", "4"))

# ---------------------------------------------------------------------------
# Catalog. Same shape as the vertex server's src/catalog.js, trimmed to the
# types that are actually useful from a terminal-style chat: ids match Mermaid
# 11, keywords score type choice from the request text.
#
#   strong   — terms that on their own point at this type (3 points each)
#   keywords — supporting terms that only nudge (1 point each)
#   phrases  — substring matches on how people phrase the request (3 points)
# Naming the type outright scores 6 and always wins.
# ---------------------------------------------------------------------------

CATALOG: list[dict] = [
    {
        "id": "flowchart", "label": "Flowchart", "header": "flowchart",
        "use": "Steps, branches and decisions in a process or system.",
        "strong": ["flowchart", "workflow", "process", "decision", "pipeline"],
        "keywords": ["flow", "step", "steps", "branch", "if", "logic",
                     "algorithm", "path", "route", "stage"],
        "phrases": ["how it works", "decision tree", "control flow",
                    "data flow", "step by step"],
        "template": (
            "flowchart TD\n"
            "    A[Start] --> B{Is it valid?}\n"
            "    B -- Yes --> C[Process it]\n"
            "    B -- No --> D[Reject]\n"
            "    C --> E[Done]\n"
            "    D --> E"
        ),
    },
    {
        "id": "sequence", "label": "Sequence diagram", "header": "sequenceDiagram",
        "use": "Ordered messages between participants over time — APIs, protocols, handshakes.",
        "strong": ["sequence", "request", "response", "api", "handshake",
                   "protocol", "rpc", "webhook", "roundtrip"],
        "keywords": ["sequence", "calls", "message", "messages",
                     "interaction", "client", "server", "auth", "oauth"],
        "phrases": ["talks to", "calls the", "back and forth",
                    "request flow", "login flow"],
        "template": (
            "sequenceDiagram\n"
            "    autonumber\n"
            "    participant C as Client\n"
            "    participant S as Server\n"
            "    participant D as Database\n"
            "    C->>S: POST /login\n"
            "    S->>D: find user\n"
            "    D-->>S: user record\n"
            "    S-->>C: 200 + token"
        ),
    },
    {
        "id": "class", "label": "Class diagram", "header": "classDiagram",
        "use": "Types, fields, methods and inheritance in object-oriented code.",
        "strong": ["class", "classes", "oop", "inheritance", "interface",
                   "uml", "extends", "implements", "composition"],
        "keywords": ["object", "method", "attribute", "type", "types",
                     "model", "schema", "abstract"],
        "phrases": ["class hierarchy", "object model", "domain model",
                    "type hierarchy"],
        "template": (
            "classDiagram\n"
            "    class Animal {\n"
            "        +String name\n"
            "        +int age\n"
            "        +speak() void\n"
            "    }\n"
            "    Animal <|-- Dog"
        ),
    },
    {
        "id": "state", "label": "State diagram", "header": "stateDiagram-v2",
        "use": "States a thing moves through and the events that drive the moves.",
        "strong": ["state", "fsm", "automaton", "lifecycle", "state machine"],
        "keywords": ["state", "status", "transition", "event", "idle",
                     "machine", "mode"],
        "phrases": ["lifecycle of", "moves through"],
        "template": (
            "stateDiagram-v2\n"
            "    [*] --> Idle\n"
            "    Idle --> Running: start\n"
            "    Running --> Idle: stop\n"
            "    Running --> Error: crash\n"
            "    Error --> Idle: reset"
        ),
    },
    {
        "id": "er", "label": "Entity-relationship diagram", "header": "erDiagram",
        "use": "Database tables/entities, their fields and how they relate.",
        "strong": ["er diagram", "entity", "relation", "database", "schema", "sql"],
        "keywords": ["table", "tables", "foreign", "primary", "key",
                     "column", "column", "relational", "db"],
        "phrases": ["entity relationship", "relational model", "many to many"],
        "template": (
            "erDiagram\n"
            "    USER ||--o{ ORDER : places\n"
            "    ORDER ||--|{ LINE_ITEM : contains\n"
            "    USER {\n"
            "        int id PK\n"
            "        string email\n"
            "    }"
        ),
    },
    {
        "id": "mindmap", "label": "Mindmap", "header": "mindmap",
        "use": "Hierarchical brainstorm; one center spreading outward.",
        "strong": ["mindmap", "mind map", "brainstorm"],
        "keywords": ["idea", "ideas", "structure", "overview", "topic",
                     "topics", "map"],
        "phrases": ["break down", "map out", "mind map"],
        "template": (
            "mindmap\n"
            "  root((Project))\n"
            "    Frontend\n"
            "      React\n"
            "      Styling\n"
            "    Backend\n"
            "      API\n"
            "      Database"
        ),
    },
    {
        "id": "gantt", "label": "Gantt chart", "header": "gantt",
        "use": "Tasks on a calendar with start dates and durations.",
        "strong": ["gantt", "schedule"],
        "keywords": ["task", "tasks", "milestone", "deadline", "week",
                     "duration", "planning", "roadmap"],
        "phrases": ["project plan", "over weeks", "work plan"],
        "template": (
            "gantt\n"
            "    title Project plan\n"
            "    dateFormat YYYY-MM-DD\n"
            "    section Research\n"
            "    Survey :a1, 2026-01-01, 14d\n"
            "    section Build\n"
            "    MVP :2026-01-15, 30d"
        ),
    },
    {
        "id": "timeline", "label": "Timeline", "header": "timeline",
        "use": "Ordered events in named periods; compact history.",
        "strong": ["timeline", "chronology"],
        "keywords": ["history", "period", "era", "phase", "evolution"],
        "phrases": ["history of", "over time", "timeline of"],
        "template": (
            "timeline\n"
            "    title Project history\n"
            "    2025-09 : First commit\n"
            "    2025-11 : MVP shipped\n"
            "    2026-01 : v1.0"
        ),
    },
    {
        "id": "pie", "label": "Pie chart", "header": "pie",
        "use": "Part-to-whole shares of one set of numbers.",
        "strong": ["pie chart", "shares of"],
        "keywords": ["pie", "share", "distribution", "percent",
                     "percentage", "breakdown", "split"],
        "phrases": ["percentage breakdown"],
        "template": (
            "pie title Languages in use\n"
            "    \"Python\" : 45\n"
            "    \"TypeScript\" : 35\n"
            "    \"Other\" : 20"
        ),
    },
    {
        "id": "quadrant", "label": "Quadrant chart", "header": "quadrantChart",
        "use": "Points placed on two axes to compare trade-offs.",
        "strong": ["quadrant"],
        "keywords": ["axis", "axes", "compare", "matrix", "urgency",
                     "impact", "effort"],
        "phrases": ["impact vs effort", "vs effort"],
        "template": (
            "quadrantChart\n"
            "    title Priorities\n"
            "    x-axis Low effort --> High effort\n"
            "    y-axis Low impact --> High impact\n"
            "    quadrant-1 Do soon\n"
            "    quadrant-2 Plan well\n"
            "    Item A: [0.3, 0.7]"
        ),
    },
    {
        "id": "gitgraph", "label": "Git graph", "header": "gitGraph",
        "use": "Branches, merges and commits as they happened in a repo.",
        "strong": ["gitgraph", "git graph", "branch", "commit history"],
        "keywords": ["git", "merge", "rebase", "branch"],
        "phrases": ["git history", "branching"],
        "template": (
            "gitGraph\n"
            "    commit\n"
            "    branch feature\n"
            "    commit\n"
            "    checkout main\n"
            "    merge feature"
        ),
    },
    {
        "id": "journey", "label": "User journey", "header": "journey",
        "use": "Stages a person goes through with satisfaction scores.",
        "strong": ["journey", "user journey"],
        "keywords": ["customer", "stage", "onboarding",
                     "satisfaction", "experience"],
        "phrases": ["journey of", "experience of"],
        "template": (
            "journey\n"
            "    title User onboarding\n"
            "    section Sign up\n"
            "      Open form: 5: User\n"
            "      Submit: 3: User\n"
            "    section First run\n"
            "      Explore: 4: User"
        ),
    },
    {
        "id": "requirement", "label": "Requirements diagram", "header": "requirementDiagram",
        "use": "Requirements and the elements that satisfy or refine them.",
        "strong": ["requirement", "requirements"],
        "keywords": ["spec", "trace", "verification"],
        "phrases": ["requirement tracing", "spec diagram"],
        "template": (
            "requirementDiagram\n"
            "    requirement req_1 {\n"
            "        id: 1\n"
            "        text: system responds within 200 ms\n"
            "        risk: low\n"
            "        verifymethod: test\n"
            "    }"
        ),
    },
    {
        "id": "sankey", "label": "Sankey", "header": "sankey-beta",
        "use": "Flow of quantities between sources and sinks.",
        "strong": ["sankey"],
        "keywords": ["quantity", "budget", "throughput"],
        "phrases": ["flow of money"],
        "template": (
            "sankey-beta\n"
            "Source,Destination,Weight\n"
            "Income,Savings,20\n"
            "Income,Rent,30"
        ),
    },
    {
        "id": "block", "label": "Block diagram", "header": "block-beta",
        "use": "Boxes and columns; rough system layout without flowchart arrows.",
        "strong": ["block diagram"],
        "keywords": ["blocks", "panel", "columns", "layout"],
        "phrases": ["layout of"],
        "template": (
            "block-beta\n"
            "    columns 2\n"
            "    a[Input] b[Processor]\n"
            "    c[Output] d[Storage]"
        ),
    },
]

# ---------------------------------------------------------------------------
# Type picking — same scoring as the vertex server, as a plain function.
# ---------------------------------------------------------------------------

_CATALOG_BY_ID = {entry["id"]: entry for entry in CATALOG}

_types_env = os.environ.get("MERMAID_TYPES", "").strip()
if _types_env:
    _allowed = {t.strip().lower() for t in _types_env.split(",") if t.strip()}
    CATALOG = [e for e in CATALOG if e["id"] in _allowed]
    _CATALOG_BY_ID = {e["id"]: e for e in CATALOG}


def score_entry(entry: dict, text_lower: str, tokens: set[str]) -> int:
    """Points for one type, for one request. Naming the id scores 6 and wins."""
    if entry["id"] in tokens:
        return 6
    score = 0
    score += 3 * sum(1 for t in entry["strong"] if t in tokens)
    score += sum(1 for t in entry["keywords"] if t in tokens)
    score += 3 * sum(1 for p in entry["phrases"] if p in text_lower)
    return score


def ranked_types(request: str) -> list[dict]:
    """Catalog entries best matching a plain-language request, best first."""
    text = " ".join(request.split()).lower()
    tokens = set(re.findall(r"[a-z][a-z0-9_-]*", text))
    scored = [(score_entry(e, text, tokens), e) for e in CATALOG]
    scored = [s for s in scored if s[0] > 0]
    scored.sort(key=lambda pair: (-pair[0], pair[1]["id"]))
    return [e for _, e in scored]


def lines_result(lines: list[str]) -> dict:
    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


def code_result(code: str) -> dict:
    """The finished diagram, as a ```mermaid fence the Web UI renders inline.

    The trailing output rule is load-bearing, measured 2026-09-27: an opencode
    deepseek-v4.1 chat that received the raw 1,769-char architecture fence
    discarded it and replied with 170 chars of ASCII deco (`──►`, no
    flowchart header), which the chat's mermaid renderer rejected as
    "Syntax error in text". The same chat given the explicit verbatim rule
    below emitted all 1,769 characters byte-for-byte. Models treat tool
    results as loose source material by default, so the rule names the exact
    failure (shortening, ASCII arrows, re-labeling) rather than a vague
    "pass it on".
    """
    return {
        "content": [{
            "type": "text",
            "text": f"```mermaid\n{code.strip()}\n```\n"
                    "Show this block to the user as-is — do not describe it, "
                    "re-type it, or claim it was saved anywhere.\n\n"
                    "MANDATORY OUTPUT RULE: your reply must contain exactly "
                    "the fenced mermaid block above, verbatim, "
                    "character-for-character. Do not shorten, re-draw, "
                    "translate into ASCII art, re-label nodes, or add arrows "
                    "like ──►. You may add nothing before or after except an "
                    "empty line. The renderer errors on re-drawn text.",
        }]
    }


def validate_code(_type_id: str, code: str) -> list[str]:
    """Cheap structural checks — no browser, so no full parser. Errors list."""
    errors: list[str] = []
    lines = code.strip().splitlines()
    if not lines:
        return ["code is empty"]
    headers = sorted(e["header"] for e in _CATALOG_BY_ID.values())
    first = lines[0].strip()
    if not any(first == h or first.startswith(h + " ") or first.startswith(h)
               for h in headers):
        errors.append(
            "first line does not start with a catalog header; expected one of: "
            + ", ".join(headers)
        )
    for i, line in enumerate(lines[1:], start=2):
        if line.count("(") != line.count(")"):
            errors.append(f"line {i}: unbalanced parentheses: {line.strip()!r}")
        if "{{" in line and line.count("{{") != line.count("}}"):
            errors.append(f"line {i}: unbalanced double braces: {line.strip()!r}")
    return errors


mcp = FastMCP(
    "mermaid-mcp",
    instructions=(
        "Picks a Mermaid diagram type for a request and returns the finished "
        "diagram as a ```mermaid code block for this chat to render inline. "
        "No files are written; nothing is rendered into an image. Show the "
        "returned block to the user as-is.\n\n"
        "Flow: when unsure which type fits, call `mermaid_pick` with the "
        "user's request text, then write the diagram in the chosen type's "
        "syntax and call `mermaid_chart` with `type_id` and `code`. Types: "
        + ", ".join(e["id"] for e in CATALOG)
        + ". When the user already names the type, skip mermaid_pick and "
        "call mermaid_chart directly. Output is code-only — never describe a "
        "diagram in prose when mermaid_chart can return it."
    ),
)


@mcp.tool()
def mermaid_pick(request: str) -> dict:
    """Pick a Mermaid diagram type from a plain-language request.

    Return the best-matching type with starter syntax when the request is
    clear, or the top few candidates to choose between when it is ambiguous.

    Hard steering first, before any ranking: requests about llama.cpp's own
    integration - its system architecture, what is wired where, the MCP/
    panel/router layout - must answered with the live fetcher tool
    (mermaid_architecture), not a hand-composed type. A hand-made diagram
    goes stale the moment a file moves; the fetcher's only source is a
    real-time regeneration, which is why this is a rule and not a hint.
    """
    lowered = request.lower()
    if any(term in lowered for term in (
        "system architecture", "integration architecture",
        "how is my setup", "what is wired", "llama.cpp integrations",
        "be the architecture diagram of this setup",
    )):
        return {
            "tool_transfer": "mermaid_architecture",
            "reason": (
                "This looks like a request about the llama.cpp integration "
                "itself. Use the mermaid_architecture tool instead - it "
                "regenerates the diagram from the actual machine each call, "
                "so what the user sees reflects real registered state, not "
                "a possibly stale description."
            ),
        }
    if not request.strip():
        return lines_result(["Empty request — describe what should be drawn."])
    ranked = ranked_types(request)
    if not ranked:
        known = ", ".join(e["id"] for e in CATALOG)
        return lines_result([
            f"No diagram type matched. Available types: {known}.",
            "If none fits, say so instead of picking one.",
            "When the user names a type, use that id with mermaid_chart.",
        ])
    best = ranked[0]
    if len(ranked) == 1 or len(ranked) > SUGGESTION_COUNT:
        ranked = ranked[:1]
    if len(ranked) == 1:
        e = best
        return lines_result([
            f"Type: {e['label']} (id: {e['id']})",
            f"Best for: {e['use']}",
            "Write the real diagram in this syntax (replace the placeholders "
            "with the user's subject) and call mermaid_chart with",
            f"type_id: {e['id']} and your finished code.",
            "",
            "Starter syntax:",
            "```mermaid",
            e["template"],
            "```",
        ])
    lines = [
        f"Request \"{request}\" is ambiguous — it fits several diagram "
        "types. Ask the user to choose one of these, do not pick for them:",
        "",
    ]
    lines += [
        f"{i}. {e['label']} (id: {e['id']}) — {e['use']}"
        for i, e in enumerate(ranked, start=1)
    ]
    lines += [
        "",
        "Once the user picks, write the diagram in that type's syntax and "
        "call mermaid_chart.",
    ]
    return lines_result(lines)


@mcp.tool()
def mermaid_chart(type_id: str, code: str) -> dict:
    """Return finished Mermaid code as a ```mermaid block this chat renders.

    type_id: catalog id (e.g. "flowchart", "sequence"). Checked against the
    code's own header — pass the type you actually wrote.
    code: complete Mermaid source, without a ``` fence (one is stripped if
    supplied).
    """
    code = code.strip()
    fence = re.match(r"^```(?:mermaid|mmd)?\s*\n([\s\S]*?)\n?```$", code)
    if fence:
        code = fence.group(1).strip()
    errors = validate_code(type_id, code)
    if type_id not in _CATALOG_BY_ID:
        errors.insert(0, (
            f"unknown type_id {type_id!r}; available types: "
            + ", ".join(sorted(_CATALOG_BY_ID))
        ))
    if errors:
        return lines_result(
            ["Mermaid render failed:"] + [f"  - {e}" for e in errors]
            + ["Fix the code and call mermaid_chart again.", "", code]
        )
    return code_result(code)


ARCH_URL = (
    os.environ.get(
        "MERMAID_ARCHITECTURE_URL",
        "http://127.0.0.1:9010/api/architecture.mmd",
    ).rstrip("/")
)


@mcp.tool()
def mermaid_architecture() -> dict:
    """THE system-architecture diagram, regenerated live from the actual
    machine each call - felibriage, but real: the panel backend reads its own
    registered API routes, the MCP server list from llama-mcp-servers.json,
    the UI feature files in the served llama.cpp tree, the learned context
    limits, and the relay's live journal lines, then emits the flowchart. Use
    this instead of composing an architecture diagram by hand whenever the
    request is about llama.cpp's own integration layout, since only this
    cannot drift from reality: hand-built diagrams go stale the moment a file
    moves, this one is a portrait fetched at call time.

    Returns:
        The finished mermaid in a fenced block, ready for the chat to render.
        The panel backend must be running (systemctl --user status
        mcp-panel-backend) for a fetch; otherwise the error text says so and
        no fallback guess is made - a stale or invented diagram would defeat
        the point of the tool existing.
    """
    try:
        import httpx

        resp = httpx.get(ARCH_URL, timeout=15)
        resp.raise_for_status()
        code = resp.text.strip()
    except Exception as exc:
        return {
            "error": (
                "could not fetch the live architecture from "
                f"{ARCH_URL} ({exc}) - is mcp-panel-backend running? Nothing "
                "is invented to take its place: the whole point of this tool "
                "is that its answer is the machine's real state."
            )
        }

    errors = validate_code("flowchart", code)
    if errors:
        return {
            "error": "the backend returned a mermaid that no longer validates: " + "; ".join(errors),
            "raw": code,
        }

    return code_result(code)


if __name__ == "__main__":
    mcp.run("stdio")
