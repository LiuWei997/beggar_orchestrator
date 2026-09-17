from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .lifecycle import AgentError
from .messages import Message, normalize_messages
from .providers import LLMProvider

EventHandler = Callable[[dict[str, Any]], Awaitable[None] | None]


@dataclass(frozen=True, slots=True)
class RouteTarget:
    provider: str
    model: str | None = None
    priority: int = 100
    timeout_seconds: float = 30


@dataclass(frozen=True, slots=True)
class Route:
    targets: list[RouteTarget]
    max_attempts: int = 3
    deadline_seconds: float = 60


class ProviderRuntime:
    """Own configured providers, routes, system message, and lifecycle callback."""

    def __init__(
        self,
        *,
        system: str,
        providers: dict[str, LLMProvider],
        routes: dict[str, Route],
        on_event: EventHandler | None = None,
    ):
        if not isinstance(system, str) or not system.strip():
            raise AgentError("Agent requires a non-empty system instruction")
        self.system = system
        self.providers = providers
        self.routes = routes
        self.on_event = on_event

    def prepare_messages(
        self, messages: Iterable[Message | Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        return normalize_messages(self.system, messages)

    async def emit_lifecycle_event(self, event: str, **details: Any) -> None:
        if self.on_event is None:
            return
        result = self.on_event({"event": event, **details})
        if inspect.isawaitable(result):
            await result
