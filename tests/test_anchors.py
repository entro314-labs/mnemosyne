"""Tests for permalink anchors in markdown rendering."""

from __future__ import annotations

import re

from mnemosyne.parser import Message, TextBlock
from mnemosyne.render import RenderOptions, _inject_anchors, render_markdown


def _msg(role: str, text: str, ts: str = "2026-01-01T00:00:00Z") -> Message:
    return Message(
        uuid="u", parent_uuid=None, timestamp=ts, role=role, blocks=[TextBlock(text=text)]
    )


def test_inject_anchors_adds_one_before_each_headed_chunk() -> None:
    chunks = [
        "# Title",  # no role → no anchor
        "### 👤 **User**  _(t0)_\n\nhello",
        "### 🤖 **Assistant**  _(t1)_\n\nhi",
        "### 👤 **User**  _(t2)_\n\nfollowup",
    ]
    out = _inject_anchors(chunks, session_id="abc12345-rest-of-uuid")
    # Title chunk untouched
    assert out[0] == "# Title"
    # Each headed chunk gets `<a id="t-abc12345-N"></a>` prefix
    assert out[1].startswith('<a id="t-abc12345-0"></a>\n\n### 👤')
    assert out[2].startswith('<a id="t-abc12345-1"></a>\n\n### 🤖')
    assert out[3].startswith('<a id="t-abc12345-2"></a>\n\n### 👤')


def test_anchors_appear_in_render_markdown_when_session_id_passed() -> None:
    events = [_msg("user", "a"), _msg("assistant", "b")]
    md = render_markdown(events, session_id="zzzzzzzz", opts=RenderOptions())
    anchors = re.findall(r'<a id="t-zzzzzzzz-(\d+)"></a>', md)
    assert anchors == ["0", "1"]


def test_no_anchors_without_session_id() -> None:
    events = [_msg("user", "a"), _msg("assistant", "b")]
    md = render_markdown(events, opts=RenderOptions())
    assert '<a id="t-' not in md


def test_anchors_numbered_post_coalesce() -> None:
    """After coalesce, three consecutive assistant turns are ONE turn → one anchor."""
    events = [
        _msg("user", "u1", ts="t0"),
        _msg("assistant", "a1", ts="t1"),
        _msg("assistant", "a2", ts="t2"),
        _msg("assistant", "a3", ts="t3"),
        _msg("user", "u2", ts="t4"),
    ]
    md = render_markdown(events, session_id="abcdefgh", opts=RenderOptions())
    anchors = re.findall(r'<a id="t-abcdefgh-(\d+)"></a>', md)
    # 3 anchors: user / coalesced-assistant / user
    assert anchors == ["0", "1", "2"]
