"""Tests for artifact_export.py — writing the artifact bundle to disk."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from mnemosyne.artifact_export import (
    ArtifactWriteResult,
    slugify,
    write_project_memories,
    write_session_artifacts,
)
from mnemosyne.artifacts import ArtifactSelection, discover_session_artifacts
from mnemosyne.memory import collect_memories
from mnemosyne.render import RenderOptions

if TYPE_CHECKING:
    from pathlib import Path

SESSION_ID = "11111111-2222-3333-4444-555555555555"


def _agent_jsonl(text: str) -> str:
    record = {
        "type": "user",
        "uuid": "u1",
        "parentUuid": None,
        "timestamp": "2026-01-01T00:00:00Z",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }
    return json.dumps(record) + "\n"


def _make_session_tree(project_dir: Path, sid: str = SESSION_ID) -> None:
    ad = project_dir / sid
    sub = ad / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-aaa111.jsonl").write_text(_agent_jsonl("explore X here"), encoding="utf-8")
    (sub / "agent-aaa111.meta.json").write_text(
        json.dumps({"agentType": "Explore", "description": "explore X", "toolUseId": "toolu_1"}),
        encoding="utf-8",
    )
    wf = sub / "workflows" / "wf_123abc"
    wf.mkdir(parents=True)
    (wf / "agent-bbb222.jsonl").write_text(_agent_jsonl("audit Y deeply"), encoding="utf-8")
    (wf / "agent-bbb222.meta.json").write_text(
        json.dumps({"agentType": "general-purpose", "description": "audit Y"}), encoding="utf-8"
    )
    (wf / "journal.jsonl").write_text(
        json.dumps({"type": "started", "agentId": "bbb222"}) + "\n", encoding="utf-8"
    )
    scripts = ad / "workflows" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "my-audit-wf_123abc.js").write_text(
        "export const meta = { name: 'my-audit' }\n", encoding="utf-8"
    )
    sm = ad / "session-memory"
    sm.mkdir()
    (sm / "summary.md").write_text("# Session Title\n\nDid stuff.\n", encoding="utf-8")
    tr = ad / "tool-results"
    tr.mkdir()
    (tr / "out.txt").write_text("some output\n", encoding="utf-8")


# ---- slugify ----


def test_slugify_drops_punctuation_and_lowercases() -> None:
    assert slugify("Fix Godot: Spawn!") == "fix-godot-spawn"
    assert slugify("Can you do X? (please!)") == "can-you-do-x-please"


def test_slugify_collapses_whitespace_underscores_hyphens() -> None:
    assert slugify("hello___world  --  foo") == "hello-world-foo"


def test_slugify_empty_input() -> None:
    assert slugify("!!!") == ""
    assert slugify("---!!!  ___") == ""


def test_slugify_truncates_to_max_len() -> None:
    assert slugify("a" * 100, max_len=10) == "a" * 10
    assert len(slugify("a" * 200, max_len=50)) <= 50


# ---- write_session_artifacts ----


def test_selected_artifact_read_failure_is_not_silently_reported(tmp_path: Path) -> None:
    project = tmp_path / "-proj"
    _make_session_tree(project)
    artifacts = discover_session_artifacts(project, SESSION_ID)
    artifacts.subagents[0].path.unlink()

    with pytest.raises(OSError):
        write_session_artifacts(
            artifacts,
            tmp_path / "out",
            "session",
            opts=RenderOptions(),
            selection=ArtifactSelection(subagents=True),
        )


def test_write_session_artifacts_full(tmp_path: Path) -> None:
    proj = tmp_path / "-proj"
    _make_session_tree(proj)
    arts = discover_session_artifacts(proj, SESSION_ID)
    out = tmp_path / "export"
    out.mkdir()

    res = write_session_artifacts(
        arts, out, "mysession", opts=RenderOptions(), selection=ArtifactSelection.resolve(full=True)
    )

    assert res.subagents == 1
    assert res.workflow_agents == 1
    assert res.scripts == 1
    assert res.summaries == 1
    assert res.tool_results == 1
    assert (out / "mysession.subagents" / "index.json").is_file()
    assert (out / "mysession.summary.md").is_file()
    assert (out / "mysession.workflows" / "scripts" / "my-audit-wf_123abc.js").is_file()
    assert (out / "mysession.workflows" / "wf_123abc" / "journal-summary.json").is_file()
    assert (out / "mysession.tool-results" / "out.txt").is_file()

    # The subagent transcript is rendered through the normal pipeline.
    sub_md = next((out / "mysession.subagents").glob("*.md")).read_text(encoding="utf-8")
    assert "explore X here" in sub_md
    assert "Subagent: Explore" in sub_md


def test_write_session_artifacts_respects_selection(tmp_path: Path) -> None:
    proj = tmp_path / "-proj"
    _make_session_tree(proj)
    arts = discover_session_artifacts(proj, SESSION_ID)
    out = tmp_path / "export"
    out.mkdir()

    res = write_session_artifacts(
        arts, out, "s", opts=RenderOptions(), selection=ArtifactSelection.resolve(subagents=True)
    )
    assert res.subagents == 1
    assert res.summaries == 0
    assert res.scripts == 0
    assert res.tool_results == 0
    assert not (out / "s.summary.md").exists()
    assert not (out / "s.tool-results").exists()


def test_write_session_artifacts_noop_without_selection(tmp_path: Path) -> None:
    proj = tmp_path / "-proj"
    _make_session_tree(proj)
    arts = discover_session_artifacts(proj, SESSION_ID)
    out = tmp_path / "export"
    out.mkdir()
    res = write_session_artifacts(
        arts, out, "s", opts=RenderOptions(), selection=ArtifactSelection.resolve()
    )
    assert res.total == 0
    assert list(out.iterdir()) == []


# ---- write_project_memories ----


def test_write_project_memories_emits_copies_and_index(tmp_path: Path) -> None:
    mem = tmp_path / "-proj" / "memory"
    mem.mkdir(parents=True)
    (mem / "a.md").write_text(
        '---\nname: a\ndescription: "x"\nmetadata:\n  type: project\n---\nbody links [[b]]\n',
        encoding="utf-8",
    )
    (mem / "b.md").write_text("---\nname: b\n---\nother body\n", encoding="utf-8")
    (mem / "MEMORY.md").write_text("- [a](a.md) — hook\n", encoding="utf-8")

    out = tmp_path / "export"
    out.mkdir()
    mem_out = write_project_memories(
        collect_memories(tmp_path / "-proj"), out, project_label="proj", project_path="/x"
    )

    assert mem_out == out / "memory"
    assert (out / "memory" / "a.md").is_file()
    assert (out / "memory" / "b.md").is_file()
    assert (out / "memory" / "MEMORY.md").is_file()
    assert (out / "memory" / "memories.md").is_file()
    idx = json.loads((out / "memory" / "memories.json").read_text(encoding="utf-8"))
    assert idx["memory_count"] == 2
    assert idx["project_path"] == "/x"


def test_write_project_memories_none_when_empty(tmp_path: Path) -> None:
    out = tmp_path / "export"
    out.mkdir()
    assert write_project_memories(collect_memories(tmp_path / "-empty"), out) is None
    assert not (out / "memory").exists()


# ---- ArtifactWriteResult ----


def test_artifact_write_result_total_and_merge() -> None:
    a = ArtifactWriteResult(subagents=2, summaries=1)
    a.merge(ArtifactWriteResult(subagents=3, scripts=1))
    assert a.subagents == 5
    assert a.scripts == 1
    assert a.summaries == 1
    assert a.total == 7


# ---- managed subtrees are reconciled, not merely appended to (F-06) ----


def test_rerun_removes_artifacts_whose_source_disappeared(tmp_path: Path) -> None:
    """A subagent deleted upstream must stop appearing in the export."""
    proj = tmp_path / "-proj"
    _make_session_tree(proj)
    out = tmp_path / "export"
    out.mkdir()
    selection = ArtifactSelection.resolve(subagents=True)

    arts = discover_session_artifacts(proj, SESSION_ID)
    write_session_artifacts(arts, out, "s", opts=RenderOptions(), selection=selection)
    stale = out / "s.subagents" / "ghost-agent.md"
    stale.write_text("output from a source that no longer exists\n", encoding="utf-8")

    write_session_artifacts(arts, out, "s", opts=RenderOptions(), selection=selection)

    assert not stale.exists()
    assert (out / "s.subagents" / "index.json").is_file()


def test_selected_but_empty_category_clears_its_stale_subtree(tmp_path: Path) -> None:
    proj = tmp_path / "-proj"
    _make_session_tree(proj)
    out = tmp_path / "export"
    out.mkdir()
    selection = ArtifactSelection.resolve(subagents=True)

    arts = discover_session_artifacts(proj, SESSION_ID)
    write_session_artifacts(arts, out, "s", opts=RenderOptions(), selection=selection)
    assert (out / "s.subagents").is_dir()

    # Same selection, but the source now yields nothing.
    empty = discover_session_artifacts(tmp_path / "-empty-proj", SESSION_ID)
    write_session_artifacts(empty, out, "s", opts=RenderOptions(), selection=selection)

    assert not (out / "s.subagents").exists()


def test_unselected_category_is_left_untouched(tmp_path: Path) -> None:
    proj = tmp_path / "-proj"
    _make_session_tree(proj)
    out = tmp_path / "export"
    out.mkdir()

    write_session_artifacts(
        discover_session_artifacts(proj, SESSION_ID),
        out,
        "s",
        opts=RenderOptions(),
        selection=ArtifactSelection.resolve(full=True),
    )
    assert (out / "s.tool-results").is_dir()

    # Re-export selecting only subagents: tool-results is not ours to touch now.
    write_session_artifacts(
        discover_session_artifacts(proj, SESSION_ID),
        out,
        "s",
        opts=RenderOptions(),
        selection=ArtifactSelection.resolve(subagents=True),
    )
    assert (out / "s.tool-results" / "out.txt").is_file()


def test_no_staging_directories_survive_a_write(tmp_path: Path) -> None:
    proj = tmp_path / "-proj"
    _make_session_tree(proj)
    out = tmp_path / "export"
    out.mkdir()
    write_session_artifacts(
        discover_session_artifacts(proj, SESSION_ID),
        out,
        "s",
        opts=RenderOptions(),
        selection=ArtifactSelection.resolve(full=True),
    )
    assert [p.name for p in out.rglob(".*.staging")] == []
