"""User settings + project registry persisted to ~/.config/mnemosyne/."""

from __future__ import annotations

import json
import subprocess
import tomllib
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import tomli_w

CONFIG_DIR = Path.home() / ".config" / "mnemosyne"
CONFIG_PATH = CONFIG_DIR / "config.toml"
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"


@dataclass(slots=True)
class Defaults:
    output_dir: str = "{local_path}/.claude-exports"
    mode: str = "transcript"  # transcript | compact | full
    include_thinking: bool = False
    include_attachments: bool = False
    include_reminders: bool = False
    max_tool_chars: int = 2000


@dataclass(slots=True)
class ProjectEntry:
    slug: str
    local_path: str | None = None
    friendly_name: str | None = None
    git_remote: str | None = None
    git_branch: str | None = None
    last_used: str | None = None


@dataclass(slots=True)
class Settings:
    defaults: Defaults = field(default_factory=Defaults)
    projects: dict[str, ProjectEntry] = field(default_factory=dict)


def _filter_known_fields(cls: type, data: dict[str, Any]) -> dict[str, Any]:
    """Drop keys that aren't fields of `cls` so future schema drift doesn't crash load."""
    known = {f.name for f in fields(cls)}
    return {k: v for k, v in data.items() if k in known}


def load_settings(path: Path = CONFIG_PATH) -> Settings:
    if not path.exists():
        return Settings()
    with path.open("rb") as f:
        data = tomllib.load(f)
    defaults = Defaults(**_filter_known_fields(Defaults, data.get("defaults", {})))
    projects: dict[str, ProjectEntry] = {}
    for raw in data.get("projects", []):
        if "slug" not in raw:
            continue
        entry = ProjectEntry(**_filter_known_fields(ProjectEntry, raw))
        projects[entry.slug] = entry
    return Settings(defaults=defaults, projects=projects)


def save_settings(settings: Settings, path: Path = CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "defaults": asdict(settings.defaults),
        "projects": [
            {k: v for k, v in asdict(p).items() if v is not None}
            for p in sorted(settings.projects.values(), key=lambda p: p.slug)
        ],
    }
    with path.open("wb") as f:
        tomli_w.dump(payload, f)


# ---- Discovery ----


def _read_first_cwd(slug_dir: Path) -> str | None:
    """Resolve a Claude slug to its real local path by reading any session's `cwd` field.

    The slug → path mapping is lossy (path with `/` replaced by `-`), so we can't
    invert it reliably. Each session record stores the actual cwd; that's authoritative.
    """
    for jsonl in sorted(slug_dir.glob("*.jsonl")):
        try:
            with jsonl.open(encoding="utf-8") as f:
                for raw in f:
                    stripped = raw.strip()
                    if not stripped:
                        continue
                    try:
                        obj = json.loads(stripped)
                    except json.JSONDecodeError:
                        continue
                    cwd = obj.get("cwd")
                    if cwd:
                        return cwd
        except OSError:
            continue
    return None


def _git_info(local_path: str) -> tuple[str | None, str | None]:
    path = Path(local_path)
    if not (path / ".git").exists():
        return None, None
    remote = _run_git(path, ["remote", "get-url", "origin"])
    branch = _run_git(path, ["branch", "--show-current"])
    return remote, branch


def _run_git(path: Path, args: list[str]) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    out = result.stdout.strip()
    return out or None


def _derive_friendly_name(local_path: str | None, slug: str) -> str:
    """Pick a non-empty, human-readable name for a project.

    Tries (1) last path component of the actual cwd, (2) the slug with its
    leading dash trimmed, (3) "root" for the filesystem-root edge case, (4)
    "unnamed" as the last resort. Guaranteed non-empty so downstream code can
    rely on it for filesystem-safe directory names.
    """
    if local_path:
        name = Path(local_path).name
        if name:
            return name
        # local_path was "/" — Path("/").name is "".
        return "root"
    return slug.lstrip("-") or "unnamed"


def discover_projects(claude_root: Path | None = None) -> list[ProjectEntry]:
    """Walk ~/.claude/projects/* and resolve each slug to a real local path."""
    # Resolve at call time so monkeypatching CLAUDE_PROJECTS in tests works.
    root = claude_root if claude_root is not None else CLAUDE_PROJECTS
    if not root.is_dir():
        return []
    entries: list[ProjectEntry] = []
    for slug_dir in sorted(root.iterdir()):
        if not slug_dir.is_dir() or not slug_dir.name.startswith("-"):
            continue
        local_path = _read_first_cwd(slug_dir)
        entries.append(
            ProjectEntry(
                slug=slug_dir.name,
                local_path=local_path,
                friendly_name=_derive_friendly_name(local_path, slug_dir.name),
            )
        )
    return entries


def sync_registry(
    settings: Settings,
    *,
    claude_root: Path | None = None,
    refresh_git: bool = False,
) -> Settings:
    """Merge filesystem discovery into the persisted registry.

    Preserves user-edited fields (friendly_name, git_*, last_used) on existing
    entries. New entries get their git info populated.
    """
    root = claude_root if claude_root is not None else CLAUDE_PROJECTS
    for discovered in discover_projects(root):
        existing = settings.projects.get(discovered.slug)
        if existing is None:
            if discovered.local_path:
                remote, branch = _git_info(discovered.local_path)
                discovered.git_remote = remote
                discovered.git_branch = branch
            settings.projects[discovered.slug] = discovered
            continue

        # Refresh fields we own from discovery; keep user-set fields.
        if not existing.local_path and discovered.local_path:
            existing.local_path = discovered.local_path
        if not existing.friendly_name and discovered.friendly_name:
            existing.friendly_name = discovered.friendly_name
        if refresh_git and existing.local_path:
            remote, branch = _git_info(existing.local_path)
            if remote:
                existing.git_remote = remote
            if branch:
                existing.git_branch = branch
    return settings


def mark_used(settings: Settings, slug: str) -> None:
    entry = settings.projects.get(slug)
    if entry is not None:
        entry.last_used = datetime.now(UTC).isoformat(timespec="seconds")


def resolve_output_dir(template: str, *, entry: ProjectEntry | None) -> Path:
    """Expand `{local_path}` and `{slug}` in the output-dir template.

    Falls back to cwd if `{local_path}` is referenced but the entry has none.
    """
    local = (entry.local_path if entry else None) or str(Path.cwd())
    slug = entry.slug if entry else ""
    expanded = template.format(local_path=local, slug=slug, cwd=str(Path.cwd()))
    return Path(expanded).expanduser()
