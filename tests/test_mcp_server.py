"""Tests for mcp_server.py — verify each tool returns the expected shape.

The MCP tools remain plain callables after MCPServer registration, so tests can
call them directly without a transport. ``_call`` retains the old ``.fn``
fallback for fixtures created with earlier SDK versions.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from mnemosyne import config, mcp_server, parser
from mnemosyne.mcp_server import (
    get_memory,
    get_session,
    get_session_handoff,
    get_session_summary,
    get_subagent,
    list_memories,
    list_projects,
    list_sessions,
    list_subagents,
    recall_recent,
    search_memories,
    search_sessions,
    self_align,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def fake_claude_home(tmp_path: Path, monkeypatch):
    """Spin up a fake ~/.claude/projects/<slug>/ with one session and stub the registry."""
    claude_projects = tmp_path / "claude" / "projects"
    slug = "-private-tmp-fake-project"
    project_dir = claude_projects / slug
    project_dir.mkdir(parents=True)

    # One session: user prompt + assistant reply
    records = [
        {
            "type": "user",
            "uuid": "u1",
            "parentUuid": None,
            "timestamp": "2026-01-01T00:00:00Z",
            "cwd": "/private/tmp/fake-project",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "investigate the auth flow please"}],
            },
        },
        {
            "type": "assistant",
            "uuid": "a1",
            "parentUuid": "u1",
            "timestamp": "2026-01-01T00:00:05Z",
            "message": {
                "role": "assistant",
                "stop_reason": "end_turn",
                "content": [
                    {"type": "text", "text": "Auth uses JWT with 24h expiry — found the issue."}
                ],
            },
        },
        {"type": "ai-title", "aiTitle": "Investigate auth flow", "sessionId": "fake"},
    ]
    full_sid = "abc12345-0000-0000-0000-000000000000"
    session_path = project_dir / f"{full_sid}.jsonl"
    session_path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

    # Curated memory layer for this project.
    mem_dir = project_dir / "memory"
    mem_dir.mkdir()
    (mem_dir / "the-plan.md").write_text(
        '---\nname: the-plan\ndescription: "ship v2"\nmetadata:\n  type: project\n---\n'
        "The roadmap is to ship v2 with JWT auth.\n",
        encoding="utf-8",
    )

    # A subagent transcript stored beside the session (hidden from the main transcript).
    sub_dir = project_dir / full_sid / "subagents"
    sub_dir.mkdir(parents=True)
    (sub_dir / "agent-deadbeef01.jsonl").write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "su1",
                "parentUuid": None,
                "timestamp": "2026-01-01T00:01:00Z",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "audit the login route"}],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (sub_dir / "agent-deadbeef01.meta.json").write_text(
        json.dumps({"agentType": "Explore", "description": "audit login", "toolUseId": "toolu_x"}),
        encoding="utf-8",
    )

    # A compaction handoff digest for the session.
    sm_dir = project_dir / full_sid / "session-memory"
    sm_dir.mkdir(parents=True)
    (sm_dir / "summary.md").write_text(
        "# Session Title\n\nAuth flow\n\n# Next steps\n\nWire refresh tokens.\n", encoding="utf-8"
    )

    # Stub the module-level CLAUDE_PROJECTS in every place that reads it.
    monkeypatch.setattr(config, "CLAUDE_PROJECTS", claude_projects)
    monkeypatch.setattr(mcp_server, "CLAUDE_PROJECTS", claude_projects)
    monkeypatch.setattr(parser, "project_dir_for_cwd", lambda cwd: project_dir)
    monkeypatch.setattr(mcp_server, "project_dir_for_cwd", lambda cwd: project_dir)
    # Force load_settings to use a temp config file so we don't pollute the real one.
    config_path = tmp_path / "config.toml"
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)

    return {"slug": slug, "session_id": "abc12345", "project_dir": project_dir}


def _call(tool):
    """Return the registered function, unwrapping older SDK wrappers if needed."""
    return tool.fn if hasattr(tool, "fn") else tool


def test_list_projects_returns_discovered_entry(fake_claude_home) -> None:
    result = _call(list_projects)()
    assert any(p["slug"] == fake_claude_home["slug"] for p in result)
    project = next(p for p in result if p["slug"] == fake_claude_home["slug"])
    assert project["session_count"] == 1
    assert project["local_path"] == "/private/tmp/fake-project"


def test_list_sessions_returns_summary(fake_claude_home) -> None:
    result = _call(list_sessions)(project=fake_claude_home["slug"])
    assert len(result) == 1
    s = result[0]
    assert s["session_id"].startswith(fake_claude_home["session_id"])
    assert s["title"] == "Investigate auth flow"
    assert s["user_count"] == 1
    assert s["assistant_count"] == 1


def test_get_session_summary_cheap_header(fake_claude_home) -> None:
    s = _call(get_session_summary)(
        session_id=fake_claude_home["session_id"], project=fake_claude_home["slug"]
    )
    assert s["title"] == "Investigate auth flow"
    assert "auth flow" in s["first_prompt"]


def test_get_session_returns_rendered_markdown(fake_claude_home) -> None:
    md = _call(get_session)(
        session_id=fake_claude_home["session_id"],
        project=fake_claude_home["slug"],
        mode="transcript",
    )
    assert "Investigate auth flow" in md
    assert "investigate the auth flow please" in md
    assert "JWT with 24h expiry" in md


def test_recall_recent_is_list_sessions(fake_claude_home) -> None:
    result = _call(recall_recent)(project=fake_claude_home["slug"], limit=3)
    assert len(result) == 1


def test_search_sessions_finds_match(fake_claude_home) -> None:
    hits = _call(search_sessions)(query="JWT", project=fake_claude_home["slug"])
    assert len(hits) == 1
    assert "JWT" in hits[0]["snippet"]
    assert hits[0]["title"] == "Investigate auth flow"


def test_search_sessions_no_match(fake_claude_home) -> None:
    hits = _call(search_sessions)(
        query="this string definitely does not appear", project=fake_claude_home["slug"]
    )
    assert hits == []


def test_search_sessions_empty_query_returns_empty(fake_claude_home) -> None:
    assert _call(search_sessions)(query="   ", project=fake_claude_home["slug"]) == []


def test_search_sessions_defaults_to_current_project(fake_claude_home) -> None:
    hits = _call(search_sessions)(query="JWT")
    assert len(hits) == 1
    assert hits[0]["project_slug"] == fake_claude_home["slug"]


def test_cross_project_search_requires_explicit_flag(fake_claude_home) -> None:
    other = fake_claude_home["project_dir"].parent / "-other-project"
    other.mkdir()
    (other / "other.jsonl").write_text(
        json.dumps(
            {
                "type": "user",
                "timestamp": "2026-01-02T00:00:00Z",
                "message": {"content": [{"type": "text", "text": "cross-project-only"}]},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert _call(search_sessions)(query="cross-project-only") == []
    hits = _call(search_sessions)(query="cross-project-only", all_projects=True)
    assert [hit["project_slug"] for hit in hits] == ["-other-project"]


def test_project_slug_rejects_relative_path_traversal(fake_claude_home) -> None:
    with pytest.raises(ValueError, match="Invalid project slug"):
        _call(list_sessions)(project="../../tmp")


def test_unknown_session_prefix_raises(fake_claude_home) -> None:
    with pytest.raises(FileNotFoundError, match="No session matching"):
        _call(get_session)(session_id="zzzzzzz", project=fake_claude_home["slug"])


# ---- memory tools ----


def test_list_memories_returns_curated_facts(fake_claude_home) -> None:
    mems = _call(list_memories)(project=fake_claude_home["slug"])
    assert len(mems) == 1
    assert mems[0]["name"] == "the-plan"
    assert mems[0]["type"] == "project"
    assert mems[0]["description"] == "ship v2"


def test_get_memory_returns_full_body(fake_claude_home) -> None:
    m = _call(get_memory)(name="the-plan", project=fake_claude_home["slug"])
    assert m["description"] == "ship v2"
    assert "JWT auth" in m["body"]


def test_get_memory_unknown_raises(fake_claude_home) -> None:
    with pytest.raises(FileNotFoundError, match="No memory named"):
        _call(get_memory)(name="does-not-exist", project=fake_claude_home["slug"])


def test_search_memories_finds_body_match(fake_claude_home) -> None:
    hits = _call(search_memories)(query="roadmap", project=fake_claude_home["slug"])
    assert len(hits) == 1
    assert hits[0]["name"] == "the-plan"


def test_search_memories_empty_query(fake_claude_home) -> None:
    assert _call(search_memories)(query="  ", project=fake_claude_home["slug"]) == []


# ---- subagent tools ----


def test_list_subagents_returns_hidden_agents(fake_claude_home) -> None:
    subs = _call(list_subagents)(
        session_id=fake_claude_home["session_id"], project=fake_claude_home["slug"]
    )
    assert len(subs) == 1
    assert subs[0]["agent_id"] == "deadbeef01"
    assert subs[0]["agent_type"] == "Explore"
    assert subs[0]["tool_use_id"] == "toolu_x"


def test_get_subagent_renders_transcript(fake_claude_home) -> None:
    md = _call(get_subagent)(
        session_id=fake_claude_home["session_id"],
        agent_id="deadbeef01",
        project=fake_claude_home["slug"],
    )
    assert "audit the login route" in md
    assert "Subagent: Explore" in md


def test_get_subagent_unknown_raises(fake_claude_home) -> None:
    with pytest.raises(FileNotFoundError, match="No subagent"):
        _call(get_subagent)(
            session_id=fake_claude_home["session_id"],
            agent_id="zzzz",
            project=fake_claude_home["slug"],
        )


# ---- self_align + handoff ----


def test_self_align_returns_bounded_packet(fake_claude_home) -> None:
    p = _call(self_align)(project=fake_claude_home["slug"])
    assert set(p) >= {"memories", "recent_sessions", "session_hits", "suggested_next", "guidance"}
    assert p["recent_sessions"]  # the one fixture session
    assert any(m["name"] == "the-plan" for m in p["memories"])
    # no full bodies / transcripts in the packet
    assert all("body" not in m for m in p["memories"])


def test_self_align_with_query(fake_claude_home) -> None:
    p = _call(self_align)(query="JWT", project=fake_claude_home["slug"])
    assert any("JWT" in h["snippet"] for h in p["session_hits"])


def test_get_session_handoff(fake_claude_home) -> None:
    h = _call(get_session_handoff)(
        session_id=fake_claude_home["session_id"], project=fake_claude_home["slug"]
    )
    assert h["has_handoff"] is True
    assert "Next steps" in h["summary"]
