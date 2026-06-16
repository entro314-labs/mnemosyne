"""Tests for artifacts.py — per-session artifact discovery and selection."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from mnemosyne.artifacts import (
    ArtifactSelection,
    discover_session_artifacts,
    summarize_journal,
)

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


def _make_session_tree(project_dir: Path, sid: str = SESSION_ID) -> Path:
    """Build a full artifact tree for one session; return its artifact dir."""
    ad = project_dir / sid
    # direct subagent
    sub = ad / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-aaa111.jsonl").write_text(_agent_jsonl("explore X"), encoding="utf-8")
    (sub / "agent-aaa111.meta.json").write_text(
        json.dumps({"agentType": "Explore", "description": "explore X", "toolUseId": "toolu_1"}),
        encoding="utf-8",
    )
    # workflow run (agents + journal)
    wf = sub / "workflows" / "wf_123abc"
    wf.mkdir(parents=True)
    (wf / "agent-bbb222.jsonl").write_text(_agent_jsonl("audit Y"), encoding="utf-8")
    (wf / "agent-bbb222.meta.json").write_text(
        json.dumps(
            {"agentType": "general-purpose", "description": "audit Y", "toolUseId": "toolu_2"}
        ),
        encoding="utf-8",
    )
    (wf / "journal.jsonl").write_text(
        json.dumps({"type": "started", "key": "k", "agentId": "bbb222"})
        + "\n"
        + json.dumps({"type": "result", "key": "k", "agentId": "bbb222"})
        + "\n",
        encoding="utf-8",
    )
    # workflow script
    scripts = ad / "workflows" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "my-audit-wf_123abc.js").write_text(
        "export const meta = {\n  name: 'my-audit',\n  description: 'Audit the thing',\n}\n",
        encoding="utf-8",
    )
    # session-memory handoff digest
    sm = ad / "session-memory"
    sm.mkdir()
    (sm / "summary.md").write_text("# Session Title\n\nDid stuff.\n", encoding="utf-8")
    # externalised tool output
    tr = ad / "tool-results"
    tr.mkdir()
    (tr / "out.txt").write_text("some output\n", encoding="utf-8")
    return ad


def test_discover_finds_every_category(tmp_path: Path) -> None:
    proj = tmp_path / "-proj"
    _make_session_tree(proj)
    arts = discover_session_artifacts(proj, SESSION_ID)

    assert len(arts.subagents) == 1
    direct = arts.subagents[0]
    assert direct.agent_id == "aaa111"
    assert direct.agent_type == "Explore"
    assert direct.tool_use_id == "toolu_1"
    assert direct.workflow_id is None

    assert len(arts.workflow_runs) == 1
    run = arts.workflow_runs[0]
    assert run.workflow_id == "wf_123abc"
    assert run.journal_path is not None
    assert len(run.agents) == 1
    assert run.agents[0].workflow_id == "wf_123abc"

    assert len(arts.scripts) == 1
    assert arts.scripts[0].name == "my-audit"
    assert arts.scripts[0].description == "Audit the thing"
    assert arts.scripts[0].workflow_id == "wf_123abc"

    assert len(arts.summaries) == 1
    assert len(arts.tool_results) == 1
    assert bool(arts) is True


def test_discover_empty_when_no_artifact_dir(tmp_path: Path) -> None:
    arts = discover_session_artifacts(tmp_path / "-proj", "no-such-session")
    assert not arts
    assert arts.subagents == []
    assert arts.workflow_runs == []


def test_subagent_with_missing_meta_is_graceful(tmp_path: Path) -> None:
    proj = tmp_path / "-proj"
    sub = proj / "s" / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-ccc333.jsonl").write_text("{}\n", encoding="utf-8")  # no .meta.json
    arts = discover_session_artifacts(proj, "s")
    assert len(arts.subagents) == 1
    a = arts.subagents[0]
    assert a.agent_id == "ccc333"
    assert a.agent_type is None
    assert a.description is None
    assert a.tool_use_id is None


def test_summarize_journal_counts_started_and_completed(tmp_path: Path) -> None:
    j = tmp_path / "journal.jsonl"
    j.write_text(
        json.dumps({"type": "started", "agentId": "a"})
        + "\n"
        + json.dumps({"type": "started", "agentId": "b"})
        + "\n"
        + json.dumps({"type": "result", "agentId": "a"})
        + "\n",
        encoding="utf-8",
    )
    s = summarize_journal(j)
    assert s["agents_started"] == 2
    assert s["agents_completed"] == 1
    assert s["distinct_agents"] == 2


def test_selection_full_turns_everything_on() -> None:
    sel = ArtifactSelection.resolve(full=True)
    assert sel.memories and sel.subagents and sel.summaries
    assert sel.workflows and sel.tool_results
    assert sel.any_session_scoped
    assert bool(sel)


def test_selection_memories_is_project_scoped_only() -> None:
    sel = ArtifactSelection.resolve(memories=True)
    assert sel.memories
    assert not sel.subagents
    # memories is written per-project, not per-session
    assert not sel.any_session_scoped
    assert bool(sel)


def test_selection_empty_is_falsy() -> None:
    sel = ArtifactSelection.resolve()
    assert not sel
    assert not sel.any_session_scoped
