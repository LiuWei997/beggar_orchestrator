from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum

from .providers import Response


class AgentError(RuntimeError):
    pass


class AgentAlreadyStartedError(AgentError):
    pass


class AgentClearedError(AgentError):
    pass


class InvalidAgentTransitionError(AgentError):
    pass


class AgentStatus(StrEnum):
    RUN_CREATED = "run_created"
    INIT_FAILED = "init_failed"
    ATTEMPT_STARTED = "attempt_started"
    CONNECTED = "connected"
    OUT_OF_USAGE = "out_of_usage"
    RUN_COMPLETED = "run_completed"
    RUN_TIMED_OUT = "run_timed_out"
    RUN_CANCELLED = "run_cancelled"


TERMINAL_AGENT_STATUSES = frozenset(
    {
        AgentStatus.INIT_FAILED,
        AgentStatus.OUT_OF_USAGE,
        AgentStatus.RUN_COMPLETED,
        AgentStatus.RUN_TIMED_OUT,
        AgentStatus.RUN_CANCELLED,
    }
)


@dataclass(frozen=True, slots=True)
class AgentTransition:
    previous: AgentStatus | None
    current: AgentStatus
    occurred_at: float


@dataclass(frozen=True, slots=True)
class AgentSnapshot:
    agent_id: str
    status: AgentStatus
    route: str
    provider: str | None
    model: str | None
    attempt: int
    created_at: float
    started_at: float | None
    connected_at: float | None
    ended_at: float | None
    elapsed_seconds: float
    content: str
    response: Response | None
    errors: tuple[tuple[str, Exception], ...]
    cancel_reason: str | None
    transitions: tuple[AgentTransition, ...]


class AgentStateMachine:
    """Validate and record every lifecycle transition for one Agent."""

    _allowed: dict[AgentStatus, frozenset[AgentStatus]] = {
        AgentStatus.RUN_CREATED: frozenset(
            {
                AgentStatus.INIT_FAILED,
                AgentStatus.ATTEMPT_STARTED,
                AgentStatus.OUT_OF_USAGE,
                AgentStatus.RUN_TIMED_OUT,
                AgentStatus.RUN_CANCELLED,
            }
        ),
        AgentStatus.INIT_FAILED: frozenset(),
        AgentStatus.ATTEMPT_STARTED: frozenset(
            {
                AgentStatus.CONNECTED,
                AgentStatus.OUT_OF_USAGE,
                AgentStatus.RUN_TIMED_OUT,
                AgentStatus.RUN_CANCELLED,
            }
        ),
        AgentStatus.CONNECTED: frozenset(
            {
                AgentStatus.OUT_OF_USAGE,
                AgentStatus.RUN_COMPLETED,
                AgentStatus.RUN_TIMED_OUT,
                AgentStatus.RUN_CANCELLED,
            }
        ),
        AgentStatus.OUT_OF_USAGE: frozenset(),
        AgentStatus.RUN_COMPLETED: frozenset(),
        AgentStatus.RUN_TIMED_OUT: frozenset(),
        AgentStatus.RUN_CANCELLED: frozenset(),
    }

    def __init__(self) -> None:
        self._status = AgentStatus.RUN_CREATED
        self._history = [AgentTransition(None, self._status, time.time())]

    @property
    def status(self) -> AgentStatus:
        return self._status

    @property
    def is_terminal(self) -> bool:
        return self._status in TERMINAL_AGENT_STATUSES

    @property
    def history(self) -> tuple[AgentTransition, ...]:
        return tuple(self._history)

    def transition(self, target: AgentStatus) -> AgentTransition:
        if target not in self._allowed[self._status]:
            raise InvalidAgentTransitionError(
                f"Invalid Agent lifecycle transition: "
                f"{self._status.value} -> {target.value}"
            )
        transition = AgentTransition(self._status, target, time.time())
        self._status = target
        self._history.append(transition)
        return transition
