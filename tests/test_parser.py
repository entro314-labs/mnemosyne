"""Tests for parser.py — text normalization + event filtering."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from claude_session_export.parser import (
    Attachment,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    _coerce_tool_result_content,
    _is_filler_assistant,
    _parse_blocks,
    _strip_reminder_wrappers,
    _unescape_if_encoded,
    iter_events,
)

# ---- _unescape_if_encoded ----


def test_unescape_fires_when_text_looks_serialized() -> None:
    text = r"hello\n\nworld\n\nthird paragraph\n\nfourth"
    out = _unescape_if_encoded(text)
    assert "\\n" not in out
    assert out.count("\n") == 6


def test_unescape_skips_text_with_real_newlines_already() -> None:
    text = "hello\n\nworld\n\nsomeone mentioned \\n in passing"
    # 4 real newlines, 1 escape → leave alone
    assert _unescape_if_encoded(text) == text


def test_unescape_skips_text_with_few_escapes() -> None:
    text = r"don't unescape me — \n alone is not a signal"
    assert _unescape_if_encoded(text) == text


def test_unescape_handles_backslash_escape_correctly() -> None:
    # \\n (a literal backslash followed by an 'n') must survive when surrounded
    # by enough \n escapes to trip the heuristic.
    text = r"\n\n\n\\n\n"  # 4 \n, 1 \\n
    out = _unescape_if_encoded(text)
    assert "\\n" in out  # the literal \n must remain
    assert out.count("\n") >= 4


def test_unescape_handles_other_common_escapes() -> None:
    text = r"a\nb\nc\nd\tquoted: \"yes\""
    out = _unescape_if_encoded(text)
    assert "\t" in out
    assert '"yes"' in out


# ---- _strip_reminder_wrappers ----


def test_strip_removes_system_reminder() -> None:
    text = "do the thing\n<system-reminder>noise</system-reminder>\nthanks"
    out = _strip_reminder_wrappers(text)
    assert "system-reminder" not in out
    assert "do the thing" in out
    assert "thanks" in out


def test_strip_removes_ide_selection() -> None:
    text = "real prompt\n<ide_selection>filename.py lines 1-10</ide_selection>"
    out = _strip_reminder_wrappers(text)
    assert "ide_selection" not in out
    assert "real prompt" in out


def test_strip_removes_ide_opened_file_and_command_family() -> None:
    text = (
        "<ide_opened_file>x.py</ide_opened_file>"
        "<command-name>/export</command-name>"
        "<local-command-stdout>ok</local-command-stdout>"
        "real content"
    )
    out = _strip_reminder_wrappers(text).strip()
    assert out == "real content"


def test_task_notification_unwraps_summary_and_result() -> None:
    text = (
        "<task-notification>"
        "<task-id>abc123</task-id>"
        "<tool-use-id>toolu_xyz</tool-use-id>"
        "<status>completed</status>"
        '<summary>Agent "Research X" completed</summary>'
        "<result>The findings are: 42</result>"
        "<usage><total_tokens>100</total_tokens></usage>"
        "</task-notification>"
    )
    out = _strip_reminder_wrappers(text)
    assert "task-id" not in out
    assert "tool-use-id" not in out
    assert "<usage>" not in out
    assert "<total_tokens>" not in out
    assert '🤖 Agent: Agent "Research X" completed' in out
    assert "The findings are: 42" in out


def test_task_notification_with_failed_status_shown() -> None:
    text = (
        "<task-notification>"
        "<status>failed</status>"
        "<summary>Agent crashed</summary>"
        "<result>boom</result>"
        "</task-notification>"
    )
    out = _strip_reminder_wrappers(text)
    assert "(failed)" in out
    assert "boom" in out


# ---- _is_filler_assistant ----


def test_filler_caught_when_stop_sequence_and_text_only() -> None:
    obj = {
        "type": "assistant",
        "message": {
            "stop_reason": "stop_sequence",
            "content": [{"type": "text", "text": "No response requested."}],
        },
    }
    assert _is_filler_assistant(obj) is True


def test_filler_caught_for_any_stop_sequence_text_payload() -> None:
    # The rule is project-agnostic — the stop_reason is the signal, not the
    # text. An arbitrary error string still gets caught.
    obj = {
        "type": "assistant",
        "message": {
            "stop_reason": "stop_sequence",
            "content": [{"type": "text", "text": "API Error: 529 Overloaded."}],
        },
    }
    assert _is_filler_assistant(obj) is True


def test_filler_not_caught_when_end_turn() -> None:
    obj = {
        "type": "assistant",
        "message": {
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "No response requested."}],
        },
    }
    assert _is_filler_assistant(obj) is False


def test_filler_not_caught_when_has_tool_use() -> None:
    obj = {
        "type": "assistant",
        "message": {
            "stop_reason": "stop_sequence",
            "content": [
                {"type": "text", "text": "checking…"},
                {"type": "tool_use", "id": "x", "name": "Bash", "input": {}},
            ],
        },
    }
    assert _is_filler_assistant(obj) is False


# ---- _coerce_tool_result_content ----


def test_tool_result_str_passthrough() -> None:
    assert _coerce_tool_result_content("hello") == "hello"


def test_tool_result_list_of_text_joins() -> None:
    out = _coerce_tool_result_content(
        [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
    )
    assert out == "a\nb"


def test_tool_result_tool_reference_renders_marker() -> None:
    out = _coerce_tool_result_content([{"type": "tool_reference", "tool_name": "TodoWrite"}])
    assert "TodoWrite" in out
    assert "used tool" in out


def test_tool_result_none_returns_empty() -> None:
    assert _coerce_tool_result_content(None) == ""


# ---- _parse_blocks ----


def test_parse_blocks_handles_str_content() -> None:
    blocks = _parse_blocks("plain string content")
    assert blocks == [TextBlock(text="plain string content")]


def test_parse_blocks_skips_empty_thinking() -> None:
    blocks = _parse_blocks(
        [
            {"type": "thinking", "thinking": "", "signature": "abc"},
            {"type": "text", "text": "real"},
        ]
    )
    assert len(blocks) == 1
    assert isinstance(blocks[0], TextBlock)


def test_parse_blocks_keeps_non_empty_thinking() -> None:
    blocks = _parse_blocks([{"type": "thinking", "thinking": "let me think", "signature": "abc"}])
    assert len(blocks) == 1
    assert isinstance(blocks[0], ThinkingBlock)


def test_parse_blocks_full_round_trip() -> None:
    blocks = _parse_blocks(
        [
            {"type": "text", "text": "hi"},
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}},
            {
                "type": "tool_result",
                "tool_use_id": "t1",
                "content": "ok",
                "is_error": False,
            },
        ]
    )
    assert len(blocks) == 3
    assert isinstance(blocks[0], TextBlock)
    assert isinstance(blocks[1], ToolUseBlock)
    assert isinstance(blocks[2], ToolResultBlock)
    assert blocks[1].input == {"command": "ls"}
    assert blocks[2].content == "ok"


# ---- iter_events end-to-end (with a fixture JSONL) ----


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def test_iter_events_skips_isMeta_user(tmp_path: Path) -> None:
    f = tmp_path / "s.jsonl"
    _write_jsonl(
        f,
        [
            {
                "type": "user",
                "isMeta": True,
                "uuid": "1",
                "message": {
                    "content": [{"type": "text", "text": "Continue from where you left off."}]
                },
            },
            {
                "type": "user",
                "uuid": "2",
                "message": {"content": [{"type": "text", "text": "real prompt"}]},
            },
        ],
    )
    events = list(iter_events(f))
    assert len(events) == 1
    assert isinstance(events[0], Message)
    assert events[0].uuid == "2"


def test_iter_events_skips_stop_sequence_filler(tmp_path: Path) -> None:
    f = tmp_path / "s.jsonl"
    _write_jsonl(
        f,
        [
            {
                "type": "assistant",
                "uuid": "1",
                "message": {
                    "stop_reason": "stop_sequence",
                    "content": [{"type": "text", "text": "API Error: anything"}],
                },
            },
            {
                "type": "assistant",
                "uuid": "2",
                "message": {
                    "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": "real response"}],
                },
            },
        ],
    )
    events = list(iter_events(f))
    assert len(events) == 1
    assert events[0].uuid == "2"


def test_iter_events_yields_attachment(tmp_path: Path) -> None:
    f = tmp_path / "s.jsonl"
    _write_jsonl(
        f,
        [
            {
                "type": "attachment",
                "uuid": "a1",
                "attachment": {"type": "hook_success", "content": "ok"},
            },
        ],
    )
    events = list(iter_events(f))
    assert len(events) == 1
    assert isinstance(events[0], Attachment)
    assert events[0].attachment_type == "hook_success"


def test_iter_events_ignores_bookkeeping(tmp_path: Path) -> None:
    f = tmp_path / "s.jsonl"
    _write_jsonl(
        f,
        [
            {"type": "queue-operation", "operation": "enqueue"},
            {"type": "ai-title", "aiTitle": "x"},
            {"type": "last-prompt", "lastPrompt": "x"},
            {"type": "file-history-snapshot", "messageId": "m"},
            {"type": "permission-mode", "mode": "x"},
            {"type": "system", "subtype": "stop_hook_summary"},
        ],
    )
    assert list(iter_events(f)) == []


@pytest.mark.parametrize(
    "bad_line",
    ["not json at all", "", "{incomplete"],
)
def test_iter_events_skips_malformed_lines(tmp_path: Path, bad_line: str) -> None:
    f = tmp_path / "s.jsonl"
    f.write_text(bad_line + "\n", encoding="utf-8")
    assert list(iter_events(f)) == []
