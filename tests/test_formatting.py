"""Tests for pure formatting helpers."""

import pytest

from claudechic.formatting import (
    LITERAL_EVAL_MAX_SIZE,
    extract_tool_search_names,
    strip_ansi,
    trim_model_name,
)


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


@pytest.mark.parametrize(
    "raw, expected",
    [
        # SGR colors and resets — the common case
        ("\x1b[31mred\x1b[0m", "red"),
        ("\x1b[1;31mbold red\x1b[0m", "bold red"),
        ("\x1b[31m[ERROR]\x1b[0m boom", "[ERROR] boom"),
        # Cursor movement
        ("before\x1b[2Jafter", "beforeafter"),
        # Bracketed paste markers
        ("\x1b[?2004hpasted\x1b[?2004l", "pasted"),
        # Pass-through cases — must not corrupt
        ("plain text", "plain text"),
        ("", ""),
        ("[INFO] not a real escape", "[INFO] not a real escape"),
        # Non-ASCII / C1-range Unicode codepoints must be preserved
        # (PR #73's broader regex would strip these — we deliberately don't)
        ("café — résumé", "café — résumé"),
        ("NEL char preserved", "NEL char preserved"),
    ],
)
def test_strip_ansi(raw, expected):
    assert strip_ansi(raw) == expected


def test_extract_tool_search_names_size_capped():
    """Oversized stringified list bypasses ast.literal_eval to avoid UI freeze."""
    # Build a string just over the cap that *looks* parseable. literal_eval on
    # this would succeed but cost time/memory proportional to length — the
    # guard short-circuits to None instead.
    payload = "[{'type': 'tool_reference', 'tool_name': 'X'}," * 2000
    payload = "[" + payload.rstrip(",") + "]"
    assert len(payload) > LITERAL_EVAL_MAX_SIZE
    assert extract_tool_search_names(payload) is None


def test_extract_tool_search_names_normal_case_still_works():
    """Size guard doesn't regress the happy path."""
    payload = "[{'type': 'tool_reference', 'tool_name': 'Read'}]"
    assert extract_tool_search_names(payload) == ["Read"]
