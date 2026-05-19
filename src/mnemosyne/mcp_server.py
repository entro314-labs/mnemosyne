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


def _summary_dict(path: Path, project_entry: ProjectEntry | None = None) -> dict[str, Any]:
    s = summarize_session(path)
    return {
        "session_id": s.session_id,
        "title": s.ai_title or s.first_user_text or s.session_id,
        "first_prompt": s.first_user_text,
        "first_timestamp": s.first_timestamp,
        "last_timestamp": s.last_timestamp,
        "user_count": s.user_count,
        "assistant_count": s.assistant_count,
        "size_bytes": s.size_bytes,
        "project_slug": project_entry.slug if project_entry else None,
        "project_path": project_entry.local_path if project_entry else None,
    }


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
    files = list_session_files(project_dir)
    summaries = [summarize_session(p) for p in files]
    summaries.sort(key=lambda s: s.last_timestamp or "", reverse=True)
    settings = load_settings()
    entry = settings.projects.get(project_dir.name)
    out: list[dict[str, Any]] = []
    for s in summaries[:limit]:
        out.append(_summary_dict(s.path, entry))
    return out


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
    settings = load_settings()
    entry = settings.projects.get(project_dir.name)
    return _summary_dict(path, entry)


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
    needle = query.lower()
    if not needle.strip():
        return []

    if project is not None:
        project_dirs = [_resolve_project(project)]
    else:
        if not CLAUDE_PROJECTS.is_dir():
            return []
        project_dirs = [
            d for d in CLAUDE_PROJECTS.iterdir() if d.is_dir() and d.name.startswith("-")
        ]

    settings = load_settings()
    opts = RenderOptions(mode="transcript")
    hits: list[dict[str, Any]] = []

    for pd in project_dirs:
        entry = settings.projects.get(pd.name)
        for jsonl in pd.glob("*.jsonl"):
            try:
                events = read_session(jsonl)
            except OSError:
                continue
            text = render_markdown(events, opts=opts)
            lower = text.lower()
            idx = lower.find(needle)
            if idx < 0:
                continue
            start = max(0, idx - context_chars)
            end = min(len(text), idx + len(query) + context_chars)
            snippet = text[start:end].replace("\n", " ").strip()
            summary = summarize_session(jsonl)
            hits.append(
                {
                    "session_id": summary.session_id,
                    "title": summary.ai_title or summary.first_user_text or summary.session_id,
                    "project_slug": pd.name,
                    "project_name": entry.friendly_name if entry else pd.name.lstrip("-"),
                    "last_timestamp": summary.last_timestamp,
                    "snippet": snippet,
                    "match_index": idx,
                }
            )
            if len(hits) >= max_results:
                return hits
    return hits


def run() -> None:
    """Entry point for ``syne mcp`` — runs the FastMCP server on stdio."""
    mcp.run()


if __name__ == "__main__":
    run()
