"""Tests for memory.py — frontmatter parsing, collection, and the link graph."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mnemosyne.memory import (
    _parse_frontmatter,
    build_memory_index,
    collect_memories,
    parse_memory,
    render_memories_markdown,
)

if TYPE_CHECKING:
    from pathlib import Path

# A memory whose description carries escaped quotes and whose body references the
# same sibling twice plus a not-yet-written one.
_MEM = """---
name: clerk-org-claims
description: "Backend reads RESERVED claims, not \\"custom\\" ones"
metadata:
  node_type: memory
  type: project
  originSessionId: 01522f46-c999
---

Body prose referencing [[product-roadmap]] and [[product-roadmap]] again.

More body with a [[dangling-link]].
"""


def _write_memory(mem_dir: Path, name: str, text: str) -> None:
    (mem_dir / f"{name}.md").write_text(text, encoding="utf-8")


def test_parse_frontmatter_extracts_flat_and_nested_fields() -> None:
    front, body = _parse_frontmatter(_MEM)
    assert front["name"] == "clerk-org-claims"
    assert front["description"] == 'Backend reads RESERVED claims, not "custom" ones'
    assert front["metadata"]["type"] == "project"
    assert front["metadata"]["originSessionId"] == "01522f46-c999"
    assert body.startswith("Body prose")


def test_parse_frontmatter_no_header_returns_whole_body() -> None:
    front, body = _parse_frontmatter("no frontmatter here\njust text\n")
    assert front == {}
    assert body == "no frontmatter here\njust text\n"


def test_parse_memory_dedupes_links_in_order(tmp_path: Path) -> None:
    p = tmp_path / "m.md"
    p.write_text(_MEM, encoding="utf-8")
    m = parse_memory(p)
    assert m.name == "clerk-org-claims"
    assert m.type == "project"
    assert m.origin_session_id == "01522f46-c999"
    # de-duplicated, first-seen order preserved
    assert m.links == ["product-roadmap", "dangling-link"]


def test_parse_memory_without_frontmatter_falls_back_to_stem(tmp_path: Path) -> None:
    p = tmp_path / "loose-note.md"
    p.write_text("just a body, no frontmatter\n", encoding="utf-8")
    m = parse_memory(p)
    assert m.name == "loose-note"
    assert m.description is None
    assert m.type is None
    assert "just a body" in m.body


def test_collect_memories_skips_the_index_file(tmp_path: Path) -> None:
    mem = tmp_path / "-proj" / "memory"
    mem.mkdir(parents=True)
    _write_memory(mem, "a", "---\nname: a\n---\nbody a\n")
    _write_memory(mem, "b", "---\nname: b\n---\nbody b links [[a]]\n")
    (mem / "MEMORY.md").write_text("- [a](a.md) — hook\n", encoding="utf-8")

    coll = collect_memories(tmp_path / "-proj")
    assert {m.name for m in coll.memories} == {"a", "b"}
    assert coll.index_path is not None
    assert coll.index_path.name == "MEMORY.md"
    assert bool(coll) is True


def test_collect_memories_empty_when_no_memory_dir(tmp_path: Path) -> None:
    coll = collect_memories(tmp_path / "-noproj")
    assert not coll
    assert coll.memories == []
    assert coll.index_path is None


def test_build_index_splits_resolved_and_dangling_links(tmp_path: Path) -> None:
    mem = tmp_path / "-proj" / "memory"
    mem.mkdir(parents=True)
    _write_memory(mem, "a", "---\nname: a\n---\nbody\n")
    _write_memory(mem, "b", "---\nname: b\n---\nlinks [[a]] and [[ghost]]\n")

    idx = build_memory_index(collect_memories(tmp_path / "-proj"), project_path="/x")
    assert idx["memory_count"] == 2
    assert idx["project_path"] == "/x"
    b = next(e for e in idx["memories"] if e["name"] == "b")
    assert b["links"]["resolved"] == ["a"]
    assert b["links"]["dangling"] == ["ghost"]


def test_render_memories_markdown_has_headings_and_bodies(tmp_path: Path) -> None:
    mem = tmp_path / "-proj" / "memory"
    mem.mkdir(parents=True)
    _write_memory(
        mem,
        "roadmap",
        '---\nname: roadmap\ndescription: "the plan"\nmetadata:\n  type: project\n---\nship it\n',
    )
    md = render_memories_markdown(collect_memories(tmp_path / "-proj"), project_label="proj")
    assert "# Memories — proj" in md
    assert "## roadmap" in md
    assert "the plan" in md
    assert "ship it" in md
