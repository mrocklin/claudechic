"""Textual widgets for Claude Code UI.

Re-exports all widgets from submodules for backward compatibility.
"""

# Base classes
from claudechic.widgets.base import ToolWidget

# Primitives
from claudechic.widgets.primitives import (
    Button,
    QuietCollapsible,
    AutoHideScroll,
    Spinner,
)

# Content widgets
from claudechic.widgets.content import (
    ChatMessage,
    ChatInput,
    ThinkingIndicator,
    ConnectingIndicator,
    ImageAttachments,
    ErrorMessage,
    SystemInfo,
    ChatAttachment,
    ToolUseWidget,
    TaskWidget,
    AgentToolWidget,
    AgentListWidget,
    ShellOutputWidget,
    PendingShellWidget,
    EditPlanRequested,
    DiffWidget,
    TodoWidget,
    TodoPanel,
)

# Input widgets
from claudechic.widgets.input import TextAreaAutoComplete, HistorySearch

# Layout widgets
from claudechic.widgets.layout import (
    ChatView,
    AgentItem,
    AgentSection,
    WorktreeItem,
    PlanItem,
    PlanSection,
    FileItem,
    FilesSection,
    SidebarSection,
    SidebarItem,
    HamburgerButton,
    SessionItem,
    PermissionModeLabel,
    ModelLabel,
    StatusFooter,
    IndicatorWidget,
    CPUBar,
    ContextBar,
    ProcessIndicator,
    ProcessPanel,
    ProcessItem,
    ReviewPanel,
    ReviewItem,
)

# Base re-exports (ClickableLabel used by layout widgets)
from claudechic.widgets.base import ClickableLabel

# Data classes (re-exported for convenience)
from claudechic.processes import BackgroundProcess

# Report widgets
from claudechic.widgets.reports import UsageReport, ContextReport

# Modal screens
from claudechic.widgets.modals import ProfileModal, ProcessModal

# Prompts
from claudechic.widgets.prompts import (
    BasePrompt,
    SelectionPrompt,
    QuestionPrompt,
    ModelPrompt,
    EffortPrompt,
    WorktreePrompt,
    UncommittedChangesPrompt,
)

__all__ = [
    # Base
    "ToolWidget",
    # Primitives
    "Button",
    "QuietCollapsible",
    "AutoHideScroll",
    "Spinner",
    # Content
    "ChatMessage",
    "ChatInput",
    "ThinkingIndicator",
    "ConnectingIndicator",
    "ImageAttachments",
    "ErrorMessage",
    "SystemInfo",
    "ChatAttachment",
    "ToolUseWidget",
    "TaskWidget",
    "AgentToolWidget",
    "AgentListWidget",
    "ShellOutputWidget",
    "PendingShellWidget",
    "EditPlanRequested",
    "DiffWidget",
    "TodoWidget",
    "TodoPanel",
    # Input
    "TextAreaAutoComplete",
    "HistorySearch",
    # Layout
    "ChatView",
    "AgentItem",
    "AgentSection",
    "WorktreeItem",
    "SessionItem",
    "PlanItem",
    "PlanSection",
    "FileItem",
    "FilesSection",
    "SidebarSection",
    "SidebarItem",
    "HamburgerButton",
    "ClickableLabel",
    "PermissionModeLabel",
    "ModelLabel",
    "StatusFooter",
    "IndicatorWidget",
    "CPUBar",
    "ContextBar",
    "ProcessIndicator",
    "ProcessPanel",
    "ProcessItem",
    "ReviewPanel",
    "ReviewItem",
    "BackgroundProcess",
    # Reports
    "UsageReport",
    "ContextReport",
    # Modals
    "ProfileModal",
    "ProcessModal",
    # Prompts
    "BasePrompt",
    "SelectionPrompt",
    "QuestionPrompt",
    "ModelPrompt",
    "EffortPrompt",
    "WorktreePrompt",
    "UncommittedChangesPrompt",
]
