"""Tests for cwd → workspace detection (the auto-load default behaviour)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mnemosyne.cli import _detect_cwd_project
from mnemosyne.config import ProjectEntry, Settings

if TYPE_CHECKING:
    from pathlib import Path


def _make_workspace(tmp_path: Path, *, with_session: bool) -> tuple[Path, Path, str]:
    """Create a cwd plus a matching ~/.claude/projects/<slug> dir; return both + slug."""
    cwd = tmp_path / "my-proj"
    cwd.mkdir()
    claude_root = tmp_path / "projects"
    claude_root.mkdir()
    slug = str(cwd.resolve()).replace("/", "-")
    slug_dir = claude_root / slug
    slug_dir.mkdir()
    if with_session:
        (slug_dir / "session.jsonl").write_text("{}\n", encoding="utf-8")
    return cwd, claude_root, slug


def test_detect_returns_entry_for_workspace_with_sessions(tmp_path: Path) -> None:
    cwd, claude_root, slug = _make_workspace(tmp_path, with_session=True)
    entry = _detect_cwd_project(Settings(), cwd=cwd, claude_root=claude_root)
    assert entry is not None
    assert entry.slug == slug
    assert entry.friendly_name == "my-proj"
    assert entry.local_path == str(cwd)


def test_detect_returns_none_when_workspace_has_no_sessions(tmp_path: Path) -> None:
    cwd, claude_root, _ = _make_workspace(tmp_path, with_session=False)
    assert _detect_cwd_project(Settings(), cwd=cwd, claude_root=claude_root) is None


def test_detect_returns_none_outside_a_workspace(tmp_path: Path) -> None:
    cwd = tmp_path / "not-a-project"
    cwd.mkdir()
    claude_root = tmp_path / "projects"
    claude_root.mkdir()
    assert _detect_cwd_project(Settings(), cwd=cwd, claude_root=claude_root) is None


def test_detect_prefers_existing_registry_entry(tmp_path: Path) -> None:
    cwd, claude_root, slug = _make_workspace(tmp_path, with_session=True)
    existing = ProjectEntry(slug=slug, friendly_name="Custom Name", local_path="/elsewhere")
    settings = Settings(projects={slug: existing})
    entry = _detect_cwd_project(settings, cwd=cwd, claude_root=claude_root)
    assert entry is existing
    assert entry.friendly_name == "Custom Name"
