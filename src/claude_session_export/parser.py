"""Parse Claude Code session JSONL files into typed events.

Sessions live in ~/.claude/projects/<project-slug>/<session-uuid>.jsonl
The slug is the absolute cwd with "/" replaced by "-" and a leading "-".
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


Block = TextBlock | ThinkingBlock | ToolUseBlock | ToolResultBlock


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
                # Unknown block types are dropped silently.
                continue
    return blocks


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

    with path.open(encoding="utf-8") as f:
        for raw in f:
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            t = obj.get("type")
            if t == "ai-title":
                ai_title = obj.get("aiTitle") or ai_title
                continue
            if t not in {"user", "assistant"}:
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


def project_dir_for_cwd(cwd: Path, claude_home: Path | None = None) -> Path:
    """Map an absolute working directory to its ~/.claude/projects slug."""
    home = claude_home or Path.home() / ".claude" / "projects"
    slug = str(cwd.resolve()).replace("/", "-")
    return home / slug


def list_session_files(project_dir: Path) -> list[Path]:
    return sorted(project_dir.glob("*.jsonl"))
