"""Theme definitions for Claude Chic.

Custom themes can be defined in ~/.claude/.claudechic.yaml:

    themes:
      moonfly:
        primary: "#80a0ff"
        secondary: "#ae81ff"
        accent: "#36c692"
        background: "#080808"
        surface: "#121212"
        panel: "#323437"
        success: "#8cc85f"
        warning: "#e3c78a"
        error: "#ff5d5d"

    theme: moonfly  # Set as active theme

Use /theme to search and switch between available themes.
"""

from textual.theme import Theme

from claudechic.config import CONFIG

# Default Claude Chic theme - orange accent, dark background
CHIC_THEME = Theme(
    name="chic",
    primary="#cc7700",
    secondary="#5599dd",  # Sky blue for syntax highlighting
    accent="#445566",
    background="black",
    surface="#111111",
    panel="#555555",  # Used for borders and subtle UI elements
    success="#5599dd",  # Same as secondary - strings in code
    warning="#aaaa00",  # Yellow - moderate usage/caution
    error="#cc3333",  # Red - high usage/errors
    dark=True,
)


def load_custom_themes() -> list[Theme]:
    """Load custom themes from config file.

    Returns list of Theme objects defined in ~/.claude/.claudechic.yaml
    """
    themes_config = CONFIG.get("themes", {})
    custom_themes = []

    for name, colors in themes_config.items():
        if not isinstance(colors, dict):
            continue

        theme = Theme(
            name=name,
            **colors,
        )
        custom_themes.append(theme)

    return custom_themes
