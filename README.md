# mnemosyne

Mnemosyne is a Titaness in Greek mythology. She is the personification of memory and remembrance, and a fitting namesake for a project that serves as a clean, structured layer on top of the raw conversation logs produced by Claude Code. Mnemosyne takes the noisy, append-only JSONL files that Claude Code generates and transforms them into human- and agent-readable formats, while also providing tools for browsing, exporting, merging, and searching through past sessions.

> Memory + context suite for Claude Code — clean transcript exports, cross-session merge, project archive, MCP server, and a Claude Code plugin. For humans and agents.

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
| "Give me JSONL to feed into an embedding pipeline" | `syne export-all --format jsonl` |
| "Plain text I can paste into another LLM" | `syne export-all --format plain` |
| "Let Claude itself search and load my past sessions" | `syne install` (the MCP server + plugin) |

## Install (two commands)

Requires Python 3.13+ and [uv](https://github.com/astral-sh/uv).

```bash
# 1. install the package — puts `syne` and `syne mcp` on PATH globally
uv tool install git+https://github.com/entro314-labs/mnemosyne

# 2. (optional) wire the Claude Code plugin sidecar — adds slash commands + skill
syne install
```

Step 2 copies the plugin into `~/.claude/plugins/mnemosyne/` and
registers it. Then in Claude Code: `/plugin install
mnemosyne@mnemosyne`. The CLI works fine without
step 2; step 2 is only needed if you want `/recall`, `/history`, `/summon`,
`/export` slash commands inside Claude Code.

Update later: `uv tool upgrade mnemosyne && syne install`.
Uninstall: `syne uninstall && uv tool uninstall mnemosyne`.

## The three layers

| Layer | Consumer | What it does |
| --- | --- | --- |
| **CLI (`syne`)** | You, in a terminal | Browse, export, merge, search. |
| **MCP server (`syne mcp`)** | Any agent (incl. Claude Code) | 6 read-only tools that expose the same operations. |
| **Claude Code plugin** | Claude Code specifically | `session-history` skill + `/recall`, `/history`, `/summon`, `/export` slash commands — wires Claude to its own past via the MCP. |

All three share the same parser, renderer, project discovery, and noise
filtering.

## CLI

Run `syne` with no arguments for an interactive picker. Otherwise:

```bash
syne list                            # sessions for the cwd's project
syne export <id-or-prefix>           # single session → <project>/.mnemosyne-exports/
syne export-all                      # every session in the project
syne merge <id1> <id2> -o out.md     # combine specific sessions
syne merge --all-from <project>      # combine every session from a project
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

### Output layout

A `syne export-all` lands like this:

```
<project>/.mnemosyne-exports/
├── fix-godot-spawn-location.md          # rendered transcript
├── fix-godot-spawn-location.meta.json   # per-session metadata sidecar
├── …
└── index.json                           # project-level index of all exports
```

Each `.meta.json` carries everything a downstream tool needs without
re-parsing the raw JSONL: `{session_id, project_slug, ai_title, first_prompt,
first_timestamp, last_timestamp, user_count, assistant_count, source_jsonl,
source_size_bytes, rendered_file, rendered_size_bytes, mode, format,
generated_at}`. `index.json` is the same data aggregated across all sessions
in the export.

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

## Output formats

| `--format` | Shape | Best for |
| --- | --- | --- |
| `markdown` | Default. Headed turns, fenced tool blocks, code in fenced blocks. | Human reading. |
| `jsonl` | One JSON object per coalesced turn: `{turn_id, turn_index, role, timestamp, text, char_count, session_id, project_slug, project_path}`. `turn_id` is `{session_id}#{turn_index}` — stable across re-renders, safe to use as a vector store primary key. | Embedding pipelines, vector store ingestion, structured downstream consumers. |
| `plain` | No markdown decoration. `=== USER (ts) === / === ASSISTANT (ts) ===` headers. | Pasting into prompts for models that prefer no markup. |

## MCP server

`syne mcp` speaks MCP over stdio. Six read-only tools:

| Tool | Purpose |
| --- | --- |
| `list_projects()` | Every project with sessions, sorted most-recent-used. |
| `list_sessions(project?, limit=20)` | Newest sessions in a project. |
| `get_session_summary(session_id, project?)` | Cheap header — no transcript loading. |
| `get_session(session_id, project?, mode="transcript", max_tool_chars=2000)` | Rendered markdown for one session. |
| `recall_recent(project?, limit=5)` | Last N session summaries for the current project. |
| `search_sessions(query, project?, max_results=10, context_chars=200)` | Case-insensitive substring search across rendered transcripts. |

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
| `/history [limit]` | List recent sessions for the current project. |
| `/summon <id-or-prefix> [mode]` | Load a session's transcript into context. |
| `/export <id-or-prefix \| --all>` | Export from inside Claude Code. |

Plus a `session-history` skill that teaches Claude *when* to reach for the
MCP tools (e.g., "have I done X before?" → triggers `search_sessions`).

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
export from a project. Claude Code's slug encoding (`/` → `-`) is lossy
(`My-Projects` and `My/Projects` collide), so `syne` resolves the real local
path by reading the `cwd` field stored inside each session record —
authoritative, not heuristic.

## Storage architecture

**Files all the way down.** The only persistent state this project creates:

- Cleaned exports under `<project>/.mnemosyne-exports/` (.md + .meta.json + index.json)
- TOML registry at `~/.config/mnemosyne/config.toml`

No database. The raw JSONL is the source of truth, owned by Claude Code; our
output is durable, version-controllable, grep-able, and embed-able. A SQLite
FTS5 cache would only be worth adding past ~5,000 sessions when full-text
search starts feeling slow. At that point it would be a derived cache, never
canonical — `rm sessions.db` would lose nothing.

## Layout

```
src/claude_session_export/
  parser.py         # JSONL → typed events (Message / Attachment / *Block)
  render.py         # events → markdown (3 modes + same-role coalescing) + collect_turns
  formats.py        # render_jsonl + render_plain (share collect_turns)
  config.py         # TOML settings + project registry + git enrichment
  cli.py            # cyclopts app: list / export / export-all / merge / projects / install / mcp
  mcp_server.py     # FastMCP server with 6 read-only tools
  installer.py      # syne install / uninstall — deploys plugin to ~/.claude/plugins/
  plugin_assets/    # bundled plugin templates (.claude-plugin/, skills/, commands/, .mcp.json)
tests/
  test_parser.py / test_render.py / test_formats.py / test_cli_helpers.py
  test_installer.py / test_mcp_server.py
```

## License

MIT © 2026 Dominikos Pritis. See [LICENSE](LICENSE).
