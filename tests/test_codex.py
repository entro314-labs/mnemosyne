"""Tests for codex.py — the Codex CLI (~/.codex) ingestion adapter."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from mnemosyne.codex import (
    codex_available,
    codex_first_user_text,
    codex_memory_summary,
    codex_rollout_summaries,
    codex_sessions_for,
    load_session_index,
    read_codex_meta,
    read_codex_rollout_summary,
    read_codex_session,
    resolve_codex_session,
    summarize_codex_session,
)
from mnemosyne.parser import Message, TextBlock, ThinkingBlock, ToolResultBlock, ToolUseBlock

if TYPE_CHECKING:
    from pathlib import Path

SID = "019f0000-0000-7000-8000-000000000001"
OTHER_SID = "019f9999-0000-7000-8000-000000000002"
CWD = "/work/proj"


def _record(kind: str, payload: dict[str, Any], ts: str = "2026-07-01T10:00:00.000Z") -> str:
    return json.dumps({"timestamp": ts, "type": kind, "payload": payload})


def _rollout_lines(sid: str, cwd: str) -> list[str]:
    return [
        _record(
            "session_meta",
            {
                "id": sid,
                "timestamp": "2026-07-01T10:00:00.000Z",
                "cwd": cwd,
                "originator": "codex_cli",
                "cli_version": "1.0.0",
            },
        ),
        # Injected environment wrapper — never conversational content.
        _record(
            "response_item",
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "<environment_context>x</environment_context>"}
                ],
            },
        ),
        # The real prompt, inside Codex's IDE wrapper.
        _record(
            "response_item",
            {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "# Context from my IDE setup:\n\n## Active file: a.py\n\n"
                        "## My request for Codex:\nfix the login bug\n",
                    }
                ],
            },
            ts="2026-07-01T10:00:01.000Z",
        ),
        # event_msg duplicates the user message — must be skipped.
        _record("event_msg", {"type": "user_message", "message": "fix the login bug"}),
        _record("turn_context", {"cwd": cwd, "model": "gpt-5.1-codex", "effort": "high"}),
        _record(
            "response_item",
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "Plan the fix"}]},
        ),
        # Encrypted reasoning (empty summary) — must be dropped.
        _record("response_item", {"type": "reasoning", "summary": [], "encrypted_content": "xx"}),
        _record(
            "response_item",
            {
                "type": "function_call",
                "name": "exec_command",
                "arguments": json.dumps({"cmd": "pytest -q"}),
                "call_id": "call_1",
            },
        ),
        _record(
            "response_item",
            {"type": "function_call_output", "call_id": "call_1", "output": "1 passed"},
        ),
        _record(
            "response_item",
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Done — login fixed."}],
            },
            ts="2026-07-01T10:00:09.000Z",
        ),
    ]


@pytest.fixture
def codex_home(tmp_path: Path) -> Path:
    home = tmp_path / ".codex"
    day = home / "sessions" / "2026" / "07" / "01"
    day.mkdir(parents=True)
    (day / f"rollout-2026-07-01T10-00-00-{SID}.jsonl").write_text(
        "\n".join(_rollout_lines(SID, CWD)) + "\n", encoding="utf-8"
    )
    (day / f"rollout-2026-07-01T11-00-00-{OTHER_SID}.jsonl").write_text(
        "\n".join(_rollout_lines(OTHER_SID, "/work/elsewhere")) + "\n", encoding="utf-8"
    )
    (home / "session_index.jsonl").write_text(
        json.dumps({"id": SID, "thread_name": "Fix login bug", "updated_at": "2026-07-01"}) + "\n",
        encoding="utf-8",
    )
    memories = home / "memories"
    summaries = memories / "rollout_summaries"
    summaries.mkdir(parents=True)
    (memories / "memory_summary.md").write_text(
        "v1\n\n## User Profile\n\nLikes evidence-backed fixes.\n", encoding="utf-8"
    )
    (summaries / "2026-07-01T10-00-00-abcd-fix_login.md").write_text(
        f"thread_id: {SID}\nupdated_at: 2026-07-01T10:10:00+00:00\n"
        f"rollout_path: /x/rollout.jsonl\ncwd: {CWD}\ngit_branch: main\n\n"
        "# Fixed the login bug\n\nDetails of the fix.\n",
        encoding="utf-8",
    )
    (summaries / "2026-07-02T09-00-00-efgh-other_project.md").write_text(
        f"thread_id: {OTHER_SID}\nupdated_at: 2026-07-02T09:10:00+00:00\n"
        "cwd: /work/elsewhere\n\n# Other work\n\nBody.\n",
        encoding="utf-8",
    )
    return home


def test_codex_available(tmp_path: Path, codex_home: Path) -> None:
    assert codex_available(codex_home) is True
    assert codex_available(tmp_path / "nope") is False


def test_read_codex_meta(codex_home: Path) -> None:
    path = next(iter(sorted((codex_home / "sessions").rglob(f"*{SID}.jsonl"))))
    meta = read_codex_meta(path)
    assert meta is not None
    assert meta.session_id == SID
    assert meta.cwd == CWD
    assert meta.cli_version == "1.0.0"


def test_sessions_for_cwd_filters_and_titles(tmp_path: Path, codex_home: Path) -> None:
    proj = tmp_path / "work-proj"
    proj.mkdir()
    # cwd matching is on the recorded string; use the recorded path directly.
    metas = codex_sessions_for(None, codex_home)
    assert {m.session_id for m in metas} == {SID, OTHER_SID}
    mine = [m for m in metas if m.cwd == CWD]
    assert len(mine) == 1
    assert mine[0].thread_name == "Fix login bug"


def test_sessions_for_specific_cwd(codex_home: Path, tmp_path: Path) -> None:
    # Build a real directory whose resolved path is recorded in a fresh rollout.
    real = tmp_path / "realproj"
    real.mkdir()
    day = codex_home / "sessions" / "2026" / "07" / "02"
    day.mkdir(parents=True)
    sid = "019f0000-0000-7000-8000-000000000003"
    (day / f"rollout-2026-07-02T08-00-00-{sid}.jsonl").write_text(
        "\n".join(_rollout_lines(sid, str(real.resolve()))) + "\n", encoding="utf-8"
    )
    metas = codex_sessions_for(real, codex_home)
    assert [m.session_id for m in metas] == [sid]


def test_read_codex_session_event_mapping(codex_home: Path) -> None:
    path = resolve_codex_session(SID, codex_home)
    events = read_codex_session(path)
    messages = [e for e in events if isinstance(e, Message)]

    # env-context wrapper skipped; event_msg duplicate skipped.
    user_texts = [
        b.text for m in messages if m.role == "user" for b in m.blocks if isinstance(b, TextBlock)
    ]
    assert len(user_texts) == 1
    assert "fix the login bug" in user_texts[0]

    thinking = [b for m in messages for b in m.blocks if isinstance(b, ThinkingBlock)]
    assert [t.text for t in thinking] == ["Plan the fix"]  # encrypted reasoning dropped

    tool_uses = [b for m in messages for b in m.blocks if isinstance(b, ToolUseBlock)]
    assert len(tool_uses) == 1
    assert tool_uses[0].name == "exec_command"
    assert tool_uses[0].input == {"cmd": "pytest -q"}

    results = [b for m in messages for b in m.blocks if isinstance(b, ToolResultBlock)]
    assert [r.content for r in results] == ["1 passed"]
    assert results[0].tool_use_id == "call_1"

    # turn_context model stamps subsequent assistant messages.
    assistant = [m for m in messages if m.role == "assistant" and m.blocks]
    assert any(m.model == "gpt-5.1-codex" for m in assistant)


def test_summarize_codex_session(codex_home: Path) -> None:
    path = resolve_codex_session(SID, codex_home)
    s = summarize_codex_session(path, load_session_index(codex_home))
    assert s.session_id == SID
    assert s.ai_title == "Fix login bug"
    assert s.first_user_text == "fix the login bug"
    assert (s.user_count, s.assistant_count) == (1, 1)
    assert s.first_timestamp and s.last_timestamp and s.first_timestamp < s.last_timestamp


def test_resolve_codex_session_prefix_contract(codex_home: Path) -> None:
    assert resolve_codex_session(SID[:12], codex_home).stem.endswith(SID)
    with pytest.raises(ValueError, match="matches 2"):
        resolve_codex_session("019f", codex_home)
    with pytest.raises(FileNotFoundError):
        resolve_codex_session("ffffffff", codex_home)


def test_rollout_summaries_filter_by_cwd(codex_home: Path, tmp_path: Path) -> None:
    every = codex_rollout_summaries(codex_home)
    # Newest first by updated_at.
    assert [s.updated_at for s in every] == [
        "2026-07-02T09:10:00+00:00",
        "2026-07-01T10:10:00+00:00",
    ]
    # cwd filter needs a real dir whose resolved path matches the recorded one —
    # the fixture cwd is synthetic, so filter on the string via the parsed field.
    mine = [s for s in every if s.cwd == CWD]
    assert len(mine) == 1
    assert mine[0].title == "Fixed the login bug"
    assert mine[0].thread_id == SID


def test_read_rollout_summary_by_prefix_and_name(codex_home: Path) -> None:
    body = read_codex_rollout_summary(SID[:12], codex_home)
    assert body["title"] == "Fixed the login bug"
    assert "Details of the fix" in body["body"]
    by_name = read_codex_rollout_summary("2026-07-01T10-00-00-abcd-fix_login.md", codex_home)
    assert by_name["thread_id"] == SID
    with pytest.raises(FileNotFoundError):
        read_codex_rollout_summary("zzz", codex_home)


def test_codex_memory_summary_caps(codex_home: Path, tmp_path: Path) -> None:
    full = codex_memory_summary(codex_home)
    assert full is not None and "User Profile" in full
    capped = codex_memory_summary(codex_home, max_chars=10)
    assert capped is not None and len(capped) < 40 and "truncated" in capped
    assert codex_memory_summary(tmp_path / "empty") is None


def test_first_user_text_unwraps_ide_marker() -> None:
    assert codex_first_user_text("## My request for Codex:\n  do it  ") == "do it"
    assert codex_first_user_text("plain prompt") == "plain prompt"
