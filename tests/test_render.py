"""Tests for render.py — modes + same-role coalescing + tool compaction."""

from __future__ import annotations

from mnemosyne.parser import (
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from mnemosyne.render import (
    RenderOptions,
    _coalesce_same_role,
    _scrub_ack,
    render_markdown,
)

# ---- _scrub_ack ----


def test_scrub_removes_file_update_ack() -> None:
    body = "The file /tmp/foo.md has been updated successfully. (file state is current)"
    assert _scrub_ack(body) == ""


def test_scrub_removes_file_create_ack() -> None:
    body = "File created successfully at: /tmp/foo.md"
    assert _scrub_ack(body) == ""


def test_scrub_removes_todo_ack() -> None:
    body = "Todos have been modified successfully. Ensure that you continue."
    assert _scrub_ack(body) == ""


def test_scrub_preserves_real_content() -> None:
    body = "Permissions Size User\n.rw-r--r-- 12K foo  bar.md"
    assert _scrub_ack(body) == body


# ---- _coalesce_same_role ----


def test_coalesce_merges_adjacent_assistants() -> None:
    chunks = [
        "### 🤖 **Assistant**  _(ts1)_\n\npara one",
        "### 🤖 **Assistant**  _(ts2)_\n\npara two",
        "### 🤖 **Assistant**  _(ts3)_\n\npara three",
    ]
    out = _coalesce_same_role(chunks)
    assert len(out) == 1
    assert "_(ts1)_" in out[0]
    assert "_(ts2)_" not in out[0]
    assert "para one" in out[0]
    assert "para two" in out[0]
    assert "para three" in out[0]


def test_coalesce_does_not_merge_across_user_turn() -> None:
    chunks = [
        "### 🤖 **Assistant**  _(t1)_\n\nfirst",
        "### 👤 **User**  _(t2)_\n\nuser prompt",
        "### 🤖 **Assistant**  _(t3)_\n\nsecond",
    ]
    assert len(_coalesce_same_role(chunks)) == 3


def test_coalesce_handles_no_headers() -> None:
    chunks = ["plain chunk", "another plain chunk"]
    # Neither has a role header — should not merge
    assert _coalesce_same_role(chunks) == chunks


# ---- render_markdown smoke ----


def _msg(role: str, blocks: list, uuid: str = "u", ts: str = "2026-01-01T00:00:00Z") -> Message:
    return Message(uuid=uuid, parent_uuid=None, timestamp=ts, role=role, blocks=blocks)


def test_transcript_drops_all_tools() -> None:
    events = [
        _msg("user", [TextBlock("hello")]),
        _msg(
            "assistant",
            [
                TextBlock("I'll run a command."),
                ToolUseBlock(id="t1", name="Bash", input={"command": "ls"}),
            ],
        ),
        _msg("user", [ToolResultBlock(tool_use_id="t1", content="file.md\n")]),
        _msg("assistant", [TextBlock("Done.")]),
    ]
    md = render_markdown(events, opts=RenderOptions(mode="transcript"))
    assert "Bash" not in md
    assert "Tool result" not in md
    assert "ls" not in md
    assert "I'll run a command." in md
    assert "Done." in md


def test_compact_renders_tool_oneliner() -> None:
    events = [
        _msg(
            "assistant",
            [
                ToolUseBlock(id="t1", name="Read", input={"file_path": "/tmp/x.md"}),
            ],
        ),
        _msg("user", [ToolResultBlock(tool_use_id="t1", content="A" * 1234)]),
    ]
    md = render_markdown(events, opts=RenderOptions(mode="compact"))
    assert "📄" in md
    assert "Read" in md
    assert "/tmp/x.md" in md
    assert "1,234" in md  # char count
    # No fenced raw result in compact
    assert "AAA" not in md


def test_compact_skips_todowrite() -> None:
    events = [
        _msg(
            "assistant",
            [ToolUseBlock(id="t1", name="TodoWrite", input={"todos": [{"content": "x"}]})],
        ),
        _msg(
            "user",
            [ToolResultBlock(tool_use_id="t1", content="Todos have been modified successfully.")],
        ),
    ]
    md = render_markdown(events, opts=RenderOptions(mode="compact"))
    assert "TodoWrite" not in md
    assert "Todos have been modified" not in md


def test_full_keeps_tool_use_json_input() -> None:
    events = [
        _msg(
            "assistant",
            [ToolUseBlock(id="t1", name="Bash", input={"command": "ls", "description": "list"})],
        ),
        _msg("user", [ToolResultBlock(tool_use_id="t1", content="file.md")]),
    ]
    md = render_markdown(events, opts=RenderOptions(mode="full"))
    assert "Tool call: `Bash`" in md
    assert '"command": "ls"' in md
    assert "Tool result" in md
    assert "file.md" in md


def test_tool_result_only_user_turn_has_no_user_header() -> None:
    """When a user turn contains only tool_result blocks (parallel-tool fan-in),
    we shouldn't render a "User" header — the tool-result markers already
    convey what these are."""
    events = [
        _msg(
            "assistant",
            [ToolUseBlock(id="t1", name="Bash", input={"command": "ls"})],
        ),
        _msg("user", [ToolResultBlock(tool_use_id="t1", content="file.md")]),
    ]
    md = render_markdown(events, opts=RenderOptions(mode="full"))
    assert "👤" not in md  # no User header for the tool-result turn
