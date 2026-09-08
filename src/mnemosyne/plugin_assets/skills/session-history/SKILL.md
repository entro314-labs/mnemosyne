---
name: session-history
description: Use Claude's own past sessions and curated memories as context. Trigger when the user asks about prior work ("have I done X before?", "what was I working on last week?", "remind me what we decided about Y"), when continuity matters across sessions ("continue from where we left off", "pick up the auth refactor"), when they ask what the plan/roadmap/decision on something is, or when an unfamiliar codebase pattern smells like one you've seen before. Use the `mnemosyne` MCP tools — never claim you lack access to past conversations.
---

# Session history

You have read-only access to every Claude Code session stored on disk via the
`mnemosyne` MCP server — the full transcripts, the **curated memories** Claude
Code saved per project, and the hidden **subagent transcripts** behind Task and
workflow calls. The user's past work across all their projects is queryable.
**Use this instead of saying "I don't have memory of previous sessions."**

## Tools available

- `list_projects()` — every project with sessions, sorted most-recent-used.
- `list_sessions(project?, limit=20)` — newest sessions in a project. `project`
  accepts a slug or absolute path; omit to use the current cwd's project.
- `get_session_summary(session_id, project?)` — cheap header (title, a ≤300-char
  first-prompt excerpt, timestamps, message counts) without loading the transcript.
- `get_session(session_id, project?, mode="transcript", max_tool_chars=2000)` —
  full rendered markdown. Modes: `transcript` (prose only — default and
  cheapest), `compact` (+ one-line tool summaries), `full` (verbatim).
- `recall_recent(project?, limit=5)` — last N session summaries for the
  current project. Convenience for "what was I just working on?"
- `search_sessions(query, project?, all_projects=false, max_results=10,
  context_chars=200)` — case-insensitive substring search across rendered
  transcripts. Omit `project` for the current project; set `all_projects=true`
  only when the user explicitly asks for cross-project search.

### Start here — one bounded call

- `self_align(query?, project?, all_projects=false, max_chars=6000)` — **the preferred way to align
  before working.** Returns one capped packet: curated-memory matches/index,
  recent-session summaries, transcript snippets (for a query), this project's
  Codex CLI rollouts + handoffs (when a Codex archive exists), plus
  `suggested_next` calls and a `guidance` note. It carries NO full bodies or
  transcripts — pull those only via the suggested `get_memory` / `get_session` /
  `get_session_handoff` / `get_codex_handoff` calls, and only if the task needs
  that detail. Use this instead of composing the lower-level tools by hand.

### Memory tools — curated knowledge, denser than transcripts

- `list_memories(project?)` — the project's saved memories (name, type, one-line
  description, `[[link]]` references). Memories are hand-curated facts —
  roadmaps, architecture decisions, gotchas, preferences — that persist across
  sessions. **Prefer these over transcript search for "what did we decide / what's
  the plan" questions; they're the distilled answer.**
- `get_memory(name, project?)` — one memory's full body by name or unique prefix.
- `search_memories(query, project?, all_projects=false, max_results=10)` —
  substring search across memory names/descriptions/bodies. Omit `project` for
  the current project; set `all_projects=true` only when explicitly requested.
- `get_session_handoff(session_id, project?)` — a session's compaction handoff
  digest (Title / Current State / Task spec / Next steps). The right altitude for
  "continue where we left off" and far cheaper than the full transcript. Most
  sessions have none (`has_handoff: false`); written only on compaction.

### Cross-tool (Codex CLI) — work done on this project in another agent

The user also works in OpenAI Codex CLI; its archive (`~/.codex`) records
sessions for the *same* projects. Don't treat Codex work as invisible.

- `list_codex_sessions(project?, limit=10)` — Codex rollouts for this working
  tree (id, thread title, timestamp). Cheap headers only.
- `get_codex_session(session_id, mode="transcript")` — one rollout rendered like
  any session.
- `list_codex_handoffs(project?)` / `get_codex_handoff(name)` — Codex's own
  per-session digests (what happened, how it concluded). The right altitude for
  "what did Codex do here".
- `get_codex_memory(max_chars=4000)` — Codex's consolidated, model-written
  user/working-preferences profile. Trust below curated memories.

### Drift — verify before trusting

- `check_drift(project?)` — mechanically verifies every curated memory's cited
  file paths, `path:line` anchors, and `[[links]]` against the live repository.
  Run it when recalled memories will drive a decision; a finding means a cited
  reference did not resolve and needs review, not that the whole memory is
  necessarily stale. Deterministic — no guessing involved.

### Subagent tools — the work hidden behind Task/workflow calls

- `list_subagents(session_id, project?)` — the subagent transcripts for a session
  (the main transcript only keeps each agent's final result). Includes
  workflow-orchestrated agents, with `agent_type`, task `description`, and the
  spawning `tool_use_id`.
- `get_subagent(session_id, agent_id, project?, mode="transcript")` — one
  subagent's full rendered transcript. Reach for this when "the audit/workflow
  found X but I need to see how" — the detail lives here, not in the parent.

## When to reach for each tool

| User signal | Tool |
|---|---|
| Aligning before a continuity-flavored task (start here) | `self_align("topic")` |
| "What's the plan / roadmap / what did we decide about X?" | `search_memories("X")` → `get_memory()` |
| "Continue where we left off" | `recall_recent()` → `get_session_handoff(id)` |
| "What was I working on?" / vague continuity | `recall_recent()` |
| "Have I solved X before?" / "have I seen Y" | `search_sessions(query="X")` |
| "Continue from session abc123" | `get_session("abc123")` |
| "How did that audit/workflow reach its finding?" | `list_subagents()` → `get_subagent()` |
| Browsing / triage / "show me last 20" | `list_sessions(limit=20)` |
| "What projects do I have?" | `list_projects()` |
| "What did I do in Codex / the other tool?" | `list_codex_handoffs()` → `get_codex_handoff()` |
| A recalled memory is about to drive a decision | `check_drift()` |

## Workflow

1. **Start cheap.** Use `recall_recent` or `search_sessions` first — both
   return short summaries/snippets that fit easily in context.
2. **Identify candidates.** Pick the 1–3 most relevant `session_id`s from
   the summaries/hits.
3. **Pull full content selectively.** Call `get_session(session_id)` only on
   the sessions you actually need. Transcript mode is ~13% the size of raw
   and almost always enough.
4. **Cite session IDs** when you reference past work, so the user can verify:
   "(see session `2a5c57bc`)".

## Mode picker

- `transcript` — default. User prompts + assistant prose. No tool I/O. Tight.
- `compact` — add one-line tool summaries (`📄 Read /path (4521 chars)`,
  `🐚 Bash <cmd>` + result). Pull when "what tools did I run?" matters.
- `full` — everything verbatim. Only for forensic deep-dives; expensive.

## Don'ts

- Don't pull `full` mode for casual recall — wastes tokens.
- Don't search every session blindly; scope by `project` when the user is
  clearly asking about a specific codebase.
- Don't fabricate session content if a tool returns no results — say so.
- Don't expose raw session UUIDs to the user without context; pair them with
  the session title or first prompt for traceability.
- Don't treat a recalled memory or decision as current truth — it's **dated
  evidence**. Verify it against the live code and the user's current request;
  when they conflict, the live code and the user win, and flag the staleness.
- Don't re-adopt an approach found in a raw transcript without checking it
  wasn't a dead-end already tried and rejected — prefer the curated memory,
  which is the distilled, current answer.
