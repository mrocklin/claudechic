"""Safe syntax-highlighting wrapper around Pygments.

Single source of truth for both the diff view and the main chat view (via the
``MarkdownFence.highlight`` patch in :mod:`claudechic._textual_patches`).

Two Pygments anti-patterns have bitten us in the wild and motivate this module:

1. Trusting ``get_tokens_unprocessed`` indices — sub-lexers (e.g. the python
   block delegated by the markdown lexer) emit indices that reset to 0
   relative to the inner block. Using them directly splats inner-block spans
   onto the start of the outer document.
2. Letting Pygments expand tabs (``tabsize=8`` default for many lexers) —
   each ``\\t`` becomes 8 spaces inside the token text, and the position
   accumulator drifts +7 chars per tab, so spans drift right and overflow.

This module avoids both by:
  * accumulating position from ``len(token)`` over ``lexer.get_tokens()``
    (a source we control exactly), instead of trusting lexer indices, and
  * building lexers with ``stripnl=False, ensurenl=False`` (which keeps the
    default ``tabsize=0``) so token text == source text byte-for-byte.

Callers that need cell-accurate widths must expand tabs at the input
boundary (see e.g. ``DiffWidget.__init__`` and the patched fence highlight).
"""

from functools import lru_cache

from pygments.lexers import get_lexer_by_name
from pygments.util import ClassNotFound
from textual.content import Content, Span
from textual.highlight import HighlightTheme


@lru_cache(maxsize=64)
def _get_cached_lexer(language: str):
    """Cache Pygments lexers to avoid repeated loading (~15% CPU savings)."""
    try:
        return get_lexer_by_name(language, stripnl=False, ensurenl=False)
    except ClassNotFound:
        return None


def highlight_text(text: str, language: str) -> Content:
    """Syntax highlight text using cached lexer and default HighlightTheme.

    Accumulates span positions from token-text lengths rather than trusting
    the lexer's reported source indices. Some lexers (notably ``markdown``,
    which delegates fenced code blocks to a sub-lexer) emit indices that
    reset to 0 inside embedded blocks, so using those indices directly would
    splat inner-block spans onto the start of the document.

    Safe because the cached lexer is built with ``stripnl=False,
    ensurenl=False`` (see ``_get_cached_lexer``) and default ``tabsize=0``,
    and callers are expected to expand tabs at the input boundary — so
    tokens preserve source text exactly (verified across
    markdown/python/go/js/rust).

    Note: line endings are normalized via ``"\\n".join(text.splitlines())``,
    which collapses ``\\r\\n``/lone ``\\r`` to ``\\n`` and strips a single
    trailing newline. Output length matches the normalized text, not
    necessarily ``len(text)``.
    """
    if not language:
        return Content(text)

    lexer = _get_cached_lexer(language)
    if lexer is None:
        return Content(text)

    text = "\n".join(text.splitlines())
    spans: list[Span] = []

    pos = 0
    for token_type, token in lexer.get_tokens(text):
        current_type = token_type
        while True:
            if style := HighlightTheme.STYLES.get(current_type):
                spans.append(Span(pos, pos + len(token), style))
                break
            if (current_type := current_type.parent) is None:
                break
        pos += len(token)

    return Content(text, spans=spans).stylize_before("$text")


def highlight_lines(text: str, language: str) -> list[Content]:
    """Syntax highlight text and split into lines."""
    if not text:
        return []
    return highlight_text(text, language).split("\n")
