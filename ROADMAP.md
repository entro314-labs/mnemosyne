# Mnemosyne Roadmap — v1.8 → v2.0

Mnemosyne's premise stays fixed: a **deterministic, files-all-the-way-down
memory/context harness** for agentic coding tools. No embeddings service, no
live-tailing dashboard, no orchestration. Everything below either (a) widens
the set of tools whose archives we can read, (b) deepens what we deterministically
produce from those archives, or (c) gets the project in front of more users.

---

## 0. Architectural foundation: the SourceAdapter protocol

### Why

Today Claude Code (`parser.py`) and Codex (`codex.py`) are two special cases
that converge by convention: both emit the shared event model
(`Message` / `Attachment` / `TextBlock` / `ThinkingBlock` / `ToolUseBlock` /
`ToolResultBlock` from `parser.py`), so `render.py`, `formats.py`, `clean.py`,
`query.py`, `drift.py`, and the MCP tools work unchanged on either. That
convergence is the product. Make it explicit before adding a third source.

### Design

New module `src/mnemosyne/sources.py`:

```python
class SourceAdapter(Protocol):
    name: str                       # "claude" | "codex" | "gemini" | ...
    display_name: str               # "Claude Code", "Codex CLI", ...

    def available(self) -> bool:
        """True when this tool's archive exists on the machine (~/.claude,
        ~/.codex, ~/.gemini, ...). Cheap check, no parsing."""

    def iter_session_files(self, project: ProjectRef | None) -> Iterator[Path]:
        """All session files, or those for one project. Project association
        comes from each session's recorded cwd (authoritative), never from
        directory-name heuristics — the slug-collision lesson from Claude Code."""

    def read_session(self, path: Path) -> list[Event]:
        """Parse into the shared event model. Malformed lines tolerated,
        counted, never hidden (existing contract)."""

    def summarize(self, path: Path) -> SessionSummary:
        """Cheap header — no full parse. Must carry the existing fields
        (session_id, ai_title, first_prompt, timestamps, counts,
        malformed_lines) plus `source: str` = self.name."""
```

Two refactors fall out of this:

1. **`SessionSummary` gains a `source` field** (`"claude"` default). Every
   downstream surface (CLI list, MCP headers, export sidecars, `index.json`)
   records which tool produced the session. Sidecar/`index.json` schema bump
   is additive — old exports remain readable.
2. **Capability flags**, not adapter subclasses, for the extras. Only Claude
   Code has subagent transcripts, workflow bundles, curated `memory/` dirs,
   and session-memory handoffs; Codex has rollout summaries and
   `memory_summary.md`; Gemini has checkpoints. Model as:

```python
class Capabilities(Flag):
    NONE = 0
    SUBAGENTS = auto()      # per-session hidden agent transcripts
    WORKFLOWS = auto()      # orchestration bundles
    CURATED_MEMORY = auto() # project-level memory/ dir with [[links]]
    HANDOFFS = auto()       # per-session compaction digests
    TOOL_RESULTS = auto()   # externalised large tool outputs
    CHECKPOINTS = auto()    # restorable session snapshots (Gemini)
```

`artifacts.py` / `artifact_export.py` already gate per-category exports;
they read the flag set instead of assuming Claude. A source that lacks a
capability returns empty, and the CLI/MCP say "not recorded by this tool"
rather than failing.

### What does NOT change

- The event model in `parser.py` stays the single canonical shape.
- `clean.py`, `render.py`, `formats.py` take zero adapter awareness.
- Files-all-the-way-down: adapters only read vendor-owned archives; we never
  write into `~/.claude`, `~/.codex`, `~/.gemini`, etc.
- Fail-visible durability contract and atomic shared-state writes, unchanged.

### Adapter conformance test

One parametrized test suite (`tests/test_adapter_contract.py`) run against
every registered adapter with a fixture archive per tool: discovery finds
sessions, `read_session` round-trips through `render` in all three modes,
`summarize` agrees with `read_session` on counts and timestamps, malformed
fixtures are counted not hidden. Adding adapter N+1 = one fixture dir + one
registration line.

---

## 1. v1.8 — "More tools, safer exports"

Theme: adapter protocol + Gemini CLI + secrets redaction.

### 1.1 SourceAdapter refactor (prerequisite)

- [ ] Extract `sources.py` protocol + registry (`get_adapter(name)`,
      `available_adapters()`).
- [ ] Wrap existing Claude parsing (`parser.py`) as `ClaudeAdapter`; wrap
      `codex.py` as `CodexAdapter`. No behavior change — the full existing
      test suite (21 files) must pass unmodified.
- [ ] `SessionSummary.source` plumbed through CLI list output, MCP session
      headers, `.meta.json` sidecars, and `index.json`.
- [ ] `tests/test_adapter_contract.py` running against both adapters.
- Acceptance: `pytest` green, `mypy` green, zero user-visible change except
  the `source` field appearing in sidecars/headers.

### 1.2 Gemini CLI adapter

- [ ] **Spike first (timeboxed):** pin down the on-disk formats —
      saved chats and checkpoints under `~/.gemini` (exact paths and JSON
      schema to be verified against a live install during the spike; the
      format has moved between versions). Record findings in
      `docs/adapters/gemini.md` with the Gemini CLI version inspected.
- [ ] `GeminiAdapter`: parse chat sessions into the shared event model
      (user/model messages, tool calls/results where recorded); project
      association via recorded workspace cwd.
- [ ] `CAPABILITIES = CHECKPOINTS` — surface checkpoint metadata
      (tag name, created-at) in `syne list` and the session header.
- [ ] CLI: sessions from all available tools appear in `syne list` with a
      `SOURCE` column; `syne export <id>` resolves across adapters with
      `gemini:<prefix>` disambiguation when prefixes collide.
- [ ] MCP: `list_sessions` / `get_session` / `search_sessions` gain an
      optional `source` filter; `self_align` folds Gemini sessions into the
      same "recent work" packet.
- [ ] Fixture-based contract tests + parser unit tests, including a
      malformed-lines fixture.
- Acceptance: on a machine with real Gemini CLI usage, `syne list` shows
  Gemini sessions and `syne export` renders them in all three modes with
  the standard scrub applied.

### 1.3 Secrets redaction pass

Motivation: exports get *shared* — team handoffs, issue reproductions,
committed archive repos. Today nothing stops a pasted API key from landing
in `.mnemosyne-exports/`. (This repo itself had a credential-shaped PyPI token sitting in
a working-tree file in July 2026; redaction is not hypothetical.)

- [ ] New deterministic pass in `clean.py` (pure, idempotent, same
      input → same output — the existing contract), e.g. `scrub_secrets()`:
      pattern table for high-confidence credential shapes (PyPI `pypi-…`,
      GitHub `ghp_…`/`github_pat_…`/`gho_…`, OpenAI `sk-…`, Anthropic
      `sk-ant-…`, AWS `AKIA…`, GCP service-account JSON blocks, generic
      `Bearer <token>`, `-----BEGIN … PRIVATE KEY-----` blocks, common
      `KEY=value` env dumps). Each hit → `[REDACTED:<kind>]` stub.
- [ ] Applied by default on export/render of tool output and message text;
      `--no-redact` escape hatch for private local archives.
- [ ] Every export sidecar gains `redactions: <count>` — counted, never
      hidden, same philosophy as `malformed_lines`.
- [ ] Tests: per-pattern fixtures, idempotence proof
      (`scrub(scrub(x)) == scrub(x)`), and a "no false positives on normal
      code" corpus (the pattern table must be conservative — a false
      positive destroys recalled evidence).
- Acceptance: exporting a session containing a real-format key yields the
  stub, the sidecar count, and zero diff on a clean-session corpus.

### 1.4 Repo hygiene (immediate, not versioned)

- [ ] Rotate the PyPI token exposed in the stray brainstorm file; delete or
      sanitize that file; move release credentials to CI secrets.
- [ ] Add a `gitleaks`-style secret scan step to `.github/workflows/ci.yml`
      so this class of mistake fails CI instead of being found by reading.

**v1.8 exit criteria:** three adapters behind one protocol (Claude, Codex,
Gemini), redaction on by default, full test suite + contract suite green.

---

## 2. v1.9 — "Power users"

Theme: scale and habit — make mnemosyne effortless to run daily over years
of accumulated sessions.

### 2.1 SQLite FTS5 index (derived cache)

The README already names the ~5,000-session threshold. Build it now, strictly
derived:

- [ ] `syne index build|rebuild|drop` → `~/.config/mnemosyne/index.db`
      (FTS5 over rendered turn text, keyed by the existing stable
      `{session_id}#{turn_index}` turn_id). Incremental by source-file mtime
      + size fingerprint.
- [ ] `search_sessions` / `syne recall` use the index **when present and
      fresh**, falling back to the current substring scan otherwise —
      identical result shape either way, so nothing downstream branches.
- [ ] `rm index.db` loses nothing (documented, tested: drop → rebuild →
      same top results on a fixture corpus).
- [ ] Ranked results replace substring ordering when the index serves the
      query; `context_chars` snippet behavior unchanged.

### 2.2 Incremental export

- [ ] `syne export-all --incremental`: skip sessions whose rendered output +
      sidecar already exist and whose source mtime is unchanged (sidecar
      already records `source_size_bytes`; add `source_mtime`). Idempotence
      makes this safe by construction.
- [ ] Makes `export-all --full` viable as a nightly launchd/systemd/cron
      job; document that setup in the README with a sample plist/unit.

### 2.3 `syne stats`

- [ ] File-derived analytics from sidecars/`index.json` where present, raw
      archives otherwise: sessions/week, turn counts, per-project activity,
      malformed-line trends, source breakdown (which tools you actually
      use), token/cost estimates where the source format records usage.
- [ ] `--format json` for piping; markdown table default.

**v1.9 exit criteria:** 10k-session corpus searchable in <200ms via index;
nightly incremental export documented; `syne stats` answers "what did I do
last month across all tools" in one command.

---

## 3. v2.0 — "Distribution"

Theme: the product is good; discovery is the bottleneck. Feature work here
is only what distribution requires.

- [ ] **MCP registry submission** — 19 read-only annotated tools is exactly
      what registry review wants. Publish server metadata, keep `syne mcp`
      stdio contract stable.
- [ ] **Claude Code plugin marketplace + awesome-* lists**
      (awesome-claude-code, awesome-gemini-cli, awesome-mcp).
- [ ] **Homebrew tap** (`brew install mnemosyne`) — many CLI users never
      touch uv/pipx.
- [ ] **Demo GIF** in the README: `export-all` → `recall --bundle` →
      `drift` in 60 seconds. The README is thorough but has no visual proof.
- [ ] **Windows support decision:** audit path handling (already
      `pathlib`-clean, ruff `PTH` rules enforced), either ship Windows or
      document the exclusion explicitly in classifiers/README.
- [ ] **opencode adapter** (storage under `~/.local/share/opencode`;
      ~172k-star user base, model-agnostic audience that matches our
      cross-tool pitch). Format spike same as Gemini's.
- [ ] **Aider adapter** (`.aider.chat.history.md` — trivial parser, cheap
      goodwill win).
- [ ] **Extended `syne align` targets:** `GEMINI.md`, `.cursor/rules/`,
      `.github/copilot-instructions.md`, `opencode.json` — the existing
      marker-scoped idempotent writer generalized to a target table.
- [ ] **PreCompact hook:** snapshot the handoff brief *before* compaction,
      closing the last gap in the resume/compact continuity chain.

**v2.0 exit criteria:** installable via brew + uv; listed in the MCP
registry and at least two awesome lists; five tools covered (Claude, Codex,
Gemini, opencode, Aider); README demo GIF live.

---

## 4. Explicit non-goals (re-confirmed)

- **No embedding/vector memory service.** Contradicts the deterministic
  premise; the FTS index is the search ceiling.
- **No live TUI/observability dashboard.** agentwatch/agenttrace own that
  niche; mnemosyne is the canonical layer, not the tail.
- **No agent orchestration.** Memory/context harness, not swarm runner.
- **No writes into vendor archives.** Ever. Adapters are read-only by
  contract (the MCP surface keeps `readOnlyHint` everywhere).

## 5. Risks and open questions

| Risk | Mitigation |
|---|---|
| Vendor formats drift (Gemini checkpoints already moved once) | Format-version detection per adapter + loud "unsupported format version" errors; fixture archives pinned per version in `tests/fixtures/`; contract suite fails CI on drift. |
| Redaction false positives destroying evidence | Conservative pattern table, `--no-redact`, counts in sidecars, clean-corpus regression test. |
| Cursor adapter (deferred) — opaque SQLite, unstable schema | Deliberately out of v1.8–v2.0; revisit only if a stable export path appears. |
| Index staleness bugs silently serving old results | Freshness check (fingerprint compare) on every index read; fall back to scan on mismatch; never serve stale silently. |

## 6. Suggested execution order (next 4 focused work sessions)

1. **1.4 repo hygiene** (30 min, do today — the token is live).
2. **1.3 redaction pass** — smallest self-contained feature, patches the
   hole the repo just demonstrated, no architectural dependencies.
3. **1.1 adapter refactor** — pure refactor, protected by the existing
   21-file test suite.
4. **1.2 Gemini spike → adapter** — the first proof the protocol holds for
   a tool with no special-casing.
