"""Tests for formats.py — JSONL + plain renderers + Turn collection."""

from __future__ import annotations

import json

from mnemosyne.formats import _strip_markdown, render_jsonl, render_plain
from mnemosyne.parser import Message, TextBlock, ToolResultBlock, ToolUseBlock
from mnemosyne.render import RenderOptions, Turn, collect_turns


def _msg(role: str, blocks: list, uuid: str = "u", ts: str = "2026-01-01T00:00:00Z") -> Message:
    return Message(uuid=uuid, parent_uuid=None, timestamp=ts, role=role, blocks=blocks)


# ---- collect_turns (the shared collector) ----


def test_collect_turns_coalesces_same_role() -> None:
    events = [
        _msg("user", [TextBlock("hi")], ts="t0"),
        _msg("assistant", [TextBlock("hello")], ts="t1"),
        _msg("assistant", [TextBlock("more")], ts="t2"),
        _msg("user", [TextBlock("ok")], ts="t3"),
    ]
    turns = collect_turns(events, RenderOptions(mode="transcript"))
    assert [t.role for t in turns] == ["user", "assistant", "user"]
    assert "hello" in turns[1].body
    assert "more" in turns[1].body  # coalesced
    assert turns[1].timestamp == "t1"  # earliest timestamp kept


def test_collect_turns_attaches_tool_result_to_prev_turn() -> None:
    events = [
        _msg(
            "assistant",
            [
                TextBlock("calling tool"),
                ToolUseBlock(id="t1", name="Bash", input={"command": "ls"}),
            ],
            ts="t0",
        ),
        _msg("user", [ToolResultBlock(tool_use_id="t1", content="foo.md\n")], ts="t1"),
        _msg("assistant", [TextBlock("done")], ts="t2"),
    ]
    turns = collect_turns(events, RenderOptions(mode="full"))
    # The user tool-result-only message should NOT create a separate "user" turn.
    assert [t.role for t in turns] == ["assistant"]
    # Both pieces of assistant text + the tool result should all be in the same turn.
    body = turns[0].body
    assert "calling tool" in body
    assert "foo.md" in body
    assert "done" in body


def test_collect_turns_in_transcript_drops_tools_entirely() -> None:
    events = [
        _msg(
            "assistant",
            [
                TextBlock("prose"),
                ToolUseBlock(id="t1", name="Bash", input={"command": "ls"}),
            ],
        ),
        _msg("user", [ToolResultBlock(tool_use_id="t1", content="result")]),
    ]
    turns = collect_turns(events, RenderOptions(mode="transcript"))
    assert len(turns) == 1
    assert turns[0].body == "prose"


# ---- render_jsonl ----


def test_jsonl_one_object_per_turn() -> None:
    events = [
        _msg("user", [TextBlock("a")], ts="t0"),
        _msg("assistant", [TextBlock("b")], ts="t1"),
    ]
    out = render_jsonl(events, session_id="abc", project_slug="-p", opts=RenderOptions())
    lines = out.strip().split("\n")
    assert len(lines) == 2
    first = json.loads(lines[0])
    second = json.loads(lines[1])
    # Core required shape (additions are tested separately)
    assert first["turn_index"] == 0
    assert first["role"] == "user"
    assert first["timestamp"] == "t0"
    assert first["text"] == "a"
    assert first["session_id"] == "abc"
    assert first["project_slug"] == "-p"
    assert second["role"] == "assistant"
    assert second["text"] == "b"


def test_jsonl_includes_turn_id_when_session_id_present() -> None:
    events = [_msg("user", [TextBlock("a")]), _msg("assistant", [TextBlock("b")])]
    out = render_jsonl(events, session_id="abc12345")
    lines = [json.loads(line) for line in out.strip().split("\n")]
    assert lines[0]["turn_id"] == "abc12345#0"
    assert lines[1]["turn_id"] == "abc12345#1"


def test_jsonl_omits_turn_id_without_session_id() -> None:
    events = [_msg("user", [TextBlock("a")])]
    out = render_jsonl(events)
    obj = json.loads(out.strip())
    assert "turn_id" not in obj
    assert "session_id" not in obj


def test_jsonl_includes_char_count() -> None:
    events = [_msg("user", [TextBlock("hello world")])]
    out = render_jsonl(events)
    obj = json.loads(out.strip())
    assert obj["char_count"] == len("hello world")


def test_jsonl_includes_project_path_when_provided() -> None:
    events = [_msg("user", [TextBlock("hi")])]
    out = render_jsonl(events, project_path="/Users/me/proj")
    obj = json.loads(out.strip())
    assert obj["project_path"] == "/Users/me/proj"


def test_jsonl_no_metadata_when_omitted() -> None:
    events = [_msg("user", [TextBlock("x")])]
    out = render_jsonl(events)
    obj = json.loads(out.strip())
    assert "session_id" not in obj
    assert "project_slug" not in obj


def test_jsonl_handles_empty_input() -> None:
    assert render_jsonl([]) == ""


# ---- render_plain ----


def test_plain_uses_role_brackets() -> None:
    events = [
        _msg("user", [TextBlock("hi")], ts="t0"),
        _msg("assistant", [TextBlock("hello")], ts="t1"),
    ]
    out = render_plain(events, title="My session", opts=RenderOptions())
    assert "My session" in out
    assert "=== USER (t0) ===" in out
    assert "=== ASSISTANT (t1) ===" in out
    assert "hi" in out
    assert "hello" in out


def test_plain_strips_markdown_fences_and_bold() -> None:
    raw = "**Bold text**\n```python\nprint('x')\n```\nmore"
    stripped = _strip_markdown(raw)
    assert "**" not in stripped
    assert "```" not in stripped
    assert "Bold text" in stripped
    assert "print('x')" in stripped


def test_plain_strips_tool_emoji_labels() -> None:
    # _compact_one_liner emits "<emoji> **<name>**" — emoji outside the bold.
    raw = "📄 **Read** `/foo.md`"
    stripped = _strip_markdown(raw)
    assert "📄" not in stripped
    assert "**" not in stripped
    assert "[Read]" in stripped


def test_plain_strips_decorator_emojis() -> None:
    raw = "🤖 🤖 👤 thinking 💭 done"
    stripped = _strip_markdown(raw)
    assert "🤖" not in stripped
    assert "👤" not in stripped
    assert "💭" not in stripped


def test_plain_render_round_trip_preserves_content() -> None:
    events = [
        _msg("user", [TextBlock("real prompt")]),
        _msg(
            "assistant",
            [
                TextBlock("here's the diff"),
                ToolUseBlock(id="t1", name="Edit", input={"file_path": "/a.py"}),
            ],
        ),
        _msg("user", [ToolResultBlock(tool_use_id="t1", content="ok")]),
    ]
    out = render_plain(events, opts=RenderOptions(mode="compact"))
    # User prompt makes it through
    assert "real prompt" in out
    # Assistant prose makes it through
    assert "here's the diff" in out
    # Tool label is plain (no asterisks, no emoji)
    assert "[Edit" in out
    assert "/a.py" in out


# ---- Turn dataclass sanity ----


def test_turn_is_dataclass_with_expected_fields() -> None:
    t = Turn(role="user", timestamp="2026-01-01T00:00:00Z", body="hi")
    assert t.role == "user"
    assert t.timestamp == "2026-01-01T00:00:00Z"
    assert t.body == "hi"
