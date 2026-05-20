"""Monkey-patches applied to Textual to work around upstream bugs.

Call :func:`apply_patches` once at process startup before any Markdown widget
is constructed.
"""

from textual.content import Content
from textual.widgets._markdown import MarkdownFence

from claudechic.highlight import highlight_text

_original_fence_update_from_block = MarkdownFence._update_from_block


async def _fence_update_from_block(self, block):
    """Sync stale fence state on streaming updates, then delegate upstream.

    Upstream ``MarkdownFence._update_from_block`` updates the visible Label via
    ``set_content`` but never syncs ``self.code`` / ``self._highlighted_code``
    from the new block. When streaming via ``MarkdownStream``, the fence is
    constructed from the first chunk (a few characters) and updated in-place
    for later chunks. If anything triggers a recompose afterwards — terminal
    focus change, style refresh, layout cascade, text selection — ``compose``
    re-yields ``Label(self._highlighted_code, ...)``, which has been frozen at
    the first chunk's value, and the code body collapses back to a handful of
    characters (or empty).

    We wrap (rather than replace) the upstream method so that:
      * upstream behavior is preserved if internals are renamed
      * any future upstream fix wins automatically — our sync becomes
        redundant (re-assigns the same values) rather than conflicting

    See https://github.com/Textualize/textual/issues/6518. The upstream fix
    (PR #6519) was closed unmerged.
    """
    if isinstance(block, MarkdownFence):
        try:
            self.code = block.code
            self._highlighted_code = block._highlighted_code
        except AttributeError:
            # Upstream changed shape; fall through to original behavior.
            pass
    await _original_fence_update_from_block(self, block)


def _fence_highlight(cls, code: str, language: str, **kwargs) -> Content:
    """Route fenced-code-block highlighting through our safe highlighter.

    Upstream ``MarkdownFence.highlight`` calls ``textual.highlight.highlight``,
    which builds the Pygments lexer with ``tabsize=8`` while constructing
    ``Content`` from the unexpanded source. Tab-indented languages (Go,
    Makefiles) drift +7 chars per tab as a result — the same bug class fixed
    in the diff view (commit d46b3af).

    We delegate to :func:`claudechic.highlight.highlight_text` and expand
    tabs at this boundary, mirroring how ``DiffWidget.__init__`` pre-expands
    its input. ``self.code`` is read only by ``highlight()`` upstream, so the
    expansion is invisible to copy/clipboard paths.

    ``**kwargs`` swallows theme-control args upstream adds over time (e.g.
    ``ansi``, ``dark`` in Textual 8.x); we keep our own theme inside
    ``highlight_text``.

    Wrapping in ``classmethod`` is required: plain function assignment to a
    ``@classmethod`` slot strips the descriptor and breaks ``cls`` binding.
    """
    del cls, kwargs
    return highlight_text(code.expandtabs(8), language or "")


def apply_patches() -> None:
    """Apply Textual monkey-patches. Safe to call multiple times."""
    MarkdownFence._update_from_block = _fence_update_from_block
    MarkdownFence.highlight = classmethod(_fence_highlight)  # pyright: ignore[reportAttributeAccessIssue]
