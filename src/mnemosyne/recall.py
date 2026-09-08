"""``syne recall`` — a small, hard-capped stdout reader for self-alignment.

Prints recent-session headers, the curated-memory index, or search results to
stdout, size-capped, so a SessionStart hook or a non-MCP agent (told by its
instruction file to shell out to ``syne``) can pull *relevant* context without
ever dumping a whole transcript into the window. This is the CLI counterpart to
the MCP recall tools; both sit on :mod:`mnemosyne.query`, so they never drift.

Deliberately light: it never loads full transcripts, defaults to the current
project, and avoids the git-touching registry sync so it stays fast enough for a
session-start hook.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal

from mnemosyne import query
from mnemosyne.render import validate_cap

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mnemosyne.config import Settings
    from mnemosyne.query import ProjectPaths

RecallFormat = Literal["markdown", "json"]


def _short(text: str | None, n: int = 90) -> str:
    if not text:
        return ""
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _ts(value: str | None) -> str:
    return (value or "").replace("T", " ").split(".")[0] or "-"


def _cap(text: str, max_chars: int | None) -> str:
    """Cap `text`. ``None`` means unlimited; non-positive is rejected upstream."""
    if max_chars is None or len(text) <= max_chars:
        return text
    marker = "\n… [truncated]"
    if max_chars <= len(marker):
        return text[:max_chars]
    return text[: max_chars - len(marker)].rstrip() + marker


def _capped_json(payload: dict[str, Any], max_chars: int | None) -> str:
    """Serialize a recall payload without ever cutting JSON syntax in half."""

    def render(value: dict[str, Any]) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    out = dict(payload)
    out["items"] = list(payload.get("items", []))
    text = render(out)
    if max_chars is None or len(text) <= max_chars:
        return text
    out["truncated"] = True
    while out["items"]:
        out["items"].pop()
        text = render(out)
        if len(text) <= max_chars:
            return text
    raise ValueError(
        f"max_chars={max_chars} is too small for valid JSON recall output "
        f"({len(text)} characters required)"
    )


def _gather(
    dirs: list[Path],
    settings: Settings,
    *,
    query_str: str | None,
    memories: bool,
    limit: int,
    context_chars: int,
    project_paths: ProjectPaths | None,
) -> tuple[str, list[dict[str, Any]]]:
    """Return (kind, rows) for the requested recall, dispatched by the flags."""
    if query_str:
        if memories:
            return "memory_search", query.search_memories(dirs, query_str, limit)
        return "session_search", query.search_sessions(
            dirs, query_str, settings, limit, context_chars, project_paths=project_paths
        )
    if memories:
        rows: list[dict[str, Any]] = []
        for pd in dirs:
            rows.extend(query.memory_entries(pd))
        return "memory_index", rows[:limit]
    recent: list[dict[str, Any]] = []
    for pd in dirs:
        recent.extend(query.recent_sessions(pd, limit, settings, project_paths=project_paths))
    recent.sort(key=lambda r: r.get("last_timestamp") or "", reverse=True)
    return "recent", recent[:limit]


def _excluded_line(n: int) -> str:
    return f"_{n} session{'s' if n != 1 else ''} from a sibling working tree excluded._"


def _render_markdown(
    kind: str, rows: list[dict[str, Any]], *, multi_project: bool, excluded: int = 0
) -> str:
    if not rows:
        empty = {
            "recent": "_No recent sessions found._",
            "memory_index": "_No curated memories for this project._",
            "memory_search": "_No matching memories._",
            "session_search": "_No matching sessions._",
        }.get(kind, "_Nothing found._")
        return f"{empty}\n{_excluded_line(excluded)}" if excluded else empty

    def proj(r: dict[str, Any]) -> str:
        slug = r.get("project_slug") or r.get("project_name")
        return f" · _{str(slug).lstrip('-')}_" if (multi_project and slug) else ""

    lines: list[str] = []
    if kind == "recent":
        lines.append("## Recent sessions")
        for r in rows:
            lines.append(
                f"- `{r['session_id'][:8]}` · {_ts(r.get('last_timestamp'))} · "
                f"{_short(r.get('title'))}{proj(r)} "
                f"_({r.get('user_count', 0)}+{r.get('assistant_count', 0)} msgs)_"
            )
    elif kind == "memory_index":
        lines.append("## Curated memories")
        for r in rows:
            t = f" ({r['type']})" if r.get("type") else ""
            lines.append(f"- **{r['name']}**{t}{proj(r)} — {_short(r.get('description'))}")
    elif kind == "memory_search":
        lines.append("## Matching memories")
        for r in rows:
            t = f" ({r['type']})" if r.get("type") else ""
            lines.append(f"- **{r['name']}**{t}{proj(r)} — {_short(r.get('description'))}")
    else:  # session_search
        lines.append("## Matching sessions")
        for r in rows:
            lines.append(
                f"- `{r['session_id'][:8]}` · {_short(r.get('title'))}{proj(r)}\n"
                f"  …{_short(r.get('snippet'), 200)}…"
            )
    if excluded:
        lines.append(_excluded_line(excluded))
    return "\n".join(lines)


def _render_bundle(p: dict[str, Any]) -> str:
    lines = ["## Self-align brief"]
    meta: list[str] = []
    if p.get("project_slugs"):
        meta.append("project " + ", ".join(str(s).lstrip("-") for s in p["project_slugs"]))
    if p.get("query"):
        meta.append(f'query "{p["query"]}"')
    if meta:
        lines.append("_" + " · ".join(meta) + "_")
    if p.get("excluded_sessions"):
        lines.append(_excluded_line(int(p["excluded_sessions"])))
    multi_project = len(p.get("project_slugs", [])) > 1

    def project_suffix(row: dict[str, Any]) -> str:
        slug = row.get("project_slug")
        return f" · _{str(slug).lstrip('-')}_" if multi_project and slug else ""

    def memory_row(m: dict[str, Any]) -> str:
        t = f" ({m['type']})" if m.get("type") else ""
        return f"- **{m['name']}**{t}{project_suffix(m)} — {_short(m.get('description'))}"

    def recent_row(r: dict[str, Any]) -> str:
        sid, ts = r["session_id"][:8], _ts(r.get("last_timestamp"))
        return f"- `{sid}` · {ts} · {_short(r.get('title'))}{project_suffix(r)}"

    def hit_row(h: dict[str, Any]) -> str:
        sid, snip = h["session_id"][:8], _short(h.get("snippet"), 160)
        return f"- `{sid}` · {_short(h.get('title'))}{project_suffix(h)}  …{snip}…"

    def codex_row(c: dict[str, Any]) -> str:
        sid, ts = c["session_id"][:8], _ts(c.get("timestamp"))
        return f"- `{sid}` · {ts} · {_short(c.get('title')) or '(untitled)'}"

    def codex_summary_row(c: dict[str, Any]) -> str:
        return f"- `{c['file']}` · {_ts(c.get('updated_at'))} · {_short(c.get('title'))}"

    def suggestion_row(s: dict[str, Any]) -> str:
        return f"- `{s['call']}` — {s['why']}"

    sections: tuple[tuple[str, str, Callable[[dict[str, Any]], str]], ...] = (
        ("memories", "**Curated memories**", memory_row),
        ("recent_sessions", "**Recent sessions**", recent_row),
        ("session_hits", "**Transcript hits**", hit_row),
        ("codex_sessions", "**Codex sessions** _(same project, via Codex CLI)_", codex_row),
        ("codex_summaries", "**Codex handoffs**", codex_summary_row),
        ("suggested_next", "**Suggested next**", suggestion_row),
    )
    for key, header, row in sections:
        rows = p.get(key)
        if rows:
            lines.append("\n" + header)
            lines.extend(row(r) for r in rows)
    if p.get("guidance"):
        lines.append("\n_" + p["guidance"] + "_")
    return "\n".join(lines)


def build_bundle(
    dirs: list[Path],
    settings: Settings,
    *,
    query_str: str | None = None,
    max_chars: int | None = 6000,
    fmt: RecallFormat = "markdown",
    include_codex: bool = True,
    codex_home: Path | None = None,
    project_paths: ProjectPaths | None = None,
) -> str:
    """Render the aggregated self-align packet (the one-call entry point), capped.

    ``max_chars`` must be positive, or ``None`` to opt out of the cap explicitly.
    ``project_paths`` is the caller's scope decision per archive dir (see
    :data:`mnemosyne.query.ProjectPaths`); omit it to scope by the registry.
    """
    validate_cap(max_chars, "max_chars")
    raw_packet = query.self_align(
        dirs,
        settings,
        query=query_str,
        include_codex=include_codex,
        codex_home=codex_home,
        project_paths=project_paths,
    )
    size_fn = None if fmt == "json" else lambda value: len(_render_bundle(value))
    packet = query.fit_packet(
        raw_packet,
        max_chars,
        size_fn=size_fn,
    )
    if fmt == "json":
        return json.dumps(packet, ensure_ascii=False, separators=(",", ":"))
    return _cap(_render_bundle(packet), max_chars)


def build_recall(
    dirs: list[Path],
    settings: Settings,
    *,
    query_str: str | None = None,
    memories: bool = False,
    limit: int = 5,
    context_chars: int = 160,
    max_chars: int | None = 4000,
    fmt: RecallFormat = "markdown",
    project_paths: ProjectPaths | None = None,
) -> str:
    """Render a capped recall over ``dirs`` (the project dirs to consult).

    ``query_str`` set → search; otherwise recent sessions (or the memory index
    when ``memories`` is set). Output is hard-capped at ``max_chars``, which must
    be positive, or ``None`` to opt out of the cap explicitly. ``project_paths``
    is the caller's scope decision per archive dir; omit it to scope by the registry.
    """
    if limit < 0:
        raise ValueError("limit must be non-negative")
    if context_chars < 0:
        raise ValueError("context_chars must be non-negative")
    validate_cap(max_chars, "max_chars")
    kind, rows = _gather(
        dirs,
        settings,
        query_str=query_str,
        memories=memories,
        limit=limit,
        context_chars=context_chars,
        project_paths=project_paths,
    )
    # Memory kinds never touch sessions, so nothing was excluded from them.
    excluded = 0
    if kind in {"recent", "session_search"}:
        excluded = sum(
            query.excluded_session_count(pd, settings, project_paths=project_paths) for pd in dirs
        )
    if fmt == "json":
        payload: dict[str, Any] = {"kind": kind, "items": rows}
        if excluded:
            payload["excluded_sessions"] = excluded
        return _capped_json(payload, max_chars)
    return _cap(
        _render_markdown(kind, rows, multi_project=len(dirs) != 1, excluded=excluded), max_chars
    )
