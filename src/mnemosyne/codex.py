"""Read OpenAI Codex CLI archives (``~/.codex``) into mnemosyne's event model.

Codex CLI stores each session ("rollout") as JSONL under
``~/.codex/sessions/YYYY/MM/DD/rollout-<stamp>-<uuid>.jsonl``. Every line is
``{timestamp, type, payload}``:

- ``session_meta``   — first line: ``{id, cwd, originator, cli_version, …}``.
  The ``cwd`` field is what lets us associate a rollout with a local project —
  Codex has no per-project directories, so discovery scans first lines only.
- ``response_item``  — the conversation. ``payload.type`` is one of ``message``
  (role user/assistant with ``content: [{type: input_text|output_text, text}]``),
  ``reasoning`` (usually ``encrypted_content`` with an empty ``summary`` — only a
  non-empty summary is readable), ``function_call`` (``name`` + JSON-string
  ``arguments`` + ``call_id``), and ``function_call_output`` (``call_id`` +
  ``output``).
- ``event_msg``      — the UI event stream (``user_message`` / ``agent_message``
  / ``token_count`` …). It duplicates ``response_item`` content, so it is skipped.
- ``turn_context``   — per-turn model/effort; used to stamp assistant messages.

``~/.codex/session_index.jsonl`` maps session ids to human thread names, and
``~/.codex/memories/`` holds Codex's own model-written memory: a consolidated
``memory_summary.md`` plus per-session ``rollout_summaries/*.md`` digests whose
header block carries ``cwd`` / ``thread_id`` / ``updated_at``. Both are surfaced
as recall sources, trust-ordered *below* the curated Claude memory layer (they
are model-written, not hand-curated).

Everything maps into the same dataclasses as the Claude parser
(:class:`~mnemosyne.parser.Message`, :class:`~mnemosyne.parser.SessionSummary` …)
so the whole render / recall / export pipeline works on Codex sessions unchanged.
Codex function calls arrive as standalone records rather than blocks inside a
message, so they are wrapped in single-block messages mirroring the Claude shape
(assistant carries ``tool_use``, user carries ``tool_result``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mnemosyne.parser import (
    Block,
    Event,
    Message,
    SessionSummary,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

CODEX_HOME = Path.home() / ".codex"

# rollout-2025-12-04T14-59-48-<uuid>.jsonl — the uuid tail is the session id.
_ROLLOUT_ID_RE = re.compile(
    r"rollout-.*-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)
# Codex-injected wrappers that are never conversational content.
_NOISE_PREFIXES = ("<environment_context>", "<user_instructions>", "<ENVIRONMENT_CONTEXT>")
# The IDE-context wrapper keeps the real prompt under this heading.
_IDE_REQUEST_MARKER = "## My request for Codex:"


def _home(codex_home: Path | None) -> Path:
    return codex_home if codex_home is not None else CODEX_HOME


def codex_available(codex_home: Path | None = None) -> bool:
    """True when a Codex CLI archive exists on this machine."""
    return (_home(codex_home) / "sessions").is_dir()


@dataclass(slots=True)
class CodexSessionMeta:
    """The cheap first-line header of one rollout file."""

    session_id: str
    path: Path
    cwd: str | None
    timestamp: str | None
    originator: str | None
    cli_version: str | None
    thread_name: str | None = None  # joined in from session_index.jsonl


@dataclass(slots=True)
class CodexRolloutSummary:
    """One ``memories/rollout_summaries/*.md`` digest — Codex's session handoff."""

    path: Path
    thread_id: str | None
    cwd: str | None
    git_branch: str | None
    updated_at: str | None
    title: str | None  # first `# ` heading in the body


def _read_json_lines(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8") as f:
        for raw in f:
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                yield obj


def read_codex_meta(path: Path) -> CodexSessionMeta | None:
    """Read a rollout's ``session_meta`` header without loading the file.

    The meta record is the first line by construction; a few lines are scanned
    for robustness. Returns ``None`` for files without one (not a rollout).
    """
    try:
        with path.open(encoding="utf-8") as f:
            for _ in range(5):
                raw = f.readline()
                if not raw:
                    break
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "session_meta":
                    continue
                payload = obj.get("payload") or {}
                if not isinstance(payload, dict):
                    return None
                return CodexSessionMeta(
                    session_id=str(payload.get("id") or _session_id_from_name(path) or path.stem),
                    path=path,
                    cwd=payload.get("cwd"),
                    timestamp=payload.get("timestamp") or obj.get("timestamp"),
                    originator=payload.get("originator"),
                    cli_version=payload.get("cli_version"),
                )
    except OSError:
        return None
    return None


def _session_id_from_name(path: Path) -> str | None:
    m = _ROLLOUT_ID_RE.search(path.stem)
    return m.group(1) if m else None


def iter_codex_session_paths(codex_home: Path | None = None) -> Iterator[Path]:
    """Every rollout JSONL under ``sessions/``, newest first (date-tree order)."""
    sessions = _home(codex_home) / "sessions"
    if not sessions.is_dir():
        return
    yield from sorted(sessions.rglob("*.jsonl"), reverse=True)


def load_session_index(codex_home: Path | None = None) -> dict[str, dict[str, Any]]:
    """``session_index.jsonl`` as ``{id: {thread_name, updated_at}}`` (may be empty)."""
    index_path = _home(codex_home) / "session_index.jsonl"
    if not index_path.is_file():
        return {}
    out: dict[str, dict[str, Any]] = {}
    for obj in _read_json_lines(index_path):
        sid = obj.get("id")
        if sid:
            out[str(sid)] = obj
    return out


def codex_sessions_for(
    cwd: Path | None,
    codex_home: Path | None = None,
    limit: int | None = None,
) -> list[CodexSessionMeta]:
    """Rollouts whose ``session_meta.cwd`` matches ``cwd``, newest first.

    ``cwd=None`` returns every rollout. Discovery reads only the first line of
    each file, so a large archive stays cheap; ``limit`` stops the scan early.
    """
    if limit is not None and limit <= 0:
        return []
    wanted = str(cwd.resolve()) if cwd is not None else None
    index = load_session_index(codex_home)
    out: list[CodexSessionMeta] = []
    for path in iter_codex_session_paths(codex_home):
        meta = read_codex_meta(path)
        if meta is None:
            continue
        if wanted is not None and meta.cwd != wanted:
            continue
        row = index.get(meta.session_id)
        if row:
            meta.thread_name = row.get("thread_name")
        out.append(meta)
        if limit is not None and len(out) >= limit:
            break
    return out


def resolve_codex_session(
    session_id: str,
    codex_home: Path | None = None,
) -> Path:
    """Find one rollout by full session id or unique prefix (filename-based).

    Mirrors the Claude session-lookup contract: ``FileNotFoundError`` when
    nothing matches, ``ValueError`` on an ambiguous prefix.
    """
    matches: list[Path] = []
    for path in iter_codex_session_paths(codex_home):
        sid = _session_id_from_name(path)
        if sid is not None and sid.startswith(session_id):
            matches.append(path)
    if not matches:
        raise FileNotFoundError(f"No Codex session matching {session_id!r}")
    if len(matches) > 1:
        raise ValueError(
            f"Prefix {session_id!r} matches {len(matches)} Codex sessions; pass a longer prefix."
        )
    return matches[0]


# ---- rollout → events ----


def _is_noise_user_text(text: str) -> bool:
    return text.lstrip().startswith(_NOISE_PREFIXES)


def _message_text(payload: dict[str, Any]) -> str:
    """Join the text parts of a ``response_item``/``message`` payload."""
    content = payload.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") in {"input_text", "output_text", "text"}:
            parts.append(str(item.get("text", "")))
    return "\n".join(p for p in parts if p)


def _reasoning_text(payload: dict[str, Any]) -> str:
    """The readable summary of a reasoning record ('' when encrypted-only)."""
    summary = payload.get("summary")
    if not isinstance(summary, list):
        return ""
    parts = [
        str(item.get("text", ""))
        for item in summary
        if isinstance(item, dict) and item.get("type") == "summary_text"
    ]
    return "\n\n".join(p for p in parts if p.strip())


def _tool_input(arguments: Any) -> dict[str, Any]:
    """Codex ``function_call.arguments`` is a JSON string; degrade gracefully."""
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            return {"arguments": arguments}
        if isinstance(parsed, dict):
            return parsed
        return {"arguments": parsed}
    return {}


def _tool_output(output: Any) -> str:
    if isinstance(output, str):
        return output
    if isinstance(output, dict):
        inner = output.get("output")
        if isinstance(inner, str):
            return inner
        return json.dumps(output, ensure_ascii=False)
    return "" if output is None else str(output)


def _response_item_blocks(payload: dict[str, Any]) -> tuple[str, list[Block]] | None:
    """Map one ``response_item`` payload to a (role, blocks) pair, or None to skip."""
    match payload.get("type"):
        case "message":
            role = str(payload.get("role", ""))
            text = _message_text(payload)
            skip = (
                role not in {"user", "assistant"}
                or not text.strip()
                or (role == "user" and _is_noise_user_text(text))
            )
            return None if skip else (role, [TextBlock(text=text)])
        case "reasoning":
            text = _reasoning_text(payload)
            return ("assistant", [ThinkingBlock(text=text)]) if text else None
        case "function_call":
            block = ToolUseBlock(
                id=str(payload.get("call_id", "")),
                name=str(payload.get("name", "?")),
                input=_tool_input(payload.get("arguments")),
            )
            return "assistant", [block]
        case "function_call_output":
            result = ToolResultBlock(
                tool_use_id=str(payload.get("call_id", "")),
                content=_tool_output(payload.get("output")),
            )
            return "user", [result]
        case _:
            return None


def read_codex_session(path: Path) -> list[Event]:
    """Parse one rollout into the shared event model (same shape as Claude sessions).

    ``event_msg`` records are skipped (they duplicate ``response_item`` content);
    ``session_meta`` / ``turn_context`` records only feed the cwd/model stamps.
    """
    events: list[Event] = []
    cwd: str | None = None
    model: str | None = None

    for obj in _read_json_lines(path):
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            continue
        kind = obj.get("type")
        if kind in {"session_meta", "turn_context"}:
            cwd = payload.get("cwd") or cwd
            model = payload.get("model") or model
            continue
        if kind != "response_item":
            continue
        mapped = _response_item_blocks(payload)
        if mapped is None:
            continue
        role, blocks = mapped
        events.append(
            Message(
                uuid=f"codex-{len(events) + 1}",
                parent_uuid=None,
                timestamp=obj.get("timestamp") or "",
                role=role,
                blocks=blocks,
                model=model if role == "assistant" else None,
                cwd=cwd,
            )
        )
    return events


def codex_first_user_text(text: str) -> str:
    """The real prompt inside Codex's IDE-context wrapper (or the text itself)."""
    if _IDE_REQUEST_MARKER in text:
        return text.split(_IDE_REQUEST_MARKER, 1)[1].strip()
    return text.strip()


def summarize_codex_session(
    path: Path,
    index: dict[str, dict[str, Any]] | None = None,
) -> SessionSummary:
    """One-pass header for a rollout, in the shared :class:`SessionSummary` shape.

    ``ai_title`` carries the ``session_index.jsonl`` thread name when available.
    Only conversational ``message`` records count toward the message tallies, so
    the numbers are comparable with Claude session summaries.
    """
    session_id = _session_id_from_name(path) or path.stem
    first_user_text: str | None = None
    first_ts: str | None = None
    last_ts: str | None = None
    user_count = assistant_count = 0

    for obj in _read_json_lines(path):
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            continue
        if obj.get("type") == "session_meta":
            session_id = str(payload.get("id") or session_id)
            continue
        if obj.get("type") != "response_item" or payload.get("type") != "message":
            continue
        role = payload.get("role")
        if role not in {"user", "assistant"}:
            continue
        text = _message_text(payload)
        if not text.strip():
            continue
        if role == "user" and _is_noise_user_text(text):
            continue
        ts = obj.get("timestamp")
        if ts:
            if first_ts is None:
                first_ts = ts
            last_ts = ts
        if role == "user":
            user_count += 1
            if first_user_text is None:
                first_user_text = codex_first_user_text(text) or None
        else:
            assistant_count += 1

    row = (index or {}).get(session_id) or {}
    thread_name = row.get("thread_name")
    return SessionSummary(
        session_id=session_id,
        path=path,
        ai_title=str(thread_name) if thread_name else None,
        first_user_text=first_user_text,
        first_timestamp=first_ts,
        last_timestamp=last_ts,
        message_count=user_count + assistant_count,
        user_count=user_count,
        assistant_count=assistant_count,
        size_bytes=path.stat().st_size,
    )


# ---- Codex's own memory layer ----


def codex_memory_summary(codex_home: Path | None = None, max_chars: int = 2000) -> str | None:
    """The head of ``memories/memory_summary.md`` — Codex's consolidated profile.

    Model-written (not hand-curated), so callers should trust it below the
    curated Claude memory layer. Capped so it can ride inside a bounded packet.
    """
    path = _home(codex_home) / "memories" / "memory_summary.md"
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text:
        return None
    if max_chars > 0 and len(text) > max_chars:
        return text[:max_chars].rstrip() + "\n… [truncated]"
    return text


def _parse_summary_header(text: str) -> dict[str, str]:
    """The ``key: value`` block at the top of a rollout summary, up to the body."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            break
        key, sep, value = stripped.partition(":")
        if not sep:
            break
        out[key.strip()] = value.strip()
    return out


def _first_heading(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return None


def codex_rollout_summaries(
    codex_home: Path | None = None,
    cwd: Path | None = None,
) -> list[CodexRolloutSummary]:
    """Codex's per-session digests, optionally filtered to one project ``cwd``.

    These are the Codex analogue of Claude's compaction handoffs — the right
    altitude for "where did the Codex session leave off". Sorted newest first
    by ``updated_at``.
    """
    summaries_dir = _home(codex_home) / "memories" / "rollout_summaries"
    if not summaries_dir.is_dir():
        return []
    wanted = str(cwd.resolve()) if cwd is not None else None
    out: list[CodexRolloutSummary] = []
    for path in sorted(summaries_dir.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        header = _parse_summary_header(text)
        if wanted is not None and header.get("cwd") != wanted:
            continue
        out.append(
            CodexRolloutSummary(
                path=path,
                thread_id=header.get("thread_id"),
                cwd=header.get("cwd"),
                git_branch=header.get("git_branch"),
                updated_at=header.get("updated_at"),
                title=_first_heading(text),
            )
        )
    out.sort(key=lambda s: s.updated_at or "", reverse=True)
    return out


def read_codex_rollout_summary(
    name_or_thread_id: str,
    codex_home: Path | None = None,
) -> dict[str, Any]:
    """One rollout summary's full body, by filename or thread-id prefix.

    Raises ``FileNotFoundError`` / ``ValueError`` per the shared lookup contract.
    """
    rows = codex_rollout_summaries(codex_home)
    matches = [
        s
        for s in rows
        if name_or_thread_id in {s.path.name, s.path.stem}
        or (s.thread_id or "").startswith(name_or_thread_id)
    ]
    if not matches:
        raise FileNotFoundError(f"No Codex rollout summary matching {name_or_thread_id!r}")
    if len(matches) > 1:
        raise ValueError(
            f"{name_or_thread_id!r} matches {len(matches)} rollout summaries; "
            "pass a longer prefix or the filename."
        )
    s = matches[0]
    return {
        "file": s.path.name,
        "thread_id": s.thread_id,
        "cwd": s.cwd,
        "git_branch": s.git_branch,
        "updated_at": s.updated_at,
        "title": s.title,
        "body": s.path.read_text(encoding="utf-8"),
    }
