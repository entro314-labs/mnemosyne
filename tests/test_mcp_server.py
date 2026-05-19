"""Tests for mcp_server.py — verify each tool returns the expected shape.

The MCP tools are plain functions wrapped by FastMCP. We unwrap via
``.fn`` (FastMCP exposes the original callable) so we can call them
directly without a transport.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from claude_session_export import config, mcp_server, parser
from claude_session_export.mcp_server import (
    get_session,
    get_session_summary,
    list_projects,
    list_sessions,
    recall_recent,
    search_sessions,
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
    session_path = project_dir / "abc12345-0000-0000-0000-000000000000.jsonl"
    session_path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

    # Stub the module-level CLAUDE_PROJECTS in every place that reads it.
    monkeypatch.setattr(config, "CLAUDE_PROJECTS", claude_projects)
    monkeypatch.setattr(mcp_server, "CLAUDE_PROJECTS", claude_projects)
    monkeypatch.setattr(parser, "project_dir_for_cwd", lambda cwd: project_dir)
    # Force load_settings to use a temp config file so we don't pollute the real one.
    config_path = tmp_path / "config.toml"
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)

    return {"slug": slug, "session_id": "abc12345", "project_dir": project_dir}


def _call(tool):
    """FastMCP wraps the function; unwrap to call directly in tests."""
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


def test_unknown_session_prefix_raises(fake_claude_home) -> None:
    with pytest.raises(FileNotFoundError, match="No session matching"):
        _call(get_session)(session_id="zzzzzzz", project=fake_claude_home["slug"])
