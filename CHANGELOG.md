# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.4.0] — 2026-06-16

Export everything Claude Code stores, not just transcripts. Beside each session,
Claude Code keeps a curated **memory** layer, session-handoff **summaries**, and
the full transcripts of every spawned **subagent** and **workflow** agent — all
previously invisible to mnemosyne (the main transcript only retains a subagent's
final result). This release captures every artifact category, each behind its own
opt-in flag, with the same deterministic cleaning applied to the rendered ones.

### Added — artifact discovery + export
- **`memory.py`** — collect the per-project curated memory layer
  (`~/.claude/projects/<slug>/memory/*.md`): a focused frontmatter parser
  (`name`, `description`, `type`, `originSessionId`), `[[link]]`-graph extraction,
  a consolidated `memories.md`, and a `memories.json` index that splits every
  memory's links into **resolved** vs **dangling**. Memories are the densest,
  most reusable knowledge in the archive — roadmaps, architecture decisions,
  gotchas, preferences — and persist across sessions.
- **`artifacts.py`** — discover the per-session sidecar bundle stored under
  `<session-uuid>/`: subagent transcripts (`subagents/agent-*.jsonl` +
  `.meta.json` → `agentType` / `description` / `toolUseId`), workflow runs
  (`subagents/workflows/wf_*/` journals + agents), workflow scripts
  (`workflows/scripts/*.js`), session-memory digests (`session-memory/summary.md`),
  and externalised tool output (`tool-results/`). Subagent and workflow
  transcripts share the **exact** record shape of a top-level session, so they
  render through the existing parser/renderer with no new parsing.
- **`artifact_export.py`** — write the discovered bundle to a clean tree beside
  each transcript. Subagent/workflow transcripts get the **same mode + noise
  scrub** as the main transcript; memories and summaries are copied through
  whitespace-normalised; tool output runs through `scrub_tool_output`; workflow
  scripts are copied verbatim (they are source). A `journal-summary.json` rolls
  up each workflow run (agents started/completed/distinct).

### Added — CLI flags (all default off; existing behaviour unchanged)
- **`--memories`** → `<out>/memory/` (per-file copies + `MEMORY.md` +
  `memories.md` + `memories.json`).
- **`--subagents`** → `<title>.subagents/` (rendered + `index.json`).
- **`--summaries`** → `<title>.summary.md`.
- **`--workflows`** → `<title>.workflows/` (scripts + per-run agents + journal summary).
- **`--tool-results`** → `<title>.tool-results/` (scrubbed).
- **`--full`** — enable all five. Distinct from `--mode full`, which sets
  transcript verbosity.
- Wired into `syne export`, `syne export-all` (including `--all-projects`), and
  the interactive default. Each run prints a one-line artifact tally.

### Added — MCP tools
- **`list_memories`**, **`get_memory`**, **`search_memories`** — expose the curated
  memory graph to agents; the first stop for "what did we decide / what's the plan".
- **`list_subagents`**, **`get_subagent`** — reach the work hidden behind Task and
  workflow calls.

### Added — plugin
- **`/memories [query]`** slash command — list or search a project's memories.
- `session-history` skill updated to teach Claude the memory and subagent tools.
- Plugin manifest bumped to 1.4.0 (now tracks the package version).

### Changed
- `_slugify` moved from `cli.py` to `artifact_export.py` as the shared `slugify`
  — one implementation for both session and artifact filenames.
- Import package renamed to `mnemosyne` (the PyPI distribution stays `mnemosyne-cc`;
  the `syne` command is unchanged).

### Tests
- `test_memory.py`, `test_artifacts.py`, `test_artifact_export.py`, plus
  memory/subagent coverage added to `test_mcp_server.py`. **160 tests** total.

### Packaging
- GitHub Actions workflow for publishing to PyPI; unused dependencies trimmed
  from `pyproject.toml` / `uv.lock`.

## [1.3.0] — 2026-06-09

Higher-fidelity output through deterministic noise removal, plus a friction-free
default that loads the current directory's workspace without a chooser.

### Added — deterministic cleanup
- **`clean.py`** — a new module of pure, idempotent, language-agnostic
  noise-reduction passes (same input always yields the same output; running a
  pass over its own output is a no-op). Inspired by repomix's deterministic
  cleanup pipeline, adapted from code to conversation transcripts:
  - **`strip_ansi`** — removes ANSI/VT escape sequences (colour codes, cursor
    moves, OSC titles) left in tool output captured from a TTY.
  - **`collapse_carriage_returns`** — emulates a terminal and keeps only the
    final frame of `\r`-overwrite progress bars (`npm install`, download
    spinners), normalising CRLF to `\n` first so real line breaks survive.
  - **`truncate_base64`** — shrinks data-URI and standalone base64 blobs
    (pasted screenshots, embedded binaries) to a `…[+N base64 chars]` stub,
    using a digit + mixed-case heuristic to avoid false positives on long hex
    or identifiers.
  - **`normalize_whitespace`** — right-trims every line and collapses runs of
    3+ blank lines (the gaps left when wrapper tags / ack lines are stripped)
    without disturbing intentional paragraph separation.
- These compose into `scrub_tool_output` (applied to captured tool output in
  compact/full modes) and a final `normalize_whitespace` pass over every
  rendered document — markdown, and the shared `collect_turns` path feeding
  JSONL and plain. User prose also gets base64 truncation for pasted images.
- Verified on 178 real sessions: 57 with ANSI, 173 with carriage returns, 19
  with base64 → **zero residual noise** after rendering.

### Added — workspace auto-load
- **`syne` (no args) now loads the current directory's workspace directly**
  when the cwd maps to a known Claude Code project with sessions — skipping the
  project chooser and jumping straight to its sessions. Run from a
  non-workspace directory, or pass **`--pick`**, to choose from all projects.
  A synthesized entry is used when the workspace has sessions on disk but isn't
  yet in the registry.

### Tests
- `test_clean.py` (deterministic + idempotency coverage for every pass) and
  `test_workspace.py` (cwd → workspace detection), plus render-level
  integration tests that the cleanup flows through `render_markdown`.

### Packaging
- **First PyPI release, published as `mnemosyne-cc`** — the bare `mnemosyne`
  name is held by an unrelated project. The import package and the `syne`
  command are unchanged; only the `pip install` / `uv tool` name differs
  (`uv tool install mnemosyne-cc`, `uv tool upgrade mnemosyne-cc`).

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

[1.4.0]: https://github.com/entro314-labs/mnemosyne/releases/tag/v1.4.0
[1.3.0]: https://github.com/entro314-labs/mnemosyne/releases/tag/v1.3.0
[1.2.0]: https://github.com/entro314-labs/mnemosyne/releases/tag/v1.2.0
[1.1.0]: https://github.com/entro314-labs/mnemosyne/releases/tag/v1.1.0
[1.0.0]: https://github.com/entro314-labs/mnemosyne/releases/tag/v1.0.0
