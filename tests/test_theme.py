"""Tests for theme loading."""

from unittest.mock import patch

from textual.theme import Theme

from claudechic.theme import CHIC_THEME, load_custom_themes


def test_chic_theme_is_dark():
    """Default theme should be dark."""
    assert CHIC_THEME.dark is True
    assert CHIC_THEME.name == "chic"


def test_load_custom_themes_empty_config():
    """No themes defined returns empty list."""
    with patch("claudechic.theme.CONFIG", {}):
        themes = load_custom_themes()
        assert themes == []


def test_load_custom_themes_from_config():
    """Custom themes are loaded from config."""
    config = {
        "themes": {
            "moonfly": {
                "primary": "#80a0ff",
                "secondary": "#ae81ff",
                "accent": "#36c692",
                "background": "#080808",
                "surface": "#121212",
                "panel": "#323437",
                "success": "#8cc85f",
                "warning": "#e3c78a",
                "error": "#ff5d5d",
            }
        }
    }
    with patch("claudechic.theme.CONFIG", config):
        themes = load_custom_themes()
        assert len(themes) == 1
        assert themes[0].name == "moonfly"
        assert themes[0].primary == "#80a0ff"
        assert themes[0].dark is True


def test_load_custom_themes_uses_defaults():
    """Missing color values use defaults from chic theme."""
    config = {
        "themes": {
            "minimal": {
                "primary": "#ff0000",
            }
        }
    }
    with patch("claudechic.theme.CONFIG", config):
        themes = load_custom_themes()
        assert len(themes) == 1
        assert themes[0].name == "minimal"
        assert themes[0].primary == "#ff0000"
        assert themes[0].secondary == "#5599dd"  # Default from chic


def test_load_custom_themes_skips_invalid():
    """Non-dict theme entries are skipped."""
    config = {
        "themes": {
            "valid": {"primary": "#ff0000"},
            "invalid": "not a dict",
        }
    }
    with patch("claudechic.theme.CONFIG", config):
        themes = load_custom_themes()
        assert len(themes) == 1
        assert themes[0].name == "valid"
