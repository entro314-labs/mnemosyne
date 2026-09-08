"""Command-line interface for mnemosyne."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Annotated, cast, get_args

import cyclopts
from cyclopts import Parameter
from rich.console import Console
from rich.prompt import IntPrompt, Prompt
from rich.table import Table

from mnemosyne import __version__
from mnemosyne import codex as codex_store
from mnemosyne.align import (
    apply_to_file,
    files_with_region,
    is_available,
    render_block,
    target_files,
)
from mnemosyne.artifact_export import (
    ArtifactWriteResult,
    slugify,
    write_project_memories,
    write_session_artifacts,
)
from mnemosyne.artifacts import ArtifactSelection, discover_session_artifacts
from mnemosyne.config import (
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
from mnemosyne.drift import check_drift, render_drift_markdown
from mnemosyne.formats import Format, render_jsonl, render_plain
from mnemosyne.memory import collect_memories
from mnemosyne.parser import (
    SessionSummary,
    list_session_files,
    project_dir_for_cwd,
    read_session,
    summarize_session,
)
from mnemosyne.query import all_project_dirs
from mnemosyne.recall import RecallFormat, build_bundle, build_recall
from mnemosyne.render import Mode, RenderOptions, render_markdown

_VALID_MODES = get_args(Mode)
_VALID_FORMATS = get_args(Format)

_FORMAT_EXTENSION = {"markdown": ".md", "jsonl": ".jsonl", "plain": ".txt"}

# Artifact-selection flags, shared verbatim by `export`, `export-all`, and the
# interactive default so their help text and behaviour never drift.
MemoriesFlag = Annotated[
    bool,
    Parameter(
        name=["--memories"],
        help="Also export the project's curated memory layer (memory/*.md + index).",
    ),
]
SubagentsFlag = Annotated[
    bool,
    Parameter(
        name=["--subagents"],
        help="Also export spawned subagent transcripts (rendered like the main transcript).",
    ),
]
SummariesFlag = Annotated[
    bool,
    Parameter(
        name=["--summaries"],
        help="Also export session-memory handoff digests (session-memory/summary.md).",
    ),
]
WorkflowsFlag = Annotated[
    bool,
    Parameter(
        name=["--workflows"],
        help="Also export workflow scripts, run journals, and orchestrated subagents.",
    ),
]
ToolResultsFlag = Annotated[
    bool,
    Parameter(
        name=["--tool-results"],
        help="Also export externalised large tool outputs (deterministically scrubbed).",
    ),
]
FullArtifactsFlag = Annotated[
    bool,
    Parameter(
        name=["--full"],
        help=(
            "Export ALL artifact types (memories + subagents + summaries + workflows + "
            "tool-results). Note: distinct from --mode full, which sets transcript verbosity."
        ),
    ),
]

app = cyclopts.App(
    name="syne",
    help="Export Claude Code session JSONL files to readable markdown.",
    version=__version__,
)
console = Console()
err_console = Console(stderr=True, style="red")


# ---- helpers ----


@dataclass(frozen=True, slots=True)
class ProjectScope:
    """An archive directory plus the working tree that authoritatively defines it.

    ``path`` is the recorded-``cwd`` filter. It is set when the archive was found
    from the current working directory (the documented default scope), and ``None``
    when the user named a directory with ``--project-dir`` — naming a directory is
    explicit selection, so it is taken at face value.
    """

    dir: Path
    path: Path | None

    def session_files(self) -> list[Path]:
        return list_session_files(self.dir, project_path=self.path)

    def excluded_count(self) -> int:
        """Sessions in the directory that belong to a different working tree."""
        if self.path is None:
            return 0
        return len(list_session_files(self.dir)) - len(self.session_files())


def _warn_if_foreign_sessions(scope: ProjectScope) -> None:
    """Surface slug-collision filtering instead of hiding it (source-integrity rule)."""
    n = scope.excluded_count()
    if n:
        err_console.print(
            f"[dim]note: {n} session(s) in {scope.dir.name} belong to a different "
            f"working tree and were excluded; use --project-dir to include them.[/dim]"
        )


def _resolve_project_dir(project_dir: Path | None) -> ProjectScope:
    if project_dir is not None:
        if not project_dir.is_dir():
            raise SystemExit(f"error: project directory not found: {project_dir}")
        return ProjectScope(dir=project_dir, path=None)
    cwd = Path.cwd()
    candidate = project_dir_for_cwd(cwd)
    if not candidate.is_dir():
        raise SystemExit(
            f"error: no Claude Code project found for cwd ({cwd}).\n"
            f"  expected: {candidate}\n"
            f"  pass --project-dir explicitly or run from a project's working directory."
        )
    return ProjectScope(dir=candidate, path=cwd)


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
        return cast("Mode", value)
    return "transcript"


def _session_title(s: SessionSummary) -> str:
    return s.ai_title or s.first_user_text or s.session_id


def _filename_for(s: SessionSummary, *, fmt: Format = "markdown") -> str:
    """`ai_title` (or first prompt) slugified into a filename, else session id."""
    ext = _FORMAT_EXTENSION[fmt]
    slug = slugify(s.ai_title or s.first_user_text or "")
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
            prefix_len = 8
            while any(
                other.session_id != s.session_id
                and other.session_id[:prefix_len] == s.session_id[:prefix_len]
                for other in group
            ):
                prefix_len += 1
            out[s.session_id] = f"{stem}-{s.session_id[:prefix_len]}{ext}"
    return out


def _project_output_names(entries: list[ProjectEntry]) -> dict[str, str]:
    """Stable, non-colliding directory names for a cross-project export."""
    bases = {
        entry.slug: (
            slugify(entry.friendly_name or entry.slug.lstrip("-"))
            or slugify(entry.slug.lstrip("-"))
            or "unnamed"
        )
        for entry in entries
    }
    counts: dict[str, int] = {}
    for base in bases.values():
        counts[base] = counts.get(base, 0) + 1
    return {
        slug: base if counts[base] == 1 else f"{base}-{sha256(slug.encode()).hexdigest()[:8]}"
        for slug, base in bases.items()
    }


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
    selection: ArtifactSelection | None = None,
    project_dir: Path | None = None,
    artifact_tally: ArtifactWriteResult | None = None,
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
    _maybe_write_session_artifacts(
        s,
        out_dir,
        out_path.stem,
        opts=opts,
        selection=selection,
        project_dir=project_dir,
        tally=artifact_tally,
    )
    return out_path


def _maybe_write_session_artifacts(
    s: SessionSummary,
    out_dir: Path,
    base: str,
    *,
    opts: RenderOptions,
    selection: ArtifactSelection | None,
    project_dir: Path | None,
    tally: ArtifactWriteResult | None,
) -> None:
    """Discover and write a session's subagent/summary/workflow/tool-result bundle.

    Subagent and workflow transcripts render with the same ``opts`` (mode + noise
    scrub) as the main transcript, so the bundle stays consistent with it.
    """
    if selection is None or not selection.any_session_scoped or project_dir is None:
        return
    arts = discover_session_artifacts(project_dir, s.session_id)
    res = write_session_artifacts(arts, out_dir, base, opts=opts, selection=selection)
    if tally is not None:
        tally.merge(res)


def _selection_from(
    *,
    memories: bool,
    subagents: bool,
    summaries: bool,
    workflows: bool,
    tool_results: bool,
    full: bool,
) -> ArtifactSelection:
    return ArtifactSelection.resolve(
        memories=memories,
        subagents=subagents,
        summaries=summaries,
        workflows=workflows,
        tool_results=tool_results,
        full=full,
    )


def _maybe_write_memories(
    selection: ArtifactSelection,
    out_dir: Path,
    project_dir: Path,
    entry: ProjectEntry | None,
) -> int:
    """Write a project's curated memory layer when ``--memories``/``--full`` is set.

    Returns the number of memories written (0 when none / not selected).
    """
    if not selection.memories:
        return 0
    coll = collect_memories(project_dir)
    if not coll:
        return 0
    write_project_memories(
        coll,
        out_dir,
        project_label=(entry.friendly_name if entry else None),
        project_path=(entry.local_path if entry else None),
    )
    return len(coll.memories)


def _report_artifacts(tally: ArtifactWriteResult, memories: int) -> None:
    """Print a one-line summary of artifacts written, if any."""
    bits: list[str] = []
    if memories:
        bits.append(f"{memories} mem{'ory' if memories == 1 else 'ories'}")
    if tally.summaries:
        bits.append(f"{tally.summaries} summary")
    if tally.subagents:
        bits.append(f"{tally.subagents} subagents")
    if tally.workflow_agents or tally.scripts:
        bits.append(f"{tally.scripts} scripts/{tally.workflow_agents} workflow agents")
    if tally.tool_results:
        bits.append(f"{tally.tool_results} tool-results")
    if bits:
        console.print(f"[dim]artifacts:[/dim] {', '.join(bits)}")


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
        "source_malformed_lines": s.malformed_lines,
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
    """Write `<out_dir>/index.json` summarizing every exported session.

    The index mirrors the export directory, not just this run: entries from a
    previous index are kept while their rendered file still exists, so a filtered
    rerun (``--since``, ``--matching``) refreshes its own sessions without
    forgetting the rest. An unreadable prior index is derived state we own and is
    simply rebuilt. ``mode`` / ``format`` describe the run that wrote the file.
    """
    from datetime import UTC, datetime  # noqa: PLC0415

    index_path = out_dir / "index.json"
    by_id: dict[str, dict[str, object]] = {}
    for row in _prior_index_rows(index_path):
        prior_id, prior_file = row.get("session_id"), row.get("filename")
        if (
            isinstance(prior_id, str)
            and isinstance(prior_file, str)
            and (out_dir / prior_file).is_file()
        ):
            by_id[prior_id] = row
    for s in summaries:
        filename = name_map.get(s.session_id, _filename_for(s, fmt=fmt))
        by_id[s.session_id] = {
            "session_id": s.session_id,
            "title": _session_title(s),
            "filename": filename,
            "first_timestamp": s.first_timestamp,
            "last_timestamp": s.last_timestamp,
            "user_count": s.user_count,
            "assistant_count": s.assistant_count,
        }
    sessions = sorted(
        by_id.values(), key=lambda r: str(r.get("last_timestamp") or ""), reverse=True
    )
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


def _prior_index_rows(index_path: Path) -> list[dict[str, object]]:
    """Session rows from an existing ``index.json``; empty when absent or unreadable."""
    if not index_path.is_file():
        return []
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = data.get("sessions") if isinstance(data, dict) else None
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


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


def _detect_cwd_project(
    settings: Settings,
    *,
    cwd: Path | None = None,
    claude_root: Path | None = None,
) -> ProjectEntry | None:
    """Return the registry entry for the workspace the CLI was invoked from.

    Maps the current working directory to its ``~/.claude/projects/<slug>``
    directory; if that directory exists and holds at least one session, ``syne``
    (no args) can skip the project picker and jump straight to this workspace's
    sessions. Returns ``None`` when the cwd is not a known Claude Code workspace,
    so the caller falls back to the interactive project chooser.

    A synthesized entry is returned when the workspace has sessions on disk but
    has not yet been recorded in the registry.
    """
    cwd = cwd or Path.cwd()
    candidate = project_dir_for_cwd(cwd, claude_home=claude_root)
    if not candidate.is_dir() or not any(candidate.glob("*.jsonl")):
        return None
    entry = settings.projects.get(candidate.name)
    if entry is not None:
        return entry
    return ProjectEntry(slug=candidate.name, local_path=str(cwd), friendly_name=cwd.name)


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
# Cyclopts binds POSITIONAL_OR_KEYWORD parameters to CLI positional args, so this
# signature IS the command's public interface — `*` here would silently break
# existing invocations. Suppressed per-command rather than globally.
def interactive(  # noqa: PLR0917
    mode: Annotated[
        Mode | None,
        Parameter(help="Override the saved mode default."),
    ] = None,
    pick: Annotated[
        bool,
        Parameter(
            name=["--pick"],
            help="Force the project chooser even when run from a known workspace.",
        ),
    ] = False,
    memories: MemoriesFlag = False,
    subagents: SubagentsFlag = False,
    summaries: SummariesFlag = False,
    workflows: WorkflowsFlag = False,
    tool_results: ToolResultsFlag = False,
    full: FullArtifactsFlag = False,
) -> None:
    """Pick sessions, pick output dir, export. Updates the registry.

    When invoked from a directory that maps to a known Claude Code workspace, the
    project chooser is skipped and that workspace loads directly. Pass ``--pick``
    (or run from a non-workspace directory) to choose from all projects instead.
    """
    settings = load_settings()
    sync_registry(settings)
    save_settings(settings)

    entry = None if pick else _detect_cwd_project(settings)
    if entry is not None:
        console.print(
            f"[dim]workspace:[/dim] [cyan]{entry.friendly_name or entry.slug}[/cyan]"
            f"  [dim]{entry.local_path or Path.cwd()}[/dim]  "
            f"[dim](run with --pick to choose another)[/dim]"
        )
    else:
        rows = _list_projects_with_sessions(settings)
        if not rows:
            err_console.print(
                f"No Claude Code projects with sessions found under {CLAUDE_PROJECTS}."
            )
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
    # The workspace was identified by a working tree — the cwd when auto-detected,
    # the registry path when chosen from the table — so that tree scopes the
    # archive directory, exactly as `syne list` does.
    tree = Path.cwd() if not pick else (Path(entry.local_path) if entry.local_path else None)
    scope = ProjectScope(dir=slug_dir, path=tree)
    _warn_if_foreign_sessions(scope)

    session_summaries = sorted(
        (summarize_session(p) for p in scope.session_files()),
        key=lambda s: s.last_timestamp or "",
        reverse=True,
    )
    if not session_summaries:
        err_console.print("No sessions in selected project.")
        return

    _render_session_table(session_summaries)
    picked = Prompt.ask(
        "Sessions to export (e.g. '1', '1,3-5', or 'all')",
        default="all",
    )
    targets = [session_summaries[i] for i in _parse_selection(picked, len(session_summaries))]
    if not targets:
        err_console.print("No sessions selected.")
        return

    default_out = str(resolve_output_dir(settings.defaults.output_dir, entry=entry))
    out_str = Prompt.ask("Export directory", default=default_out)
    out_dir = Path(out_str).expanduser()

    opts = _opts_from_settings(settings, mode=mode)
    console.print(f"[dim]mode: {opts.mode}[/dim]")
    selection = _selection_from(
        memories=memories,
        subagents=subagents,
        summaries=summaries,
        workflows=workflows,
        tool_results=tool_results,
        full=full,
    )
    tally = ArtifactWriteResult()
    name_map = _resolve_filenames(session_summaries)
    for s in targets:
        path = _write_export(
            s,
            out_dir,
            opts,
            filename=name_map[s.session_id],
            project_slug=entry.slug,
            project_path=entry.local_path,
            selection=selection,
            project_dir=slug_dir,
            artifact_tally=tally,
        )
        console.print(f"[green]✓[/green] {s.session_id[:8]}  →  {path.name}")

    n_mem = _maybe_write_memories(selection, out_dir, slug_dir, entry)
    mark_used(settings, entry.slug)
    save_settings(settings)
    console.print(f"\n[bold]{len(targets)} session(s) exported[/bold]  →  {out_dir}")
    _report_artifacts(tally, n_mem)


# ---- non-interactive commands ----


@app.command(name="list")
def list_cmd(
    project_dir: Annotated[
        Path | None,
        Parameter(name=["--project-dir", "-p"], help="Path to ~/.claude/projects/<slug>/."),
    ] = None,
) -> None:
    """List sessions in a Claude Code project directory."""
    scope = _resolve_project_dir(project_dir)
    pd = scope.dir
    files = scope.session_files()
    _warn_if_foreign_sessions(scope)
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
    bad = [(s.session_id[:8], s.malformed_lines) for s in summaries if s.malformed_lines]
    if bad:
        detail = ", ".join(f"{sid} ({n})" for sid, n in bad)
        console.print(
            f"[yellow]⚠ undecodable JSONL lines:[/yellow] {detail}  "
            "[dim](1 transient line is normal for an active session; "
            "persistent counts suggest source corruption)[/dim]"
        )


@app.command
# Cyclopts binds POSITIONAL_OR_KEYWORD parameters to CLI positional args, so this
# signature IS the command's public interface — `*` here would silently break
# existing invocations. Suppressed per-command rather than globally.
def export(  # noqa: PLR0917
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
    memories: MemoriesFlag = False,
    subagents: SubagentsFlag = False,
    summaries: SummariesFlag = False,
    workflows: WorkflowsFlag = False,
    tool_results: ToolResultsFlag = False,
    full: FullArtifactsFlag = False,
) -> None:
    """Export a single session (by full UUID or unique prefix) to disk."""
    settings = load_settings()
    scope = _resolve_project_dir(project_dir)
    pd = scope.dir
    path = _resolve_session(pd, session_id)
    summary = summarize_session(path)
    selection = _selection_from(
        memories=memories,
        subagents=subagents,
        summaries=summaries,
        workflows=workflows,
        tool_results=tool_results,
        full=full,
    )

    # Make sure the entry exists so the {local_path} template (and memory labels) resolve.
    sync_registry(settings)
    entry = _entry_for_project_dir(settings, pd)
    if output is None:
        output = resolve_output_dir(settings.defaults.output_dir, entry=entry)

    opts = _opts_from_settings(
        settings,
        mode=mode,
        include_thinking=include_thinking,
        include_attachments=include_attachments,
        include_reminders=include_reminders,
        max_tool_chars=max_tool_chars,
    )
    tally = ArtifactWriteResult()

    if output.suffix and not output.is_dir():  # explicit filename
        events = read_session(path)
        rendered = _render_for_format(
            summary,
            events,
            opts,
            fmt=fmt,
            project_slug=pd.name,
            project_path=entry.local_path if entry else None,
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
        out_path = output
        if sidecar:
            _write_session_sidecar(summary, output, opts, fmt=fmt, project_slug=pd.name)
        _maybe_write_session_artifacts(
            summary,
            output.parent,
            output.stem,
            opts=opts,
            selection=selection,
            project_dir=pd,
            tally=tally,
        )
        memory_out = output.parent
    else:  # directory
        # Disambiguate against the same session set `export-all` writes, so one
        # session gets one filename regardless of which command exported it.
        all_summaries = [summarize_session(p) for p in scope.session_files()]
        name_map = _resolve_filenames(all_summaries, fmt=fmt)
        out_path = _write_export(
            summary,
            output,
            opts,
            filename=name_map.get(summary.session_id, _filename_for(summary, fmt=fmt)),
            fmt=fmt,
            project_slug=pd.name,
            project_path=entry.local_path if entry else None,
            write_sidecar=sidecar,
            selection=selection,
            project_dir=pd,
            artifact_tally=tally,
        )
        memory_out = output

    n_mem = _maybe_write_memories(selection, memory_out, pd, entry)
    mark_used(settings, pd.name)
    save_settings(settings)
    console.print(f"✓ Wrote [green]{out_path}[/green]")
    _report_artifacts(tally, n_mem)


@app.command(name="export-all")
# Cyclopts binds POSITIONAL_OR_KEYWORD parameters to CLI positional args, so this
# signature IS the command's public interface — `*` here would silently break
# existing invocations. Suppressed per-command rather than globally.
def export_all(  # noqa: PLR0917
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
    memories: MemoriesFlag = False,
    subagents: SubagentsFlag = False,
    summaries: SummariesFlag = False,
    workflows: WorkflowsFlag = False,
    tool_results: ToolResultsFlag = False,
    full: FullArtifactsFlag = False,
) -> None:
    """Export every session in a project directory (with optional filters)."""
    if all_projects and project_dir is not None:
        raise SystemExit("error: --project-dir and --all-projects are mutually exclusive.")
    settings = load_settings()
    sync_registry(settings)
    selection = _selection_from(
        memories=memories,
        subagents=subagents,
        summaries=summaries,
        workflows=workflows,
        tool_results=tool_results,
        full=full,
    )

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
            selection=selection,
        )
        return

    scope = _resolve_project_dir(project_dir)
    pd = scope.dir
    files = scope.session_files()
    _warn_if_foreign_sessions(scope)
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
    name_map = _resolve_filenames(all_summaries, fmt=fmt)

    tally = ArtifactWriteResult()
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
            selection=selection,
            project_dir=pd,
            artifact_tally=tally,
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

    n_mem = _maybe_write_memories(selection, output, pd, entry)
    mark_used(settings, pd.name)
    save_settings(settings)
    summary_line = f"{written} written, {skipped} skipped"
    if filtered:
        summary_line += f", {filtered} filtered out"
    console.print(f"\n[bold]{summary_line}[/bold]  →  {output}")
    _report_artifacts(tally, n_mem)


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
    selection: ArtifactSelection,
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

    eligible_entries = [
        entry
        for entry in settings.projects.values()
        if (CLAUDE_PROJECTS / entry.slug).is_dir()
        and list_session_files(CLAUDE_PROJECTS / entry.slug)
    ]
    output_names = _project_output_names(eligible_entries)
    tally = ArtifactWriteResult()
    total_memories = 0
    total_written = total_skipped = total_filtered = projects_touched = 0
    for entry in eligible_entries:
        slug_dir = CLAUDE_PROJECTS / entry.slug
        if not slug_dir.is_dir():
            continue
        files = list_session_files(slug_dir)
        if not files:
            continue

        all_summaries = [summarize_session(p) for p in files]
        candidates = [s for s in all_summaries if not (skip_empty and s.message_count == 0)]
        targets = _apply_filters(candidates, since=since, until=until, matching=matching)
        has_memories = selection.memories and bool(collect_memories(slug_dir))
        if not targets and not has_memories:
            continue

        project_label = entry.friendly_name or entry.slug.lstrip("-") or "unnamed"
        project_out = root / output_names[entry.slug]
        project_out.mkdir(parents=True, exist_ok=True)
        name_map = _resolve_filenames(all_summaries, fmt=fmt)

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
                selection=selection,
                project_dir=slug_dir,
                artifact_tally=tally,
            )
            total_written += 1

        if index and targets:
            _write_project_index(
                project_out,
                targets,
                name_map,
                project_slug=entry.slug,
                project_entry=entry,
                fmt=fmt,
                mode=opts.mode,
            )
        total_memories += _maybe_write_memories(selection, project_out, slug_dir, entry)
        projects_touched += 1
        console.print(
            f"[green]✓[/green] {project_label:30s} → {project_out}  ({len(targets)} sessions)"
        )

    console.print(
        f"\n[bold]{total_written} sessions across {projects_touched} projects[/bold] → {root}"
        f"  [dim]({total_skipped} skipped, {total_filtered} filtered)[/dim]"
    )
    _report_artifacts(tally, total_memories)


def _apply_filters(
    summaries: list[SessionSummary],
    *,
    since: str | None,
    until: str | None,
    matching: str | None,
) -> list[SessionSummary]:
    """Keep sessions whose last_timestamp falls in [since, until] AND whose
    title / first prompt matches `matching` (regex, case-sensitive by default)."""
    pattern = re.compile(matching) if matching else None
    since_dt = _parse_filter_bound(since, end_of_day=False) if since else None
    until_dt = _parse_filter_bound(until, end_of_day=True) if until else None
    out: list[SessionSummary] = []
    for s in summaries:
        ts = s.last_timestamp or s.first_timestamp or ""
        if since_dt is not None or until_dt is not None:
            parsed_ts = _parse_session_timestamp(ts)
            if parsed_ts is None:
                continue
            if since_dt is not None and parsed_ts < since_dt:
                continue
            if until_dt is not None:
                if _is_date_only(until or ""):
                    if parsed_ts >= until_dt:
                        continue
                elif parsed_ts > until_dt:
                    continue
        if pattern is not None:
            haystack = " ".join(filter(None, [s.ai_title, s.first_user_text]))
            if not pattern.search(haystack):
                continue
        out.append(s)
    return out


def _is_date_only(value: str) -> bool:
    return re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()) is not None


def _parse_filter_bound(value: str, *, end_of_day: bool) -> datetime:
    raw = value.strip()
    try:
        if _is_date_only(raw):
            day = date.fromisoformat(raw)
            start = datetime.combine(day, time.min, tzinfo=UTC)
            return start + timedelta(days=1) if end_of_day else start
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Invalid ISO 8601 date/time: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_session_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


@app.command
# Cyclopts binds POSITIONAL_OR_KEYWORD parameters to CLI positional args, so this
# signature IS the command's public interface — `*` here would silently break
# existing invocations. Suppressed per-command rather than globally.
def merge(  # noqa: PLR0917
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
    if last is not None and last <= 0:
        raise SystemExit("error: --last must be a positive integer.")

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
            id(s)
            for s in _apply_filters(
                [s for s, _ in raw_pairs], since=since, until=until, matching=matching
            )
        }
        targets = [(s, pd) for (s, pd) in raw_pairs if id(s) in kept]
    elif all_from:
        scope = _resolve_project_for_merge(all_from)
        pd = scope.dir
        _warn_if_foreign_sessions(scope)
        summaries = [summarize_session(p) for p in scope.session_files()]
        summaries = [summary for summary in summaries if summary.message_count > 0]
        filtered = _apply_filters(summaries, since=since, until=until, matching=matching)
        targets = [(s, pd) for s in filtered]
    else:
        assert session_ids is not None
        pd = _resolve_project_dir(project_dir).dir
        summaries = [summarize_session(_resolve_session(pd, sid)) for sid in session_ids]
        filtered = _apply_filters(summaries, since=since, until=until, matching=matching)
        targets = [(s, pd) for s in filtered]

    if not targets:
        raise SystemExit("error: no sessions matched (after filters).")

    targets.sort(key=lambda pair: _summary_timestamp(pair[0], prefer_first=True))

    if last is not None:
        # Take the N most-recent (by last_timestamp), then re-sort chronologically.
        targets.sort(key=lambda pair: _summary_timestamp(pair[0]), reverse=True)
        targets = targets[:last]
        targets.sort(key=lambda pair: _summary_timestamp(pair[0], prefer_first=True))

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


def _resolve_project_for_merge(name_or_path: str) -> ProjectScope:
    """Accept a slug, an absolute project path, or a friendly name from the registry.

    A working tree (absolute path, or the registry path behind a friendly name)
    scopes the archive directory; a bare slug names the directory and is taken
    at face value, like ``--project-dir``.
    """
    p = Path(name_or_path)
    if p.is_absolute() and p.is_dir():
        archive = project_dir_for_cwd(p)
        return (
            ProjectScope(dir=archive, path=p)
            if archive.is_dir()
            else ProjectScope(dir=p, path=None)
        )
    if p.name == name_or_path and name_or_path.startswith("-") and (CLAUDE_PROJECTS / p).is_dir():
        return ProjectScope(dir=CLAUDE_PROJECTS / name_or_path, path=None)
    # Try friendly_name lookup in the registry.
    settings = load_settings()
    sync_registry(settings)
    matches = [
        entry
        for entry in settings.projects.values()
        if entry.friendly_name == name_or_path and (CLAUDE_PROJECTS / entry.slug).is_dir()
    ]
    if len(matches) == 1:
        entry = matches[0]
        return ProjectScope(
            dir=CLAUDE_PROJECTS / entry.slug,
            path=Path(entry.local_path) if entry.local_path else None,
        )
    if len(matches) > 1:
        slugs = ", ".join(entry.slug for entry in matches)
        raise SystemExit(
            f"error: project name {name_or_path!r} is ambiguous; pass one of these slugs: {slugs}"
        )
    raise SystemExit(f"error: cannot resolve project {name_or_path!r}")


def _summary_timestamp(summary: SessionSummary, *, prefer_first: bool = False) -> datetime:
    primary = summary.first_timestamp if prefer_first else summary.last_timestamp
    fallback = summary.last_timestamp if prefer_first else summary.first_timestamp
    return _parse_session_timestamp(primary or fallback or "") or datetime.min.replace(tzinfo=UTC)


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
# Cyclopts binds POSITIONAL_OR_KEYWORD parameters to CLI positional args, so this
# signature IS the command's public interface — `*` here would silently break
# existing invocations. Suppressed per-command rather than globally.
def recall(  # noqa: PLR0917
    query: Annotated[
        str | None,
        Parameter(
            help="Search term. Omit for recent sessions (or the memory index with --memories)."
        ),
    ] = None,
    /,
    bundle: Annotated[
        bool,
        Parameter(
            name=["--bundle"],
            help="One aggregated self-align packet (memories + recent + hits + suggested-next).",
        ),
    ] = False,
    memories: Annotated[
        bool,
        Parameter(
            name=["--memories"], help="Operate over the curated memory layer, not transcripts."
        ),
    ] = False,
    recent: Annotated[
        bool,
        Parameter(
            name=["--recent"], help="Recent session headers (the default when no query is given)."
        ),
    ] = False,
    project_dir: Annotated[
        Path | None,
        Parameter(
            name=["--project-dir", "-p"], help="A ~/.claude/projects/<slug>/ dir (default: cwd's)."
        ),
    ] = None,
    all_projects: Annotated[
        bool,
        Parameter(
            name=["--all-projects"], help="Span every project (default: current project only)."
        ),
    ] = False,
    limit: Annotated[int, Parameter(help="Max rows to print.")] = 5,
    context_chars: Annotated[int, Parameter(name=["--context-chars"])] = 160,
    max_chars: Annotated[
        int,
        Parameter(
            name=["--max-chars"],
            help="Hard cap on output size (keeps a hook/agent from over-pulling).",
        ),
    ] = 4000,
    fmt: Annotated[
        RecallFormat, Parameter(name=["--format", "-f"], help="markdown (default) or json.")
    ] = "markdown",
    codex: Annotated[
        bool,
        Parameter(
            name=["--codex"],
            help="Include Codex CLI rollouts/handoffs in --bundle (on by default).",
        ),
    ] = True,
) -> None:
    """Print a small, capped recall (recent sessions / memories / search) to stdout.

    The CLI counterpart to the MCP recall tools, for a SessionStart hook or a
    non-MCP agent told to shell out to `syne`: headers-first, project-scoped,
    never a full transcript. `--recent` is implied when no query and no --memories.
    """
    if all_projects and project_dir is not None:
        raise SystemExit("error: --project-dir and --all-projects are mutually exclusive.")
    _ = recent  # recent is the default; the flag exists for explicitness
    settings = load_settings()
    # Same scope rule as `syne list`: the cwd defines the default scope, an
    # explicit --project-dir is taken at face value, --all-projects leaves it to
    # the registry.
    project_paths: dict[str, Path | None] = {}
    if all_projects:
        dirs = all_project_dirs(CLAUDE_PROJECTS)
    else:
        cwd = Path.cwd()
        pd = project_dir or project_dir_for_cwd(cwd)
        if not pd.is_dir():
            err_console.print("(no mnemosyne archive for this project)")
            return
        dirs = [pd]
        project_paths[pd.name] = None if project_dir is not None else cwd
    if bundle:
        print(
            build_bundle(
                dirs,
                settings,
                query_str=query,
                max_chars=max_chars,
                fmt=fmt,
                include_codex=codex,
                project_paths=project_paths,
            )
        )
        return
    print(
        build_recall(
            dirs,
            settings,
            query_str=query,
            memories=memories,
            limit=limit,
            context_chars=context_chars,
            max_chars=max_chars,
            fmt=fmt,
            project_paths=project_paths,
        )
    )


@app.command
# Cyclopts binds POSITIONAL_OR_KEYWORD parameters to CLI positional args, so this
# signature IS the command's public interface — `*` here would silently break
# existing invocations. Suppressed per-command rather than globally.
def align(  # noqa: PLR0917
    path: Annotated[
        Path | None,
        Parameter(help="Project root holding CLAUDE.md / AGENTS.md (default: cwd)."),
    ] = None,
    /,
    export: Annotated[
        bool,
        Parameter(
            name=["--export"], help="Print the directive block to stdout instead of writing files."
        ),
    ] = False,
    remove: Annotated[
        bool,
        Parameter(
            name=["--remove"], help="Remove the mnemosyne region from the instruction files."
        ),
    ] = False,
    claude_only: Annotated[
        bool, Parameter(name=["--claude-only"], help="Only manage CLAUDE.md.")
    ] = False,
    agents_only: Annotated[
        bool, Parameter(name=["--agents-only"], help="Only manage AGENTS.md.")
    ] = False,
    force: Annotated[
        bool,
        Parameter(
            name=["--force"], help="Write even if mnemosyne isn't detected for this project."
        ),
    ] = False,
) -> None:
    """Write the mnemosyne self-alignment directive into CLAUDE.md and AGENTS.md.

    Idempotent and marker-scoped — only the `<!-- mnemosyne:begin -->`…`<!-- mnemosyne:end -->`
    span is ever touched. AGENTS.md is the cross-tool standard (Codex/opencode/Cursor/
    Copilot/Windsurf/Gemini); CLAUDE.md is for Claude Code, which does not read AGENTS.md.
    Use `--export` to print the block for manual placement; `--remove` to strip it.
    """
    if export:
        print(render_block())
        return

    local = (path or Path.cwd()).resolve()
    if not remove and not local.is_dir():
        raise SystemExit(f"error: project root not found: {local}")
    if claude_only and agents_only:
        raise SystemExit("error: --claude-only and --agents-only are mutually exclusive.")
    files = target_files(local, claude=not agents_only, agents=not claude_only)

    if not remove and not force and not is_available(local):
        err_console.print(
            f"no archive to align against for {local}\n"
            "  (no sessions, no .mnemosyne-exports/, no curated memories for this project).\n"
            "  the directive would claim an archive that doesn't exist — run a session or\n"
            "  an export first, or pass --force to pre-wire a new project."
        )
        return

    labels = {
        "created": ("green", "✓ created "),
        "written": ("green", "✓ updated "),
        "removed": ("green", "✓ removed "),
        "unchanged": ("dim", "· unchanged"),
        "absent": ("dim", "· absent   "),
    }
    for f in files:
        outcome = apply_to_file(f, remove=remove)
        style, label = labels[outcome]
        console.print(f"[{style}]{label}[/{style}] {f}")


@app.command
def drift(
    path: Annotated[
        Path | None,
        Parameter(help="Project working tree to verify against (default: cwd)."),
    ] = None,
    /,
    fmt: Annotated[
        RecallFormat, Parameter(name=["--format", "-f"], help="markdown (default) or json.")
    ] = "markdown",
) -> None:
    """Mechanically verify curated memories against the live repository.

    Checks every memory's cited file paths, `path:line` anchors, and `[[links]]`
    against the working tree. Deterministic, but a finding is a *review signal*,
    not proof: an unresolved reference means the citation no longer resolves —
    the memory's underlying claim may still hold (the file may have moved or been
    renamed). The computed counterpart of the directive's "treat recall as dated
    evidence" rule.
    """
    root = (path or Path.cwd()).resolve()
    project_dir = project_dir_for_cwd(root)
    if not project_dir.is_dir():
        raise SystemExit(f"error: no Claude Code project archive for {root} ({project_dir})")
    report = check_drift(project_dir, root)
    if fmt == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    # markup=False: memory links are [[name]], which rich would eat as style tags.
    console.print(render_drift_markdown(report), markup=False)


@app.command(name="codex-list")
def codex_list(
    cwd: Annotated[
        Path | None,
        Parameter(name=["--cwd"], help="Working tree to filter by (default: current dir)."),
    ] = None,
    all_sessions: Annotated[
        bool,
        Parameter(name=["--all"], help="List every Codex rollout regardless of project."),
    ] = False,
    limit: Annotated[int, Parameter(help="Max rollouts to show.")] = 20,
) -> None:
    """List OpenAI Codex CLI sessions (rollouts) recorded for this project.

    Reads `~/.codex/sessions` first-line headers plus the thread-name index —
    cheap even on large archives. Use `syne codex-export` to render one.
    """
    if all_sessions and cwd is not None:
        raise SystemExit("error: --cwd and --all are mutually exclusive.")
    if not codex_store.codex_available():
        err_console.print(f"No Codex archive found at {codex_store.CODEX_HOME}")
        return
    target = None if all_sessions else (cwd or Path.cwd())
    metas = codex_store.codex_sessions_for(target, limit=limit)
    if not metas:
        err_console.print("No Codex sessions match.")
        return
    table = Table(title="Codex sessions", header_style="bold")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Started", no_wrap=True)
    table.add_column("Title / thread")
    if all_sessions:
        table.add_column("cwd", style="dim")
    for m in metas:
        row = [m.session_id[:8], _fmt_short_ts(m.timestamp), _short(m.thread_name, 70)]
        if all_sessions:
            row.append(_short(m.cwd, 50))
        table.add_row(*row)
    console.print(table)
    console.print(f"\n[dim]{len(metas)} rollouts · archive: {codex_store.CODEX_HOME}[/dim]")


@app.command(name="codex-export")
# Cyclopts binds POSITIONAL_OR_KEYWORD parameters to CLI positional args, so this
# signature IS the command's public interface — `*` here would silently break
# existing invocations. Suppressed per-command rather than globally.
def codex_export(  # noqa: PLR0917
    session_id: str,
    /,
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
    max_tool_chars: int | None = None,
    sidecar: bool = True,
) -> None:
    """Export one Codex rollout (by session UUID or unique prefix) to disk.

    Renders through the same pipeline as Claude sessions, so modes, formats, noise
    scrubbing and the `.meta.json` sidecar behave identically.
    """
    settings = load_settings()
    try:
        path = codex_store.resolve_codex_session(session_id)
    except (FileNotFoundError, ValueError) as exc:
        raise SystemExit(f"error: {exc}") from exc
    summary = codex_store.summarize_codex_session(path, codex_store.load_session_index())
    events = codex_store.read_codex_session(path)
    opts = _opts_from_settings(settings, mode=mode, max_tool_chars=max_tool_chars)
    rendered = _render_for_format(summary, events, opts, fmt=fmt, project_slug="codex")

    if output is not None and (output.suffix and not output.is_dir()):
        out_path = output
    else:
        out_dir = output or (resolve_output_dir(settings.defaults.output_dir, entry=None) / "codex")
        out_path = out_dir / _filename_for(summary, fmt=fmt)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(rendered, encoding="utf-8")
    if sidecar:
        _write_session_sidecar(summary, out_path, opts, fmt=fmt, project_slug="codex")
    console.print(f"✓ Wrote [green]{out_path}[/green]")


@app.command
def mcp() -> None:
    """Run the MCP server on stdio (for `syne install`-managed Claude Code plugin)."""
    # Lazy import: pulls in mcp/anyio/uvicorn which we don't need for the
    # other subcommands.
    from mnemosyne.mcp_server import run  # noqa: PLC0415

    run()


@app.command
def install(
    install_path: Annotated[
        Path | None,
        Parameter(
            name=["--install-path"],
            help=("Override default plugin install location (~/.claude/plugins/mnemosyne)."),
        ),
    ] = None,
) -> None:
    """Install the Claude Code plugin sidecar (skills, slash commands, MCP server).

    Copies bundled templates into ~/.claude/plugins/mnemosyne/ and
    registers a local-directory marketplace so Claude Code's `/plugin install`
    flow can find it. Re-running this command updates the plugin in place.
    """
    from mnemosyne.installer import (  # noqa: PLC0415
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
    from mnemosyne.installer import (  # noqa: PLC0415
        DEFAULT_INSTALL_PATH,
        uninstall_plugin,
    )

    target = install_path or DEFAULT_INSTALL_PATH
    removed = uninstall_plugin(target)
    if removed:
        console.print(f"[green]✓ uninstalled[/green] {target}")
    else:
        console.print(f"[dim](nothing to remove at {target})[/dim]")

    # Strip the self-alignment region from THIS project's instruction files, then
    # report (never touch) other registered projects that still carry one — a
    # directive pointing at a CLI that may be about to disappear shouldn't be
    # silently orphaned.
    for f in target_files(Path.cwd()):
        if apply_to_file(f, remove=True) == "removed":
            console.print(f"[green]✓ removed align region[/green] {f}")

    settings = load_settings()
    cwd = Path.cwd().resolve()
    other_roots = sorted(
        {
            Path(entry.local_path)
            for entry in settings.projects.values()
            if entry.local_path
            and Path(entry.local_path).is_dir()
            and Path(entry.local_path).resolve() != cwd
        }
    )
    remaining = files_with_region(other_roots)
    if remaining:
        console.print("\n[yellow]Managed alignment regions remain in other projects:[/yellow]")
        for f in remaining:
            console.print(f"  {f}")
        console.print("[dim]Remove each with:[/dim] syne align <project-root> --remove")


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
