"""Monkey-patches applied to Textual to work around upstream bugs.

Call :func:`apply_patches` once at process startup before any Markdown widget
is constructed.
"""

from textual.widgets._markdown import MarkdownFence

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


def apply_patches() -> None:
    """Apply Textual monkey-patches. Safe to call multiple times."""
    MarkdownFence._update_from_block = _fence_update_from_block
