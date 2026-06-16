"""Tests for recall.py — the capped stdout reader."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from mnemosyne.config import Settings
from mnemosyne.recall import build_bundle, build_recall

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
    (pd / "abc12345-0000-0000-0000-000000000000.jsonl").write_text(
        _session_jsonl("investigate the auth flow", "Auth uses JWT 24h expiry.", "Auth flow"),
        encoding="utf-8",
    )
    mem = pd / "memory"
    mem.mkdir()
    (mem / "the-plan.md").write_text(
        '---\nname: the-plan\ndescription: "ship v2"\nmetadata:\n  type: project\n---\n'
        "The roadmap is to ship v2 with JWT auth.\n",
        encoding="utf-8",
    )
    return pd


def test_recall_recent_default(tmp_path: Path) -> None:
    out = build_recall([_make_project(tmp_path)], Settings())
    assert "## Recent sessions" in out
    assert "Auth flow" in out


def test_recall_memory_index(tmp_path: Path) -> None:
    out = build_recall([_make_project(tmp_path)], Settings(), memories=True)
    assert "## Curated memories" in out
    assert "the-plan" in out
    assert "ship v2" in out


def test_recall_session_search(tmp_path: Path) -> None:
    out = build_recall([_make_project(tmp_path)], Settings(), query_str="JWT")
    assert "## Matching sessions" in out
    assert "JWT" in out


def test_recall_memory_search(tmp_path: Path) -> None:
    out = build_recall([_make_project(tmp_path)], Settings(), query_str="roadmap", memories=True)
    assert "## Matching memories" in out
    assert "the-plan" in out


def test_recall_no_match_message(tmp_path: Path) -> None:
    out = build_recall([_make_project(tmp_path)], Settings(), query_str="zzz-nope")
    assert "No matching sessions" in out


def test_recall_json_format(tmp_path: Path) -> None:
    out = build_recall([_make_project(tmp_path)], Settings(), memories=True, fmt="json")
    parsed = json.loads(out)
    assert parsed["kind"] == "memory_index"
    assert parsed["items"][0]["name"] == "the-plan"


def test_recall_hard_caps_output(tmp_path: Path) -> None:
    out = build_recall([_make_project(tmp_path)], Settings(), memories=True, max_chars=40)
    assert len(out) <= 80  # cap + the truncation marker
    assert "truncated" in out


def test_bundle_markdown(tmp_path: Path) -> None:
    out = build_bundle([_make_project(tmp_path)], Settings(), query_str="JWT")
    assert "## Self-align brief" in out
    assert "Curated memories" in out
    assert "Suggested next" in out
    assert "get_memory(" in out


def test_bundle_json(tmp_path: Path) -> None:
    parsed = json.loads(build_bundle([_make_project(tmp_path)], Settings(), fmt="json"))
    assert "suggested_next" in parsed
    assert "guidance" in parsed
    assert parsed["recent_sessions"]
