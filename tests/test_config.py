"""Tests for project discovery edge cases — empty paths, unreadable jsonl, stale registry."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from claude_session_export.config import (
    ProjectEntry,
    Settings,
    _derive_friendly_name,
    discover_projects,
    sync_registry,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_derive_friendly_name_uses_path_basename() -> None:
    assert _derive_friendly_name("/Users/me/proj", "-Users-me-proj") == "proj"


def test_derive_friendly_name_handles_root_path() -> None:
    # Path("/").name == "" — must not produce an empty friendly_name.
    assert _derive_friendly_name("/", "-") == "root"


def test_derive_friendly_name_falls_back_to_slug_when_no_path() -> None:
    assert _derive_friendly_name(None, "-Users-me-proj") == "Users-me-proj"


def test_derive_friendly_name_last_resort_unnamed() -> None:
    assert _derive_friendly_name(None, "-") == "unnamed"


def _make_session_jsonl(path: Path, cwd: str) -> None:
    path.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "u1",
                "cwd": cwd,
                "message": {"content": [{"type": "text", "text": "hi"}]},
            }
        )
        + "\n",
        encoding="utf-8",
    )


def test_discover_assigns_friendly_name_root_for_filesystem_root(tmp_path: Path) -> None:
    """Project at filesystem root (slug='-') must get friendly_name='root'."""
    claude_root = tmp_path / "projects"
    slug_dir = claude_root / "-"
    slug_dir.mkdir(parents=True)
    _make_session_jsonl(slug_dir / "s1.jsonl", cwd="/")

    entries = discover_projects(claude_root)
    assert len(entries) == 1
    assert entries[0].slug == "-"
    assert entries[0].local_path == "/"
    assert entries[0].friendly_name == "root"


def test_discover_assigns_friendly_name_from_path(tmp_path: Path) -> None:
    claude_root = tmp_path / "projects"
    slug_dir = claude_root / "-private-tmp-myproj"
    slug_dir.mkdir(parents=True)
    _make_session_jsonl(slug_dir / "s1.jsonl", cwd="/private/tmp/myproj")

    entries = discover_projects(claude_root)
    assert entries[0].friendly_name == "myproj"


def test_sync_registry_heals_stale_empty_friendly_name(tmp_path: Path) -> None:
    """A registry saved before the fix may have friendly_name=''. sync_registry
    must rewrite it from fresh discovery."""
    claude_root = tmp_path / "projects"
    slug_dir = claude_root / "-"
    slug_dir.mkdir(parents=True)
    _make_session_jsonl(slug_dir / "s1.jsonl", cwd="/")

    # Pre-seed the registry as if it had been saved with the old buggy default.
    stale = Settings(projects={"-": ProjectEntry(slug="-", local_path="/", friendly_name="")})
    sync_registry(stale, claude_root=claude_root)
    assert stale.projects["-"].friendly_name == "root"


def test_discover_skips_non_slug_dirs(tmp_path: Path) -> None:
    claude_root = tmp_path / "projects"
    # Marketplace dirs and other non-slug entries should be ignored.
    (claude_root / "marketplaces").mkdir(parents=True)
    (claude_root / "cache").mkdir()
    slug_dir = claude_root / "-foo-bar"
    slug_dir.mkdir()
    _make_session_jsonl(slug_dir / "s1.jsonl", cwd="/foo/bar")

    entries = discover_projects(claude_root)
    assert [e.slug for e in entries] == ["-foo-bar"]
