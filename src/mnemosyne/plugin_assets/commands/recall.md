---
description: Search past Claude Code sessions for a topic and load matching snippets into context.
argument-hint: <query> [project-slug]
---

You are being asked to recall prior work matching: **$ARGUMENTS**

1. Call `search_sessions` from the `mnemosyne` MCP server with
   the query above. If the user supplied a second argument, pass it as the
   `project` parameter; otherwise leave `project` unset to search ALL
   projects.
2. Report the matches as a short list, each entry showing: the session
   title (with `session_id` prefix), the project name, the timestamp, and a
   trimmed snippet from the match.
3. Ask the user if they want any of the matching sessions loaded fully via
   `get_session` — don't load them automatically.

If there are zero matches, say so plainly. Don't speculate.
