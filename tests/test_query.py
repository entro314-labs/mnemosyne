"""Tests for query.py — the shared recall/search primitives."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from mnemosyne.config import Settings
from mnemosyne.query import (
    all_project_dirs,
    fit_packet,
    memory_detail,
    memory_entries,
    recent_sessions,
    search_memories,
    search_sessions,
    self_align,
    session_handoff,
    session_summary_dict,
)

SESSION_ID = "abc12345-0000-0000-0000-000000000000"

if TYPE_CHECKING:
    from pathlib import Path


def _session_jsonl(user_text: str, asst_text: str, title: str) -> str:
    records = [
        {
            "type": "user",
            "uuid": "u1",
            "parentUuid": None,
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {"role": "user", "content": [{"type": "text", "text": user_text}]},
        },
        {
            "type": "assistant",
            "uuid": "a1",
            "parentUuid": "u1",
            "timestamp": "2026-01-01T00:00:05Z",
            "message": {
                "role": "assistant",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": asst_text}],
            },
        },
        {"type": "ai-title", "aiTitle": title},
    ]
    return "\n".join(json.dumps(r) for r in records) + "\n"


def _make_project(tmp_path: Path) -> Path:
    pd = tmp_path / "-proj"
    pd.mkdir()
    (pd / f"{SESSION_ID}.jsonl").write_text(
        _session_jsonl("investigate the auth flow", "Auth uses JWT 24h expiry.", "Auth flow"),
        encoding="utf-8",
    )
    # Handoff digest for that session (Claude Code writes these on compaction).
    sm = pd / SESSION_ID / "session-memory"
    sm.mkdir(parents=True)
    (sm / "summary.md").write_text(
        "# Session Title\n\nAuth flow\n\n# Next steps\n\nWire the refresh token.\n",
        encoding="utf-8",
    )
    mem = pd / "memory"
    mem.mkdir()
    (mem / "the-plan.md").write_text(
        '---\nname: the-plan\ndescription: "ship v2"\nmetadata:\n  type: project\n---\n'
        "The roadmap is to ship v2 with JWT auth.\n",
        encoding="utf-8",
    )
    (mem / "the-pivot.md").write_text(
        "---\nname: the-pivot\n---\nWe pivoted to Postgres.\n", encoding="utf-8"
    )
    return pd


def test_recent_sessions_headers(tmp_path: Path) -> None:
    pd = _make_project(tmp_path)
    rows = recent_sessions(pd, 5, Settings())
    assert len(rows) == 1
    assert rows[0]["title"] == "Auth flow"
    assert rows[0]["user_count"] == 1
    assert rows[0]["assistant_count"] == 1


def test_memory_entries(tmp_path: Path) -> None:
    pd = _make_project(tmp_path)
    entries = memory_entries(pd)
    names = {e["name"] for e in entries}
    assert names == {"the-plan", "the-pivot"}
    plan = next(e for e in entries if e["name"] == "the-plan")
    assert plan["type"] == "project"
    assert "body" not in plan  # headers only, no bodies


def test_memory_detail_returns_body(tmp_path: Path) -> None:
    pd = _make_project(tmp_path)
    m = memory_detail(pd, "the-plan")
    assert m["description"] == "ship v2"
    assert "JWT auth" in m["body"]


def test_memory_detail_prefix_and_errors(tmp_path: Path) -> None:
    pd = _make_project(tmp_path)
    # unique prefix
    assert memory_detail(pd, "the-pl")["name"] == "the-plan"
    with pytest.raises(FileNotFoundError, match="No memory named"):
        memory_detail(pd, "nope")
    # "the-p" is ambiguous (the-plan / the-pivot)
    with pytest.raises(ValueError, match="matches 2 memories"):
        memory_detail(pd, "the-p")


def test_search_memories(tmp_path: Path) -> None:
    pd = _make_project(tmp_path)
    hits = search_memories([pd], "roadmap", 10)
    assert [h["name"] for h in hits] == ["the-plan"]
    assert search_memories([pd], "   ", 10) == []


def test_search_sessions(tmp_path: Path) -> None:
    pd = _make_project(tmp_path)
    hits = search_sessions([pd], "JWT", Settings())
    assert len(hits) == 1
    assert "JWT" in hits[0]["snippet"]
    assert hits[0]["title"] == "Auth flow"
    assert search_sessions([pd], "nonexistent-string", Settings()) == []


def test_session_summary_dict_shape(tmp_path: Path) -> None:
    from mnemosyne.parser import summarize_session  # noqa: PLC0415

    pd = _make_project(tmp_path)
    path = next(pd.glob("*.jsonl"))
    d = session_summary_dict(summarize_session(path))
    assert d["title"] == "Auth flow"
    assert d["project_slug"] is None  # no entry passed


def test_all_project_dirs_filters_slug_dirs(tmp_path: Path) -> None:
    (tmp_path / "-proj-a").mkdir()
    (tmp_path / "-proj-b").mkdir()
    (tmp_path / "not-a-slug").mkdir()  # no leading dash
    (tmp_path / "file.txt").write_text("x", encoding="utf-8")
    dirs = {p.name for p in all_project_dirs(tmp_path)}
    assert dirs == {"-proj-a", "-proj-b"}
    assert all_project_dirs(tmp_path / "missing") == []


def test_session_handoff_present(tmp_path: Path) -> None:
    pd = _make_project(tmp_path)
    h = session_handoff(pd, SESSION_ID)
    assert h["has_handoff"] is True
    assert "Wire the refresh token" in h["summary"]
    assert h["files"] == ["summary.md"]


def test_session_handoff_absent(tmp_path: Path) -> None:
    pd = _make_project(tmp_path)
    h = session_handoff(pd, "no-such-session")
    assert h["has_handoff"] is False
    assert h["summary"] is None
    assert h["files"] == []


def test_self_align_no_query_is_index_plus_recent(tmp_path: Path) -> None:
    pd = _make_project(tmp_path)
    packet = self_align([pd], Settings())
    assert {m["name"] for m in packet["memories"]} == {"the-plan", "the-pivot"}
    assert len(packet["recent_sessions"]) == 1
    assert packet["session_hits"] == []  # no transcript search without a query
    # carries no full bodies / transcripts
    assert all("body" not in m for m in packet["memories"])
    assert packet["suggested_next"]  # at least one follow-up suggested
    assert packet["guidance"]


def test_self_align_with_query_adds_hits(tmp_path: Path) -> None:
    pd = _make_project(tmp_path)
    packet = self_align([pd], Settings(), query="JWT")
    assert any("JWT" in h["snippet"] for h in packet["session_hits"])
    assert any(m["name"] == "the-plan" for m in packet["memories"])
    calls = [s["call"] for s in packet["suggested_next"]]
    assert any(c.startswith("get_memory(") for c in calls)
    assert any("get_session_handoff(" in c for c in calls)


def test_fit_packet_trims_to_budget(tmp_path: Path) -> None:
    pd = _make_project(tmp_path)
    packet = self_align([pd], Settings(), query="JWT")
    import json  # noqa: PLC0415

    tiny = fit_packet(packet, 400)
    assert len(json.dumps(tiny, ensure_ascii=False)) <= 400 or len(tiny["memories"]) == 1
    # never trims below the top memory match
    assert len(tiny["memories"]) >= 1
    assert tiny["guidance"] == packet["guidance"]
