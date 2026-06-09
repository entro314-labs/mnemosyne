"""Tests for clean.py — deterministic noise-reduction passes.

Every pass must be deterministic (same input → same output) and idempotent
(running it over its own output is a no-op).
"""

from __future__ import annotations

from mnemosyne.clean import (
    collapse_carriage_returns,
    normalize_whitespace,
    scrub_tool_output,
    strip_ansi,
    truncate_base64,
)

# ---- strip_ansi ----


def test_strip_ansi_removes_color_codes() -> None:
    colored = "\x1b[31mERROR\x1b[0m: build \x1b[1;32mpassed\x1b[0m"
    assert strip_ansi(colored) == "ERROR: build passed"


def test_strip_ansi_removes_cursor_moves() -> None:
    assert strip_ansi("loading\x1b[2K\x1b[1Gdone") == "loadingdone"


def test_strip_ansi_removes_osc_title() -> None:
    assert strip_ansi("\x1b]0;my-title\x07hello") == "hello"


def test_strip_ansi_noop_without_escapes() -> None:
    plain = "no escapes here, just text"
    assert strip_ansi(plain) is plain  # fast-path returns the same object


def test_strip_ansi_idempotent() -> None:
    colored = "\x1b[31mred\x1b[0m \x1b[34mblue\x1b[0m"
    once = strip_ansi(colored)
    assert strip_ansi(once) == once


# ---- collapse_carriage_returns ----


def test_collapse_cr_keeps_final_progress_frame() -> None:
    progress = "Downloading\r 10%\r 50%\r100% done"
    assert collapse_carriage_returns(progress) == "100% done"


def test_collapse_cr_preserves_crlf_line_endings() -> None:
    crlf = "line one\r\nline two\r\nline three"
    assert collapse_carriage_returns(crlf) == "line one\nline two\nline three"


def test_collapse_cr_per_line_independent() -> None:
    text = "a\rA\nb\rB"
    assert collapse_carriage_returns(text) == "A\nB"


def test_collapse_cr_noop_without_cr() -> None:
    plain = "no carriage returns\nhere"
    assert collapse_carriage_returns(plain) is plain


def test_collapse_cr_idempotent() -> None:
    progress = "x\r 1\r 2\r 3 final"
    once = collapse_carriage_returns(progress)
    assert collapse_carriage_returns(once) == once


# ---- truncate_base64 ----


def test_truncate_data_uri_payload() -> None:
    payload = "iVBORw0KGgoAAAANSUhEUg" + "ABCdef123/+" * 30
    text = f"![img](data:image/png;base64,{payload})"
    out = truncate_base64(text)
    assert "data:image/png;base64," in out  # prefix kept
    assert payload not in out  # full blob gone
    assert "base64 chars]" in out


def test_truncate_standalone_long_base64() -> None:
    blob = "AaBbCc0123456789+/" * 20  # 360 chars, mixed case + digits + symbols
    text = f"hash is {blob} end"
    out = truncate_base64(text)
    assert blob not in out
    assert "base64 chars]" in out
    assert out.startswith("hash is ")
    assert out.endswith(" end")


def test_truncate_base64_keeps_short_runs() -> None:
    text = "short token aGVsbG8gd29ybGQ= here"  # well under the 256 threshold
    assert truncate_base64(text) == text


def test_truncate_base64_ignores_long_hex() -> None:
    # 300 hex chars: no upper/lower mix, no symbols → not base64-like.
    hex_run = "abcdef0123456789" * 20
    text = f"sha {hex_run} done"
    assert truncate_base64(text) == text


def test_truncate_base64_idempotent() -> None:
    blob = "AaBbCc0123456789+/" * 20
    once = truncate_base64(f"x {blob} y")
    assert truncate_base64(once) == once


# ---- normalize_whitespace ----


def test_normalize_collapses_blank_runs() -> None:
    assert normalize_whitespace("a\n\n\n\n\nb") == "a\n\nb"


def test_normalize_preserves_single_blank_line() -> None:
    assert normalize_whitespace("para one\n\npara two") == "para one\n\npara two"


def test_normalize_rtrims_each_line() -> None:
    assert normalize_whitespace("foo   \nbar\t\nbaz") == "foo\nbar\nbaz"


def test_normalize_strips_outer_blank_lines_and_trailing_ws() -> None:
    # Outer blank lines and trailing whitespace go; leading indentation stays.
    assert normalize_whitespace("\n\ncontent  \n\n") == "content"
    assert normalize_whitespace("\n\n  indented  \n\n") == "  indented"


def test_normalize_idempotent() -> None:
    once = normalize_whitespace("a\n\n\n\nb   \n\n\nc")
    assert normalize_whitespace(once) == once


# ---- scrub_tool_output (composed) ----


def test_scrub_tool_output_combines_all_passes() -> None:
    blob = "AaBbCc0123456789+/" * 20
    raw = f"\x1b[32mDownloading\x1b[0m\r 50%\r100%   \n\n\n\nblob: {blob}\n\n"
    out = scrub_tool_output(raw)
    assert "\x1b" not in out  # ansi stripped
    assert "50%" not in out  # progress frames collapsed
    assert "100%" in out
    assert blob not in out  # base64 truncated
    assert "\n\n\n" not in out  # blank runs collapsed
    assert out == out.strip()  # outer whitespace gone


def test_scrub_tool_output_empty_string() -> None:
    assert scrub_tool_output("") == ""


def test_scrub_tool_output_idempotent() -> None:
    raw = "\x1b[31merr\x1b[0m\rfinal\n\n\n\ntail"
    once = scrub_tool_output(raw)
    assert scrub_tool_output(once) == once
