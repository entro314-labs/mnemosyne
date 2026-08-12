"""Tests for align.py — the idempotent marked-region writer + availability gate."""

from __future__ import annotations

from pathlib import Path

import pytest

from mnemosyne.align import (
    BEGIN,
    END,
    apply_to_file,
    files_with_region,
    has_region,
    is_available,
    render_block,
    strip_region,
    target_files,
    upsert_region,
)
from mnemosyne.parser import project_slug

# ---- render_block ----


def test_render_block_has_markers_and_guardrails() -> None:
    block = render_block()
    assert block.startswith(BEGIN)
    assert block.rstrip().endswith(END)
    low = block.lower()
    # The non-negotiable safeguards must be present in the always-on directive.
    for needle in (
        "do not auto-load",
        "dated evidence",
        "curated memories",
        "this project",
        "fabricate",
    ):
        assert needle in low


# ---- upsert / strip ----


def test_upsert_appends_when_absent() -> None:
    out = upsert_region("# My project\n\nstuff\n", render_block())
    assert "# My project" in out
    assert has_region(out)
    assert out.count(BEGIN) == 1


def test_upsert_into_empty_text() -> None:
    out = upsert_region("", render_block())
    assert out.startswith(BEGIN)
    assert has_region(out)


def test_upsert_is_idempotent() -> None:
    once = upsert_region("# x\n", render_block())
    twice = upsert_region(once, render_block())
    assert once == twice
    assert once.count(BEGIN) == 1


def test_strip_restores_surrounding_content() -> None:
    base = "# My project\n\nbody text\n"
    withr = upsert_region(base, render_block())
    stripped = strip_region(withr)
    assert not has_region(stripped)
    assert "# My project" in stripped
    assert "body text" in stripped


def test_strip_noop_without_region() -> None:
    assert strip_region("# nothing here\n") == "# nothing here\n"


@pytest.mark.parametrize(
    "malformed",
    [
        f"# Mine\n\n{BEGIN}\nmissing end\n",
        f"# Mine\n\n{END}\n",
        f"{BEGIN}\nx\n{END}\n{BEGIN}\ny\n{END}\n",
    ],
)
def test_malformed_regions_are_rejected_without_rewriting(malformed: str, tmp_path: Path) -> None:
    f = tmp_path / "AGENTS.md"
    f.write_text(malformed, encoding="utf-8")
    with pytest.raises(ValueError, match="Malformed mnemosyne region"):
        apply_to_file(f)
    assert f.read_text(encoding="utf-8") == malformed


def test_upsert_does_not_strip_user_whitespace() -> None:
    original = "# Mine\n\nbody\n\n\n"
    updated = upsert_region(original, render_block())
    assert updated.startswith(original)


# ---- apply_to_file ----


def test_apply_lifecycle(tmp_path: Path) -> None:
    f = tmp_path / "AGENTS.md"
    assert apply_to_file(f) == "created"
    assert has_region(f.read_text(encoding="utf-8"))
    assert apply_to_file(f) == "unchanged"
    assert apply_to_file(f, remove=True) == "removed"
    assert not has_region(f.read_text(encoding="utf-8"))
    assert apply_to_file(f, remove=True) == "absent"


def test_apply_preserves_user_content(tmp_path: Path) -> None:
    f = tmp_path / "CLAUDE.md"
    f.write_text("# Mine\n\nmy rules\n", encoding="utf-8")
    assert apply_to_file(f) == "written"
    text = f.read_text(encoding="utf-8")
    assert "# Mine" in text and "my rules" in text and has_region(text)
    apply_to_file(f, remove=True)
    restored = f.read_text(encoding="utf-8")
    assert "# Mine" in restored and "my rules" in restored
    assert not has_region(restored)


# ---- target_files ----


def test_target_files_defaults_to_both() -> None:
    p = Path("/proj")
    assert [f.name for f in target_files(p)] == ["CLAUDE.md", "AGENTS.md"]
    assert [f.name for f in target_files(p, agents=False)] == ["CLAUDE.md"]
    assert [f.name for f in target_files(p, claude=False)] == ["AGENTS.md"]


# ---- is_available gate ----


def test_gate_false_when_nothing_present(tmp_path: Path) -> None:
    local = tmp_path / "proj"
    local.mkdir()
    home = tmp_path / "claude"
    (home / "projects").mkdir(parents=True)
    assert is_available(local, claude_home=home) is False


def test_gate_true_via_exports(tmp_path: Path) -> None:
    local = tmp_path / "proj"
    local.mkdir()
    exports = local / ".mnemosyne-exports"
    exports.mkdir()
    (exports / "session.md").write_text("# transcript\n", encoding="utf-8")
    home = tmp_path / "claude"
    (home / "projects").mkdir(parents=True)
    assert is_available(local, claude_home=home) is True


def test_gate_false_for_empty_export_dir(tmp_path: Path) -> None:
    """`export-all` creates the directory before filtering; empty is not an archive."""
    local = tmp_path / "proj"
    local.mkdir()
    (local / ".mnemosyne-exports").mkdir()
    home = tmp_path / "claude"
    (home / "projects").mkdir(parents=True)
    assert is_available(local, claude_home=home) is False


def test_gate_true_via_sessions(tmp_path: Path) -> None:
    local = tmp_path / "proj"
    local.mkdir()
    home = tmp_path / "claude"
    slug = project_slug(local)
    slug_dir = home / "projects" / slug
    slug_dir.mkdir(parents=True)
    (slug_dir / "s.jsonl").write_text("{}\n", encoding="utf-8")
    assert is_available(local, claude_home=home) is True


def test_gate_false_when_only_plugin_installed(tmp_path: Path) -> None:
    """A global plugin proves the tools exist, not that THIS project has an archive."""
    local = tmp_path / "proj"
    local.mkdir()
    home = tmp_path / "claude"
    plugin = home / "plugins" / "mnemosyne" / ".claude-plugin"
    plugin.mkdir(parents=True)
    (plugin / "plugin.json").write_text("{}", encoding="utf-8")
    (home / "projects").mkdir(parents=True)
    assert is_available(local, claude_home=home) is False


def test_gate_true_via_curated_memories(tmp_path: Path) -> None:
    local = tmp_path / "proj"
    local.mkdir()
    home = tmp_path / "claude"
    slug = project_slug(local)
    memory_dir = home / "projects" / slug / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "a-fact.md").write_text("---\nname: a-fact\n---\nBody.\n", encoding="utf-8")
    assert is_available(local, claude_home=home) is True


def test_gate_ignores_memory_index_alone(tmp_path: Path) -> None:
    """MEMORY.md is the index, not a memory — alone it isn't archive evidence."""
    local = tmp_path / "proj"
    local.mkdir()
    home = tmp_path / "claude"
    slug = project_slug(local)
    memory_dir = home / "projects" / slug / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "MEMORY.md").write_text("- index\n", encoding="utf-8")
    assert is_available(local, claude_home=home) is False


# ---- files_with_region ----


def test_files_with_region_reports_healthy_and_malformed(tmp_path: Path) -> None:
    aligned = tmp_path / "aligned"
    aligned.mkdir()
    (aligned / "CLAUDE.md").write_text(render_block(), encoding="utf-8")

    malformed = tmp_path / "malformed"
    malformed.mkdir()
    (malformed / "AGENTS.md").write_text(f"{BEGIN}\norphan without end\n", encoding="utf-8")

    clean = tmp_path / "clean"
    clean.mkdir()
    (clean / "CLAUDE.md").write_text("# no region\n", encoding="utf-8")

    missing = tmp_path / "missing"  # no instruction files at all

    found = files_with_region([aligned, malformed, clean, missing])
    assert [f.parent.name for f in found] == ["aligned", "malformed"]


def test_apply_to_both_targets(tmp_path: Path) -> None:
    outcomes = [apply_to_file(f) for f in target_files(tmp_path)]
    assert outcomes == ["created", "created"]
    assert (tmp_path / "CLAUDE.md").is_file()
    assert (tmp_path / "AGENTS.md").is_file()
