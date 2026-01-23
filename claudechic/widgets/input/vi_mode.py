"""Vi-mode state machine and key handling for text input."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from textual.widgets import TextArea


class ViMode(Enum):
    """Vi editor modes."""

    INSERT = auto()
    NORMAL = auto()
    VISUAL = auto()


@dataclass
class ViState:
    """State for vi-mode key handling."""

    mode: ViMode = ViMode.INSERT
    pending_operator: str | None = None  # 'd', 'c', 'y'
    pending_count: str = ""  # Accumulated count digits (e.g., "12" for 12j)
    pending_motion: str | None = None  # 'f', 't', 'F', 'T' waiting for char
    pending_g: bool = False  # Waiting for second char after 'g'
    last_change: tuple[str, ...] | None = None  # For '.' repeat
    yank_buffer: str = ""  # Yanked text for p/P

    def reset_pending(self) -> None:
        """Clear pending state after command execution."""
        self.pending_operator = None
        self.pending_count = ""
        self.pending_motion = None
        self.pending_g = False

    def get_count(self) -> int:
        """Get pending count as int, defaulting to 1."""
        return int(self.pending_count) if self.pending_count else 1


class ViHandler:
    """Handles vi-mode key processing for a TextArea widget."""

    def __init__(self, text_area: TextArea) -> None:
        self.text_area = text_area
        self.state = ViState()
        self._mode_changed_callback: Callable[[ViMode], None] | None = None

    def set_mode_changed_callback(self, callback: Callable[[ViMode], None]) -> None:
        """Set callback to be called when mode changes."""
        self._mode_changed_callback = callback

    def _notify_mode_change(self) -> None:
        """Notify callback of mode change."""
        if self._mode_changed_callback:
            self._mode_changed_callback(self.state.mode)

    def _set_mode(self, mode: ViMode) -> None:
        """Set mode and notify."""
        if self.state.mode != mode:
            self.state.mode = mode
            self._notify_mode_change()

    def _set_selection(self, start: tuple[int, int], end: tuple[int, int]) -> None:
        """Set text area selection using Selection class."""
        from textual.widgets.text_area import Selection

        self.text_area.selection = Selection(start, end)

    def handle_key(self, key: str, character: str | None) -> bool:
        """Handle a key press in vi-mode.

        Returns True if the key was consumed, False if it should be handled normally.
        """
        mode = self.state.mode

        # INSERT mode - only intercept Escape
        if mode == ViMode.INSERT:
            if key == "escape":
                self._set_mode(ViMode.NORMAL)
                self.state.reset_pending()
                # Move cursor back one position (vim behavior)
                row, col = self.text_area.cursor_location
                if col > 0:
                    self.text_area.move_cursor((row, col - 1))
                return True
            return False

        # VISUAL mode
        if mode == ViMode.VISUAL:
            if key == "escape":
                self._set_mode(ViMode.NORMAL)
                loc = self.text_area.cursor_location
                self._set_selection(loc, loc)
                self.state.reset_pending()
                return True
            # Handle visual mode motions (extend selection)
            return self._handle_visual_key(key, character)

        # NORMAL mode
        return self._handle_normal_key(key, character)

    def _handle_normal_key(self, key: str, character: str | None) -> bool:
        """Handle keys in NORMAL mode."""
        state = self.state
        ta = self.text_area

        # Handle pending 'g' commands
        if state.pending_g:
            state.pending_g = False
            if character == "g":
                # gg - go to start
                ta.move_cursor((0, 0))
                state.reset_pending()
                return True
            state.reset_pending()
            return True

        # Handle pending character motion (f/F/t/T)
        if state.pending_motion:
            motion = state.pending_motion
            state.pending_motion = None
            if character:
                self._execute_char_motion(motion, character)
            state.reset_pending()
            return True

        # Accumulate count digits
        if (
            character
            and character.isdigit()
            and (state.pending_count or character != "0")
        ):
            state.pending_count += character
            return True

        # Mode switching
        if character == "i":
            self._set_mode(ViMode.INSERT)
            state.reset_pending()
            return True
        if character == "I":
            ta.action_cursor_line_start()
            self._set_mode(ViMode.INSERT)
            state.reset_pending()
            return True
        if character == "a":
            # Move cursor right before entering insert mode
            row, col = ta.cursor_location
            line = ta.document.get_line(row)
            if col < len(line):
                ta.move_cursor((row, col + 1))
            self._set_mode(ViMode.INSERT)
            state.reset_pending()
            return True
        if character == "A":
            ta.action_cursor_line_end()
            self._set_mode(ViMode.INSERT)
            state.reset_pending()
            return True
        if character == "o":
            ta.action_cursor_line_end()
            ta.insert("\n")
            self._set_mode(ViMode.INSERT)
            state.reset_pending()
            return True
        if character == "O":
            ta.action_cursor_line_start()
            ta.insert("\n")
            ta.action_cursor_up()
            self._set_mode(ViMode.INSERT)
            state.reset_pending()
            return True
        if character == "v":
            self._set_mode(ViMode.VISUAL)
            # Start selection at current position
            loc = ta.cursor_location
            self._set_selection(loc, loc)
            state.reset_pending()
            return True

        # Navigation
        if character == "h" or key == "left":
            for _ in range(state.get_count()):
                ta.action_cursor_left()
            state.reset_pending()
            return True
        if character == "l" or key == "right":
            for _ in range(state.get_count()):
                ta.action_cursor_right()
            state.reset_pending()
            return True
        if character == "j" or key == "down":
            for _ in range(state.get_count()):
                ta.action_cursor_down()
            state.reset_pending()
            return True
        if character == "k" or key == "up":
            for _ in range(state.get_count()):
                ta.action_cursor_up()
            state.reset_pending()
            return True
        if character == "w":
            for _ in range(state.get_count()):
                ta.action_cursor_word_right()
            state.reset_pending()
            return True
        if character == "b":
            for _ in range(state.get_count()):
                ta.action_cursor_word_left()
            state.reset_pending()
            return True
        if character == "e":
            # Move to end of word
            for _ in range(state.get_count()):
                self._move_to_word_end()
            state.reset_pending()
            return True
        if character == "0":
            ta.action_cursor_line_start()
            state.reset_pending()
            return True
        if character == "$":
            ta.action_cursor_line_end()
            state.reset_pending()
            return True
        if character == "^":
            # First non-whitespace character
            ta.action_cursor_line_start()
            row, _ = ta.cursor_location
            line = ta.document.get_line(row)
            for i, ch in enumerate(line):
                if not ch.isspace():
                    ta.move_cursor((row, i))
                    break
            state.reset_pending()
            return True
        if character == "g":
            state.pending_g = True
            return True
        if character == "G":
            ta.move_cursor(ta.document.end)
            state.reset_pending()
            return True

        # Character motions
        if character is not None and character in "fFtT":
            state.pending_motion = character
            return True

        # Operators (d, c, y)
        if character is not None and character in "dcy":
            if state.pending_operator == character:
                # Double operator (dd, cc, yy) - operate on line
                self._execute_line_operator(character)
                state.reset_pending()
                return True
            state.pending_operator = character
            return True

        # Immediate operators with pending operator
        if state.pending_operator:
            op = state.pending_operator
            if character == "w":
                self._execute_operator_motion(op, "word_right")
                state.reset_pending()
                return True
            if character == "b":
                self._execute_operator_motion(op, "word_left")
                state.reset_pending()
                return True
            if character == "$":
                self._execute_operator_motion(op, "line_end")
                state.reset_pending()
                return True
            if character == "0":
                self._execute_operator_motion(op, "line_start")
                state.reset_pending()
                return True
            # Unknown motion - cancel
            state.reset_pending()
            return True

        # Standalone editing commands
        if character == "x":
            # Delete character under cursor
            for _ in range(state.get_count()):
                ta.action_delete_right()
            state.last_change = ("x",)
            state.reset_pending()
            return True
        if character == "X":
            # Delete character before cursor (backspace)
            for _ in range(state.get_count()):
                ta.action_delete_left()
            state.last_change = ("X",)
            state.reset_pending()
            return True
        if character == "D":
            # Delete to end of line
            ta.action_delete_to_end_of_line()
            state.last_change = ("D",)
            state.reset_pending()
            return True
        if character == "C":
            # Change to end of line
            ta.action_delete_to_end_of_line()
            self._set_mode(ViMode.INSERT)
            state.last_change = ("C",)
            state.reset_pending()
            return True
        if character == "s":
            # Substitute character (delete and insert)
            ta.action_delete_right()
            self._set_mode(ViMode.INSERT)
            state.last_change = ("s",)
            state.reset_pending()
            return True
        if character == "S":
            # Substitute line (clear and insert)
            ta.action_cursor_line_start()
            ta.action_delete_line()
            self._set_mode(ViMode.INSERT)
            state.last_change = ("S",)
            state.reset_pending()
            return True

        # Paste
        if character == "p":
            if state.yank_buffer:
                row, col = ta.cursor_location
                line = ta.document.get_line(row)
                # Paste after cursor
                new_col = min(col + 1, len(line))
                ta.move_cursor((row, new_col))
                ta.insert(state.yank_buffer)
            state.reset_pending()
            return True
        if character == "P":
            if state.yank_buffer:
                ta.insert(state.yank_buffer)
            state.reset_pending()
            return True

        # Undo/redo
        if character == "u":
            ta.action_undo()
            state.reset_pending()
            return True
        if key == "ctrl+r":
            ta.action_redo()
            state.reset_pending()
            return True

        # Repeat last change
        if character == ".":
            if state.last_change:
                self._replay_change(state.last_change)
            state.reset_pending()
            return True

        # Join lines
        if character == "J":
            row, _ = ta.cursor_location
            lines = ta.text.split("\n")
            if row < len(lines) - 1:
                # Move to end of current line, delete newline, insert space
                ta.action_cursor_line_end()
                ta.action_delete_right()  # Delete the newline
                # Add space if next line doesn't start with one
                ta.insert(" ")
            state.reset_pending()
            return True

        # Replace single character
        if character == "r":
            # Wait for next character to replace with
            state.pending_motion = "r"  # Reuse pending_motion for this
            return True

        state.reset_pending()
        return True

    def _handle_visual_key(self, key: str, character: str | None) -> bool:
        """Handle keys in VISUAL mode (extend selection)."""
        ta = self.text_area
        state = self.state

        # Get current selection start
        start = ta.selection.start

        # Navigation extends selection
        if character == "h" or key == "left":
            ta.action_cursor_left()
            self._set_selection(start, ta.cursor_location)
            return True
        if character == "l" or key == "right":
            ta.action_cursor_right()
            self._set_selection(start, ta.cursor_location)
            return True
        if character == "j" or key == "down":
            ta.action_cursor_down()
            self._set_selection(start, ta.cursor_location)
            return True
        if character == "k" or key == "up":
            ta.action_cursor_up()
            self._set_selection(start, ta.cursor_location)
            return True
        if character == "w":
            ta.action_cursor_word_right()
            self._set_selection(start, ta.cursor_location)
            return True
        if character == "b":
            ta.action_cursor_word_left()
            self._set_selection(start, ta.cursor_location)
            return True
        if character == "$":
            ta.action_cursor_line_end()
            self._set_selection(start, ta.cursor_location)
            return True
        if character == "0":
            ta.action_cursor_line_start()
            self._set_selection(start, ta.cursor_location)
            return True

        # Get end for operations
        end = ta.selection.end

        # Operators on selection
        if character == "d" or character == "x":
            # Delete selection
            selected = ta.selected_text
            state.yank_buffer = selected
            ta.delete(start, end)
            self._set_mode(ViMode.NORMAL)
            state.reset_pending()
            return True
        if character == "c":
            # Change selection
            selected = ta.selected_text
            state.yank_buffer = selected
            ta.delete(start, end)
            self._set_mode(ViMode.INSERT)
            state.reset_pending()
            return True
        if character == "y":
            # Yank selection
            state.yank_buffer = ta.selected_text
            self._set_selection(start, start)  # Clear selection
            ta.move_cursor(start)
            self._set_mode(ViMode.NORMAL)
            state.reset_pending()
            return True

        return True

    def _move_to_word_end(self) -> None:
        """Move cursor to end of current/next word."""
        ta = self.text_area
        text = ta.text
        row, col = ta.cursor_location

        # Convert to linear position
        lines = text.split("\n")
        pos = sum(len(lines[i]) + 1 for i in range(row)) + col

        # Skip current word if on non-whitespace
        while pos < len(text) and not text[pos].isspace():
            pos += 1
        # Skip whitespace
        while pos < len(text) and text[pos].isspace():
            pos += 1
        # Move to end of word
        while pos < len(text) and not text[pos].isspace():
            pos += 1
        # Back up one to be at last char of word
        if pos > 0:
            pos -= 1

        # Convert back to row, col
        lines = text[:pos].split("\n")
        new_row = len(lines) - 1
        new_col = len(lines[-1]) if lines else 0
        ta.move_cursor((new_row, new_col))

    def _execute_char_motion(self, motion: str, char: str) -> None:
        """Execute f/F/t/T motion to character."""
        ta = self.text_area

        # Handle 'r' for replace
        if motion == "r":
            ta.action_delete_right()
            ta.insert(char)
            # Move cursor back (replace doesn't advance)
            row, col = ta.cursor_location
            if col > 0:
                ta.move_cursor((row, col - 1))
            return

        row, col = ta.cursor_location
        line = ta.document.get_line(row)

        if motion in "ft":
            # Search forward
            idx = line.find(char, col + 1)
            if idx >= 0:
                target = idx if motion == "f" else idx - 1
                ta.move_cursor((row, max(col, target)))
        else:
            # Search backward
            idx = line.rfind(char, 0, col)
            if idx >= 0:
                target = idx if motion == "F" else idx + 1
                ta.move_cursor((row, min(col, target)))

    def _execute_line_operator(self, op: str) -> None:
        """Execute line operator (dd, cc, yy)."""
        ta = self.text_area
        state = self.state
        row, _ = ta.cursor_location

        # Select the entire line
        ta.action_cursor_line_start()
        line_start = ta.cursor_location
        ta.action_cursor_line_end()

        # Include newline if not last line
        lines = ta.text.split("\n")
        if row < len(lines) - 1:
            ta.action_cursor_right()  # Include \n

        line_end = ta.cursor_location

        if op == "y":
            # Yank line
            self._set_selection(line_start, line_end)
            state.yank_buffer = ta.selected_text
            self._set_selection(line_start, line_start)
            ta.move_cursor(line_start)
        elif op == "d":
            # Delete line
            self._set_selection(line_start, line_end)
            state.yank_buffer = ta.selected_text
            ta.delete(line_start, line_end)
            state.last_change = ("dd",)
        elif op == "c":
            # Change line (delete and enter insert mode)
            self._set_selection(line_start, line_end)
            state.yank_buffer = ta.selected_text
            ta.delete(line_start, line_end)
            self._set_mode(ViMode.INSERT)
            state.last_change = ("cc",)

    def _execute_operator_motion(self, op: str, motion: str) -> None:
        """Execute operator with motion (dw, cw, y$, etc.)."""
        ta = self.text_area
        state = self.state
        start = ta.cursor_location

        # Execute motion
        if motion == "word_right":
            ta.action_cursor_word_right()
        elif motion == "word_left":
            ta.action_cursor_word_left()
        elif motion == "line_end":
            ta.action_cursor_line_end()
        elif motion == "line_start":
            ta.action_cursor_line_start()

        end = ta.cursor_location

        # Ensure start < end
        if start > end:
            start, end = end, start

        if op == "y":
            self._set_selection(start, end)
            state.yank_buffer = ta.selected_text
            self._set_selection(start, start)
            ta.move_cursor(start)
        elif op == "d":
            self._set_selection(start, end)
            state.yank_buffer = ta.selected_text
            ta.delete(start, end)
            state.last_change = ("d", motion)
        elif op == "c":
            self._set_selection(start, end)
            state.yank_buffer = ta.selected_text
            ta.delete(start, end)
            self._set_mode(ViMode.INSERT)
            state.last_change = ("c", motion)

    def _replay_change(self, change: tuple[str, ...]) -> None:
        """Replay a recorded change."""
        ta = self.text_area

        if change == ("x",):
            ta.action_delete_right()
        elif change == ("X",):
            ta.action_delete_left()
        elif change == ("D",):
            ta.action_delete_to_end_of_line()
        elif change == ("C",):
            ta.action_delete_to_end_of_line()
            self._set_mode(ViMode.INSERT)
        elif change == ("s",):
            ta.action_delete_right()
            self._set_mode(ViMode.INSERT)
        elif change == ("S",):
            ta.action_cursor_line_start()
            ta.action_delete_line()
            self._set_mode(ViMode.INSERT)
        elif change == ("dd",):
            self._execute_line_operator("d")
        elif change == ("cc",):
            self._execute_line_operator("c")
        elif len(change) == 2 and change[0] == "d":
            self._execute_operator_motion("d", change[1])
        elif len(change) == 2 and change[0] == "c":
            self._execute_operator_motion("c", change[1])
