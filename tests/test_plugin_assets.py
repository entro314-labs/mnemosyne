"""Plugin asset invariants — hook wiring and version alignment."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

ASSETS = Path(__file__).resolve().parents[1] / "src" / "mnemosyne" / "plugin_assets"


def test_hooks_json_targets_resume_and_compact_only() -> None:
    data = json.loads((ASSETS / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    session_start = data["hooks"]["SessionStart"]
    assert len(session_start) == 1
    entry = session_start[0]
    # Fresh sessions stay clean: the matcher must scope injection to resume/compact.
    assert entry["matcher"] == "resume|compact"
    (hook,) = entry["hooks"]
    assert hook["type"] == "command"
    # A capped bundle, guarded so a missing `syne` is a silent no-op.
    assert "syne recall --bundle" in hook["command"]
    assert "--max-chars" in hook["command"]
    assert "command -v syne" in hook["command"]
    assert isinstance(hook["timeout"], int)


def test_plugin_versions_match_package() -> None:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    version = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
    plugin = json.loads((ASSETS / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    marketplace = json.loads(
        (ASSETS / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
    )
    assert plugin["version"] == version
    assert [p["version"] for p in marketplace["plugins"]] == [version]
