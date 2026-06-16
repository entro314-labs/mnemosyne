"""MCP server exposing session-history tools to agents.

Run as ``syne mcp`` (stdio transport). Same parsing / rendering / project
discovery primitives as the CLI — this module is a thin wrapper.

Tools:

- ``list_projects`` — every project with at least one session
- ``list_sessions`` — sessions in a project (slug or local path)
- ``get_session_summary`` — cheap header for a session (no full transcript)
- ``get_session`` — rendered markdown for a session in chosen mode
- ``recall_recent`` — last N session summaries for the current project
- ``search_sessions`` — substring search across rendered transcripts
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from mnemosyne.artifacts import SubagentRef, discover_session_artifacts
from mnemosyne.config import (
    CLAUDE_PROJECTS,
    ProjectEntry,
    load_settings,
    sync_registry,
)
from mnemosyne.parser import (
    list_session_files,
    project_dir_for_cwd,
    read_session,
    summarize_session,
)
from mnemosyne.query import (
    all_project_dirs,
    fit_packet,
    memory_detail,
    memory_entries,
    recent_sessions,
    session_handoff,
    session_summary_dict,
)
from mnemosyne.query import search_memories as _query_memories
from mnemosyne.query import search_sessions as _query_sessions
from mnemosyne.query import self_align as _query_self_align
from mnemosyne.render import Mode, RenderOptions, render_markdown

mcp = FastMCP("mnemosyne")


# ---- helpers ----


def _resolve_project(project: str | None) -> Path:
    """Accept a slug, an absolute path, or None (use cwd)."""
    if project is None:
        candidate = project_dir_for_cwd(Path.cwd())
        if not candidate.is_dir():
            raise FileNotFoundError(
                f"No Claude Code project found for cwd ({Path.cwd()}). "
                f"Pass `project` as a slug or absolute path."
            )
        return candidate
    p = Path(project)
    if p.is_absolute() and p.is_dir():
        # User passed an absolute local path; map to the Claude slug dir.
        candidate = project_dir_for_cwd(p)
        if candidate.is_dir():
            return candidate
        raise FileNotFoundError(f"No Claude project recorded for {p}")
    # Treat as slug.
    candidate = CLAUDE_PROJECTS / project
    if not candidate.is_dir():
        raise FileNotFoundError(f"Unknown project slug: {project}")
    return candidate


def _resolve_session_path(project_dir: Path, session_id: str) -> Path:
    matches = [p for p in list_session_files(project_dir) if p.stem.startswith(session_id)]
    if not matches:
        raise FileNotFoundError(f"No session matching {session_id!r} in {project_dir.name}")
    if len(matches) > 1:
        raise ValueError(
            f"Prefix {session_id!r} matches {len(matches)} sessions; pass a longer prefix."
        )
    return matches[0]


def _project_dict(entry: ProjectEntry, n_sessions: int) -> dict[str, Any]:
    return {
        "slug": entry.slug,
        "name": entry.friendly_name,
        "local_path": entry.local_path,
        "git_remote": entry.git_remote,
        "git_branch": entry.git_branch,
        "last_used": entry.last_used,
        "session_count": n_sessions,
    }


# ---- tools ----


@mcp.tool()
def list_projects() -> list[dict[str, Any]]:
    """List every Claude Code project that has at least one session on disk.

    Returns slug, friendly name, local path, git remote/branch, last-used
    timestamp, and session count. Sorted by last_used desc.
    """
    settings = load_settings()
    sync_registry(settings)
    rows: list[tuple[ProjectEntry, int]] = []
    for entry in settings.projects.values():
        slug_dir = CLAUDE_PROJECTS / entry.slug
        if not slug_dir.is_dir():
            continue
        n = len(list(slug_dir.glob("*.jsonl")))
        if n > 0:
            rows.append((entry, n))
    rows.sort(key=lambda r: (r[0].last_used or "", r[1]), reverse=True)
    return [_project_dict(e, n) for e, n in rows]


@mcp.tool()
def list_sessions(
    project: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """List sessions in a project, newest first.

    Args:
        project: project slug (e.g. ``-Users-foo-bar``) or absolute local path.
            Omit to use the current working directory's project.
        limit: cap on number of sessions returned (default 20).
    """
    project_dir = _resolve_project(project)
    return recent_sessions(project_dir, limit, load_settings())


@mcp.tool()
def get_session_summary(
    session_id: str,
    project: str | None = None,
) -> dict[str, Any]:
    """Return one session's header without loading the full transcript.

    Args:
        session_id: full UUID or any unique prefix (e.g. ``2a5c57bc``).
        project: same semantics as ``list_sessions``.
    """
    project_dir = _resolve_project(project)
    path = _resolve_session_path(project_dir, session_id)
    entry = load_settings().projects.get(project_dir.name)
    return session_summary_dict(summarize_session(path), entry)


@mcp.tool()
def get_session(
    session_id: str,
    project: str | None = None,
    mode: Mode = "transcript",
    max_tool_chars: int = 2000,
) -> str:
    """Return one session's full rendered markdown.

    Args:
        session_id: full UUID or unique prefix.
        project: same semantics as ``list_sessions``.
        mode: ``transcript`` (default, prose only), ``compact`` (+ tool one-liners),
            or ``full`` (everything verbatim).
        max_tool_chars: per-block truncation for tool inputs/outputs in compact/full modes.
    """
    project_dir = _resolve_project(project)
    path = _resolve_session_path(project_dir, session_id)
    summary = summarize_session(path)
    events = read_session(path)
    label = summary.ai_title or summary.first_user_text or summary.session_id
    title = f"{label}  \n_session {summary.session_id}_"
    opts = RenderOptions(
        mode=mode,
        max_tool_result_chars=max_tool_chars,
        max_tool_input_chars=max_tool_chars,
    )
    return render_markdown(events, title=title, opts=opts)


@mcp.tool()
def recall_recent(
    project: str | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Return the most recently active sessions for a project — cheap headers only.

    Convenience wrapper for the common 'what was I just working on?' case. Same
    output shape as ``list_sessions`` but defaults to limit=5.

    Args:
        project: slug or absolute path; omit for cwd's project.
        limit: how many sessions to return (default 5).
    """
    return list_sessions(project=project, limit=limit)


@mcp.tool()
def search_sessions(
    query: str,
    project: str | None = None,
    max_results: int = 10,
    context_chars: int = 200,
) -> list[dict[str, Any]]:
    """Substring search across rendered transcripts (case-insensitive).

    Scans the transcript-mode rendering of each session (the cheapest form
    that still contains prose). Returns matches with a short snippet of
    surrounding context. Limited to one match per session.

    Args:
        query: case-insensitive substring to look for.
        project: slug or absolute path; omit to search ALL projects.
        max_results: cap on total matches (default 10).
        context_chars: characters of context to include on either side of the hit.
    """
    dirs = [_resolve_project(project)] if project is not None else all_project_dirs(CLAUDE_PROJECTS)
    return _query_sessions(dirs, query, load_settings(), max_results, context_chars)


def _subagent_dict(ref: SubagentRef) -> dict[str, Any]:
    return {
        "agent_id": ref.agent_id,
        "agent_type": ref.agent_type,
        "description": ref.description,
        "tool_use_id": ref.tool_use_id,
        "workflow_id": ref.workflow_id,
    }


@mcp.tool()
def list_memories(project: str | None = None) -> list[dict[str, Any]]:
    """List the curated memories Claude Code has saved for a project.

    Memories are durable, hand-curated facts — roadmaps, architecture decisions,
    gotchas, user preferences — that persist across sessions and are denser and
    more reliable than transcripts. Reach for these first when the user asks
    "what did we decide about X?" or "what's the plan for Y?". Returns each
    memory's name, type (user/feedback/project/reference), description, origin
    session, and ``[[link]]`` references. Empty when the project has no memories.

    Args:
        project: slug or absolute path; omit to use the current cwd's project.
    """
    return memory_entries(_resolve_project(project))


@mcp.tool()
def get_memory(name: str, project: str | None = None) -> dict[str, Any]:
    """Return one memory's full body and metadata by name (or unique prefix).

    Args:
        name: memory name (e.g. ``product-roadmap-mid-2026``) or a unique prefix.
        project: slug or absolute path; omit for the cwd's project.
    """
    return memory_detail(_resolve_project(project), name)


@mcp.tool()
def search_memories(
    query: str,
    project: str | None = None,
    max_results: int = 10,
) -> list[dict[str, Any]]:
    """Case-insensitive substring search across memory names, descriptions, and bodies.

    Args:
        query: case-insensitive substring to look for.
        project: slug or absolute path; omit to search ALL projects.
        max_results: cap on total matches (default 10).
    """
    dirs = [_resolve_project(project)] if project is not None else all_project_dirs(CLAUDE_PROJECTS)
    return _query_memories(dirs, query, max_results)


@mcp.tool()
def list_subagents(session_id: str, project: str | None = None) -> list[dict[str, Any]]:
    """List the subagent transcripts stored for a session.

    The main transcript only keeps a subagent's final result; the full back-and-forth
    of every spawned agent (including workflow-orchestrated ones) lives in a sibling
    directory. Use this to see what work happened inside a Task/workflow, then pull
    one with ``get_subagent``. Returns each agent's id, type, task description,
    spawning ``tool_use_id``, and workflow id (when run inside a workflow).

    Args:
        session_id: full UUID or unique prefix of the parent session.
        project: slug or absolute path; omit for the cwd's project.
    """
    project_dir = _resolve_project(project)
    path = _resolve_session_path(project_dir, session_id)
    arts = discover_session_artifacts(project_dir, path.stem)
    out = [_subagent_dict(ref) for ref in arts.subagents]
    for run in arts.workflow_runs:
        out.extend(_subagent_dict(ref) for ref in run.agents)
    return out


@mcp.tool()
def get_subagent(
    session_id: str,
    agent_id: str,
    project: str | None = None,
    mode: Mode = "transcript",
    max_tool_chars: int = 2000,
) -> str:
    """Return one subagent's full rendered transcript (markdown).

    Args:
        session_id: full UUID or unique prefix of the parent session.
        agent_id: the subagent's id (or unique prefix) from ``list_subagents``.
        project: slug or absolute path; omit for the cwd's project.
        mode: ``transcript`` (default), ``compact``, or ``full`` — same semantics
            as ``get_session``.
        max_tool_chars: per-block truncation for tool I/O in compact/full modes.
    """
    project_dir = _resolve_project(project)
    path = _resolve_session_path(project_dir, session_id)
    arts = discover_session_artifacts(project_dir, path.stem)
    refs = [*arts.subagents, *(a for run in arts.workflow_runs for a in run.agents)]
    exact = [r for r in refs if r.agent_id == agent_id]
    matches = exact or [r for r in refs if r.agent_id.startswith(agent_id)]
    if not matches:
        raise FileNotFoundError(f"No subagent {agent_id!r} for session {path.stem}")
    if len(matches) > 1:
        raise ValueError(f"{agent_id!r} matches {len(matches)} subagents; pass a longer prefix.")
    ref = matches[0]
    events = read_session(ref.path)
    desc = f" — {ref.description}" if ref.description else ""
    title = f"Subagent: {ref.agent_type or 'subagent'}{desc}  \n_agent {ref.agent_id}_"
    opts = RenderOptions(
        mode=mode,
        max_tool_result_chars=max_tool_chars,
        max_tool_input_chars=max_tool_chars,
    )
    return render_markdown(events, title=title, opts=opts, session_id=ref.agent_id)


@mcp.tool()
def self_align(
    query: str | None = None,
    project: str | None = None,
    max_chars: int = 6000,
) -> dict[str, Any]:
    """One bounded retrieval packet to self-align before working — the preferred start.

    Returns the cheap layers in a single call: curated-memory matches/index, recent
    session summaries, transcript snippets (when ``query`` is given), plus
    ``suggested_next`` calls for the expensive follow-ups and a ``guidance`` note.
    Carries NO full memory bodies or transcripts — pull those on demand via the
    suggested ``get_memory`` / ``get_session`` / ``get_session_handoff`` calls only if
    the task needs that detail.

    Recalled content is **dated evidence**, not authority: the live code and the
    user's current request win. Cite memory names / session IDs.

    Args:
        query: topic to align on (e.g. "auth", "billing"). Omit for a general
            "what is this project / what was I doing" brief.
        project: slug or absolute path; omit for the cwd's project (use ALL projects
            only when the user explicitly asks).
        max_chars: soft cap; the packet is trimmed (hits → recent → memories) to fit.
    """
    dirs = [_resolve_project(project)] if project is not None else all_project_dirs(CLAUDE_PROJECTS)
    packet = _query_self_align(dirs, load_settings(), query=query)
    return fit_packet(packet, max_chars)


@mcp.tool()
def get_session_handoff(session_id: str, project: str | None = None) -> dict[str, Any]:
    """Return a session's handoff digest (the compaction "where we left off" summary).

    Claude Code writes these on compaction — a curated Title / Current State / Task
    spec / Next steps digest, the right altitude for "continue where we left off" and
    far cheaper than the full transcript. ``has_handoff`` is False when the session
    has no digest (most don't).

    Args:
        session_id: full UUID or unique prefix.
        project: slug or absolute path; omit for the cwd's project.
    """
    project_dir = _resolve_project(project)
    path = _resolve_session_path(project_dir, session_id)
    return session_handoff(project_dir, path.stem)


def run() -> None:
    """Entry point for ``syne mcp`` — runs the FastMCP server on stdio."""
    mcp.run()


if __name__ == "__main__":
    run()
