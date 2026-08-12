"""Cross-tool recall: Codex context inside the self_align packet and bundle."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from mnemosyne.config import ProjectEntry, Settings
from mnemosyne.query import fit_packet, self_align
from mnemosyne.recall import build_bundle

if TYPE_CHECKING:
    from pathlib import Path

SID = "019f0000-0000-7000-8000-00000000000a"


def _claude_project(tmp_path: Path) -> Path:
    pd = tmp_path / "-proj"
    pd.mkdir()
    (pd / "abc12345-0000-0000-0000-000000000000.jsonl").write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "u1",
                "parentUuid": None,
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {"role": "user", "content": [{"type": "text", "text": "auth work"}]},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    mem = pd / "memory"
    mem.mkdir()
    (mem / "the-plan.md").write_text(
        '---\nname: the-plan\ndescription: "ship v2"\nmetadata:\n  type: project\n---\nBody.\n',
        encoding="utf-8",
    )
    return pd


def _codex_home(tmp_path: Path, cwd: Path) -> Path:
    home = tmp_path / ".codex"
    day = home / "sessions" / "2026" / "07" / "01"
    day.mkdir(parents=True)
    records = [
        {
            "timestamp": "2026-07-01T10:00:00.000Z",
            "type": "session_meta",
            "payload": {"id": SID, "cwd": str(cwd.resolve()), "originator": "codex_cli"},
        },
        {
            "timestamp": "2026-07-01T10:00:01.000Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "port the auth flow"}],
            },
        },
    ]
    (day / f"rollout-2026-07-01T10-00-00-{SID}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )
    (home / "session_index.jsonl").write_text(
        json.dumps({"id": SID, "thread_name": "Port auth flow"}) + "\n", encoding="utf-8"
    )
    summaries = home / "memories" / "rollout_summaries"
    summaries.mkdir(parents=True)
    (summaries / "2026-07-01T10-00-00-xxxx-port_auth.md").write_text(
        f"thread_id: {SID}\nupdated_at: 2026-07-01T10:10:00+00:00\ncwd: {cwd.resolve()}\n\n"
        "# Ported the auth flow\n\nBody.\n",
        encoding="utf-8",
    )
    return home


def _fixture(tmp_path: Path) -> tuple[Path, Settings, Path]:
    pd = _claude_project(tmp_path)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    home = _codex_home(tmp_path, worktree)
    settings = Settings(projects={pd.name: ProjectEntry(slug=pd.name, local_path=str(worktree))})
    return pd, settings, home


def test_self_align_includes_codex_context(tmp_path: Path) -> None:
    pd, settings, home = _fixture(tmp_path)
    packet = self_align([pd], settings, codex_home=home)
    assert [c["session_id"] for c in packet["codex_sessions"]] == [SID]
    assert packet["codex_sessions"][0]["title"] == "Port auth flow"
    assert packet["codex_sessions"][0]["source"] == "codex"
    assert [s["title"] for s in packet["codex_summaries"]] == ["Ported the auth flow"]
    calls = [s["call"] for s in packet["suggested_next"]]
    assert any(c.startswith("get_codex_handoff(") for c in calls)


def test_self_align_codex_off_or_absent(tmp_path: Path) -> None:
    pd, settings, home = _fixture(tmp_path)
    off = self_align([pd], settings, codex_home=home, include_codex=False)
    assert off["codex_sessions"] == [] and off["codex_summaries"] == []
    absent = self_align([pd], settings, codex_home=tmp_path / "no-codex")
    assert absent["codex_sessions"] == [] and absent["codex_summaries"] == []


def test_self_align_no_local_path_skips_codex(tmp_path: Path) -> None:
    pd, _, home = _fixture(tmp_path)
    packet = self_align([pd], Settings(), codex_home=home)  # unregistered project
    assert packet["codex_sessions"] == []


def test_self_align_query_filters_codex_titles(tmp_path: Path) -> None:
    pd, settings, home = _fixture(tmp_path)
    packet = self_align([pd], settings, query="auth", codex_home=home)
    assert [c["session_id"] for c in packet["codex_sessions"]] == [SID]
    assert [s["title"] for s in packet["codex_summaries"]] == ["Ported the auth flow"]


def test_self_align_query_does_not_fall_back_to_unrelated_codex_rows(tmp_path: Path) -> None:
    pd, settings, home = _fixture(tmp_path)
    packet = self_align([pd], settings, query="not-present", codex_home=home)
    assert packet["codex_sessions"] == []
    assert packet["codex_summaries"] == []


def test_fit_packet_trims_codex_rows_and_suggestions(tmp_path: Path) -> None:
    pd, settings, home = _fixture(tmp_path)
    packet = self_align([pd], settings, codex_home=home)
    tiny = fit_packet(packet, 900)
    assert tiny.get("truncated") is True
    if not tiny["codex_summaries"]:
        assert not any(s["call"].startswith("get_codex_handoff(") for s in tiny["suggested_next"])
    assert tiny["memories"]  # never trimmed below the top memory


def test_bundle_renders_codex_sections(tmp_path: Path) -> None:
    pd, settings, home = _fixture(tmp_path)
    out = build_bundle([pd], settings, codex_home=home)
    assert "**Codex sessions**" in out
    assert "Port auth flow" in out
    assert "**Codex handoffs**" in out
    no_codex = build_bundle([pd], settings, codex_home=home, include_codex=False)
    assert "Codex sessions" not in no_codex
