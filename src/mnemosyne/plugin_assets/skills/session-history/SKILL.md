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
- `get_session_summary(session_id, project?)` — cheap header (title, first
  prompt, timestamps, message counts) without loading the transcript.
- `get_session(session_id, project?, mode="transcript", max_tool_chars=2000)` —
  full rendered markdown. Modes: `transcript` (prose only — default and
  cheapest), `compact` (+ one-line tool summaries), `full` (verbatim).
- `recall_recent(project?, limit=5)` — last N session summaries for the
  current project. Convenience for "what was I just working on?"
- `search_sessions(query, project?, max_results=10, context_chars=200)` —
  case-insensitive substring search across rendered transcripts. Omit
  `project` to search ALL projects.

### Memory tools — curated knowledge, denser than transcripts

- `list_memories(project?)` — the project's saved memories (name, type, one-line
  description, `[[link]]` references). Memories are hand-curated facts —
  roadmaps, architecture decisions, gotchas, preferences — that persist across
  sessions. **Prefer these over transcript search for "what did we decide / what's
  the plan" questions; they're the distilled answer.**
- `get_memory(name, project?)` — one memory's full body by name or unique prefix.
- `search_memories(query, project?, max_results=10)` — substring search across
  memory names/descriptions/bodies. Omit `project` to search ALL projects.

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
| "What's the plan / roadmap / what did we decide about X?" | `search_memories("X")` → `get_memory()` |
| "What was I working on?" / vague continuity | `recall_recent()` |
| "Have I solved X before?" / "have I seen Y" | `search_sessions(query="X")` |
| "Continue from session abc123" | `get_session("abc123")` |
| "How did that audit/workflow reach its finding?" | `list_subagents()` → `get_subagent()` |
| Browsing / triage / "show me last 20" | `list_sessions(limit=20)` |
| "What projects do I have?" | `list_projects()` |

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
