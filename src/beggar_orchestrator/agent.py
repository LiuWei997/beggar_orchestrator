from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterable, Mapping
from pathlib import Path
from typing import Any

from .execution import AgentExecution
from .lifecycle import AgentError, AgentSnapshot, AgentStatus
from .messages import Message
from .providers import LLMProvider, Response, StreamEvent
from .runtime import EventHandler, ProviderRuntime, Route


class Agent:
    """A single-use LLM conversation configured from ``providers.toml``."""

    def __init__(
        self,
        *,
        system: str,
        config_path: str | Path | None = None,
        on_event: EventHandler | None = None,
        agent_id: str | None = None,
    ):
        if not isinstance(system, str) or not system.strip():
            raise AgentError("Agent requires a non-empty system instruction")

        selected_path = Path(
            config_path or os.getenv("BEGGAR_CONFIG", "providers.toml")
        )
        try:
            from .config import _build_runtime, load_config

            runtime = _build_runtime(
                load_config(selected_path),
                system=system,
                on_event=on_event,
            )
            initialization_error: Exception | None = None
        except Exception as exc:
            runtime = ProviderRuntime(
                system=system,
                providers={},
                routes={},
                on_event=on_event,
            )
            initialization_error = exc

        self.config_path = selected_path
        self._execution = AgentExecution(runtime, agent_id=agent_id)
        if initialization_error is not None:
            self._execution.record_initialization_error(initialization_error)

    @classmethod
    def _for_testing(
        cls,
        *,
        system: str,
        providers: dict[str, LLMProvider],
        routes: dict[str, Route],
        on_event: EventHandler | None = None,
        agent_id: str | None = None,
    ) -> Agent:
        instance = cls.__new__(cls)
        instance.config_path = None
        runtime = ProviderRuntime(
            system=system,
            providers=providers,
            routes=routes,
            on_event=on_event,
        )
        instance._execution = AgentExecution(runtime, agent_id=agent_id)
        return instance

    @property
    def system(self) -> str:
        return self._execution.system

    @property
    def agent_id(self) -> str:
        return self._execution.agent_id

    @property
    def status(self) -> AgentStatus:
        return self._execution.status

    @property
    def response(self) -> Response | None:
        return self._execution.response

    @property
    def is_terminal(self) -> bool:
        return self._execution.is_terminal

    @property
    def is_cleared(self) -> bool:
        return self._execution.is_cleared

    def snapshot(self) -> AgentSnapshot:
        return self._execution.snapshot()

    async def start(
        self,
        *,
        route: str,
        provider: str,
        messages: Iterable[Message | Mapping[str, Any]],
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        response_schema: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> Response:
        self._configure_request(
            route=route,
            provider=provider,
            messages=messages,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            response_schema=response_schema,
            request_id=request_id,
        )
        return await self._execution.start()

    async def stream(
        self,
        *,
        route: str,
        provider: str,
        messages: Iterable[Message | Mapping[str, Any]],
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        response_schema: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self._configure_request(
            route=route,
            provider=provider,
            messages=messages,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            response_schema=response_schema,
            request_id=request_id,
        )
        stream = self._execution.stream()
        self._execution.register_active_stream(stream)
        try:
            async for event in stream:
                yield event
        finally:
            await stream.aclose()
            self._execution.unregister_active_stream(stream)

    async def __aenter__(self) -> Agent:
        self._execution.enter_context()
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> bool:
        await self._execution.close_active_stream()
        if not self._execution.is_terminal:
            reason = (
                f"Context exited after {exc_type.__name__}"
                if exc_type is not None
                else "Context exited before completion"
            )
            await self.cancel(reason)
        return False

    async def cancel(self, reason: str = "Cancelled by caller") -> None:
        await self._execution.cancel(reason)

    async def clear(self) -> None:
        await self._execution.clear()

    def _configure_request(
        self,
        *,
        route: str,
        provider: str,
        messages: Iterable[Message | Mapping[str, Any]],
        temperature: float | None,
        max_output_tokens: int | None,
        response_schema: dict[str, Any] | None,
        request_id: str | None,
    ) -> None:
        self._execution.configure_request(
            route=route,
            provider=provider,
            messages=messages,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            response_schema=response_schema,
            request_id=request_id,
        )
