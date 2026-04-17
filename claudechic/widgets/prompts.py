"""Selection and question prompts for user interaction."""

import asyncio
from collections.abc import Sequence
from typing import Any

from textual.app import ComposeResult
from textual.widgets import Static


class BasePrompt(Static):
    """Base class for selection prompts with arrow/number navigation and optional text input."""

    can_focus = True

    def __init__(self) -> None:
        super().__init__()
        self.add_class("base-prompt")
        self.selected_idx = 0
        self._result_event: asyncio.Event = asyncio.Event()
        self._result_value: Any = None
        # Text input mode (for "Other" / "New" options)
        self._in_text_mode = False
        self._text_buffer = ""

    def _total_options(self) -> int:
        """Return total number of selectable options. Override in subclasses."""
        return 0

    def _get_option_id(self, idx: int) -> str:
        """Return the DOM id for option at index."""
        return f"opt-{idx}"

    def _text_option_idx(self) -> int | None:
        """Return index of text input option, or None if no text input. Override in subclasses."""
        return None

    def _text_option_placeholder(self) -> str:
        """Return placeholder text for text input option. Override in subclasses."""
        return "Enter text..."

    def _update_selection(self) -> None:
        """Update visual selection state."""
        for i in range(self._total_options()):
            if opt := self.query_one_optional(f"#{self._get_option_id(i)}", Static):
                if i == self.selected_idx:
                    opt.add_class("selected")
                else:
                    opt.remove_class("selected")

    def _resolve(self, result: Any) -> None:
        """Set result and signal completion."""
        if not self._result_event.is_set():
            self._result_value = result
            self._result_event.set()
        self.remove()

    def on_mount(self) -> None:
        """Auto-focus on mount to capture keys immediately (only if visible)."""
        if not self.has_class("hidden"):
            self.focus()

    def on_blur(self) -> None:
        """Refocus when focus is lost - prompt must stay focused (unless hidden)."""
        if not self.has_class("hidden"):
            self.focus()

    def cancel(self) -> None:
        """Cancel and dismiss the prompt without a selection."""
        self._resolve(None)

    def _select_option(self, idx: int) -> None:
        """Handle selection of option at index. Override in subclasses."""
        pass

    def _submit_text(self, text: str) -> None:
        """Handle text submission. Override in subclasses that support text input."""
        pass

    def _submit_text_option_empty(self) -> None:
        """Handle empty text submission. Override in subclasses. Default: exit text mode."""
        self._exit_text_mode()

    # Text mode methods
    def _enter_text_mode(self) -> None:
        """Enter inline text input mode."""
        self._in_text_mode = True
        text_idx = self._text_option_idx()
        if text_idx is not None:
            opt = self.query_one(f"#{self._get_option_id(text_idx)}", Static)
            opt.remove_class("prompt-placeholder")

    def _update_text_display(self) -> None:
        """Update the text input display with current buffer."""
        text_idx = self._text_option_idx()
        if text_idx is not None:
            opt = self.query_one(f"#{self._get_option_id(text_idx)}", Static)
            opt.update(f"{text_idx + 1}. {self._text_buffer}_")

    def _exit_text_mode(self) -> None:
        """Exit text mode and restore placeholder."""
        self._in_text_mode = False
        self._text_buffer = ""
        text_idx = self._text_option_idx()
        if text_idx is not None:
            opt = self.query_one(f"#{self._get_option_id(text_idx)}", Static)
            opt.add_class("prompt-placeholder")
            opt.update(f"{text_idx + 1}. {self._text_option_placeholder()}")

    def _handle_text_mode_key(self, event) -> bool:
        """Handle keys in text mode. Returns True if handled."""
        if event.key in ("escape", "up", "down"):
            self._exit_text_mode()
            if event.key in ("up", "down"):
                # Let navigation happen after exiting text mode
                return False
            return True
        elif event.key == "enter":
            if self._text_buffer.strip():
                self._submit_text(self._text_buffer.strip())
            else:
                self._submit_text_option_empty()
            return True
        elif event.key == "backspace":
            self._text_buffer = self._text_buffer[:-1]
            self._update_text_display()
            return True
        elif len(event.character or "") == 1 and event.character.isprintable():
            self._text_buffer += event.character
            self._update_text_display()
            return True
        return True  # Consume other keys in text mode

    def _handle_key(self, event) -> bool:
        """Handle common navigation keys. Returns True if handled."""
        if event.key == "up":
            self.selected_idx = (self.selected_idx - 1) % self._total_options()
            self._update_selection()
            return True
        elif event.key == "down":
            self.selected_idx = (self.selected_idx + 1) % self._total_options()
            self._update_selection()
            return True
        elif event.key == "enter":
            self._select_option(self.selected_idx)
            return True
        elif event.key == "escape":
            self.cancel()
            return True
        elif event.key.isdigit():
            idx = int(event.key) - 1
            if 0 <= idx < self._total_options():
                self._select_option(idx)
                return True
        return False

    def on_key(self, event) -> None:
        # Ignore input when hidden (agent not active)
        if self.has_class("hidden"):
            return

        # Text mode takes priority
        if self._in_text_mode:
            if self._handle_text_mode_key(event):
                event.prevent_default()
                event.stop()
                return
            # Fall through for up/down navigation

        # Start typing to enter text mode (if text option exists)
        text_idx = self._text_option_idx()
        if (
            text_idx is not None
            and len(event.character or "") == 1
            and event.character.isalpha()
        ):
            self.selected_idx = text_idx
            self._update_selection()
            self._text_buffer = event.character
            self._enter_text_mode()
            self._update_text_display()
            event.prevent_default()
            event.stop()
            return

        # Default navigation
        if self._handle_key(event):
            event.prevent_default()
            event.stop()

    async def wait(self) -> Any:
        """Wait for selection. Returns result or None if cancelled."""
        await self._result_event.wait()
        return self._result_value


class SelectionPrompt(BasePrompt):
    """Simple selection prompt with arrow/number navigation and optional text input."""

    def __init__(
        self,
        title: str,
        options: Sequence[tuple[str, str]],
        text_option: tuple[str, str] | None = None,
    ) -> None:
        """Create selection prompt.

        Args:
            title: Prompt title/question
            options: List of (value, label) tuples
            text_option: Optional (value_prefix, placeholder) for text input option.
                         Result will be f"{value_prefix}:{user_text}"
        """
        super().__init__()
        self.title = title
        self.options = options
        self.text_option = text_option
        self._result_value = options[0][0] if options else ""

    def compose(self) -> ComposeResult:
        # Set min-height: 1 top margin + title + blank + options + 1 bottom margin
        min_h = 1 + 1 + 1 + self._total_options() + 1
        self.styles.min_height = min_h

        yield Static(self.title, classes="prompt-title", markup=False)
        for i, (value, label) in enumerate(self.options):
            classes = "prompt-option selected" if i == 0 else "prompt-option"
            yield Static(
                f"{i + 1}. {label}", classes=classes, id=f"opt-{i}", markup=False
            )
        # Text input option (if enabled)
        if self.text_option:
            text_idx = len(self.options)
            classes = "prompt-option prompt-placeholder"
            yield Static(
                f"{text_idx + 1}. {self.text_option[1]}",
                classes=classes,
                id=f"opt-{text_idx}",
                markup=False,
            )

    def _total_options(self) -> int:
        return len(self.options) + (1 if self.text_option else 0)

    def _text_option_idx(self) -> int | None:
        return len(self.options) if self.text_option else None

    def _text_option_placeholder(self) -> str:
        return self.text_option[1] if self.text_option else ""

    def _select_option(self, idx: int) -> None:
        if idx < len(self.options):
            self._resolve(self.options[idx][0])
        elif self.text_option:
            # Enter text mode for the text option
            self._text_buffer = ""
            self._enter_text_mode()
            self._update_text_display()

    def _submit_text(self, text: str) -> None:
        if self.text_option:
            self._resolve(f"{self.text_option[0]}:{text}")

    def _submit_text_option_empty(self) -> None:
        """Submit text option with no text (just the value without colon)."""
        if self.text_option:
            self._resolve(self.text_option[0])

    def cancel(self) -> None:
        self._resolve("")

    async def wait(self) -> str:
        """Wait for selection. Returns value or empty string if cancelled."""
        await super().wait()
        return self._result_value


class QuestionPrompt(BasePrompt):
    """Multi-question prompt for AskUserQuestion tool."""

    def __init__(self, questions: list[dict]) -> None:
        super().__init__()
        self.questions = questions
        self.current_q = 0
        self.answers: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        yield from self._render_question()

    def _render_question(self):
        """Yield widgets for current question."""
        q = self.questions[self.current_q]
        yield Static(
            f"[{self.current_q + 1}/{len(self.questions)}] {q['question']}",
            classes="prompt-title",
            markup=False,
        )
        for i, opt in enumerate(q.get("options", [])):
            classes = (
                "prompt-option selected" if i == self.selected_idx else "prompt-option"
            )
            label = opt.get("label", "?")
            desc = opt.get("description", "")
            text = f"{i + 1}. {label}" + (f" - {desc}" if desc else "")
            yield Static(text, classes=classes, id=self._get_option_id(i), markup=False)
        # "Other" option
        other_idx = len(q.get("options", []))
        classes = "prompt-option prompt-placeholder"
        if self.selected_idx == other_idx:
            classes += " selected"
        yield Static(
            f"{other_idx + 1}. {self._text_option_placeholder()}",
            classes=classes,
            id=self._get_option_id(other_idx),
            markup=False,
        )

    def _total_options(self) -> int:
        q = self.questions[self.current_q]
        return len(q.get("options", [])) + 1  # +1 for "Other"

    def _text_option_idx(self) -> int:
        q = self.questions[self.current_q]
        return len(q.get("options", []))

    def _text_option_placeholder(self) -> str:
        return "Other..."

    def _select_option(self, idx: int) -> None:
        q = self.questions[self.current_q]
        options = q.get("options", [])
        if idx < len(options):
            answer = options[idx].get("label", "?")
            self._record_answer(answer)
        else:
            self._text_buffer = ""
            self._enter_text_mode()
            self._update_text_display()

    def _submit_text(self, text: str) -> None:
        self._record_answer(text)

    def _get_option_id(self, idx: int) -> str:
        """Return unique DOM id for option at index (includes question number)."""
        return f"opt-{self.current_q}-{idx}"

    def _record_answer(self, answer: str) -> None:
        """Record answer and advance to next question or finish."""
        q = self.questions[self.current_q]
        self.answers[q["question"]] = answer

        if self.current_q < len(self.questions) - 1:
            self.current_q += 1
            self.selected_idx = 0
            self._in_text_mode = False
            self._text_buffer = ""
            # Remove old children and mount new question
            for child in list(self.children):
                child.remove()
            for w in self._render_question():
                self.mount(w)
        else:
            self._resolve(self.answers)

    def cancel(self) -> None:
        self.answers = {}
        self._resolve({})

    async def wait(self) -> dict[str, str]:
        """Wait for all answers. Returns answers dict or empty if cancelled."""
        await super().wait()
        return self._result_value if self._result_value else {}


class ModelPrompt(BasePrompt):
    """Prompt for selecting a model from SDK-provided list."""

    def __init__(self, models: list[dict], current_value: str | None = None) -> None:
        """Create model prompt.

        Args:
            models: List of model dicts from SDK with 'value', 'displayName', 'description'
            current_value: Currently selected model value (e.g., 'opus', 'sonnet')
        """
        super().__init__()
        self.models = models
        self.current_value = current_value
        # Find current model index for initial selection
        self.selected_idx = 0
        for i, m in enumerate(models):
            if m.get("value") == current_value:
                self.selected_idx = i
                break

    def compose(self) -> ComposeResult:
        yield Static("Select Model", classes="prompt-title")
        for i, m in enumerate(self.models):
            value = m.get("value", "")
            # Extract short name from description like "Opus 4.5 · ..."
            desc = m.get("description", "")
            name = (
                desc.split("·")[0].strip()
                if "·" in desc
                else m.get("displayName", value)
            )
            current = " *" if value == self.current_value else ""
            classes = "prompt-option"
            if i == self.selected_idx:
                classes += " selected"
            yield Static(
                f"{i + 1}. {name}{current}",
                classes=classes,
                id=f"opt-{i}",
                markup=False,
            )

    def _total_options(self) -> int:
        return len(self.models)

    def _select_option(self, idx: int) -> None:
        value = self.models[idx].get("value", "")
        self._resolve(value)

    async def wait(self) -> str | None:
        """Wait for selection. Returns model value or None if cancelled."""
        await super().wait()
        return self._result_value


class EffortPrompt(BasePrompt):
    """Prompt for selecting an effort level.

    Effort tunes speed vs. intelligence: lower means fewer tool calls, less
    preamble, terser confirmations; higher means deeper reasoning and more
    thinking tokens. See the matching `# type: ignore` in
    `ChatApp._make_options` for the SDK/CLI type-lag note.
    """

    # Source of truth for supported effort levels. Used by `/effort <level>`
    # validation in commands.py — keep in sync if we add values.
    OPTIONS: tuple[tuple[str, str], ...] = (
        ("low", "Fewer tool calls, terse output"),
        ("medium", "Balanced"),
        ("high", "Deeper reasoning (often the sweet spot)"),
        ("xhigh", "Extra deep reasoning"),
        ("max", "Maximum thinking budget"),
    )

    def __init__(self, current_value: str | None = None) -> None:
        super().__init__()
        self.current_value = current_value
        self.selected_idx = 0
        for i, (value, _) in enumerate(self.OPTIONS):
            if value == current_value:
                self.selected_idx = i
                break

    def compose(self) -> ComposeResult:
        yield Static("Select Effort Level", classes="prompt-title")
        for i, (value, desc) in enumerate(self.OPTIONS):
            current = " *" if value == self.current_value else ""
            classes = "prompt-option"
            if i == self.selected_idx:
                classes += " selected"
            yield Static(
                f"{i + 1}. {value} — {desc}{current}",
                classes=classes,
                id=f"opt-{i}",
                markup=False,
            )

    def _total_options(self) -> int:
        return len(self.OPTIONS)

    def _select_option(self, idx: int) -> None:
        self._resolve(self.OPTIONS[idx][0])

    async def wait(self) -> str | None:
        """Wait for selection. Returns effort value or None if cancelled."""
        await super().wait()
        return self._result_value


class WorktreePrompt(BasePrompt):
    """Prompt for selecting or creating worktrees."""

    def __init__(self, worktrees: list[tuple[str, str]]) -> None:
        """Create worktree prompt.

        Args:
            worktrees: List of (path, branch) tuples for existing worktrees
        """
        super().__init__()
        self.worktrees = worktrees

    def compose(self) -> ComposeResult:
        yield Static("Worktrees", classes="prompt-title")
        for i, (path, branch) in enumerate(self.worktrees):
            classes = "prompt-option selected" if i == 0 else "prompt-option"
            yield Static(
                f"{i + 1}. {branch}", classes=classes, id=f"opt-{i}", markup=False
            )
        # "New" option at the end
        new_idx = len(self.worktrees)
        classes = "prompt-option prompt-placeholder"
        if new_idx == 0:
            classes += " selected"
        yield Static(
            f"{new_idx + 1}. {self._text_option_placeholder()}",
            classes=classes,
            id=f"opt-{new_idx}",
        )

    def _total_options(self) -> int:
        return len(self.worktrees) + 1  # +1 for "New"

    def _text_option_idx(self) -> int:
        return len(self.worktrees)

    def _text_option_placeholder(self) -> str:
        return "Enter name..."

    def _select_option(self, idx: int) -> None:
        if idx < len(self.worktrees):
            path, branch = self.worktrees[idx]
            self._resolve(("switch", path))
        else:
            self._text_buffer = ""
            self._enter_text_mode()
            self._update_text_display()

    def _submit_text(self, text: str) -> None:
        self._resolve(("new", text))

    async def wait(self) -> tuple[str, str] | None:
        """Wait for selection. Returns (action, value) or None if cancelled."""
        await super().wait()
        return self._result_value


class UncommittedChangesPrompt(BasePrompt):
    """Prompt for handling uncommitted changes during worktree finish."""

    def __init__(
        self,
        uncommitted: list[str],
        untracked: list[str],
    ) -> None:
        super().__init__()
        self.uncommitted = uncommitted
        self.untracked = untracked

    def compose(self) -> ComposeResult:
        yield Static("Uncommitted Changes", classes="prompt-title")

        # Show summary
        details = []
        if self.uncommitted:
            details.append(f"{len(self.uncommitted)} modified")
        if self.untracked:
            details.append(f"{len(self.untracked)} untracked")
        yield Static(" | ".join(details), classes="prompt-subtitle")

        yield Static("1. Commit changes", classes="prompt-option selected", id="opt-0")
        yield Static("2. Discard all changes", classes="prompt-option", id="opt-1")
        yield Static("3. Abort finish", classes="prompt-option", id="opt-2")

    def _total_options(self) -> int:
        return 3

    def _select_option(self, idx: int) -> None:
        choices = ["commit", "discard", "abort"]
        self._resolve(choices[idx])

    async def wait(self) -> str | None:
        """Returns 'commit', 'discard', or 'abort'. None if cancelled."""
        await super().wait()
        return self._result_value
