from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Iterable, Mapping
from typing import Any

from .lifecycle import (
    AgentAlreadyStartedError,
    AgentClearedError,
    AgentError,
    AgentSnapshot,
    AgentStateMachine,
    AgentStatus,
    AllProvidersFailed,
    TERMINAL_AGENT_STATUSES,
)
from .messages import Message
from .providers import ProviderError, Response, StreamEvent, StreamEventType
from .runtime import ProviderRuntime


class AgentExecution:
    """Execute exactly one routed LLM request and retain its observable state."""

    def __init__(self, runtime: ProviderRuntime, *, agent_id: str | None = None):
        self._runtime: ProviderRuntime | None = runtime
        self.agent_id = agent_id or str(uuid.uuid4())
        self.route = ""
        self.temperature: float | None = None
        self.max_output_tokens: int | None = None
        self.response_schema: dict[str, Any] | None = None
        self._messages: list[Message | Mapping[str, Any]] = []

        self._state = AgentStateMachine()
        self._provider: str | None = None
        self._model: str | None = None
        self._attempt = 0
        self._created_at = time.time()
        self._started_at: float | None = None
        self._connected_at: float | None = None
        self._ended_at: float | None = None
        self._content_parts: list[str] = []
        self._response: Response | None = None
        self._errors: list[tuple[str, Exception]] = []
        self._cancel_reason: str | None = None
        self._request_configured = False
        self._started = False
        self._cleared = False
        self._context_entered = False
        self._running_task: asyncio.Task[Any] | None = None
        self._active_stream: AsyncIterator[StreamEvent] | None = None

        if not runtime.providers:
            transition = self._state.transition(AgentStatus.INIT_FAILED)
            self._ended_at = transition.occurred_at

    @property
    def system(self) -> str:
        return self._require_runtime().system

    @property
    def status(self) -> AgentStatus:
        self.ensure_available()
        return self._state.status

    @property
    def response(self) -> Response | None:
        self.ensure_available()
        return self._response

    @property
    def is_terminal(self) -> bool:
        return not self._cleared and self._state.is_terminal

    @property
    def is_cleared(self) -> bool:
        return self._cleared

    def snapshot(self) -> AgentSnapshot:
        self.ensure_available()
        now = self._ended_at or time.time()
        return AgentSnapshot(
            agent_id=self.agent_id,
            status=self._state.status,
            route=self.route,
            provider=self._provider,
            model=self._model,
            attempt=self._attempt,
            created_at=self._created_at,
            started_at=self._started_at,
            connected_at=self._connected_at,
            ended_at=self._ended_at,
            elapsed_seconds=0.0 if self._started_at is None else now - self._started_at,
            content="".join(self._content_parts),
            response=self._response,
            errors=tuple(self._errors),
            cancel_reason=self._cancel_reason,
            transitions=self._state.history,
        )

    def record_initialization_error(self, error: Exception) -> None:
        self._errors.append(("initialization", error))

    def configure_request(
        self,
        *,
        route: str,
        messages: Iterable[Message | Mapping[str, Any]],
        temperature: float | None,
        max_output_tokens: int | None,
        response_schema: dict[str, Any] | None,
        request_id: str | None,
    ) -> None:
        self.ensure_available()
        if self._started or self._request_configured:
            raise AgentAlreadyStartedError(
                f"Agent {self.agent_id!r} has already been configured"
            )
        self.route = route
        self._messages = list(messages)
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self.response_schema = response_schema
        if request_id is not None:
            self.agent_id = request_id
        self._request_configured = True

    async def start(self) -> Response:
        response: Response | None = None
        async for event in self.stream():
            if event.type is StreamEventType.COMPLETED:
                response = event.response
        if response is None:
            raise AgentError(f"Agent {self.agent_id!r} ended without a response")
        return response

    async def stream(self) -> AsyncIterator[StreamEvent]:
        self._claim_start()
        self._running_task = asyncio.current_task()
        runtime = self._require_runtime()
        route_policy = runtime.routes.get(self.route)
        if route_policy is None:
            await self.transition_to(AgentStatus.OUT_OF_USAGE)
            raise AgentError(f"Unknown route {self.route!r}")

        prepared_messages = runtime.prepare_messages(self._messages)
        deadline = time.monotonic() + route_policy.deadline_seconds
        timed_out_attempts = 0

        try:
            for target in sorted(route_policy.targets, key=lambda item: item.priority):
                if self._attempt >= route_policy.max_attempts:
                    break
                if time.monotonic() >= deadline:
                    break
                if not runtime.circuit_breaker.allows(target.provider):
                    continue

                provider = runtime.providers.get(target.provider)
                if provider is None:
                    self._errors.append(
                        (target.provider, AgentError("Provider is not configured"))
                    )
                    continue

                selected_model = target.model or provider.model
                await self.transition_to(
                    AgentStatus.ATTEMPT_STARTED,
                    provider=target.provider,
                    model=selected_model,
                    attempt=self._attempt + 1,
                )
                emitted_content = False
                attempt_timeout = min(
                    target.timeout_seconds,
                    max(0.001, deadline - time.monotonic()),
                )

                try:
                    final_response: Response | None = None
                    async with asyncio.timeout(attempt_timeout):
                        async for event in provider.stream(
                            messages=prepared_messages,
                            model=target.model,
                            temperature=self.temperature,
                            max_output_tokens=self.max_output_tokens,
                            response_schema=self.response_schema,
                            timeout=attempt_timeout,
                        ):
                            if event.type is StreamEventType.CONNECTED:
                                await self.transition_to(
                                    AgentStatus.CONNECTED,
                                    provider=event.provider,
                                    model=event.model,
                                )
                                yield event
                            elif event.type is StreamEventType.CONTENT_DELTA:
                                emitted_content = True
                                self._content_parts.append(event.content)
                                yield event
                            else:
                                final_response = event.response
                    if final_response is None:
                        raise ProviderError(
                            "Provider stream ended without a result", retryable=True
                        )
                except TimeoutError as exc:
                    timed_out_attempts += 1
                    attempt_error = ProviderError(
                        "Agent attempt timed out", retryable=True
                    )
                    attempt_error.__cause__ = exc
                except ProviderError as exc:
                    attempt_error = exc
                except asyncio.CancelledError:
                    await self._mark_cancelled()
                    return
                except Exception as exc:
                    attempt_error = ProviderError(
                        "Unexpected provider failure", retryable=False
                    )
                    attempt_error.__cause__ = exc
                else:
                    runtime.circuit_breaker.record_success(target.provider)
                    self._response = final_response
                    await self.transition_to(
                        AgentStatus.RUN_COMPLETED,
                        provider=final_response.provider,
                        model=final_response.model,
                    )
                    yield StreamEvent(
                        StreamEventType.COMPLETED,
                        target.provider,
                        final_response.model,
                        response=final_response,
                    )
                    return

                self._errors.append((target.provider, attempt_error))
                if attempt_error.retryable:
                    runtime.circuit_breaker.record_failure(target.provider)
                if emitted_content:
                    await self.transition_to(AgentStatus.OUT_OF_USAGE)
                    raise attempt_error

            all_failures_were_timeouts = bool(self._errors) and (
                timed_out_attempts == len(self._errors)
            )
            if time.monotonic() >= deadline or all_failures_were_timeouts:
                await self.transition_to(AgentStatus.RUN_TIMED_OUT)
            else:
                await self.transition_to(AgentStatus.OUT_OF_USAGE)
            raise AllProvidersFailed(self.route, self._errors)
        except asyncio.CancelledError:
            await self._mark_cancelled()
            return
        finally:
            self._running_task = None

    async def cancel(self, reason: str = "Cancelled by caller") -> None:
        self.ensure_available()
        if self._state.is_terminal:
            return
        self._cancel_reason = reason
        if not self._started:
            self._started = True
            await self.transition_to(AgentStatus.RUN_CANCELLED)
            return
        running_task = self._running_task
        if running_task is not None and running_task is not asyncio.current_task():
            running_task.cancel()
            await asyncio.gather(running_task, return_exceptions=True)
        await self._mark_cancelled()

    async def clear(self) -> None:
        """Cancel unfinished work and release retained request and result data."""
        self.ensure_available()
        if not self._state.is_terminal:
            await self.cancel(reason="Agent cleared by caller")
        await self.close_active_stream()
        self._messages.clear()
        self._content_parts.clear()
        self._errors.clear()
        self._response = None
        self.response_schema = None
        self._running_task = None
        self._runtime = None
        self._cleared = True

    def register_active_stream(self, stream: AsyncIterator[StreamEvent]) -> None:
        self._active_stream = stream

    def enter_context(self) -> None:
        self.ensure_available()
        if self._context_entered or self._started:
            raise AgentAlreadyStartedError(
                f"Agent {self.agent_id!r} has already been entered or started"
            )
        self._context_entered = True

    async def close_active_stream(self) -> None:
        stream = self._active_stream
        self._active_stream = None
        if stream is not None:
            await stream.aclose()

    def unregister_active_stream(self, stream: AsyncIterator[StreamEvent]) -> None:
        if self._active_stream is stream:
            self._active_stream = None

    async def transition_to(
        self,
        status: AgentStatus,
        *,
        provider: str | None = None,
        model: str | None = None,
        attempt: int | None = None,
    ) -> None:
        transition = self._state.transition(status)
        if provider is not None:
            self._provider = provider
        if model is not None:
            self._model = model
        if attempt is not None:
            self._attempt = attempt
        if status is AgentStatus.ATTEMPT_STARTED and self._started_at is None:
            self._started_at = transition.occurred_at
        if status is AgentStatus.CONNECTED:
            self._connected_at = transition.occurred_at
        if status in TERMINAL_AGENT_STATUSES:
            self._ended_at = transition.occurred_at

        runtime = self._runtime
        if runtime is not None:
            await runtime.emit_lifecycle_event(
                status.value,
                agent_id=self.agent_id,
                route=self.route,
                provider=self._provider,
                model=self._model,
                attempt=self._attempt,
            )

    def ensure_available(self) -> None:
        if self._cleared:
            raise AgentClearedError(f"Agent {self.agent_id!r} has been cleared")

    def _claim_start(self) -> None:
        self.ensure_available()
        if self._started:
            raise AgentAlreadyStartedError(
                f"Agent {self.agent_id!r} has already been started"
            )
        if not self._request_configured:
            raise AgentError("Agent request has not been configured")
        self._started = True
        if self._state.status is AgentStatus.INIT_FAILED:
            raise AgentError("Agent cannot start because no providers are configured")

    def _require_runtime(self) -> ProviderRuntime:
        self.ensure_available()
        if self._runtime is None:
            raise AgentClearedError(f"Agent {self.agent_id!r} has been cleared")
        return self._runtime

    async def _mark_cancelled(self) -> None:
        if not self._state.is_terminal:
            await self.transition_to(AgentStatus.RUN_CANCELLED)
