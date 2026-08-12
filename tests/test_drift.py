"""Tests for drift.py — deterministic staleness checks for curated memories."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mnemosyne.drift import (
    check_drift,
    extract_path_refs,
    render_drift_markdown,
)

if TYPE_CHECKING:
    from pathlib import Path


def _memory(body: str, name: str = "the-fact", description: str = "a fact") -> str:
    return (
        f'---\nname: {name}\ndescription: "{description}"\nmetadata:\n  type: project\n---\n'
        f"{body}\n"
    )


def _project(tmp_path: Path, memories: dict[str, str]) -> tuple[Path, Path]:
    """Build an archive dir (with memory/) plus a working tree to check against."""
    archive = tmp_path / "-proj"
    (archive / "memory").mkdir(parents=True)
    for name, body in memories.items():
        (archive / "memory" / f"{name}.md").write_text(_memory(body, name), encoding="utf-8")
    root = tmp_path / "worktree"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("line1\nline2\nline3\n", encoding="utf-8")
    return archive, root


# ---- reference extraction ----


def test_extract_path_refs_accepts_real_shapes() -> None:
    body = "See `src/app.py` and `src/app.py:2` plus `tests/deep/mod.rs` and `config.toml`."
    assert extract_path_refs(body) == [
        "src/app.py",
        "src/app.py:2",
        "tests/deep/mod.rs",
        "config.toml",
    ]


def test_extract_path_refs_rejects_prose_and_commands() -> None:
    body = (
        "Use `and/or` wording, run `syne drift`, pass `--force`, visit `https://x.dev/a.py`, "
        "check `pd.name` or `foo/bar` casually, and write `.meta.json` sidecars as `.jsonl`."
    )
    assert extract_path_refs(body) == []


# ---- drift report ----


def test_check_drift_flags_missing_shrunk_and_dangling(tmp_path: Path) -> None:
    archive, root = _project(
        tmp_path,
        {
            "clean-memory": "All good: `src/app.py` and `src/app.py:2` and [[stale-memory]].",
            "stale-memory": "Cites `src/gone.py` and `src/app.py:99` and [[never-written]].",
        },
    )
    report = check_drift(archive, root)
    assert report["memory_count"] == 2
    assert report["clean_count"] == 1
    assert len(report["findings"]) == 1
    f = report["findings"][0]
    assert f["memory"] == "stale-memory"
    assert f["missing_paths"] == ["src/gone.py"]
    assert f["short_files"] == [{"ref": "src/app.py:99", "lines": 3}]
    assert f["dangling_links"] == ["never-written"]
    assert f["age_days"] >= 0


def test_check_drift_clean_project(tmp_path: Path) -> None:
    archive, root = _project(tmp_path, {"clean-memory": "Only `src/app.py` here."})
    report = check_drift(archive, root)
    assert report["findings"] == []
    assert report["clean_count"] == 1
    assert "No drift detected" in render_drift_markdown(report)


def test_bare_names_resolve_anywhere_in_tree(tmp_path: Path) -> None:
    archive, root = _project(
        tmp_path,
        {"nested-memory": "The parser lives in `app.py`; `vanished.py` is gone."},
    )
    # app.py exists at src/app.py — a bare-name reference must find it there.
    report = check_drift(archive, root)
    assert report["findings"][0]["missing_paths"] == ["vanished.py"]


def test_archive_relative_refs_resolve(tmp_path: Path) -> None:
    archive, root = _project(
        tmp_path,
        {"handoff-memory": "See `session-memory/summary.md` for the digest."},
    )
    sm = archive / "session-memory"
    sm.mkdir()
    (sm / "summary.md").write_text("digest\n", encoding="utf-8")
    report = check_drift(archive, root)
    assert report["findings"] == []


def test_codex_archive_refs_resolve(tmp_path: Path) -> None:
    archive, root = _project(
        tmp_path,
        {"codex-memory": "Titles come from `session_index.jsonl`."},
    )
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    (codex_home / "session_index.jsonl").write_text("{}\n", encoding="utf-8")
    report = check_drift(archive, root, codex_home=codex_home)
    assert report["findings"] == []


def test_vendor_trees_pruned_from_index(tmp_path: Path) -> None:
    archive, root = _project(
        tmp_path,
        {"vendor-memory": "Depends on `left-pad.js` somewhere."},
    )
    vendored = root / "node_modules" / "left-pad"
    vendored.mkdir(parents=True)
    (vendored / "left-pad.js").write_text("x\n", encoding="utf-8")
    report = check_drift(archive, root)
    # The only copy lives in a pruned tree — the reference counts as missing.
    assert report["findings"][0]["missing_paths"] == ["left-pad.js"]


def test_garbage_links_filtered_from_dangling(tmp_path: Path) -> None:
    archive, root = _project(
        tmp_path,
        {"doc-memory": "Noise like [[--not-a-name]] and [[ ]] is dropped; [[real-target]] counts."},
    )
    report = check_drift(archive, root)
    assert report["findings"][0]["dangling_links"] == ["real-target"]


def test_check_drift_no_memories(tmp_path: Path) -> None:
    archive = tmp_path / "-empty"
    archive.mkdir()
    report = check_drift(archive, tmp_path)
    assert report["memory_count"] == 0
    assert report["findings"] == []


def test_render_drift_markdown_lists_findings(tmp_path: Path) -> None:
    archive, root = _project(tmp_path, {"stale-memory": "Gone: `src/missing.ts`."})
    out = render_drift_markdown(check_drift(archive, root))
    assert "stale-memory" in out
    assert "src/missing.ts" in out
    assert "deterministic review signal" in out


def test_absolute_refs_checked_as_is(tmp_path: Path) -> None:
    archive, root = _project(tmp_path, {})
    target = tmp_path / "abs.py"
    target.write_text("x\n", encoding="utf-8")
    (archive / "memory" / "abs-memory.md").write_text(
        _memory(f"Present `{target}` but missing `{tmp_path / 'nope.py'}`.", "abs-memory"),
        encoding="utf-8",
    )
    report = check_drift(archive, root)
    assert report["findings"][0]["missing_paths"] == [str(tmp_path / "nope.py")]
