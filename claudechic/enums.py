"""Enums for magic strings used throughout the codebase."""

from enum import Enum


class StrEnum(str, Enum):
    """String enum base class (compatible with Python < 3.11)."""

    def __str__(self) -> str:
        return self.value


class ToolName(StrEnum):
    """Tool names from Claude Code SDK."""

    # File operations
    EDIT = "Edit"
    WRITE = "Write"
    READ = "Read"
    NOTEBOOK_EDIT = "NotebookEdit"

    # Command execution
    BASH = "Bash"

    # Search tools
    GLOB = "Glob"
    GREP = "Grep"

    # Task management
    TASK = "Task"
    TODO_WRITE = "TodoWrite"

    # Web tools
    WEB_SEARCH = "WebSearch"
    WEB_FETCH = "WebFetch"

    # User interaction
    ASK_USER_QUESTION = "AskUserQuestion"

    # Plan mode
    ENTER_PLAN_MODE = "EnterPlanMode"
    EXIT_PLAN_MODE = "ExitPlanMode"

    # Skills
    SKILL = "Skill"

    # Tool discovery
    TOOL_SEARCH = "ToolSearch"


class AgentStatus(StrEnum):
    """Agent status values."""

    IDLE = "idle"
    BUSY = "busy"
    NEEDS_INPUT = "needs_input"


class PermissionChoice(StrEnum):
    """Permission choice values returned from permission prompts."""

    ALLOW = "allow"
    ALLOW_ALL = "allow_all"
    ALLOW_SESSION = "allow_session"
    DENY = "deny"


class TodoStatus(StrEnum):
    """Todo item status values (from TodoWrite tool)."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
