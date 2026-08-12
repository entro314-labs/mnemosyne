"""``syne drift`` — deterministic staleness checks for the curated memory layer.

The self-alignment directive tells agents to treat recalled memories as *dated
evidence*. This module turns that instruction into a computed signal: it
mechanically verifies each memory's claims about the repository — file paths it
references, ``path:line`` anchors, and ``[[link]]`` references to sibling
memories — against the live project tree. No model involvement: every finding is
reproducible from the filesystem alone.

Checks per memory:

- **missing paths** — a backticked file reference (``src/foo.py``) that no longer
  exists under the project root (absolute references are checked as-is);
- **short files** — a ``path:line`` anchor pointing past the current end of file;
- **dangling links** — ``[[name]]`` references to memories that don't exist;
- **age** — days since the memory file was last written (reported, not judged).

Only findings are returned; memories whose references all resolve are counted but
not listed. A memory with no checkable references produces no finding — absence
of evidence is not staleness.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mnemosyne.codex import CODEX_HOME
from mnemosyne.memory import collect_memories

if TYPE_CHECKING:
    from mnemosyne.memory import Memory

# Backtick spans are where memories cite concrete files (`src/foo.py`, `a/b.ts:42`).
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")
# path:line anchor at the end of a reference.
_LINE_ANCHOR_RE = re.compile(r"^(?P<path>.+?):(?P<line>\d+)(?:-\d+)?$")
# Extensions that mark a token as a code/file reference (used when it has no "/").
_CODE_EXTENSIONS = frozenset(
    [
        "py",
        "ts",
        "tsx",
        "js",
        "jsx",
        "mjs",
        "cjs",
        "md",
        "mdx",
        "json",
        "jsonl",
        "toml",
        "yaml",
        "yml",
        "rs",
        "go",
        "java",
        "rb",
        "sh",
        "zsh",
        "sql",
        "css",
        "html",
        "txt",
        "lock",
        "cfg",
        "ini",
        "xml",
        "swift",
        "kt",
        "c",
        "h",
        "cpp",
        "hpp",
        "proto",
        "tf",
        "ipynb",
        "env",
    ]
)
# Characters that mean a token is a pattern/snippet, not a literal path.
_NON_PATH_CHARS = frozenset("*?<>{}()[]|$\"' =")
# A real memory name is a kebab/word slug — anything else in [[…]] is prose noise.
_LINK_NAME_RE = re.compile(r"^[A-Za-z0-9][\w-]*$")
# Vendor/cache trees pruned from the one-pass filename index.
_PRUNED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        "dist",
        "build",
        "target",
        ".next",
        ".turbo",
        ".idea",
        ".vscode",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
    }
)


@dataclass(slots=True)
class MemoryFinding:
    """Everything mechanically wrong (or aged) about one memory."""

    name: str
    file: str
    age_days: int
    missing_paths: list[str] = field(default_factory=list)
    short_files: list[dict[str, Any]] = field(default_factory=list)
    dangling_links: list[str] = field(default_factory=list)

    @property
    def has_issues(self) -> bool:
        return bool(self.missing_paths or self.short_files or self.dangling_links)


def _looks_like_path(token: str) -> bool:
    if not token or len(token) > 250 or any(c in _NON_PATH_CHARS for c in token):
        return False
    if token.startswith(("-", "http://", "https://")):
        return False
    bare = _LINE_ANCHOR_RE.match(token)
    candidate = (bare.group("path") if bare else token).rstrip("/")
    if not candidate:
        return False
    if "/" not in candidate and candidate.startswith("."):
        # `.meta.json`, `.jsonl` — extension references, not files.
        return False
    suffix = candidate.rsplit(".", 1)
    has_ext = len(suffix) == 2 and suffix[1].lower() in _CODE_EXTENSIONS
    if "/" not in candidate:
        return has_ext
    # Slash-bearing tokens must look strongly path-like, or prose fragments like
    # `and/or` would surface as "missing" noise.
    return (
        has_ext
        or candidate.count("/") >= 2
        or candidate.startswith(("src/", "tests/", "test/", "./", "~/", "/"))
    )


def extract_path_refs(body: str) -> list[str]:
    """Backticked tokens in a memory body that read as file references, deduped."""
    seen: dict[str, None] = {}
    for m in _BACKTICK_RE.finditer(body):
        token = m.group(1).strip()
        if _looks_like_path(token):
            seen.setdefault(token, None)
    return list(seen)


def _split_line_anchor(ref: str) -> tuple[str, int | None]:
    m = _LINE_ANCHOR_RE.match(ref)
    if m:
        return m.group("path"), int(m.group("line"))
    return ref, None


def _age_days(path: Path, *, now: datetime | None = None) -> int:
    current = now or datetime.now(UTC)
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError:
        return 0
    return max(0, (current - mtime).days)


def _count_lines(path: Path, at_least: int) -> int | None:
    """Line count, short-circuited once ``at_least`` is reached. None on read error."""
    try:
        count = 0
        with path.open(encoding="utf-8", errors="replace") as f:
            for count, _ in enumerate(f, 1):
                if count >= at_least:
                    return count
        return count
    except OSError:
        return None


def _filename_index(root: Path) -> frozenset[str]:
    """Every file basename under ``root`` (vendor/cache trees pruned), one pass.

    Lets a bare-name reference like ``memory.py`` resolve wherever the file
    lives in the tree — memories rarely cite full relative paths.
    """
    names: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _PRUNED_DIRS]
        names.update(filenames)
        _ = dirpath
    return frozenset(names)


def _ref_target(
    ref_path: str,
    roots: tuple[Path, ...],
    filenames: frozenset[str],
    *,
    nested_archive: Path | None = None,
) -> Path | bool | None:
    """Resolve a reference against the roots (worktree first, then the archive).

    Returns the resolved ``Path`` when a concrete file/dir is found, ``True``
    when a bare name is known only via the filename index (existence proven,
    location ambiguous), and ``None`` when nothing matches.
    """
    p = Path(ref_path).expanduser()
    if p.is_absolute():
        return p if p.exists() else None
    for root in roots:
        candidate = root / p
        if candidate.exists():
            return candidate
    # Claude archive artifacts live one level down
    # (<archive>/<session-id>/session-memory/…), and memories cite them without
    # the session prefix — try that level too.
    if nested_archive is not None:
        found = next(nested_archive.glob(f"*/{p.as_posix()}"), None)
        if found is not None:
            return found
    if "/" not in ref_path and p.name in filenames:
        return True
    return None


def check_memory(
    memory: Memory,
    project_root: Path,
    known_names: frozenset[str],
    *,
    archive_dir: Path | None = None,
    extra_roots: tuple[Path, ...] = (),
    filenames: frozenset[str] = frozenset(),
    now: datetime | None = None,
) -> MemoryFinding:
    """Run every deterministic check for one memory against the live tree.

    References are resolved against the worktree, then the archive dir (memories
    legitimately cite archive files like ``session-memory/summary.md``), then a
    basename index for bare names — only a reference that fails all three is a
    finding. Line anchors are verified only when the reference resolved to a
    concrete file.
    """
    finding = MemoryFinding(
        name=memory.name,
        file=memory.path.name,
        age_days=_age_days(memory.path, now=now),
        dangling_links=[
            link for link in memory.links if link not in known_names and _LINK_NAME_RE.match(link)
        ],
    )
    roots = (project_root, *((archive_dir,) if archive_dir is not None else ()), *extra_roots)
    for ref in extract_path_refs(memory.body):
        ref_path, line = _split_line_anchor(ref)
        target = _ref_target(ref_path, roots, filenames, nested_archive=archive_dir)
        if target is None:
            finding.missing_paths.append(ref)
            continue
        if line is not None and isinstance(target, Path) and target.is_file():
            actual = _count_lines(target, line)
            if actual is not None and actual < line:
                finding.short_files.append({"ref": ref, "lines": actual})
    return finding


def check_drift(
    project_dir: Path,
    project_root: Path,
    *,
    codex_home: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Check every curated memory of one project against its live repository.

    ``project_dir`` is the ``~/.claude/projects/<slug>/`` archive dir (where the
    memories live); ``project_root`` is the working tree they make claims about.
    """
    coll = collect_memories(project_dir)
    known = frozenset(m.name for m in coll.memories)
    filenames = _filename_index(project_root) if coll.memories else frozenset()
    codex_root = codex_home if codex_home is not None else CODEX_HOME
    extra_roots = (codex_root,) if codex_root.is_dir() else ()
    findings: list[MemoryFinding] = []
    clean = 0
    for m in coll.memories:
        finding = check_memory(
            m,
            project_root,
            known,
            archive_dir=project_dir,
            extra_roots=extra_roots,
            filenames=filenames,
            now=now,
        )
        if finding.has_issues:
            findings.append(finding)
        else:
            clean += 1
    findings.sort(key=lambda f: len(f.missing_paths) + len(f.short_files), reverse=True)
    return {
        "project_slug": project_dir.name,
        "project_root": str(project_root),
        "memory_count": len(coll.memories),
        "clean_count": clean,
        "findings": [
            {
                "memory": f.name,
                "file": f.file,
                "age_days": f.age_days,
                "missing_paths": f.missing_paths,
                "short_files": f.short_files,
                "dangling_links": f.dangling_links,
            }
            for f in findings
        ],
        "guidance": (
            "A finding is an unresolved file, line anchor, or memory link in the configured "
            "worktree/archive roots. It is a deterministic review signal, not proof that the "
            "memory's entire body is stale; re-verify the cited claim before relying on it."
        ),
    }


def render_drift_markdown(report: dict[str, Any]) -> str:
    """A compact human/agent-readable rendering of a drift report."""
    lines = [
        f"## Memory drift — {report['project_slug'].lstrip('-')}",
        f"_{report['memory_count']} memories · {report['clean_count']} clean · "
        f"{len(report['findings'])} with findings · root `{report['project_root']}`_",
    ]
    if not report["findings"]:
        lines.append("\nNo drift detected — every checkable reference resolves.")
        return "\n".join(lines)
    for f in report["findings"]:
        lines.append(f"\n**{f['memory']}** _(age {f['age_days']}d)_")
        lines.extend(f"- missing: `{p}`" for p in f["missing_paths"])
        lines.extend(
            f"- shrunk: `{s['ref']}` (file now {s['lines']} lines)" for s in f["short_files"]
        )
        lines.extend(f"- dangling link: `[[{link}]]`" for link in f["dangling_links"])
    lines.append(f"\n_{report['guidance']}_")
    return "\n".join(lines)
