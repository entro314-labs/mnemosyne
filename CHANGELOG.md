# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.2.0] — 2026-05-19

Memory + context suite. Adds the access patterns needed to use the cleaned
session archive as input to other tools, sessions, or pipelines — not just
read it.

### Added — operations
- **`syne merge <ids…> -o file.md`** — combine multiple sessions into one
  document. Section breaks per session, ordered chronologically.
- **`syne merge --all-from <project>`** — combine an entire project's
  sessions (the "collapse N raw sessions into a clean synthesis" workflow).
- **`syne merge --all-projects`** — combine sessions across every known
  project, with each section labelled by origin. Pair with `--since` /
  `--matching` for weekly digests or topic dumps across all your work.
- **`syne merge --last N`** — after filters, keep only the N most-recent
  sessions. The "give me onboarding context — the last 5 sessions here"
  one-liner.
- **`syne export-all --all-projects`** — write every project's sessions
  into `<output>/<project>/<title>.md` (defaults to `~/claude-archive/`).
  The "back up every Claude Code conversation I've ever had" command.

### Added — formats
- **`--format {markdown,jsonl,plain}`** on `export` / `export-all` / `merge`:
  - `markdown` (default) — headed turns, fenced tool blocks, with
    **per-turn permalink anchors** (`<a id="t-{short_id}-{idx}"></a>`)
    so external tools can link to a specific turn (`other.md#t-abc12345-3`).
  - `jsonl` — one JSON object per coalesced turn. **Embedding-ready**:
    every line carries `turn_id` (`{session_id}#{turn_index}`),
    `turn_index`, `role`, `timestamp`, `text`, `char_count`,
    `session_id`, `project_slug`, `project_path`. Stable IDs make this
    safe to use as a vector store primary key.
  - `plain` — markdown stripped, `=== USER (ts) ===` headers.
    Paste-into-prompt friendly for models that prefer no markup.

### Added — filters
- **`--since DATE` / `--until DATE`** on `export-all` and `merge` —
  inclusive bounds on `last_timestamp` (ISO 8601).
- **`--matching REGEX`** — case-sensitive regex on session title or first
  prompt; use `(?i)` for case-insensitive matching.

### Added — sidecars
- **Per-session metadata sidecar** — every exported file gets a
  `<title>.meta.json` carrying `session_id`, `project_slug`, `ai_title`,
  `first_prompt`, first/last timestamps, message counts, `source_jsonl`,
  `source_size_bytes`, `rendered_file`, `rendered_size_bytes`, `mode`,
  `format`, `generated_at`. Disable with `--no-sidecar`.
- **Per-project `index.json`** — `export-all` writes a manifest at the
  export root aggregating every session's metadata, sorted by
  `last_timestamp`. Disable with `--no-index`.
- **Merge sidecar** — `syne merge` writes `<out>.meta.json` alongside the
  merged document listing every included session with `project_slug`,
  `scope` (single-project / all-projects / ids), and the full session list.

### Changed
- Refactored `render.py` to expose `collect_turns(events, opts) → list[Turn]`
  so markdown / JSONL / plain renderers share the same parsing, filtering,
  and same-role coalescing pipeline.
- Tool-result-only user turns now attach to the previous turn's body
  rather than creating an orphan "User" turn — fixes a subtle ambiguity
  in compact-mode merging.
- `render_markdown` now accepts an optional `session_id` parameter; when
  provided, injects per-turn permalink anchors.

### Architecture decisions documented
- **Storage**: files-only, no DB. Sidecars + `index.json` are the
  archive's structured layer. SQLite FTS5 is deferred until search
  performance becomes a problem at ~5,000+ sessions; it would be a
  derived cache, never canonical.
- **Language**: stay on Python + uv. Rewrite trigger would be
  10,000+ sessions or a real CPU bottleneck. PyO3 hot-path is the
  middle path if/when it's needed — not a project-wide rewrite.

## [1.1.0] — 2026-05-19

MCP server + Claude Code plugin sidecar.

### Added
- **`syne mcp`** subcommand — runs a FastMCP server on stdio with six
  read-only tools: `list_projects`, `list_sessions`, `get_session_summary`,
  `get_session`, `recall_recent`, `search_sessions`.
- **`syne install` / `syne uninstall`** — deploys / removes the Claude Code
  plugin sidecar at `~/.claude/plugins/mnemosyne/`. Registers
  a local-directory marketplace in `~/.claude/plugins/known_marketplaces.json`.
  Idempotent — safe to re-run for updates.
- **Bundled plugin assets**:
  - `session-history` skill teaching Claude when to reach for the MCP
    tools (e.g., "have I done X before?" → `search_sessions`).
  - Slash commands: `/recall <query>`, `/history`, `/summon <id>`,
    `/export <id|--all>`.
  - `.mcp.json` referencing the global `syne mcp` binary (single source
    of truth — `uv tool upgrade` updates the MCP server transparently).
- Tests for installer (8) and MCP tools (10).

## [1.0.0] — 2026-05-19

Initial cleanup-focused release. CLI for exporting Claude Code session
JSONL files to readable markdown for humans and agents.

### Added
- **CLI** built on cyclopts 5 + rich:
  - Interactive default — picks a project, then sessions, then output dir.
  - `syne list`, `syne export`, `syne export-all`, `syne projects`, `syne config-show`.
- **Three render modes** — `transcript` (default, prose only, ~13% of raw),
  `compact` (+ one-line tool summaries, ~49%), `full` (everything verbatim, ~93%).
- **Title-based filenames** — slugified `ai_title` or first prompt, with
  collision suffixes from session ID.
- **Same-role coalescing** — consecutive assistant (or user) turns merge
  under one header. Reduces 3,593 headers to 47 on a 6,000-message session.
- **Project registry + git enrichment** at
  `~/.config/mnemosyne/config.toml`. Real local path
  resolved by reading each session's `cwd` field (slug encoding is lossy).
- **Universal noise filters** (project-agnostic; verified across 90 sessions
  / 68 projects):
  - Skip `isMeta=True` user messages (system-injected control messages).
  - Skip `stop_reason='stop_sequence'` text-only assistant errors.
  - Unwrap `<task-notification>` XML to summary + result body.
  - Strip system wrappers: `<system-reminder>`, `<ide_opened_file>`,
    `<ide_selection>`, `<command-name>` family, `<local-command-*>`.
  - Scrub boilerplate acks from tool results.
  - Heuristic unescape of JSON-escape-encoded paste-ins (`\n\n` → real
    newlines) when text looks serialized rather than typed.
- 41 tests, GitHub Actions CI (lint + format + tests on ubuntu and macos),
  MIT license, full publish metadata.

[1.2.0]: https://github.com/entro314-labs/mnemosyne/releases/tag/v1.2.0
[1.1.0]: https://github.com/entro314-labs/mnemosyne/releases/tag/v1.1.0
[1.0.0]: https://github.com/entro314-labs/mnemosyne/releases/tag/v1.0.0
