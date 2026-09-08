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

    tiny = fit_packet(packet, 800)
    assert len(json.dumps(tiny, ensure_ascii=False)) <= 800
    assert len(tiny["memories"]) >= 1
    assert tiny["guidance"] == packet["guidance"]


def test_fit_packet_rejects_budget_smaller_than_minimum_policy_packet(tmp_path: Path) -> None:
    packet = self_align([_make_project(tmp_path)], Settings(), query="JWT")
    with pytest.raises(ValueError, match="too small"):
        fit_packet(packet, 100)


# ---- scope: the working tree that defines an archive dir (slug collisions) ----


def _session_in(path: Path, cwd: Path, text: str) -> None:
    path.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "u1",
                "timestamp": "2026-01-01T00:00:00Z",
                "cwd": str(cwd),
                "message": {"role": "user", "content": [{"type": "text", "text": text}]},
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _collision(tmp_path: Path) -> tuple[Path, Path, Path]:
    """One archive dir holding sessions from two sibling trees (foo.bar / foo_bar)."""
    archive = tmp_path / "-a-foo-bar"
    archive.mkdir()
    mine = tmp_path / "foo.bar"
    mine.mkdir()
    theirs = tmp_path / "foo_bar"
    theirs.mkdir()
    _session_in(archive / "aaaa0000-mine.jsonl", mine, "my work on auth")
    _session_in(archive / "bbbb0000-theirs.jsonl", theirs, "their work on auth")
    return archive, mine, theirs


def _registry(archive: Path, local_path: Path | None) -> Settings:
    from mnemosyne.config import ProjectEntry  # noqa: PLC0415

    entry = ProjectEntry(slug=archive.name, local_path=str(local_path) if local_path else None)
    return Settings(projects={archive.name: entry})


@pytest.mark.parametrize(
    ("registry_tree", "override", "expected"),
    [
        # An explicit tree is authoritative, whatever the registry says.
        ("theirs", "mine", {"aaaa0000-mine.jsonl"}),
        (None, "mine", {"aaaa0000-mine.jsonl"}),
        # An explicit None takes the directory at face value.
        ("mine", "none", {"aaaa0000-mine.jsonl", "bbbb0000-theirs.jsonl"}),
        # No override: the registry path scopes the directory.
        ("mine", None, {"aaaa0000-mine.jsonl"}),
        ("theirs", None, {"bbbb0000-theirs.jsonl"}),
        # Unregistered slug: unfiltered.
        (None, None, {"aaaa0000-mine.jsonl", "bbbb0000-theirs.jsonl"}),
    ],
)
def test_scoped_session_files_scope_precedence(
    tmp_path: Path, registry_tree: str | None, override: str | None, expected: set[str]
) -> None:
    from mnemosyne.query import scoped_session_files  # noqa: PLC0415

    archive, mine, theirs = _collision(tmp_path)
    trees = {"mine": mine, "theirs": theirs}
    settings = _registry(archive, trees[registry_tree] if registry_tree else None)
    project_paths: dict[str, Path | None] | None = None
    if override == "none":
        project_paths = {archive.name: None}
    elif override is not None:
        project_paths = {archive.name: trees[override]}
    files = scoped_session_files(archive, settings, project_paths=project_paths)
    assert {p.name for p in files} == expected


def test_scoped_session_files_registry_path_falls_back_but_explicit_tree_does_not(
    tmp_path: Path,
) -> None:
    """A stale registry path must not empty a real archive; an explicit tree may."""
    from mnemosyne.query import scoped_session_files  # noqa: PLC0415

    archive, _, _ = _collision(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert len(scoped_session_files(archive, _registry(archive, elsewhere))) == 2
    assert scoped_session_files(archive, Settings(), project_paths={archive.name: elsewhere}) == []


def test_recall_surfaces_honor_the_explicit_tree(tmp_path: Path) -> None:
    """recent / search / self_align all scope by the caller's tree, not the registry."""
    archive, mine, theirs = _collision(tmp_path)
    settings = _registry(archive, theirs)  # registry points at the sibling tree
    paths = {archive.name: mine}

    recent = recent_sessions(archive, 10, settings, project_paths=paths)
    assert [r["session_id"] for r in recent] == ["aaaa0000-mine"]

    hits = search_sessions([archive], "work on auth", settings, project_paths=paths)
    assert [h["session_id"] for h in hits] == ["aaaa0000-mine"]

    packet = self_align([archive], settings, query="auth", project_paths=paths)
    assert [r["session_id"] for r in packet["recent_sessions"]] == ["aaaa0000-mine"]
    assert [h["session_id"] for h in packet["session_hits"]] == ["aaaa0000-mine"]


def test_local_paths_for_prefers_the_explicit_tree(tmp_path: Path) -> None:
    from mnemosyne.query import local_paths_for  # noqa: PLC0415

    archive, mine, theirs = _collision(tmp_path)
    settings = _registry(archive, theirs)
    assert local_paths_for([archive], settings) == [theirs]
    assert local_paths_for([archive], settings, project_paths={archive.name: mine}) == [mine]
    # Face value carries no tree: fall through to the registry.
    assert local_paths_for([archive], settings, project_paths={archive.name: None}) == [theirs]


# ---- headers are pointers, not content: a huge first prompt must not empty the packet ----


def test_long_first_prompt_is_excerpted_in_headers_but_not_in_summaries(tmp_path: Path) -> None:
    from mnemosyne.parser import summarize_session  # noqa: PLC0415
    from mnemosyne.query import HEADER_EXCERPT_CHARS  # noqa: PLC0415

    pd = tmp_path / "-proj"
    pd.mkdir()
    long_prompt = "audit everything " * 700  # ~12k chars, no AI title
    (pd / f"{SESSION_ID}.jsonl").write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "u1",
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {"role": "user", "content": [{"type": "text", "text": long_prompt}]},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    summary = summarize_session(pd / f"{SESSION_ID}.jsonl")
    assert summary.first_user_text is not None
    assert len(summary.first_user_text) > HEADER_EXCERPT_CHARS  # the source keeps it all

    header = session_summary_dict(summary)
    assert len(header["first_prompt"]) <= HEADER_EXCERPT_CHARS
    assert header["first_prompt"].endswith("…")
    assert len(header["title"]) <= HEADER_EXCERPT_CHARS

    packet = fit_packet(self_align([pd], Settings()), 6000)
    assert [r["session_id"] for r in packet["recent_sessions"]] == [SESSION_ID]
    assert any("get_session_handoff(" in s["call"] for s in packet["suggested_next"])


# ---- project counts and excluded-session reporting follow the same scope ----


def test_projects_with_sessions_counts_the_scoped_set(tmp_path: Path) -> None:
    from mnemosyne.query import excluded_session_count, projects_with_sessions  # noqa: PLC0415

    archive, mine, theirs = _collision(tmp_path)
    settings = _registry(archive, theirs)
    # Registry-scoped: the registered tree owns one session.
    assert [(e.slug, n) for e, n in projects_with_sessions(settings, tmp_path)] == [
        (archive.name, 1)
    ]
    # The cwd override wins for its own slug and counts the other session.
    rows = projects_with_sessions(settings, tmp_path, project_paths={archive.name: mine})
    assert [(e.slug, n) for e, n in rows] == [(archive.name, 1)]
    assert excluded_session_count(archive, settings, project_paths={archive.name: mine}) == 1
    assert excluded_session_count(archive, settings, project_paths={archive.name: None}) == 0


def test_self_align_reports_excluded_sessions(tmp_path: Path) -> None:
    archive, mine, _ = _collision(tmp_path)
    packet = self_align([archive], Settings(), project_paths={archive.name: mine})
    assert packet["excluded_sessions"] == 1
    assert [r["session_id"] for r in packet["recent_sessions"]] == ["aaaa0000-mine"]
    assert fit_packet(packet, 6000)["excluded_sessions"] == 1
