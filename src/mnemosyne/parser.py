"""Parse Claude Code session JSONL files into typed events.

Sessions live in ~/.claude/projects/<project-slug>/<session-uuid>.jsonl
The slug is the absolute cwd with "/", "." and "_" each replaced by "-", so it is
lossy and not injective: distinct trees can share one archive directory. The `cwd`
recorded on each record is the authoritative project identity — see
`session_touches_project`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator


@dataclass(slots=True)
class TextBlock:
    text: str


@dataclass(slots=True)
class ThinkingBlock:
    text: str


@dataclass(slots=True)
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(slots=True)
class ToolResultBlock:
    tool_use_id: str
    content: str
    is_error: bool = False


@dataclass(slots=True)
class AttachmentBlock:
    """A non-text content block carried inline in a message.

    Covers `image`, `document`, and `fallback` records (and anything else the
    vendor adds later). The base64 payload is deliberately NOT retained — a single
    screenshot is ~180KB of base64 and a PDF over 1MB, which would dwarf the
    transcript it belongs to. The decoded byte size is kept instead, so the export
    records that something was attached and how big it was without embedding it.
    """

    kind: str
    media_type: str | None = None
    title: str | None = None
    byte_size: int | None = None
    detail: dict[str, Any] = field(default_factory=dict)


Block = TextBlock | ThinkingBlock | ToolUseBlock | ToolResultBlock | AttachmentBlock


@dataclass(slots=True)
class Message:
    uuid: str
    parent_uuid: str | None
    timestamp: str
    role: str  # "user" | "assistant"
    blocks: list[Block]
    model: str | None = None
    cwd: str | None = None
    git_branch: str | None = None


@dataclass(slots=True)
class Attachment:
    uuid: str | None
    parent_uuid: str | None
    timestamp: str | None
    attachment_type: str
    content: str | None
    extra: dict[str, Any] = field(default_factory=dict)


Event = Message | Attachment


@dataclass(slots=True)
class SessionSummary:
    session_id: str
    path: Path
    ai_title: str | None
    first_user_text: str | None
    first_timestamp: str | None
    last_timestamp: str | None
    message_count: int
    user_count: int
    assistant_count: int
    size_bytes: int
    # Non-empty lines that failed to decode as JSON. A transient count of 1 is
    # normal for an actively-appended log (a partially written final record); a
    # persistent or growing count signals real source corruption.
    malformed_lines: int = 0


def _coerce_tool_result_content(raw: Any) -> str:
    """tool_result.content can be str or list[{type,text|tool_name}]. Normalize to str."""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        parts: list[str] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            t = item.get("type")
            if t == "text":
                parts.append(str(item.get("text", "")))
            elif t == "tool_reference":
                parts.append(f"[→ used tool: {item.get('tool_name', '?')}]")
            else:
                parts.append(f"[unhandled tool_result block: {t}]")
        return "\n".join(parts)
    return "" if raw is None else str(raw)


def _parse_blocks(content: Any) -> list[Block]:
    """Convert a message.content list (or str) into typed blocks."""
    if isinstance(content, str):
        return [TextBlock(text=content)]
    if not isinstance(content, list):
        return []

    blocks: list[Block] = []
    for b in content:
        if not isinstance(b, dict):
            continue
        bt = b.get("type")
        match bt:
            case "text":
                blocks.append(TextBlock(text=str(b.get("text", ""))))
            case "thinking":
                # Signatures are noise; only the text is interesting.
                text = str(b.get("thinking", ""))
                if text.strip():
                    blocks.append(ThinkingBlock(text=text))
            case "tool_use":
                blocks.append(
                    ToolUseBlock(
                        id=str(b.get("id", "")),
                        name=str(b.get("name", "?")),
                        input=b.get("input") or {},
                    )
                )
            case "tool_result":
                blocks.append(
                    ToolResultBlock(
                        tool_use_id=str(b.get("tool_use_id", "")),
                        content=_coerce_tool_result_content(b.get("content")),
                        is_error=bool(b.get("is_error", False)),
                    )
                )
            case _:
                # Never drop silently: preserve type + safe metadata so the
                # export records that content existed here (source-integrity rule).
                blocks.append(_parse_attachment_block(b, str(bt) if bt else "unknown"))
    return blocks


# base64 encodes 3 bytes as 4 chars; padding costs at most 2 bytes.
def _b64_size(data: str) -> int:
    return max(0, (len(data) * 3) // 4 - data.count("="))


_ATTACHMENT_PAYLOAD_KEYS = frozenset({"type", "source", "title"})


def _parse_attachment_block(b: dict[str, Any], kind: str) -> AttachmentBlock:
    """Normalize an inline non-text block, dropping only the binary payload."""
    source = b.get("source")
    media_type: str | None = None
    byte_size: int | None = None
    if isinstance(source, dict):
        raw_media = source.get("media_type")
        media_type = str(raw_media) if raw_media else None
        data = source.get("data")
        if isinstance(data, str):
            byte_size = _b64_size(data)
    raw_title = b.get("title")
    # Everything else is small structured metadata (e.g. `fallback`'s from/to
    # models) and is worth keeping verbatim.
    detail = {k: v for k, v in b.items() if k not in _ATTACHMENT_PAYLOAD_KEYS}
    return AttachmentBlock(
        kind=kind,
        media_type=media_type,
        title=str(raw_title) if raw_title else None,
        byte_size=byte_size,
        detail=detail,
    )


def _parse_message(obj: dict[str, Any], role: str) -> Message:
    msg = obj.get("message") or {}
    return Message(
        uuid=str(obj.get("uuid", "")),
        parent_uuid=obj.get("parentUuid"),
        timestamp=str(obj.get("timestamp", "")),
        role=role,
        blocks=_parse_blocks(msg.get("content")),
        model=msg.get("model") if isinstance(msg, dict) else None,
        cwd=obj.get("cwd"),
        git_branch=obj.get("gitBranch"),
    )


def _parse_attachment(obj: dict[str, Any]) -> Attachment:
    a = obj.get("attachment") or {}
    return Attachment(
        uuid=obj.get("uuid"),
        parent_uuid=obj.get("parentUuid"),
        timestamp=obj.get("timestamp"),
        attachment_type=str(a.get("type", "?")),
        content=a.get("content") if isinstance(a.get("content"), str) else None,
        extra={k: v for k, v in a.items() if k not in {"type", "content"}},
    )


def _is_filler_assistant(obj: dict[str, Any]) -> bool:
    """Skip assistant turns whose entire payload is a `stop_sequence`-terminated text.

    These show up across all projects as API/auth/credit errors, overload notices,
    quota-reset banners, or 'No response requested.' control responses. They are
    never real conversational content. The `stop_reason` is the universal signal —
    no string denylist needed.
    """
    msg = obj.get("message") or {}
    if msg.get("stop_reason") != "stop_sequence":
        return False
    content = msg.get("content")
    if not isinstance(content, list) or not content:
        return False
    return all(isinstance(b, dict) and b.get("type") == "text" for b in content)


def iter_events(path: Path) -> Iterator[Event]:
    """Yield Message and Attachment events from a session JSONL, in file order.

    Skips:
    - bookkeeping records (queue-operation, file-history-snapshot, ai-title, …)
    - user messages with `isMeta: True` (Claude Code-injected control messages
      like "Continue from where you left off." or skill-init notices)
    - assistant messages that are stop_sequence-terminated text-only filler/errors
    """
    with path.open(encoding="utf-8") as f:
        for raw in f:
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            match obj.get("type"):
                case "user":
                    if obj.get("isMeta"):
                        continue
                    yield _parse_message(obj, "user")
                case "assistant":
                    if _is_filler_assistant(obj):
                        continue
                    yield _parse_message(obj, "assistant")
                case "attachment":
                    yield _parse_attachment(obj)
                case _:
                    continue


def read_session(path: Path) -> list[Event]:
    return list(iter_events(path))


def _extract_first_user_text(obj: dict[str, Any]) -> str | None:
    blocks = _parse_blocks(obj.get("message", {}).get("content"))
    for b in blocks:
        if isinstance(b, TextBlock) and b.text.strip():
            stripped = _strip_reminder_wrappers(_unescape_if_encoded(b.text)).strip()
            if stripped:
                return stripped
    return None


def summarize_session(path: Path) -> SessionSummary:
    """Build a one-line-per-session summary without loading the full file into memory."""
    session_id = path.stem
    ai_title: str | None = None
    first_user_text: str | None = None
    first_ts: str | None = None
    last_ts: str | None = None
    user_count = 0
    assistant_count = 0
    msg_count = 0
    malformed = 0

    with path.open(encoding="utf-8") as f:
        for raw in f:
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                malformed += 1
                continue
            t = obj.get("type")
            if t == "ai-title":
                ai_title = obj.get("aiTitle") or ai_title
                continue
            if t not in {"user", "assistant"}:
                continue
            if t == "user" and obj.get("isMeta"):
                continue
            if t == "assistant" and _is_filler_assistant(obj):
                continue
            msg_count += 1
            ts = obj.get("timestamp")
            if ts:
                if first_ts is None:
                    first_ts = ts
                last_ts = ts
            if t == "user":
                user_count += 1
                if first_user_text is None:
                    first_user_text = _extract_first_user_text(obj)
            else:
                assistant_count += 1

    return SessionSummary(
        session_id=session_id,
        path=path,
        ai_title=ai_title,
        first_user_text=first_user_text,
        first_timestamp=first_ts,
        last_timestamp=last_ts,
        message_count=msg_count,
        user_count=user_count,
        assistant_count=assistant_count,
        size_bytes=path.stat().st_size,
        malformed_lines=malformed,
    )


# ---- helpers used by both parser and renderer ----

_REMINDER_TAGS = (
    "system-reminder",
    "ide_opened_file",
    "ide_selection",
    "command-name",
    "command-message",
    "command-args",
    "local-command-stdout",
    "local-command-stderr",
    "local-command-caveat",
)
_REMINDER_RE = re.compile(
    r"<(" + "|".join(_REMINDER_TAGS) + r")\b[^>]*>.*?</\1>",
    re.DOTALL,
)

# Claude Code wraps subagent task completions in <task-notification>…</task-notification>
# with metadata (task-id, tool-use-id, output-file, usage stats) around the actual
# <result>. The wrapper is pure noise — but the result body is the agent's report
# and should be kept. Unwrap instead of strip.
_TASK_NOTIFICATION_RE = re.compile(
    r"<task-notification>([\s\S]*?)</task-notification>",
    re.DOTALL,
)
_TN_SUMMARY_RE = re.compile(r"<summary>([\s\S]*?)</summary>", re.DOTALL)
_TN_RESULT_RE = re.compile(r"<result>([\s\S]*?)</result>", re.DOTALL)
_TN_STATUS_RE = re.compile(r"<status>([\s\S]*?)</status>", re.DOTALL)


def _unwrap_task_notification(match: re.Match[str]) -> str:
    body = match.group(1)
    summary = _TN_SUMMARY_RE.search(body)
    result = _TN_RESULT_RE.search(body)
    status = _TN_STATUS_RE.search(body)
    summary_text = summary.group(1).strip() if summary else "Agent task"
    status_text = status.group(1).strip() if status else ""
    result_text = result.group(1).strip() if result else ""
    header = f"**🤖 Agent: {summary_text}**"
    if status_text and status_text != "completed":
        header += f" _({status_text})_"
    return f"{header}\n\n{result_text}" if result_text else header


_UNESCAPE_SENTINEL = "\x00BS\x00"


def _unescape_if_encoded(text: str) -> str:
    """If text looks JSON-escape-encoded, unescape `\\n`, `\\t`, `\\"`, `\\\\`.

    Heuristic: at least 3 literal `\\n` sequences AND more literal `\\n` than
    real newlines. Catches prompts pasted from a source that serialized the
    string (e.g., a JSON value copied verbatim, a regex pattern) without
    touching ordinary prose that happens to mention `\\n`.
    """
    real_newlines = text.count("\n")
    escape_n = text.count("\\n")
    if escape_n < 3 or escape_n <= real_newlines:
        return text
    # Protect `\\` first via sentinel so `\\n` inside `\\\\n` survives correctly.
    out = text.replace("\\\\", _UNESCAPE_SENTINEL)
    out = out.replace("\\n", "\n").replace("\\t", "\t")
    out = out.replace('\\"', '"').replace("\\'", "'")
    return out.replace(_UNESCAPE_SENTINEL, "\\")


def _strip_reminder_wrappers(text: str) -> str:
    """Strip system-injected wrappers and unwrap `<task-notification>` to its body.

    Drops `<system-reminder>`, `<ide_opened_file>`, `<ide_selection>`,
    `<command-name>` family, and `<local-command-*>` (and their bodies).
    Replaces `<task-notification>` with its `<summary>` + `<result>` body.
    """
    text = _TASK_NOTIFICATION_RE.sub(_unwrap_task_notification, text)
    return _REMINDER_RE.sub("", text)


# Claude Code flattens a path into a slug by replacing the separator AND the two
# characters it treats as unsafe in a directory name. Verified against every slug
# directory in a real archive: `/`, `.`, and `_` all collapse to `-`
# (`/Users/x/.ssh` → `-Users-x--ssh`, `…/macos_contacts` → `…-macos-contacts`).
# Replacing only `/` silently resolves such projects to a directory that does not
# exist, which reads as "no archive" rather than as an error.
_SLUG_UNSAFE_RE = re.compile(r"[/._]")


def project_slug(cwd: Path) -> str:
    """Flatten an absolute working directory into its Claude Code archive slug."""
    return _SLUG_UNSAFE_RE.sub("-", str(cwd.resolve()))


def project_dir_for_cwd(cwd: Path, claude_home: Path | None = None) -> Path:
    """Map an absolute working directory to its ~/.claude/projects slug."""
    home = claude_home or Path.home() / ".claude" / "projects"
    return home / project_slug(cwd)


def path_in_project(cwd: str, project_path: Path) -> bool:
    """True when a recorded `cwd` is the project root itself or a directory below it."""
    try:
        candidate = Path(cwd)
    except (TypeError, ValueError):
        return False
    return candidate == project_path or project_path in candidate.parents


def session_touches_project(path: Path, project_path: Path) -> bool:
    """True when any `cwd` recorded in a session lies within `project_path`.

    The slug encoding is lossy and not injective, so one archive directory can
    hold sessions belonging to different working trees (`foo.bar` and `foo_bar`
    both flatten to `foo-bar`). The recorded `cwd` is authoritative; a session is
    in scope when it did any work at or below the requested root.

    Scanning stops at the first match, so the common case (a session that starts
    in the project) costs one line. Lines without a ``cwd`` key are skipped on a
    substring check before any JSON decoding — bookkeeping records (file-history
    snapshots, tool results) can run to many kilobytes and never carry one.
    """
    try:
        with path.open(encoding="utf-8") as f:
            for raw in f:
                if '"cwd"' not in raw:
                    continue
                stripped = raw.strip()
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                cwd = obj.get("cwd")
                if cwd and path_in_project(str(cwd), project_path):
                    return True
    except OSError:
        return False
    return False


def list_session_files(project_dir: Path, *, project_path: Path | None = None) -> list[Path]:
    """Session files in an archive directory, newest-name-sorted.

    Pass ``project_path`` to restrict the result to sessions that actually worked
    in that tree; omit it to take the directory at face value (explicit
    all-projects scope, or selection by an unambiguous session id).
    """
    files = sorted(project_dir.glob("*.jsonl"))
    if project_path is None:
        return files
    root = project_path.resolve()
    return [p for p in files if session_touches_project(p, root)]
