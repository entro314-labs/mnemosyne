"""Render parsed session events to markdown.

Three modes, picked by `RenderOptions.mode`:

- **transcript** (default): user prompts + assistant prose only. No tool I/O.
- **compact**: like transcript, plus a one-line summary per tool call
  (Read/LS/Glob/Grep/Edit/Write etc.) and a noise-stripped tool result for
  tools whose input *is* the value (Bash). TodoWrite is dropped.
- **full**: everything verbatim, with tool inputs and results fenced.

Each mode applies the same ack-line scrubber to tool_result content so
boilerplate like "Todos have been modified successfully…" never leaks through.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from claude_session_export.parser import (
    Attachment,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    _strip_reminder_wrappers,
    _unescape_if_encoded,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from claude_session_export.parser import Event


Mode = Literal["transcript", "compact", "full"]


@dataclass(slots=True)
class RenderOptions:
    mode: Mode = "transcript"
    include_thinking: bool = False
    include_attachments: bool = False
    include_reminders: bool = False
    max_tool_result_chars: int = 2000
    max_tool_input_chars: int = 2000


_NOISY_ATTACHMENTS = frozenset(
    {
        "hook_success",
        "todo_reminder",
        "hook_additional_context",
        "hook_non_blocking_error",
        "skill_listing",
        "deferred_tools_delta",
        "mcp_instructions_delta",
        "date_change",
        "queued_command",
        "compact_file_reference",
    }
)


# Tool-result boilerplate that adds nothing — strip from every tool_result.
_ACK_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"Todos have been modified successfully\.[^\n]*", re.MULTILINE),
    re.compile(r"The file .+? has been (?:created|updated) successfully\.[^\n]*", re.MULTILINE),
    re.compile(r"File created successfully at: [^\n]+", re.MULTILINE),
    re.compile(r"^<system-reminder>[\s\S]*?</system-reminder>", re.MULTILINE),
)

# Tools whose existence-of-call is the interesting part; in compact mode their
# input is shown inline and their result is replaced by a "N chars" indicator.
_INLINE_TOOLS = frozenset({"Read", "LS", "Glob", "Grep", "Edit", "MultiEdit", "Write"})

# Tools dropped entirely (input + matching result) in compact mode.
_SKIPPED_TOOLS = frozenset({"TodoWrite"})


def _scrub_ack(content: str) -> str:
    for pat in _ACK_PATTERNS:
        content = pat.sub("", content)
    return content.strip()


def _truncate(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + f"\n… [truncated {len(text) - limit} chars]"


def _fence(text: str, lang: str = "") -> str:
    n = 3
    while "`" * n in text:
        n += 1
    fence = "`" * n
    return f"{fence}{lang}\n{text}\n{fence}"


def _render_user_text(text: str, opts: RenderOptions) -> str:
    # Always normalize JSON-escape-encoded paste-ins ("\\n\\n" → real newlines);
    # only strip system wrappers when the user didn't ask to keep them visible.
    body = _unescape_if_encoded(text)
    if not opts.include_reminders:
        body = _strip_reminder_wrappers(body)
    return body.strip()


# ---- compact rendering ----


def _compact_one_liner(call: ToolUseBlock, result: ToolResultBlock | None) -> str | None:  # noqa: PLR0911
    """Return a one-line summary for trivial inspection tools, or None."""
    inp = call.input or {}
    err = " ❌" if (result and result.is_error) else ""
    result_chars = len(result.content) if result else 0

    match call.name:
        case "Read":
            return f"📄 **Read** `{inp.get('file_path', '?')}`{err} _({result_chars:,} chars)_"
        case "LS":
            return f"📁 **LS** `{inp.get('path', '.')}`{err}"
        case "Glob":
            pat = inp.get("pattern", "?")
            where = inp.get("path") or ""
            suffix = f" in `{where}`" if where else ""
            return f"🔍 **Glob** `{pat}`{suffix}{err} _({result_chars:,} chars)_"
        case "Grep":
            pat = inp.get("pattern", "?")
            where = inp.get("path") or ""
            suffix = f" in `{where}`" if where else ""
            return f"🔎 **Grep** `{pat}`{suffix}{err} _({result_chars:,} chars)_"
        case "Edit" | "MultiEdit":
            return f"✏️ **{call.name}** `{inp.get('file_path', '?')}`{err}"
        case "Write":
            n = len(inp.get("content", ""))
            return f"✍️ **Write** `{inp.get('file_path', '?')}`{err} _({n:,} chars written)_"
    return None


def _compact_bash(call: ToolUseBlock, result: ToolResultBlock | None, opts: RenderOptions) -> str:
    """Bash gets special treatment: the command IS the value; show the result body too."""
    inp = call.input or {}
    cmd = (inp.get("command") or "").rstrip()
    if not cmd:
        return "🐚 **Bash** _(empty command)_"
    if "\n" in cmd or len(cmd) > 120:
        head = f"🐚 **Bash**\n\n{_fence(_truncate(cmd, opts.max_tool_input_chars), 'sh')}"
    else:
        head = f"🐚 **Bash** `{cmd}`"
    if result is None:
        return head
    err = " ❌" if result.is_error else ""
    body = _truncate(_scrub_ack(result.content), opts.max_tool_result_chars)
    if not body.strip():
        return f"{head}{err}"
    return f"{head}{err}\n\n{_fence(body)}"


def _render_full_tool_use(call: ToolUseBlock, opts: RenderOptions) -> str:
    pretty = json.dumps(call.input, indent=2, ensure_ascii=False, default=str)
    pretty = _truncate(pretty, opts.max_tool_input_chars)
    return f"**🔧 Tool call: `{call.name}`**\n\n{_fence(pretty, 'json')}"


def _render_full_tool_result(b: ToolResultBlock, opts: RenderOptions) -> str | None:
    body = _scrub_ack(b.content)
    if not body.strip():
        return None
    body = _truncate(body, opts.max_tool_result_chars)
    label = "❌ Tool error" if b.is_error else "📤 Tool result"
    return f"**{label}**\n\n{_fence(body)}"


# ---- main render loop ----


def _build_result_index(events: Iterable[Event]) -> dict[str, ToolResultBlock]:
    idx: dict[str, ToolResultBlock] = {}
    for ev in events:
        if isinstance(ev, Message):
            for b in ev.blocks:
                if isinstance(b, ToolResultBlock):
                    idx[b.tool_use_id] = b
    return idx


def _message_parts(  # noqa: PLR0912
    msg: Message,
    opts: RenderOptions,
    *,
    results_by_id: dict[str, ToolResultBlock],
    consumed_result_ids: set[str],
) -> list[str]:
    """Render a Message's blocks to a list of body paragraphs (no role header).

    Shared by markdown chunk building and format-agnostic turn collection.
    """
    parts: list[str] = []
    for b in msg.blocks:
        match b:
            case TextBlock():
                rendered = _render_user_text(b.text, opts) if msg.role == "user" else b.text.strip()
                if rendered:
                    parts.append(rendered)

            case ThinkingBlock():
                if opts.include_thinking:
                    parts.append(
                        f"<details><summary>💭 thinking</summary>\n\n{b.text}\n\n</details>"
                    )

            case ToolUseBlock():
                if opts.mode == "transcript":
                    continue
                if opts.mode == "compact":
                    if b.name in _SKIPPED_TOOLS:
                        consumed_result_ids.add(b.id)
                        continue
                    result = results_by_id.get(b.id)
                    if b.name in _INLINE_TOOLS:
                        line = _compact_one_liner(b, result)
                        if line is not None:
                            parts.append(line)
                            consumed_result_ids.add(b.id)
                            continue
                    if b.name == "Bash":
                        parts.append(_compact_bash(b, result, opts))
                        consumed_result_ids.add(b.id)
                        continue
                    # Other tools: render full call but use scrubbed result.
                parts.append(_render_full_tool_use(b, opts))

            case ToolResultBlock():
                if opts.mode == "transcript":
                    continue
                if b.tool_use_id in consumed_result_ids:
                    continue
                rendered_block = _render_full_tool_result(b, opts)
                if rendered_block:
                    parts.append(rendered_block)
    return parts


def _is_tool_result_only_user(msg: Message) -> bool:
    return (
        msg.role == "user"
        and bool(msg.blocks)
        and all(isinstance(b, ToolResultBlock) for b in msg.blocks)
    )


def _render_message(
    msg: Message,
    opts: RenderOptions,
    *,
    results_by_id: dict[str, ToolResultBlock],
    consumed_result_ids: set[str],
) -> str | None:
    parts = _message_parts(
        msg,
        opts,
        results_by_id=results_by_id,
        consumed_result_ids=consumed_result_ids,
    )
    if not parts:
        return None
    if _is_tool_result_only_user(msg):
        # Parallel-tool fan-in turns lose their "User" header — the 📤 markers
        # already convey what these are.
        return "\n\n".join(parts)
    icon = "👤 **User**" if msg.role == "user" else "🤖 **Assistant**"
    suffix = f"  _({msg.timestamp})_" if msg.timestamp else ""
    return f"### {icon}{suffix}\n\n" + "\n\n".join(parts)


@dataclass(slots=True)
class Turn:
    """A coalesced conversational turn, ready for any output format."""

    role: str  # "user" | "assistant"
    timestamp: str | None
    body: str  # rendered paragraphs joined by blank lines; mode-aware


def collect_turns(
    events: Iterable[Event],
    opts: RenderOptions | None = None,
) -> list[Turn]:
    """Walk events through the same pipeline as `render_markdown` and return
    a list of coalesced turns.

    - Tool-result-only user messages (parallel fan-in) attach to the previous
      turn's body rather than creating a new turn.
    - Adjacent same-role turns merge into one (matching the markdown coalescer).
    - Attachments are skipped here; they have no role and are rendered as
      separate markdown chunks only.
    """
    opts = opts or RenderOptions()
    events_list = list(events)
    results_by_id = _build_result_index(events_list)
    consumed_result_ids: set[str] = set()
    turns: list[Turn] = []

    for ev in events_list:
        if not isinstance(ev, Message):
            continue
        parts = _message_parts(
            ev,
            opts,
            results_by_id=results_by_id,
            consumed_result_ids=consumed_result_ids,
        )
        if not parts:
            continue
        body = "\n\n".join(parts)
        if _is_tool_result_only_user(ev):
            if turns:
                turns[-1] = Turn(
                    role=turns[-1].role,
                    timestamp=turns[-1].timestamp,
                    body=turns[-1].body + "\n\n" + body,
                )
            # If there's no previous turn, the orphaned tool result is dropped —
            # there's nothing meaningful to attach it to.
            continue
        if turns and turns[-1].role == ev.role:
            turns[-1] = Turn(
                role=turns[-1].role,
                timestamp=turns[-1].timestamp,
                body=turns[-1].body + "\n\n" + body,
            )
        else:
            turns.append(Turn(role=ev.role, timestamp=ev.timestamp, body=body))
    return turns


def _render_attachment(att: Attachment, opts: RenderOptions) -> str | None:
    if not opts.include_attachments:
        return None
    if att.attachment_type in _NOISY_ATTACHMENTS and not att.content:
        return None
    content = att.content or json.dumps(att.extra, indent=2, ensure_ascii=False, default=str)
    content = _truncate(content, opts.max_tool_result_chars)
    summary = f"📎 attachment: {att.attachment_type}"
    return f"<details><summary>{summary}</summary>\n\n{_fence(content)}\n\n</details>"


def _chunk_role(chunk: str) -> str | None:
    """Return 'assistant' / 'user' if the chunk starts with a role header, else None."""
    first = chunk.split("\n", 1)[0]
    if first.startswith("### 🤖 **Assistant**"):
        return "assistant"
    if first.startswith("### 👤 **User**"):
        return "user"
    return None


def _strip_first_header(chunk: str) -> str:
    """Drop the leading `### 🤖/👤 …` line and any single trailing blank."""
    lines = chunk.split("\n")
    if not lines or not (lines[0].startswith("### 🤖") or lines[0].startswith("### 👤")):
        return chunk
    rest = lines[1:]
    while rest and not rest[0]:
        rest = rest[1:]
    return "\n".join(rest)


def _coalesce_same_role(chunks: list[str]) -> list[str]:
    """Merge adjacent chunks that share a role header.

    Consecutive assistant turns (or consecutive user turns) are concatenated
    under the *first* header, dropping the later header(s) and timestamp(s) so
    they read as one continuous block.
    """
    if not chunks:
        return chunks
    out: list[str] = []
    for chunk in chunks:
        cur = _chunk_role(chunk)
        prev = _chunk_role(out[-1]) if out else None
        if cur is not None and cur == prev:
            body = _strip_first_header(chunk).strip()
            if body:
                out[-1] = out[-1].rstrip() + "\n\n" + body
        else:
            out.append(chunk)
    return out


def _inject_anchors(chunks: list[str], session_id: str) -> list[str]:
    """Prepend a stable HTML anchor before each role-headed chunk.

    Anchor format: ``<a id="t-{short}-{turn_index}"></a>`` where ``short`` is
    the first 8 chars of the session id. Lets external tools link to a
    specific turn (e.g. ``other-session.md#t-abc12345-3``).
    """
    short = (session_id or "x")[:8]
    out: list[str] = []
    turn_idx = 0
    for chunk in chunks:
        if _chunk_role(chunk) is not None:
            anchor = f'<a id="t-{short}-{turn_idx}"></a>'
            out.append(f"{anchor}\n\n{chunk}")
            turn_idx += 1
        else:
            out.append(chunk)
    return out


def render_markdown(
    events: Iterable[Event],
    *,
    title: str | None = None,
    opts: RenderOptions | None = None,
    session_id: str | None = None,
) -> str:
    opts = opts or RenderOptions()
    events_list = list(events)

    results_by_id = _build_result_index(events_list)
    consumed_result_ids: set[str] = set()

    chunks: list[str] = []
    if title:
        chunks.append(f"# {title}")

    for ev in events_list:
        match ev:
            case Message():
                rendered = _render_message(
                    ev,
                    opts,
                    results_by_id=results_by_id,
                    consumed_result_ids=consumed_result_ids,
                )
            case Attachment():
                rendered = _render_attachment(ev, opts)
            case _:
                rendered = None
        if rendered:
            chunks.append(rendered)

    chunks = _coalesce_same_role(chunks)
    if session_id:
        chunks = _inject_anchors(chunks, session_id)
    return "\n\n---\n\n".join(chunks) + "\n"
