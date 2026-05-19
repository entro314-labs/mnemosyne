"""Command-line interface for mnemosyne."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Annotated, get_args

import cyclopts
from cyclopts import Parameter
from rich.console import Console
from rich.prompt import IntPrompt, Prompt
from rich.table import Table

from claude_session_export.config import (
    CLAUDE_PROJECTS,
    CONFIG_PATH,
    ProjectEntry,
    Settings,
    load_settings,
    mark_used,
    resolve_output_dir,
    save_settings,
    sync_registry,
)
from claude_session_export.formats import Format, render_jsonl, render_plain
from claude_session_export.parser import (
    SessionSummary,
    list_session_files,
    project_dir_for_cwd,
    read_session,
    summarize_session,
)
from claude_session_export.render import Mode, RenderOptions, render_markdown

_VALID_MODES = get_args(Mode)
_VALID_FORMATS = get_args(Format)

_FORMAT_EXTENSION = {"markdown": ".md", "jsonl": ".jsonl", "plain": ".txt"}

app = cyclopts.App(
    name="syne",
    help="Export Claude Code session JSONL files to readable markdown.",
)
console = Console()
err_console = Console(stderr=True, style="red")


# ---- helpers ----


def _resolve_project_dir(project_dir: Path | None) -> Path:
    if project_dir is not None:
        if not project_dir.is_dir():
            raise SystemExit(f"error: project directory not found: {project_dir}")
        return project_dir
    candidate = project_dir_for_cwd(Path.cwd())
    if not candidate.is_dir():
        raise SystemExit(
            f"error: no Claude Code project found for cwd ({Path.cwd()}).\n"
            f"  expected: {candidate}\n"
            f"  pass --project-dir explicitly or run from a project's working directory."
        )
    return candidate


def _resolve_session(project_dir: Path, session_id: str) -> Path:
    matches = [p for p in list_session_files(project_dir) if p.stem.startswith(session_id)]
    if not matches:
        raise SystemExit(f"error: no session matching {session_id!r} in {project_dir}")
    if len(matches) > 1:
        ids = "\n  ".join(p.stem for p in matches)
        raise SystemExit(f"error: prefix {session_id!r} matches multiple sessions:\n  {ids}")
    return matches[0]


def _fmt_short_ts(ts: str | None) -> str:
    if not ts:
        return "-"
    return ts.replace("T", " ").split(".")[0] + "Z" if "." in ts else ts


def _short(text: str | None, n: int = 70) -> str:
    if not text:
        return ""
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _entry_for_project_dir(settings: Settings, project_dir: Path) -> ProjectEntry | None:
    return settings.projects.get(project_dir.name)


def _opts_from_settings(
    settings: Settings,
    *,
    mode: Mode | None = None,
    include_thinking: bool | None = None,
    include_attachments: bool | None = None,
    include_reminders: bool | None = None,
    max_tool_chars: int | None = None,
) -> RenderOptions:
    d = settings.defaults
    n = max_tool_chars if max_tool_chars is not None else d.max_tool_chars
    chosen_mode: Mode = mode if mode is not None else _coerce_mode(d.mode)
    return RenderOptions(
        mode=chosen_mode,
        include_thinking=include_thinking if include_thinking is not None else d.include_thinking,
        include_attachments=(
            include_attachments if include_attachments is not None else d.include_attachments
        ),
        include_reminders=(
            include_reminders if include_reminders is not None else d.include_reminders
        ),
        max_tool_result_chars=n,
        max_tool_input_chars=n,
    )


def _coerce_mode(value: str) -> Mode:
    if value in _VALID_MODES:
        return value  # type: ignore[return-value]
    return "transcript"


def _session_title(s: SessionSummary) -> str:
    return s.ai_title or s.first_user_text or s.session_id


_SLUG_KEEP = re.compile(r"[^\w\s-]+", re.UNICODE)
_SLUG_SQUASH = re.compile(r"[-\s_]+")


def _slugify(text: str, max_len: int = 80) -> str:
    """Title → 'fix-godot-project-initialization-errors'.

    Drops punctuation, collapses whitespace/underscores/hyphens to single '-',
    lower-cases, trims to max_len. Returns '' if nothing usable remains.
    """
    cleaned = _SLUG_KEEP.sub(" ", text).lower().strip()
    slug = _SLUG_SQUASH.sub("-", cleaned).strip("-")
    if len(slug) > max_len:
        slug = slug[:max_len].rstrip("-")
    return slug


def _filename_for(s: SessionSummary, *, fmt: Format = "markdown") -> str:
    """`ai_title` (or first prompt) slugified into a filename, else session id."""
    ext = _FORMAT_EXTENSION[fmt]
    slug = _slugify(s.ai_title or s.first_user_text or "")
    return f"{slug}{ext}" if slug else f"{s.session_id}{ext}"


def _resolve_filenames(
    summaries: list[SessionSummary],
    *,
    fmt: Format = "markdown",
) -> dict[str, str]:
    """Map session_id → final filename, disambiguating collisions with a short id."""
    ext = _FORMAT_EXTENSION[fmt]
    buckets: dict[str, list[SessionSummary]] = {}
    for s in summaries:
        buckets.setdefault(_filename_for(s, fmt=fmt), []).append(s)
    out: dict[str, str] = {}
    for name, group in buckets.items():
        if len(group) == 1:
            out[group[0].session_id] = name
            continue
        stem = name[: -len(ext)] if name.endswith(ext) else name
        for s in group:
            out[s.session_id] = f"{stem}-{s.session_id[:8]}{ext}"
    return out


def _render_for_format(
    summary: SessionSummary,
    events: list,
    opts: RenderOptions,
    *,
    fmt: Format,
    project_slug: str | None = None,
    project_path: str | None = None,
) -> str:
    """Dispatch to the renderer for the chosen output format."""
    label = _session_title(summary)
    title = f"{label}  \n_session {summary.session_id}_"
    if fmt == "markdown":
        return render_markdown(events, title=title, opts=opts, session_id=summary.session_id)
    if fmt == "plain":
        return render_plain(events, title=f"{label}\nSession {summary.session_id}", opts=opts)
    if fmt == "jsonl":
        return render_jsonl(
            events,
            session_id=summary.session_id,
            project_slug=project_slug,
            project_path=project_path,
            opts=opts,
        )
    raise ValueError(f"Unknown format: {fmt}")


def _write_export(
    s: SessionSummary,
    out_dir: Path,
    opts: RenderOptions,
    *,
    filename: str | None = None,
    fmt: Format = "markdown",
    project_slug: str | None = None,
    project_path: str | None = None,
    write_sidecar: bool = True,
) -> Path:
    events = read_session(s.path)
    rendered = _render_for_format(
        s, events, opts, fmt=fmt, project_slug=project_slug, project_path=project_path
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / (filename or _filename_for(s, fmt=fmt))
    out_path.write_text(rendered, encoding="utf-8")
    if write_sidecar:
        _write_session_sidecar(s, out_path, opts, fmt=fmt, project_slug=project_slug)
    return out_path


def _write_session_sidecar(
    s: SessionSummary,
    output_file: Path,
    opts: RenderOptions,
    *,
    fmt: Format,
    project_slug: str | None,
) -> Path:
    """Write `<output_file_stem>.meta.json` alongside the rendered export."""
    from datetime import UTC, datetime  # noqa: PLC0415

    sidecar_path = output_file.with_suffix(".meta.json")
    payload: dict[str, object] = {
        "session_id": s.session_id,
        "project_slug": project_slug,
        "ai_title": s.ai_title,
        "first_prompt": s.first_user_text,
        "first_timestamp": s.first_timestamp,
        "last_timestamp": s.last_timestamp,
        "user_count": s.user_count,
        "assistant_count": s.assistant_count,
        "source_jsonl": str(s.path),
        "source_size_bytes": s.size_bytes,
        "rendered_file": output_file.name,
        "rendered_size_bytes": output_file.stat().st_size,
        "mode": opts.mode,
        "format": fmt,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    sidecar_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return sidecar_path


def _write_project_index(
    out_dir: Path,
    summaries: list[SessionSummary],
    name_map: dict[str, str],
    *,
    project_slug: str,
    project_entry: ProjectEntry | None,
    fmt: Format,
    mode: Mode,
) -> Path:
    """Write `<out_dir>/index.json` summarizing every exported session."""
    from datetime import UTC, datetime  # noqa: PLC0415

    index_path = out_dir / "index.json"
    sessions = []
    for s in summaries:
        filename = name_map.get(s.session_id, _filename_for(s, fmt=fmt))
        sessions.append(
            {
                "session_id": s.session_id,
                "title": _session_title(s),
                "filename": filename,
                "first_timestamp": s.first_timestamp,
                "last_timestamp": s.last_timestamp,
                "user_count": s.user_count,
                "assistant_count": s.assistant_count,
            }
        )
    sessions.sort(key=lambda r: r["last_timestamp"] or "", reverse=True)
    payload = {
        "version": 1,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "project_slug": project_slug,
        "project_path": project_entry.local_path if project_entry else None,
        "friendly_name": project_entry.friendly_name if project_entry else None,
        "mode": mode,
        "format": fmt,
        "session_count": len(sessions),
        "sessions": sessions,
    }
    index_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return index_path


def _parse_selection(sel: str, n: int) -> list[int]:
    """Parse '1,3-5,8' → [0,2,3,4,7]. 'all' or empty → all."""
    cleaned = sel.strip().lower()
    if cleaned in ("", "all", "*"):
        return list(range(n))
    indices: set[int] = set()
    for raw in cleaned.split(","):
        token = raw.strip()
        if not token:
            continue
        try:
            if "-" in token:
                a, b = token.split("-", 1)
                indices.update(range(int(a) - 1, int(b)))
            else:
                indices.add(int(token) - 1)
        except ValueError as e:
            raise SystemExit(f"error: bad selection token {token!r}") from e
    return sorted(i for i in indices if 0 <= i < n)


# ---- interactive default ----


def _list_projects_with_sessions(settings: Settings) -> list[tuple[ProjectEntry, int]]:
    rows: list[tuple[ProjectEntry, int]] = []
    for entry in settings.projects.values():
        slug_dir = CLAUDE_PROJECTS / entry.slug
        if not slug_dir.is_dir():
            continue
        n = len(list(slug_dir.glob("*.jsonl")))
        if n > 0:
            rows.append((entry, n))
    rows.sort(key=lambda r: (r[0].last_used or "", r[1]), reverse=True)
    return rows


def _render_project_table(rows: list[tuple[ProjectEntry, int]]) -> None:
    table = Table(title="Claude Code projects", header_style="bold")
    table.add_column("#", justify="right")
    table.add_column("Name", style="cyan")
    table.add_column("Sessions", justify="right")
    table.add_column("Last used")
    table.add_column("Local path", style="dim")
    table.add_column("Git remote", style="dim")
    for i, (entry, n) in enumerate(rows, 1):
        table.add_row(
            str(i),
            entry.friendly_name or entry.slug,
            str(n),
            _fmt_short_ts(entry.last_used) if entry.last_used else "-",
            entry.local_path or "[red]missing[/red]",
            _short(entry.git_remote, 36) if entry.git_remote else "",
        )
    console.print(table)


def _render_session_table(summaries: list[SessionSummary]) -> None:
    table = Table(title="Sessions", header_style="bold")
    table.add_column("#", justify="right")
    table.add_column("ID", style="cyan")
    table.add_column("Last activity")
    table.add_column("Msgs", justify="right")
    table.add_column("Title / first prompt")
    for i, s in enumerate(summaries, 1):
        table.add_row(
            str(i),
            s.session_id[:8],
            _fmt_short_ts(s.last_timestamp),
            f"{s.user_count}+{s.assistant_count}",
            _short(_session_title(s), 70),
        )
    console.print(table)


@app.default
def interactive(
    mode: Annotated[
        Mode | None,
        Parameter(help="Override the saved mode default."),
    ] = None,
) -> None:
    """Pick a project, pick sessions, pick output dir, export. Updates the registry."""
    settings = load_settings()
    sync_registry(settings)
    save_settings(settings)

    rows = _list_projects_with_sessions(settings)
    if not rows:
        err_console.print(f"No Claude Code projects with sessions found under {CLAUDE_PROJECTS}.")
        return

    _render_project_table(rows)
    choice = IntPrompt.ask(
        "Pick a project",
        choices=[str(i) for i in range(1, len(rows) + 1)],
        default=1,
        show_choices=False,
    )
    entry, _ = rows[choice - 1]
    slug_dir = CLAUDE_PROJECTS / entry.slug

    summaries = sorted(
        (summarize_session(p) for p in list_session_files(slug_dir)),
        key=lambda s: s.last_timestamp or "",
        reverse=True,
    )
    if not summaries:
        err_console.print("No sessions in selected project.")
        return

    _render_session_table(summaries)
    selection = Prompt.ask(
        "Sessions to export (e.g. '1', '1,3-5', or 'all')",
        default="all",
    )
    targets = [summaries[i] for i in _parse_selection(selection, len(summaries))]
    if not targets:
        err_console.print("No sessions selected.")
        return

    default_out = str(resolve_output_dir(settings.defaults.output_dir, entry=entry))
    out_str = Prompt.ask("Export directory", default=default_out)
    out_dir = Path(out_str).expanduser()

    opts = _opts_from_settings(settings, mode=mode)
    console.print(f"[dim]mode: {opts.mode}[/dim]")
    name_map = _resolve_filenames(targets)
    for s in targets:
        path = _write_export(s, out_dir, opts, filename=name_map[s.session_id])
        console.print(f"[green]✓[/green] {s.session_id[:8]}  →  {path.name}")

    mark_used(settings, entry.slug)
    save_settings(settings)
    console.print(f"\n[bold]{len(targets)} session(s) exported[/bold]  →  {out_dir}")


# ---- non-interactive commands ----


@app.command(name="list")
def list_cmd(
    project_dir: Annotated[
        Path | None,
        Parameter(name=["--project-dir", "-p"], help="Path to ~/.claude/projects/<slug>/."),
    ] = None,
) -> None:
    """List sessions in a Claude Code project directory."""
    pd = _resolve_project_dir(project_dir)
    files = list_session_files(pd)
    if not files:
        err_console.print(f"No .jsonl sessions in {pd}")
        return

    summaries = sorted(
        (summarize_session(p) for p in files),
        key=lambda s: s.last_timestamp or "",
        reverse=True,
    )

    table = Table(title=f"Sessions in {pd}", header_style="bold")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Last activity", no_wrap=True)
    table.add_column("Msgs", justify="right")
    table.add_column("Size", justify="right")
    table.add_column("Title / first prompt")
    for s in summaries:
        size_kb = s.size_bytes / 1024
        size_s = f"{size_kb:.0f}K" if size_kb < 1024 else f"{size_kb / 1024:.1f}M"
        table.add_row(
            s.session_id[:8],
            _fmt_short_ts(s.last_timestamp),
            f"{s.user_count}+{s.assistant_count}",
            size_s,
            _short(_session_title(s), 80),
        )
    console.print(table)
    console.print(f"\n[dim]{len(summaries)} sessions[/dim]")


@app.command
def export(
    session_id: str,
    /,
    project_dir: Annotated[Path | None, Parameter(name=["--project-dir", "-p"])] = None,
    output: Annotated[
        Path | None,
        Parameter(name=["--output", "-o"], help="Output file or directory."),
    ] = None,
    fmt: Annotated[
        Format,
        Parameter(name=["--format", "-f"], help="markdown (default), jsonl, or plain."),
    ] = "markdown",
    mode: Annotated[
        Mode | None,
        Parameter(help="transcript (default), compact, or full."),
    ] = None,
    include_thinking: bool | None = None,
    include_attachments: bool | None = None,
    include_reminders: bool | None = None,
    max_tool_chars: int | None = None,
    sidecar: bool = True,
) -> None:
    """Export a single session (by full UUID or unique prefix) to disk."""
    settings = load_settings()
    pd = _resolve_project_dir(project_dir)
    path = _resolve_session(pd, session_id)
    summary = summarize_session(path)

    if output is None:
        # Make sure the entry exists so the {local_path} template can resolve.
        sync_registry(settings)
        entry = _entry_for_project_dir(settings, pd)
        output = resolve_output_dir(settings.defaults.output_dir, entry=entry)

    opts = _opts_from_settings(
        settings,
        mode=mode,
        include_thinking=include_thinking,
        include_attachments=include_attachments,
        include_reminders=include_reminders,
        max_tool_chars=max_tool_chars,
    )

    if output.suffix:  # explicit filename
        events = read_session(path)
        rendered = _render_for_format(summary, events, opts, fmt=fmt, project_slug=pd.name)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
        out_path = output
        if sidecar:
            _write_session_sidecar(summary, output, opts, fmt=fmt, project_slug=pd.name)
    else:  # directory
        out_path = _write_export(
            summary, output, opts, fmt=fmt, project_slug=pd.name, write_sidecar=sidecar
        )

    mark_used(settings, pd.name)
    save_settings(settings)
    console.print(f"✓ Wrote [green]{out_path}[/green]")


@app.command(name="export-all")
def export_all(
    project_dir: Annotated[Path | None, Parameter(name=["--project-dir", "-p"])] = None,
    output: Annotated[
        Path | None,
        Parameter(name=["--output", "-o"], help="Output directory."),
    ] = None,
    all_projects: Annotated[
        bool,
        Parameter(
            name=["--all-projects"],
            help=(
                "Iterate every known project. Writes to <output>/<project>/<title>.md by default."
            ),
        ),
    ] = False,
    fmt: Annotated[
        Format,
        Parameter(name=["--format", "-f"], help="markdown (default), jsonl, or plain."),
    ] = "markdown",
    mode: Annotated[
        Mode | None,
        Parameter(help="transcript (default), compact, or full."),
    ] = None,
    since: Annotated[
        str | None,
        Parameter(help="Keep sessions with last_timestamp >= this (e.g. 2026-05-01)."),
    ] = None,
    until: Annotated[
        str | None,
        Parameter(help="Keep sessions with last_timestamp <= this."),
    ] = None,
    matching: Annotated[
        str | None,
        Parameter(help="Regex; keep sessions whose title or first prompt matches."),
    ] = None,
    include_thinking: bool | None = None,
    include_attachments: bool | None = None,
    include_reminders: bool | None = None,
    max_tool_chars: int | None = None,
    sidecar: bool = True,
    index: bool = True,
    skip_empty: bool = True,
) -> None:
    """Export every session in a project directory (with optional filters)."""
    settings = load_settings()
    sync_registry(settings)

    if all_projects:
        _export_all_projects(
            settings,
            output=output,
            fmt=fmt,
            mode=mode,
            since=since,
            until=until,
            matching=matching,
            include_thinking=include_thinking,
            include_attachments=include_attachments,
            include_reminders=include_reminders,
            max_tool_chars=max_tool_chars,
            sidecar=sidecar,
            index=index,
            skip_empty=skip_empty,
        )
        return

    pd = _resolve_project_dir(project_dir)
    files = list_session_files(pd)
    if not files:
        err_console.print(f"No .jsonl sessions in {pd}")
        return

    if output is None:
        entry = _entry_for_project_dir(settings, pd)
        output = resolve_output_dir(settings.defaults.output_dir, entry=entry)

    opts = _opts_from_settings(
        settings,
        mode=mode,
        include_thinking=include_thinking,
        include_attachments=include_attachments,
        include_reminders=include_reminders,
        max_tool_chars=max_tool_chars,
    )
    output.mkdir(parents=True, exist_ok=True)

    sync_registry(settings)
    entry = _entry_for_project_dir(settings, pd)

    all_summaries = [summarize_session(p) for p in files]
    candidates = [s for s in all_summaries if not (skip_empty and s.message_count == 0)]
    targets = _apply_filters(candidates, since=since, until=until, matching=matching)
    name_map = _resolve_filenames(targets, fmt=fmt)

    written = skipped = filtered = 0
    target_ids = {s.session_id for s in targets}
    for summary in all_summaries:
        if skip_empty and summary.message_count == 0:
            skipped += 1
            console.print(f"[dim]skip[/dim] {summary.session_id[:8]}  (no conversation content)")
            continue
        if summary.session_id not in target_ids:
            filtered += 1
            continue
        out_path = _write_export(
            summary,
            output,
            opts,
            filename=name_map[summary.session_id],
            fmt=fmt,
            project_slug=pd.name,
            project_path=entry.local_path if entry else None,
            write_sidecar=sidecar,
        )
        console.print(f"[green]✓[/green] {summary.session_id[:8]}  →  {out_path.name}")
        written += 1

    if index and targets:
        index_path = _write_project_index(
            output,
            targets,
            name_map,
            project_slug=pd.name,
            project_entry=entry,
            fmt=fmt,
            mode=opts.mode,
        )
        console.print(f"[dim]wrote index:[/dim] {index_path.name}")

    mark_used(settings, pd.name)
    save_settings(settings)
    summary_line = f"{written} written, {skipped} skipped"
    if filtered:
        summary_line += f", {filtered} filtered out"
    console.print(f"\n[bold]{summary_line}[/bold]  →  {output}")


def _export_all_projects(
    settings: Settings,
    *,
    output: Path | None,
    fmt: Format,
    mode: Mode | None,
    since: str | None,
    until: str | None,
    matching: str | None,
    include_thinking: bool | None,
    include_attachments: bool | None,
    include_reminders: bool | None,
    max_tool_chars: int | None,
    sidecar: bool,
    index: bool,
    skip_empty: bool,
) -> None:
    """Walk every known project; write each into <output>/<project>/."""
    root = output if output is not None else (Path.home() / "claude-archive")
    root.mkdir(parents=True, exist_ok=True)

    opts = _opts_from_settings(
        settings,
        mode=mode,
        include_thinking=include_thinking,
        include_attachments=include_attachments,
        include_reminders=include_reminders,
        max_tool_chars=max_tool_chars,
    )

    total_written = total_skipped = total_filtered = projects_touched = 0
    for entry in settings.projects.values():
        slug_dir = CLAUDE_PROJECTS / entry.slug
        if not slug_dir.is_dir():
            continue
        files = list_session_files(slug_dir)
        if not files:
            continue

        all_summaries = [summarize_session(p) for p in files]
        candidates = [s for s in all_summaries if not (skip_empty and s.message_count == 0)]
        targets = _apply_filters(candidates, since=since, until=until, matching=matching)
        if not targets:
            continue

        project_label = entry.friendly_name or entry.slug.lstrip("-") or "unnamed"
        project_dir_name = _slugify(project_label) or _slugify(entry.slug.lstrip("-")) or "unnamed"
        project_out = root / project_dir_name
        project_out.mkdir(parents=True, exist_ok=True)
        name_map = _resolve_filenames(targets, fmt=fmt)

        target_ids = {s.session_id for s in targets}
        for summary in all_summaries:
            if skip_empty and summary.message_count == 0:
                total_skipped += 1
                continue
            if summary.session_id not in target_ids:
                total_filtered += 1
                continue
            _write_export(
                summary,
                project_out,
                opts,
                filename=name_map[summary.session_id],
                fmt=fmt,
                project_slug=entry.slug,
                project_path=entry.local_path,
                write_sidecar=sidecar,
            )
            total_written += 1

        if index:
            _write_project_index(
                project_out,
                targets,
                name_map,
                project_slug=entry.slug,
                project_entry=entry,
                fmt=fmt,
                mode=opts.mode,
            )
        projects_touched += 1
        console.print(
            f"[green]✓[/green] {project_label:30s} → {project_out}  ({len(targets)} sessions)"
        )

    console.print(
        f"\n[bold]{total_written} sessions across {projects_touched} projects[/bold] → {root}"
        f"  [dim]({total_skipped} skipped, {total_filtered} filtered)[/dim]"
    )


def _apply_filters(
    summaries: list[SessionSummary],
    *,
    since: str | None,
    until: str | None,
    matching: str | None,
) -> list[SessionSummary]:
    """Keep sessions whose last_timestamp falls in [since, until] AND whose
    title / first prompt matches `matching` (regex, case-insensitive)."""
    pattern = re.compile(matching, re.IGNORECASE) if matching else None
    out: list[SessionSummary] = []
    for s in summaries:
        ts = s.last_timestamp or s.first_timestamp or ""
        if since and ts and ts < since:
            continue
        if until and ts and ts > until:
            continue
        if pattern is not None:
            haystack = " ".join(filter(None, [s.ai_title, s.first_user_text]))
            if not pattern.search(haystack):
                continue
        out.append(s)
    return out


@app.command
def merge(
    session_ids: Annotated[
        list[str] | None,
        Parameter(
            help=(
                "One or more session IDs / prefixes to merge. "
                "Omit when using --all-from or --all-projects."
            ),
        ),
    ] = None,
    output: Annotated[
        Path,
        Parameter(name=["--output", "-o"], help="Output file path."),
    ] = Path("synthesis.md"),
    project_dir: Annotated[
        Path | None,
        Parameter(name=["--project-dir", "-p"]),
    ] = None,
    all_from: Annotated[
        str | None,
        Parameter(
            name=["--all-from"],
            help="Merge every session in this project (slug, path, or friendly name).",
        ),
    ] = None,
    all_projects: Annotated[
        bool,
        Parameter(
            name=["--all-projects"],
            help="Merge every session across ALL projects. Pair with --since/--matching to scope.",
        ),
    ] = False,
    last: Annotated[
        int | None,
        Parameter(
            name=["--last"],
            help="After filters, keep only the N most-recent sessions (by last_timestamp).",
        ),
    ] = None,
    fmt: Annotated[
        Format,
        Parameter(name=["--format", "-f"], help="markdown (default), jsonl, or plain."),
    ] = "markdown",
    mode: Annotated[Mode | None, Parameter()] = None,
    since: Annotated[str | None, Parameter()] = None,
    until: Annotated[str | None, Parameter()] = None,
    matching: Annotated[str | None, Parameter()] = None,
    include_thinking: bool | None = None,
    include_attachments: bool | None = None,
    include_reminders: bool | None = None,
    max_tool_chars: int | None = None,
) -> None:
    """Combine multiple sessions into one document.

    Common shapes:
        syne merge abc12345 def67890 -o synthesis.md
        syne merge --all-from scifigame --since 2026-05-01 -o may-recap.md
        syne merge --all-from scifigame --last 5 -o last-5.md   # onboarding context
        syne merge --all-projects --since 2026-05-01 -o weekly.md   # cross-project
        syne merge abc12345 def67890 --format jsonl -o sessions.jsonl
    """
    settings = load_settings()
    if sum(1 for x in (session_ids, all_from, all_projects) if x) != 1:
        raise SystemExit(
            "error: pick exactly one of: session IDs as args, --all-from <project>, "
            "or --all-projects."
        )

    targets: list[tuple[SessionSummary, Path]]  # (summary, project_dir)

    if all_projects:
        sync_registry(settings)
        raw_pairs: list[tuple[SessionSummary, Path]] = []
        for entry in settings.projects.values():
            slug_dir = CLAUDE_PROJECTS / entry.slug
            if not slug_dir.is_dir():
                continue
            for jsonl in list_session_files(slug_dir):
                s = summarize_session(jsonl)
                if s.message_count > 0:
                    raw_pairs.append((s, slug_dir))
        kept = {
            s.session_id
            for s in _apply_filters(
                [s for s, _ in raw_pairs], since=since, until=until, matching=matching
            )
        }
        targets = [(s, pd) for (s, pd) in raw_pairs if s.session_id in kept]
    elif all_from:
        pd = _resolve_project_for_merge(all_from)
        files = list_session_files(pd)
        summaries = [summarize_session(p) for p in files if summarize_session(p).message_count > 0]
        filtered = _apply_filters(summaries, since=since, until=until, matching=matching)
        targets = [(s, pd) for s in filtered]
    else:
        assert session_ids is not None
        pd = _resolve_project_dir(project_dir)
        summaries = [summarize_session(_resolve_session(pd, sid)) for sid in session_ids]
        filtered = _apply_filters(summaries, since=since, until=until, matching=matching)
        targets = [(s, pd) for s in filtered]

    if not targets:
        raise SystemExit("error: no sessions matched (after filters).")

    targets.sort(key=lambda pair: pair[0].first_timestamp or pair[0].last_timestamp or "")

    if last is not None and last > 0:
        # Take the N most-recent (by last_timestamp), then re-sort chronologically.
        targets.sort(key=lambda pair: pair[0].last_timestamp or "", reverse=True)
        targets = targets[:last]
        targets.sort(key=lambda pair: pair[0].first_timestamp or pair[0].last_timestamp or "")

    opts = _opts_from_settings(
        settings,
        mode=mode,
        include_thinking=include_thinking,
        include_attachments=include_attachments,
        include_reminders=include_reminders,
        max_tool_chars=max_tool_chars,
    )

    rendered = _render_merged(targets, opts, fmt=fmt, settings=settings)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")

    sidecar = output.with_suffix(".meta.json")
    sidecar.write_text(
        json.dumps(
            _merge_sidecar_payload(targets, opts.mode, fmt, all_projects=all_projects),
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    console.print(f"✓ Merged [bold]{len(targets)}[/bold] sessions → [green]{output}[/green]")
    console.print(f"[dim]  sidecar: {sidecar.name}[/dim]")


def _resolve_project_for_merge(name_or_path: str) -> Path:
    """Accept a slug, an absolute project path, or a friendly name from the registry."""
    p = Path(name_or_path)
    if p.is_absolute() and p.is_dir():
        return project_dir_for_cwd(p) if project_dir_for_cwd(p).is_dir() else p
    if (CLAUDE_PROJECTS / name_or_path).is_dir():
        return CLAUDE_PROJECTS / name_or_path
    # Try friendly_name lookup in the registry.
    settings = load_settings()
    sync_registry(settings)
    for entry in settings.projects.values():
        if entry.friendly_name == name_or_path:
            slug_dir = CLAUDE_PROJECTS / entry.slug
            if slug_dir.is_dir():
                return slug_dir
    raise SystemExit(f"error: cannot resolve project {name_or_path!r}")


def _render_merged(
    targets: list[tuple[SessionSummary, Path]],
    opts: RenderOptions,
    *,
    fmt: Format,
    settings: Settings,
) -> str:
    """Render N (session, project_dir) pairs into a single document, per format."""
    if fmt == "jsonl":
        # One JSON line per turn across all sessions, in chronological order.
        chunks: list[str] = []
        for s, pd in targets:
            entry = settings.projects.get(pd.name)
            events = read_session(s.path)
            chunks.append(
                render_jsonl(
                    events,
                    session_id=s.session_id,
                    project_slug=pd.name,
                    project_path=entry.local_path if entry else None,
                    opts=opts,
                ).rstrip("\n")
            )
        return "\n".join(c for c in chunks if c) + "\n"

    sections: list[str] = []
    distinct_projects = {pd.name for _, pd in targets}
    cross_project = len(distinct_projects) > 1

    if fmt == "markdown":
        scope = "across projects" if cross_project else "from one project"
        sections.append(
            f"# Merged transcripts\n\n"
            f"_{len(targets)} sessions {scope}, ordered chronologically. "
            f"Generated by `syne merge`._"
        )
    else:  # plain
        sections.append(f"Merged transcripts — {len(targets)} sessions")

    for s, pd in targets:
        entry = settings.projects.get(pd.name)
        project_label = (entry.friendly_name if entry else pd.name) or pd.name
        if fmt == "markdown":
            origin = f" · _{project_label}_" if cross_project else ""
            section_title = (
                f"## {_session_title(s)}{origin}\n\n"
                f"_session `{s.session_id}` · "
                f"{s.first_timestamp or '?'} → {s.last_timestamp or '?'} · "
                f"{s.user_count}+{s.assistant_count} msgs_"
            )
            body = render_markdown(
                read_session(s.path), title=None, opts=opts, session_id=s.session_id
            )
            sections.append(section_title + "\n\n" + body.lstrip())
        else:  # plain
            origin = f" [{project_label}]" if cross_project else ""
            section_title = (
                f"\n========================================\n"
                f"  {_session_title(s)}{origin}\n"
                f"  Session {s.session_id} | {s.first_timestamp} → {s.last_timestamp}\n"
                f"========================================"
            )
            body = render_plain(read_session(s.path), title=None, opts=opts)
            sections.append(section_title + "\n\n" + body)

    separator = "\n\n---\n\n" if fmt == "markdown" else "\n\n"
    return separator.join(sections) + "\n"


def _merge_sidecar_payload(
    targets: list[tuple[SessionSummary, Path]],
    mode: Mode,
    fmt: Format,
    *,
    all_projects: bool = False,
) -> dict[str, object]:
    from datetime import UTC, datetime  # noqa: PLC0415

    distinct_slugs = sorted({pd.name for _, pd in targets})
    return {
        "version": 1,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "scope": "all-projects"
        if all_projects
        else ("single-project" if len(distinct_slugs) == 1 else "ids"),
        "project_slugs": distinct_slugs,
        "mode": mode,
        "format": fmt,
        "session_count": len(targets),
        "sessions": [
            {
                "session_id": s.session_id,
                "project_slug": pd.name,
                "title": _session_title(s),
                "first_timestamp": s.first_timestamp,
                "last_timestamp": s.last_timestamp,
                "user_count": s.user_count,
                "assistant_count": s.assistant_count,
            }
            for s, pd in targets
        ],
    }


@app.command
def mcp() -> None:
    """Run the MCP server on stdio (for `syne install`-managed Claude Code plugin)."""
    # Lazy import: pulls in mcp/anyio/uvicorn which we don't need for the
    # other subcommands.
    from claude_session_export.mcp_server import run  # noqa: PLC0415

    run()


@app.command
def install(
    install_path: Annotated[
        Path | None,
        Parameter(
            name=["--install-path"],
            help=(
                "Override default plugin install location "
                "(~/.claude/plugins/mnemosyne)."
            ),
        ),
    ] = None,
) -> None:
    """Install the Claude Code plugin sidecar (skills, slash commands, MCP server).

    Copies bundled templates into ~/.claude/plugins/mnemosyne/ and
    registers a local-directory marketplace so Claude Code's `/plugin install`
    flow can find it. Re-running this command updates the plugin in place.
    """
    from claude_session_export.installer import (  # noqa: PLC0415
        DEFAULT_INSTALL_PATH,
        install_plugin,
        print_post_install_instructions,
    )

    target = install_path or DEFAULT_INSTALL_PATH
    result = install_plugin(target)
    print_post_install_instructions(result, console=console)


@app.command
def uninstall(
    install_path: Annotated[
        Path | None,
        Parameter(name=["--install-path"]),
    ] = None,
) -> None:
    """Remove the plugin sidecar from ~/.claude/plugins/ and de-register the marketplace."""
    from claude_session_export.installer import (  # noqa: PLC0415
        DEFAULT_INSTALL_PATH,
        uninstall_plugin,
    )

    target = install_path or DEFAULT_INSTALL_PATH
    removed = uninstall_plugin(target)
    if removed:
        console.print(f"[green]✓ uninstalled[/green] {target}")
    else:
        console.print(f"[dim](nothing to remove at {target})[/dim]")


@app.command
def projects(refresh_git: bool = False) -> None:
    """List all discovered Claude Code projects (and refresh the registry)."""
    settings = load_settings()
    sync_registry(settings, refresh_git=refresh_git)
    save_settings(settings)

    rows = _list_projects_with_sessions(settings)
    if not rows:
        err_console.print(f"No projects with sessions under {CLAUDE_PROJECTS}.")
        return
    _render_project_table(rows)
    console.print(f"\n[dim]{len(rows)} projects · registry: {CONFIG_PATH}[/dim]")


@app.command
def config_show() -> None:
    """Show the current config file path and contents."""
    if not CONFIG_PATH.exists():
        console.print(f"[dim]No config yet. Will be written to:[/dim] {CONFIG_PATH}")
        return
    console.print(f"[dim]{CONFIG_PATH}[/dim]\n")
    console.print(CONFIG_PATH.read_text(encoding="utf-8"))


if __name__ == "__main__":
    app()
