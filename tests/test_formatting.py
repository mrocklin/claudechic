"""Tests for pure formatting helpers."""

import pytest

from claudechic.formatting import strip_ansi, trim_model_name


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Opus 4.7 with 1M context", "Opus 4.7"),
        ("Opus 4.7 With 1M Context", "Opus 4.7"),
        ("Sonnet 4.5 (beta)", "Sonnet 4.5"),
        ("Sonnet 4.5 (experimental)", "Sonnet 4.5"),
        ("Haiku", "Haiku"),
        ("Opus 4.7 with 1M context (beta)", "Opus 4.7"),
        ("", ""),
        ("   Opus 4.7   ", "Opus 4.7"),
    ],
)
def test_trim_model_name(raw, expected):
    assert trim_model_name(raw) == expected


class TestStripAnsi:
    def test_strips_sgr_reset(self):
        assert strip_ansi("\x1b[0mhello") == "hello"

    def test_strips_sgr_color(self):
        assert strip_ansi("\x1b[31mred\x1b[0m") == "red"

    def test_multiple_sgr_sequences(self):
        assert strip_ansi("\x1b[1m\x1b[31mbold red\x1b[0m") == "bold red"

    def test_cursor_movement(self):
        assert strip_ansi("\x1b[2Jhello\x1b[H") == "hello"

    def test_osc_terminal_title_bel(self):
        assert strip_ansi("\x1b]0;My Title\x07text") == "text"

    def test_osc_terminal_title_7bit_st(self):
        assert strip_ansi("\x1b]0;Title\x1b\\text") == "text"

    def test_dcs_sequence(self):
        assert strip_ansi("\x1bPdata\x1b\\text") == "text"

    def test_charset_designation(self):
        assert strip_ansi("\x1b(Btext") == "text"

    def test_c1_csi(self):
        assert strip_ansi("\x9b31mred\x9b0m") == "red"

    def test_clean_input_unchanged(self):
        assert strip_ansi("hello world") == "hello world"

    def test_empty_string(self):
        assert strip_ansi("") == ""

    def test_brackets_preserved(self):
        assert strip_ansi("[INFO] message") == "[INFO] message"

    def test_combined_ansi_and_brackets(self):
        assert strip_ansi("\x1b[31m[ERROR]\x1b[0m msg") == "[ERROR] msg"

    def test_remaining_c1_bytes_stripped(self):
        assert strip_ansi("hello\x01world") == "helloworld"

    def test_nel_byte_does_not_swallow_text(self):
        assert strip_ansi("ok\x85abc") == "okabc"

    def test_8bit_osc_with_payload(self):
        assert strip_ansi("\x9dTitle\x9ctext") == "text"

    def test_bracketed_paste_mode(self):
        assert strip_ansi("\x1b[?2004hpasted\x1b[?2004l") == "pasted"

    def test_osc8_hyperlink(self):
        assert strip_ansi("\x1b]8;;https://example.com\x1b\\link\x1b]8;;\x1b\\") == "link"


def test_extract_tool_search_names_rejects_oversized_input():
    """Inputs > 64KB should return None without attempting ast.literal_eval."""
    from claudechic.formatting import extract_tool_search_names
    huge = "[{" + "x" * 70_000 + "}]"
    assert extract_tool_search_names(huge) is None
