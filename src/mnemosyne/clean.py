"""Deterministic noise-reduction passes for transcript text.

Inspired by repomix's deterministic cleanup pipeline: every function here is
**pure**, **idempotent**, and **language-agnostic**. The same input always yields
the same output, and re-running a pass over its own output is a no-op. No model,
no heuristics that depend on wall-clock or randomness — just mechanical stripping
of machine noise that costs tokens and conveys nothing:

- VT/ANSI escape sequences (colour codes, cursor moves) left in captured CLI output
- carriage-return progress bars (``npm install``, download spinners) that bloat a
  single logical line into thousands of overwrite frames
- base64 / data-URI blobs (pasted screenshots, embedded binaries)
- ragged whitespace — trailing spaces and runs of blank lines, including the gaps
  left behind when wrapper tags are stripped upstream

These compose into :func:`scrub_tool_output` (applied to captured tool output) and
:func:`normalize_whitespace` (applied as the final pass over a rendered document).
"""

from __future__ import annotations

import re

# ---- ANSI / VT escape sequences -------------------------------------------------

# CSI: ESC [ ... <final byte>   (the common colour / cursor-move codes)
_ANSI_CSI = r"\[[0-9;:?]*[ -/]*[@-~]"
# OSC: ESC ] ... terminated by BEL or ST   (window-title sets, hyperlinks)
_ANSI_OSC = r"\][^\x07\x1b]*(?:\x07|\x1b\\)"
# Other C1 two-byte escapes: ESC followed by @..Z, '\', '^' or '_'.
_ANSI_RE = re.compile(r"\x1b(?:" + _ANSI_CSI + r"|" + _ANSI_OSC + r"|[@-Z\\^_])")


def strip_ansi(text: str) -> str:
    """Remove ANSI/VT escape sequences (colour codes, cursor moves, OSC titles).

    Tool output captured from a TTY is riddled with escape codes that render as
    noise in a plaintext transcript. Stripping them is fully deterministic.
    """
    if "\x1b" not in text:
        return text
    return _ANSI_RE.sub("", text)


# ---- carriage-return progress bars ----------------------------------------------


def collapse_carriage_returns(text: str) -> str:
    r"""Emulate a terminal's handling of lone ``\r`` and keep only the final state.

    Progress bars redraw a line by emitting ``\r`` then overwriting; the bytes
    before the last ``\r`` on a line are never visible to a human. CRLF line
    endings are normalised to ``\n`` first so real line breaks survive.
    """
    if "\r" not in text:
        return text
    text = text.replace("\r\n", "\n")
    if "\r" not in text:
        return text
    return "\n".join(
        line.rsplit("\r", 1)[-1] if "\r" in line else line for line in text.split("\n")
    )


# ---- base64 / data-URI truncation -----------------------------------------------

_DATA_URI_RE = re.compile(r"(data:[\w.+-]+/[\w.+-]+;base64,)([A-Za-z0-9+/]{40,}={0,2})")
# A standalone base64 run must be long and bounded by non-base64 chars to qualify.
_LONG_B64_RE = re.compile(r"(?<![A-Za-z0-9+/=])([A-Za-z0-9+/]{256,}={0,2})(?![A-Za-z0-9+/=])")
_B64_KEEP = 32


def _looks_base64(s: str) -> bool:
    """Reject false positives (long hex strings, identifiers, prose-without-spaces).

    Real base64 of binary data is high-entropy: it mixes letter case with digits.
    Matches repomix's heuristic — require a digit plus both an upper- and a
    lower-case letter (or a ``+``/``/`` byte) before treating a run as base64.
    """
    has_digit = any(c.isdigit() for c in s)
    has_upper = any(c.isupper() for c in s)
    has_lower = any(c.islower() for c in s)
    has_symbol = "+" in s or "/" in s
    return has_digit and has_upper and (has_lower or has_symbol)


def _shorten_b64(run: str) -> str:
    return f"{run[:_B64_KEEP]}…[+{len(run) - _B64_KEEP} base64 chars]"


def truncate_base64(text: str) -> str:
    """Replace long base64 payloads (data URIs, embedded blobs) with a short stub.

    Keeps the first ``32`` characters plus a ``…[+N base64 chars]`` marker so the
    reader knows what was elided and how big it was, without paying the tokens.
    """

    def _data_uri(m: re.Match[str]) -> str:
        payload = m.group(2)
        if len(payload) <= _B64_KEEP:
            return m.group(0)
        return m.group(1) + _shorten_b64(payload)

    def _standalone(m: re.Match[str]) -> str:
        run = m.group(1)
        return _shorten_b64(run) if _looks_base64(run) else run

    text = _DATA_URI_RE.sub(_data_uri, text)
    return _LONG_B64_RE.sub(_standalone, text)


# ---- whitespace normalisation ---------------------------------------------------

_BLANK_RUN_RE = re.compile(r"\n{3,}")


def normalize_whitespace(text: str) -> str:
    """Right-trim every line and collapse runs of 3+ newlines to a single blank line.

    Removes the ragged gaps left behind when wrapper tags / ack lines are stripped
    upstream, without disturbing intentional paragraph separation (``\\n\\n``).
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = _BLANK_RUN_RE.sub("\n\n", text)
    return text.strip("\n")


# ---- composed pass for captured tool output -------------------------------------


def scrub_tool_output(text: str) -> str:
    """Full deterministic scrub for captured command/tool output.

    Order matters: collapse progress bars first so escape codes embedded in the
    surviving frame are then stripped, shrink base64 blobs, and finally tidy the
    whitespace the earlier passes left behind.
    """
    if not text:
        return text
    text = collapse_carriage_returns(text)
    text = strip_ansi(text)
    text = truncate_base64(text)
    return normalize_whitespace(text)
