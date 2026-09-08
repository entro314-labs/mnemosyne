"""Tests for non-trivial CLI helpers (filename slugging, filters)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mnemosyne import cli
from mnemosyne.cli import (
    _apply_filters,
    _filename_for,
    _project_output_names,
    _resolve_filenames,
)
from mnemosyne.config import ProjectEntry
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


def test_resolve_filenames_extends_colliding_short_ids() -> None:
    summaries = [
        _summary(session_id="aaaaaaaa-1111", ai_title="same"),
        _summary(session_id="aaaaaaaa-2222", ai_title="same"),
    ]
    out = _resolve_filenames(summaries)
    assert out["aaaaaaaa-1111"] != out["aaaaaaaa-2222"]


def test_cross_project_output_names_disambiguate_same_friendly_name() -> None:
    entries = [
        ProjectEntry(slug="-Users-a-api", friendly_name="api"),
        ProjectEntry(slug="-Users-b-api", friendly_name="api"),
    ]
    names = _project_output_names(entries)
    assert names[entries[0].slug] != names[entries[1].slug]
    assert all(name.startswith("api-") for name in names.values())


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


def test_filter_until_date_includes_the_whole_day() -> None:
    sessions = [
        _summary(session_id="a", last_timestamp="2026-05-01T23:59:59Z"),
        _summary(session_id="b", last_timestamp="2026-05-02T00:00:00Z"),
    ]
    out = _apply_filters(sessions, since=None, until="2026-05-01", matching=None)
    assert [s.session_id for s in out] == ["a"]


def test_filter_compares_timezone_offsets_by_instant() -> None:
    sessions = [_summary(session_id="a", last_timestamp="2026-05-01T01:00:00+02:00")]
    out = _apply_filters(
        sessions,
        since="2026-04-30T23:30:00Z",
        until=None,
        matching=None,
    )
    assert out == []


def test_filter_excludes_undated_sessions_from_date_range() -> None:
    sessions = [_summary(session_id="a", last_timestamp=None)]
    assert _apply_filters(sessions, since="2026-05-01", until=None, matching=None) == []


def test_filter_matching_regex_on_title_and_first_prompt() -> None:
    sessions = [
        _summary(session_id="a", ai_title="Fix Godot bug"),
        _summary(session_id="b", ai_title="Add Rust feature"),
        _summary(session_id="c", ai_title=None, first_user_text="check the godot scene"),
    ]
    out = _apply_filters(sessions, since=None, until=None, matching=r"(?i)godot")
    assert {s.session_id for s in out} == {"a", "c"}


def test_filter_matching_is_case_sensitive_unless_regex_opts_in() -> None:
    sessions = [_summary(session_id="a", ai_title="Godot")]
    assert _apply_filters(sessions, since=None, until=None, matching="godot") == []
    assert _apply_filters(sessions, since=None, until=None, matching="(?i)godot") == sessions


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


def test_single_jsonl_export_keeps_project_path_and_avoids_title_collision(
    tmp_path, monkeypatch
) -> None:
    project_dir = tmp_path / "-project"
    project_dir.mkdir()
    session_ids = ["aaaaaaaa-1111", "bbbbbbbb-2222"]
    for session_id in session_ids:
        records = [
            {
                "type": "user",
                "uuid": f"u-{session_id}",
                "timestamp": "2026-05-01T12:00:00Z",
                "message": {"content": [{"type": "text", "text": "same prompt"}]},
            },
            {"type": "ai-title", "aiTitle": "Same title"},
        ]
        (project_dir / f"{session_id}.jsonl").write_text(
            "\n".join(json.dumps(record) for record in records) + "\n",
            encoding="utf-8",
        )

    local_path = str(tmp_path / "workspace")
    settings = cli.Settings(
        projects={
            project_dir.name: ProjectEntry(
                slug=project_dir.name,
                local_path=local_path,
                friendly_name="project",
            )
        }
    )
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "sync_registry", lambda value: value)
    monkeypatch.setattr(cli, "save_settings", lambda value: None)

    output = tmp_path / "out"
    cli.export(session_ids[0], project_dir=project_dir, output=output, fmt="jsonl")

    exported = output / "same-title-aaaaaaaa.jsonl"
    assert exported.is_file()
    turn = json.loads(exported.read_text(encoding="utf-8").splitlines()[0])
    assert turn["project_path"] == local_path


@pytest.mark.parametrize("include_summaries", [False, True])
def test_interactive_preserves_summaries_flag(include_summaries, tmp_path, monkeypatch) -> None:
    claude_root = tmp_path / "projects"
    project_dir = claude_root / "-project"
    project_dir.mkdir(parents=True)
    session_id = "aaaaaaaa-1111"
    (project_dir / f"{session_id}.jsonl").write_text(
        json.dumps(
            {
                "type": "user",
                "timestamp": "2026-05-01T12:00:00Z",
                "cwd": str(tmp_path),
                "message": {"content": [{"type": "text", "text": "prompt"}]},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    entry = ProjectEntry(slug=project_dir.name, local_path=str(tmp_path), friendly_name="project")
    settings = cli.Settings(projects={entry.slug: entry})
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "CLAUDE_PROJECTS", claude_root)
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "sync_registry", lambda value: value)
    monkeypatch.setattr(cli, "save_settings", lambda value: None)
    monkeypatch.setattr(cli, "_detect_cwd_project", lambda value: entry)
    answers = iter(["all", str(tmp_path / "out")])
    monkeypatch.setattr(cli.Prompt, "ask", lambda *args, **kwargs: next(answers))

    observed: list[bool] = []

    def fake_write_export(*args, selection, **kwargs):
        observed.append(selection.summaries)
        return Path(tmp_path / "out" / "session.md")

    monkeypatch.setattr(cli, "_write_export", fake_write_export)
    cli.interactive(summaries=include_summaries)
    assert observed == [include_summaries]


def test_all_projects_exports_project_memories_even_when_session_filter_is_empty(
    tmp_path, monkeypatch
) -> None:
    claude_root = tmp_path / "projects"
    project_dir = claude_root / "-project"
    memory_dir = project_dir / "memory"
    memory_dir.mkdir(parents=True)
    (project_dir / "session.jsonl").write_text(
        json.dumps(
            {
                "type": "user",
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {"content": [{"type": "text", "text": "old session"}]},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (memory_dir / "decision.md").write_text(
        "---\nname: decision\n---\nkeep this\n", encoding="utf-8"
    )
    entry = ProjectEntry(slug=project_dir.name, local_path=str(tmp_path), friendly_name="project")
    settings = cli.Settings(projects={entry.slug: entry})
    monkeypatch.setattr(cli, "CLAUDE_PROJECTS", claude_root)

    output = tmp_path / "archive"
    cli._export_all_projects(
        settings,
        output=output,
        fmt="markdown",
        mode=None,
        since="2027-01-01",
        until=None,
        matching=None,
        include_thinking=None,
        include_attachments=None,
        include_reminders=None,
        max_tool_chars=None,
        sidecar=True,
        index=True,
        skip_empty=True,
        selection=cli.ArtifactSelection(memories=True),
    )

    assert (output / "project" / "memory" / "decision.md").is_file()
    assert not (output / "project" / "index.json").exists()


def test_cross_project_flags_reject_an_explicit_project_scope(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    with pytest.raises(SystemExit, match="mutually exclusive"):
        cli.export_all(project_dir=project_dir, all_projects=True)
    with pytest.raises(SystemExit, match="mutually exclusive"):
        cli.recall(project_dir=project_dir, all_projects=True)


def test_codex_all_rejects_an_explicit_cwd(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="mutually exclusive"):
        cli.codex_list(cwd=tmp_path, all_sessions=True)


# ---- index.json mirrors the directory, not just the last filter (F-idx) ----


def _write_session(project_dir: Path, session_id: str, title: str, ts: str, cwd: Path) -> None:
    records = [
        {
            "type": "user",
            "uuid": f"u-{session_id}",
            "timestamp": ts,
            "cwd": str(cwd),
            "message": {"content": [{"type": "text", "text": f"prompt for {title}"}]},
        },
        {"type": "ai-title", "aiTitle": title},
    ]
    (project_dir / f"{session_id}.jsonl").write_text(
        "\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8"
    )


def _stub_registry(monkeypatch, project_dir: Path, local_path: Path) -> cli.Settings:
    settings = cli.Settings(
        projects={
            project_dir.name: ProjectEntry(
                slug=project_dir.name, local_path=str(local_path), friendly_name="project"
            )
        }
    )
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "sync_registry", lambda value: value)
    monkeypatch.setattr(cli, "save_settings", lambda value: None)
    return settings


# ---- one session → one filename, whichever command exported it ----


def _collision_archive(tmp_path: Path) -> tuple[Path, Path, Path]:
    project_dir = tmp_path / "-a-foo-bar"
    project_dir.mkdir()
    mine = tmp_path / "foo.bar"
    mine.mkdir()
    theirs = tmp_path / "foo_bar"
    theirs.mkdir()
    _write_session(project_dir, "aaaaaaaa-1", "Same title", "2026-01-01T00:00:00Z", mine)
    _write_session(project_dir, "bbbbbbbb-2", "Same title", "2026-01-02T00:00:00Z", theirs)
    return project_dir, mine, theirs


def test_export_and_export_all_agree_on_filenames_under_the_same_scope(
    tmp_path: Path, monkeypatch
) -> None:
    project_dir, mine, _ = _collision_archive(tmp_path)
    _stub_registry(monkeypatch, project_dir, mine)
    monkeypatch.chdir(mine)
    monkeypatch.setattr(cli, "project_dir_for_cwd", lambda cwd, claude_home=None: project_dir)
    output = tmp_path / "out"

    cli.export("aaaaaaaa", output=output)
    cli.export_all(output=output)
    # Only the cwd's session is in scope, so it owns the bare title — once.
    assert sorted(p.name for p in output.glob("*.md")) == ["same-title.md"]


def test_interactive_default_scopes_the_detected_workspace_by_cwd(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    project_dir, mine, _ = _collision_archive(tmp_path)
    claude_root = project_dir.parent
    settings = _stub_registry(monkeypatch, project_dir, mine)
    entry = settings.projects[project_dir.name]
    monkeypatch.chdir(mine)
    monkeypatch.setattr(cli, "CLAUDE_PROJECTS", claude_root)
    monkeypatch.setattr(cli, "_detect_cwd_project", lambda value: entry)
    answers = iter(["all", str(tmp_path / "out")])
    monkeypatch.setattr(cli.Prompt, "ask", lambda *args, **kwargs: next(answers))
    exported: list[str] = []

    def fake_write_export(s, *args, **kwargs):
        exported.append(s.session_id)
        return Path(tmp_path / "out" / "session.md")

    monkeypatch.setattr(cli, "_write_export", fake_write_export)
    cli.interactive()
    assert exported == ["aaaaaaaa-1"]
    assert (
        "1 session(s) in -a-foo-bar belong to a different working tree" in capsys.readouterr().err
    )


def test_merge_all_from_path_scopes_by_that_tree_and_slug_is_face_value(
    tmp_path: Path, monkeypatch
) -> None:
    project_dir, mine, theirs = _collision_archive(tmp_path)
    _stub_registry(monkeypatch, project_dir, theirs)
    monkeypatch.setattr(cli, "CLAUDE_PROJECTS", project_dir.parent)
    monkeypatch.setattr(cli, "project_dir_for_cwd", lambda cwd, claude_home=None: project_dir)

    by_path = tmp_path / "by-path.md"
    cli.merge(all_from=str(mine), output=by_path)
    assert (
        json.loads(by_path.with_suffix(".meta.json").read_text(encoding="utf-8"))["session_count"]
        == 1
    )

    by_slug = tmp_path / "by-slug.md"
    cli.merge(all_from=project_dir.name, output=by_slug)
    assert (
        json.loads(by_slug.with_suffix(".meta.json").read_text(encoding="utf-8"))["session_count"]
        == 2
    )
