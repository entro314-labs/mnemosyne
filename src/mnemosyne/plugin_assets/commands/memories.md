---
description: List or search the curated memories Claude Code has saved for a project, and load one fully.
argument-hint: [query] [project-slug]
---

You are being asked to consult the project's **curated memory layer** — durable,
hand-written facts (roadmaps, architecture decisions, gotchas, preferences) that
persist across sessions and are denser and more reliable than transcripts.

Parse **$ARGUMENTS**:

- **No query** → call `list_memories` from the `mnemosyne` MCP server (pass the
  second argument as `project` if given; otherwise omit to use the current
  project). Report each memory as a short list: name, type
  (`user`/`feedback`/`project`/`reference`), and its one-line description.
- **A query** → call `search_memories` with it (omit `project` to search ALL
  projects, or pass the second argument to scope to one). Report the matching
  memory names with their descriptions and project.

Then offer to load any specific memory in full via `get_memory(name)` — don't
dump full bodies automatically. When you cite a memory, use its name so the user
can verify (e.g. "per `product-roadmap-mid-2026`").

If there are zero memories or matches, say so plainly. Don't invent memories.
