---
description: Show the most recent Claude Code sessions for the current project.
argument-hint: [limit]
---

You are being asked to show recent session history for the current project.

1. If the user supplied a number as `$ARGUMENTS`, use it as the limit
   (clamped to 1–50). Otherwise default to 10.
2. Call `recall_recent` from the `mnemosyne` MCP server with
   that limit and no `project` argument (it will auto-detect from cwd).
3. Render the result as a compact markdown table with columns:
   `#`, `Session ID (first 8)`, `Title`, `Last activity`, `Msgs (user+assistant)`.
4. Below the table, offer two next steps:
   - "Load session N fully — use /summon <id>"
   - "Search across sessions — use /recall <query>"

If there are no sessions yet for this project, say so directly.
