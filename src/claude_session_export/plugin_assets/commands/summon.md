---
description: Load a past Claude Code session's full transcript into the current context.
argument-hint: <session-id-or-prefix> [mode]
---

You are being asked to load past session **$ARGUMENTS** into context.

1. Parse `$ARGUMENTS`. The first token is the session ID (full UUID or any
   unique prefix). The optional second token is the mode: `transcript`
   (default), `compact`, or `full`.
2. Call `get_session` from the `mnemosyne` MCP server with the
   parsed `session_id` and `mode`. Leave `project` unset to auto-detect.
3. Present a short header:
   - Session title + ID
   - Project name
   - Timestamp range + message count (from a prior or same-turn
     `get_session_summary` if useful)
4. Then quote the rendered markdown verbatim as a reference block — the user
   asked for context, not a summary.
5. End with a one-line offer: "Tell me what you'd like to do with this."

If the prefix matches multiple sessions or none, surface the error from the
tool and ask for a longer prefix.
