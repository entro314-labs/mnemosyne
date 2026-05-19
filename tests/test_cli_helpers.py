"""Tests for non-trivial CLI helpers (filename slugging, filters)."""

from __future__ import annotations

from mnemosyne.cli import (
    _apply_filters,
    _filename_for,
    _resolve_filenames,
    _slugify,
)
from mnemosyne.parser import SessionSummary


def _summary(
    session_id: str = "abc12345-0000-0000-0000-000000000000",
    ai_title: str | None = None,
    first_user_text: str | None = None,
    last_timestamp: str | None = None,
) -> SessionSummary:
    from pathlib import Path  # noqa: PLC0415

    return SessionSummary(
        session_id=session_id,
        path=Path(f"/fake/{session_id}.jsonl"),
        ai_title=ai_title,
        first_user_text=first_user_text,
        first_timestamp=last_timestamp,
        last_timestamp=last_timestamp,
        message_count=2,
        user_count=1,
        assistant_count=1,
        size_bytes=1000,
    )


# ---- _slugify ----


def test_slugify_basic() -> None:
    assert (
        _slugify("Fix Godot project initialization errors")
        == "fix-godot-project-initialization-errors"
    )


def test_slugify_strips_punctuation() -> None:
    assert _slugify("Can you do X? (please!)") == "can-you-do-x-please"


def test_slugify_collapses_whitespace_underscores_hyphens() -> None:
    assert _slugify("hello___world  --  foo") == "hello-world-foo"


def test_slugify_truncates() -> None:
    long_text = "a" * 200
    result = _slugify(long_text, max_len=50)
    assert len(result) <= 50


def test_slugify_returns_empty_for_no_alphanumerics() -> None:
    assert _slugify("---!!!  ___") == ""


# ---- _filename_for ----


def test_filename_for_uses_ai_title_when_present() -> None:
    s = _summary(ai_title="Fix the bug")
    assert _filename_for(s) == "fix-the-bug.md"


def test_filename_for_falls_back_to_first_user_text() -> None:
    s = _summary(ai_title=None, first_user_text="please help")
    assert _filename_for(s) == "please-help.md"


def test_filename_for_falls_back_to_session_id() -> None:
    s = _summary(ai_title=None, first_user_text=None)
    assert _filename_for(s) == "abc12345-0000-0000-0000-000000000000.md"


def test_filename_for_uses_format_extension() -> None:
    s = _summary(ai_title="hi")
    assert _filename_for(s, fmt="markdown") == "hi.md"
    assert _filename_for(s, fmt="jsonl") == "hi.jsonl"
    assert _filename_for(s, fmt="plain") == "hi.txt"


# ---- _resolve_filenames ----


def test_resolve_filenames_no_collision() -> None:
    summaries = [
        _summary(session_id="aaa", ai_title="foo"),
        _summary(session_id="bbb", ai_title="bar"),
    ]
    out = _resolve_filenames(summaries)
    assert out == {"aaa": "foo.md", "bbb": "bar.md"}


def test_resolve_filenames_disambiguates_collisions() -> None:
    summaries = [
        _summary(session_id="aaaaaaaa-foo", ai_title="same title"),
        _summary(session_id="bbbbbbbb-foo", ai_title="same title"),
    ]
    out = _resolve_filenames(summaries)
    assert out["aaaaaaaa-foo"] == "same-title-aaaaaaaa.md"
    assert out["bbbbbbbb-foo"] == "same-title-bbbbbbbb.md"


# ---- _apply_filters ----


def test_filter_since_keeps_only_newer() -> None:
    sessions = [
        _summary(session_id="a", last_timestamp="2026-04-01T00:00:00Z"),
        _summary(session_id="b", last_timestamp="2026-05-15T00:00:00Z"),
    ]
    out = _apply_filters(sessions, since="2026-05-01", until=None, matching=None)
    assert [s.session_id for s in out] == ["b"]


def test_filter_until_keeps_only_older() -> None:
    sessions = [
        _summary(session_id="a", last_timestamp="2026-04-01T00:00:00Z"),
        _summary(session_id="b", last_timestamp="2026-05-15T00:00:00Z"),
    ]
    out = _apply_filters(sessions, since=None, until="2026-05-01", matching=None)
    assert [s.session_id for s in out] == ["a"]


def test_filter_matching_regex_on_title_and_first_prompt() -> None:
    sessions = [
        _summary(session_id="a", ai_title="Fix Godot bug"),
        _summary(session_id="b", ai_title="Add Rust feature"),
        _summary(session_id="c", ai_title=None, first_user_text="check the godot scene"),
    ]
    out = _apply_filters(sessions, since=None, until=None, matching=r"(?i)godot")
    assert {s.session_id for s in out} == {"a", "c"}


def test_filter_no_filters_returns_all() -> None:
    sessions = [_summary(session_id="a"), _summary(session_id="b")]
    assert len(_apply_filters(sessions, since=None, until=None, matching=None)) == 2


def test_filters_combine_with_and() -> None:
    sessions = [
        _summary(session_id="a", ai_title="Godot", last_timestamp="2026-04-01T00:00:00Z"),
        _summary(session_id="b", ai_title="Godot", last_timestamp="2026-05-15T00:00:00Z"),
        _summary(session_id="c", ai_title="Rust", last_timestamp="2026-05-15T00:00:00Z"),
    ]
    out = _apply_filters(sessions, since="2026-05-01", until=None, matching=r"(?i)godot")
    assert [s.session_id for s in out] == ["b"]
