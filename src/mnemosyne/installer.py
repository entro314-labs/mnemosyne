"""Install / uninstall the Claude Code plugin sidecar.

Drops the plugin templates from ``plugin_assets/`` into
``~/.claude/plugins/mnemosyne/`` and registers that directory as
a local marketplace so Claude Code's ``/plugin install`` flow can pick it up.
"""

from __future__ import annotations

import json
import shutil
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

CLAUDE_HOME = Path.home() / ".claude"
PLUGINS_DIR = CLAUDE_HOME / "plugins"
KNOWN_MARKETPLACES = PLUGINS_DIR / "known_marketplaces.json"
DEFAULT_INSTALL_PATH = PLUGINS_DIR / "mnemosyne"
MARKETPLACE_KEY = "mnemosyne"


@dataclass(slots=True)
class InstallResult:
    install_path: Path
    files_written: int
    marketplace_registered: bool
    already_existed: bool


def _asset_root() -> Path:
    """Resolve the on-disk path of the bundled ``plugin_assets/`` directory.

    Works both from an installed wheel (via importlib.resources) and from a
    source checkout.
    """
    # importlib.resources.files returns a Traversable; for a real-fs package
    # (our case), it's backed by a Path.
    return Path(str(resources.files("mnemosyne") / "plugin_assets"))


def _copy_tree(src: Path, dst: Path) -> int:
    """Copy `src` → `dst` recursively, overwriting. Returns count of files copied."""
    count = 0
    for entry in src.rglob("*"):
        if entry.is_dir():
            continue
        if entry.name == ".gitkeep":
            continue
        rel = entry.relative_to(src)
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(entry, target)
        count += 1
    return count


def _ensure_marketplace_registered(install_path: Path) -> bool:
    """Add the install dir to ~/.claude/plugins/known_marketplaces.json as a 'directory' source.

    Returns True if a write happened, False if it was already registered.
    """
    PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if KNOWN_MARKETPLACES.exists():
        try:
            data = json.loads(KNOWN_MARKETPLACES.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    if not isinstance(data, dict):
        data = {}

    existing = data.get(MARKETPLACE_KEY)
    expected_source = {"source": "directory", "path": str(install_path)}
    expected_location = str(install_path)
    if (
        isinstance(existing, dict)
        and existing.get("source") == expected_source
        and existing.get("installLocation") == expected_location
    ):
        return False

    data[MARKETPLACE_KEY] = {
        "source": expected_source,
        "installLocation": expected_location,
        "lastUpdated": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    }
    KNOWN_MARKETPLACES.parent.mkdir(parents=True, exist_ok=True)
    KNOWN_MARKETPLACES.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return True


def install_plugin(install_path: Path = DEFAULT_INSTALL_PATH) -> InstallResult:
    """Deploy the plugin sidecar to `install_path` and register the marketplace.

    Idempotent — safe to re-run; existing files are overwritten with the
    bundled templates (newer plugin versions update in place).
    """
    asset_root = _asset_root()
    if not asset_root.is_dir():
        raise FileNotFoundError(
            f"Plugin assets directory not found inside the package: {asset_root}"
        )

    already_existed = (install_path / ".claude-plugin" / "plugin.json").exists()
    install_path.mkdir(parents=True, exist_ok=True)
    files_written = _copy_tree(asset_root, install_path)
    marketplace_registered = _ensure_marketplace_registered(install_path)

    return InstallResult(
        install_path=install_path,
        files_written=files_written,
        marketplace_registered=marketplace_registered,
        already_existed=already_existed,
    )


def uninstall_plugin(install_path: Path = DEFAULT_INSTALL_PATH) -> bool:
    """Remove the plugin sidecar and de-register the marketplace.

    Returns True if anything was removed, False if nothing was there.
    """
    removed = False
    if install_path.is_dir():
        shutil.rmtree(install_path)
        removed = True

    if KNOWN_MARKETPLACES.exists():
        try:
            data = json.loads(KNOWN_MARKETPLACES.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
        if isinstance(data, dict) and MARKETPLACE_KEY in data:
            del data[MARKETPLACE_KEY]
            KNOWN_MARKETPLACES.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            removed = True

    return removed


def syne_on_path() -> bool:
    """Best-effort check that `syne` is callable from a fresh shell."""
    return shutil.which("syne") is not None


def print_post_install_instructions(result: InstallResult, *, console) -> None:
    """Tell the user what to do next in Claude Code."""
    console.print(f"\n[bold green]✓ Plugin installed[/bold green] → {result.install_path}")
    console.print(f"  files written: {result.files_written}")
    if result.marketplace_registered:
        console.print("  marketplace registered ([dim]known_marketplaces.json[/dim])")
    elif result.already_existed:
        console.print("  marketplace already registered (re-installed in place)")

    if not syne_on_path():
        console.print(
            "\n[yellow]⚠ `syne` is not on PATH.[/yellow] The MCP server inside the "
            "plugin invokes `syne mcp`; install globally with:\n"
            "  [dim]uv tool install git+https://github.com/entro314-labs/mnemosyne[/dim]"
        )

    console.print("\n[bold]Next steps in Claude Code:[/bold]")
    console.print("  1. Restart Claude Code (so it picks up the new marketplace).")
    console.print("  2. Run: [cyan]/plugin install mnemosyne@mnemosyne[/cyan]")
    console.print("  3. Try: [cyan]/history[/cyan]  or  [cyan]/recall <topic>[/cyan]")


def main() -> int:  # pragma: no cover — wired through CLI, not directly invoked
    """Allow ``python -m mnemosyne.installer`` for debugging."""
    from rich.console import Console  # noqa: PLC0415

    console = Console()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "install"
    if cmd == "install":
        result = install_plugin()
        print_post_install_instructions(result, console=console)
        return 0
    if cmd == "uninstall":
        removed = uninstall_plugin()
        console.print("✓ uninstalled" if removed else "(nothing to remove)")
        return 0
    console.print(f"unknown command: {cmd}")
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
