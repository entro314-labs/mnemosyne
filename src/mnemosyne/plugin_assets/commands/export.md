---
description: Export one or all Claude Code sessions for the current project to markdown files on disk.
argument-hint: [session-id-or-prefix | --all] [--mode transcript|compact|full]
---

You are being asked to export session(s) for the current project.

1. Parse `$ARGUMENTS`:
   - If it contains `--all`, plan to export every session in the project.
   - Otherwise the first non-flag token is a session ID / prefix.
   - The `--mode` flag (if present) overrides the saved default; valid values
     are `transcript`, `compact`, `full`.
2. Run the export via Bash, NOT via the MCP server (the CLI handles
   filesystem side-effects):
   - Single session: `syne export <id> [--mode <mode>]`
   - All sessions: `syne export-all [--mode <mode>]`
3. Both commands write to `<project>/.claude-exports/` by default. Report
   the output path back to the user along with the filenames written.
4. If you exported to a git-tracked project, suggest adding
   `.claude-exports/` to `.gitignore` (unless it's already there).
