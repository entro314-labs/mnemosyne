"""Tests for installer.py — plugin sidecar deploy/teardown."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from mnemosyne import installer

if TYPE_CHECKING:
    from pathlib import Path


def _patched_paths(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    """Redirect the installer's writes into a temp directory."""
    plugins_dir = tmp_path / "plugins"
    install_path = plugins_dir / "mnemosyne"
    known_marketplaces = plugins_dir / "known_marketplaces.json"
    monkeypatch.setattr(installer, "PLUGINS_DIR", plugins_dir)
    monkeypatch.setattr(installer, "KNOWN_MARKETPLACES", known_marketplaces)
    monkeypatch.setattr(installer, "DEFAULT_INSTALL_PATH", install_path)
    return install_path, known_marketplaces


def test_install_deploys_all_assets(tmp_path, monkeypatch) -> None:
    install_path, _ = _patched_paths(tmp_path, monkeypatch)
    result = installer.install_plugin(install_path)
    assert result.install_path == install_path
    assert result.files_written > 0
    assert result.marketplace_registered is True
    assert result.already_existed is False

    expected_files = {
        ".claude-plugin/plugin.json",
        ".claude-plugin/marketplace.json",
        ".mcp.json",
        "skills/session-history/SKILL.md",
        "commands/recall.md",
        "commands/history.md",
        "commands/summon.md",
        "commands/export.md",
        "hooks/hooks.json",
    }
    actual = {str(p.relative_to(install_path)) for p in install_path.rglob("*") if p.is_file()}
    assert expected_files <= actual


def test_plugin_json_has_required_fields(tmp_path, monkeypatch) -> None:
    install_path, _ = _patched_paths(tmp_path, monkeypatch)
    installer.install_plugin(install_path)

    manifest = json.loads(
        (install_path / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    assert manifest["name"] == "mnemosyne"
    assert "version" in manifest
    assert "description" in manifest


def test_mcp_config_points_at_syne_binary(tmp_path, monkeypatch) -> None:
    install_path, _ = _patched_paths(tmp_path, monkeypatch)
    installer.install_plugin(install_path)

    mcp_conf = json.loads((install_path / ".mcp.json").read_text(encoding="utf-8"))
    server = mcp_conf["mcpServers"]["mnemosyne"]
    assert server["command"] == "syne"
    assert server["args"] == ["mcp"]


def test_marketplace_registered_with_directory_source(tmp_path, monkeypatch) -> None:
    install_path, known = _patched_paths(tmp_path, monkeypatch)
    installer.install_plugin(install_path)

    data = json.loads(known.read_text(encoding="utf-8"))
    entry = data[installer.MARKETPLACE_KEY]
    assert entry["source"] == {"source": "directory", "path": str(install_path)}
    assert entry["installLocation"] == str(install_path)
    assert "lastUpdated" in entry


def test_install_is_idempotent(tmp_path, monkeypatch) -> None:
    install_path, _ = _patched_paths(tmp_path, monkeypatch)
    first = installer.install_plugin(install_path)
    second = installer.install_plugin(install_path)
    assert first.marketplace_registered is True
    assert second.marketplace_registered is False  # already registered, no rewrite
    assert second.already_existed is True


def test_uninstall_removes_files_and_marketplace(tmp_path, monkeypatch) -> None:
    install_path, known = _patched_paths(tmp_path, monkeypatch)
    installer.install_plugin(install_path)
    assert install_path.is_dir()

    removed = installer.uninstall_plugin(install_path)
    assert removed is True
    assert not install_path.exists()

    data = json.loads(known.read_text(encoding="utf-8"))
    assert installer.MARKETPLACE_KEY not in data


def test_uninstall_on_clean_system_is_noop(tmp_path, monkeypatch) -> None:
    install_path, _ = _patched_paths(tmp_path, monkeypatch)
    assert installer.uninstall_plugin(install_path) is False


def test_uninstall_preserves_other_marketplaces(tmp_path, monkeypatch) -> None:
    install_path, known = _patched_paths(tmp_path, monkeypatch)
    # Pre-seed the marketplaces file with an unrelated entry.
    known.parent.mkdir(parents=True, exist_ok=True)
    known.write_text(
        json.dumps(
            {
                "other-marketplace": {
                    "source": {"source": "github", "repo": "foo/bar"},
                    "installLocation": "/tmp/foo",
                    "lastUpdated": "2026-01-01T00:00:00Z",
                }
            }
        ),
        encoding="utf-8",
    )

    installer.install_plugin(install_path)
    installer.uninstall_plugin(install_path)
    data = json.loads(known.read_text(encoding="utf-8"))
    assert "other-marketplace" in data
    assert installer.MARKETPLACE_KEY not in data
