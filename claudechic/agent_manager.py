"""AgentManager: coordinates multiple concurrent agents."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Callable, Iterator

from claude_agent_sdk import ClaudeAgentOptions

from claudechic.agent import Agent
from claudechic.config import get_claude_model
from claudechic.protocols import AgentManagerObserver, AgentObserver, PermissionHandler

log = logging.getLogger(__name__)


class AgentManager:
    """Coordinates multiple concurrent Claude agents.

    Responsibilities:
    - Create and connect agents
    - Track active agent
    - Switch between agents
    - Close agents cleanly
    - Wire agent callbacks for UI integration

    This class has no UI dependencies - it's pure async coordination.
    """

    def __init__(
        self,
        options_factory: Callable[..., ClaudeAgentOptions],
    ):
        """Initialize the agent manager.

        Args:
            options_factory: Function to create SDK options. Called with
                keyword args: cwd, resume. Should NOT set can_use_tool
                (Agent sets its own permission handler).
        """
        self.agents: dict[str, Agent] = {}
        self.active_id: str | None = None
        self._options_factory = options_factory

        # Protocol-based observers (set by ChatApp)
        self.manager_observer: AgentManagerObserver | None = None
        self.agent_observer: AgentObserver | None = None
        self.permission_handler: PermissionHandler | None = None

    @property
    def active(self) -> Agent | None:
        """Get the currently active agent."""
        if self.active_id and self.active_id in self.agents:
            return self.agents[self.active_id]
        return None

    def get(self, agent_id: str | None = None) -> Agent | None:
        """Get agent by ID, or active agent if None."""
        if agent_id:
            return self.agents.get(agent_id)
        return self.active

    def find_by_name(self, name: str) -> Agent | None:
        """Find agent by name."""
        for agent in self.agents.values():
            if agent.name == name:
                return agent
        return None

    def create_unconnected(
        self,
        name: str,
        cwd: Path,
        *,
        worktree: str | None = None,
        switch_to: bool = True,
    ) -> Agent:
        """Create a new agent without connecting to SDK.

        Use this when you need to populate UI immediately and connect later.
        Call agent.connect() separately to establish SDK connection.

        Args:
            name: Display name for the agent
            cwd: Working directory
            worktree: Git worktree branch name if applicable
            switch_to: Whether to make this the active agent

        Returns:
            The created agent (not yet connected)
        """
        agent = Agent(name=name, cwd=cwd, worktree=worktree)
        agent.model = get_claude_model()

        # Wire callbacks
        self._wire_agent_callbacks(agent)

        # Register agent
        self.agents[agent.id] = agent
        log.info(f"Created agent '{name}' (id={agent.id}, cwd={cwd})")

        if self.manager_observer:
            self.manager_observer.on_agent_created(agent)

        # Switch to new agent if requested or if it's the first agent
        if switch_to or self.active_id is None:
            self.switch(agent.id)

        return agent

    async def create(
        self,
        name: str,
        cwd: Path,
        *,
        worktree: str | None = None,
        resume: str | None = None,
        switch_to: bool = True,
        model: str | None = None,
    ) -> Agent:
        """Create and connect a new agent.

        Args:
            name: Display name for the agent
            cwd: Working directory
            worktree: Git worktree branch name if applicable
            resume: Session ID to resume
            switch_to: Whether to make this the active agent
            model: Model override (None = SDK default)

        Returns:
            The created agent (connected and ready)
        """
        agent = Agent(name=name, cwd=cwd, worktree=worktree)
        agent.model = model if model is not None else get_claude_model()

        # Wire callbacks
        self._wire_agent_callbacks(agent)

        # Create options and connect
        options = self._options_factory(
            cwd=cwd, resume=resume, agent_name=agent.name, model=agent.model
        )
        await agent.connect(options, resume=resume)

        # Register agent
        self.agents[agent.id] = agent
        log.info(f"Created agent '{name}' (id={agent.id}, cwd={cwd})")

        if self.manager_observer:
            self.manager_observer.on_agent_created(agent)

        # Switch to new agent if requested or if it's the first agent
        if switch_to or self.active_id is None:
            self.switch(agent.id)

        return agent

    def _wire_agent_callbacks(self, agent: Agent) -> None:
        """Wire up agent observer and permission handler."""
        agent.observer = self.agent_observer
        agent.permission_handler = self.permission_handler

    def switch(self, agent_id: str) -> bool:
        """Switch to a different agent.

        Args:
            agent_id: ID of agent to switch to

        Returns:
            True if switch succeeded, False if agent not found
        """
        if agent_id not in self.agents:
            log.warning(f"Cannot switch to unknown agent: {agent_id}")
            return False

        old_agent = self.active
        self.active_id = agent_id
        new_agent = self.agents[agent_id]

        log.info(f"Switched to agent '{new_agent.name}' (id={agent_id})")

        if self.manager_observer:
            self.manager_observer.on_agent_switched(new_agent, old_agent)

        return True

    async def close(self, agent_id: str, *, skip_switch: bool = False) -> None:
        """Close an agent and clean up.

        Args:
            agent_id: ID of agent to close
            skip_switch: If True, don't switch to another agent (used by close_all)
        """
        agent = self.agents.pop(agent_id, None)
        if not agent:
            log.warning(f"Cannot close unknown agent: {agent_id}")
            return

        name = agent.name
        was_active = agent_id == self.active_id
        message_count = len(agent.messages)

        # Disconnect
        await agent.disconnect()
        log.info(f"Closed agent '{name}' (id={agent_id})")

        if self.manager_observer:
            self.manager_observer.on_agent_closed(agent_id, message_count)

        # Switch to another agent if we closed the active one
        if not skip_switch and was_active and self.agents:
            next_id = next(iter(self.agents))
            self.switch(next_id)
        elif was_active:
            self.active_id = None

    async def close_all(self) -> None:
        """Close all agents in parallel."""
        agent_ids = list(self.agents.keys())
        results = await asyncio.gather(
            *(self.close(aid, skip_switch=True) for aid in agent_ids),
            return_exceptions=True,
        )
        for aid, result in zip(agent_ids, results):
            if isinstance(result, Exception):
                log.warning(f"Failed to close agent {aid}: {result}")

    def __len__(self) -> int:
        """Number of agents."""
        return len(self.agents)

    def __iter__(self) -> Iterator[Agent]:
        """Iterate over agents."""
        return iter(self.agents.values())

    def __contains__(self, agent_id: str) -> bool:
        """Check if agent exists."""
        return agent_id in self.agents
