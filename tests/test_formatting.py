"""Tests for pure formatting helpers."""

import pytest

from claudechic.formatting import trim_model_name


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
