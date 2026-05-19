"""Format-specific session renderers.

`render_markdown` lives in `render.py`. This module adds two alternative output
formats that share the same parser/filter/coalesce pipeline via `collect_turns`:

- **JSONL** — one JSON object per coalesced turn. For embedding pipelines,
  vector store ingestion, or any structured downstream consumer.
- **Plain text** — markdown stripped of decoration. For pasting into prompts
  for models that prefer no markup, or for plaintext archives.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Literal

from mnemosyne.render import RenderOptions, collect_turns

if TYPE_CHECKING:
    from collections.abc import Iterable

    from mnemosyne.parser import Event


Format = Literal["markdown", "jsonl", "plain"]


def render_jsonl(
    events: Iterable[Event],
    *,
    session_id: str | None = None,
    project_slug: str | None = None,
    project_path: str | None = None,
    opts: RenderOptions | None = None,
) -> str:
    """Emit one JSON object per coalesced turn, separated by newlines.

    Each line carries:

    - ``turn_index`` — position within the session (zero-based, post-coalesce).
    - ``turn_id`` — globally unique ``{session_id}#{turn_index}`` when
      ``session_id`` is provided. Stable across re-renders → safe to use as a
      vector store primary key.
    - ``role`` — ``"user"`` / ``"assistant"``.
    - ``timestamp`` — original ISO 8601 from the source event.
    - ``text`` — rendered turn body (markdown for compact/full modes, plain
      prose for transcript mode).
    - ``char_count`` — len(text). Useful for chunk sizing.
    - ``session_id`` / ``project_slug`` / ``project_path`` — included when
      provided. Stable identifiers for downstream linkage.
    """
    turns = collect_turns(events, opts)
    lines: list[str] = []
    for i, t in enumerate(turns):
        obj: dict[str, object] = {
            "turn_index": i,
            "role": t.role,
            "timestamp": t.timestamp,
            "text": t.body,
            "char_count": len(t.body),
        }
        if session_id is not None:
            obj["turn_id"] = f"{session_id}#{i}"
            obj["session_id"] = session_id
        if project_slug is not None:
            obj["project_slug"] = project_slug
        if project_path is not None:
            obj["project_path"] = project_path
        lines.append(json.dumps(obj, ensure_ascii=False))
    return "\n".join(lines) + ("\n" if lines else "")


# Markdown-decoration scrubbers — applied to each turn body when rendering plain.
_FENCE_OPEN_RE = re.compile(r"```[\w-]*\n")
_FENCE_CLOSE_RE = re.compile(r"\n```")
_BOLD_RE = re.compile(r"\*\*([^*]+?)\*\*")
_ITALIC_TIMESTAMP_RE = re.compile(r"_\(([^)]+)\)_")
_TOOL_LABEL_RE = re.compile(r"(?:📄|📁|🔍|🔎|✏️|✍️|🐚|❓|🔧)\s*\*\*([^*]+?)\*\*")
_STRIP_EMOJIS = ("📤", "❌", "💭", "🤖", "👤")


def _strip_markdown(body: str) -> str:
    """Best-effort markdown → plaintext on a single turn body."""
    body = _TOOL_LABEL_RE.sub(r"[\1]", body)
    body = _FENCE_OPEN_RE.sub("----\n", body)
    body = _FENCE_CLOSE_RE.sub("\n----", body)
    body = _BOLD_RE.sub(r"\1", body)
    body = _ITALIC_TIMESTAMP_RE.sub(r"(\1)", body)
    for emoji in _STRIP_EMOJIS:
        body = body.replace(emoji, "")
    return body.strip()


def render_plain(
    events: Iterable[Event],
    *,
    title: str | None = None,
    opts: RenderOptions | None = None,
) -> str:
    """Plaintext rendering — no markdown headers, no fences, no decoration.

    Each turn is bracketed by a `=== USER (ts) ===` / `=== ASSISTANT (ts) ===`
    line. Turn bodies have markdown formatting stripped (best-effort).
    """
    turns = collect_turns(events, opts)
    chunks: list[str] = []
    if title:
        chunks.append(title.strip())
    for t in turns:
        label = t.role.upper()
        suffix = f" ({t.timestamp})" if t.timestamp else ""
        body = _strip_markdown(t.body)
        chunks.append(f"=== {label}{suffix} ===\n\n{body}")
    return "\n\n".join(chunks) + ("\n" if chunks else "")
