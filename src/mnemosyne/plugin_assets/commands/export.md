---
description: Export one or all Claude Code sessions for the current project to markdown files on disk.
argument-hint: [session-id-or-prefix | --all] [--mode transcript|compact|full] [--full | --memories | --subagents | ...]
---

You are being asked to export session(s) for the current project.

1. Parse `$ARGUMENTS`:
   - If it contains `--all`, plan to export every session in the project.
   - Otherwise the first non-flag token is a session ID / prefix.
   - The `--mode` flag (if present) overrides the saved default; valid values
     are `transcript`, `compact`, `full`.
   - Artifact flags (all off by default) also export what Claude Code stored
     *beside* the transcript: `--memories` (the project's curated memory layer),
     `--subagents` (spawned subagent transcripts), `--summaries` (session-memory
     handoff digests), `--workflows` (workflow scripts + orchestrated agents),
     `--tool-results` (externalised tool output). `--full` turns on all five —
     this is the artifact switch and is distinct from `--mode full`.
2. Run the export via Bash, NOT via the MCP server (the CLI handles
   filesystem side-effects):
   - Single session: `syne export <id> [--mode <mode>] [--full | --memories …]`
   - All sessions: `syne export-all [--mode <mode>] [--full | --memories …]`
3. Both commands write to `<project>/.mnemosyne-exports/` by default. Artifacts
   land in sibling `<title>.subagents/`, `<title>.workflows/`,
   `<title>.summary.md`, `<title>.tool-results/`, and a project-level `memory/`
   directory. Report the output path and the artifact tally back to the user.
4. If you exported to a git-tracked project, suggest adding
   `.mnemosyne-exports/` to `.gitignore` (unless it's already there).
