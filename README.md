# mnemosyne

Mnemosyne is a Titaness in Greek mythology. She is the personification of memory and remembrance, and a fitting namesake for a project that serves as a clean, structured layer on top of the raw conversation logs produced by Claude Code. Mnemosyne takes the noisy, append-only JSONL files that Claude Code generates and transforms them into human- and agent-readable formats, while also providing tools for browsing, exporting, merging, and searching through past sessions.

> Memory + context suite for Claude Code and Codex CLI — clean transcript exports, curated memories, hidden subagent transcripts, cross-session merge, cross-tool recall, memory drift checks, project archive, MCP server, and a Claude Code plugin. For humans and agents.

Claude Code stores every conversation under
`~/.claude/projects/<slug>/<session-uuid>.jsonl` — a noisy, append-only log
of every event the IDE saw. This project is **the clean read/transform/query
layer on top of that archive**: take raw JSONL → produce the canonical
human- and agent-readable version → serve it through the access patterns
that actually matter.

## What you can do with it

| Workflow | Command |
| --- | --- |
| "Export all my sessions for this project as readable markdown" | `syne export-all` |
| "Just the last month, please" | `syne export-all --since 2026-04-01` |
| "Only the sessions about auth" | `syne export-all --matching '(?i)auth'` |
| "Combine 3 sessions into one synthesis doc" | `syne merge abc123 def456 ghi789 -o synthesis.md` |
| "All Godot work from this project in one document" | `syne merge --all-from scifigame --matching '(?i)godot' -o godot-archive.md` |
| "Onboarding context — last 5 sessions concatenated" | `syne merge --all-from <project> --last 5 -o context.md` |
| "What did I do this week, across every project" | `syne merge --all-projects --since 2026-05-13 -o week.md` |
| "Back up every Claude Code conversation I've ever had" | `syne export-all --all-projects -o ~/claude-archive` |
| "…and everything else Claude saved alongside them" | `syne export-all --full` |
| "Just this project's curated memories" | `syne export-all --memories` |
| "The full transcripts of every subagent an audit spawned" | `syne export <id> --subagents` |
| "Give me JSONL to feed into an embedding pipeline" | `syne export-all --format jsonl` |
| "Plain text I can paste into another LLM" | `syne export-all --format plain` |
| "Let Claude itself search and load my past sessions and memories" | `syne install` (the MCP server + plugin) |
| "Tell every agent (Claude, opencode, Codex…) to self-align from its own memory" | `syne align` |
| "A small recall brief I can pipe into a session-start hook" | `syne recall --recent` |
| "What did I do in **Codex CLI** on this project?" | `syne codex-list` |
| "Export a Codex rollout as readable markdown" | `syne codex-export <id>` |
| "Which of my curated memories have gone stale?" | `syne drift` |

## Install (two commands)

Requires Python 3.13+ and [uv](https://github.com/astral-sh/uv).

```bash
# 1. install the package — puts `syne` and `syne mcp` on PATH globally
uv tool install mnemosyne-cc
# …or straight from source for the bleeding edge:
# uv tool install git+https://github.com/entro314-labs/mnemosyne

# 2. (optional) wire the Claude Code plugin sidecar — adds slash commands + skill
syne install
```

The PyPI distribution is **`mnemosyne-cc`** (the bare `mnemosyne` name is taken by
an unrelated project); the import package and the `syne` command are still `mnemosyne`.

Step 2 copies the plugin into `~/.claude/plugins/mnemosyne/` and
registers it. Then in Claude Code: `/plugin install
mnemosyne@mnemosyne`. The CLI works fine without
step 2; step 2 is only needed if you want `/recall`, `/history`, `/summon`,
`/export` slash commands inside Claude Code.

Update later: `uv tool upgrade mnemosyne-cc && syne install`. The plugin
directory is **fully managed**: updates overwrite the packaged assets *and
delete files that are no longer part of the release* (renamed commands, removed
skills), so old and new definitions never coexist. Don't keep personal files
inside `~/.claude/plugins/mnemosyne/`.

Uninstall: `syne uninstall && uv tool uninstall mnemosyne-cc`. Uninstall strips
the self-alignment region from the **current** project's instruction files and
prints any *other* registered projects that still carry one (each removable
with `syne align <project-root> --remove`) — nothing outside the cwd is
modified silently.

## The three layers

| Layer | Consumer | What it does |
| --- | --- | --- |
| **CLI (`syne`)** | You, in a terminal | Browse, export, merge, search, drift-check. |
| **MCP server (`syne mcp`)** | Any agent (incl. Claude Code) | 19 read-only tools: self-align, transcripts, memories, handoffs, subagents, Codex rollouts, drift. |
| **Claude Code plugin** | Claude Code specifically | `session-history` skill + `/recall`, `/memories`, `/history`, `/summon`, `/export` slash commands — wires Claude to its own past via the MCP. |

All three share the same parser, renderer, project discovery, and noise
filtering.

## CLI

Run `syne` with no arguments. When the current directory is a known Claude Code
workspace, it loads that workspace's sessions directly — no project chooser.
Run it from anywhere else (or pass `--pick`) to choose from all projects. Otherwise:

```bash
syne list                            # sessions for the cwd's project
syne export <id-or-prefix>           # single session → <project>/.mnemosyne-exports/
syne export-all                      # every session in the project
syne merge <id1> <id2> -o out.md     # combine specific sessions
syne merge --all-from <project>      # combine every session from a project
syne recall [query] [--memories]     # small capped recall brief → stdout (hooks / non-MCP agents)
syne align                           # write the self-alignment directive into CLAUDE.md + AGENTS.md
syne drift                           # verify curated memories against the live repo (staleness check)
syne codex-list [--all]              # Codex CLI rollouts for this project (~/.codex)
syne codex-export <id>               # render one Codex rollout through the same pipeline (+ sidecar)
syne projects                        # registry of all known projects
syne config-show                     # current settings file
syne install / syne uninstall         # plugin sidecar
syne mcp                             # MCP server on stdio (used by plugin)
```

### Filters and scope (on `export-all` and `merge`)

- `--since 2026-05-01` / `--until 2026-05-31` — date-range on `last_timestamp`.
- `--matching '(?i)regex'` — keep sessions whose title or first prompt matches.
- `--last N` (merge only) — keep the N most-recent sessions after filters.
- `--all-from <project>` (merge only) — combine every session in one project.
- `--all-projects` (merge + export-all) — operate across every known project.
- `--mode {transcript,compact,full}` — render-time mode override.
- `--format {markdown,jsonl,plain}` — output format.
- `--no-sidecar` / `--no-index` — skip sidecars or per-project index.

### Artifacts beyond the transcript (on `export` and `export-all`)

Claude Code stores far more than the transcript next to each session — a curated
memory layer, session-handoff summaries, and the full transcripts of every
subagent and workflow agent (the main transcript only keeps each agent's final
result). These are **off by default**; opt in per category:

- `--memories` — the project's curated memory layer (`memory/*.md`): durable
  facts (roadmaps, decisions, gotchas, preferences) with a `[[link]]` graph.
- `--subagents` — the full transcript of every spawned subagent, rendered in the
  same mode as the main transcript.
- `--summaries` — session-memory handoff digests (`session-memory/summary.md`).
- `--workflows` — workflow orchestration scripts, run-journal summaries, and the
  agents each workflow orchestrated.
- `--tool-results` — externalised large tool outputs, deterministically scrubbed.
- `--full` — all five at once. (Distinct from `--mode full`, which only controls
  transcript verbosity.)

The same noise-reduction is applied: subagent/workflow transcripts go through the
full clean+render pipeline; memories and summaries are whitespace-normalised;
tool output is scrubbed; only workflow scripts are copied verbatim (they're code).

### Output layout

A `syne export-all --full` lands like this (artifact bundles only appear for the
categories you enable):

```tree
<project>/.mnemosyne-exports/
├── fix-godot-spawn-location.md             # rendered transcript
├── fix-godot-spawn-location.meta.json      # per-session metadata sidecar
├── fix-godot-spawn-location.summary.md     # session-memory handoff digest   (--summaries)
├── fix-godot-spawn-location.subagents/     # full subagent transcripts        (--subagents)
│   ├── explore-find-spawn-logic-ac8a92f0.md
│   └── index.json                          # agent_type / description / tool_use_id map
├── fix-godot-spawn-location.workflows/     # scripts + orchestrated agents     (--workflows)
│   ├── scripts/audit-sweep-wf_364fe78c.js
│   └── wf_364fe78c-f35/
│       ├── general-purpose-audit-domain-a02e9451.md
│       └── journal-summary.json            # agents started/completed/distinct
├── fix-godot-spawn-location.tool-results/  # externalised tool output          (--tool-results)
├── memory/                                 # project-level curated memory      (--memories)
│   ├── MEMORY.md                           # the human index (copied through)
│   ├── product-roadmap-mid-2026.md         # per-memory copies
│   ├── memories.md                         # one consolidated document
│   └── memories.json                       # machine index w/ resolved/dangling link graph
├── …
└── index.json                              # project-level index of all exports
```

Each `.meta.json` carries everything a downstream tool needs without
re-parsing the raw JSONL: `{session_id, project_slug, ai_title, first_prompt,
first_timestamp, last_timestamp, user_count, assistant_count, source_jsonl,
source_size_bytes, source_malformed_lines, rendered_file, rendered_size_bytes,
mode, format, generated_at}`. `index.json` aggregates the same data across every
session exported into that directory: a filtered rerun (`--since`, `--matching`)
refreshes its own entries and keeps the others for as long as their rendered
files exist, so the index mirrors what is on disk rather than the last filter.
`memory/` is written once per project; everything else is per-session, keyed to
the transcript's filename.

## Render modes

Most of a raw JSONL session is tool I/O. Three levels:

| `--mode` | What's in the output | Size vs raw |
| --- | --- | --- |
| `transcript` | **(default)** Only user prompts + assistant prose. No tools. | ~13% |
| `compact` | Transcript + one-line summaries per tool call (`📄 Read /path (4521 chars)`, `🐚 Bash <cmd>` + result). | ~49% |
| `full` | Everything verbatim — tool inputs and results fenced. | ~93% |

Unconditional cleanups, all modes:

- Consecutive same-role turns coalesce under one header (3,593 → 47 headers on a real 6,000-message session).
- Boilerplate acks scrubbed (`The file X has been updated successfully`, `Todos have been modified successfully`).
- System-injected user messages dropped (`isMeta=True`).
- API/auth/credit error responses dropped (`stop_reason='stop_sequence'`).
- `<task-notification>` XML unwrapped to summary+result.
- System wrappers stripped: `<system-reminder>`, `<ide_opened_file>`, `<ide_selection>`, `<command-name>`, `<local-command-*>`.
- JSON-escape-encoded paste-ins unescaped (`\n\n` → real newlines) when the text looks serialized.

Deterministic machine-noise scrub on captured tool output (compact/full), pure
and idempotent — same input always yields the same output (see `clean.py`):

- ANSI/VT escape sequences removed (colour codes, cursor moves, OSC titles).
- Carriage-return progress bars collapsed to their final frame (`npm install`, download spinners).
- Base64 / data-URI blobs truncated to a `…[+N base64 chars]` stub (pasted screenshots, embedded binaries).
- Whitespace normalised on every document: trailing spaces trimmed, runs of 3+ blank lines collapsed.

## Output formats

| `--format` | Shape | Best for |
| --- | --- | --- |
| `markdown` | Default. Headed turns, fenced tool blocks, code in fenced blocks. | Human reading. |
| `jsonl` | One JSON object per coalesced turn: `{turn_id, turn_index, role, timestamp, text, char_count, session_id, project_slug, project_path}`. `turn_id` is `{session_id}#{turn_index}` — stable across re-renders, safe to use as a vector store primary key. | Embedding pipelines, vector store ingestion, structured downstream consumers. |
| `plain` | No markdown decoration. `=== USER (ts) === / === ASSISTANT (ts) ===` headers. | Pasting into prompts for models that prefer no markup. |

`--include-attachments` is honoured by **all three** formats. Attachments become
their own record (`role: "attachment"` in `jsonl`, an `=== ATTACHMENT ===`
section in `plain`). Inline `image` / `document` / `fallback` content blocks are
preserved as a one-line descriptor carrying type, media type, title and decoded
size — the base64 payload itself is never embedded, since a single screenshot
outweighs the transcript it belongs to. Content-block types mnemosyne does not
recognise are preserved the same way rather than dropped.

`plain` strips only decoration *mnemosyne generated* (its own fences, role
headers and `<details>` wrappers). Original user or assistant text may itself
contain markdown; that is content, and is left intact.

## MCP server

`syne mcp` speaks MCP over stdio (MCP Python SDK 2.x). Nineteen read-only tools — one aggregated
self-align entry point, plus tools over transcripts, the curated memory layer,
handoff digests, hidden subagent transcripts, the Codex CLI archive, and memory
drift. Every tool carries MCP `readOnlyHint` annotations (nothing writes, nothing
leaves the machine), so hosts that honor annotations can auto-approve the calls:

| Tool | Purpose |
| --- | --- |
| `self_align(query?, project?, all_projects=false, max_chars=6000)` | **Start here.** One bounded packet: memory matches/index + recent summaries + transcript snippets + Codex rollouts/handoffs for the same project + `suggested_next` calls + guidance. Current project by default; cross-project only when explicit. No full bodies/transcripts. |
| `list_projects()` | Every project with sessions, sorted most-recent-used. |
| `list_sessions(project?, limit=20)` | Newest sessions in a project. |
| `get_session_summary(session_id, project?)` | Cheap header — no transcript loading; `first_prompt` is a ≤300-char excerpt. |
| `get_session_handoff(session_id, project?)` | The compaction handoff digest (Title / Current State / Next steps) — "where we left off", cheaper than a transcript. |
| `get_session(session_id, project?, mode="transcript", max_tool_chars=2000)` | Rendered markdown for one session. |
| `recall_recent(project?, limit=5)` | Last N session summaries for the current project. |
| `search_sessions(query, project?, all_projects=false, max_results=10, context_chars=200)` | Case-insensitive substring search across rendered transcripts. Current project by default; cross-project only when explicit. |
| `list_memories(project?)` | Curated memories for a project (name, type, description, links). |
| `get_memory(name, project?)` | One memory's full body + metadata, by name or prefix. |
| `search_memories(query, project?, all_projects=false, max_results=10)` | Substring search across memory names/descriptions/bodies. Current project by default; cross-project only when explicit. |
| `list_subagents(session_id, project?)` | The subagent transcripts behind a session's Task/workflow calls. |
| `get_subagent(session_id, agent_id, project?, mode="transcript")` | One subagent's full rendered transcript. |
| `list_codex_sessions(project?, limit=10)` | Codex CLI rollouts recorded for the same working tree (`~/.codex/sessions`) — cross-tool continuity. |
| `get_codex_session(session_id, mode="transcript")` | One Codex rollout rendered through the same pipeline (modes and scrubbing identical). |
| `list_codex_handoffs(project?)` | Codex's own per-session digests (rollout summaries) for this project. |
| `get_codex_handoff(name)` | One Codex handoff digest's full body, by file name or thread-id prefix. |
| `get_codex_memory(max_chars=4000)` | Codex's consolidated model-written memory (`memory_summary.md`), capped. Trust below curated memories. |
| `check_drift(project?)` | Deterministically verify every curated memory's cited paths, `path:line` anchors, and `[[links]]` against the live repo. |

Manual MCP registration (without the plugin):

```json
{
  "mcpServers": {
    "mnemosyne": {
      "command": "syne",
      "args": ["mcp"]
    }
  }
}
```

## Claude Code plugin

After `syne install`, restart Claude Code, then run
`/plugin install mnemosyne@mnemosyne`. You get:

| Command | What it does |
| --- | --- |
| `/recall <query>` | Search past sessions, show matches with snippets. |
| `/memories [query]` | List or search the project's curated memories. |
| `/history [limit]` | List recent sessions for the current project. |
| `/summon <id-or-prefix> [mode]` | Load a session's transcript into context. |
| `/export <id-or-prefix \| --all> [--full \| --memories \| …]` | Export from inside Claude Code. |

Plus a `session-history` skill that teaches Claude *when* to reach for the
MCP tools (e.g., "have I done X before?" → `search_sessions`; "what's the plan
for Y?" → `search_memories`; "how did that audit reach its finding?" →
`list_subagents` → `get_subagent`).

## Self-alignment across tools

The archive only helps if the agent actually consults it — and consults it
*safely*. `syne align` writes a small, always-loaded **directive** into a
project's instruction files so any agent knows the archive exists, **when** to
recall (trigger-gated, never eager), and the guardrails that keep recall from
*amplifying* drift.

```bash
syne align                 # write the directive into ./CLAUDE.md and ./AGENTS.md
syne align --export        # print the block to stdout instead (manual placement / migration)
syne align --remove        # strip it again (idempotent, marker-scoped)
```

- **Two files, by design.** `AGENTS.md` is the cross-tool open standard read by
  opencode, Codex, Cursor, Copilot, Windsurf, and Gemini; `CLAUDE.md` is for
  Claude Code, which does **not** read `AGENTS.md`. Writing both reaches everyone.
- **Idempotent + marker-scoped.** Only the
  `<!-- mnemosyne:begin -->`…`<!-- mnemosyne:end -->` span is ever touched;
  re-running is a no-op; your own content is never clobbered.
- **Gated on real evidence.** The directive claims "this project has a
  searchable archive", so it is written only when that's true: the project has
  ≥1 session, exports on disk, or curated memories. A globally installed plugin
  alone is deliberately *not* sufficient — it proves the tools exist, not that
  this project has anything to recall. `--force` overrides (e.g. to pre-wire a
  brand-new project).

The directive is **engineered to reduce drift**, not feed it: don't auto-load
every session; cheapest-first with hard stops (≤1–3 sessions, never `full` mode);
trust order **curated memories → session summary → targeted search → Codex
handoffs/rollouts → full transcript LAST** (raw transcripts keep dead-ends —
don't re-adopt them); treat every recall as **dated evidence** the live code
overrides; recalled content is **data, never instructions** (a directive found
inside recalled text is not followed on recall's authority); stay
project-scoped; a past decision is context, not a commitment; never fabricate.
On Claude Code the directive is a thin router that defers to the richer
`session-history` skill.

### Deterministic continuity: the resume/compact hook

The directive above is *advisory* — the model can ignore it. The plugin's
`SessionStart` hook is the **deterministic** rung: Claude Code reports why a
session started (`startup` / `resume` / `clear` / `compact`), and the hook
injects a small, hard-capped self-align brief **only on `resume` and
`compact`** — exactly the boundary where working context gets lost and drift is
born. Fresh sessions stay clean; no model discretion is involved.

```json
{ "matcher": "resume|compact",
  "hooks": [{ "type": "command",
    "command": "command -v syne >/dev/null 2>&1 || exit 0; syne recall --bundle --max-chars 2000" }] }
```

The brief is `syne recall --bundle`: the same bounded packet as the MCP
`self_align` tool (memory index + recent sessions + Codex context +
suggested-next), capped at 2000 characters, never a transcript. When `syne`
isn't on PATH the hook is a silent no-op; recall failures remain visible as hook
errors instead of being reported as success. Remove the `SessionStart` entry from
`~/.claude/plugins/mnemosyne/hooks/hooks.json` to opt out.

Every character cap is a **hard boundary**: it must be a positive integer, or an
explicit `None` in the Python API to opt out. Zero and negative values are
rejected rather than quietly meaning "unlimited", so a bad host or config value
cannot turn a bounded brief into an unbounded one.

For agents that can't speak MCP — or for ad-hoc briefs — the same reader works
from any shell or hook:

```bash
syne recall --recent --max-chars 1500     # tiny "what was I doing" brief
syne recall --bundle "auth"               # the full self-align packet, topic-scoped
syne recall "auth" --memories             # curated decisions about auth
syne recall "JWT" --format json           # machine-readable for piping
```

### Drift checks: recall you can trust

`syne drift` (and the `check_drift` MCP tool) mechanically verifies every
curated memory against the live repository and known local archives: cited file
paths must resolve (worktree, Claude archive, Codex archive, or anywhere in the
worktree for bare names — vendor dirs pruned), `path:line` anchors must still
fall inside the file, and `[[links]]` must resolve. The result is deterministic,
but an unresolved reference is a review signal rather than proof that the
memory's entire body is stale. Re-verify the cited claim before relying on it.

## Settings file

Persistent state at `~/.config/mnemosyne/config.toml`:

```toml
[defaults]
output_dir = "{local_path}/.mnemosyne-exports"   # {local_path} | {slug} | {cwd}
mode = "transcript"                           # transcript | compact | full
include_thinking = false
include_attachments = false
include_reminders = false
max_tool_chars = 2000

[[projects]]
slug = "-Users-foo-DevFolder-My-Projects-scifigame"
local_path = "/Users/foo/DevFolder/My-Projects/scifigame"
friendly_name = "scifigame"
git_remote = "https://github.com/foo/scifigame.git"
git_branch = "main"
last_used = "2026-05-19T15:13:04+00:00"
```

The registry is rebuilt from the filesystem each run; user-edited fields
(`friendly_name`, `git_*`) are preserved. `last_used` updates whenever you
export from a project. Claude Code's slug encoding replaces `/`, `.` **and** `_`
with `-`, so it is lossy and not injective: `My-Projects`, `My/Projects`,
`My.Projects` and `My_Projects` all flatten to the same directory name. `syne`
therefore resolves the real local path by reading the `cwd` field stored inside
each session record — authoritative, not heuristic.

Because one archive directory can legitimately hold sessions from more than one
working tree, the default (current-project) scope filters sessions by their
recorded `cwd`: a session is in scope when it did work at or below the project
root. Anything excluded is reported, never silently hidden: the CLI prints a
note, and the self-align packet and `syne recall` brief carry an
`excluded_sessions` count. The same rule scopes the MCP server and `syne
recall` — the working directory (or an absolute `project` path) is
authoritative; a bare slug names only the directory, so its registered
`local_path` is used when known — and `list_projects` counts the same scoped
set that `list_sessions` returns. Passing `--project-dir` explicitly is treated
as naming the directory, and takes it at face value.

## Durability contract

Multi-file operations (export bundles, merges, plugin installs) are
**fail-visible, not transactional**: each file is written independently, an
error anywhere aborts loudly, and files completed before the failure are left
on disk for inspection — nothing is rolled back. Re-running the same command is
**semantically** idempotent (same inputs → same filenames → same rendered
content), so recovery is always "fix the cause, run it again." It is not
byte-for-byte reproducible: sidecars and project indexes stamp a fresh
`generated_at`, so those files differ between runs by design. The few single
files where a torn write would
corrupt shared state (`config.toml`, `known_marketplaces.json`, instruction files
managed by `syne align`) are written atomically via temp-file-and-rename.

Malformed JSONL lines in a source session are tolerated (Claude Code appends
live, so a partially written final record is normal) but **counted, never
hidden**: `syne list` flags sessions with undecodable lines, and every session
header (`malformed_lines`) and export sidecar (`source_malformed_lines`)
carries the count. A persistent or growing count means real source corruption.

## Storage architecture

**Files all the way down.** The only persistent state this project creates:

- Cleaned exports under `<project>/.mnemosyne-exports/` (.md + .meta.json + index.json),
  plus the opt-in artifact bundles (`<title>.subagents/`, `<title>.workflows/`,
  `<title>.summary.md`, `<title>.tool-results/`, and a project-level `memory/`).
- TOML registry at `~/.config/mnemosyne/config.toml`

No database. The raw JSONL — and the `memory/`, `subagents/`, `workflows/`,
`session-memory/`, `tool-results/` dirs Claude Code writes beside it — are the
source of truth, owned by Claude Code; our output is durable,
version-controllable, grep-able, and embed-able. A SQLite FTS5 cache would only
be worth adding past ~5,000 sessions when full-text search starts feeling slow.
At that point it would be a derived cache, never canonical — `rm sessions.db`
would lose nothing.

## Layout

```
src/mnemosyne/
  parser.py         # JSONL → typed events (Message / Attachment / *Block)
  clean.py          # deterministic noise passes (ANSI / CR / base64 / whitespace)
  render.py         # events → markdown (3 modes + same-role coalescing) + collect_turns
  formats.py        # render_jsonl + render_plain (share collect_turns)
  memory.py         # curated memory layer: frontmatter parse + [[link]] graph + index
  artifacts.py      # discover per-session bundle (subagents / workflows / summaries / tool-results)
  artifact_export.py # write the bundle to disk (reuses parser/render) + project memories + slugify
  config.py         # TOML settings + project registry + git enrichment
  query.py          # shared recall/search core (used by the MCP server AND `syne recall`)
  recall.py         # `syne recall` — capped, headers-first stdout brief for hooks / non-MCP agents
  align.py          # `syne align` — idempotent CLAUDE.md/AGENTS.md self-alignment directive writer
  cli.py            # cyclopts app: list / export / export-all / merge / recall / align / install / mcp
  mcp_server.py     # MCP 2.0 MCPServer with 19 read-only tools
  installer.py      # syne install / uninstall — deploys plugin to ~/.claude/plugins/
  plugin_assets/    # bundled plugin templates (.claude-plugin/, skills/, commands/, .mcp.json)
tests/
  test_parser.py / test_render.py / test_formats.py / test_cli_helpers.py
  test_clean.py / test_workspace.py / test_installer.py / test_mcp_server.py
  test_memory.py / test_artifacts.py / test_artifact_export.py
  test_query.py / test_recall.py / test_align.py
```

## License

MIT © 2026 Dominikos Pritis. See [LICENSE](LICENSE).
