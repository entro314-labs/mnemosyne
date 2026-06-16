"""Collect the curated memory layer Claude Code writes per project.

Beyond the append-only session transcripts, Claude Code maintains a
hand-curated knowledge base under ``~/.claude/projects/<slug>/memory/``:

    memory/
      MEMORY.md          # human index — one ``- [Title](file.md) — hook`` line per memory
      <name>.md          # one fact per file: YAML frontmatter + markdown body

Each memory file carries frontmatter like::

    ---
    name: clerk-org-claims-reserved-not-custom
    description: "Backend reads RESERVED Clerk org claims, not custom ones"
    metadata:
      node_type: memory
      type: project
      originSessionId: 01522f46-c999-4f92-b48e-aa01bce100e1
    ---

    Body prose. Links to sibling memories with [[their-name]].

This is the densest, most reusable knowledge in the whole archive — roadmaps,
architecture decisions, gotchas, user preferences — and it survives across
sessions. Unlike transcripts it is already clean, so we copy it through and
build a machine-readable index (resolving the ``[[link]]`` graph) rather than
running it through the transcript render pipeline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mnemosyne.clean import normalize_whitespace

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

# A wiki-style link to a sibling memory: [[some-other-memory-name]].
_LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
# Recognised memory categories (mirrors Claude Code's own taxonomy).
MEMORY_TYPES = ("user", "feedback", "project", "reference")


@dataclass(slots=True)
class Memory:
    """One curated fact: frontmatter fields plus its markdown body."""

    name: str
    description: str | None
    type: str | None  # user | feedback | project | reference
    origin_session_id: str | None
    body: str
    links: list[str]  # names referenced via [[link]] in the body
    path: Path


@dataclass(slots=True)
class MemoryCollection:
    """Every memory for one project, plus the human index if present."""

    project_slug: str
    memory_dir: Path
    index_path: Path | None  # memory/MEMORY.md, when it exists
    memories: list[Memory]

    def __bool__(self) -> bool:
        return bool(self.memories)


def memory_dir_for(project_dir: Path) -> Path:
    """The ``memory/`` directory inside a ``~/.claude/projects/<slug>/`` dir."""
    return project_dir / "memory"


def _unquote(value: str) -> str:
    """Strip matching surrounding quotes and unescape ``\\"`` / ``\\\\``."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        inner = value[1:-1]
        return inner.replace('\\"', '"').replace("\\'", "'").replace("\\\\", "\\")
    return value


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a ``---``-delimited YAML-ish frontmatter header from its body.

    Handles the regular shape Claude Code emits: flat ``key: value`` pairs plus a
    single nested ``metadata:`` block indented by two spaces. This is deliberately
    not a general YAML parser — the frontmatter is machine-written and uniform, so
    a focused line scanner avoids a dependency. On anything unexpected it returns
    ``({}, original_text)`` so a malformed file degrades to a bodied, unkeyed memory.
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines(keepends=True)
    # First line is the opening fence; find the closing one.
    close = next((i for i in range(1, len(lines)) if lines[i].rstrip() == "---"), None)
    if close is None:
        return {}, text

    front: dict[str, Any] = {}
    nested: dict[str, str] | None = None
    nested_key: str | None = None
    for raw in lines[1:close]:
        line = raw.rstrip("\n")
        if not line.strip():
            continue
        indented = line[0] in {" ", "\t"}
        if indented and nested is not None and ":" in line:
            k, _, v = line.strip().partition(":")
            nested[k.strip()] = _unquote(v)
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        if not value.strip():
            # Opens a nested block (e.g. ``metadata:``).
            nested = {}
            nested_key = key
            front[key] = nested
        else:
            front[key] = _unquote(value)
            nested = None
            nested_key = None
    _ = nested_key  # retained for readability of the scan; not needed afterwards

    body = "".join(lines[close + 1 :]).lstrip("\n")
    return front, body


def _extract_links(body: str) -> list[str]:
    """Return the de-duplicated ``[[name]]`` references in document order."""
    seen: dict[str, None] = {}
    for m in _LINK_RE.finditer(body):
        seen.setdefault(m.group(1).strip(), None)
    return list(seen)


def parse_memory(path: Path) -> Memory:
    """Parse one ``memory/<name>.md`` file into a :class:`Memory`."""
    text = path.read_text(encoding="utf-8")
    front, body = _parse_frontmatter(text)
    metadata = front.get("metadata") if isinstance(front.get("metadata"), dict) else {}
    name = str(front.get("name") or path.stem)
    description = front.get("description")
    body = normalize_whitespace(body)
    return Memory(
        name=name,
        description=str(description) if description is not None else None,
        type=metadata.get("type"),
        origin_session_id=metadata.get("originSessionId") or metadata.get("origin_session_id"),
        body=body,
        links=_extract_links(body),
        path=path,
    )


def iter_memory_files(project_dir: Path) -> Iterator[Path]:
    """Yield ``memory/*.md`` paths (excluding the ``MEMORY.md`` index), sorted."""
    mem_dir = memory_dir_for(project_dir)
    if not mem_dir.is_dir():
        return
    for p in sorted(mem_dir.glob("*.md")):
        if p.name != "MEMORY.md":
            yield p


def collect_memories(project_dir: Path) -> MemoryCollection:
    """Gather every memory for a project directory.

    ``project_dir`` is a ``~/.claude/projects/<slug>/`` directory. Returns an empty
    collection (falsy) when the project has no ``memory/`` directory.
    """
    mem_dir = memory_dir_for(project_dir)
    index = mem_dir / "MEMORY.md"
    memories = [parse_memory(p) for p in iter_memory_files(project_dir)]
    return MemoryCollection(
        project_slug=project_dir.name,
        memory_dir=mem_dir,
        index_path=index if index.is_file() else None,
        memories=memories,
    )


def build_memory_index(
    coll: MemoryCollection, *, project_path: str | None = None
) -> dict[str, Any]:
    """A machine-readable index over a collection, resolving the link graph.

    Each entry's ``links`` are split into ``resolved`` (names that exist in this
    collection) and ``dangling`` (referenced but not yet written) so downstream
    consumers — embedding pipelines, graph viewers — get the edges for free.
    """
    known = {m.name for m in coll.memories}
    entries: list[dict[str, Any]] = []
    for m in coll.memories:
        resolved = [link for link in m.links if link in known]
        dangling = [link for link in m.links if link not in known]
        entries.append(
            {
                "name": m.name,
                "type": m.type,
                "description": m.description,
                "origin_session_id": m.origin_session_id,
                "file": m.path.name,
                "char_count": len(m.body),
                "links": {"resolved": resolved, "dangling": dangling},
            }
        )
    return {
        "version": 1,
        "project_slug": coll.project_slug,
        "project_path": project_path,
        "memory_count": len(entries),
        "memories": entries,
    }


def render_memories_markdown(
    coll: MemoryCollection,
    *,
    project_label: str | None = None,
) -> str:
    """Render a single consolidated document containing every memory.

    A convenience companion to the per-file copies — one greppable file with all
    of a project's curated knowledge, each fact under its own heading.
    """
    label = project_label or coll.project_slug.lstrip("-") or coll.project_slug
    header = (
        f"# Memories — {label}\n\n"
        f"_{len(coll.memories)} curated memor{'y' if len(coll.memories) == 1 else 'ies'} "
        f"written by Claude Code. Source: `{coll.memory_dir}`._"
    )
    sections = [header]
    for m in coll.memories:
        meta_bits = [
            b
            for b in (m.type, f"origin `{m.origin_session_id}`" if m.origin_session_id else None)
            if b
        ]
        subtitle = f"  \n_{' · '.join(meta_bits)}_" if meta_bits else ""
        parts = [f"## {m.name}{subtitle}"]
        if m.description:
            parts.append(f"> {m.description}")
        if m.body:
            parts.append(m.body)
        if m.links:
            parts.append("_links:_ " + ", ".join(f"`[[{link}]]`" for link in m.links))
        sections.append("\n\n".join(parts))
    return normalize_whitespace("\n\n---\n\n".join(sections)) + "\n"
