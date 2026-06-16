"""Write discovered artifacts to a clean export tree.

The discovery layer ([`artifacts`][mnemosyne.artifacts], [`memory`][mnemosyne.memory])
finds what Claude Code stored; this module renders it to disk next to the main
transcript export, reusing the *same* parser → clean → render pipeline so subagent
and workflow transcripts get the identical noise-reduction the top-level
transcript gets. Layout produced for one exported session ``<base>``::

    <base>.md                      # main transcript (written by cli)
    <base>.summary.md              # session-memory handoff digest
    <base>.subagents/
      <type>-<desc>-<id8>.md       # each subagent, rendered in the chosen mode
      index.json                   # agent_type / description / tool_use_id map
    <base>.workflows/
      scripts/<name>.js            # orchestration scripts (copied verbatim)
      <wf_id>/<agent>.md           # orchestrated subagents, rendered
      <wf_id>/journal-summary.json # rolled-up run journal
    <base>.tool-results/<name>     # externalised tool output, scrubbed

Project-scoped memories are written once per project into ``<out_dir>/memory/``.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mnemosyne.artifacts import summarize_journal
from mnemosyne.clean import normalize_whitespace, scrub_tool_output
from mnemosyne.memory import build_memory_index, render_memories_markdown
from mnemosyne.parser import Message, read_session
from mnemosyne.render import RenderOptions, render_markdown

if TYPE_CHECKING:
    from pathlib import Path

    from mnemosyne.artifacts import ArtifactSelection, SessionArtifacts, SubagentRef
    from mnemosyne.memory import MemoryCollection
    from mnemosyne.parser import Event

_SLUG_KEEP = re.compile(r"[^\w\s-]+", re.UNICODE)
_SLUG_SQUASH = re.compile(r"[-\s_]+")


def slugify(text: str, max_len: int = 80) -> str:
    """Title → ``fix-godot-project-initialization``.

    Drops punctuation, collapses whitespace/underscores/hyphens to a single
    ``-``, lower-cases, trims to ``max_len``. Returns ``""`` if nothing usable
    remains. Shared by the CLI's session filenames and artifact filenames so the
    two never drift.
    """
    cleaned = _SLUG_KEEP.sub(" ", text).lower().strip()
    slug = _SLUG_SQUASH.sub("-", cleaned).strip("-")
    if len(slug) > max_len:
        slug = slug[:max_len].rstrip("-")
    return slug


@dataclass(slots=True)
class ArtifactWriteResult:
    """Per-session counts of what was written, for CLI reporting."""

    summaries: int = 0
    subagents: int = 0
    workflow_agents: int = 0
    scripts: int = 0
    tool_results: int = 0

    @property
    def total(self) -> int:
        return (
            self.summaries
            + self.subagents
            + self.workflow_agents
            + self.scripts
            + self.tool_results
        )

    def merge(self, other: ArtifactWriteResult) -> None:
        self.summaries += other.summaries
        self.subagents += other.subagents
        self.workflow_agents += other.workflow_agents
        self.scripts += other.scripts
        self.tool_results += other.tool_results


def _count_roles(events: list[Event]) -> tuple[int, int]:
    user = sum(1 for e in events if isinstance(e, Message) and e.role == "user")
    asst = sum(1 for e in events if isinstance(e, Message) and e.role == "assistant")
    return user, asst


def _subagent_filename(ref: SubagentRef) -> str:
    parts = [slugify(ref.agent_type or "subagent", 40)]
    if ref.description:
        parts.append(slugify(ref.description, 50))
    parts.append(ref.agent_id[:8])
    return "-".join(p for p in parts if p) + ".md"


def _render_subagent(ref: SubagentRef, opts: RenderOptions) -> tuple[str, dict[str, Any], str]:
    """Render one subagent transcript; return (markdown, index_entry, filename)."""
    events = read_session(ref.path)
    user, asst = _count_roles(events)
    label = ref.agent_type or "subagent"
    desc = f" — {ref.description}" if ref.description else ""
    origin = f"spawned by tool_use `{ref.tool_use_id}`" if ref.tool_use_id else ""
    wf = f" · workflow `{ref.workflow_id}`" if ref.workflow_id else ""
    title = f"Subagent: {label}{desc}  \n_agent `{ref.agent_id}`{wf}_"
    body = render_markdown(events, title=title, opts=opts, session_id=ref.agent_id)
    if origin:
        body = f"{body.rstrip()}\n\n---\n\n_{origin}_\n"
    filename = _subagent_filename(ref)
    entry = {
        "agent_id": ref.agent_id,
        "agent_type": ref.agent_type,
        "description": ref.description,
        "tool_use_id": ref.tool_use_id,
        "workflow_id": ref.workflow_id,
        "file": filename,
        "user_count": user,
        "assistant_count": asst,
    }
    return body, entry, filename


def _write_unique(out_dir: Path, filename: str, content: str, used: set[str]) -> None:
    """Write ``content`` to ``out_dir/filename``, disambiguating name collisions."""
    name = filename
    if name in used:
        stem, dot, ext = filename.rpartition(".")
        i = 2
        while name in used:
            name = f"{stem}-{i}.{ext}" if dot else f"{filename}-{i}"
            i += 1
    used.add(name)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / name).write_text(content, encoding="utf-8")


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _write_summaries(out_dir: Path, base: str, summaries: list[Path]) -> int:
    """Copy session-memory handoff digests through, whitespace-normalised."""
    written = 0
    for i, src in enumerate(summaries):
        try:
            text = normalize_whitespace(src.read_text(encoding="utf-8")) + "\n"
        except OSError:
            continue
        # The common case is a single summary.md → `<base>.summary.md`.
        name = f"{base}.summary.md" if len(summaries) == 1 else f"{base}.summary-{i + 1}.md"
        (out_dir / name).write_text(text, encoding="utf-8")
        written += 1
    return written


def _write_subagents(out_dir: Path, base: str, refs: list[SubagentRef], opts: RenderOptions) -> int:
    sub_dir = out_dir / f"{base}.subagents"
    used: set[str] = set()
    entries: list[dict[str, Any]] = []
    for ref in refs:
        try:
            body, entry, filename = _render_subagent(ref, opts)
        except OSError:
            continue
        # _write_unique may rename on collision; reflect the final name in the index.
        before = set(used)
        _write_unique(sub_dir, filename, body, used)
        entry["file"] = next(iter(used - before), filename)
        entries.append(entry)
    if entries:
        _write_json(
            sub_dir / "index.json",
            {"version": 1, "subagent_count": len(entries), "subagents": entries},
        )
    return len(entries)


def _write_workflows(
    out_dir: Path,
    base: str,
    artifacts: SessionArtifacts,
    opts: RenderOptions,
) -> tuple[int, int]:
    """Write workflow scripts + per-run journals and orchestrated agents.

    Returns ``(scripts_written, workflow_agents_written)``.
    """
    wf_root = out_dir / f"{base}.workflows"
    scripts = 0
    for script in artifacts.scripts:
        dest = wf_root / "scripts" / script.path.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copyfile(script.path, dest)  # verbatim: it is source code
            scripts += 1
        except OSError:
            continue

    agents = 0
    for run in artifacts.workflow_runs:
        run_dir = wf_root / run.workflow_id
        used: set[str] = set()
        entries: list[dict[str, Any]] = []
        for ref in run.agents:
            try:
                body, entry, filename = _render_subagent(ref, opts)
            except OSError:
                continue
            before = set(used)
            _write_unique(run_dir, filename, body, used)
            entry["file"] = next(iter(used - before), filename)
            entries.append(entry)
            agents += 1
        if run.journal_path is not None:
            _write_json(run_dir / "journal-summary.json", summarize_journal(run.journal_path))
        if entries:
            _write_json(
                run_dir / "index.json",
                {"version": 1, "workflow_id": run.workflow_id, "agents": entries},
            )
    return scripts, agents


def _write_tool_results(out_dir: Path, base: str, files: list[Path]) -> int:
    """Copy externalised tool outputs through the deterministic output scrub."""
    tr_dir = out_dir / f"{base}.tool-results"
    written = 0
    for src in files:
        try:
            raw = src.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            tr_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, tr_dir / src.name)  # not text — copy raw bytes
            written += 1
            continue
        except OSError:
            continue
        tr_dir.mkdir(parents=True, exist_ok=True)
        (tr_dir / src.name).write_text(scrub_tool_output(raw) + "\n", encoding="utf-8")
        written += 1
    return written


def write_session_artifacts(
    artifacts: SessionArtifacts,
    out_dir: Path,
    base: str,
    *,
    opts: RenderOptions,
    selection: ArtifactSelection,
) -> ArtifactWriteResult:
    """Write the selected per-session artifact categories beside ``<base>.md``.

    ``selection`` is an :class:`~mnemosyne.artifacts.ArtifactSelection`. Subagent
    and workflow transcripts are rendered with ``opts`` — the same mode/cleaning as
    the main transcript — so the whole bundle reads consistently.
    """
    result = ArtifactWriteResult()
    if not artifacts:
        return result
    if selection.summaries and artifacts.summaries:
        result.summaries = _write_summaries(out_dir, base, artifacts.summaries)
    if selection.subagents and artifacts.subagents:
        result.subagents = _write_subagents(out_dir, base, artifacts.subagents, opts)
    if selection.workflows and (artifacts.scripts or artifacts.workflow_runs):
        result.scripts, result.workflow_agents = _write_workflows(out_dir, base, artifacts, opts)
    if selection.tool_results and artifacts.tool_results:
        result.tool_results = _write_tool_results(out_dir, base, artifacts.tool_results)
    return result


def write_project_memories(
    coll: MemoryCollection,
    out_dir: Path,
    *,
    project_label: str | None = None,
    project_path: str | None = None,
) -> Path | None:
    """Write a project's curated memory layer into ``<out_dir>/memory/``.

    Emits per-file copies (whitespace-normalised), the original ``MEMORY.md`` index
    if present, a consolidated ``memories.md``, and a machine-readable
    ``memories.json`` resolving the ``[[link]]`` graph. Returns the memory directory,
    or ``None`` when the project has no memories.
    """
    if not coll:
        return None
    mem_out = out_dir / "memory"
    mem_out.mkdir(parents=True, exist_ok=True)
    for m in coll.memories:
        front = m.path.read_text(encoding="utf-8")
        (mem_out / m.path.name).write_text(normalize_whitespace(front) + "\n", encoding="utf-8")
    if coll.index_path is not None:
        text = normalize_whitespace(coll.index_path.read_text(encoding="utf-8")) + "\n"
        (mem_out / "MEMORY.md").write_text(text, encoding="utf-8")
    (mem_out / "memories.md").write_text(
        render_memories_markdown(coll, project_label=project_label), encoding="utf-8"
    )
    _write_json(mem_out / "memories.json", build_memory_index(coll, project_path=project_path))
    return mem_out
