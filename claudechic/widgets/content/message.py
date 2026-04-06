"""Chat widgets - messages, input, and thinking indicator."""

import re
import tempfile
import time
from pathlib import Path

try:
    from PIL import ImageGrab as _ImageGrab

    _HAS_PIL = True
except ImportError:
    _ImageGrab = None
    _HAS_PIL = False

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Markdown, TextArea, Static

from claudechic.errors import log_exception
from claudechic.widgets.primitives.button import Button
from claudechic.widgets.primitives.spinner import Spinner
from claudechic.widgets.input.vi_mode import ViHandler, ViMode


class ThinkingIndicator(Spinner):
    """Animated spinner shown when Claude is thinking."""

    can_focus = False
    DEFAULT_CSS = """
    ThinkingIndicator {
        width: auto;
        height: 1;
    }
    """

    def __init__(self, id: str | None = None, classes: str | None = None) -> None:
        super().__init__("Thinking...")
        if id:
            self.id = id
        if classes:
            self.set_classes(classes)


class ConnectingIndicator(Vertical):
    """Centered indicator shown while connecting to SDK."""

    can_focus = False
    DEFAULT_CSS = """
    ConnectingIndicator {
        width: 100%;
        height: 1fr;
        align: center middle;
    }
    ConnectingIndicator > Vertical {
        width: auto;
        height: auto;
        padding: 2 4;
        border: round $surface;
    }
    ConnectingIndicator Static {
        width: auto;
        text-align: center;
        color: $text-muted;
    }
    ConnectingIndicator .connecting-title {
        text-style: bold;
        padding-bottom: 1;
    }
    ConnectingIndicator Spinner {
        width: auto;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Connecting to Claude...", classes="connecting-title")
            yield Spinner("Establishing session")


class ErrorMessage(Static):
    """Error message displayed in the chat view with red styling."""

    can_focus = False

    def __init__(self, message: str, exception: Exception | None = None) -> None:
        super().__init__()
        self._message = message
        self._exception = exception
        # Log the exception if provided
        if exception:
            log_exception(exception, message)

    def compose(self) -> ComposeResult:
        display = f"**Error:** {self._message}"
        if self._exception:
            display += f"\n\n`{type(self._exception).__name__}: {self._exception}`"
        yield Markdown(display, id="content")


class SystemInfo(Static):
    """System info message displayed in chat (not stored in history)."""

    can_focus = False

    def __init__(self, message: str, severity: str = "info") -> None:
        super().__init__(classes=f"system-{severity}")
        self._message = message

    def compose(self) -> ComposeResult:
        yield Markdown(self._message, id="content")


class ChatMessage(Static):
    """A single chat message.

    Uses Textual's MarkdownStream for efficient incremental rendering.
    Adds debouncing on top of MarkdownStream's internal batching to reduce
    the frequency of markdown parsing during fast streaming.

    Text cursor is set via CSS (pointer: text).
    """

    can_focus = False

    # Debounce settings for streaming text
    _DEBOUNCE_INTERVAL = 0.05  # 50ms - flush accumulated text at most 20x/sec
    _DEBOUNCE_MAX_CHARS = 200  # Flush immediately if buffer exceeds this

    def __init__(self, content: str = "", is_agent: bool = False) -> None:
        super().__init__()
        self._initial_content = content.rstrip()  # Content to render in compose()
        self._content = self._initial_content  # Full accumulated content
        self._is_agent = is_agent
        self._stream = None  # Lazy-initialized MarkdownStream
        self._pending_text = ""  # Accumulated text waiting to be flushed
        self._flush_timer = None  # Timer for debounced flush
        self._first_flush_done = False  # Track if first stream write has happened

    def _is_streaming(self) -> bool:
        """Check if we're actively streaming content."""
        return bool(self._pending_text) or self._flush_timer is not None

    def compose(self) -> ComposeResult:
        # History messages (non-empty content): render as Markdown immediately.
        # New streaming messages (empty content): start with Static for zero-cost updates.
        # Static is swapped to Markdown once streaming completes (see flush()).
        if self._initial_content:
            inner = Markdown(self._initial_content, id="content")
        else:
            inner = Static("", id="content", markup=False)
        if self._is_agent:
            with Vertical(id="agent-inner"):
                yield inner
        else:
            yield inner

    def _get_stream(self):
        """Get or create the MarkdownStream for this message."""
        if self._stream is None:
            if md := self.query_one_optional("#content", Markdown):
                self._stream = Markdown.get_stream(md)
        return self._stream

    def append_content(self, text: str) -> None:
        """Append text using debounced MarkdownStream for efficient incremental rendering.

        Text is accumulated in a buffer and flushed either:
        - After _DEBOUNCE_INTERVAL (50ms) of no new text
        - Immediately if buffer exceeds _DEBOUNCE_MAX_CHARS (200 chars)

        This reduces markdown parsing frequency during fast streaming while
        maintaining responsive updates.
        """
        self._content += text
        self._pending_text += text

        # Flush immediately if we have a lot of pending text
        if len(self._pending_text) >= self._DEBOUNCE_MAX_CHARS:
            self._flush_pending()
            return

        # Otherwise, schedule a debounced flush
        if self._flush_timer is None:
            self._flush_timer = self.set_timer(
                self._DEBOUNCE_INTERVAL, self._flush_pending
            )

    def _flush_pending(self) -> None:
        """Flush accumulated text to the content widget."""
        if self._flush_timer is not None:
            self._flush_timer.stop()
            self._flush_timer = None

        if not self._pending_text:
            return

        # Fast path: streaming message uses Static — direct string update, zero DOM cost.
        # Avoids MarkdownStream's repeated remove/mount operations which trigger a full
        # layout pass over every widget in the chat on every text chunk.
        static = self.query_one_optional("#content", Static)
        if static is not None and not isinstance(static, Markdown):
            self._first_flush_done = True
            static.update(self._content)
            self._pending_text = ""
            return

        # Fallback: MarkdownStream path (history messages already have a Markdown widget).
        stream = self._get_stream()
        if not stream:
            # Widget not mounted yet - reschedule flush
            self._flush_timer = self.set_timer(
                self._DEBOUNCE_INTERVAL, self._flush_pending
            )
            return

        if not self._first_flush_done:
            self._first_flush_done = True
            text_to_write = self._content[len(self._initial_content) :]
        else:
            text_to_write = self._pending_text

        if text_to_write:
            self.call_later(stream.write, text_to_write)
        self._pending_text = ""

    def flush(self) -> None:
        """Flush any pending text and finalise the message display."""
        self._flush_pending()

        if self._stream:
            self.call_later(self._stream.stop)
            self._stream = None

        # If we were streaming into a Static, schedule a one-time swap to Markdown.
        # This gives smooth streaming (zero DOM ops) then a single final render.
        static = self.query_one_optional("#content", Static)
        if static is not None and not isinstance(static, Markdown):
            self.call_later(self._replace_static_with_markdown)

    def _replace_static_with_markdown(self) -> None:
        """Swap the streaming Static with a rendered Markdown widget (called once on flush)."""
        static = self.query_one_optional("#content", Static)
        if static is None or isinstance(static, Markdown):
            return
        content = self._content
        if not content:
            return
        parent = static.parent
        if parent is None:
            return
        # Remove Static then mount Markdown in its place (processed in queue order).
        static.remove()
        parent.mount(Markdown(content, id="content"))

    def get_raw_content(self) -> str:
        """Get raw content."""
        return self._content


class ChatAttachment(Button):
    """Clickable attachment tag in chat messages - opens file on click."""

    def __init__(self, path: str, display_name: str) -> None:
        super().__init__(f"📎 {display_name}", classes="chat-attachment")
        self._path = path

    def on_click(self, event) -> None:
        """Open the file when clicked."""
        import subprocess
        import sys

        try:
            if sys.platform == "darwin":
                subprocess.run(["open", self._path], check=True)
            elif sys.platform == "win32":
                subprocess.run(["start", self._path], shell=True, check=True)
            else:
                subprocess.run(["xdg-open", self._path], check=True)
        except Exception as e:
            self.app.notify(f"Failed to open: {e}", severity="error")


class ImageAttachments(Horizontal):
    """Shows pending image attachments as removable tags."""

    class Removed(Message):
        """Posted when user removes an image."""

        def __init__(self, filename: str) -> None:
            self.filename = filename
            super().__init__()

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._images: list[str] = []
        self._counter = 0  # Unique ID counter

    def add_image(self, filename: str) -> None:
        """Add an image tag."""
        self._images.append(filename)
        self._update_display()

    def remove_image(self, filename: str) -> None:
        """Remove a specific image."""
        if filename in self._images:
            self._images.remove(filename)
            self._update_display()
            self.post_message(self.Removed(filename))

    def clear(self) -> None:
        """Clear all images."""
        self._images.clear()
        self._update_display()

    def _update_display(self) -> None:
        # Remove existing buttons
        for child in list(self.children):
            child.remove()

        if self._images:
            screenshot_num = 0
            for name in self._images:
                self._counter += 1
                # Shorten screenshot names for display
                if name.lower().startswith("screenshot"):
                    screenshot_num += 1
                    display_name = f"Screenshot #{screenshot_num}"
                else:
                    display_name = name
                btn = Button(
                    f"📎 {display_name} ×",
                    id=f"img-{self._counter}",
                    classes="image-tag",
                )
                btn._image_name = name  # type: ignore[attr-defined]  # Store actual name for removal
                self.mount(btn)
            self.remove_class("hidden")
        else:
            self.add_class("hidden")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle click on image tag to remove it."""
        if hasattr(event.button, "_image_name"):
            self.remove_image(event.button._image_name)  # type: ignore[attr-defined]
            event.stop()


class ChatInput(TextArea):
    """Text input that submits on Enter, newline on Shift+Enter, history with Up/Down.

    Text cursor is set via CSS (pointer: text).
    """

    BINDINGS = [
        Binding("enter", "submit", "Send", priority=True, show=False),
        # Newline: shift+enter (macOS), ctrl+j (cross-platform)
        Binding("shift+enter", "newline", "Newline", priority=True, show=False),
        Binding("ctrl+j", "newline", "Newline", priority=True, show=False),
        Binding("up", "history_prev", "Previous", priority=True, show=False),
        Binding("down", "history_next", "Next", priority=True, show=False),
        # Word deletion: alt+backspace/delete (cross-platform), ctrl+delete (some terminals)
        # Note: ctrl+w for delete_word_left is inherited from TextArea
        Binding(
            "alt+backspace",
            "delete_word_left",
            "Delete word left",
            priority=True,
            show=False,
        ),
        Binding(
            "alt+delete",
            "delete_word_right",
            "Delete word right",
            priority=True,
            show=False,
        ),
        Binding(
            "ctrl+delete",
            "delete_word_right",
            "Delete word right",
            priority=True,
            show=False,
        ),
        # Readline/emacs navigation
        Binding("ctrl+f", "cursor_right", "Forward char", priority=True, show=False),
        Binding("ctrl+b", "cursor_left", "Backward char", priority=True, show=False),
        Binding("ctrl+p", "cursor_up", "Previous line", priority=True, show=False),
        Binding("ctrl+n", "cursor_down", "Next line", priority=True, show=False),
        Binding(
            "alt+f", "cursor_word_right", "Forward word", priority=True, show=False
        ),
        Binding(
            "alt+b", "cursor_word_left", "Backward word", priority=True, show=False
        ),
    ]

    class Submitted(Message):
        """Posted when user presses Enter."""

        def __init__(self, text: str) -> None:
            self.text = text
            super().__init__()

    class ViModeChanged(Message):
        """Posted when vi mode changes."""

        def __init__(self, mode: ViMode) -> None:
            self.mode = mode
            super().__init__()

    # Supported image extensions for drag-and-drop
    IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

    def __init__(self, *args, **kwargs) -> None:
        kwargs.setdefault("tab_behavior", "indent")
        kwargs.setdefault("soft_wrap", True)
        kwargs.setdefault("show_line_numbers", False)
        super().__init__(*args, **kwargs)
        self._history: list[str] = []
        self._history_index: int = -1
        self._current_input: str = ""
        self._autocomplete = None
        self._last_image_paste: tuple[str, float] | None = (
            None  # (text, time) for dedup
        )
        # Vi-mode support
        self._vi_mode_enabled: bool = False
        self._vi_handler: ViHandler | None = None

    def enable_vi_mode(self, enabled: bool = True) -> None:
        """Enable or disable vi-mode for this input."""
        self._vi_mode_enabled = enabled
        if enabled and self._vi_handler is None:
            self._vi_handler = ViHandler(self)
            self._vi_handler.set_mode_changed_callback(self._on_vi_mode_changed)
        if enabled and self._vi_handler:
            # Always start in INSERT mode when enabling
            self._vi_handler.state.mode = ViMode.INSERT
            self._on_vi_mode_changed(ViMode.INSERT)
        elif not enabled and self._vi_handler:
            # Notify that vi-mode is disabled (no mode indicator)
            self.post_message(self.ViModeChanged(ViMode.INSERT))

    def _on_vi_mode_changed(self, mode: ViMode) -> None:
        """Handle vi mode change."""
        self.post_message(self.ViModeChanged(mode))

    @property
    def vi_mode(self) -> ViMode | None:
        """Get current vi mode, or None if vi-mode is disabled."""
        if self._vi_mode_enabled and self._vi_handler:
            return self._vi_handler.state.mode
        return None

    async def _on_key(self, event) -> None:  # type: ignore[override]
        """Intercept keys for autocomplete and vi-mode before normal processing."""
        # Autocomplete takes priority when visible
        if self._autocomplete and self._autocomplete.handle_key(event.key):
            event.prevent_default()
            event.stop()
            return

        # Vi-mode handling (when not in INSERT mode)
        if self._vi_mode_enabled and self._vi_handler:
            vi_mode = self._vi_handler.state.mode
            # In INSERT mode, only Escape is handled by vi-mode
            # In NORMAL/VISUAL mode, all keys go through vi-mode first
            if vi_mode != ViMode.INSERT or event.key == "escape":
                if self._vi_handler.handle_key(event.key, event.character):
                    event.prevent_default()
                    event.stop()
                    return

        await super()._on_key(event)

    def _safe_path_exists(self, path: Path) -> bool:
        """Check if path exists, handling OSError for paths too long."""
        try:
            return path.exists()
        except OSError:
            return False

    def _is_image_path(self, text: str) -> list:
        """Check if text contains image file paths."""
        images = []
        text = text.strip()

        # Handle file:// URLs (newline or space separated)
        if text.startswith("file://"):
            for part in text.split():
                if part.startswith("file://"):
                    part = part[7:]
                path = Path(part)
                if (
                    self._safe_path_exists(path)
                    and path.suffix.lower() in self.IMAGE_EXTENSIONS
                ):
                    images.append(path)
            return images

        # Handle shell-escaped paths (backslash-escaped spaces)
        # Split on unescaped spaces (space not preceded by backslash)
        if "\\ " in text:
            parts = re.split(r"(?<!\\) ", text)
            for part in parts:
                part = part.replace("\\ ", " ")
                path = Path(part)
                if (
                    self._safe_path_exists(path)
                    and path.suffix.lower() in self.IMAGE_EXTENSIONS
                ):
                    images.append(path)
            return images

        # Simple case: one path per line or single path
        for line in text.splitlines():
            line = line.strip()
            path = Path(line)
            if (
                self._safe_path_exists(path)
                and path.suffix.lower() in self.IMAGE_EXTENSIONS
            ):
                images.append(path)
        return images

    def _grab_clipboard_image(self) -> Path | None:
        """Grab raw image data from clipboard and save to a temp file.

        Returns path to saved PNG, or None if clipboard has no image.
        Used as fallback when pasted text is not a file path (e.g. screenshots).
        """
        if not _HAS_PIL:
            return None
        try:
            img = _ImageGrab.grabclipboard()
            if img is None or not hasattr(img, "save"):
                return None
            tmp_dir = Path(tempfile.gettempdir()) / "claudechic_paste"
            tmp_dir.mkdir(exist_ok=True)
            tmp_path = tmp_dir / f"paste_{int(time.time() * 1000)}.png"
            img.save(tmp_path, "PNG")
            return tmp_path
        except Exception:
            return None

    async def on_paste(self, event) -> None:
        """Intercept paste - check for images BEFORE inserting text."""
        import asyncio

        images = self._is_image_path(event.text)
        if images:
            # Deduplicate - terminals sometimes fire paste twice
            now = time.time()
            if self._last_image_paste and self._last_image_paste[0] == event.text:
                if now - self._last_image_paste[1] < 0.5:  # Within 500ms = duplicate
                    event.prevent_default()
                    event.stop()
                    return
            self._last_image_paste = (event.text, now)

            # Attach images
            for path in images:
                self.app._attach_image(path)  # type: ignore[attr-defined]
            event.prevent_default()
            event.stop()
            return
        # Fallback: raw clipboard image (e.g. Win+Shift+S screenshot)
        # Run PIL in a thread — grabclipboard() blocks the event loop on Windows
        clipboard_image = await asyncio.to_thread(self._grab_clipboard_image)
        if clipboard_image:
            now = time.time()
            if self._last_image_paste and now - self._last_image_paste[1] < 0.5:
                event.prevent_default()
                event.stop()
                return
            self._last_image_paste = ("__clipboard__", now)
            self.app._attach_image(clipboard_image)  # type: ignore[attr-defined]
            event.prevent_default()
            event.stop()
            return
        # Wrap multi-line pastes in triple backticks for markdown formatting
        if "\n" in event.text:
            wrapped = f"```\n{event.text}\n```"
            self.insert(wrapped)
            event.prevent_default()
            event.stop()
            return
        # Normal paste - let parent handle it

    def action_submit(self) -> None:
        """Submit current input or accept autocomplete selection."""
        # If autocomplete is showing, complete instead of submit
        if self._autocomplete and self._autocomplete.display:
            self._autocomplete.handle_key("enter")
            return
        text = self.text.strip()
        if text:
            # Add to history (avoid duplicates of last entry)
            if not self._history or self._history[-1] != text:
                self._history.append(text)
        self._history_index = -1
        self.post_message(self.Submitted(self.text))

    def action_newline(self) -> None:
        """Insert a newline character (Ctrl+J)."""
        self.insert("\n")

    def action_history_prev(self) -> None:
        """Go to previous command in history (only when cursor at top visual row)."""
        # If autocomplete is visible, navigate it instead
        if self._autocomplete and self._autocomplete.display:
            self._autocomplete.handle_key("up")
            return
        # Check if we're at the top visual row (considering soft wrap)
        visual_offset = self.wrapped_document.location_to_offset(self.cursor_location)
        if visual_offset.y > 0:
            # Not at top - use built-in wrap-aware cursor movement
            self.action_cursor_up()
            return
        if not self._history:
            return
        if self._history_index == -1:
            self._current_input = self.text
            self._history_index = len(self._history) - 1
        elif self._history_index > 0:
            self._history_index -= 1
        # Suppress autocomplete BEFORE setting text to prevent timer start
        if self._autocomplete:
            self._autocomplete.suppress()
        self.text = self._history[self._history_index]
        self.move_cursor(self.document.end)

    def action_history_next(self) -> None:
        """Go to next command in history (only when cursor at bottom visual row)."""
        # If autocomplete is visible, navigate it instead
        if self._autocomplete and self._autocomplete.display:
            self._autocomplete.handle_key("down")
            return
        # Check if we're at the bottom visual row (considering soft wrap)
        visual_offset = self.wrapped_document.location_to_offset(self.cursor_location)
        total_visual_rows = self.wrapped_document.height
        if visual_offset.y < total_visual_rows - 1:
            # Not at bottom - use built-in wrap-aware cursor movement
            self.action_cursor_down()
            return
        if self._history_index == -1:
            return
        # Suppress autocomplete BEFORE setting text to prevent timer start
        if self._autocomplete:
            self._autocomplete.suppress()
        if self._history_index < len(self._history) - 1:
            self._history_index += 1
            self.text = self._history[self._history_index]
        else:
            self._history_index = -1
            self.text = self._current_input
        self.move_cursor(self.document.end)
